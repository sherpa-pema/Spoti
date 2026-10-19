import os
import sys
import base64
import json
import time
import threading
from typing import Optional, Dict, Any

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import database
from pocketbase.client import ClientResponseError

# Fast in-memory token cache: token -> (payload_dict, cached_at)
_TOKEN_CACHE: Dict[str, tuple] = {}
_CACHE_LOCK = threading.Lock()

# User lookup cache: user_id -> username
_USERNAME_CACHE: Dict[str, str] = {}


def _get_username_for_id(user_id: str) -> str:
    with _CACHE_LOCK:
        if user_id in _USERNAME_CACHE:
            return _USERNAME_CACHE[user_id]

    try:
        admin_pb = database.get_admin_pb()
        user_record = admin_pb.collection("users").get_one(user_id)
        name = getattr(user_record, "name", "") or getattr(user_record, "email", "").split("@")[0] or user_id
        with _CACHE_LOCK:
            _USERNAME_CACHE[user_id] = name
        return name
    except Exception:
        return user_id


def verify_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Verifies a PocketBase user JWT token.
    Returns dict with 'uid' and 'usr' if valid, None otherwise.
    """
    if not token or "." not in token:
        return None

    now = time.time()
    with _CACHE_LOCK:
        if token in _TOKEN_CACHE:
            cached_payload, cached_at = _TOKEN_CACHE[token]
            if cached_payload.get("exp", 0) > now:
                return cached_payload
            else:
                del _TOKEN_CACHE[token]

    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        # Base64 decode payload
        padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        raw_payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))

        if raw_payload.get("exp", 0) < now:
            return None

        user_id = raw_payload.get("id")
        if not user_id:
            return None

        username = _get_username_for_id(user_id)
        payload = {
            "uid": user_id,
            "usr": username,
            "exp": raw_payload.get("exp", 0)
        }

        with _CACHE_LOCK:
            _TOKEN_CACHE[token] = (payload, now)

        return payload
    except Exception:
        return None


def register_user(username: str, password: str) -> Dict[str, Any]:
    """Registers a new user in PocketBase and returns their auth token."""
    username = username.strip()
    if not username or len(username) < 2:
        raise ValueError("Username must be at least 2 characters long.")
    if not password or len(password) < 4:
        raise ValueError("Password must be at least 4 characters long.")

    email = f"{username.lower().replace(' ', '_')}@spoti.local"

    admin_pb = database.get_admin_pb()

    # Check if username or email already taken in PocketBase
    try:
        existing = admin_pb.collection("users").get_list(
            1, 1, query_params={"filter": f'email = "{email}" || name = "{username}"'}
        )
        if existing.total_items > 0:
            raise ValueError(f"Username '{username}' is already taken.")
    except ClientResponseError as e:
        if e.status != 404:
            pass

    try:
        record = admin_pb.collection("users").create({
            "email": email,
            "password": password,
            "passwordConfirm": password,
            "name": username,
            "verified": True
        })
    except ClientResponseError as e:
        err_data = getattr(e, "data", {})
        msg = err_data.get("message") or str(e)
        raise ValueError(f"Registration failed: {msg}")

    # Authenticate immediately to obtain JWT token
    user_pb = database.get_pb()
    auth_data = user_pb.collection("users").auth_with_password(email, password)

    with _CACHE_LOCK:
        _USERNAME_CACHE[record.id] = username

    return {
        "id": record.id,
        "username": username,
        "token": auth_data.token,
        "created_at": time.time()
    }


def authenticate_user(username: str, password: str) -> Dict[str, Any]:
    """Authenticates a user against PocketBase."""
    username = username.strip()
    if not username or not password:
        raise ValueError("Invalid username or password.")

    email = f"{username.lower().replace(' ', '_')}@spoti.local" if "@" not in username else username

    user_pb = database.get_pb()
    try:
        auth_data = user_pb.collection("users").auth_with_password(email, password)
        user_record = auth_data.record
        display_name = getattr(user_record, "name", "") or username

        with _CACHE_LOCK:
            _USERNAME_CACHE[user_record.id] = display_name

        return {
            "id": user_record.id,
            "username": display_name,
            "token": auth_data.token,
            "created_at": getattr(user_record, "created", time.time())
        }
    except Exception:
        # Check by name in case email differs
        try:
            admin_pb = database.get_admin_pb()
            found = admin_pb.collection("users").get_list(1, 1, query_params={"filter": f'name = "{username}"'})
            if found.total_items > 0:
                user_email = found.items[0].email
                auth_data = user_pb.collection("users").auth_with_password(user_email, password)
                user_record = auth_data.record
                return {
                    "id": user_record.id,
                    "username": getattr(user_record, "name", "") or username,
                    "token": auth_data.token,
                    "created_at": getattr(user_record, "created", time.time())
                }
        except Exception:
            pass

        raise ValueError("Invalid username or password.")


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    """Fetches user details by PocketBase user ID."""
    try:
        admin_pb = database.get_admin_pb()
        user = admin_pb.collection("users").get_one(user_id)
        return {
            "id": user.id,
            "username": getattr(user, "name", "") or getattr(user, "email", "").split("@")[0],
            "email": getattr(user, "email", ""),
            "created_at": getattr(user, "created", "")
        }
    except Exception:
        return None
