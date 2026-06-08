from __future__ import annotations

import os
import sqlite3
import json
import bcrypt
import logging
from datetime import datetime
from pathlib import Path

DB_PATH = "ytx_metrics.db"
SUPER_ADMIN_EMAIL = "hello@atlasnow.co"

# Setup logging for super admin protection attempts
logging.basicConfig(
    filename="super_admin_protection.log",
    level=logging.WARNING,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def get_db_connection():
    """
    Returns a database connection.
    - Uses PostgreSQL if DATABASE_URL is set (for deployment on Render, Railway, etc.)
    - Falls back to local SQLite for development.
    """
    database_url = os.getenv("DATABASE_URL")

    if database_url and database_url.startswith("postgres"):
        import psycopg2
        from psycopg2.extras import DictCursor
        # Render/Railway often give postgres:// but psycopg2 prefers postgresql://
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(database_url)
        conn.cursor_factory = DictCursor
        return conn
    else:
        # Local SQLite
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


def _get_placeholder():
    """Return the correct placeholder style for the current DB."""
    database_url = os.getenv("DATABASE_URL", "")
    return "%s" if database_url.startswith("postgres") else "?"

def _execute(conn, query, params=()):
    """Execute with correct placeholder style."""
    ph = _get_placeholder()
    # Replace all ? with the correct placeholder for safety
    if ph == "%s":
        query = query.replace("?", "%s")
    cursor = conn.cursor()
    cursor.execute(query, params)
    return cursor


def ensure_tables():
    """Paranoid helper: ensure all tables exist (calls init if needed).
    Safe to call from any function; uses try to avoid recursion issues.
    """
    try:
        init_db()
        init_payment_gateways_table()
    except Exception:
        pass  # don't crash the caller if init has transient issues

def init_db():
    """Initialize database tables if they don't exist."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Users table
    is_postgres = bool(os.getenv("DATABASE_URL", "").startswith("postgres"))
    if is_postgres:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                quota_used INTEGER DEFAULT 0,
                is_subscribed INTEGER DEFAULT 0,
                is_admin INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                youtube_calls INTEGER DEFAULT 0,
                x_calls INTEGER DEFAULT 0,
                grok_calls INTEGER DEFAULT 0,
                grok_tokens INTEGER DEFAULT 0
            )
        """)
        conn.commit()  # explicit commit after CREATE for Postgres
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                quota_used INTEGER DEFAULT 0,
                is_subscribed INTEGER DEFAULT 0,
                is_admin INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                youtube_calls INTEGER DEFAULT 0,
                x_calls INTEGER DEFAULT 0,
                grok_calls INTEGER DEFAULT 0,
                grok_tokens INTEGER DEFAULT 0
            )
        """)
        conn.commit()  # explicit commit after CREATE
    conn.commit()  # explicit commit after DDL for Postgres paranoia

    # Force super admin right after users table is created (same connection/transaction)
    # This UPDATE happens on the exact same cursor right after CREATE, so table is guaranteed to exist.
    ph = "%s" if is_postgres else "?"
    try:
        cursor.execute(
            f"UPDATE users SET is_admin = 1, is_subscribed = 1 WHERE email = {ph}",
            ("hello@atlasnow.co",)
        )
    except Exception:
        pass  # if row doesn't exist yet, fine (will be set on first signup via create_user)

    # Projects table - stores user's video tables or X profile lists
    if is_postgres:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                parent_id INTEGER,
                is_folder INTEGER DEFAULT 0,
                name TEXT NOT NULL,
                project_type TEXT DEFAULT 'youtube',
                data_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
                FOREIGN KEY (parent_id) REFERENCES projects (id) ON DELETE CASCADE
            )
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                parent_id INTEGER,
                is_folder INTEGER DEFAULT 0,
                name TEXT NOT NULL,
                project_type TEXT DEFAULT 'youtube',
                data_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
                FOREIGN KEY (parent_id) REFERENCES projects (id) ON DELETE CASCADE
            )
        """)
    conn.commit()  # explicit commit after projects CREATE for Postgres paranoia

    # Migration for existing DBs (works for both SQLite and Postgres)
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
    except Exception:
        pass

    try:
        cursor.execute("ALTER TABLE projects ADD COLUMN parent_id INTEGER")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE projects ADD COLUMN is_folder INTEGER DEFAULT 0")
    except Exception:
        pass

    try:
        cursor.execute("ALTER TABLE projects ADD COLUMN project_type TEXT DEFAULT 'youtube'")
    except Exception:
        pass

    # Robust migration for usage columns - works even on old schemas or after failed previous inits
    for col in ["youtube_calls", "x_calls", "grok_calls", "grok_tokens"]:
        try:
            if is_postgres:
                # Use DO block to add column only if it doesn't exist - avoids "already exists" errors and transaction issues
                cursor.execute(f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns 
                            WHERE table_name='users' AND column_name='{col}'
                        ) THEN
                            ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0;
                        END IF;
                    END $$;
                """)
            else:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0")
            conn.commit()  # commit immediately after each successful ADD (Postgres safety)
        except Exception:
            conn.rollback()  # reset transaction state on error for Postgres
            pass  # ignore if can't add (e.g. permission or already there)

    conn.commit()  # final commit after all migrations/DDL
    conn.close()
    print("Database initialized.")

    # Extra paranoid commit in case of partial DDL in some Postgres setups
    try:
        conn = get_db_connection()
        conn.commit()
        conn.close()
    except Exception:
        pass

    # One more explicit ensure for the usage columns migration (idempotent)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        for col in ["youtube_calls", "x_calls", "grok_calls", "grok_tokens"]:
            if bool(os.getenv("DATABASE_URL", "").startswith("postgres")):
                cursor.execute(f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns 
                            WHERE table_name='users' AND column_name='{col}'
                        ) THEN
                            ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0;
                        END IF;
                    END $$;
                """)
            else:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0")
            conn.commit()
        conn.close()
    except Exception:
        pass

def hash_password(password: str) -> str:
    """Hash password using bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    """Verify password against hash."""
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

# --- User functions ---
def create_user(email: str, password: str) -> int | None:
    """Create a new user. Returns user_id or None if email exists."""
    ensure_tables()  # paranoid for Postgres
    conn = get_db_connection()
    try:
        password_hash = hash_password(password)
        email_clean = email.lower().strip()
        is_admin = 1 if email_clean == "hello@atlasnow.co" else 0
        is_sub = is_admin
        ph = _get_placeholder()
        cursor = _execute(
            conn,
            f"INSERT INTO users (email, password_hash, is_subscribed, is_admin) VALUES ({ph}, {ph}, {ph}, {ph})",
            (email_clean, password_hash, is_sub, is_admin)
        )
        conn.commit()
        # lastrowid works for sqlite, for postgres we use RETURNING
        if ph == "%s":
            # For postgres, re-fetch or use RETURNING in future
            cursor.execute(f"SELECT id FROM users WHERE email = {ph}", (email_clean,))
            row = cursor.fetchone()
            return row["id"] if row else None
        else:
            return cursor.lastrowid
    except Exception as e:
        if "unique" in str(e).lower() or "integrity" in str(e).lower():
            return None
        raise
    finally:
        conn.close()

def get_user_by_email(email: str):
    """Get user row by email."""
    conn = get_db_connection()
    ph = _get_placeholder()
    cursor = _execute(conn, f"SELECT * FROM users WHERE email = {ph}", (email.lower().strip(),))
    user = cursor.fetchone()
    conn.close()
    return dict(user) if user else None

def get_user_by_id(user_id: int):
    conn = get_db_connection()
    ph = _get_placeholder()
    cursor = _execute(conn, f"SELECT * FROM users WHERE id = {ph}", (user_id,))
    user = cursor.fetchone()
    conn.close()
    return dict(user) if user else None

def update_user_quota(user_id: int, additional: int):
    """Add to the user's quota_used."""
    ensure_tables()  # paranoid for Postgres
    conn = get_db_connection()
    ph = _get_placeholder()
    cursor = _execute(
        conn,
        f"UPDATE users SET quota_used = quota_used + {ph} WHERE id = {ph}",
        (additional, user_id)
    )
    conn.commit()
    conn.close()

def set_user_subscribed(user_id: int, subscribed: bool = True):
    conn = get_db_connection()
    ph = _get_placeholder()
    cursor = _execute(
        conn,
        f"UPDATE users SET is_subscribed = {ph} WHERE id = {ph}",
        (1 if subscribed else 0, user_id)
    )
    conn.commit()
    conn.close()

def get_user_quota(user_id: int) -> dict:
    """Return quota info for user."""
    user = get_user_by_id(user_id)
    if not user:
        return {"used": 0, "limit": 200, "remaining": 200, "is_subscribed": False, "is_admin": False}
    used = user.get("quota_used", 0)
    is_sub = bool(user.get("is_subscribed", 0))
    is_admin = bool(user.get("is_admin", 0))
    limit = 999999 if (is_sub or is_admin) else 200  # unlimited for admin too
    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "is_subscribed": is_sub,
        "is_admin": is_admin
    }

# --- Project functions ---
def create_project(user_id: int, name: str, parent_id: int = None, is_folder: int = 0, project_type: str = "youtube") -> int:
    """Create a new empty project/folder for user. Returns project_id."""
    ensure_tables()  # paranoid for Postgres
    conn = get_db_connection()
    now = datetime.now().isoformat()
    data_default = "[]" if not is_folder else None
    ph = _get_placeholder()
    cursor = _execute(
        conn,
        f"INSERT INTO projects (user_id, parent_id, is_folder, name, project_type, data_json, created_at, updated_at) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})",
        (user_id, parent_id, is_folder, name, project_type, data_default, now, now)
    )
    conn.commit()
    if ph == "%s":
        cursor.execute(f"SELECT id FROM projects WHERE user_id = {ph} AND name = {ph} ORDER BY id DESC LIMIT 1", (user_id, name))
        row = cursor.fetchone()
        project_id = row["id"] if row else None
    else:
        project_id = cursor.lastrowid
    conn.close()
    return project_id

def get_user_projects(user_id: int):
    """List all projects/folders for a user (flat, with parent info for tree building)."""
    conn = get_db_connection()
    ph = _get_placeholder()
    cursor = _execute(
        conn,
        f"SELECT id, parent_id, is_folder, name, project_type, updated_at FROM projects WHERE user_id = {ph} ORDER BY name ASC",
        (user_id,)
    )
    projects = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return projects

def get_project(project_id: int, user_id: int):
    """Get a single project (with ownership check)."""
    conn = get_db_connection()
    ph = _get_placeholder()
    cursor = _execute(
        conn,
        f"SELECT * FROM projects WHERE id = {ph} AND user_id = {ph}",
        (project_id, user_id)
    )
    project = cursor.fetchone()
    conn.close()
    return dict(project) if project else None

def save_project_data(project_id: int, user_id: int, data_json: str):
    """Save the table data (JSON string) to a project."""
    ensure_tables()  # paranoid for Postgres
    conn = get_db_connection()
    now = datetime.now().isoformat()
    ph = _get_placeholder()
    cursor = _execute(
        conn,
        f"UPDATE projects SET data_json = {ph}, updated_at = {ph} WHERE id = {ph} AND user_id = {ph}",
        (data_json, now, project_id, user_id)
    )
    conn.commit()
    conn.close()

def load_project_data(project_id: int, user_id: int) -> list:
    """Load the table data as list of dicts."""
    project = get_project(project_id, user_id)
    if not project or not project.get("data_json"):
        return []
    try:
        return json.loads(project["data_json"])
    except:
        return []

def delete_project(project_id: int, user_id: int):
    """Delete project/folder and all its children recursively."""
    ensure_tables()  # paranoid for Postgres
    conn = get_db_connection()
    ph = _get_placeholder()
    cursor = conn.cursor()

    def delete_recursive(pid):
        cursor.execute(f"SELECT id FROM projects WHERE parent_id = {ph} AND user_id = {ph}", (pid, user_id))
        children = [row[0] for row in cursor.fetchall()]
        for child in children:
            delete_recursive(child)
        cursor.execute(
            f"DELETE FROM projects WHERE id = {ph} AND user_id = {ph}",
            (pid, user_id)
        )
    delete_recursive(project_id)
    conn.commit()
    conn.close()

def rename_project(project_id: int, user_id: int, new_name: str):
    """Rename a project (with ownership check)."""
    if not new_name or not new_name.strip():
        return False
    ensure_tables()  # paranoid for Postgres
    conn = get_db_connection()
    now = datetime.now().isoformat()
    ph = _get_placeholder()
    cursor = _execute(
        conn,
        f"UPDATE projects SET name = {ph}, updated_at = {ph} WHERE id = {ph} AND user_id = {ph}",
        (new_name.strip(), now, project_id, user_id)
    )
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success

def set_user_admin(email: str, is_admin: bool = True):
    """Set super admin status for a user by email. Also sets is_subscribed for unlimited.
    The main super admin (hello@atlasnow.co) cannot be demoted.
    """
    email_clean = email.lower().strip()

    if email_clean == SUPER_ADMIN_EMAIL and not is_admin:
        # Hardcoded protection: super admin cannot be demoted
        logging.warning(f"Blocked attempt to demote super admin {email_clean}")
        return False

    # Paranoid: ensure tables exist before any operation on Postgres
    try:
        init_db()
    except Exception:
        pass

    conn = get_db_connection()
    ph = _get_placeholder()
    cursor = _execute(
        conn,
        f"UPDATE users SET is_admin = {ph}, is_subscribed = {ph} WHERE email = {ph}",
        (1 if is_admin else 0, 1 if is_admin else 0, email_clean)
    )
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success

def get_all_users() -> list:
    """Return all users for admin panel (sorted by created_at desc).
    Defensive: uses SELECT * + setdefault so it doesn't crash if the
    usage columns (youtube_calls etc.) haven't been added by migration yet
    (e.g. on old DB schemas from previous deploys).
    The migration in init_db() will add the columns on startup.
    """
    ensure_tables()  # extra safety
    conn = get_db_connection()
    cursor = _execute(conn, "SELECT * FROM users ORDER BY created_at DESC")
    users = []
    for row in cursor.fetchall():
        user = dict(row)
        # Provide defaults for columns that may be missing in old DBs
        user.setdefault('youtube_calls', 0)
        user.setdefault('x_calls', 0)
        user.setdefault('grok_calls', 0)
        user.setdefault('grok_tokens', 0)
        users.append(user)
    conn.close()
    return users

def increment_user_api_usage(user_id: int, youtube: int = 0, x: int = 0, grok: int = 0, grok_tokens: int = 0):
    """Persistently increment per-user API usage counters.
    Wrapped in try/except so missing columns on legacy DBs (e.g. Render Postgres from old deploys)
    do not kill the fetch operation. Main quota_used is still updated separately.
    """
    if not user_id:
        return
    ensure_tables()  # paranoid for Postgres
    conn = get_db_connection()
    ph = _get_placeholder()
    try:
        cursor = _execute(
            conn,
            f"""
            UPDATE users SET
                youtube_calls = COALESCE(youtube_calls, 0) + {ph},
                x_calls = COALESCE(x_calls, 0) + {ph},
                grok_calls = COALESCE(grok_calls, 0) + {ph},
                grok_tokens = COALESCE(grok_tokens, 0) + {ph}
            WHERE id = {ph}
            """,
            (youtube, x, grok, grok_tokens, user_id)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        # Do not crash the fetch; granular counters are nice-to-have
        # The main quota_used update in update_user_quota will still succeed
        print(f"[DB] Warning: could not increment granular usage for user {user_id}: {e}")
    finally:
        conn.close()

def reset_user_usage(user_id: int):
    """Reset all usage counters and quota for a user (admin action)."""
    ensure_tables()  # paranoid for Postgres
    conn = get_db_connection()
    ph = _get_placeholder()
    cursor = _execute(
        conn,
        f"""
        UPDATE users SET
            quota_used = 0,
            youtube_calls = 0,
            x_calls = 0,
            grok_calls = 0,
            grok_tokens = 0
        WHERE id = {ph}
        """,
        (user_id,)
    )
    conn.commit()
    conn.close()

def set_user_premium(user_id: int, is_premium: bool = True):
    """Manually activate/revoke premium for a user.
    Super admin cannot have premium revoked.
    """
    ensure_tables()  # paranoid for Postgres
    user = get_user_by_id(user_id)
    if user and user.get("email", "").lower() == SUPER_ADMIN_EMAIL and not is_premium:
        logging.warning(f"Blocked attempt to revoke premium from super admin user_id={user_id}")
        return False

    conn = get_db_connection()
    ph = _get_placeholder()
    cursor = _execute(
        conn,
        f"UPDATE users SET is_subscribed = {ph} WHERE id = {ph}",
        (1 if is_premium else 0, user_id)
    )
    conn.commit()
    conn.close()
    return True


def delete_user(user_id: int) -> bool:
    """Delete a user from the database.
    Super admin (hardcoded) cannot be deleted under any circumstances.
    """
    user = get_user_by_id(user_id)
    if user and user.get("email", "").lower() == SUPER_ADMIN_EMAIL:
        logging.warning(f"Blocked attempt to DELETE super admin from database. user_id={user_id}")
        return False

    # Hardcoded protection in query: explicitly exclude super admin email
    conn = get_db_connection()
    ph = _get_placeholder()
    cursor = _execute(
        conn,
        f"DELETE FROM users WHERE id = {ph} AND email != {ph}",
        (user_id, SUPER_ADMIN_EMAIL)
    )
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if success:
        logging.info(f"User deleted successfully: user_id={user_id}")
    return success


# --- Payment Gateway Config Functions ---

def get_payment_gateways():
    """Return all payment gateways with their config."""
    conn = get_db_connection()
    ph = _get_placeholder()
    cursor = _execute(
        conn,
        f"SELECT gateway, is_active, config, updated_at FROM payment_gateways ORDER BY gateway"
    )
    gateways = []
    for row in cursor.fetchall():
        gw = dict(row)
        try:
            gw['config'] = json.loads(gw['config']) if gw.get('config') else {}
        except:
            gw['config'] = {}
        gateways.append(gw)
    conn.close()
    return gateways


def save_payment_gateway(gateway: str, is_active: bool, config: dict):
    """Save or update a payment gateway config."""
    ensure_tables()  # paranoid for Postgres
    conn = get_db_connection()
    ph = _get_placeholder()
    config_json = json.dumps(config or {})
    now = datetime.now().isoformat()

    cursor = _execute(
        conn,
        f"""
        UPDATE payment_gateways 
        SET is_active = {ph}, config = {ph}, updated_at = {ph}
        WHERE gateway = {ph}
        """,
        (1 if is_active else 0, config_json, now, gateway)
    )

    if cursor.rowcount == 0:
        # Insert if not exists
        _execute(
            conn,
            f"INSERT INTO payment_gateways (gateway, is_active, config) VALUES ({ph}, {ph}, {ph})",
            (gateway, 1 if is_active else 0, config_json)
        )

    conn.commit()
    conn.close()
    return True


def get_active_payment_gateways():
    """Return only active payment gateways."""
    all_gw = get_payment_gateways()
    return [gw for gw in all_gw if gw.get('is_active')]

def init_payment_gateways_table():
    """Create payment gateways config table if not exists."""
    conn = get_db_connection()
    cursor = conn.cursor()
    is_postgres = bool(os.getenv("DATABASE_URL", "").startswith("postgres"))

    if is_postgres:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS payment_gateways (
                id SERIAL PRIMARY KEY,
                gateway TEXT UNIQUE NOT NULL,
                is_active INTEGER DEFAULT 0,
                config TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()  # explicit commit after DDL for Postgres paranoia

        # Seed default gateways if not exist (Postgres syntax)
        default_gateways = ['stripe', 'paypal', 'xendit']
        for gw in default_gateways:
            cursor.execute(
                "INSERT INTO payment_gateways (gateway, is_active, config) VALUES (%s, 0, '{}') ON CONFLICT (gateway) DO NOTHING",
                (gw,)
            )
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS payment_gateways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gateway TEXT UNIQUE NOT NULL,
                is_active INTEGER DEFAULT 0,
                config TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()  # explicit commit after DDL

        # Seed default gateways if not exist (SQLite)
        default_gateways = ['stripe', 'paypal', 'xendit']
        for gw in default_gateways:
            cursor.execute(
                "INSERT OR IGNORE INTO payment_gateways (gateway, is_active, config) VALUES (?, 0, '{}')",
                (gw,)
            )

    conn.commit()
    conn.close()


# Initialize on import (wrapped for robustness on production deploys like Render)
if __name__ != "__main__":
    try:
        init_db()
        init_payment_gateways_table()
    except Exception as e:
        print(f"[DB] Warning: init failed (may be transient): {e}")
        # App can still start; functions will retry via ensure_tables()
