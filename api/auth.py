"""
Hermes Web UI -- optional authentication.
Off by default. Enable by setting HERMES_WEBUI_PASSWORD, configuring a
password in Settings, registering passkeys, or configuring native OIDC SSO.
"""
import hashlib
import hmac
import http.cookies
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

from api.config import STATE_DIR, get_config, load_settings

logger = logging.getLogger(__name__)


# Default session TTL — 30 days. Kept as a module-level constant for backwards
# compatibility with downstream code and regression tests that import it.
# At runtime, prefer ``_resolve_session_ttl()`` which honours the env var and
# settings.json overrides; this constant is the floor / fallback.
SESSION_TTL = 86400 * 30  # 30 days


def _resolve_session_ttl() -> int:
    """Resolve session TTL from env > settings > default.

    Priority mirrors get_password_hash(): HERMES_WEBUI_SESSION_TTL env var
    first, then settings.json, falling back to ``SESSION_TTL`` (30 days).
    Clamped to [60s, 1 year] to prevent runaway cookies or self-lockout.
    """
    env_v = os.getenv('HERMES_WEBUI_SESSION_TTL', '').strip()
    if env_v.isdigit():
        val = int(env_v)
        if 60 <= val <= 86400 * 365:
            return val
    s = load_settings()
    v = s.get('session_ttl_seconds')
    if isinstance(v, int) and 60 <= v <= 86400 * 365:
        return v
    return SESSION_TTL


# ── Public paths (no auth required) ─────────────────────────────────────────
PUBLIC_PATHS = frozenset({
    '/login', '/health', '/favicon.ico', '/sw.js',
    '/api/auth/login', '/api/auth/status',
    '/api/auth/oidc/start', '/api/auth/oidc/callback',
    '/api/auth/passkey/options', '/api/auth/passkey/login',
    '/manifest.json', '/manifest.webmanifest',
    '/session/manifest.json', '/session/manifest.webmanifest',
})

COOKIE_NAME = 'hermes_session'
CSRF_HEADER_NAME = 'X-Hermes-CSRF-Token'


# RFC 6265 cookie-name token: a non-empty run of token chars
# (no controls, whitespace, or separators such as ';', '=', ',').
_COOKIE_NAME_RE = re.compile(r"^[-!#$%&'*+.^_`|~0-9A-Za-z]+$")


def _resolve_cookie_name() -> str:
    """Resolve the auth session cookie name from env > default.

    Honours ``HERMES_WEBUI_COOKIE_NAME`` so multiple WebUI instances sharing a
    hostname (different ports) can use distinct cookie names instead of
    trampling each other's session — browsers scope cookies by host, not
    host+port (RFC 6265). Falls back to ``COOKIE_NAME`` when the env var is
    unset, empty, or not a valid RFC 6265 token.
    """
    name = os.getenv('HERMES_WEBUI_COOKIE_NAME', '').strip()
    if not name:
        return COOKIE_NAME
    if _COOKIE_NAME_RE.match(name):
        return name
    logger.warning(
        'Ignoring invalid HERMES_WEBUI_COOKIE_NAME=%r; falling back to %r '
        '(name must be a valid RFC 6265 token)', name, COOKIE_NAME,
    )
    return COOKIE_NAME


def _warn_auth_persistence_failure(prefix: str, artifact: Path, exc: Exception, consequence: str) -> None:
    logger.warning(
        '%s at %s (STATE_DIR=%s): %s: %s; %s',
        prefix,
        artifact,
        STATE_DIR,
        exc.__class__.__name__,
        exc,
        consequence,
    )


_SESSIONS_FILE = STATE_DIR / '.sessions.json'


def _load_sessions() -> dict[str, float]:
    """Load persisted sessions from STATE_DIR, pruning expired entries.

    Returns an empty dict on any read or parse error so startup is never
    blocked by a corrupt or missing sessions file.
    """
    try:
        if not _SESSIONS_FILE.exists():
            return {}
        raw = _SESSIONS_FILE.read_text(encoding='utf-8')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('malformed sessions file: expected dict')
    except OSError as e:
        _warn_auth_persistence_failure(
            'Auth session store read failed',
            _SESSIONS_FILE,
            e,
            'starting fresh with an empty session table',
        )
        return {}
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        _warn_auth_persistence_failure(
            'Ignoring malformed auth session store',
            _SESSIONS_FILE,
            e,
            'starting fresh with an empty session table',
        )
        return {}
    except Exception as e:
        _warn_auth_persistence_failure(
            'Ignoring malformed auth session store',
            _SESSIONS_FILE,
            e,
            'starting fresh with an empty session table',
        )
        return {}
    now = time.time()
    return {t: exp for t, exp in data.items()
            if isinstance(t, str) and isinstance(exp, (int, float)) and exp > now}


def _save_sessions(sessions: dict[str, float]) -> None:
    """Atomically persist sessions to STATE_DIR/.sessions.json (0600).

    Uses a temp file + os.replace() so a crash mid-write never leaves a
    truncated file.  Mirrors the same pattern as .signing_key persistence.
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix='.sessions.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(sessions, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, _SESSIONS_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        _warn_auth_persistence_failure(
            'Auth session persistence failed',
            _SESSIONS_FILE,
            e,
            'keeping the in-process session table available',
        )


# Active sessions: token -> expiry timestamp (persisted across restarts via STATE_DIR)
_sessions = _load_sessions()
_SESSIONS_LOCK = threading.Lock()

# ── Login rate limiter ──────────────────────────────────────────────────────
_LOGIN_ATTEMPTS_FILE = STATE_DIR / '.login_attempts.json'
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW = 60  # seconds


def _load_login_attempts() -> dict[str, list[float]]:
    """Load persisted login attempts from STATE_DIR, pruning expired entries."""
    try:
        if _LOGIN_ATTEMPTS_FILE.exists():
            data = json.loads(_LOGIN_ATTEMPTS_FILE.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('malformed login-attempts file — expected dict')
            now = time.time()
            attempts: dict[str, list[float]] = {}
            for ip, raw_times in data.items():
                if not isinstance(ip, str) or not isinstance(raw_times, list):
                    continue
                fresh = [
                    float(t)
                    for t in raw_times
                    if isinstance(t, (int, float)) and now - float(t) < _LOGIN_WINDOW
                ]
                if fresh:
                    attempts[ip] = fresh
            return attempts
    except Exception as e:
        logger.debug("Failed to load login attempts file, starting fresh: %s", e)
    return {}


def _save_login_attempts(attempts: dict[str, list[float]]) -> None:
    """Atomically persist login attempts to STATE_DIR/.login_attempts.json (0600)."""
    try:
        _LOGIN_ATTEMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_LOGIN_ATTEMPTS_FILE.parent, suffix='.login_attempts.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(attempts, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, _LOGIN_ATTEMPTS_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.debug("Failed to persist login attempts: %s", e)


_login_attempts = _load_login_attempts()  # ip -> [timestamp, ...]
_LOGIN_ATTEMPTS_LOCK = threading.Lock()


def _check_login_rate(ip: str) -> bool:
    """Return True if the IP is allowed to attempt login (thread-safe)."""
    with _LOGIN_ATTEMPTS_LOCK:
        now = time.time()
        attempts = _login_attempts.get(ip, [])
        # Prune old attempts
        attempts = [t for t in attempts if now - t < _LOGIN_WINDOW]
        if attempts:
            _login_attempts[ip] = attempts
        else:
            _login_attempts.pop(ip, None)
        _save_login_attempts(_login_attempts)
        return len(attempts) < _LOGIN_MAX_ATTEMPTS


def _record_login_attempt(ip: str) -> None:
    """Record a login attempt for rate limiting (thread-safe)."""
    with _LOGIN_ATTEMPTS_LOCK:
        now = time.time()
        attempts = _login_attempts.get(ip, [])
        attempts.append(now)
        _login_attempts[ip] = attempts
        _save_login_attempts(_login_attempts)


def _clear_login_attempts(ip: str) -> None:
    """Clear failed login attempts after a successful login (thread-safe)."""
    with _LOGIN_ATTEMPTS_LOCK:
        if ip in _login_attempts:
            _login_attempts.pop(ip, None)
            _save_login_attempts(_login_attempts)


def _load_key(filename: str) -> bytes:
    """Load a 32-byte key from STATE_DIR, generating and persisting one if missing."""
    key_file = STATE_DIR / filename
    try:
        if key_file.exists():
            raw = key_file.read_bytes()
            if len(raw) >= 32:
                return raw[:32]
    except OSError as e:
        _warn_auth_persistence_failure(
            'Auth key read failed',
            key_file,
            e,
            'generating a new key and continuing',
        )
    except Exception as e:
        _warn_auth_persistence_failure(
            'Auth key read failed',
            key_file,
            e,
            'generating a new key and continuing',
        )
    key = secrets.token_bytes(32)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(key)
        key_file.chmod(0o600)
    except OSError as e:
        _warn_auth_persistence_failure(
            'Auth key persistence failed',
            key_file,
            e,
            'returning the generated key so startup can continue',
        )
    except Exception as e:
        _warn_auth_persistence_failure(
            'Auth key persistence failed',
            key_file,
            e,
            'returning the generated key so startup can continue',
        )
    return key


_PBKDF2_KEY_CACHE: bytes | None = None
_SIGNING_KEY_CACHE: bytes | None = None


def _pbkdf2_key() -> bytes:
    global _PBKDF2_KEY_CACHE
    if _PBKDF2_KEY_CACHE is None:
        _PBKDF2_KEY_CACHE = _load_key('.pbkdf2_key')
    return _PBKDF2_KEY_CACHE


def _signing_key() -> bytes:
    global _SIGNING_KEY_CACHE
    if _SIGNING_KEY_CACHE is None:
        _SIGNING_KEY_CACHE = _load_key('.signing_key')
    return _SIGNING_KEY_CACHE


def _hash_password(password, *, salt: bytes | None = None) -> str:
    """PBKDF2-SHA256 with 600k iterations (OWASP recommendation).
    Salt is the persisted PBKDF2 key, which is secret and unique per
    installation. This keeps the stored hash format a plain hex string
    (no format change to settings.json) while replacing the predictable
    STATE_DIR-derived salt from the original implementation.

    The *salt* parameter exists solely to support transparent migration
    of password hashes that were computed with a different key (e.g. the
    old `.signing_key`). Normal callers should never pass it.
    """
    if salt is None:
        salt = _pbkdf2_key()
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600_000)
    return dk.hex()


_AUTH_HASH_LOCK = threading.Lock()
_AUTH_HASH_COMPUTED: bool = False
_AUTH_HASH_CACHE: str | None = None


def _invalidate_password_hash_cache() -> None:
    """Invalidate the in-process password hash cache so the next call to
    get_password_hash() re-reads from settings.json or the env var."""
    global _AUTH_HASH_COMPUTED, _AUTH_HASH_CACHE
    with _AUTH_HASH_LOCK:
        _AUTH_HASH_COMPUTED = False
        _AUTH_HASH_CACHE = None


def get_password_hash() -> str | None:
    """Return the active password hash, or None if auth is disabled.
    Priority: env var > settings.json.

    The hash is computed once and cached for the lifetime of the process.
    PBKDF2-600k takes ~1 s and is called on nearly every HTTP request via
    check_auth → is_auth_enabled, so caching avoids wasting a full second
    of CPU per request after the first one.

    Thread-safe: double-checked locking ensures that under a burst of
    concurrent requests only one thread computes PBKDF2, while the fast
    path (after initialisation) requires zero locks.
    """
    global _AUTH_HASH_COMPUTED, _AUTH_HASH_CACHE

    # Fast path — no lock needed once cache is populated.
    if _AUTH_HASH_COMPUTED:
        return _AUTH_HASH_CACHE

    with _AUTH_HASH_LOCK:
        # Re-check inside lock — another thread may have populated while
        # we were waiting to acquire.
        if _AUTH_HASH_COMPUTED:
            return _AUTH_HASH_CACHE

        env_pw = os.getenv('HERMES_WEBUI_PASSWORD', '').strip()
        if env_pw:
            result = _hash_password(env_pw)
        else:
            result = load_settings().get('password_hash') or None

        _AUTH_HASH_CACHE = result
        _AUTH_HASH_COMPUTED = True
        return result


def is_password_auth_enabled() -> bool:
    """True if a password is configured (env var or settings)."""
    return get_password_hash() is not None


def _passkey_feature_flag_enabled() -> bool:
    """Return True if the passkey/WebAuthn surface is enabled for this deployment.

    Passkey support is opt-in default-off behind a feature flag so deployments
    that don't want the WebAuthn surface (or whose RP-ID setup isn't ready for
    non-localhost hosts) can disable it entirely with no UI surface, no
    endpoints, no credential storage. To enable:

      - Set ``HERMES_WEBUI_PASSKEY=1`` in the environment, OR
      - Set ``webui_passkey_enabled: true`` in the per-profile config.yaml

    With the flag off, ``are_passkeys_enabled()`` always returns False even if
    credentials were registered in the past, and ``/login`` shows password-only.
    """
    env_value = os.getenv("HERMES_WEBUI_PASSKEY", "")
    if env_value:
        return env_value.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from api.config import get_config

        cfg = get_config()
        if isinstance(cfg, dict):
            raw = cfg.get("webui_passkey_enabled")
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                return raw.strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        pass
    return False


def are_passkeys_enabled() -> bool:
    """True if the passkey feature flag is on AND at least one local passkey credential is registered."""
    if not _passkey_feature_flag_enabled():
        return False
    try:
        from api.passkeys import passkeys_available

        return passkeys_available()
    except Exception as exc:
        logger.debug("Failed to inspect passkey availability: %s", exc)
        return False


def is_oidc_auth_enabled() -> bool:
    """True if native OIDC login is configured for WebUI sessions."""
    try:
        from api.auth_oidc import is_oidc_enabled

        return is_oidc_enabled()
    except Exception as exc:
        logger.debug("Failed to inspect OIDC availability: %s", exc)
        return False


def get_oidc_startup_warning() -> str | None:
    """Return a startup warning when OIDC auth is only partially configured."""
    try:
        cfg = get_config()
        raw = cfg.get("webui_oidc") if isinstance(cfg, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        logger.debug("Failed to read webui_oidc config", exc_info=True)
        raw = {}

    def pick(name: str, env_name: str) -> str:
        env_value = os.getenv(env_name)
        value = env_value if env_value is not None else raw.get(name)
        return str(value or "").strip()

    issuer = bool(pick("issuer", "HERMES_WEBUI_OIDC_ISSUER"))
    client_id = bool(pick("client_id", "HERMES_WEBUI_OIDC_CLIENT_ID"))
    allow_claim = bool(pick("allow_claim", "HERMES_WEBUI_OIDC_ALLOW_CLAIM"))
    allow_values = bool(pick("allow_values", "HERMES_WEBUI_OIDC_ALLOW_VALUES"))

    if not any((issuer, client_id, allow_claim, allow_values)):
        return None
    if issuer and client_id and allow_claim and allow_values:
        return None

    missing = []
    if not issuer:
        missing.append("issuer")
    if not client_id:
        missing.append("client_id")
    if not allow_claim:
        missing.append("allow_claim")
    if not allow_values:
        missing.append("allow_values")

    joined = ", ".join(missing)
    return (
        "Native OIDC login is only partially configured; missing "
        f"{joined}. The WebUI will not enable OIDC auth until all four fields are set."
    )


def is_auth_enabled() -> bool:
    """True if password auth, passkeys, or OIDC login is configured."""
    return (
        is_password_auth_enabled()
        or are_passkeys_enabled()
        or is_oidc_auth_enabled()
    )


def verify_password(plain: str) -> bool:
    """Verify a plaintext password against the stored hash.

    Supports transparent migration of password hashes that were computed
    with the old `.signing_key` salt.  When the two keys differ and the
    legacy-salted hash matches, the password is transparently re-hashed
    with the current `.pbkdf2_key` and persisted to settings.json.
    """
    expected = get_password_hash()
    if not expected:
        return False
    # Fast path: current PBKDF2 key
    if hmac.compare_digest(_hash_password(plain), expected):
        return True
    # Migration: some hashes were computed with `.signing_key` before the
    # PBKDF2 key was separated.  Try the legacy salt; if it matches,
    # transparently upgrade so the next login uses the fast path.
    legacy_salt = _signing_key()
    current_salt = _pbkdf2_key()
    if legacy_salt != current_salt:
        if hmac.compare_digest(_hash_password(plain, salt=legacy_salt), expected):
            from api.config import save_settings

            save_settings({'_set_password': plain})
            # Password re-hashed and persisted to disk using the current salt.
            # Cache invalidation is handled by fix 2/3 (#2192) which adds the
            # _invalidate_password_hash_cache() call inside save_settings().
            return True
    return False


def create_session() -> str:
    """Create a new auth session. Returns signed cookie value."""
    token = secrets.token_hex(32)
    with _SESSIONS_LOCK:
        _sessions[token] = time.time() + _resolve_session_ttl()
        _save_sessions(_sessions)
    sig = hmac.new(_signing_key(), token.encode(), hashlib.sha256).hexdigest()
    return f"{token}.{sig}"


def _prune_expired_sessions():
    """Remove all expired session entries to prevent unbounded memory growth."""
    now = time.time()
    with _SESSIONS_LOCK:
        expired = [t for t, exp in _sessions.items() if now > exp]
        if expired:
            for token in expired:
                _sessions.pop(token, None)
            _save_sessions(_sessions)


def verify_session(cookie_value: str) -> bool:
    """Verify a signed session cookie. Returns True if valid and not expired."""
    if not cookie_value or '.' not in cookie_value:
        return False
    _prune_expired_sessions()  # lazy cleanup on every verification attempt
    token, sig = cookie_value.rsplit('.', 1)
    full_sig = hmac.new(_signing_key(), token.encode(), hashlib.sha256).hexdigest()
    # Accept both new (64-char) and legacy (32-char truncated) signatures so
    # existing sessions survive the upgrade without a forced global logout.
    # The legacy branch can be removed once session TTLs have expired (~30 days).
    valid = hmac.compare_digest(sig, full_sig) or (
        len(sig) == 32 and hmac.compare_digest(sig, full_sig[:32])
    )
    if not valid:
        return False
    with _SESSIONS_LOCK:
        expiry = _sessions.get(token)
        if not expiry or time.time() > expiry:
            _sessions.pop(token, None)
            _save_sessions(_sessions)
            return False
    return True


def _session_token_from_cookie_value(cookie_value: str) -> str | None:
    """Return the raw server-side session token from a signed cookie value."""
    if not cookie_value or '.' not in cookie_value:
        return None
    token, _sig = cookie_value.rsplit('.', 1)
    return token or None


def sign_profile_cookie_value(profile_name: str, session_cookie_value: str | None) -> str:
    """Return a profile cookie value authenticated for one WebUI session.

    The active-profile cookie is client-controlled, so when auth is enabled it
    must not be trusted as a bare profile name. Binding the selected profile to
    the HttpOnly session token prevents a client from forging
    ``hermes_profile=<other-profile>`` and bypassing profile visibility guards.
    """
    if not session_cookie_value or not verify_session(session_cookie_value):
        raise ValueError("active auth session is required to sign profile cookie")
    token = _session_token_from_cookie_value(session_cookie_value)
    if not token:
        raise ValueError("active auth session is required to sign profile cookie")
    sig = hmac.new(
        _signing_key(),
        f"profile:{token}:{profile_name}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{profile_name}.{sig}"


def verify_profile_cookie_value(cookie_value: str, session_cookie_value: str | None) -> str | None:
    """Verify a session-bound profile cookie and return its profile name."""
    if not cookie_value or '.' not in cookie_value:
        return None
    if not session_cookie_value or not verify_session(session_cookie_value):
        return None
    profile_name, sig = cookie_value.rsplit('.', 1)
    token = _session_token_from_cookie_value(session_cookie_value)
    if not profile_name or not token or not sig:
        return None
    # Defense-in-depth: validate the profile-name pattern here too, not only in
    # get_profile_cookie(), so any future caller of this verifier can't return an
    # unvalidated name. (#4023 Opus hardening.)
    from api.profiles import _PROFILE_ID_RE
    if profile_name != 'default' and not _PROFILE_ID_RE.fullmatch(profile_name):
        return None
    expected = hmac.new(
        _signing_key(),
        f"profile:{token}:{profile_name}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if hmac.compare_digest(str(sig), expected):
        return profile_name
    return None


def csrf_token_for_session(cookie_value: str) -> str | None:
    """Return the CSRF token bound to an authenticated WebUI session.

    The browser can read this token from the authenticated shell and echoes it
    in ``X-Hermes-CSRF-Token`` on unsafe API requests. The token is derived
    from the HttpOnly session cookie's server-side token, so it automatically
    rotates on login and is invalidated when the auth session expires or logs
    out. Callers must still verify the auth session before trusting it.
    """
    token = _session_token_from_cookie_value(cookie_value)
    if not token:
        return None
    return hmac.new(_signing_key(), f"csrf:{token}".encode(), hashlib.sha256).hexdigest()


def verify_csrf_token(cookie_value: str, csrf_token: str) -> bool:
    """Verify a submitted CSRF token against the authenticated session."""
    if not cookie_value or not csrf_token or not verify_session(cookie_value):
        return False
    expected = csrf_token_for_session(cookie_value)
    return bool(expected and hmac.compare_digest(str(csrf_token), expected))


def invalidate_session(cookie_value) -> None:
    """Remove a session token."""
    if cookie_value and '.' in cookie_value:
        token = cookie_value.rsplit('.', 1)[0]
        with _SESSIONS_LOCK:
            if token in _sessions:
                _sessions.pop(token, None)
                _save_sessions(_sessions)


def parse_cookie(handler) -> str | None:
    """Extract the auth cookie from the request headers."""
    cookie_header = handler.headers.get('Cookie', '')
    if not cookie_header:
        return None
    cookie = http.cookies.SimpleCookie()
    try:
        cookie.load(cookie_header)
    except http.cookies.CookieError:
        return None
    morsel = cookie.get(_resolve_cookie_name())
    return morsel.value if morsel else None


def _safe_login_inner_next(query: str | None) -> str:
    """#5578: extract a SAFE, non-login inner redirect from a login page's query.

    When an expired-auth bounce lands back on the login page (which already
    carries its own `next` in the query), we want to preserve a legitimate inner
    destination X across the redirect to the real login route — but only if X is
    itself safe (path-absolute, not protocol-relative/backslash, no control
    chars) AND not login-shaped / not itself carrying a nested next param.
    Anything else collapses to '' (no inner redirect), which kills the
    self-referential chain. Mirrors _safe_login_redirect_path().
    """
    import urllib.parse as _u
    raw = _u.parse_qs(query or "").get("next", [""])[0]
    path = str(raw or "").strip()
    if not path or path[0] != "/" or path[1:2] in {"/", "\\"}:
        return ""
    if re.search(r"[\x00-\x1f\x7f\s]", path) or len(path) > 2048:
        return ""
    # Collapse only login-route chains — decode a few levels so a nested
    # `/session/login%3Fnext%3D...` (encoded `?`) is still recognized by its
    # leading PATH — but preserve a legitimate non-login inner path that merely
    # carries its own `next=` query key (e.g. `/admin?next=/real/path`).
    _probe = path
    for _ in range(8):
        _p = _probe.split("?", 1)[0].split("#", 1)[0].split("&", 1)[0].rstrip("/")
        if _p == "/login" or _p.endswith("/login"):
            return ""
        _decoded = _u.unquote(_probe)
        if _decoded == _probe:
            break
        _probe = _decoded
    else:
        # Still decoding at the cap (pathologically deep encoding) → fail closed.
        _p = _probe.split("?", 1)[0].split("#", 1)[0].split("&", 1)[0].rstrip("/")
        if _p == "/login" or _p.endswith("/login"):
            return ""
        return ""
    return path


def check_auth(handler, parsed) -> bool:
    """Check if request is authorized. Returns True if OK.
    If not authorized, sends 401 (API) or 302 redirect (page) and returns False."""
    if not is_auth_enabled():
        return True
    # Public paths don't require auth
    if parsed.path in PUBLIC_PATHS or parsed.path.startswith('/static/') or parsed.path.startswith('/session/static/'):
        return True
    # Check session cookie
    cookie_val = parse_cookie(handler)
    if cookie_val and verify_session(cookie_val):
        return True
    # Not authorized
    if parsed.path.startswith('/api/'):
        body = b'{"error":"Authentication required"}'
        handler.send_response(401)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    else:
        handler.send_response(302)
        # Pass the original path as ?next= so login.js redirects back after auth.
        # SECURITY/CORRECTNESS: the inner `?` and `&` MUST be percent-encoded
        # when stuffed into the outer `?next=` parameter, otherwise:
        #   (a) multi-param query strings get truncated at the first inner `&`
        #       (e.g. `/api/sessions?limit=50&offset=0` would round-trip as
        #       just `/api/sessions?limit=50` after the browser parses the
        #       outer URL — `offset=0` becomes a separate top-level query
        #       parameter that the login page ignores).
        #   (b) attacker-controlled paths could inject a second `next=`
        #       parameter; per RFC 3986 the duplicate behaviour is undefined
        #       and parsers diverge (Python's parse_qs returns last-match,
        #       URLSearchParams returns first-match), opening a query-pollution
        #       footgun even though _safeNextPath() rejects most malicious
        #       shapes downstream.
        # Encoding the entire `path?query` blob with quote(safe='/') turns
        # `?` → `%3F` and `&` → `%26`, so the outer parameter holds exactly
        # one path-with-query string and `searchParams.get('next')` returns
        # the full original URL (the browser auto-decodes once).
        # (Opus pre-release advisor finding for v0.50.258.)
        import urllib.parse as _urlparse
        # #5578: if the page being redirected is ALREADY login-shaped, do NOT
        # wrap its full `path?query` into a fresh `next=` — that query already
        # carries a `next=`, so quoting the whole thing nests the login URL into
        # itself and re-encodes it on every expired-auth bounce, exploding the
        # URL until the tab breaks. This guard runs in check_auth() (BEFORE
        # route handling), the actual source of the server-side loop.
        #
        # The login page is served ONLY at the public `/login` route (see
        # PUBLIC_PATHS + the routes.py `/login` handler); the app's client route
        # `/session/login` is NOT public, so a bare relative `login` from
        # `/session/login` resolves to `/session/login` again and re-triggers
        # check_auth() — an infinite redirect. Resolve to the real login route
        # with `../login`, which lands on `/login` from a `/session/*` scope and
        # on `<mount>/login` under a subpath mount (verified via urljoin). Carry
        # through only a validated, non-login inner `next` so a legitimate
        # post-login destination still survives a bounce that happened to land
        # on the login page.
        _login_path = (parsed.path or '/').rstrip('/')
        if _login_path == '/login' or _login_path.endswith('/login'):
            # /login itself is public → check_auth never redirects it; this only
            # fires for the non-public client login route (e.g. /session/login).
            _target = '../login' if '/' in _login_path.lstrip('/') else 'login'
            _inner = _safe_login_inner_next(parsed.query)
            if _inner:
                _target += '?next=' + _urlparse.quote(_inner, safe='/')
            handler.send_header('Location', _target)
            handler.send_header('Content-Length', '0')
            handler.end_headers()
            return False
        _path_with_query = parsed.path or '/'
        if parsed.query:
            _path_with_query += '?' + parsed.query
        # safe='/' keeps path separators readable; everything else (including
        # `?`, `&`, `=`) gets percent-encoded.
        _next = _urlparse.quote(_path_with_query, safe='/')
        handler.send_header('Location', 'login?next=' + _next)
        handler.send_header('Content-Length', '0')
        handler.end_headers()
    return False


def _is_loopback(addr: str) -> bool:
    """Return True if *addr* is a loopback address (127.x.x.x, ::1, or ::ffff:127.x.x.x)."""
    import ipaddress as _ipaddress
    try:
        ip = _ipaddress.ip_address(addr)
        if ip.is_loopback:
            return True
        # Python < 3.12: is_loopback is False for ::ffff:127.x.x.x (gh-117566)
        if hasattr(ip, 'ipv4_mapped') and ip.ipv4_mapped is not None:
            return ip.ipv4_mapped.is_loopback
        return False
    except ValueError:
        return False


def _is_secure_context(handler=None) -> bool:
    """Return True if cookies should carry the Secure flag.

    Priority order:
    1. ``HERMES_WEBUI_SECURE`` env var: 1/true/yes -> True; 0/false/no -> False.
    2. Direct TLS socket (handler.request.getpeercert present) -> True.
    3. ``HERMES_WEBUI_TRUST_FORWARDED_PROTO=1`` opt-in: trust
       ``X-Forwarded-Proto: https`` header from a known reverse proxy.
    4. Otherwise -> False (loopback or non-loopback, plain HTTP is not secure).

    .. warning::
       ``X-Forwarded-Proto`` is only trustworthy behind a reverse proxy.
       It is ignored unless ``HERMES_WEBUI_TRUST_FORWARDED_PROTO=1`` is
       set explicitly, preventing header-injection attacks on plain-HTTP
       deployments.
    """
    env = os.getenv('HERMES_WEBUI_SECURE', '').strip().lower()
    if env in ('1', 'true', 'yes'):
        return True
    if env in ('0', 'false', 'no'):
        return False
    if handler is not None:
        if getattr(handler.request, 'getpeercert', None) is not None:
            return True
        trust_fwd = os.getenv('HERMES_WEBUI_TRUST_FORWARDED_PROTO', '').strip().lower()
        if trust_fwd in ('1', 'true', 'yes'):
            if handler.headers.get('X-Forwarded-Proto', '') == 'https':
                return True
    return False


def set_auth_cookie(handler, cookie_value) -> None:
    """Set the auth cookie on the response."""
    cookie = http.cookies.SimpleCookie()
    name = _resolve_cookie_name()
    cookie[name] = cookie_value
    cookie[name]['httponly'] = True
    cookie[name]['samesite'] = 'Lax'
    cookie[name]['path'] = '/'
    cookie[name]['max-age'] = str(_resolve_session_ttl())
    if _is_secure_context(handler):
        cookie[name]['secure'] = True
    handler.send_header('Set-Cookie', cookie[name].OutputString())


def clear_auth_cookie(handler) -> None:
    """Clear the auth cookie on the response."""
    cookie = http.cookies.SimpleCookie()
    name = _resolve_cookie_name()
    cookie[name] = ''
    cookie[name]['httponly'] = True
    cookie[name]['path'] = '/'
    cookie[name]['max-age'] = '0'
    handler.send_header('Set-Cookie', cookie[name].OutputString())
