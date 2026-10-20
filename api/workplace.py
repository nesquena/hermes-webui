"""Operator-controlled workplace presentation and temporary administration.

This is a WebUI administration boundary, not an agent/OS sandbox. Configuration
comes from the server environment, never from user-editable settings.json.
"""
import hashlib
import hmac
import os
import re
import secrets
import threading
import time


ADMIN_TTL = 600
_LOCK = threading.Lock()
_sessions: dict[str, tuple[float, str, bytes]] = {}
_attempts: dict[str, list[float]] = {}
_pin_salt = secrets.token_bytes(32)
_pin_cache: tuple[bytes, bytes] | None = None

HIDDEN_TABS = [
    "tasks", "kanban", "skills", "memory", "workspaces", "profiles",
    "todos", "insights", "logs",
]
POLICY = {
    "hidden_tabs": HIDDEN_TABS,
    "show_cli_sessions": False,
    "show_cron_sessions": False,
    "show_webhook_sessions": False,
    "show_kanban_sessions": False,
    "show_previous_messaging_sessions": False,
    "show_titlebar_profile": False,
    "hide_empty_state_suggestions": True,
    "hide_composer_model": True,
    "hide_composer_profile": True,
    "hide_composer_workspace": True,
    "hide_composer_toolsets": True,
    "hide_composer_reasoning": True,
    "hide_composer_yolo": True,
    "hide_composer_mobile_config": True,
    "hide_composer_mic": True,
    "hide_composer_voice_mode": True,
    "hide_composer_saved_prompts": True,
    "voice_mode_button": False,
    "tts_enabled": False,
    "tts_auto_read": False,
    "check_for_updates": False,
    "dashboard_plugins": {},
}

# Exact operational routes; new mutation routes require administration by default.
# In particular do not allow whole /session/, /auth/, /commands/ or /file/ prefixes.
_EMPLOYEE_POST = frozenset({
    "/api/auth/login", "/api/auth/logout", "/api/auth/passkey/options",
    "/api/auth/passkey/login", "/api/csp-report", "/api/client-events/log",
    "/api/session/new", "/api/session/rename", "/api/session/title/regenerate",
    "/api/session/draft", "/api/session/pin", "/api/session/archive",
    "/api/session/delete", "/api/session/clear", "/api/session/truncate",
    "/api/session/branch", "/api/session/duplicate", "/api/session/retry",
    "/api/session/undo", "/api/session/anchor-scene", "/api/session/conversation-rounds",
    "/api/session/compression-recovery/start", "/api/session/compress/start",
    "/api/session/compress", "/api/chat/start", "/api/chat", "/api/chat/steer",
    "/api/upload", "/api/upload/extract", "/api/approval/respond",
    "/api/clarify/respond", "/api/bg-task-complete-ack",
})
_EMPLOYEE_GET = frozenset({
    "/api/auth/status", "/api/settings", "/api/models", "/api/models/live",
    "/api/auth/oidc/start", "/api/auth/oidc/callback",
    "/api/model/auxiliary", "/api/reasoning", "/api/profile/active", "/api/profiles",
    "/api/workspaces", "/api/projects", "/api/prompts", "/api/commands",
    "/api/commands/bundles", "/api/personalities", "/api/provider/quota",
    "/api/dashboard/status", "/api/health/agent", "/api/transcribe/capability",
    "/api/session", "/api/session/status", "/api/session/yolo", "/api/session/usage",
    "/api/session/export", "/api/session/stream", "/api/sessions",
    "/api/sessions/search", "/api/sessions/gateway/stream", "/api/sessions/events",
    "/api/chat/stream/status", "/api/chat/cancel", "/api/chat/stream",
    "/api/media", "/api/file/raw", "/api/file", "/api/folder/download",
    "/api/approval/pending", "/api/approval/stream", "/api/clarify/pending",
    "/api/clarify/stream", "/api/background/status",
})


def enabled() -> bool:
    return os.getenv("HERMES_WEBUI_MANAGED_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def apply_policy(settings: dict) -> dict:
    if enabled():
        # Copy mutable values: consumers must never change the operator's policy.
        settings.update({k: v.copy() if isinstance(v, (list, dict)) else v for k, v in POLICY.items()})
    return settings


def _pin() -> str:
    return os.getenv("HERMES_WEBUI_ADMIN_PIN", "").strip()


def _pin_ready() -> bool:
    # Permit a passphrase too. No shipped PIN, and no four-digit workplace default.
    pin = _pin()
    try:
        pin.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return 6 <= len(pin) <= 128


def _pin_fingerprint() -> bytes:
    return hmac.digest(_pin_salt, _pin().encode(), "sha256")


def _binding(handler) -> str:
    from api.auth import is_auth_enabled, parse_cookie, verify_session

    if not is_auth_enabled():
        return "local-no-auth"
    cookie = parse_cookie(handler) or getattr(handler, "_trusted_auth_session_cookie_value", None)
    return hashlib.sha256(cookie.encode()).hexdigest() if cookie and verify_session(cookie) else ""


def _cookie_name() -> str:
    from api.auth import _resolve_cookie_name

    return _resolve_cookie_name() + "_admin"


def _prune(now: float) -> None:
    for token, (deadline, _, fingerprint) in list(_sessions.items()):
        if deadline <= now or not hmac.compare_digest(fingerprint, _pin_fingerprint()):
            _sessions.pop(token, None)
    for ip, attempts in list(_attempts.items()):
        fresh = [stamp for stamp in attempts if stamp > now - 60]
        if fresh:
            _attempts[ip] = fresh
        else:
            _attempts.pop(ip, None)


def admin_expires(handler) -> float:
    from api.auth import read_cookie

    if not enabled() or not _pin_ready():
        return 0
    token = read_cookie(handler, _cookie_name()) or ""
    binding = _binding(handler)
    with _LOCK:
        _prune(time.time())
        session = _sessions.get(token)
        return session[0] if session and binding and hmac.compare_digest(session[1], binding) else 0


def status(handler) -> dict:
    return {
        "enabled": enabled(),
        "admin_unlocked": bool(deadline := admin_expires(handler)),
        "expires_at": deadline,
        "pin_configured": _pin_ready(),
    }


def _cookie(handler, token: str, max_age: int) -> str:
    from api.auth import _is_secure_context

    cookie = f"{_cookie_name()}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"
    return cookie + ("; Secure" if _is_secure_context(handler) else "")


def _reply(handler, payload, code=200, headers=None) -> bool:
    from api.helpers import j

    j(handler, payload, status=code, extra_headers=headers)
    return True


def _unlock(handler, body: dict) -> bool:
    global _pin_cache
    if not _pin_ready():
        return _reply(handler, {"error": "Administrator PIN is not configured on this server."}, 503)
    binding = _binding(handler)
    if not binding:
        return _reply(handler, {"error": "Sign in before unlocking administration."}, 403)
    candidate = body.get("pin")
    if not isinstance(candidate, str) or not 1 <= len(candidate) <= 128:
        candidate = ""
    try:
        candidate_bytes = candidate.encode("utf-8")
    except UnicodeEncodeError:
        candidate_bytes = b""
    # Use the socket peer, never an untrusted forwarded header. The global cap
    # also bounds memory and guessing distributed across many source addresses.
    ip = str(handler.client_address[0])
    with _LOCK:
        now = time.time()
        _prune(now)
        if len(_attempts.get(ip, [])) >= 5 or sum(map(len, _attempts.values())) >= 50:
            return _reply(handler, {"error": "Too many PIN attempts. Try again in one minute."}, 429,
                          {"Retry-After": "60"})
        _attempts.setdefault(ip, []).append(now)
        fingerprint = _pin_fingerprint()
        if _pin_cache is None or not hmac.compare_digest(_pin_cache[0], fingerprint):
            expected = hashlib.pbkdf2_hmac("sha256", _pin().encode(), _pin_salt, 600_000)
            _pin_cache = (fingerprint, expected)
        actual = hashlib.pbkdf2_hmac("sha256", candidate_bytes, _pin_salt, 600_000)
        if not hmac.compare_digest(actual, _pin_cache[1]):
            return _reply(handler, {"error": "Incorrect administrator PIN."}, 403)
        from api.auth import read_cookie

        _sessions.pop(read_cookie(handler, _cookie_name()) or "", None)
        if len(_sessions) >= 128:
            return _reply(handler, {"error": "Administrator session limit reached."}, 429)
        token = secrets.token_urlsafe(32)
        _sessions[token] = (now + ADMIN_TTL, binding, fingerprint)
    return _reply(handler, {"enabled": True, "admin_unlocked": True,
                            "expires_at": now + ADMIN_TTL, "pin_configured": True},
                  headers={"Set-Cookie": _cookie(handler, token, ADMIN_TTL)})


def handle_request(handler, parsed) -> bool:
    """Run after normal auth, before every GET/write router and sidecar proxy."""
    if not enabled():
        return False
    path = parsed.path.casefold()
    method = handler.command
    if path == "/api/workplace/status" and method == "GET":
        return _reply(handler, status(handler))
    if path in {"/api/workplace/unlock", "/api/workplace/lock"} and method == "POST":
        from api.routes import _check_csrf
        from api.helpers import read_body

        if not _check_csrf(handler):
            return _reply(handler, {"error": "Cross-origin or tokenless request rejected."}, 403)
        try:
            body = read_body(handler)
        except ValueError:
            from api.helpers import arm_connection_close_if_body_pending
            arm_connection_close_if_body_pending(handler)
            return _reply(handler, {"error": "Invalid request body."}, 400)
        if not isinstance(body, dict):
            return _reply(handler, {"error": "Expected a JSON object."}, 400)
        if path.endswith("/unlock"):
            return _unlock(handler, body)
        from api.auth import read_cookie

        with _LOCK:
            _sessions.pop(read_cookie(handler, _cookie_name()) or "", None)
        return _reply(handler, {"enabled": True, "admin_unlocked": False, "expires_at": 0,
                                "pin_configured": _pin_ready()},
                      headers={"Set-Cookie": _cookie(handler, "", 0)})
    if admin_expires(handler):
        return False
    if method == "GET" and path == "/api/dashboard/config":
        return _reply(handler, {"enabled": "off", "url": ""})
    if method == "GET" and path == "/api/settings":
        from api.config import _SETTINGS_DEFAULTS, load_settings
        from api.updates import WEBUI_VERSION

        settings = load_settings()
        # Only UI preferences, not auth hashes, unknown saved keys or provider config.
        public = {k: settings[k] for k in _SETTINGS_DEFAULTS
                  if k in settings and k not in {"password_hash", "auth_disabled_acknowledged"}}
        public.update({k: settings.get(k) for k in ("language", "default_model", "default_model_provider")})
        public.update(onboarding_completed=True, webui_version=WEBUI_VERSION)
        return _reply(handler, public)
    session_events = re.fullmatch(r"/api/sessions/[^/]+/events", path)
    allowed = (method == "GET" and (path in _EMPLOYEE_GET or session_events)) or (method == "POST" and path in _EMPLOYEE_POST)
    if path.startswith("/api/") and not allowed:
        from api.helpers import arm_connection_close_if_body_pending

        arm_connection_close_if_body_pending(handler)
        return _reply(handler, {"error": "Administrator unlock required.", "code": "admin_locked"}, 403)
    # Public shares are deliberately unavailable in workplace mode, even if an
    # older deployment had already minted share links.
    if path == "/share" or path.startswith("/share/"):
        return _reply(handler, {"error": "Public sharing is disabled in workplace mode."}, 403)
    return False
