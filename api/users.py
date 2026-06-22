"""
Hermes Web UI — Multi-user management.

Each user is stored in STATE_DIR/users.json as:
    username -> {password_hash, profile, created_at}

Users are opt-in: when the file doesn't exist or is empty, the WebUI
falls back to the legacy single-password auth (HERMES_WEBUI_PASSWORD
env var or settings.json password_hash).

The profile field maps a user to a Hermes profile (~/.hermes/profiles/<name>).
On login, the user's auth session stores the username, and the hermes_profile
cookie is set to the mapped profile so the WebUI switches contexts.
"""
import hmac
import json
import logging
import os
import tempfile
import threading
import time

from api.config import STATE_DIR

logger = logging.getLogger(__name__)

USERS_FILE = STATE_DIR / 'users.json'

_users: dict = {}
_USERS_LOCK = threading.Lock()
_loaded = False


def _load_users() -> dict:
    """Load users from STATE_DIR/users.json (thread-safe)."""
    global _users, _loaded
    try:
        if USERS_FILE.exists():
            data = json.loads(USERS_FILE.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                _users = data
                _loaded = True
                return _users
    except Exception as e:
        logger.debug("Failed to load users file: %s", e)
    _users = {}
    _loaded = True
    return _users


def _save_users(users: dict) -> None:
    """Atomically persist users to STATE_DIR/users.json (0600)."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix='.users.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(users, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, USERS_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.debug("Failed to persist users: %s", e)


def _ensure_loaded():
    """Load users on first access (double-checked locking, thread-safe)."""
    if _loaded:
        return
    with _USERS_LOCK:
        if not _loaded:
            _load_users()


# ── Public API ─────────────────────────────────────────────────────────────


def is_multi_user_enabled() -> bool:
    """Return True if at least one user is defined in users.json."""
    _ensure_loaded()
    with _USERS_LOCK:
        return len(_users) > 0


def verify_user(username: str, password: str) -> bool:
    """Verify a username/password combination against users.json.

    Returns True if the password matches the stored hash for this user.
    Thread-safe: reads under lock.
    """
    _ensure_loaded()
    with _USERS_LOCK:
        user = _users.get(username)
        if not user:
            return False
        expected = user.get('password_hash', '')
        if not expected:
            return False
    from api.auth import _hash_password
    return hmac.compare_digest(_hash_password(password), expected)


def user_exists(username: str) -> bool:
    """Check if a username is registered."""
    _ensure_loaded()
    with _USERS_LOCK:
        return username in _users


def add_user(username: str, password: str, profile: str = None) -> bool:
    """Add a new user.

    Args:
        username: Unique username. Raises ValueError if username is 'admin'
            (reserved for legacy HERMES_WEBUI_PASSWORD auth).
        password: Plaintext password (will be hashed via PBKDF2-SHA256).
        profile: Hermes profile name. Defaults to the username.

    Returns:
        True on success, False if the username already exists.
    """
    _ensure_loaded()
    from api.auth import _hash_password
    with _USERS_LOCK:
        if username == "admin":
            raise ValueError(
                "'admin' is a reserved username; use the HERMES_WEBUI_PASSWORD "
                "env var or settings password to set the admin password"
            )
        if username in _users:
            return False
        _users[username] = {
            'password_hash': _hash_password(password),
            'profile': profile or username,
            'created_at': time.time(),
        }
        _save_users(_users)
        return True


def delete_user(username: str) -> bool:
    """Remove a user. Returns True on success, False if not found."""
    _ensure_loaded()
    with _USERS_LOCK:
        if username not in _users:
            return False
        _users.pop(username, None)
        _save_users(_users)
        return True


def change_password(username: str, new_password: str) -> bool:
    """Change a user's password. Returns True on success, False if not found."""
    _ensure_loaded()
    from api.auth import _hash_password
    with _USERS_LOCK:
        if username not in _users:
            return False
        _users[username]['password_hash'] = _hash_password(new_password)
        _save_users(_users)
        return True


def get_user_profile(username: str) -> str | None:
    """Return the Hermes profile name for a user, or None if not found."""
    _ensure_loaded()
    with _USERS_LOCK:
        user = _users.get(username)
        if user:
            profile = user.get('profile')
            return profile if profile else username
        return None


def set_user_profile(username: str, profile: str) -> bool:
    """Set the Hermes profile for a user. Returns True on success."""
    _ensure_loaded()
    with _USERS_LOCK:
        if username not in _users:
            return False
        _users[username]['profile'] = profile
        _save_users(_users)
        return True


def list_users() -> list[dict]:
    """Return a list of all users (without password hashes).

    Each entry: {username, profile, created_at}
    """
    _ensure_loaded()
    result = []
    with _USERS_LOCK:
        for username, data in _users.items():
            result.append({
                'username': username,
                'profile': data.get('profile', username),
                'created_at': data.get('created_at', 0),
            })
    return result
