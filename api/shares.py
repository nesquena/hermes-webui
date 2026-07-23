"""
Hermes Web UI -- public read-only share snapshots.

Stores a sanitized, immutable snapshot of a conversation under STATE_DIR/shares.
The snapshot is intentionally narrower than a full session export so public
links do not leak local workspace paths, profile details, or raw tool payloads.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import posixpath
import re
import secrets
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

from api.config import HOST, PORT, STATE_DIR
from api.helpers import redact_session_data
# _redact_fn_cached is the ALWAYS-ON credential redactor (agent redactor with
# force=True + local fallback regex). Unlike redact_session_data it does NOT
# consult the user-toggleable api_redact_enabled setting — a public share is a
# hard safety boundary that must redact credentials even if the operator turned
# API-response redaction off.
from api.helpers import _redact_fn_cached as _force_redact_credentials

logger = logging.getLogger(__name__)

SHARES_DIR = STATE_DIR / "shares"
_SHARE_LOCK = threading.Lock()
_LOCAL_ATTACHMENT_PLACEHOLDER = "[Local attachment omitted from public share]"

_MEDIA_TOKEN_PREFIX = "MEDIA:"
_MARKDOWN_FILE_URI_RE = re.compile(
    r"!?\[[^\]\n]*\]\(\s*file://[^\s\)]+\s*\)",
    re.IGNORECASE,
)
_ANGLE_FILE_URI_RE = re.compile(r"<file://[^\s>]+>", re.IGNORECASE)
_FILE_URI_RE = re.compile(r"file://[^\s<>'\"\)\]]+", re.IGNORECASE)
_API_MEDIA_PATH_RE = re.compile(r"(?:^|/)api/media(?:/|$)", re.IGNORECASE)
_RAW_PRE_REGION_RE = re.compile(r"<pre\b[^>]*>[\s\S]*?</pre>", re.IGNORECASE)
_FENCED_CODE_REGION_RE = re.compile(
    r"(^|\n)[ ]{0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n[\s\S]*?\n[ ]{0,3}(?P=fence)[ \t]*(?=\n|$)"
)
_INLINE_CODE_REGION_RE = re.compile(r"`[^`\n]*`")
_DATA_IMAGE_RE = re.compile(
    r"^data:image/(?:png|jpe?g|gif|webp|avif)(?:;base64)?,[a-z0-9+/=%._~:@!$&'()*+,;-]*$",
    re.IGNORECASE,
)
_DATA_IMAGE_SVG_RE = re.compile(r"^data:image/svg\+xml;base64,[a-z0-9+/=]+$", re.IGNORECASE)
_DATA_IMAGE_MAX_LEN = 2 * 1024 * 1024
_EMBEDDED_IPV4_RE = re.compile(
    r"(?<!\d)(\d{1,3})[.-](\d{1,3})[.-](\d{1,3})[.-](\d{1,3})(?!\d)"
)
_BROWSER_IPV4_PART = r"(?:0[xX][0-9A-Fa-f]+|[0-9]+)"
_BROWSER_IPV4_LITERAL_RE = re.compile(
    rf"^{_BROWSER_IPV4_PART}(?:\.{_BROWSER_IPV4_PART}){{0,3}}\.?$"
)
_LOCAL_ONLY_HOSTNAMES = {"localhost", "host.docker.internal"}
_LOCAL_ONLY_HOSTNAME_SUFFIXES = (".localhost", ".local", ".home.arpa")


def _ensure_share_dir() -> None:
    SHARES_DIR.mkdir(parents=True, exist_ok=True)


def _share_path(token: str) -> Path:
    token = str(token or "").strip()
    if not token:
        raise ValueError("share token is required")
    if not token.replace("-", "").replace("_", "").isalnum():
        raise ValueError("invalid share token")
    return SHARES_DIR / f"{token}.json"


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f"{path.stem}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _share_message_text(message: dict) -> str:
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                # Non-dict list items (e.g. nested structures) are NOT plain text —
                # never stringify them into the public snapshot.
                continue
            if item.get("type") == "text":
                # Only append genuine string text — a dict-valued "text" (possible
                # via /api/session/import) must NOT be str()'d into the public
                # snapshot (that would publish structured/tool payload verbatim).
                _t = item.get("text")
                if isinstance(_t, str):
                    parts.append(_t)
        return "".join(parts).strip()
    if isinstance(content, str):
        return content.strip()
    # A dict/other structured content (e.g. a tool-result object) is NOT shareable
    # text — do NOT str() it (that would publish raw structured/tool payload).
    return ""


def _redact_share_paths(text: str, extra_paths) -> str:
    """Strip known local session/workspace/home paths out of public-share text.

    A workspace path or Hermes home can be embedded inside message prose (an
    agent quoting a file path, a traceback, etc.). Redact the concrete local
    paths so a public share never discloses the operator's filesystem layout.
    """
    if not isinstance(text, str) or not text:
        return text
    for p in extra_paths:
        if not p:
            continue
        p = str(p).strip()
        if len(p) >= 4 and p in text:
            text = text.replace(p, "[redacted-path]")
    return text


def _hostname_has_non_global_embedded_ip(hostname: str) -> bool:
    for match in _EMBEDDED_IPV4_RE.finditer(hostname):
        try:
            if not ipaddress.ip_address(".".join(match.groups())).is_global:
                return True
        except ValueError:
            continue
    return False


def _parse_browser_ipv4_number(part: str) -> int:
    raw = str(part or "")
    lower = raw.lower()
    if lower.startswith("0x"):
        return int(raw[2:], 16)
    if len(raw) > 1 and raw.startswith("0"):
        return int(raw[1:] or "0", 8)
    return int(raw, 10)


def _parse_browser_ipv4_literal(hostname: str):
    """Parse legacy IPv4 host forms accepted by browser URL parsers."""
    host = str(hostname or "").rstrip(".")
    if not _BROWSER_IPV4_LITERAL_RE.fullmatch(host):
        return None
    parts = host.split(".")
    numbers = [_parse_browser_ipv4_number(part) for part in parts]
    if len(numbers) > 4:
        raise ValueError("too many ipv4 parts")
    for number in numbers[:-1]:
        if number > 255:
            raise ValueError("ipv4 part out of range")
    max_last = (256 ** (5 - len(numbers))) - 1
    if numbers[-1] > max_last:
        raise ValueError("ipv4 final part out of range")
    value = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        value += number * (256 ** (3 - index))
    return ipaddress.IPv4Address(value)


def _hostname_looks_like_browser_ipv4_literal(hostname: str) -> bool:
    try:
        return _parse_browser_ipv4_literal(hostname) is not None
    except ValueError:
        return _BROWSER_IPV4_LITERAL_RE.fullmatch(str(hostname or "").rstrip(".")) is not None


def _browser_normalized_hostname(hostname: str) -> str:
    try:
        decoded = unquote(str(hostname or ""), errors="strict")
        normalized = unicodedata.normalize("NFKC", decoded)
        return normalized.encode("idna").decode("ascii").strip(".").lower()
    except UnicodeError as exc:
        raise ValueError("invalid hostname") from exc


def _is_safe_data_image_uri(raw_ref: str) -> bool:
    value = str(raw_ref or "")
    return len(value) <= _DATA_IMAGE_MAX_LEN and (
        _DATA_IMAGE_RE.fullmatch(value) is not None
        or _DATA_IMAGE_SVG_RE.fullmatch(value) is not None
    )


def _media_url_path_for_boundary_check(path: str) -> str:
    decoded = str(path or "")
    for _ in range(4):
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    decoded = decoded.replace("\\", "/")
    normalized = posixpath.normpath(decoded)
    if normalized == ".":
        return ""
    if decoded.startswith("/") and not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized


def _url_port(parsed) -> int | None:
    try:
        return parsed.port
    except ValueError as exc:
        raise ValueError("invalid port") from exc


def _default_origin_port(scheme: str) -> int:
    return 443 if str(scheme or "").lower() == "https" else 80


def _origin_ports_match(scheme: str, left: int | None, right: int | None) -> bool:
    if left == right:
        return True
    default = _default_origin_port(scheme)
    return (left is None and right == default) or (right is None and left == default)


def _trusted_webui_origin_parts() -> set[tuple[str, str, int | None]]:
    origins: set[tuple[str, str, int | None]] = set()

    def add_origin(raw_origin: str) -> None:
        raw = str(raw_origin or "").strip().rstrip("/")
        if not raw:
            return
        try:
            parsed = urlsplit(raw)
            scheme = parsed.scheme.lower()
            hostname = _browser_normalized_hostname(parsed.hostname or "")
            port = _url_port(parsed)
        except ValueError:
            return
        if scheme in {"http", "https"} and hostname:
            origins.add((scheme, hostname, port))

    for value in os.getenv("HERMES_WEBUI_ALLOWED_ORIGINS", "").split(","):
        add_origin(value)

    configured_host = str(HOST or "").strip()
    if configured_host and configured_host not in {"0.0.0.0", "::"}:
        if ":" in configured_host and not configured_host.startswith("["):
            configured_host = f"[{configured_host}]"
        add_origin(f"http://{configured_host}:{int(PORT)}")

    return origins


def _is_trusted_webui_origin(parsed, hostname: str) -> bool:
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        return False
    port = _url_port(parsed)
    for allowed_scheme, allowed_host, allowed_port in _trusted_webui_origin_parts():
        if (
            scheme == allowed_scheme
            and hostname == allowed_host
            and _origin_ports_match(scheme, port, allowed_port)
        ):
            return True
    return False


def _has_invalid_bracketed_authority(parsed) -> bool:
    netloc = str(parsed.netloc or "").rsplit("@", 1)[-1]
    if not netloc.startswith("["):
        return False
    end = netloc.find("]")
    if end <= 1:
        return True
    try:
        ipaddress.ip_address(_browser_normalized_hostname(netloc[1:end]))
    except ValueError:
        return True
    return False


def _is_public_media_url(raw_ref: str) -> bool:
    try:
        parsed = urlsplit(str(raw_ref or "").replace("\\", "/"))
        hostname = _browser_normalized_hostname(parsed.hostname or "")
        _url_port(parsed)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return False
    if _has_invalid_bracketed_authority(parsed):
        return False
    try:
        ip = ipaddress.ip_address(hostname)
        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            return False
    except ValueError:
        if (
            hostname in _LOCAL_ONLY_HOSTNAMES
            or hostname.endswith(_LOCAL_ONLY_HOSTNAME_SUFFIXES)
            or "." not in hostname
            or _hostname_looks_like_browser_ipv4_literal(hostname)
            or _hostname_has_non_global_embedded_ip(hostname)
        ):
            return False
    if _API_MEDIA_PATH_RE.search(_media_url_path_for_boundary_check(parsed.path)):
        return not _is_trusted_webui_origin(parsed, hostname)
    return True


def _is_public_media_reference(raw_ref: str) -> bool:
    return _is_safe_data_image_uri(raw_ref) or _is_public_media_url(raw_ref)


def _scan_media_ref_end(text: str, start: int, *, bracket_wrapped: bool) -> tuple[int, bool]:
    inner_brackets = 0
    index = start
    while index < len(text):
        ch = text[index]
        if inner_brackets == 0 and ch in " \t\r\n)`<":
            return index, False
        if ch == "[":
            inner_brackets += 1
        elif ch == "]":
            if inner_brackets:
                inner_brackets -= 1
            elif bracket_wrapped:
                return index, True
            else:
                return index, False
        index += 1
    return index, False


def _replace_media_tokens(text: str, replace_token) -> str:
    pieces: list[str] = []
    cursor = 0
    while True:
        media_start = text.find(_MEDIA_TOKEN_PREFIX, cursor)
        if media_start < 0:
            pieces.append(text[cursor:])
            break

        bracket_start = media_start - 1
        has_wrapper = bracket_start >= cursor and text[bracket_start] == "["
        raw_start = media_start + len(_MEDIA_TOKEN_PREFIX)
        if has_wrapper:
            raw_end, wrapper_closed = _scan_media_ref_end(
                text,
                raw_start,
                bracket_wrapped=True,
            )
            if wrapper_closed:
                pieces.append(text[cursor:bracket_start])
                raw_ref = text[raw_start:raw_end]
                pieces.append(replace_token(text[bracket_start : raw_end + 1], raw_ref))
                cursor = raw_end + 1
                continue

        raw_end, _ = _scan_media_ref_end(text, raw_start, bracket_wrapped=False)
        pieces.append(text[cursor:media_start])
        raw_ref = text[raw_start:raw_end]
        pieces.append(replace_token(text[media_start:raw_end], raw_ref))
        cursor = raw_end

    return "".join(pieces)


def _stash_share_code_regions(text: str):
    stashed: list[str] = []
    stash_prefix = f"\x00SHARE_CODE_{secrets.token_hex(8)}_"

    def stash(match: re.Match) -> str:
        stashed.append(match.group(0))
        return f"{stash_prefix}{len(stashed) - 1}\x00"

    protected = _RAW_PRE_REGION_RE.sub(stash, text)
    protected = _FENCED_CODE_REGION_RE.sub(stash, protected)
    protected = _INLINE_CODE_REGION_RE.sub(stash, protected)

    def restore(value: str) -> str:
        for index, original in enumerate(stashed):
            value = value.replace(f"{stash_prefix}{index}\x00", original)
        return value

    return protected, restore


def _omit_local_media_references(text: str, *, protect_code_regions: bool = True) -> str:
    """Replace refs that the public renderer would route through /api/media."""
    if not isinstance(text, str) or not text:
        return text

    public_media_tokens: list[str] = []
    stash_prefix = f"\x00SHARE_MEDIA_{secrets.token_hex(8)}_"

    def stash_public_token(token: str) -> str:
        public_media_tokens.append(token)
        return f"{stash_prefix}{len(public_media_tokens) - 1}\x00"

    def replace_media_token(token: str, raw_ref: str) -> str:
        if _is_public_media_reference(raw_ref):
            return stash_public_token(token)
        return _LOCAL_ATTACHMENT_PLACEHOLDER

    sanitized = _replace_media_tokens(text, replace_media_token)
    restore_code_regions = None
    if protect_code_regions:
        sanitized, restore_code_regions = _stash_share_code_regions(sanitized)
    sanitized = _MARKDOWN_FILE_URI_RE.sub(_LOCAL_ATTACHMENT_PLACEHOLDER, sanitized)
    sanitized = _ANGLE_FILE_URI_RE.sub(_LOCAL_ATTACHMENT_PLACEHOLDER, sanitized)
    sanitized = _FILE_URI_RE.sub(_LOCAL_ATTACHMENT_PLACEHOLDER, sanitized)
    if restore_code_regions is not None:
        sanitized = restore_code_regions(sanitized)
    for index, token in enumerate(public_media_tokens):
        sanitized = sanitized.replace(f"{stash_prefix}{index}\x00", token)
    return sanitized


def _sanitize_message(message: dict, *, redact_paths=()) -> dict | None:
    if not isinstance(message, dict):
        return None
    role = str(message.get("role") or "").strip().lower()
    if role not in {"user", "assistant"}:
        return None
    text = _share_message_text(message)
    if not text:
        return None
    # ALWAYS-ON hardening for the public boundary, independent of any setting:
    # (1) force credential redaction, (2) omit local media references that the
    # public renderer would route through authenticated /api/media, (3) strip
    # known local paths from the remaining prose.
    text = _force_redact_credentials(text)
    text = _omit_local_media_references(text)
    text = _redact_share_paths(text, redact_paths)
    if not text.strip():
        return None
    sanitized = {
        "role": role,
        "content": text,
    }
    ts = message.get("timestamp")
    if isinstance(ts, (int, float)):
        sanitized["timestamp"] = ts
    return sanitized


def _public_share_payload(payload: dict) -> dict:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        messages = []
    public = {
        "title": str(payload.get("title") or "Untitled"),
        "messages": messages,
        "message_count": int(payload.get("message_count") or len(messages)),
    }
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")
    if isinstance(created_at, (int, float)):
        public["created_at"] = created_at
    if isinstance(updated_at, (int, float)):
        public["updated_at"] = updated_at
    return public


def build_share_snapshot(session) -> dict:
    raw_dict = getattr(session, "__dict__", {}) or {}
    # redact_session_data respects the api_redact_enabled setting; keep it as a
    # first pass, but the per-message sanitizer below applies ALWAYS-ON credential
    # + path redaction that does NOT depend on that setting (the public boundary
    # must hold even if the operator disabled api_redact_enabled).
    safe_session = redact_session_data(raw_dict)
    # Concrete local paths to scrub from any message prose / title.
    redact_paths = []
    for key in ("workspace", "worktree_path", "worktree_repo_root"):
        val = raw_dict.get(key)
        if val:
            redact_paths.append(str(val))
    try:
        from api.profiles import get_active_hermes_home
        redact_paths.append(str(get_active_hermes_home()))
    except Exception:
        pass
    try:
        redact_paths.append(str(Path.home()))
    except Exception:
        pass
    safe_messages = []
    for raw in safe_session.get("messages") or []:
        sanitized = _sanitize_message(raw, redact_paths=redact_paths)
        if sanitized:
            safe_messages.append(sanitized)
    if not safe_messages:
        raise ValueError("This conversation has no shareable messages yet.")
    # Only accept a genuine string title — a dict-valued title (possible via
    # /api/session/import) must not be str()'d into the public snapshot.
    _raw_title = safe_session.get("title")
    _raw_title = _raw_title if isinstance(_raw_title, str) else "Untitled"
    title = _force_redact_credentials(_raw_title or "Untitled")
    title = _omit_local_media_references(title, protect_code_regions=False)
    title = _redact_share_paths(title, redact_paths) or "Untitled"
    return {
        "title": title,
        "messages": safe_messages,
        "message_count": len(safe_messages),
    }


def create_or_refresh_share(session) -> dict:
    snapshot = build_share_snapshot(session)
    with _SHARE_LOCK:
        _ensure_share_dir()
        existing_token = str(getattr(session, "share_token", "") or "").strip()
        token = existing_token or secrets.token_urlsafe(18)
        now = time.time()
        payload = {
            "token": token,
            "source_session_id": str(getattr(session, "session_id", "") or ""),
            "title": snapshot["title"],
            "messages": snapshot["messages"],
            "message_count": snapshot["message_count"],
            "created_at": now,
            "updated_at": now,
            "revoked_at": None,
        }
        path = _share_path(token)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    payload["created_at"] = existing.get("created_at") or now
            except Exception:
                logger.debug("Ignoring malformed share snapshot at %s", path, exc_info=True)
        _write_json_atomic(path, payload)
    return {
        "share_token": token,
        "share_title": payload["title"],
        "share_message_count": payload["message_count"],
        "share_created_at": payload["created_at"],
        "share_updated_at": payload["updated_at"],
    }


def load_share(token: str) -> dict | None:
    try:
        path = _share_path(token)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to read share snapshot %s", path, exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("revoked_at"):
        return None
    return _public_share_payload(payload)


def revoke_share(session) -> bool:
    token = str(getattr(session, "share_token", "") or "").strip()
    if not token:
        return False
    with _SHARE_LOCK:
        try:
            path = _share_path(token)
        except ValueError:
            return False
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload["revoked_at"] = time.time()
            _write_json_atomic(path, payload)
    return True
