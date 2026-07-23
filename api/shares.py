"""
Hermes Web UI -- public read-only share snapshots.

Stores a sanitized, immutable snapshot of a conversation under STATE_DIR/shares.
The snapshot is intentionally narrower than a full session export so public
links do not leak local workspace paths, profile details, or raw tool payloads.
"""

from __future__ import annotations

import base64
import html
import ipaddress
import io
import json
import logging
import mimetypes
import os
import posixpath
import re
import secrets
import tempfile
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
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


_MEDIA_TOKEN_PREFIX = "MEDIA:"
_MARKDOWN_FILE_URI_RE = re.compile(
    r"!?\[[^\]\n]*\]\(\s*file://[^\s\)]+\s*\)",
    re.IGNORECASE,
)
_ANGLE_FILE_URI_RE = re.compile(r"<file://[^\s>]+>", re.IGNORECASE)
_FILE_URI_RE = re.compile(r"(^|\s)(file://[^\s<>'\"\)\]]+)", re.IGNORECASE)
_API_MEDIA_PATH_RE = re.compile(r"(?:^|/)api/media(?:/|$)", re.IGNORECASE)
_RAW_PRE_REGION_RE = re.compile(r"<pre\b[^>]*>[\s\S]*?</pre>", re.IGNORECASE)
_ENTITY_PRE_REGION_RE = re.compile(
    r"&lt;(?i:pre)\b(?:[^&]|&(?!gt;))*&gt;[\s\S]*?&lt;/(?i:pre)&gt;"
)
_BLOCKQUOTE_FENCE_OPEN_RE = re.compile(r"^[ ]{0,3}(`{3,})([^\r\n`]*)$")
_BLOCKQUOTE_FENCE_CLOSE_RE = re.compile(r"^[ ]{0,3}(`{3,})[ \t]*$")
_FENCED_CODE_REGION_RE = re.compile(
    r"(^|\r\n|\r|\n)[ ]{0,3}(?P<fence>`{3,})([^\r\n`]*)"
    r"(?:\r\n|\r|\n)"
    r"(?:[\s\S]*?(?:\r\n|\r|\n))?[ ]{0,3}(?P=fence)`*[ \t]*(?=\r\n|\r|\n|$)"
)
_INLINE_CODE_REGION_RE = re.compile(r"`[^`\r\n]+`")
_DATA_IMAGE_RE = re.compile(
    r"^data:image/(?:png|jpe?g|gif|webp|avif)(?:;base64)?,[a-z0-9+/=%._~:@!$&'()*+,;-]*$",
    re.IGNORECASE,
)
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

# Max size (in bytes) for files we'll embed as base64 in a share snapshot.
_SHARE_EMBED_MAX_BYTES = 512 * 1024  # 512 KiB

# Only these image MIME types may be embedded in public shares.
# Non-image files and SVG are NEVER embedded — embedding arbitrary file
# bytes circumvents the credential-redaction boundary that protects
# message prose, and a public share is not a file-transfer service.
# SVG is excluded because it is the only text-bearing type in this set;
# agent-authored SVGs can carry credentials in their text content which
# _redact_share_paths (which only touches message prose, not embedded
# bytes) cannot reach.
_SHARE_ALLOWED_MIME_TYPES: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
})

# SVG namespace URI used during sanitisation.
_SVG_NS = "http://www.w3.org/2000/svg"

# Pattern matching on* event-handler attributes.
_ON_ATTR_RE = re.compile(r"^on\w+$", re.IGNORECASE)

# Dangerous href/xlink:href schemes.
_DANGEROUS_HREF_RE = re.compile(r"^\s*javascript\s*:", re.IGNORECASE)

# Static placeholder emitted when a media reference cannot be embedded.
_PLACEHOLDER = "[*Local attachment omitted from public share*]"
_LOCAL_ATTACHMENT_PLACEHOLDER = _PLACEHOLDER

# Magic byte signatures for allowed image formats — content-based validation
# that catches mismatched extensions (e.g. a .png that is actually a script).
# SVG is excluded here because it is validated by XML parsing in
# _sanitize_svg_bytes.
_IMAGE_MAGIC: dict[str, bytes] = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
    "image/gif": b"GIF8",
    "image/webp": b"RIFF",
}
# Offset for WebP magic: "RIFF" at 0, file size at 4, "WEBP" at 8.
_WEBP_MAGIC_OFFSET = 8
_WEBP_MAGIC = b"WEBP"


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


def _browser_normalized_hostname(hostname: str) -> str:
    try:
        decoded = unquote(str(hostname or ""), errors="strict")
        normalized = unicodedata.normalize("NFKC", decoded)
        return normalized.encode("idna").decode("ascii").strip(".").lower()
    except UnicodeError as exc:
        raise ValueError("invalid hostname") from exc


def _is_safe_data_image_uri(raw_ref: str) -> bool:
    value = str(raw_ref or "")
    return len(value) <= _DATA_IMAGE_MAX_LEN and _DATA_IMAGE_RE.fullmatch(value) is not None


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
    ref = str(raw_ref or "")
    if "[" in ref or "]" in ref:
        return False
    try:
        parsed = urlsplit(ref.replace("\\", "/"))
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
        try:
            browser_ipv4 = _parse_browser_ipv4_literal(hostname)
        except ValueError:
            return False
        if browser_ipv4 is not None:
            if not browser_ipv4.is_global:
                return False
        elif (
            hostname in _LOCAL_ONLY_HOSTNAMES
            or hostname.endswith(_LOCAL_ONLY_HOSTNAME_SUFFIXES)
            or "." not in hostname
            or _hostname_has_non_global_embedded_ip(hostname)
        ):
            return False
    if _API_MEDIA_PATH_RE.search(_media_url_path_for_boundary_check(parsed.path)):
        return not _is_trusted_webui_origin(parsed, hostname)
    return True


def _is_public_media_reference(raw_ref: str) -> bool:
    return _is_safe_data_image_uri(raw_ref) or _is_public_media_url(raw_ref)


def _media_authority_brackets(text: str, start: int) -> tuple[int, int] | None:
    scheme_match = re.match(r"https?://", text[start:], re.IGNORECASE)
    if not scheme_match:
        return None
    authority_start = start + scheme_match.end()
    authority_end = len(text)
    for index in range(authority_start, len(text)):
        if text[index] in "/?# \t\r\n)`<":
            authority_end = index
            break
    userinfo_end = text.rfind("@", authority_start, authority_end)
    host_start = userinfo_end + 1 if userinfo_end >= 0 else authority_start
    if host_start >= authority_end or text[host_start] != "[":
        return None
    host_end = text.find("]", host_start + 1, authority_end)
    if host_end < 0:
        return (host_start, max(host_start, authority_end - 1))
    return (host_start, host_end)


def _scan_media_ref_end(text: str, start: int, *, bracket_wrapped: bool) -> tuple[int, bool]:
    authority_brackets = _media_authority_brackets(text, start)
    index = start
    while index < len(text):
        if index > start and text.startswith(_MEDIA_TOKEN_PREFIX, index):
            return index, False
        ch = text[index]
        if ch in " \t\r\n)`<":
            return index, False
        if ch == "[":
            if authority_brackets is not None and index == authority_brackets[0]:
                index = authority_brackets[1] + 1
                continue
            return index, False
        if ch == "]":
            if bracket_wrapped:
                return index, True
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


def _line_content(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1]
    return line


def _share_lines_with_offsets(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    offset = 0
    for line in str(text or "").splitlines(keepends=True):
        lines.append((offset, _line_content(line)))
        offset += len(line)
    return lines


def _blockquote_depth_and_remainder(line: str) -> tuple[int, str]:
    depth = 0
    index = 0
    while index < len(line):
        if line[index] == ">":
            index += 1
        elif line.startswith("&gt;", index):
            index += 4
        else:
            break
        if index < len(line) and line[index] == " ":
            index += 1
        depth += 1
    return depth, line[index:]


def _is_fence_close_line(line: str, min_len: int) -> bool:
    match = _BLOCKQUOTE_FENCE_CLOSE_RE.fullmatch(line)
    return bool(match and len(match.group(1)) >= min_len)


def _stash_blockquote_fenced_code_regions(text: str, stash) -> str:
    lines = _share_lines_with_offsets(text)
    if not lines:
        return text

    regions: list[tuple[int, int]] = []
    active_fences: dict[int, tuple[int, int]] = {}
    for line_start, line in lines:
        depth, remainder = _blockquote_depth_and_remainder(line)
        for active_depth in [active_depth for active_depth in active_fences if active_depth > depth]:
            del active_fences[active_depth]

        active = active_fences.get(depth)
        if active is not None:
            region_start, fence_len = active
            if _is_fence_close_line(remainder, fence_len):
                if depth > 0:
                    regions.append((region_start, line_start + len(line)))
                del active_fences[depth]
            continue

        if any(active_depth < depth for active_depth in active_fences):
            continue

        opener = _BLOCKQUOTE_FENCE_OPEN_RE.fullmatch(remainder)
        if opener:
            active_fences[depth] = (line_start, len(opener.group(1)))

    if not regions:
        return text

    pieces: list[str] = []
    cursor = 0
    for region_start, region_end in regions:
        pieces.append(text[cursor:region_start])
        pieces.append(stash(text[region_start:region_end]))
        cursor = region_end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _stash_share_code_regions(text: str):
    stashed: list[str] = []
    stash_prefix = f"\x00SHARE_CODE_{secrets.token_hex(8)}_"

    def stash(match: re.Match | str) -> str:
        stashed.append(match.group(0) if hasattr(match, "group") else str(match))
        return f"{stash_prefix}{len(stashed) - 1}\x00"

    protected = _RAW_PRE_REGION_RE.sub(stash, text)
    protected = _ENTITY_PRE_REGION_RE.sub(stash, protected)
    protected = _stash_blockquote_fenced_code_regions(protected, stash)
    protected = _FENCED_CODE_REGION_RE.sub(stash, protected)
    protected = _INLINE_CODE_REGION_RE.sub(stash, protected)

    def restore(value: str) -> str:
        for index, original in enumerate(stashed):
            value = value.replace(f"{stash_prefix}{index}\x00", original)
        return value

    return protected, restore


def _stash_public_media_tokens(text: str):
    stashed: list[str] = []
    stash_prefix = f"\x00SHARE_MEDIA_{secrets.token_hex(8)}_"

    def maybe_stash(token: str, raw_ref: str) -> str:
        if _is_public_media_reference((raw_ref or "").strip()):
            stashed.append(token)
            return f"{stash_prefix}{len(stashed) - 1}\x00"
        return token

    protected = _replace_media_tokens(text, maybe_stash)

    def restore(value: str) -> str:
        for index, original in enumerate(stashed):
            value = value.replace(f"{stash_prefix}{index}\x00", original)
        return value

    return protected, restore


def _omit_file_uri_references(text: str, *, protect_code_regions: bool = True) -> str:
    if not isinstance(text, str) or not text:
        return text
    restore_code_regions = None
    if protect_code_regions:
        text, restore_code_regions = _stash_share_code_regions(text)
    text, restore_media_tokens = _stash_public_media_tokens(text)
    text = _MARKDOWN_FILE_URI_RE.sub(_PLACEHOLDER, text)
    text = _ANGLE_FILE_URI_RE.sub(_PLACEHOLDER, text)
    text = _FILE_URI_RE.sub(lambda match: f"{match.group(1)}{_PLACEHOLDER}", text)
    text = restore_media_tokens(text)
    if restore_code_regions is not None:
        text = restore_code_regions(text)
    return text


def _check_image_magic(data: bytes, mime_type: str) -> bool:
    """Verify *data* header bytes match the expected magic for *mime_type*.

    Returns ``True`` if the content is consistent with the claimed type.
    SVG is exempt because it is validated structurally by
    :func:`_sanitize_svg_bytes`.
    """
    if mime_type == "image/svg+xml":
        return True
    magic = _IMAGE_MAGIC.get(mime_type)
    if magic is None:
        return False
    if not data.startswith(magic):
        return False
    # Extra check for WebP: "WEBP" at offset 8.
    if mime_type == "image/webp":
        if len(data) < 12 or data[_WEBP_MAGIC_OFFSET:_WEBP_MAGIC_OFFSET + 4] != _WEBP_MAGIC:
            return False
    return True


def _sanitize_svg_bytes(data: bytes) -> bytes:
    """Strip script elements, on* handlers, and javascript: hrefs from SVG.

    SVG images served via ``<img src="data:image/svg+xml;base64,…">`` are
    sandboxed by modern browsers and script execution is blocked.  However,
    a sufficiently determined adversary with an older or exotic client may
    still extract credentials embedded in the SVG, so we strip the unsafe
    content at the server before it ever reaches a share page.

    Returns sanitised SVG bytes on success, or the original *data* unchanged
    if the content cannot be parsed as XML (fail-closed).
    """
    try:
        ET.register_namespace("", _SVG_NS)
        root = ET.fromstring(data.decode("utf-8", errors="replace"))
    except ET.ParseError:
        # Not valid XML — cannot sanitise safely.  Return a minimal empty SVG
        # so the <img> renders nothing rather than embedding un-sanitised bytes.
        return b'<svg xmlns="http://www.w3.org/2000/svg"/>'

    # Walk the tree depth-first, stripping on* attrs, dangerous hrefs,
    # and removing <script> children.
    def _walk(elem: ET.Element) -> None:
        for attr_name in list(elem.attrib):
            if _ON_ATTR_RE.match(attr_name):
                del elem.attrib[attr_name]
            elif attr_name in ("href", "xlink:href", "{http://www.w3.org/1999/xlink}href"):
                val = elem.attrib[attr_name]
                if _DANGEROUS_HREF_RE.match(val):
                    del elem.attrib[attr_name]

        for child in list(elem):
            tag = child.tag.split("}", 1)[-1] if "}" in child.tag else child.tag
            if tag == "script":
                elem.remove(child)
            else:
                _walk(child)

    _walk(root)

    buf = io.BytesIO()
    tree = ET.ElementTree(root)
    tree.write(buf, encoding="utf-8", xml_declaration=False)
    return buf.getvalue()


def _embed_share_media(text: str, *, allowed_roots: tuple[Path, ...] = ()) -> str:
    """Find local MEDIA: references and replace them with inline <img> tags.

    Only relative paths that resolve inside at least one of *allowed_roots*
    are honoured.  Absolute paths, ``file://`` URIs, paths that traverse
    outside the allowed directories via ``..`` or symlinks, non-image MIME
    types, and files larger than ``_SHARE_EMBED_MAX_BYTES`` are all replaced
    with a static placeholder — no file content leaves the server.

    This runs BEFORE :func:`_redact_share_paths` so the concrete file path
    is still available for the allowed-roots check.
    """
    if not isinstance(text, str) or not text:
        return text

    allowed = tuple(Path(r).resolve() for r in allowed_roots if r)

    def _resolve_against_roots(raw: str) -> Path | None:
        """Resolve *raw* against each allowed root, returning the first valid
        absolute Path that lives inside one of them, or ``None``.

        - ``file://`` is always rejected (absolute, un-scoped).
        - Absolute paths (``/…``, ``~…``) are resolved as-is and checked
          against the allowed-roots allow-list via ``is_relative_to()``.
        - Relative paths are joined with each allowed root in turn so they
          don't silently anchor to the server's process CWD.
        """
        if raw.startswith("file://"):
            return None

        # --- Absolute paths: resolve as-is, then allow-list check ------------
        if raw.startswith("/") or raw.startswith("~"):
            try:
                p = Path(raw).expanduser().resolve(strict=False)
            except (OSError, ValueError, RuntimeError):
                return None
            if not allowed or not any(p.is_relative_to(r) for r in allowed):
                return None
            return p if p.is_file() else None

        # --- Relative paths: try each allowed root as the anchor -------------
        for root in allowed:
            try:
                candidate = (root / raw).resolve(strict=False)
            except (OSError, ValueError, RuntimeError):
                continue
            # Path traversal guard: resolved path must still be under the root.
            if not candidate.is_relative_to(root):
                continue
            if candidate.is_file():
                return candidate
        return None

    def _replace_ref(token: str, raw_ref: str) -> str:
        raw = (raw_ref or "").strip()
        if not raw:
            return token

        if _is_public_media_reference(raw):
            return token

        if (
            raw.startswith("[")
            or raw.startswith("]")
            or re.match(r"^(?:https?://|data:)", raw, re.IGNORECASE)
        ):
            return _PLACEHOLDER

        # --- Resolve and validate against allowed roots -----------------------
        p = _resolve_against_roots(raw)
        if p is None:
            return _PLACEHOLDER

        # --- Size guard -------------------------------------------------------
        try:
            size = p.stat().st_size
        except OSError:
            return _PLACEHOLDER

        if size > _SHARE_EMBED_MAX_BYTES:
            return _PLACEHOLDER

        # --- MIME allow-list (images only) ------------------------------------
        mime_type, _ = mimetypes.guess_type(str(p))
        if not mime_type or mime_type not in _SHARE_ALLOWED_MIME_TYPES:
            return _PLACEHOLDER

        # --- Embed as base64 <img> -------------------------------------------
        try:
            data = p.read_bytes()
            # Content-based MIME validation: verify the actual file header
            # matches the claimed MIME type — catches extension-spoofed files
            # (e.g. a script renamed to .png).
            if not _check_image_magic(data, mime_type):
                return _PLACEHOLDER
            # Sanitise SVG content before embedding — SVG can carry
            # <script> elements and on* event handlers that could leak
            # credentials in the context of a public share page.
            if mime_type == "image/svg+xml":
                data = _sanitize_svg_bytes(data)
            b64 = base64.b64encode(data).decode("ascii")
            # HTML-escape the filename so a crafted name like
            # '"><script>alert(1)</script>' cannot break out of the
            # attribute and inject script into the share page.
            safe_name = html.escape(p.name, quote=True)
            return (
                f'<img src="data:{mime_type};base64,{b64}"'
                f' class="msg-media-img" alt="{safe_name}"'
                f' loading="lazy">'
            )
        except (OSError, MemoryError):
            return _PLACEHOLDER

    return _replace_media_tokens(text, _replace_ref)


def _sanitize_message(message: dict, *, redact_paths=(), allowed_roots: tuple[Path, ...] = ()) -> dict | None:
    if not isinstance(message, dict):
        return None
    role = str(message.get("role") or "").strip().lower()
    if role not in {"user", "assistant"}:
        return None
    text = _share_message_text(message)
    if not text:
        return None
    # ALWAYS-ON hardening for the public boundary, independent of any setting:
    # (1) force credential redaction, (2) embed allowed local media,
    # (3) strip known local paths.
    text = _force_redact_credentials(text)
    # Embed local media BEFORE path redaction so the concrete path is still
    # available for file reads.  MEDIA: references become self-contained data
    # URIs — or a static placeholder if the path is outside the allowed roots.
    text = _embed_share_media(text, allowed_roots=allowed_roots)
    text = _omit_file_uri_references(text)
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
    # Collect allowed roots for _embed_share_media: only files inside the
    # session workspace or the attachments root may be embedded.  This is
    # the hard security boundary that prevents arbitrary file reads through
    # crafted MEDIA: references in message text.
    _allowed_roots: list[Path] = []
    _ws = raw_dict.get("workspace")
    if _ws and isinstance(_ws, str) and _ws.strip():
        _allowed_roots.append(Path(_ws.strip()))
    try:
        from api.upload import _attachment_root
        _allowed_roots.append(_attachment_root())
    except Exception:
        pass
    _allowed_roots_tuple: tuple[Path, ...] = tuple(_allowed_roots)
    safe_messages = []
    for raw in safe_session.get("messages") or []:
        sanitized = _sanitize_message(
            raw, redact_paths=redact_paths, allowed_roots=_allowed_roots_tuple,
        )
        if sanitized:
            safe_messages.append(sanitized)
    if not safe_messages:
        raise ValueError("This conversation has no shareable messages yet.")
    # Only accept a genuine string title — a dict-valued title (possible via
    # /api/session/import) must not be str()'d into the public snapshot.
    _raw_title = safe_session.get("title")
    _raw_title = _raw_title if isinstance(_raw_title, str) else "Untitled"
    title = _force_redact_credentials(_raw_title or "Untitled")
    title = _embed_share_media(title, allowed_roots=())
    title = _omit_file_uri_references(title, protect_code_regions=False)
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
