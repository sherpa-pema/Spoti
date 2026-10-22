import os
import re
import sys
import base64
import json
import time
import threading
from typing import Optional, Dict, Any
from cachetools import TTLCache

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import database
from pocketbase.client import ClientResponseError


class AuthServiceUnavailableError(Exception):
    """Raised when PocketBase is unreachable or fails with 5xx during auth."""
    pass


# Fast in-memory token cache: token -> payload_dict (bounded TTL)
_TOKEN_CACHE: TTLCache = TTLCache(maxsize=300, ttl=1800)
_CACHE_LOCK = threading.Lock()

# User lookup cache: user_id -> username (bounded TTL)
_USERNAME_CACHE: TTLCache = TTLCache(maxsize=300, ttl=86400)



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
    Cryptographically verifies a PocketBase user JWT token.
    Returns dict with 'uid', 'usr', and 'exp' if valid.
    Raises AuthServiceUnavailableError if PocketBase cannot be reached.
    Returns None if token is invalid or expired.
    """
    if not token or "." not in token:
        return None

    now = time.time()
    with _CACHE_LOCK:
        if token in _TOKEN_CACHE:
            payload = _TOKEN_CACHE[token]
            if payload.get("exp", 0) > now:
                return payload
            else:
                try:
                    del _TOKEN_CACHE[token]
                except KeyError:
                    pass

    try:
        user_pb = database.get_user_pb(token)
        refreshed = user_pb.collection("users").auth_refresh()
        user_record = refreshed.record

        user_id = user_record.id
        username = getattr(user_record, "name", "") or getattr(user_record, "email", "").split("@")[0] or user_id

        # Cache valid verified token for up to 30 mins
        payload = {
            "uid": user_id,
            "usr": username,
            "exp": now + 1800
        }

        with _CACHE_LOCK:
            _TOKEN_CACHE[token] = payload
            _USERNAME_CACHE[user_id] = username

        return payload
    except ClientResponseError as e:
        status = getattr(e, "status", 0)
        if status in (400, 401, 403, 404):
            return None
        # PocketBase server error (5xx)
        raise AuthServiceUnavailableError(f"PocketBase auth failed with status {status}") from e
    except (ConnectionError, TimeoutError, OSError) as e:
        # Network or connection dropped to PocketBase
        raise AuthServiceUnavailableError(f"PocketBase connection unreachable: {e}") from e
    except Exception as e:
        # If string contains connection refused or reset
        err_msg = str(e).lower()
        if "connect" in err_msg or "refused" in err_msg or "timeout" in err_msg or "502" in err_msg or "503" in err_msg:
            raise AuthServiceUnavailableError(f"PocketBase connection unreachable: {e}") from e
        return None


def register_user(username: str, password: str) -> Dict[str, Any]:
    """Registers a new user in PocketBase and returns their auth token."""
    username = username.strip()
    if not username or len(username) < 2 or len(username) > 32:
        raise ValueError("Username must be between 2 and 32 characters long.")
    if not re.match(r"^[A-Za-z0-9_.\- ]+$", username):
        raise ValueError("Username contains invalid characters. Use letters, numbers, spaces, dots, hyphens, or underscores.")
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters long.")
    if len(password) > 128:
        raise ValueError("Password must not exceed 128 characters.")

    email = f"{username.lower().replace(' ', '_')}@spoti.local"

    admin_pb = database.get_admin_pb()

    # Check if username or email already taken in PocketBase with escaped parameters
    esc_email = database.pb_escape(email)
    esc_username = database.pb_escape(username)
    try:
        existing = admin_pb.collection("users").get_list(
            1, 1, query_params={"filter": f'email = "{esc_email}" || name = "{esc_username}"'}
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
            esc_username = database.pb_escape(username)
            found = admin_pb.collection("users").get_list(1, 1, query_params={"filter": f'name = "{esc_username}"'})
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
