from __future__ import annotations

import os
import sqlite3
import json
import bcrypt
from datetime import datetime
from pathlib import Path

DB_PATH = "ytx_metrics.db"

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

def init_db():
    """Initialize database tables if they don't exist."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            quota_used INTEGER DEFAULT 0,
            is_subscribed INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Projects table - stores user's video tables or X profile lists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            parent_id INTEGER,  -- for tree structure (folders/projects hierarchy)
            is_folder INTEGER DEFAULT 0,
            name TEXT NOT NULL,
            project_type TEXT DEFAULT 'youtube',  -- 'youtube' or 'x_profile'
            data_json TEXT,  -- JSON array of rows from the pandas DF
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY (parent_id) REFERENCES projects (id) ON DELETE CASCADE
        )
    """)

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

    for col in ["youtube_calls", "x_calls", "grok_calls", "grok_tokens"]:
        try:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0")
        except Exception:
            pass

    conn.commit()
    conn.close()
    print("Database initialized.")

def hash_password(password: str) -> str:
    """Hash password using bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    """Verify password against hash."""
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

# --- User functions ---
def create_user(email: str, password: str) -> int | None:
    """Create a new user. Returns user_id or None if email exists."""
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
    """Set super admin status for a user by email. Also sets is_subscribed for unlimited."""
    conn = get_db_connection()
    ph = _get_placeholder()
    cursor = _execute(
        conn,
        f"UPDATE users SET is_admin = {ph}, is_subscribed = {ph} WHERE email = {ph}",
        (1 if is_admin else 0, 1 if is_admin else 0, email.lower().strip())
    )
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success

def get_all_users() -> list:
    """Return all users for admin panel (sorted by created_at desc)."""
    conn = get_db_connection()
    ph = _get_placeholder()
    cursor = _execute(
        conn,
        f"""
        SELECT id, email, created_at, quota_used, is_subscribed, is_admin,
               COALESCE(youtube_calls, 0) as youtube_calls,
               COALESCE(x_calls, 0) as x_calls,
               COALESCE(grok_calls, 0) as grok_calls,
               COALESCE(grok_tokens, 0) as grok_tokens
        FROM users
        ORDER BY created_at DESC
        """
    )
    users = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return users

def increment_user_api_usage(user_id: int, youtube: int = 0, x: int = 0, grok: int = 0, grok_tokens: int = 0):
    """Persistently increment per-user API usage counters."""
    if not user_id:
        return
    conn = get_db_connection()
    ph = _get_placeholder()
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
    conn.close()

def reset_user_usage(user_id: int):
    """Reset all usage counters and quota for a user (admin action)."""
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
    """Manually activate/revoke premium for a user."""
    conn = get_db_connection()
    ph = _get_placeholder()
    cursor = _execute(
        conn,
        f"UPDATE users SET is_subscribed = {ph} WHERE id = {ph}",
        (1 if is_premium else 0, user_id)
    )
    conn.commit()
    conn.close()

# Initialize on import
if __name__ != "__main__":
    init_db()
    # One-time upgrade for super admin email if the user already exists
    set_user_admin("hello@atlasnow.co", True)
