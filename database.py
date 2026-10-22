import os
import sys
import threading
import time
from typing import Optional, Dict, Any
from pocketbase import PocketBase
from pocketbase.client import ClientResponseError

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_env():
    """Loads configuration variables from .env files without external dependencies."""
    env_paths = [
        os.path.join(PROJECT_DIR, ".env"),
        "/var/mobile/server/data/spoti.env",
        "/var/mobile/server/.env"
    ]
    for p in env_paths:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k and k not in os.environ:
                                os.environ[k] = v
            except Exception:
                pass

_load_env()

PB_URL = os.environ.get("POCKETBASE_URL", "http://192.168.1.67:8090")
SUPERUSER_EMAIL = os.environ.get("PB_SUPERUSER_EMAIL", "")
SUPERUSER_PASSWORD = os.environ.get("PB_SUPERUSER_PASSWORD", "")

_ADMIN_CLIENT: Optional[PocketBase] = None
_ADMIN_TOKEN_EXP: float = 0
_ADMIN_LOCK = threading.Lock()


def get_pb() -> PocketBase:
    """Returns an unauthenticated PocketBase client."""
    return PocketBase(PB_URL)


def get_admin_pb() -> PocketBase:
    """Returns a PocketBase client authenticated as superuser."""
    global _ADMIN_CLIENT, _ADMIN_TOKEN_EXP
    now = time.time()
    with _ADMIN_LOCK:
        if _ADMIN_CLIENT is not None and _ADMIN_TOKEN_EXP > (now + 60):
            return _ADMIN_CLIENT

        email = os.environ.get("PB_SUPERUSER_EMAIL") or SUPERUSER_EMAIL
        password = os.environ.get("PB_SUPERUSER_PASSWORD") or SUPERUSER_PASSWORD

        if not email or not password:
            raise RuntimeError(
                "PocketBase superuser credentials not configured. "
                "Please set PB_SUPERUSER_EMAIL and PB_SUPERUSER_PASSWORD in environment or .env file."
            )

        pb = PocketBase(PB_URL)
        try:
            pb.collection("_superusers").auth_with_password(email, password)
            _ADMIN_CLIENT = pb
            _ADMIN_TOKEN_EXP = now + 7200
            return _ADMIN_CLIENT
        except Exception as e:
            raise RuntimeError(f"PocketBase superuser auth failed at {PB_URL}: {e}")


def get_user_pb(token: str) -> PocketBase:
    """Returns a PocketBase client configured with a user JWT token."""
    pb = PocketBase(PB_URL)
    pb.auth_store.save(token, None)
    return pb


def init_db():
    """Verifies connection to central PocketBase instance."""
    try:
        admin_pb = get_admin_pb()
        admin_pb.health.check()
    except Exception as e:
        print(f"Warning: PocketBase health check failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    init_db()
    print(f"PocketBase client initialized successfully: {PB_URL}")
