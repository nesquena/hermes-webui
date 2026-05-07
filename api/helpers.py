"""
Hermes Web UI -- HTTP helper functions.
"""
import json as _json
import os
import re as _re
from pathlib import Path
from api.config import IMAGE_EXTS, MD_EXTS


def require(body: dict, *fields) -> None:
    """Phase D: Validate required fields. Raises ValueError with clean message."""
    missing = [f for f in fields if not body.get(f) and body.get(f) != 0]
    if missing:
        raise ValueError(f"Missing required field(s): {', '.join(missing)}")


def bad(handler, msg, status: int=400):
    """Return a clean JSON error response."""
    return j(handler, {'error': msg}, status=status)


def _sanitize_error(e: Exception) -> str:
    """Strip filesystem paths from exception messages before returning to client."""
    import re
    msg = str(e)
    # Remove absolute paths (Unix and Windows)
    msg = re.sub(r'(?:(?:/[a-zA-Z0-9_.-]+)+|(?:[A-Z]:\\[^\s]+))', '<path>', msg)
    return msg


def safe_resolve(root: Path, requested: str) -> Path:
    """Resolve a relative path inside root, raising ValueError on traversal."""
    resolved = (root / requested).resolve()
    resolved.relative_to(root.resolve())  # raises ValueError if outside root
    return resolved


def _security_headers(handler):
    """Add security headers to every response."""
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('X-Frame-Options', 'DENY')
    handler.send_header('Referrer-Policy', 'same-origin')
    handler.send_header(
        'Content-Security-Policy',
        "default-src 'self' https://*.cloudflareaccess.com; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://static.cloudflareinsights.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "img-src 'self' data: https: blob:; font-src 'self' data: https://cdn.jsdelivr.net https://fonts.gstatic.com; connect-src 'self'; "
        "manifest-src 'self' https://*.cloudflareaccess.com; "
        "base-uri 'self'; form-action 'self'"
    )
    handler.send_header(
        'Permissions-Policy',
        'camera=(), microphone=(self), geolocation=(), clipboard-write=(self)'
    )


def _accepts_gzip(handler) -> bool:
    """Check if the client accepts gzip encoding."""
    headers = getattr(handler, 'headers', None)
    if not headers:
        return False
    ae = headers.get('Accept-Encoding', '')
    return 'gzip' in ae


def j(handler, payload, status: int=200, extra_headers: dict=None) -> None:
    """Send a JSON response.

    *extra_headers*: optional dict of additional headers to include
    (e.g., {'Set-Cookie': '...'}).  Headers are sent before end_headers().
    """
    body = _json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')

    # Gzip-compress responses over 1KB when the client accepts it.
    # Typical JSON API responses compress 70-80%, giving a big speedup
    # for large payloads (session history, message lists).
    if _accepts_gzip(handler) and len(body) > 1024:
        import gzip
        body = gzip.compress(body, compresslevel=4)
        handler.send_header('Content-Encoding', 'gzip')

    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-store')
    _security_headers(handler)
    if extra_headers:
        for k, v in extra_headers.items():
            handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def t(handler, payload, status: int=200, content_type: str='text/plain; charset=utf-8') -> None:
    """Send a plain text or HTML response."""
    body = payload if isinstance(payload, bytes) else str(payload).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-store')
    _security_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


MAX_BODY_BYTES = 20 * 1024 * 1024  # 20MB limit for non-upload POST bodies


# ── Credential redaction ──────────────────────────────────────────────────────

def _build_redact_fn():
    """Return a redactor backed by hermes-agent plus local fallback patterns."""
    # Minimal fallback covering the most common credential prefixes.
    # Keep this active even when hermes-agent is importable so API responses do
    # not regress if the agent redactor misses a token shape.
    _CRED_RE = _re.compile(
        r"(?<![A-Za-z0-9_-])("
        r"sk-[A-Za-z0-9_-]{10,}"          # OpenAI / Anthropic / OpenRouter
        r"|ghp_[A-Za-z0-9]{10,}"          # GitHub PAT (classic)
        r"|github_pat_[A-Za-z0-9_]{10,}"  # GitHub PAT (fine-grained)
        r"|gho_[A-Za-z0-9]{10,}"          # GitHub OAuth token
        r"|ghu_[A-Za-z0-9]{10,}"          # GitHub user-to-server token
        r"|ghs_[A-Za-z0-9]{10,}"          # GitHub server-to-server token
        r"|ghr_[A-Za-z0-9]{10,}"          # GitHub refresh token
        r"|AKIA[A-Z0-9]{16}"              # AWS Access Key ID
        r"|xox[baprs]-[A-Za-z0-9-]{10,}" # Slack tokens
        r"|hf_[A-Za-z0-9]{10,}"          # HuggingFace token
        r"|SG\.[A-Za-z0-9_-]{10,}"       # SendGrid API key
        r")(?![A-Za-z0-9_-])"
    )
    _AUTH_HDR_RE = _re.compile(r"(Authorization:\s*Bearer\s+)(\S+)", _re.IGNORECASE)
    _ENV_RE = _re.compile(
        r"([A-Z0-9_]{0,50}(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)[A-Z0-9_]{0,50})"
        r"\s*=\s*(['\"]?)(\S+)\2"
    )
    _PRIVKEY_RE = _re.compile(
        r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
    )

    def _mask(token: str) -> str:
        return f"{token[:6]}...{token[-4:]}" if len(token) >= 18 else "***"

    def _fallback_redact(text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        text = _CRED_RE.sub(lambda m: _mask(m.group(1)), text)
        text = _AUTH_HDR_RE.sub(lambda m: m.group(1) + _mask(m.group(2)), text)
        text = _ENV_RE.sub(
            lambda m: f"{m.group(1)}={m.group(2)}{_mask(m.group(3))}{m.group(2)}", text
        )
        text = _PRIVKEY_RE.sub("[REDACTED PRIVATE KEY]", text)
        return text

    try:
        from agent.redact import redact_sensitive_text
    except ImportError:
        return _fallback_redact

    def _combined_redact(text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        # WebUI API responses are a hard safety boundary — pass force=True so the
        # agent's broader patterns (Stripe sk_live_, Google AIza…, JWT eyJ…, DB
        # connection strings, Telegram bot tokens) run regardless of the user's
        # HERMES_REDACT_SECRETS opt-in. The local fallback then handles the
        # common short-prefix shapes the agent omits (ghp_, sk-, hf_, AKIA).
        try:
            agent_redacted = redact_sensitive_text(text, force=True)
        except TypeError:
            # Older hermes-agent builds that predate the force kwarg.
            agent_redacted = redact_sensitive_text(text)
        return _fallback_redact(agent_redacted)

    return _combined_redact


_redact_fn_cached = _build_redact_fn()


def _redact_text(text: str, *, _enabled: bool | None = None) -> str:
    """Redact sensitive text from API responses. Respects api_redact_enabled setting.

    The ``_enabled`` parameter is an internal optimization for callers that
    redact many strings in a single response — `redact_session_data()` reads
    the setting once and threads it through ``_redact_value`` so we avoid
    re-loading settings.json from disk per string. (Opus pre-release perf fix.)
    """
    if not isinstance(text, str) or not text:
        return text
    if _enabled is None:
        from api.config import load_settings
        _enabled = bool(load_settings().get("api_redact_enabled", True))
    if not _enabled:
        return text
    return _redact_fn_cached(text)


def _redact_value(v, *, _enabled: bool | None = None):
    """Recursively redact credentials from strings, dicts, and lists.

    ``_enabled`` is threaded through so a single response-level redact pass
    only reads settings.json once. (Opus pre-release perf fix.)
    """
    if isinstance(v, str):
        return _redact_text(v, _enabled=_enabled)
    if isinstance(v, dict):
        return {k: _redact_value(val, _enabled=_enabled) for k, val in v.items()}
    if isinstance(v, list):
        return [_redact_value(item, _enabled=_enabled) for item in v]
    return v


def redact_session_data(session_dict: dict) -> dict:
    """Redact credentials from message content and tool_call data before API response.

    Applies to: messages[], tool_calls[], and title.
    The underlying session file is not modified; redaction is response-layer only.

    Reads the ``api_redact_enabled`` setting ONCE for the entire response and
    threads it through to avoid hundreds of settings.json reads per session
    payload (a 50-message session has hundreds of nested strings). When the
    setting is disabled this is also a fast path: the recursion still walks
    but every string returns early.
    """
    from api.config import load_settings
    _enabled = bool(load_settings().get("api_redact_enabled", True))
    result = dict(session_dict)
    if isinstance(result.get('title'), str):
        result['title'] = _redact_text(result['title'], _enabled=_enabled)
    if 'messages' in result:
        result['messages'] = _redact_value(result['messages'], _enabled=_enabled)
    if 'tool_calls' in result:
        result['tool_calls'] = _redact_value(result['tool_calls'], _enabled=_enabled)
    return result


def read_body(handler) -> dict:
    """Read and JSON-parse a POST request body (capped at 20MB)."""
    length = int(handler.headers.get('Content-Length', 0))
    if length > MAX_BODY_BYTES:
        raise ValueError(f'Request body too large ({length} bytes, max {MAX_BODY_BYTES})')
    raw = handler.rfile.read(length) if length else b'{}'
    try:
        return _json.loads(raw)
    except Exception:
        return {}


# ── Profile cookie helpers (issue #798) ─────────────────────────────────────

PROFILE_COOKIE_NAME = 'hermes_profile'


def get_profile_cookie_name() -> str:
    """Return the cookie name used to persist the active WebUI profile."""
    return os.getenv('WEBUI_PROFILE_COOKIE_NAME', PROFILE_COOKIE_NAME)


def get_profile_cookie(handler) -> str | None:
    """Extract the active-profile cookie value from the request, or None."""
    cookie_header = handler.headers.get('Cookie', '')
    if not cookie_header:
        return None
    import http.cookies as _hc
    cookie = _hc.SimpleCookie()
    try:
        cookie.load(cookie_header)
    except _hc.CookieError:
        return None
    cookie_name = get_profile_cookie_name()
    morsel = cookie.get(cookie_name)
    if morsel and morsel.value:
        # Validate against profile-name pattern before trusting
        from api.profiles import _PROFILE_ID_RE
        val = morsel.value
        if val == 'default' or _PROFILE_ID_RE.fullmatch(val):
            return val
    return None


def build_profile_cookie(name: str) -> str:
    """Build a Set-Cookie header value for the active-profile cookie.

    Always persist the selected profile in the cookie, including 'default'.
    Clearing the cookie causes the backend to fall back to process-global
    _active_profile, which can unexpectedly switch clients back to another
    profile.

    Set HttpOnly because the UI reads the active profile from
    /api/profile/active JSON and does not need to access this cookie via
    document.cookie.
    """
    import http.cookies as _hc
    cookie = _hc.SimpleCookie()
    cookie_name = get_profile_cookie_name()
    cookie[cookie_name] = name
    cookie[cookie_name]['path'] = '/'
    cookie[cookie_name]['httponly'] = True
    cookie[cookie_name]['samesite'] = 'Lax'
    return cookie[cookie_name].OutputString()
