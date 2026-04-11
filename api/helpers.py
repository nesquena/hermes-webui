"""
Hermes Web UI -- HTTP helper functions.
"""
import json as _json
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
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
        "base-uri 'self'; form-action 'self'"
    )
    handler.send_header(
        'Permissions-Policy',
        'camera=(), microphone=(), geolocation=()'
    )


def j(handler, payload, status: int=200) -> None:
    """Send a JSON response."""
    body = _json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-store')
    _security_headers(handler)
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
    """Return redact_sensitive_text from hermes-agent if available, else a fallback."""
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text
    except ImportError:
        pass

    # Minimal fallback covering the most common credential prefixes
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

    return _fallback_redact


_redact_text = _build_redact_fn()


def _redact_value(v):
    """Recursively redact credentials from strings, dicts, and lists."""
    if isinstance(v, str):
        return _redact_text(v)
    if isinstance(v, dict):
        return {k: _redact_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_redact_value(item) for item in v]
    return v


def redact_session_data(session_dict: dict) -> dict:
    """Redact credentials from message content and tool_call data before API response.

    Applies to: messages[], tool_calls[], and title.
    The underlying session file is not modified; redaction is response-layer only.
    """
    result = dict(session_dict)
    if isinstance(result.get('title'), str):
        result['title'] = _redact_text(result['title'])
    if 'messages' in result:
        result['messages'] = _redact_value(result['messages'])
    if 'tool_calls' in result:
        result['tool_calls'] = _redact_value(result['tool_calls'])
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
