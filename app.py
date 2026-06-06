from __future__ import annotations

# Suppress noisy FutureWarnings from Google libraries about Python 3.9 EOL.
# This is a temporary workaround. The proper fix is to upgrade to Python 3.10+.
import warnings
warnings.filterwarnings("ignore", category=FutureWarning, module="google")

import streamlit as st
from dotenv import load_dotenv
import os
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import tweepy
import pandas as pd
from datetime import datetime, timedelta
import time
from typing import Optional, List
import json
import re
import requests

import database as db

# Page config
st.set_page_config(
    page_title="Metric Reports",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Load environment
load_dotenv()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PAYMENT_LINK = os.getenv("STRIPE_PAYMENT_LINK", "")

# Ensure DB is ready (runs on import in database.py too)
db.init_db()

# ==================== AUTH STATE ====================
if "user" not in st.session_state:
    st.session_state.user = None  # dict with id, email, etc.

if "current_project_id" not in st.session_state:
    st.session_state.current_project_id = None

if "editing_project_id" not in st.session_state:
    st.session_state.editing_project_id = None

if "yt_bulk_df" not in st.session_state:
    # Will be loaded from project
    st.session_state.yt_bulk_df = pd.DataFrame({
        "url": [""], "title": [""], "views": [0], "likes": [0], "comments": [0], "last_fetched": [""]
    })

if "x_bulk_df" not in st.session_state:
    # X Profile checker data
    st.session_state.x_bulk_df = pd.DataFrame({
        "url": [""],
        "username": [""],
        "name": [""],
        "followers": [0],
        "verified": [""],
        "niche": [""],
        "last_fetched": [""],
    })

if "channel_bulk_df" not in st.session_state:
    # YouTube Channel Fetch bulk table
    ch_init = {
        "channel_url": [""],
        "title": [""],
        "subscribers": [0],
        "total_videos": [0],
        "total_views": [0],
        "last_fetched": [""],
        "category": [""],
    }
    st.session_state.channel_bulk_df = pd.DataFrame(ch_init).astype({
        "subscribers": "int64",
        "total_videos": "int64",
        "total_views": "int64",
    })

if "current_project_type" not in st.session_state:
    st.session_state.current_project_type = "youtube"

if "current_platform" not in st.session_state:
    st.session_state.current_platform = "youtube"  # legacy, project_type is the source of truth now

# API Usage Monitoring (for Settings > API Quota)
if "api_usage" not in st.session_state:
    st.session_state.api_usage = {
        "youtube_calls": 0,
        "x_api_calls": 0,
        "grok_calls": 0,
        "grok_total_tokens": 0,
    }
if "last_x_rate_limit" not in st.session_state:
    st.session_state.last_x_rate_limit = {"remaining": None, "limit": None, "reset": None}

def login_user(email: str, password: str) -> bool:
    user = db.get_user_by_email(email)
    if user and db.verify_password(password, user["password_hash"]):
        st.session_state.user = user
        st.session_state.editing_project_id = None
        # Load first project or create one (respect project_type)
        projects = db.get_user_projects(user["id"])
        leaves = [p for p in projects if not p.get("is_folder")]
        if leaves:
            first = leaves[0]
            ptype = first.get("project_type", "youtube") or "youtube"
            st.session_state.current_project_id = first["id"]
            st.session_state.current_project_type = ptype
            data = db.load_project_data(first["id"], user["id"])
            if ptype == "x_profile":
                default_x = {"url": [""], "username": [""], "name": [""], "followers": [0], "verified": [""], "niche": [""], "last_fetched": [""]}
                st.session_state.x_bulk_df = pd.DataFrame(data) if data else pd.DataFrame(default_x)
                # ensure yt schema (for in-memory YT tab when X project is first)
                if "yt_bulk_df" not in st.session_state or not isinstance(st.session_state.yt_bulk_df, pd.DataFrame):
                    st.session_state.yt_bulk_df = pd.DataFrame({
                        "url": [""], "title": [""], "views": [0], "likes": [0], "comments": [0], "last_fetched": [""]
                    })
                for col in ["url", "title", "last_fetched"]:
                    if col not in st.session_state.yt_bulk_df.columns:
                        st.session_state.yt_bulk_df[col] = ""
                for col in ["views", "likes", "comments"]:
                    if col not in st.session_state.yt_bulk_df.columns:
                        st.session_state.yt_bulk_df[col] = 0
            else:
                default_yt = {"url": [""], "title": [""], "views": [0], "likes": [0], "comments": [0], "last_fetched": [""]}
                st.session_state.yt_bulk_df = pd.DataFrame(data) if data else pd.DataFrame(default_yt)
            st.session_state.editing_project_id = None
        else:
            # Create first project for new user as YouTube
            pid = db.create_project(user["id"], "My First Project", project_type="youtube")
            st.session_state.current_project_id = pid
            st.session_state.current_project_type = "youtube"
            st.session_state.editing_project_id = None
            st.session_state.yt_bulk_df = pd.DataFrame({
                "url": [""], "title": [""], "views": [0], "likes": [0], "comments": [0], "last_fetched": [""]
            })
        return True
    return False

def signup_user(email: str, password: str) -> bool:
    user_id = db.create_user(email, password)
    if user_id:
        # Auto login after signup
        user = db.get_user_by_id(user_id)
        st.session_state.user = user
        pid = db.create_project(user_id, "My First Project", project_type="youtube")
        st.session_state.current_project_id = pid
        st.session_state.current_project_type = "youtube"
        st.session_state.editing_project_id = None
        st.session_state.yt_bulk_df = pd.DataFrame({
            "url": [""], "title": [""], "views": [0], "likes": [0], "comments": [0], "last_fetched": [""]
        })
        return True
    return False

def logout():
    st.session_state.user = None
    st.session_state.current_project_id = None
    st.session_state.current_project_type = "youtube"
    st.session_state.editing_project_id = None
    st.session_state.yt_bulk_df = pd.DataFrame({
        "url": [""], "title": [""], "views": [0], "likes": [0], "comments": [0], "last_fetched": [""]
    })
    st.session_state.x_bulk_df = pd.DataFrame({
        "url": [""], "username": [""], "name": [""], "followers": [0], "verified": [""], "niche": [""], "last_fetched": [""]
    })
    st.rerun()

def save_current_project():
    """Persist the current table(s) to the active project.
    For YouTube projects, saves both video and channel data.
    """
    if not (st.session_state.user and st.session_state.current_project_id):
        return
    ptype = st.session_state.get("current_project_type", "youtube")
    if ptype == "x_profile":
        data_json = st.session_state.x_bulk_df.to_json(orient="records")
    else:
        # YouTube project: save both video bulk and channel bulk
        data = {
            "videos": st.session_state.yt_bulk_df.to_dict("records"),
            "channels": st.session_state.channel_bulk_df.to_dict("records")
        }
        data_json = json.dumps(data)
    db.save_project_data(
        st.session_state.current_project_id,
        st.session_state.user["id"],
        data_json
    )

def load_project(project_id: int):
    if not st.session_state.user:
        return
    proj = db.get_project(project_id, st.session_state.user["id"])
    if proj and proj.get('is_folder'):
        st.sidebar.warning("Cannot load a folder.")
        return
    ptype = (proj or {}).get("project_type", "youtube") or "youtube"
    data = db.load_project_data(project_id, st.session_state.user["id"])
    st.session_state.current_project_id = project_id
    st.session_state.current_project_type = ptype
    st.session_state.editing_project_id = None  # exit any inline edit

    if ptype == "x_profile":
        default_x = {"url": [""], "username": [""], "name": [""], "followers": [0], "verified": [""], "niche": [""], "last_fetched": [""]}
        if data:
            try:
                st.session_state.x_bulk_df = pd.DataFrame(data)
            except Exception:
                st.session_state.x_bulk_df = pd.DataFrame(default_x)
        else:
            st.session_state.x_bulk_df = pd.DataFrame(default_x)
        # ensure yt schema exists (defensive)
        if "yt_bulk_df" not in st.session_state or not isinstance(st.session_state.yt_bulk_df, pd.DataFrame):
            st.session_state.yt_bulk_df = pd.DataFrame({
                "url": [""], "title": [""], "views": [0], "likes": [0], "comments": [0], "last_fetched": [""]
            })
        for col in ["url", "title", "last_fetched"]:
            if col not in st.session_state.yt_bulk_df.columns:
                st.session_state.yt_bulk_df[col] = ""
        for col in ["views", "likes", "comments"]:
            if col not in st.session_state.yt_bulk_df.columns:
                st.session_state.yt_bulk_df[col] = 0
    else:
        default_yt = {"url": [""], "title": [""], "views": [0], "likes": [0], "comments": [0], "last_fetched": [""]}
        default_ch = {
            "channel_url": [""], "title": [""], "subscribers": [0],
            "total_videos": [0], "total_views": [0], "last_fetched": [""], "category": [""]
        }
        default_ch_df = pd.DataFrame(default_ch).astype({
            "subscribers": "int64",
            "total_videos": "int64",
            "total_views": "int64",
        })
        if data:
            try:
                loaded = json.loads(data) if isinstance(data, str) else data
                if isinstance(loaded, dict) and "videos" in loaded:
                    st.session_state.yt_bulk_df = pd.DataFrame(loaded.get("videos", []))
                    st.session_state.channel_bulk_df = pd.DataFrame(loaded.get("channels", []))
                else:
                    # legacy single df
                    st.session_state.yt_bulk_df = pd.DataFrame(loaded if isinstance(loaded, list) else default_yt)
                    st.session_state.channel_bulk_df = default_ch_df
            except Exception:
                st.session_state.yt_bulk_df = pd.DataFrame(default_yt)
                st.session_state.channel_bulk_df = default_ch_df
        else:
            st.session_state.yt_bulk_df = pd.DataFrame(default_yt)
            st.session_state.channel_bulk_df = default_ch_df
        if "x_bulk_df" not in st.session_state or len(st.session_state.x_bulk_df) == 0:
            st.session_state.x_bulk_df = pd.DataFrame({
                "url": [""], "username": [""], "name": [""], "followers": [0], "verified": [""], "niche": [""], "last_fetched": [""]
            })
    st.rerun()

def create_new_project(name: str = None, parent_id: int = None, project_type: str = "youtube"):
    if not st.session_state.user:
        return
    if not name:
        name = f"Table {datetime.now().strftime('%Y-%m-%d %H:%M')}" if project_type == "youtube" else f"X Profiles {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    pid = db.create_project(st.session_state.user["id"], name, parent_id=parent_id, is_folder=0, project_type=project_type)
    st.session_state.editing_project_id = None
    load_project(pid)  # will set and rerun

def get_quota_info():
    if not st.session_state.user:
        return {"used": 0, "limit": 200, "remaining": 200, "is_subscribed": False}
    return db.get_user_quota(st.session_state.user["id"])

def can_fetch_more() -> bool:
    q = get_quota_info()
    return q["remaining"] > 0 or q["is_subscribed"]


# --- Tree view helpers for projects ---
def _build_project_tree(projects):
    """Build nested dict for tree from flat list with parent_id."""
    children_map = {p['id']: [] for p in projects}
    roots = []
    for p in projects:
        p = dict(p)  # copy
        pid = p['id']
        parent = p.get('parent_id')
        if parent and parent in children_map:
            children_map[parent].append(p)
        else:
            roots.append(p)
    def attach_children(node):
        node['children'] = children_map.get(node['id'], [])
        for child in node['children']:
            attach_children(child)
    for root in roots:
        attach_children(root)
    return sorted(roots, key=lambda x: (not x.get('is_folder'), x['name'].lower()))

def render_project_tree(nodes, user_id, level=0):
    """Recursively render tree in sidebar using expanders for folders and buttons for items."""
    for node in nodes:
        is_folder = node.get('is_folder', 0)
        is_current = node['id'] == st.session_state.current_project_id
        is_editing = st.session_state.get("editing_project_id") == node.get('id')
        indent = "　" * level  # full-width space for indent
        if is_folder:
            with st.sidebar.expander(f"{indent}📁 {node['name']}", expanded=True):
                if is_editing:
                    # Inline rename for folder
                    new_name = st.sidebar.text_input(
                        "New name", 
                        value=node['name'], 
                        key=f"edit_name_{node['id']}",
                        label_visibility="collapsed"
                    )
                    c1, c2 = st.sidebar.columns(2)
                    if c1.button("💾 Save", key=f"save_rename_{node['id']}", width="stretch"):
                        if db.rename_project(node['id'], user_id, new_name):
                            st.session_state.editing_project_id = None
                            st.sidebar.success("Renamed!")
                            st.rerun()
                        else:
                            st.sidebar.error("Name cannot be empty")
                    if c2.button("✖ Cancel", key=f"cancel_rename_{node['id']}", width="stretch"):
                        st.session_state.editing_project_id = None
                        st.rerun()
                else:
                    # folder actions (expander title already shows the name)
                    fcols = st.sidebar.columns([0.7, 0.15, 0.15])
                    with fcols[0]:
                        st.caption("(folder)")
                    with fcols[1]:
                        if st.button("✏️", key=f"editf_{node['id']}", help="Rename folder"):
                            st.session_state.editing_project_id = node['id']
                            st.rerun()
                    with fcols[2]:
                        if st.button("🗑️", key=f"delf_{node['id']}", help="Delete folder and all contents"):
                            def _all_descendant_ids(n):
                                ids = [n['id']]
                                for ch in n.get('children', []):
                                    ids.extend(_all_descendant_ids(ch))
                                return ids
                            subtree = _all_descendant_ids(node)
                            db.delete_project(node['id'], user_id)
                            if st.session_state.current_project_id in subtree:
                                st.session_state.current_project_id = None
                            st.rerun()
                render_project_tree(node.get('children', []), user_id, level + 1)
        else:
            if is_editing:
                # Inline rename directly on the list item
                new_name = st.sidebar.text_input(
                    "New name", 
                    value=node['name'], 
                    key=f"edit_name_{node['id']}",
                    label_visibility="collapsed"
                )
                c1, c2 = st.sidebar.columns(2)
                if c1.button("💾 Save", key=f"save_rename_{node['id']}", width="stretch"):
                    if db.rename_project(node['id'], user_id, new_name):
                        st.session_state.editing_project_id = None
                        st.sidebar.success("Renamed!")
                        st.rerun()
                    else:
                        st.sidebar.error("Name cannot be empty")
                if c2.button("✖ Cancel", key=f"cancel_rename_{node['id']}", width="stretch"):
                    st.session_state.editing_project_id = None
                    st.rerun()
            else:
                # Normal leaf: name (click to load) + edit + delete
                ptype = node.get("project_type", "youtube")
                type_badge = "▶️" if ptype == "youtube" else "𝕏"
                label = f"{indent}📄 {type_badge} {'✅ ' if is_current else ''}{node['name']} (updated {node['updated_at'][:16]})"
                cols = st.sidebar.columns([0.7, 0.15, 0.15])
                with cols[0]:
                    if st.button(label, key=f"proj_{node['id']}", width="stretch"):
                        load_project(node["id"])
                with cols[1]:
                    if st.button("✏️", key=f"edit_{node['id']}", help="Rename this project directly"):
                        st.session_state.editing_project_id = node['id']
                        st.rerun()
                with cols[2]:
                    if st.button("🗑️", key=f"del_{node['id']}", help="Delete"):
                        if st.session_state.current_project_id == node['id']:
                            st.session_state.current_project_id = None
                        db.delete_project(node['id'], user_id)
                        st.rerun()

def show_upgrade_button():
    """Show upgrade / payment option."""
    if STRIPE_SECRET_KEY:
        if st.button("💳 Upgrade to Pro (Stripe Checkout)", type="primary", width="stretch"):
            url = create_stripe_checkout_session(st.session_state.user["email"])
            if url:
                st.success("Checkout session created!")
                st.link_button("Open Stripe Checkout", url, width="stretch")
                st.caption("Complete the payment in the new tab. After success, come back and refresh or use the simulate button below for testing.")
            else:
                st.error("Failed to create checkout session.")
    elif STRIPE_PAYMENT_LINK:
        st.link_button(
            "💳 Upgrade / Buy More Credits (Stripe)",
            STRIPE_PAYMENT_LINK,
            width="stretch"
        )
        st.caption("Opens secure Stripe checkout (test mode supported).")
    else:
        st.info("Stripe not configured yet. Add STRIPE_SECRET_KEY in .env")

def create_stripe_checkout_session(user_email: str) -> str | None:
    """Create a real Stripe Checkout Session and return the URL."""
    if not STRIPE_SECRET_KEY:
        return None
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY

        # Using price_data for ad-hoc pricing (easy for testing, no need pre-created Price ID)
        # Change unit_amount as needed (in smallest currency unit, e.g. cents)
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": "Metric Reports Pro Plan",
                        "description": "Unlock unlimited video generations (removes 200 limit)",
                    },
                    "unit_amount": 2900,  # $29.00 - adjust as needed
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url="http://localhost:8501?payment=success",
            cancel_url="http://localhost:8501?payment=cancel",
            customer_email=user_email,
        )
        return session.url
    except Exception as e:
        st.error(f"Stripe error creating session: {str(e)}")
        return None

def mark_user_as_paid(user_id: int):
    """Manual helper for testing (in real: use Stripe webhook)."""
    db.set_user_subscribed(user_id, True)
    # Optionally reset quota or set high limit
    # For now just mark subscribed = unlimited


# Constants
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN")
X_API_KEY = os.getenv("X_API_KEY")
X_API_KEY_SECRET = os.getenv("X_API_KEY_SECRET")
X_ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN")
X_ACCESS_TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET")
XAI_API_KEY = os.getenv("XAI_API_KEY")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-2")


def get_x_bearer_token():
    """Generate a fresh app-only bearer token from consumer key+secret if needed."""
    if not (X_API_KEY and X_API_KEY_SECRET) or X_API_KEY == "your_x_api_key_here":
        return None
    try:
        resp = requests.post(
            "https://api.twitter.com/oauth2/token",
            auth=(X_API_KEY, X_API_KEY_SECRET),
            data={"grant_type": "client_credentials"}
        )
        if resp.status_code == 200:
            return resp.json().get("access_token")
    except Exception:
        pass
    return None

# ==================== BULK YOUTUBE TABLE (Session State) ====================
MAX_FREE_ROWS = 200


def get_youtube_service():
    if not YOUTUBE_API_KEY or YOUTUBE_API_KEY == "your_youtube_api_key_here":
        return None
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

def fetch_youtube_video(video_id: str):
    """Fetch basic stats for a YouTube video."""
    youtube = get_youtube_service()
    if not youtube:
        raise ValueError("YouTube API key not configured in .env")
    
    try:
        request = youtube.videos().list(
            part="snippet,statistics,contentDetails",
            id=video_id
        )
        response = request.execute()
        
        if not response.get("items"):
            return None
        
        item = response["items"][0]
        snippet = item["snippet"]
        stats = item["statistics"]
        
        return {
            "video_id": video_id,
            "title": snippet.get("title", ""),
            "channel_title": snippet.get("channelTitle", ""),
            "published_at": snippet.get("publishedAt", ""),
            "view_count": int(stats.get("viewCount", 0)),
            "like_count": int(stats.get("likeCount", 0)),
            "comment_count": int(stats.get("commentCount", 0)),
            "duration": item.get("contentDetails", {}).get("duration", ""),
            "fetched_at": datetime.now().isoformat()
        }
    except HttpError as e:
        raise ValueError(f"YouTube API error: {e}")

def fetch_youtube_channel(identifier: str):
    """Fetch channel statistics. Accepts channel ID (UC...), @handle, or full URL.
    Automatically resolves @handle to UC ID using forHandle parameter.
    Category is taken directly from YouTube API topicDetails (if available).
    """
    youtube = get_youtube_service()
    if not youtube:
        raise ValueError("YouTube API key not configured in .env")
    
    identifier = str(identifier).strip()
    
    # Extract handle or channel ID from common URL formats
    channel_id = None
    handle = None
    
    if "channel/" in identifier:
        channel_id = identifier.split("channel/")[-1].split("/")[0].split("?")[0]
    elif "@" in identifier:
        handle = identifier.split("@")[-1].split("/")[0].split("?")[0]
    elif identifier.startswith("UC") and len(identifier) > 10:
        channel_id = identifier
    else:
        # Treat as raw handle (user typed @something or just something)
        if identifier.startswith("@"):
            handle = identifier[1:]
        else:
            handle = identifier
    
    try:
        if channel_id:
            request = youtube.channels().list(
                part="snippet,statistics,topicDetails",
                id=channel_id
            )
        elif handle:
            # Use forHandle to resolve @handle to channel details
            request = youtube.channels().list(
                part="snippet,statistics,topicDetails",
                forHandle=handle
            )
        else:
            return None
        
        response = request.execute()
        
        if not response.get("items"):
            return None
        
        item = response["items"][0]
        snippet = item["snippet"]
        stats = item.get("statistics", {})
        
        # Extract category from YouTube API topicDetails (Wikipedia-style topics)
        category = "Other"
        if "topicDetails" in item:
            topics = item["topicDetails"].get("topicCategories", [])
            if topics:
                # Take the last part of the first topic URL as readable category
                cat_url = topics[0]
                category = cat_url.split("/")[-1].replace("_", " ")
        
        return {
            "channel_id": item.get("id", channel_id or handle),
            "title": snippet.get("title", ""),
            "description": snippet.get("description", "")[:200] + "...",
            "subscriber_count": int(stats.get("subscriberCount", 0)),
            "video_count": int(stats.get("videoCount", 0)),
            "view_count": int(stats.get("viewCount", 0)),
            "category": category,
            "fetched_at": datetime.now().isoformat()
        }
    except HttpError as e:
        raise ValueError(f"YouTube API error: {e}")


# ==================== BULK TABLE HELPERS ====================

def extract_video_id(url_or_id: str) -> Optional[str]:
    """Extract YouTube video ID from URL or raw ID."""
    if not url_or_id:
        return None
    s = str(url_or_id).strip()
    if not s:
        return None

    # Already a clean 11-char ID
    if len(s) == 11 and "/" not in s and "?" not in s and "&" not in s and " " not in s:
        return s

    # youtu.be/VIDEOID
    if "youtu.be/" in s:
        return s.split("youtu.be/")[-1].split("?")[0].split("&")[0]

    # ?v=VIDEOID or &v=
    if "v=" in s:
        return s.split("v=")[-1].split("&")[0].split("#")[0]

    # /embed/VIDEOID or /shorts/VIDEOID
    if "/embed/" in s:
        return s.split("/embed/")[-1].split("?")[0]
    if "/shorts/" in s:
        return s.split("/shorts/")[-1].split("?")[0]

    return None


def fetch_youtube_videos_batch(video_ids: List[str]) -> dict:
    """Batch fetch video stats (YouTube supports up to ~50 IDs per request)."""
    youtube = get_youtube_service()
    if not youtube:
        raise ValueError("YouTube API key not configured in .env")

    results = {}
    for i in range(0, len(video_ids), 50):
        batch = [vid for vid in video_ids[i:i+50] if vid]
        if not batch:
            continue
        try:
            request = youtube.videos().list(
                part="snippet,statistics",
                id=",".join(batch)
            )
            response = request.execute()

            st.session_state.api_usage["youtube_calls"] += len(batch)
            if st.session_state.user:
                db.increment_user_api_usage(st.session_state.user["id"], youtube=len(batch))

            for item in response.get("items", []):
                vid = item["id"]
                snippet = item["snippet"]
                stats = item.get("statistics", {})
                results[vid] = {
                    "title": snippet.get("title", ""),
                    "views": int(stats.get("viewCount", 0)),
                    "likes": int(stats.get("likeCount", 0)),
                    "comments": int(stats.get("commentCount", 0)),
                    "last_fetched": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
        except HttpError as e:
            for vid in batch:
                if vid not in results:
                    results[vid] = {"error": str(e)[:80]}
    return results


def get_x_client():
    # Prefer provided bearer
    if X_BEARER_TOKEN and X_BEARER_TOKEN != "your_x_bearer_token_here":
        return tweepy.Client(bearer_token=X_BEARER_TOKEN)
    # Else generate bearer from consumer key+secret (app-only, good for read-only profile data)
    bearer = get_x_bearer_token()
    if bearer:
        return tweepy.Client(bearer_token=bearer)
    # Fallback to full OAuth1 (requires user access tokens too)
    if all([X_API_KEY, X_API_KEY_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET]):
        auth = tweepy.OAuth1UserHandler(
            X_API_KEY, X_API_KEY_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
        )
        return tweepy.API(auth)
    return None


def extract_x_username(url_or_handle: str) -> Optional[str]:
    """Extract @username from x.com URL, twitter.com URL, @handle or bare username."""
    if not url_or_handle:
        return None
    s = str(url_or_handle).strip().lower().rstrip("/")
    if not s:
        return None
    if s.startswith("@"):
        return s[1:]
    # Common URL forms
    m = re.search(r"(?:https?://)?(?:www\.)?(?:x\.com|twitter\.com)/([a-z0-9_]{1,15})", s, re.I)
    if m:
        return m.group(1).lower()
    # bare username
    if re.match(r"^[a-z0-9_]{1,15}$", s):
        return s
    return None


def infer_niche_from_profile(user_obj) -> str:
    """Infer niche/category for an X profile.
    Uses Grok (xAI) for intelligent classification if XAI_API_KEY is set.
    Falls back to simple keyword heuristic otherwise.
    User can always manually edit the 'niche' column.
    """
    # Collect profile text
    name = getattr(user_obj, "name", "") or ""
    description = getattr(user_obj, "description", "") or ""
    location = getattr(user_obj, "location", "") or ""
    username = getattr(user_obj, "username", "") or ""
    verified_type = str(getattr(user_obj, "verified_type", "")).lower()

    profile_text = f"""Name: {name}
Username: @{username}
Bio: {description}
Location: {location}
Verified type: {verified_type}"""

    # If Grok API key available, use it for smart inference
    if XAI_API_KEY and XAI_API_KEY != "your_xai_api_key_here":
        try:
            from openai import OpenAI
            grok_client = OpenAI(
                api_key=XAI_API_KEY,
                base_url="https://api.x.ai/v1"
            )
            prompt = f"""You are an expert at classifying X/Twitter accounts into categories based on their profile.

Given the following profile information, determine the SINGLE best-fitting niche/category from this list ONLY:
- Crypto / Web3
- Tech / AI
- Finance / Investing
- Creator / Influencer
- Politics / News
- Sports
- Entertainment
- Business / CEO
- Public Figure
- Other / N/A

Respond with ONLY the exact category name from the list above. No explanations, no extra text.

Profile:
{profile_text}"""

            # Try configured model first, then common fallbacks (free tier / new keys sometimes have different model IDs)
            models_to_try = [m for m in [XAI_MODEL, "grok-2", "grok-beta", "grok-2-1212"] if m]
            response = None
            last_err = None
            for m in models_to_try:
                try:
                    response = grok_client.chat.completions.create(
                        model=m,
                        messages=[
                            {"role": "system", "content": "You are a precise category classifier."},
                            {"role": "user", "content": prompt}
                        ],
                        max_tokens=20,
                        temperature=0.1
                    )
                    st.session_state.api_usage["grok_calls"] += 1
                    if st.session_state.user:
                        db.increment_user_api_usage(st.session_state.user["id"], grok=1, grok_tokens=getattr(response.usage, "total_tokens", 0) if hasattr(response, "usage") and response.usage else 0)
                    try:
                        if hasattr(response, "usage") and response.usage:
                            st.session_state.api_usage["grok_total_tokens"] += getattr(response.usage, "total_tokens", 0)
                    except Exception:
                        pass
                    break
                except Exception as e:
                    last_err = e
                    if "model" not in str(e).lower():
                        break  # non-model error (e.g. credits), stop trying
            if response is None:
                raise last_err or Exception("No working model found")
            category = response.choices[0].message.content.strip()
            # Validate against allowed list
            allowed = ["Crypto / Web3", "Tech / AI", "Finance / Investing", "Creator / Influencer",
                       "Politics / News", "Sports", "Entertainment", "Business / CEO", "Public Figure", "Other / N/A"]
            if category in allowed:
                return category
            # If Grok gave something close, try to match
            for allowed_cat in allowed:
                if allowed_cat.lower() in category.lower():
                    return allowed_cat
            return "Other / N/A"
        except Exception as e:
            # Fallback to heuristic on any Grok error
            print(f"[Grok niche inference fallback] {e}")

    # Fallback heuristic
    text = f"{name} {description} {location}".lower()
    keywords = {
        "Crypto / Web3": ["crypto", "bitcoin", "ethereum", "web3", "blockchain", "nft", "defi", "solana", "btc", "eth"],
        "Tech / AI": ["ai", "artificial intelligence", "tech", "software", "engineer", "developer", "startup", "founder", "product"],
        "Finance / Investing": ["finance", "invest", "stock", "trading", "market", "hedge", "vc", "capital"],
        "Creator / Influencer": ["creator", "influencer", "content", "youtuber", "streamer"],
        "Politics / News": ["politics", "news", "journalist", "reporter", "minister", "senator", "election"],
        "Sports": ["sport", "football", "soccer", "nba", "athlete", "coach", "mlb", "ufc"],
        "Entertainment": ["actor", "actress", "singer", "musician", "artist", "movie", "film", "music"],
        "Business / CEO": ["ceo", "founder", "entrepreneur", "business", "company"],
    }
    for niche, kws in keywords.items():
        if any(kw in text for kw in kws):
            return niche
    if "verified" in verified_type:
        return "Public Figure"
    return "Other / N/A"


def fetch_x_profiles_batch(handles: List[str], client=None) -> dict:
    """Fetch X user profiles by username list. Returns dict username -> data or error.
    Uses v2 Client if available (preferred for Bearer token).
    """
    if client is None:
        client = get_x_client()
    if not client:
        raise ValueError("X API credentials not configured (.env X_BEARER_TOKEN recommended)")

    results = {}
    valid_handles = []
    handle_to_input = {}
    for h in handles:
        uname = extract_x_username(h)
        if uname:
            valid_handles.append(uname)
            handle_to_input[uname] = h  # original input for reference

    if not valid_handles:
        return results

    # Dedup while preserving order
    seen = set()
    unique_handles = []
    for h in valid_handles:
        if h not in seen:
            seen.add(h)
            unique_handles.append(h)

    try:
        # Prefer v2 Client (tweepy.Client)
        if isinstance(client, tweepy.Client):
            # X API v2 allows up to 100 usernames per request
            for i in range(0, len(unique_handles), 100):
                batch = unique_handles[i:i+100]
                resp = client.get_users(
                    usernames=batch,
                    user_fields=["id", "name", "username", "public_metrics", "verified", "verified_type", "description", "location", "created_at"]
                )
                users = resp.data or []
                errors = {e["value"]: e for e in (resp.errors or []) if "value" in e}

                st.session_state.api_usage["x_api_calls"] += len(batch)
                if st.session_state.user:
                    db.increment_user_api_usage(st.session_state.user["id"], x=len(batch))

                # Best effort: capture rate limit headers if tweepy Response exposes them
                try:
                    if hasattr(resp, "headers") and resp.headers:
                        st.session_state.last_x_rate_limit = {
                            "remaining": resp.headers.get("x-rate-limit-remaining"),
                            "limit": resp.headers.get("x-rate-limit-limit"),
                            "reset": resp.headers.get("x-rate-limit-reset"),
                        }
                except Exception:
                    pass

                for u in users:
                    uname = u.username.lower()
                    metrics = getattr(u, "public_metrics", {}) or {}
                    followers = metrics.get("followers_count", 0) if isinstance(metrics, dict) else 0
                    verified = "✅" if getattr(u, "verified", False) else ""
                    niche = infer_niche_from_profile(u)
                    results[uname] = {
                        "username": uname,
                        "name": getattr(u, "name", ""),
                        "followers": followers,
                        "verified": verified,
                        "niche": niche,
                        "last_fetched": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    }
                for bad in batch:
                    if bad in errors and bad not in results:
                        results[bad] = {"error": str(errors[bad].get("detail", "not found"))[:80]}
        else:
            # Fallback v1.1 tweepy.API (slower, one by one or lookup_users)
            for uname in unique_handles:
                try:
                    st.session_state.api_usage["x_api_calls"] += 1
                    if st.session_state.user:
                        db.increment_user_api_usage(st.session_state.user["id"], x=1)
                    u = client.get_user(screen_name=uname, include_entities=False)
                    followers = u.followers_count if hasattr(u, "followers_count") else 0
                    verified = "✅" if getattr(u, "verified", False) else ""
                    # v1 has no rich description in same way, but u.description exists
                    niche = infer_niche_from_profile(u)
                    results[uname] = {
                        "username": uname,
                        "name": getattr(u, "name", ""),
                        "followers": followers,
                        "verified": verified,
                        "niche": niche,
                        "last_fetched": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    }
                except Exception as e:
                    results[uname] = {"error": str(e)[:80]}
    except Exception as e:
        # On total failure mark all
        for h in unique_handles:
            if h not in results:
                results[h] = {"error": str(e)[:80]}

    return results


# ==================== AUTH GUARD (Login / Signup) ====================
if not st.session_state.user:
    st.title("📊 Metric Reports")
    st.markdown("**Multi-user YouTube bulk metrics with projects & quotas**")
    st.divider()

    tab_login, tab_signup = st.tabs(["🔑 Login", "📝 Sign Up"])

    with tab_login:
        with st.form("login_form", clear_on_submit=False):
            email = st.text_input("Email", placeholder="you@example.com")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Login", type="primary", width="stretch"):
                if login_user(email, password):
                    st.success("Welcome back!")
                    st.rerun()
                else:
                    st.error("Invalid email or password. Please try again.")

    with tab_signup:
        with st.form("signup_form", clear_on_submit=False):
            email = st.text_input("Email", placeholder="you@example.com")
            password = st.text_input("Password (min 6 chars)", type="password")
            password2 = st.text_input("Confirm Password", type="password")
            if st.form_submit_button("Create Free Account", type="primary", width="stretch"):
                if not email or "@" not in email:
                    st.error("Please enter a valid email.")
                elif len(password) < 6:
                    st.error("Password must be at least 6 characters.")
                elif password != password2:
                    st.error("Passwords do not match.")
                elif signup_user(email, password):
                    if email.lower().strip() == "hello@atlasnow.co":
                        st.success("Account created! Super Admin (unlimited) activated.")
                    else:
                        st.success("Account created! You're now logged in with 200 free video credits.")
                    st.rerun()
                else:
                    st.error("This email is already registered. Please login instead.")

    st.info("Free tier: 200 video URL generations. After that, upgrade via payment to continue.")
    st.stop()  # Do not render the rest of the app

# If we reach here → user is logged in
user = st.session_state.user
quota = get_quota_info()

# Handle Stripe redirect after payment (success param)
if st.query_params.get("payment") == "success":
    if not user.get("is_subscribed"):
        mark_user_as_paid(user["id"])
        st.success("🎉 Thank you! Payment successful. Your account is now upgraded (test mode). Quota limit removed.")
        # Clear query param
        st.query_params.clear()
        st.rerun()
elif st.query_params.get("payment") == "cancel":
    st.warning("Payment was cancelled.")
    st.query_params.clear()

# ==================== LOGGED-IN SIDEBAR & PROJECTS ====================
st.sidebar.title("Metric Reports")
admin_badge = " 👑 Super Admin" if quota.get("is_admin") else ""
st.sidebar.caption(f"Logged in as **{user['email']}**{admin_badge}")

# Project management - Tree view for better organization with many projects
st.sidebar.markdown("### 📁 My Projects (Tree)")

projects = db.get_user_projects(user["id"])
tree = _build_project_tree(projects)
if tree:
    render_project_tree(tree, user["id"])
else:
    st.sidebar.info("No projects yet.")

# Buttons for new folder and new project (support choosing parent folder)
# Build indented options from the tree for better UX with deep nesting
def _collect_folder_choices(nodes, prefix=""):
    opts = {}
    for node in nodes:
        if node.get('is_folder'):
            label = f"{prefix}{node['name']}"
            opts[label] = node['id']
            opts.update(_collect_folder_choices(node.get('children', []), prefix + "  "))
    return opts

folder_options = {"(Root)": None}
folder_options.update(_collect_folder_choices(tree))
parent_choice = st.sidebar.selectbox("Create under folder:", list(folder_options.keys()), key="parent_for_new")
parent_id = folder_options[parent_choice]

colf, colp = st.sidebar.columns(2)
if colf.button("📁 New Folder", width="stretch"):
    fname = f"Folder {datetime.now().strftime('%H%M')}"
    db.create_project(user["id"], fname, parent_id=parent_id, is_folder=1)
    st.session_state.editing_project_id = None
    st.rerun()

# Simple creation - two clear buttons (reverted to pre-"sidebar platform choice" style)
# User picks the type by which button they click. Projects are separate.
if colp.button("➕ New YouTube Project", type="primary", width="stretch"):
    create_new_project(parent_id=parent_id, project_type="youtube")

if st.sidebar.button("➕ New X Profile Project", width="stretch"):
    create_new_project(parent_id=parent_id, project_type="x_profile")

if st.session_state.current_project_id:
    if st.sidebar.button("💾 Save Current Table", width="stretch"):
        save_current_project()
        st.sidebar.success("Saved!")

st.sidebar.caption("Tip: YouTube projects and X Profile projects are separate. Use the tabs (YouTube Data / X Data) to work with the right one.")

st.sidebar.markdown("---")

# Quota display
st.sidebar.markdown("### 📊 Usage Quota")
if quota.get("is_admin"):
    st.sidebar.success("👑 Super Admin — Unlimited access")
    st.sidebar.write(f"**{quota['used']}** rows fetched (YT + X, no limit)")
else:
    st.sidebar.progress(min(quota["used"] / max(quota["limit"], 1), 1.0))
    st.sidebar.write(f"**{quota['used']} / {quota['limit'] if not quota['is_subscribed'] else '∞'}** videos fetched")
    if not quota["is_subscribed"] and quota["remaining"] <= 0:
        st.sidebar.error("Free limit reached (200). Upgrade to continue.")
        show_upgrade_button()
    elif not quota["is_subscribed"]:
        st.sidebar.caption(f"{quota['remaining']} free fetches remaining")
        if quota["remaining"] < 50:
            show_upgrade_button()

# Dev helper - remove in production
if not quota["is_subscribed"] and STRIPE_SECRET_KEY:
    if st.sidebar.button("🧪 Dev: Simulate successful payment", width="stretch"):
        mark_user_as_paid(user["id"])
        st.sidebar.success("Account upgraded (test)!")
        st.rerun()

# Logout
st.sidebar.markdown("---")
if st.sidebar.button("🚪 Logout", width="stretch"):
    logout()

# API status (keep for now)
st.sidebar.markdown("### API Status")
if YOUTUBE_API_KEY and YOUTUBE_API_KEY != "your_youtube_api_key_here":
    st.sidebar.success("✅ YouTube API key loaded")
else:
    st.sidebar.error("❌ YouTube API key missing in .env")

# (Old duplicate sidebar removed - now handled in the logged-in block above)

# Main content (logged in)
current_proj_name = "No project"
if st.session_state.current_project_id:
    proj = db.get_project(st.session_state.current_project_id, user["id"])
    if proj:
        current_proj_name = proj["name"]

st.title("📊 Metric Reports")
ptype = st.session_state.get("current_project_type", "youtube")
type_label = "▶️ YouTube" if ptype == "youtube" else "𝕏 X Profiles"
st.caption(f"**Project:** {current_proj_name} ({type_label})  |  User: {user['email']}")

# Quota warning banner
if quota.get("is_admin"):
    st.success("👑 Super Admin mode — unlimited video generations")
elif not quota["is_subscribed"] and quota["remaining"] <= 0:
    st.error("🚫 Free quota exhausted (200 rows). Upgrade to continue fetching more data (YouTube or X).")
    show_upgrade_button()
elif not quota["is_subscribed"]:
    st.warning(f"Free quota: {quota['remaining']} rows remaining (YouTube + X profiles).")
    if quota["remaining"] < 50:
        show_upgrade_button()

st.divider()

# Require a project before showing the heavy data tabs (prevents st.stop() from one tab killing the others)
is_admin_user = bool((st.session_state.user or {}).get("is_admin", 0))

if not st.session_state.get("current_project_id"):
    st.info("👈 Create a project or load one from the **sidebar tree** (left) to begin. You can have separate projects for YouTube and for X.")
    # Still show the tab skeleton so user sees the structure
    tab_names = ["YouTube Data", "X (Twitter) Data"]
    if is_admin_user:
        tab_names.append("⚙️ Settings")
    skeleton_tabs = st.tabs(tab_names)
    with skeleton_tabs[0]:
        st.caption("YouTube Video Fetch + Channel Fetch will appear here once a project is loaded.")
    with skeleton_tabs[1]:
        st.caption("X Profile Checker (Bulk) will appear here once an X Profile project is loaded.")
    if is_admin_user:
        with skeleton_tabs[2]:
            st.caption("Settings & API Quota monitoring.")
    st.stop()

# Clean tabs for feature separation (YouTube Data vs X Data). 
# Sidebar projects are independent — create/load the right type for persistence.
# Settings tab is only visible to admins (super admin / is_admin users)
tab_names = ["YouTube Data", "X (Twitter) Data"]
if is_admin_user:
    tab_names.append("⚙️ Settings")
main_tabs = st.tabs(tab_names)

youtube_data_tab = main_tabs[0]
x_data_tab = main_tabs[1]
settings_tab = main_tabs[2] if is_admin_user else None

with youtube_data_tab:
    st.header("YouTube Data")

    # Defensive schema repair + canonical column order.
    # This keeps the table structure consistent ("Kol A, B, C..." as user expects) even after
    # loading old/mixed project data, concats, or partial column repairs.
    yt_canonical = ["url", "title", "views", "likes", "comments", "last_fetched"]
    if "yt_bulk_df" not in st.session_state or not isinstance(st.session_state.yt_bulk_df, pd.DataFrame):
        st.session_state.yt_bulk_df = pd.DataFrame({c: ([""] if c in ("url", "title", "last_fetched") else [0]) for c in yt_canonical})
    # Add any missing columns (at end for now)
    for col in yt_canonical:
        if col not in st.session_state.yt_bulk_df.columns:
            st.session_state.yt_bulk_df[col] = "" if col in ("url", "title", "last_fetched") else 0
    # Force canonical order (this is what fixes "berantakan")
    present = [c for c in yt_canonical if c in st.session_state.yt_bulk_df.columns]
    extra = [c for c in st.session_state.yt_bulk_df.columns if c not in yt_canonical]
    st.session_state.yt_bulk_df = st.session_state.yt_bulk_df[present + extra]

    ch_canonical = ["channel_url", "title", "subscribers", "total_videos", "total_views", "last_fetched", "category"]
    if "channel_bulk_df" not in st.session_state or not isinstance(st.session_state.channel_bulk_df, pd.DataFrame):
        ch_init = {c: ([""] if c in ("channel_url", "title", "last_fetched", "category") else [0]) for c in ch_canonical}
        st.session_state.channel_bulk_df = pd.DataFrame(ch_init).astype({
            "subscribers": "int64", "total_videos": "int64", "total_views": "int64"
        })
    for col in ch_canonical:
        if col not in st.session_state.channel_bulk_df.columns:
            st.session_state.channel_bulk_df[col] = "" if col in ("channel_url", "title", "last_fetched", "category") else 0
    present_ch = [c for c in ch_canonical if c in st.session_state.channel_bulk_df.columns]
    extra_ch = [c for c in st.session_state.channel_bulk_df.columns if c not in ch_canonical]
    st.session_state.channel_bulk_df = st.session_state.channel_bulk_df[present_ch + extra_ch]

    st.subheader("📹 YouTube Video Fetch")
    st.caption("Masukkin URL YouTube ke **Kolom A (URL)**. Klik tombol Fetch untuk auto isi Judul, View, Like, Comment. Maksimal **200 baris** untuk versi gratis.")

    ptype = st.session_state.get("current_project_type", "youtube")
    if ptype != "youtube":
        st.info("💡 Current project is X Profile. YouTube table edits here are in-memory only. Load or create a YouTube project from the sidebar (use the \"➕ New YouTube Project\" button) to persist.")

        # Row counter + add rows
        current_count = len(st.session_state.yt_bulk_df)
        row_col, add_col = st.columns([3, 1])
        with row_col:
            if current_count >= MAX_FREE_ROWS:
                st.error(f"🚫 Sudah mencapai batas maksimal {MAX_FREE_ROWS} baris (versi gratis). Untuk unlimited, buat versi berbayar.")
            else:
                remaining = MAX_FREE_ROWS - current_count
                st.info(f"📊 Baris terisi: **{current_count} / {MAX_FREE_ROWS}** (sisa {remaining})")

        with add_col:
            if st.button("➕ Tambah 5 Baris Kosong", width="stretch"):
                if current_count < MAX_FREE_ROWS:
                    to_add = min(5, MAX_FREE_ROWS - current_count)
                    new_rows = pd.DataFrame({
                        "url": [""] * to_add,
                        "title": [""] * to_add,
                        "views": [0] * to_add,
                        "likes": [0] * to_add,
                        "comments": [0] * to_add,
                        "last_fetched": [""] * to_add,
                    })
                    st.session_state.yt_bulk_df = pd.concat(
                        [st.session_state.yt_bulk_df, new_rows], ignore_index=True
                    )
                    st.rerun()

    # Paste many URLs at once (very useful)
    with st.expander("📋 Paste Banyak URL Sekaligus (recommended)"):
        pasted_urls = st.text_area(
            "Tempel banyak URL di sini (satu per baris atau dipisah koma)",
            height=80,
            placeholder="https://youtu.be/xxx\nhttps://youtube.com/watch?v=yyy\n..."
        )
        if st.button("Tambah URL ke Tabel"):
            if pasted_urls:
                # Split by newline or comma
                raw = [u.strip() for u in pasted_urls.replace(",", "\n").split("\n") if u.strip()]
                existing = set(st.session_state.yt_bulk_df["url"].astype(str).tolist())
                added = 0
                for u in raw:
                    if u not in existing and len(st.session_state.yt_bulk_df) < MAX_FREE_ROWS:
                        new_row = pd.DataFrame([{
                            "url": u, "title": "", "views": 0, "likes": 0, "comments": 0, "last_fetched": ""
                        }])
                        st.session_state.yt_bulk_df = pd.concat(
                            [st.session_state.yt_bulk_df, new_row], ignore_index=True
                        )
                        existing.add(u)
                        added += 1
                    if len(st.session_state.yt_bulk_df) >= MAX_FREE_ROWS:
                        break
                if added > 0:
                    st.success(f"Berhasil menambahkan {added} URL baru ke tabel.")
                    st.rerun()
                else:
                    st.warning("Tidak ada URL baru yang ditambahkan (mungkin sudah ada atau sudah penuh 200).")

    st.divider()

    # THE MAIN EDITABLE TABLE - like Excel/GSheet
    st.subheader("Tabel Video YouTube")

    # Prepare display copy with formatted numbers (dots for thousands - Indonesian style)
    # Keep backend numeric for logic/export; use formatted strings only for nice UI
    display_yt = st.session_state.yt_bulk_df.copy()
    for col in ["views", "likes", "comments"]:
        if col in display_yt.columns:
            display_yt[col] = display_yt[col].apply(
                lambda x: f"{int(x):,}".replace(",", ".") if pd.notna(x) else ""
            )

    # Re-apply canonical order on the display copy too (belt and suspenders)
    display_yt = display_yt[[c for c in yt_canonical if c in display_yt.columns] + [c for c in display_yt.columns if c not in yt_canonical]]

    edited_df = st.data_editor(
        display_yt,  # use formatted for nice display
        column_order=yt_canonical,  # force logical order Kol A → F no matter what the DF has internally
        column_config={
            "url": st.column_config.TextColumn(
                "URL / Video ID (Kol A)",
                help="Paste link YouTube atau ID video di sini",
                width="large",
                required=False,
            ),
            "title": st.column_config.TextColumn(
                "Judul Video (Kol B)", 
                disabled=True,
                width="medium"
            ),
            "views": st.column_config.TextColumn(  # use TextColumn to show pre-formatted with dots
                "Views (Kol C)", 
                disabled=True,
                width="medium"
            ),
            "likes": st.column_config.TextColumn(
                "Likes (Kol D)", 
                disabled=True,
                width="medium"
            ),
            "comments": st.column_config.TextColumn(
                "Comments (Kol E)", 
                disabled=True,
                width="medium"
            ),
            "last_fetched": st.column_config.TextColumn(
                "Last Fetched", 
                disabled=True
            ),
        },
        num_rows="dynamic",
        width="stretch",
        hide_index=True,
        key="yt_bulk_table"
    )

    # Sync only the editable input column (url); keep numeric backend clean
    # Defensive: only if the editor result actually contains the column (protects against tab switches / partial renders)
    if isinstance(edited_df, pd.DataFrame) and "url" in edited_df.columns:
        st.session_state.yt_bulk_df["url"] = edited_df["url"]
    # Keep numbers as int after any operations
    try:
        st.session_state.yt_bulk_df = st.session_state.yt_bulk_df.astype({
            "views": "int64",
            "likes": "int64",
            "comments": "int64",
        })
    except:
        pass
    # Only auto-persist if this project is actually a YouTube project (prevent overwriting X data)
    if st.session_state.get("current_project_type", "youtube") == "youtube":
        save_current_project()

    # Main action buttons
    btn1, btn2, btn3 = st.columns([2.2, 1, 1])

    with btn1:
        if st.button("🚀 FETCH / UPDATE METRICS (Semua Video)", type="primary", width="stretch"):
            if not can_fetch_more():
                st.error("Free quota habis (200 video). Silakan upgrade untuk melanjutkan.")
                st.info("Hubungi admin atau gunakan tombol Upgrade di sidebar (akan diintegrasikan dengan payment gateway).")
            else:
                df = st.session_state.yt_bulk_df.copy()
                urls = df["url"].tolist()

                # Extract IDs
                video_ids = []
                id_to_row = {}
                for i, u in enumerate(urls):
                    vid = extract_video_id(u)
                    if vid:
                        video_ids.append(vid)
                        id_to_row[vid] = i

                if not video_ids:
                    st.warning("Tidak ada URL/ID YouTube yang valid di kolom A.")
                else:
                    progress = st.progress(0, text="Mengambil data dari YouTube API...")
                    try:
                        fetched_data = fetch_youtube_videos_batch(video_ids)
                        updated = 0

                        for vid, data in fetched_data.items():
                            if vid in id_to_row:
                                row_idx = id_to_row[vid]
                                if "error" not in data:
                                    df.at[row_idx, "title"] = data.get("title", "")
                                    df.at[row_idx, "views"] = data.get("views", 0)
                                    df.at[row_idx, "likes"] = data.get("likes", 0)
                                    df.at[row_idx, "comments"] = data.get("comments", 0)
                                    df.at[row_idx, "last_fetched"] = data.get("last_fetched", "")
                                    updated += 1
                                else:
                                    df.at[row_idx, "title"] = f"[ERROR] {data['error']}"

                        st.session_state.yt_bulk_df = df

                        # Increment quota only for successful fetches
                        if updated > 0 and st.session_state.user:
                            db.update_user_quota(st.session_state.user["id"], updated)
                            # Auto save after fetch
                            save_current_project()

                        progress.progress(1.0, text="Selesai!")
                        st.success(f"✅ Berhasil update {updated} video dari YouTube!")
                        time.sleep(0.6)
                        st.rerun()

                    except Exception as e:
                        st.error(f"Gagal mengambil data: {e}")

    with btn2:
        if st.button("🗑️ Clear Tabel", width="stretch"):
            st.session_state.yt_bulk_df = pd.DataFrame({
                "url": [""] * 5,
                "title": [""] * 5,
                "views": [0] * 5,
                "likes": [0] * 5,
                "comments": [0] * 5,
                "last_fetched": [""] * 5,
            })
            st.rerun()

    with btn3:
        # Format numbers with dot separators for rapi export
        yt_export = st.session_state.yt_bulk_df.copy()
        for col in ["views", "likes", "comments"]:
            if col in yt_export.columns:
                yt_export[col] = yt_export[col].apply(
                    lambda x: f"{int(x):,}".replace(",", ".") if pd.notna(x) else ""
                )
        csv_data = yt_export.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="⬇️ Export CSV",
            data=csv_data,
            file_name=f"ytx_youtube_table_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
            width="stretch"
        )

    # Summary
    if "title" in st.session_state.yt_bulk_df.columns:
        filled_count = (st.session_state.yt_bulk_df["title"].astype(str) != "").sum()
    else:
        filled_count = 0
    st.caption(f"📌 {filled_count} video sudah terisi data | Total baris: {len(st.session_state.yt_bulk_df)}")

    # ==================== GOOGLE SHEETS EXPORT (OTOMATIS - TANPA SETUP) ====================
    st.divider()
    st.subheader("📤 Export ke Google Sheets (Paling Otomatis - Tanpa Setup)")

    with st.container(border=True):
        st.markdown("### ✅ Export Super Gampang (Rekomendasi)")
        st.caption("User tinggal klik, download, buka Sheets. **Tidak perlu bikin Service Account, tidak perlu aktifin API, tidak perlu upload JSON.**")

        col1, col2 = st.columns(2)

        with col1:
            # Format numbers with dot separators for rapi export
            yt_export = st.session_state.yt_bulk_df.copy()
            for col in ["views", "likes", "comments"]:
                if col in yt_export.columns:
                    yt_export[col] = yt_export[col].apply(
                        lambda x: f"{int(x):,}".replace(",", ".") if pd.notna(x) else ""
                    )
            csv_data = yt_export.to_csv(index=False).encode("utf-8")
            st.download_button(
                label="📥 1. Download Data sebagai CSV",
                data=csv_data,
                file_name=f"ytx_youtube_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
                width="stretch",
                key="gs_easy_csv"
            )

        with col2:
            st.link_button(
                label="📊 2. Buka Google Sheets Baru",
                url="https://sheets.new",
                width="stretch"
            )

        st.markdown("""
        **Langkah (cuma 10 detik):**
        1. Klik tombol **Download CSV** di atas
        2. Klik tombol **Buka Google Sheets Baru** (otomatis buka tab baru)
        3. Di Google Sheets yang baru terbuka:
           - Tekan `Ctrl + Shift + V` (atau `Cmd + Shift + V` di Mac) di cell **A1**, atau
           - Pergi ke **File → Import → Upload** → pilih file CSV yang tadi didownload
        4. Selesai! Semua data (URL, Judul, View, Like, Comment) langsung rapi di tabel.
        """)

        st.info("💡 Tips: Setelah data masuk, kamu bisa langsung kasih nama sheet-nya dan share seperti biasa.")

    # End of Video Fetch content (bulk table above)

    st.divider()
    st.subheader("📺 YouTube Channel Fetch")
    st.caption("Bulk table untuk channel YouTube. Input URL/@handle di kolom Channel URL, klik Fetch untuk isi Subscribers, Total Videos, Total Views (kategori dari YouTube API). Bisa banyak channel sekaligus. Export ke CSV atau Google Sheets.")

    # Row counter + add rows for channel
    ch_current = len(st.session_state.channel_bulk_df)
    ch_row_col, ch_add_col = st.columns([3, 1])
    with ch_row_col:
        if ch_current >= MAX_FREE_ROWS:
            st.error(f"🚫 Sudah mencapai batas maksimal {MAX_FREE_ROWS} baris (versi gratis).")
        else:
            ch_remaining = MAX_FREE_ROWS - ch_current
            st.info(f"📊 Channel terisi: **{ch_current} / {MAX_FREE_ROWS}** (sisa {ch_remaining})")

    with ch_add_col:
        if st.button("➕ Tambah 5 Channel Kosong", width="stretch", key="add_ch_rows"):
            if ch_current < MAX_FREE_ROWS:
                to_add = min(5, MAX_FREE_ROWS - ch_current)
                new_ch = pd.DataFrame({
                    "channel_url": [""] * to_add,
                    "title": [""] * to_add,
                    "subscribers": [0] * to_add,
                    "total_videos": [0] * to_add,
                    "total_views": [0] * to_add,
                    "last_fetched": [""] * to_add,
                    "category": [""] * to_add,
                }).astype({
                    "subscribers": "int64",
                    "total_videos": "int64",
                    "total_views": "int64",
                })
                st.session_state.channel_bulk_df = pd.concat(
                    [st.session_state.channel_bulk_df, new_ch], ignore_index=True
                )
                st.rerun()

    # Paste many channel URLs
    with st.expander("📋 Paste Banyak Channel URL Sekaligus"):
        pasted_ch = st.text_area(
            "Tempel banyak URL/@handle di sini (satu per baris)",
            height=80,
            placeholder="https://www.youtube.com/@mkbhd\nhttps://youtube.com/c/somechannel\nUC..."
        )
        if st.button("Tambah Channel ke Tabel", key="paste_ch"):
            if pasted_ch:
                raw = [u.strip() for u in pasted_ch.replace(",", "\n").split("\n") if u.strip()]
                existing = set(st.session_state.channel_bulk_df["channel_url"].astype(str).tolist())
                added = 0
                for u in raw:
                    if u not in existing and len(st.session_state.channel_bulk_df) < MAX_FREE_ROWS:
                        new_row = pd.DataFrame([{
                            "channel_url": u, "title": "", "subscribers": 0, "total_videos": 0, "total_views": 0, "last_fetched": "", "category": ""
                        }]).astype({
                            "subscribers": "int64",
                            "total_videos": "int64",
                            "total_views": "int64",
                        })
                        st.session_state.channel_bulk_df = pd.concat(
                            [st.session_state.channel_bulk_df, new_row], ignore_index=True
                        )
                        existing.add(u)
                        added += 1
                    if len(st.session_state.channel_bulk_df) >= MAX_FREE_ROWS:
                        break
                if added > 0:
                    st.success(f"Berhasil menambahkan {added} channel baru.")
                    st.rerun()
                else:
                    st.warning("Tidak ada channel baru yang ditambahkan.")

    st.divider()

    # THE MAIN CHANNEL TABLE
    st.subheader("Tabel Channel YouTube")

    # Prepare display copy with formatted numbers (dots for thousands - Indonesian style)
    # Keep backend numeric for logic/export, use formatted strings only for nice UI display
    display_ch = st.session_state.channel_bulk_df.copy()
    for col in ["subscribers", "total_videos", "total_views"]:
        if col in display_ch.columns:
            display_ch[col] = display_ch[col].apply(
                lambda x: f"{int(x):,}".replace(",", ".") if pd.notna(x) else ""
            )
    # Force canonical column order on display copy
    display_ch = display_ch[[c for c in ch_canonical if c in display_ch.columns] + [c for c in display_ch.columns if c not in ch_canonical]]

    edited_ch = st.data_editor(
        display_ch,  # use formatted for display
        column_order=ch_canonical,  # force consistent structure
        column_config={
            "channel_url": st.column_config.TextColumn(
                "Channel URL / @handle / UCID (Input di sini)",
                help="Paste link channel atau @handle",
                width="large",
                required=False,
            ),
            "title": st.column_config.TextColumn(
                "Channel Name", 
                disabled=True,
                width="medium"
            ),
            "subscribers": st.column_config.TextColumn(  # use Text to show formatted string
                "Subscribers", 
                disabled=True,
                width="medium"
            ),
            "total_videos": st.column_config.TextColumn(
                "Total Videos", 
                disabled=True,
                width="medium"
            ),
            "total_views": st.column_config.TextColumn(
                "Total Views", 
                disabled=True,
                width="medium"
            ),
            "last_fetched": st.column_config.TextColumn(
                "Last Fetched", 
                disabled=True
            ),
            "category": st.column_config.TextColumn(
                "Category",
                help="From YouTube API (topic categories). Edit manually if needed.",
                width="medium"
            ),
        },
        num_rows="dynamic",
        width="stretch",
        key="ch_data_editor"
    )
    # Sync only editable columns back; keep numeric backend as-is
    for col in ["channel_url", "category"]:
        if col in edited_ch.columns:
            st.session_state.channel_bulk_df[col] = edited_ch[col]
    if st.session_state.get("current_project_id") and st.session_state.user:
        save_current_project()

    # Fetch for channels
    if st.button("🚀 FETCH CHANNEL STATS (Semua / Selected)", type="primary", width="stretch", key="fetch_ch_all"):
        df_ch = st.session_state.channel_bulk_df.copy()
        urls = df_ch["channel_url"].tolist()
        valid = [u for u in urls if u and u.strip()]
        if not valid:
            st.warning("Tidak ada channel URL di tabel.")
        else:
            progress = st.progress(0, text="Fetching channel stats...")
            updated = 0
            for idx, row in df_ch.iterrows():
                url = str(row.get("channel_url", "")).strip()
                if not url:
                    continue
                try:
                    # Extract ID or handle
                    ch_id = url
                    if "channel/" in url:
                        ch_id = url.split("channel/")[-1].split("/")[0].split("?")[0]
                    elif "@" in url:
                        ch_id = url.split("@")[-1].split("/")[0].split("?")[0]
                    data = fetch_youtube_channel(ch_id)
                    if data:
                        df_ch.at[idx, "title"] = data.get("title", "")
                        df_ch.at[idx, "subscribers"] = data.get("subscriber_count", 0)
                        df_ch.at[idx, "total_videos"] = data.get("video_count", 0)
                        df_ch.at[idx, "total_views"] = data.get("view_count", 0)
                        df_ch.at[idx, "last_fetched"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                        df_ch.at[idx, "category"] = data.get("category", "Other")
                        updated += 1
                except Exception as e:
                    df_ch.at[idx, "title"] = f"Error: {str(e)[:50]}"
                progress.progress((idx + 1) / len(df_ch), text=f"Processing {idx+1}/{len(df_ch)}")
            # Ensure numeric columns stay int
            df_ch = df_ch.astype({
                "subscribers": "int64",
                "total_videos": "int64",
                "total_views": "int64",
            })
            st.session_state.channel_bulk_df = df_ch
            if updated > 0 and st.session_state.user:
                db.update_user_quota(st.session_state.user["id"], updated)
                db.increment_user_api_usage(st.session_state.user["id"], youtube=updated)
                save_current_project()
            progress.progress(1.0, text="Selesai!")
            st.success(f"✅ Berhasil update {updated} channel!")
            st.rerun()

    # Export for channel table (CSV + Google Sheets)
    st.divider()
    st.subheader("📤 Export Channel Stats ke Google Sheets / CSV")

    with st.container(border=True):
        col1, col2 = st.columns(2)
        with col1:
            # Format numbers nicely with dot thousand separator for export (rapi di CSV & Sheets)
            df_export = st.session_state.channel_bulk_df.copy()
            for col in ["subscribers", "total_videos", "total_views"]:
                if col in df_export.columns:
                    df_export[col] = df_export[col].apply(
                        lambda x: f"{int(x):,.0f}".replace(",", ".") if pd.notna(x) else ""
                    )
            csv_ch = df_export.to_csv(index=False).encode("utf-8")
            st.download_button(
                label="📥 1. Download CSV",
                data=csv_ch,
                file_name=f"ytx_channels_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
                width="stretch"
            )
        with col2:
            st.link_button(
                label="📊 2. Buka Google Sheets Baru",
                url="https://sheets.new",
                width="stretch"
            )

        st.markdown("""
        **Langkah (10 detik):**
        1. Download CSV
        2. Buka sheets.new
        3. Ctrl/Cmd + Shift + V di A1
        """)

    st.info("💡 Tips: Tabel ini terpisah dari Video Fetch. Kategori diambil langsung dari YouTube API (topicDetails). Input channel URL di kolom pertama, fetch untuk isi stats.")

with x_data_tab:
    st.header("X (Twitter) Data")
    st.subheader("𝕏 X Profile Checker (Bulk)")
    st.caption("Paste X profile URLs or @handles. Fetch populates username, display name, followers, verified (blue tick), and Niche/Category — powered by Grok (xAI) when key is set. Niche is always editable manually.")

    # Defensive schema repair for X (same reason as YT: protect against mixed/old saved data)
    if "x_bulk_df" not in st.session_state or not isinstance(st.session_state.x_bulk_df, pd.DataFrame):
        st.session_state.x_bulk_df = pd.DataFrame({
            "url": [""], "username": [""], "name": [""], "followers": [0],
            "verified": [""], "niche": [""], "last_fetched": [""]
        })
    for col in ["url", "username", "name", "verified", "niche", "last_fetched"]:
        if col not in st.session_state.x_bulk_df.columns:
            st.session_state.x_bulk_df[col] = ""
    if "followers" not in st.session_state.x_bulk_df.columns:
        st.session_state.x_bulk_df["followers"] = 0

    ptype = st.session_state.get("current_project_type", "youtube")
    if ptype != "x_profile":
        st.info("💡 Current project is YouTube. X data edits here stay in-memory only. Load or create an **X Profile** project from the sidebar (use the \"➕ New X Profile Project\" button) to persist.")

    if not (XAI_API_KEY and XAI_API_KEY != "your_xai_api_key_here"):
        st.caption("ℹ️ Grok (xAI) niche inference is disabled — using keyword fallback. Get free xAI API key at https://x.ai/api and add XAI_API_KEY=... to .env (then restart).")

    x_client = get_x_client()

    if not x_client:
        st.warning("X API credentials belum diset di .env — fetch tidak akan jalan. Tambah X_BEARER_TOKEN di .env lalu restart. (Tabel & export CSV tetap bisa dipakai untuk input manual.)")

    # Row counter + add rows (shared quota with YT)
    current_count = len(st.session_state.x_bulk_df)
    row_col, add_col = st.columns([3, 1])
    with row_col:
        if current_count >= MAX_FREE_ROWS:
            st.error(f"🚫 Sudah mencapai batas maksimal {MAX_FREE_ROWS} baris (versi gratis).")
        else:
            remaining = MAX_FREE_ROWS - current_count
            st.info(f"📊 Baris terisi: **{current_count} / {MAX_FREE_ROWS}** (sisa {remaining})")

    with add_col:
        if st.button("➕ Tambah 5 Baris Kosong", width="stretch", key="x_add_rows"):
            if current_count < MAX_FREE_ROWS:
                to_add = min(5, MAX_FREE_ROWS - current_count)
                new_rows = pd.DataFrame({
                    "url": [""] * to_add,
                    "username": [""] * to_add,
                    "name": [""] * to_add,
                    "followers": [0] * to_add,
                    "verified": [""] * to_add,
                    "niche": [""] * to_add,
                    "last_fetched": [""] * to_add,
                })
                st.session_state.x_bulk_df = pd.concat(
                    [st.session_state.x_bulk_df, new_rows], ignore_index=True
                )
                st.rerun()

    # Paste many handles/URLs
    with st.expander("📋 Paste Banyak URL / @handle Sekaligus (recommended)"):
        pasted = st.text_area(
            "Tempel banyak X URL atau @username (satu per baris atau koma)",
            height=80,
            placeholder="https://x.com/elonmusk\n@naval\nhttps://twitter.com/OpenAI\n...",
            key="x_paste_area"
        )
        if st.button("Tambah ke Tabel X", key="x_paste_btn"):
            if pasted:
                raw = [u.strip() for u in pasted.replace(",", "\n").split("\n") if u.strip()]
                existing = set(st.session_state.x_bulk_df["url"].astype(str).tolist())
                added = 0
                for u in raw:
                    if u not in existing and len(st.session_state.x_bulk_df) < MAX_FREE_ROWS:
                        new_row = pd.DataFrame([{
                            "url": u, "username": "", "name": "", "followers": 0, "verified": "", "niche": "", "last_fetched": ""
                        }])
                        st.session_state.x_bulk_df = pd.concat(
                            [st.session_state.x_bulk_df, new_row], ignore_index=True
                        )
                        existing.add(u)
                        added += 1
                    if len(st.session_state.x_bulk_df) >= MAX_FREE_ROWS:
                        break
                if added > 0:
                    st.success(f"Berhasil menambahkan {added} profile baru.")
                    st.rerun()
                else:
                    st.warning("Tidak ada yang ditambahkan (duplikat atau penuh).")

    st.divider()
    st.subheader("Tabel X Profiles")

    # Prepare display copy with formatted followers (dots for thousands - Indonesian style)
    # Keep backend numeric (int) for logic/export
    display_x = st.session_state.x_bulk_df.copy()
    if "followers" in display_x.columns:
        display_x["followers"] = display_x["followers"].apply(
            lambda x: f"{int(x):,}".replace(",", ".") if pd.notna(x) else ""
        )
    # Force canonical order (consistent with YT/Channel tables)
    x_canonical = ["url", "username", "name", "followers", "verified", "niche", "last_fetched"]
    display_x = display_x[[c for c in x_canonical if c in display_x.columns] + [c for c in display_x.columns if c not in x_canonical]]

    edited_x = st.data_editor(
        display_x,  # pre-formatted for nice display
        column_order=x_canonical,
        column_config={
            "url": st.column_config.TextColumn(
                "X URL / @handle (input)",
                help="Paste https://x.com/xxx atau @xxx atau username",
                width="large",
            ),
            "username": st.column_config.TextColumn("Username", disabled=True, width="small"),
            "name": st.column_config.TextColumn("Display Name", disabled=True, width="medium"),
            "followers": st.column_config.TextColumn("Followers", disabled=True, width="small"),  # TextColumn so dots show
            "verified": st.column_config.TextColumn("Blue Tick", disabled=True, width="small"),
            "niche": st.column_config.TextColumn(
                "Niche / Category (Grok-powered + edit manual)",
                help="Grok (xAI) intelligently classifies based on full profile. Add XAI_API_KEY to .env to enable (falls back to keywords). Edit manually anytime.",
                width="medium",
            ),
            "last_fetched": st.column_config.TextColumn("Last Fetched", disabled=True),
        },
        num_rows="dynamic",
        width="stretch",
        hide_index=True,
        key="x_bulk_table"
    )

    # Sync only editable input columns back; preserve numeric backend for followers
    # Defensive guards protect against cross-tab execution or dynamic row editor edge cases
    if isinstance(edited_x, pd.DataFrame):
        if "url" in edited_x.columns:
            st.session_state.x_bulk_df["url"] = edited_x["url"]
        if "niche" in edited_x.columns:
            st.session_state.x_bulk_df["niche"] = edited_x["niche"]
    # Ensure followers stays int64
    try:
        st.session_state.x_bulk_df["followers"] = pd.to_numeric(st.session_state.x_bulk_df["followers"], errors="coerce").fillna(0).astype("int64")
    except:
        pass
    # Only auto-persist if this project is actually an X project (prevent overwriting YT data with X schema)
    if st.session_state.get("current_project_type") == "x_profile":
        save_current_project()

    # Action buttons
    b1, b2, b3 = st.columns([2.2, 1, 1])
    with b1:
        if st.button("🚀 FETCH X PROFILES (Semua Baris)", type="primary", width="stretch", key="x_fetch_all"):
            if not can_fetch_more():
                st.error("Free quota habis (200). Upgrade untuk lanjut.")
            else:
                df = st.session_state.x_bulk_df.copy()
                inputs = df["url"].tolist()

                valid_inputs = [i for i in inputs if extract_x_username(i)]
                if not valid_inputs:
                    st.warning("Tidak ada URL/@handle X yang valid di kolom pertama.")
                else:
                    progress = st.progress(0, text="Fetching profiles dari X API...")
                    try:
                        fetched = fetch_x_profiles_batch(inputs)  # pass all, it will extract inside
                        updated = 0

                        for idx, row in df.iterrows():
                            inp = str(row.get("url", "")).strip()
                            uname = extract_x_username(inp)
                            if uname and uname in fetched:
                                fd = fetched[uname]
                                if "error" not in fd:
                                    df.at[idx, "username"] = fd.get("username", uname)
                                    df.at[idx, "name"] = fd.get("name", "")
                                    df.at[idx, "followers"] = fd.get("followers", 0)
                                    df.at[idx, "verified"] = fd.get("verified", "")
                                    df.at[idx, "niche"] = fd.get("niche", "")
                                    df.at[idx, "last_fetched"] = fd.get("last_fetched", "")
                                    updated += 1
                                else:
                                    df.at[idx, "name"] = f"[ERROR] {fd['error']}"

                        st.session_state.x_bulk_df = df

                        if updated > 0 and st.session_state.user:
                            db.update_user_quota(st.session_state.user["id"], updated)
                            save_current_project()

                        progress.progress(1.0, text="Selesai!")
                        st.success(f"✅ Berhasil update {updated} X profiles!")
                        time.sleep(0.5)
                        st.rerun()

                    except Exception as e:
                        st.error(f"Gagal fetch X: {e}")

    with b2:
        if st.button("🗑️ Clear Tabel X", width="stretch", key="x_clear"):
            st.session_state.x_bulk_df = pd.DataFrame({
                "url": [""] * 5,
                "username": [""] * 5,
                "name": [""] * 5,
                "followers": [0] * 5,
                "verified": [""] * 5,
                "niche": [""] * 5,
                "last_fetched": [""] * 5,
            })
            st.rerun()

    with b3:
        # Format numbers with dot separators for rapi export (same as table)
        x_export = st.session_state.x_bulk_df.copy()
        if "followers" in x_export.columns:
            x_export["followers"] = x_export["followers"].apply(
                lambda x: f"{int(x):,}".replace(",", ".") if pd.notna(x) else ""
            )
        csvx = x_export.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Export CSV X",
            data=csvx,
            file_name=f"ytx_x_profiles_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
            width="stretch",
            key="x_export_csv"
        )

    # Summary
    if "username" in st.session_state.x_bulk_df.columns:
        filled_x = (st.session_state.x_bulk_df["username"].astype(str) != "").sum()
    else:
        filled_x = 0
    st.caption(f"📌 {filled_x} profiles sudah terisi data | Total baris: {len(st.session_state.x_bulk_df)}")

    # GSheet export (same easy flow)
    st.divider()
    st.subheader("📤 Export X Profiles ke Google Sheets (Super Gampang)")
    with st.container(border=True):
        col1, col2 = st.columns(2)
        with col1:
            # Format numbers with dot separators for rapi export (same as table)
            x_export2 = st.session_state.x_bulk_df.copy()
            if "followers" in x_export2.columns:
                x_export2["followers"] = x_export2["followers"].apply(
                    lambda x: f"{int(x):,}".replace(",", ".") if pd.notna(x) else ""
                )
            csvx2 = x_export2.to_csv(index=False).encode("utf-8")
            st.download_button(
                "📥 1. Download CSV X Profiles",
                data=csvx2,
                file_name=f"ytx_x_profiles_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
                width="stretch",
                key="x_gs_csv"
            )
        with col2:
            st.link_button("📊 2. Buka Google Sheets Baru", url="https://sheets.new", width="stretch")

        st.markdown("""
        Langkah sama seperti YouTube:
        1. Download CSV
        2. Buka sheets.new
        3. Ctrl/Cmd + Shift + V di A1 atau File → Import
        """)

    # Quick single profile (bonus)
    with st.expander("🔧 Quick Single X Profile (opsional)"):
        qx = st.text_input("X @username atau URL", key="quick_x", placeholder="elonmusk or https://x.com/elonmusk")
        if st.button("Fetch 1 X Profile", key="quick_x_btn"):
            if qx:
                try:
                    res = fetch_x_profiles_batch([qx])
                    uname = extract_x_username(qx) or ""
                    if uname in res and "error" not in res[uname]:
                        d = res[uname]
                        c1, c2, c3 = st.columns(3)
                        c1.metric("Followers", f"{d['followers']:,}")
                        c2.metric("Verified", d.get("verified") or "No")
                        c3.metric("Niche (Grok)", d.get("niche", ""))
                        st.write(f"**{d['name']}** (@{d['username']})")
                    else:
                        st.error(res.get(uname, {}).get("error", "Not found or error"))
                except Exception as e:
                    st.error(str(e))

if is_admin_user and settings_tab is not None:
    with settings_tab:
        st.header("Settings & Configuration")

    # ==================== NEW: API Quota & Monitoring ====================
    st.subheader("🔌 API Quota & Usage Monitoring")
    st.caption("Real-time (session) tracking of external API calls made by this app. For exact remaining quota, check the provider dashboards.")

    usage = st.session_state.get("api_usage", {})
    rate = st.session_state.get("last_x_rate_limit", {})
    q = get_quota_info()

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown("**▶️ YouTube Data API**")
        if YOUTUBE_API_KEY and YOUTUBE_API_KEY != "your_youtube_api_key_here":
            st.success("✅ Key configured")
        else:
            st.error("❌ Key missing")
        st.metric("Calls this session", usage.get("youtube_calls", 0))
        st.caption("Daily quota: ~10,000 units (free tier)\nCheck: [Google Cloud Console](https://console.cloud.google.com/apis/dashboard)")

    with col2:
        st.markdown("**𝕏 X / Twitter API v2**")
        if X_BEARER_TOKEN and X_BEARER_TOKEN != "your_x_bearer_token_here":
            st.success("✅ Bearer token set")
        else:
            st.error("❌ Credentials missing")
        st.metric("Calls this session", usage.get("x_api_calls", 0))
        if rate.get("remaining") is not None:
            st.caption(f"Last known: {rate.get('remaining')}/{rate.get('limit')} remaining")
        else:
            st.caption("Rate limits: 300 requests / 15 min (Bearer)\nCheck: [X Developer Portal](https://developer.x.com/en/portal/dashboard)")

    with col3:
        st.markdown("**🤖 xAI Grok API**")
        if XAI_API_KEY and XAI_API_KEY != "your_xai_api_key_here":
            st.success("✅ Key configured (Grok active)")
        else:
            st.warning("⚠️ Using keyword fallback (no key or placeholder)")
        st.metric("Grok calls (session)", usage.get("grok_calls", 0))
        st.metric("Tokens used (session)", usage.get("grok_total_tokens", 0))
        st.caption("Limits depend on your xAI plan/credits\nCheck: [console.x.ai](https://console.x.ai)")

    # Show persistent per-user stats (fresh from DB)
    if st.session_state.user:
        fresh_user = db.get_user_by_id(st.session_state.user["id"]) or st.session_state.user
        st.markdown("**Your Persistent Usage (lifetime, across sessions)**")
        pcols = st.columns(5)
        pcols[0].metric("Total Quota Used", q.get("used", 0))
        pcols[1].metric("YT Calls", fresh_user.get("youtube_calls", 0))
        pcols[2].metric("X Calls", fresh_user.get("x_calls", 0))
        pcols[3].metric("Grok Calls", fresh_user.get("grok_calls", 0))
        pcols[4].metric("Grok Tokens", fresh_user.get("grok_tokens", 0))

    st.markdown("---")
    if st.button("🔄 Reset session API counters (dev)", key="reset_api_counters"):
        st.session_state.api_usage = {"youtube_calls": 0, "x_api_calls": 0, "grok_calls": 0, "grok_total_tokens": 0}
        st.session_state.last_x_rate_limit = {"remaining": None, "limit": None, "reset": None}
        st.success("Counters reset for this session")
        st.rerun()

    # ==================== ADMIN: User Management ====================
    user = st.session_state.get("user") or {}
    if user.get("is_admin"):
        st.markdown("---")
        st.subheader("👥 User Management (Admin Only)")
        st.caption("Daftar semua user yang terdaftar, status premium, dan penggunaan API per user (persistent).")

        # Force reload database module (important during development when adding new DB functions)
        try:
            import importlib
            importlib.reload(db)
        except Exception:
            pass

        try:
            all_users = db.get_all_users()
        except Exception as e:
            all_users = []
            st.error(f"Gagal load users: {e}")
            st.info("Coba restart aplikasi Streamlit sekali (Ctrl+C lalu jalankan ulang) jika error ini muncul setelah update code.")

        if all_users:
            import pandas as pd
            df_users = pd.DataFrame(all_users)

            # Compute nice columns
            df_users["Premium"] = df_users["is_subscribed"].apply(lambda x: "✅ Yes" if x else "Free")
            df_users["Admin"] = df_users["is_admin"].apply(lambda x: "👑" if x else "")
            df_users["Quota Used"] = df_users["quota_used"]
            df_users["YT Calls"] = df_users.get("youtube_calls", 0)
            df_users["X Calls"] = df_users.get("x_calls", 0)
            df_users["Grok Calls"] = df_users.get("grok_calls", 0)
            df_users["Grok Tokens"] = df_users.get("grok_tokens", 0)

            # Summary stats
            total_users = len(df_users)
            premium_users = int(df_users["is_subscribed"].sum())
            admin_count = int(df_users["is_admin"].sum())
            total_quota = int(df_users["quota_used"].sum())
            total_yt = int(df_users["YT Calls"].sum())
            total_x = int(df_users["X Calls"].sum())

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total Users", total_users)
            c2.metric("Premium Users", f"{premium_users} / {total_users}")
            c3.metric("Admins", admin_count)
            c4.metric("Total Quota Used (all time)", f"{total_quota:,}")

            c1, c2 = st.columns(2)
            c1.metric("Total YouTube API Calls", f"{total_yt:,}")
            c2.metric("Total X API Calls", f"{total_x:,}")

            st.markdown("#### All Users")
            # Bonus: quick project count per user (lightweight)
            try:
                proj_counts = {}
                for uid in df_users["id"].tolist():
                    projs = db.get_user_projects(uid)
                    proj_counts[uid] = len([p for p in projs if not p.get("is_folder")])
                df_users["Projects"] = df_users["id"].map(proj_counts)
            except:
                df_users["Projects"] = 0

            # Show clean table (hide internal ids/hashes)
            display_cols = ["email", "created_at", "Premium", "Admin", "Quota Used", "Projects", "YT Calls", "X Calls", "Grok Calls", "Grok Tokens"]
            st.dataframe(
                df_users[display_cols].rename(columns={"email": "Email", "created_at": "Registered"}),
                width="stretch",
                hide_index=True
            )
            st.caption("Projects = jumlah proyek (bukan folder) milik user tersebut.")

            # Management actions
            st.markdown("#### Manage User")
            emails = [u["email"] for u in all_users]
            selected_email = st.selectbox("Pilih user", emails, key="admin_user_select")

            if selected_email:
                sel_user = next((u for u in all_users if u["email"] == selected_email), None)
                if sel_user:
                    col_a, col_b, col_c, col_d = st.columns(4)

                    with col_a:
                        if st.button("✅ Activate Premium", key="act_prem"):
                            db.set_user_premium(sel_user["id"], True)
                            st.success(f"{selected_email} sekarang Premium!")
                            st.rerun()

                    with col_b:
                        if st.button("❌ Revoke Premium", key="rev_prem"):
                            db.set_user_premium(sel_user["id"], False)
                            st.warning(f"{selected_email} kembali ke Free tier.")
                            st.rerun()

                    with col_c:
                        if st.button("🔄 Reset All Usage + Quota", key="reset_usage"):
                            db.reset_user_usage(sel_user["id"])
                            st.success(f"Quota & API usage untuk {selected_email} di-reset ke 0.")
                            st.rerun()

                    with col_d:
                        if st.button("👑 Toggle Admin", key="toggle_admin"):
                            new_admin = not bool(sel_user.get("is_admin", 0))
                            db.set_user_admin(selected_email, new_admin)
                            st.success(f"{selected_email} admin status: {new_admin}")
                            st.rerun()

                    st.caption(f"Current: Quota used = {sel_user.get('quota_used', 0)} | YT={sel_user.get('youtube_calls',0)} | X={sel_user.get('x_calls',0)} | Grok={sel_user.get('grok_calls',0)} (tokens {sel_user.get('grok_tokens',0)})")

        else:
            st.info("Belum ada user terdaftar.")

        st.markdown("---")

    # Existing env info (kept for reference)
    st.markdown("**Current .env values (masked)**")
    env_data = {
        "NODE_ENV": os.getenv("NODE_ENV", "development"),
        "YOUTUBE_API_KEY": "****" + (YOUTUBE_API_KEY[-6:] if YOUTUBE_API_KEY and len(YOUTUBE_API_KEY) > 6 else ""),
        "X_BEARER_TOKEN": "set" if X_BEARER_TOKEN and X_BEARER_TOKEN != "your_x_bearer_token_here" else "not set",
        "XAI_API_KEY": "set" if XAI_API_KEY and XAI_API_KEY != "your_xai_api_key_here" else "not set",
        "METRICS_REFRESH_INTERVAL": os.getenv("METRICS_REFRESH_INTERVAL", "3600"),
    }
    st.json(env_data)

    st.markdown("---")
    st.caption("Tip: After editing .env, restart the Streamlit app for changes to take effect.")

# Footer
st.divider()
st.caption("Metric Reports • Data is fetched live from APIs • Not stored unless you add DB logic")
