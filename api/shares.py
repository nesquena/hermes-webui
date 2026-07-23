"""
Hermes Web UI -- public read-only share snapshots.

Stores a sanitized, immutable snapshot of a conversation under STATE_DIR/shares.
The snapshot is intentionally narrower than a full session export so public
links do not leak local workspace paths, profile details, or raw tool payloads.
"""

from __future__ import annotations

import base64
import html
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from api.config import STATE_DIR
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


def _strip_media_references(text: str) -> str:
    """Replace local MEDIA: tokens and file:// URLs with inert placeholders.

    Public shares must not emit links to the authenticated /api/media endpoint.
    MEDIA:<path> sentinel values and file:// URLs that renderMd() would
    convert into /api/media?path=... URLs are replaced with a non-clickable
    placeholder so anonymous recipients never see broken auth-gated media links.

    Covers every renderer-recognized file:// form (issue #6285 review):
      - Bare file:// URLs (whitespace-delimited)
      - Markdown links: [label](file://...)
      - Markdown images: ![alt](file://...)
      - file:path (no slashes), file:/path (single slash)
      - file://localhost/path, file://127.0.0.1/path
      - URL-encoded file:// variants (e.g. file://%2Ftmp%2Ffile)
    while preserving fenced and inline-code regions byte-for-byte (the
    renderer keeps file:// inert inside code/preformatted content).

    Process order matches the real renderer's (ui.js) pipeline:
      1. CRLF normalisation (renderer normalises before any parsing)
      2. MEDIA: replacement (renderer converts MEDIA: to media tokens
         before fenced/inline code processing, so MEDIA: inside code
         regions is also rendered — matching that here means MEDIA:
         never survives into a public payload)
      3. Fenced code stashing (only complete balanced fences — an
         unmatched opener stays as active prose for sanitisation)
      4. Inline code stashing
      5. file:// URL replacement (bare + markdown forms)
      6. Code restoration
    """
    if not isinstance(text, str) or not text:
        return text

    # (1) Normalise CRLF / bare CR to LF — renderMd() does this first
    # so the close-fence regex does not miss \r\n-terminated lines.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    placeholder = "[Local attachment omitted from public share]"

    # (2) Replace MEDIA: EVERYWHERE before code stashing — the renderer
    # converts MEDIA: to media-stash tokens before fenced/inline code
    # processing, so MEDIA: inside code regions would also be rendered
    # as /api/media links.
    text = re.sub(r"MEDIA:\S+", placeholder, text)

    # (3) Stash fenced code blocks.  Match the renderer's line-anchored
    # variable-length fence grammar exactly.  Only stash a COMPLETE
    # balanced fence — an unmatched opener (no valid closing fence) stays
    # as active prose for sanitisation (review #6285 issue 1).
    # Opening fence: ^[ ]{0,3}(`{3,})([^`]*)$
    # Closing fence: ^[ ]{0,3}(`{3,})[ \t]*$  (length >= opening length)
    _fenced: list[str] = []
    _lines = text.split("\n")
    _fence_parts: list[str] = []
    _i = 0
    while _i < len(_lines):
        _om = re.match(r"^[ ]{0,3}(`{3,})([^`]*)$", _lines[_i])
        if _om:
            _open_len = len(_om.group(1))
            _start = _i
            _i += 1
            _found_close = False
            while _i < len(_lines):
                _cm = re.match(r"^[ ]{0,3}(`{3,})[ \t]*$", _lines[_i])
                if _cm and len(_cm.group(1)) >= _open_len:
                    _i += 1
                    _found_close = True
                    break
                _i += 1
            if _found_close:
                _block = "\n".join(_lines[_start:_i])
                _fenced.append(_block)
                _fence_parts.append(f"\x00F{len(_fenced) - 1}\x00")
            else:
                # No matching close — treat opener and subsequent lines
                # as active prose (matching renderMd() behaviour).
                for _j in range(_start, _i):
                    _fence_parts.append(_lines[_j])
        else:
            _fence_parts.append(_lines[_i])
            _i += 1
    text = "\n".join(_fence_parts)

    # (4) Stash inline code spans (`...`) so file:// inside them is preserved.
    _inline: list[str] = []
    text = re.sub(
        r"`[^`\n]+`",
        lambda m: _inline.append(m.group(0)) or f"\x00I{len(_inline) - 1}\x00",
        text,
    )

    # (5) file:// URL replacement
    # Markdown images: ![alt](file:(?://)?...) → placeholder
    text = re.sub(r"!\[[^\]]*\]\(file:(?://)?[^\s)]+\)", placeholder, text)

    # Markdown links: [label](file:(?://)?...) → placeholder
    text = re.sub(r"\[[^\]]+\]\(file:(?://)?[^\s)]+\)", placeholder, text)

    # Bare file:(?://)? URLs – preserve the leading delimiter instead of
    # consuming whitespace and unconditionally inserting a space (review
    # feedback). The file:(?://)? pattern also catches file:path and
    # file:/path forms in addition to standard file:// and file:/// variants.
    text = re.sub(r"(^|\s)file:(?://)?[^\s<>\"')\]]+", r"\1" + placeholder, text)

    # (6) Restore stashed code regions.
    for i, s in enumerate(_fenced):
        text = text.replace(f"\x00F{i}\x00", s)
    for i, s in enumerate(_inline):
        text = text.replace(f"\x00I{i}\x00", s)

    return text


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


# Regex matching local MEDIA:<path> references — same pattern that
# _inlineMediaHtmlForRef() in ui.js handles when rendering messages.
# Excludes MEDIA: followed by http/https URLs so external images pass
# through unchanged.  file:// references are NOT matched here — they are
# always rejected at the public-share boundary (absolute, un-scoped).
_SHARE_MEDIA_RE = re.compile(
    r"MEDIA:(?!https?://)([^\s\)\]>]+)"
)

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

    def _replace_ref(m: re.Match) -> str:
        raw = (m.group(1) or "").strip()
        if not raw:
            return m.group(0)

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

    return _SHARE_MEDIA_RE.sub(_replace_ref, text)


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
    text = _redact_share_paths(text, redact_paths)
    # Strip MEDIA: / file:// references so the public share never renders
    # links to the authenticated /api/media endpoint (issue #6126).
    text = _strip_media_references(text)
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
    # Sanitize each message on read so legacy snapshots stored before the
    # write-time sanitizer was introduced also have MEDIA:/file:// stripped
    # (issue #6285 review – "close the legacy-snapshot path").
    safe_messages = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            content = _strip_media_references(content)
            if content.strip():
                safe_messages.append({**msg, "content": content})
        else:
            safe_messages.append(msg)
    public = {
        "title": str(payload.get("title") or "Untitled"),
        "messages": safe_messages,
        "message_count": int(payload.get("message_count") or len(safe_messages)),
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
