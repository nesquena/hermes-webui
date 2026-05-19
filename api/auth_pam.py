"""PAM-backed login provider for Hermes WebUI.

This module is intentionally separate from api.auth so the optional PAM path
does not make the default shared-password auth flow harder to reason about.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
import subprocess

try:  # pragma: no cover - only absent on non-Unix platforms
    import pwd
except ImportError:  # pragma: no cover
    pwd = None

logger = logging.getLogger(__name__)

_TRUTHY = {'1', 'true', 'yes', 'on'}
_PROFILE_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
_PROFILE_CLEAN_RE = re.compile(r'[^a-z0-9_-]+')
_UNSAFE_ACCOUNT_CHARS = {'\x00', '\r', '\n', ':', '/'}


def enabled() -> bool:
    """Return True when WebUI should use PAM instead of a stored password hash."""
    return os.getenv('HERMES_WEBUI_AUTH_MODE', '').strip().lower() == 'pam'


def fixed_user() -> str:
    """Return the single PAM account used when multi-user login is disabled."""
    return (
        os.getenv('HERMES_WEBUI_PAM_USER', '').strip()
        or os.getenv('USER', '').strip()
        or ''
    )


def allow_any_user() -> bool:
    """Return True when the login form should accept a local system username."""
    return os.getenv('HERMES_WEBUI_PAM_ALLOW_ANY_USER', '').strip().lower() in _TRUTHY


def login_uses_username() -> bool:
    """Return True when the login UI should ask for a username."""
    return enabled() and allow_any_user()


def service() -> str:
    """Return the PAM service name to use for WebUI login checks."""
    return os.getenv('HERMES_WEBUI_PAM_SERVICE', '').strip() or 'login'


def min_uid() -> int:
    """Minimum UID accepted for multi-user login, defaulting to human users."""
    raw = os.getenv('HERMES_WEBUI_PAM_MIN_UID', '1000').strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 1000


def helper_command() -> list[str]:
    """Return an optional admin-configured helper command.

    The helper receives the password on stdin and is invoked with
    ``--user <name> --service <pam-service>``. No helper is used by default:
    deployments that need elevated PAM checks should configure a tightly
    scoped wrapper or sudoers rule explicitly.
    """
    raw = os.getenv('HERMES_WEBUI_PAM_HELPER', '').strip()
    return shlex.split(raw) if raw else []


def _safe_account_name(name: str) -> bool:
    return bool(name) and not any(ch in name for ch in _UNSAFE_ACCOUNT_CHARS)


def _account_for_name(name: str):
    if pwd is None:
        return None
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def _is_login_account(account, *, fixed_name: str) -> bool:
    if account is None:
        return False
    shell = (getattr(account, 'pw_shell', '') or '').lower()
    if shell.endswith('/nologin') or shell.endswith('/false'):
        return False
    uid = int(getattr(account, 'pw_uid', -1))
    if uid < min_uid() and getattr(account, 'pw_name', '') != fixed_name:
        return False
    return True


def resolve_login_user(username: str | None, *, allow_default: bool = False) -> str | None:
    """Resolve a submitted username to an allowed local login account."""
    requested = (username or '').strip()
    configured_user = fixed_user()
    if not requested and allow_default:
        requested = configured_user
    if not _safe_account_name(requested):
        return None

    account = _account_for_name(requested)
    if account is None:
        return None

    account_name = getattr(account, 'pw_name', requested)
    if not allow_any_user():
        return account_name if account_name == configured_user else None
    if not _is_login_account(account, fixed_name=configured_user):
        return None
    return account_name


def profile_name_for_user(username: str) -> str:
    """Map a system username to a deterministic Hermes profile identifier."""
    raw = (username or '').strip()
    lowered = raw.lower()
    base = _PROFILE_CLEAN_RE.sub('-', lowered).strip('-_') or 'user'
    needs_suffix = base != lowered or len(base) > 64 or base == 'default'
    if needs_suffix:
        suffix = '-' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:8]
        base = base[:64 - len(suffix)].rstrip('-_') or 'user'
        base = base + suffix
    name = base[:64].rstrip('-_') or 'user'
    if not _PROFILE_NAME_RE.fullmatch(name) or name == 'default':
        suffix = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]
        name = f'user-{suffix}'
    return name


def _authenticate_with_python_pam(username: str, password: str) -> bool:
    try:
        import pam
    except Exception as exc:
        logger.error("PAM auth is enabled but python-pam is unavailable: %s", exc)
        return False

    try:
        return bool(pam.pam().authenticate(username, password, service=service()))
    except Exception as exc:
        logger.warning("PAM authentication failed unexpectedly: %s", exc)
        return False


def _authenticate_with_helper(username: str, password: str) -> bool:
    cmd = helper_command()
    if not cmd:
        return False
    try:
        result = subprocess.run(
            cmd + ['--user', username, '--service', service()],
            input=password,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        logger.warning("PAM helper failed unexpectedly: %s", exc)
        return False
    return result.returncode == 0


def authenticate(username: str | None, password: str) -> dict | None:
    """Return login identity metadata when a PAM login succeeds."""
    if not password:
        return None
    account = resolve_login_user(username, allow_default=not allow_any_user())
    if not account:
        return None
    if not (
        _authenticate_with_python_pam(account, password)
        or _authenticate_with_helper(account, password)
    ):
        return None
    return {
        'user': account,
        'profile': profile_name_for_user(account),
    }
