"""
Hermes Web UI -- HTTP helper functions.
"""
import json as _json
import logging
import os
import re as _re
import ssl
from pathlib import Path
from api.config import IMAGE_EXTS, MD_EXTS

logger = logging.getLogger(__name__)


# Treat stalled/closed HTTP clients as normal disconnects.  Long-lived SSE
# connections often end this way when a browser tab sleeps, a phone switches
# networks, or Tailscale leaves the socket half-closed.
_CLIENT_DISCONNECT_ERRORS = (
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
    TimeoutError,
    ssl.SSLError,
)


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


_CSP_CONNECT_BASE = (
    "'self' http://127.0.0.1:* http://localhost:* http://ipc.localhost "
    "https://127.0.0.1:* https://localhost:* "
    "ws://127.0.0.1:* ws://localhost:*"
)
_CSP_EXTRA_CONNECT_RE = _re.compile(
    r"^(?:https?|wss?)://(?:\*\.)?[A-Za-z0-9._~-]+(?::(?P<port>\d{1,5}|\*))?$"
)
_CSP_HEADER_NAME = 'Content-Security-Policy'
_CSP_SHARED_POLICY_TEMPLATE = (
    "default-src 'self' https://*.cloudflareaccess.com; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://static.cloudflareinsights.com blob:; "
    "worker-src blob: 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "img-src 'self' data: https: blob:; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "media-src 'self' data: blob:; "
    "connect-src {connect_src}; "
    "manifest-src 'self' https://*.cloudflareaccess.com; "
    "base-uri 'self'; form-action 'self'"
)


def _valid_csp_extra_connect_source(source: str) -> bool:
    match = _CSP_EXTRA_CONNECT_RE.fullmatch(source)
    if not match:
        return False
    port = match.group("port")
    if not port or port == "*":
        return True
    try:
        return 1 <= int(port) <= 65535
    except ValueError:
        return False


def _csp_extra_connect_src() -> str:
    raw = os.getenv("HERMES_WEBUI_CSP_CONNECT_EXTRA", "").strip()
    if not raw:
        return ""
    sources = raw.split()
    if not sources or any(not _valid_csp_extra_connect_source(src) for src in sources):
        logger.warning("Ignoring invalid HERMES_WEBUI_CSP_CONNECT_EXTRA value")
        return ""
    return " " + " ".join(sources)


def _csp_connect_src(extra_connect_src: str = "") -> str:
    return f"{_CSP_CONNECT_BASE} https://cdn.jsdelivr.net{extra_connect_src}"


def _build_csp_enforced_policy(extra_connect_src: str | None = None) -> str:
    if extra_connect_src is None:
        extra_connect_src = _csp_extra_connect_src()
    return _CSP_SHARED_POLICY_TEMPLATE.format(
        connect_src=_csp_connect_src(extra_connect_src)
    )


def _build_csp_report_only_policy(extra_connect_src: str | None = None) -> str:
    return (
        _build_csp_enforced_policy(extra_connect_src)
        + "; report-uri /api/csp-report; report-to csp-endpoint"
    )


def _security_headers(handler):
    """Add security headers to every response."""
    extra_connect_src = _csp_extra_connect_src()
    handler._csp_extra_connect_src = extra_connect_src
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('X-Frame-Options', 'DENY')
    handler.send_header('Referrer-Policy', 'same-origin')
    handler.send_header(_CSP_HEADER_NAME, _build_csp_enforced_policy(extra_connect_src))
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


def _safe_write(handler, body: bytes) -> None:
    """Write response body, ignoring expected client disconnect errors.

    Logs disconnects at debug level so they are observable without
    polluting stdout/stderr during normal operation (SSE reconnects,
    tab closes, mobile network switches, etc.).
    """
    try:
        handler.end_headers()
        handler.wfile.write(body)
    except _CLIENT_DISCONNECT_ERRORS as exc:
        import logging
        logging.getLogger("hermes.webui").debug(
            "Client disconnected mid-response (%s): %s",
            type(exc).__name__,
            getattr(handler, "path", "?"),
        )


def _json_response_body(payload, *, pretty: bool = True) -> bytes:
    """Serialize API JSON responses.

    Sidebar/session endpoints can return thousands of rows on large installs.
    Pretty-printing large list responses inflates both CPU and wire bytes. Keep
    the public helper default stable for existing tests/callers; hot paths can
    opt into compact JSON with ``pretty=False``.
    """
    if pretty:
        return _json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    return _json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def j(handler, payload, status: int=200, extra_headers: dict=None, *, pretty: bool = True) -> None:
    """Send a JSON response.

    *extra_headers*: optional dict of additional headers to include
    (e.g., {'Set-Cookie': '...'}).  Headers are sent before end_headers().
    """
    body = _json_response_body(payload, pretty=pretty)
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
    _safe_write(handler, body)


def t(handler, payload, status: int=200, content_type: str='text/plain; charset=utf-8') -> None:
    """Send a plain text or HTML response."""
    body = payload if isinstance(payload, bytes) else str(payload).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-store')
    _security_headers(handler)
    _safe_write(handler, body)


MAX_BODY_BYTES = 20 * 1024 * 1024  # 20MB limit for non-upload POST bodies


# ── Credential redaction ──────────────────────────────────────────────────────

def _build_redact_fn():
    """Return a redactor backed by hermes-agent plus local fallback patterns."""
    # Fallback mirrors the agent's known credential prefixes so WebUI API
    # responses remain a hard redaction boundary even without hermes-agent.
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
        r"|xox[baprs]-[A-Za-z0-9-]{10,}"  # Slack tokens
        r"|AIza[A-Za-z0-9_-]{30,}"        # Google API keys
        r"|pplx-[A-Za-z0-9]{10,}"         # Perplexity
        r"|fal_[A-Za-z0-9_-]{10,}"        # Fal.ai
        r"|fc-[A-Za-z0-9]{10,}"           # Firecrawl
        r"|bb_live_[A-Za-z0-9_-]{10,}"    # BrowserBase
        r"|gAAAA[A-Za-z0-9_=-]{20,}"      # Codex encrypted tokens
        r"|AKIA[A-Z0-9]{16}"              # AWS Access Key ID
        r"|sk_live_[A-Za-z0-9]{10,}"      # Stripe secret key (live)
        r"|sk_test_[A-Za-z0-9]{10,}"      # Stripe secret key (test)
        r"|rk_live_[A-Za-z0-9]{10,}"      # Stripe restricted key
        r"|SG\.[A-Za-z0-9_-]{10,}"        # SendGrid API key
        r"|hf_[A-Za-z0-9]{10,}"           # HuggingFace token
        r"|r8_[A-Za-z0-9]{10,}"           # Replicate API token
        r"|npm_[A-Za-z0-9]{10,}"          # npm access token
        r"|pypi-[A-Za-z0-9_-]{10,}"       # PyPI API token
        r"|dop_v1_[A-Za-z0-9]{10,}"       # DigitalOcean PAT
        r"|doo_v1_[A-Za-z0-9]{10,}"       # DigitalOcean OAuth
        r"|am_[A-Za-z0-9_-]{10,}"         # AgentMail API key
        r"|sk_[A-Za-z0-9_]{10,}"          # ElevenLabs TTS key
        r"|tvly-[A-Za-z0-9]{10,}"         # Tavily search API key
        r"|exa_[A-Za-z0-9]{10,}"          # Exa search API key
        r"|gsk_[A-Za-z0-9]{10,}"          # Groq Cloud API key
        r"|syt_[A-Za-z0-9]{10,}"          # Matrix access token
        r"|retaindb_[A-Za-z0-9]{10,}"     # RetainDB API key
        r"|hsk-[A-Za-z0-9]{10,}"          # Hindsight API key
        r"|mem0_[A-Za-z0-9]{10,}"         # Mem0 Platform API key
        r"|brv_[A-Za-z0-9]{10,}"          # ByteRover API key
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


_SENSITIVE_CASE_MARKERS = (
    "sk-",
    "ghp_",
    "github_pat_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "AKIA",
    "xoxb-",
    "xoxa-",
    "xoxp-",
    "xoxr-",
    "xoxs-",
    "AIza",
    "pplx-",
    "fal_",
    "fc-",
    "bb_live_",
    "gAAAA",
    "sk_live_",
    "sk_test_",
    "rk_live_",
    "SG.",
    "hf_",
    "r8_",
    "npm_",
    "pypi-",
    "dop_v1_",
    "doo_v1_",
    "am_",
    "sk_",
    "tvly-",
    "exa_",
    "gsk_",
    "syt_",
    "retaindb_",
    "hsk-",
    "mem0_",
    "brv_",
    "eyJ",
    "-----BEGIN",
)
_SENSITIVE_LOWER_MARKERS = (
    "authorization: bearer ",
    "private key",
    "postgres://",
    "postgresql://",
    "mysql://",
    "mongodb://",
    "redis://",
    "amqp://",
    "://",  # stage-348 Opus SHOULD-FIX: catch http(s)/ws(s)/ftp URL userinfo + sensitive query params (#2171 follow-up)
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "apikey",
    "client_secret",
    "auth_token",
    "raw_secret",
    "secret_input",
    "key_material",
    "x-amz-signature",
    "token=",
    "secret=",
    "password=",
    "authorization=",
    "key=",
    '"token"',
    '"secret"',
    '"password"',
    '"bearer"',
)
_SENSITIVE_TELEGRAM_MARKER_RE = _re.compile(r"(?:bot)?\d{8,}:[-A-Za-z0-9_]{30,}")
_SENSITIVE_DISCORD_MARKER_RE = _re.compile(r"<@!?\d{17,20}>")
_SENSITIVE_PHONE_MARKER_RE = _re.compile(r"(?<![A-Za-z0-9])\+[1-9]\d{6,14}(?![A-Za-z0-9])")


def _might_contain_sensitive_text(text: str) -> bool:
    """Cheap prefilter before the full agent+fallback redaction pass."""
    if not isinstance(text, str) or not text:
        return False
    if any(marker in text for marker in _SENSITIVE_CASE_MARKERS):
        return True
    lower = text.lower()
    if any(marker in lower for marker in _SENSITIVE_LOWER_MARKERS):
        return True
    if ":" in text and _SENSITIVE_TELEGRAM_MARKER_RE.search(text):
        return True
    if "<@" in text and _SENSITIVE_DISCORD_MARKER_RE.search(text):
        return True
    if "+" in text and _SENSITIVE_PHONE_MARKER_RE.search(text):
        return True
    return False


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
    if not _might_contain_sensitive_text(text):
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
    """Redact credentials from message content, tool data, and session sidecars.

    Applies to: messages[], tool_calls[], todo_state, runtime_journal_snapshot,
    and title.
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
    if 'todo_state' in result:
        result['todo_state'] = _redact_value(result['todo_state'], _enabled=_enabled)
    if 'runtime_journal_snapshot' in result:
        result['runtime_journal_snapshot'] = _redact_value(
            result['runtime_journal_snapshot'],
            _enabled=_enabled,
        )
    return result


def read_body(handler) -> dict:
    """Read and JSON-parse a POST request body (capped at 20MB)."""
    raw_length = handler.headers.get('Content-Length', 0)
    try:
        length = int(raw_length)
    except (TypeError, ValueError):
        try:
            handler.close_connection = True
        except Exception:
            pass
        raise ValueError(f'Invalid Content-Length: {raw_length!r}')
    if length < 0:
        try:
            handler.close_connection = True
        except Exception:
            pass
        raise ValueError(f'Invalid Content-Length: {length}')
    if length > MAX_BODY_BYTES:
        try:
            handler.close_connection = True
        except Exception:
            pass
        raise ValueError(f'Request body too large ({length} bytes, max {MAX_BODY_BYTES})')
    raw = handler.rfile.read(length) if length else b'{}'
    try:
        return _json.loads(raw)
    except Exception:
        return {}


# ── Profile cookie helpers (issue #798) ─────────────────────────────────────

PROFILE_COOKIE_NAME = 'hermes_profile'
_PROFILE_COOKIE_ENV = 'HERMES_WEBUI_PROFILE_COOKIE_NAME'
_LEGACY_PROFILE_COOKIE_ENV = 'WEBUI_PROFILE_COOKIE_NAME'
_legacy_profile_cookie_warned = False


def get_profile_cookie_name() -> str:
    """Return the cookie name used to persist the active WebUI profile.

    Honours ``HERMES_WEBUI_PROFILE_COOKIE_NAME`` so multiple WebUI instances
    sharing a hostname (different ports) can use distinct profile-cookie names
    instead of trampling each other; browsers scope cookies by host, not
    host+port (RFC 6265). The original ``WEBUI_PROFILE_COOKIE_NAME`` is still
    honoured as a deprecated fallback (warned once per process, since this is
    called on every request).
    """
    name = os.getenv(_PROFILE_COOKIE_ENV, '').strip()
    if name:
        return name
    legacy = os.getenv(_LEGACY_PROFILE_COOKIE_ENV, '').strip()
    if legacy:
        global _legacy_profile_cookie_warned
        if not _legacy_profile_cookie_warned:
            logger.warning(
                '%s is deprecated; use %s instead.',
                _LEGACY_PROFILE_COOKIE_ENV,
                _PROFILE_COOKIE_ENV,
            )
            _legacy_profile_cookie_warned = True
        return legacy
    return PROFILE_COOKIE_NAME


def get_profile_cookie(handler) -> str | None:
    """Extract and authenticate the active-profile cookie value.

    When WebUI auth is enabled, the profile cookie is treated as an
    authorization input for profile-scoped routes. Require it to be signed for
    the current auth session so clients cannot forge ``hermes_profile`` to
    impersonate another profile. In no-auth deployments, keep the historical
    plain profile-name cookie behavior.
    """
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
    if not (morsel and morsel.value):
        return None

    from api.profiles import _PROFILE_ID_RE

    def _valid_profile_name(val: str) -> bool:
        return val == 'default' or bool(_PROFILE_ID_RE.fullmatch(val))

    raw_val = morsel.value
    try:
        from api.auth import is_auth_enabled, parse_cookie, verify_profile_cookie_value
        if is_auth_enabled():
            val = verify_profile_cookie_value(raw_val, parse_cookie(handler))
            return val if val and _valid_profile_name(val) else None
    except Exception:
        logger.warning("Failed to verify active profile cookie", exc_info=True)
        return None

    # No-auth mode: the cookie is a per-browser UI preference, not an authz
    # boundary, so retain the legacy plain profile-name format.
    return raw_val if _valid_profile_name(raw_val) else None


def build_profile_cookie(name: str, handler=None) -> str:
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
    value = name
    # Guard against a future call site silently emitting an UNSIGNED profile
    # cookie while auth is enabled (which a client could then... not forge, but
    # it would weaken the binding). If auth is on we require a handler so the
    # cookie is bound to the session. (#4023 Opus hardening.)
    try:
        from api.auth import is_auth_enabled
        _auth_on = is_auth_enabled()
    except Exception:
        _auth_on = False
    if _auth_on and handler is None:
        raise RuntimeError("build_profile_cookie requires a request handler when auth is enabled (to bind the profile cookie to the session)")
    if handler is not None:
        try:
            from api.auth import is_auth_enabled, parse_cookie, sign_profile_cookie_value
            if is_auth_enabled():
                value = sign_profile_cookie_value(name, parse_cookie(handler))
        except Exception as exc:
            logger.warning("Failed to sign active profile cookie", exc_info=True)
            raise RuntimeError("could not sign active profile cookie") from exc
    cookie[cookie_name] = value
    cookie[cookie_name]['path'] = '/'
    cookie[cookie_name]['httponly'] = True
    cookie[cookie_name]['samesite'] = 'Lax'
    return cookie[cookie_name].OutputString()
