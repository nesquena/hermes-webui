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
from typing import NamedTuple

from api.config import STATE_DIR
from api.helpers import (
    redact_session_data,
    unquote_media_ref,
    is_external_media_url,
    media_token_exceeds_max_length,
)
from api.share_refs import (
    SHARE_REFERENCE_RE,
    media_ref_of_match,
    public_reference_hides_local_target,
    url_of_match,
)
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


# Regex matching EVERY URL-bearing token the share renderer can turn into a
# live URL — the canonical `MEDIA:` token plus Markdown images/links, bare
# absolute URLs, and relative `api/media?…` references. Shape and rationale
# live in api/share_refs.py.
#
# NOTE: the `MEDIA:` branch deliberately does NOT pass exclude_urls=True. That
# option is a negative lookahead at the current start position, so an external
# URL is rejected by NOT MATCHING — and `re.sub` then resumes scanning one
# character later, inside the very token it just refused. For
# `MEDIA:https://cdn.test/img/MEDIA:/etc/passwd.png` the outer token is skipped
# and the nested `MEDIA:` matches as a fresh LOCAL token, so a legitimate
# external URL gets its tail rewritten (and local paths get probed from share
# text). Match the complete canonical token here and classify it in
# _replace_ref(): match -> classify -> decide, which cannot restart mid-token.
# Every other branch obeys the same rule, so one refusal always consumes its
# whole token.
_SHARE_MEDIA_RE = SHARE_REFERENCE_RE

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

# ── The three outcomes of ONE reference decision ─────────────────────────────
# Named so the pure classifier and its two callers cannot disagree about what a
# verdict means. `_EMBED` is the only outcome that needs the filesystem, and it
# is reachable ONLY from the create path — the load path treats it as a refusal.
_PRESERVE = "preserve"
_REFUSE = "refuse"
_EMBED = "embed"


class _RefVerdict(NamedTuple):
    """What to do with one matched reference.

    ``action`` is one of :data:`_PRESERVE`, :data:`_REFUSE`, or :data:`_EMBED`.
    ``local_path`` carries the unquoted local ref for :data:`_EMBED` and is
    empty otherwise, so an ``_EMBED`` verdict is the only shape that can name a
    file at all.
    """

    action: str
    local_path: str = ""


_PRESERVE_VERDICT = _RefVerdict(_PRESERVE)
_REFUSE_VERDICT = _RefVerdict(_REFUSE)

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


def _classify_share_ref(match: re.Match) -> _RefVerdict:
    """Decide ONE matched token, WITHOUT touching the filesystem.

    This is the pure half of the publication decision. It answers with
    :data:`_PRESERVE` (publish the span byte-for-byte), :data:`_REFUSE`
    (replace the whole span with :data:`_PLACEHOLDER`), or :data:`_EMBED`
    (a canonical ``MEDIA:`` token naming a LOCAL path, which only the create
    path may resolve and inline).

    Split out of :func:`_embed_share_media` so the LOAD path can reuse the
    identical decision. ``load_share()`` must apply the same guard to a stored
    snapshot written by an older build, but it must never read a local file to
    do it: a public read of an anonymous share is not a licence to touch the
    host filesystem, and the concrete path in a legacy snapshot is untrusted
    input. Structural separation is stronger than calling the embedder with
    empty ``allowed_roots`` — an absolute ref still reaches ``Path.resolve()``
    and ``is_file()`` there, so "no roots" is not "no filesystem access".
    """
    # Non-`MEDIA:` branches: a Markdown image/link target, a bare absolute
    # URL, or a relative reference to our own media route. Each of these is
    # a live-URL sink on the share page (see api/share_refs.py for the sink
    # inventory), so ONE fail-closed decision covers the WHOLE matched span
    # here, exactly as the `MEDIA:` branch below does. Never embedded: the
    # allowed-roots embed path exists for canonical `MEDIA:` tokens, and a
    # Markdown target that survives is preserved byte-for-byte instead.
    if match.group("media") is None:
        if public_reference_hides_local_target(url_of_match(match)):
            return _REFUSE_VERDICT
        return _PRESERVE_VERDICT

    # Quoted refs are captured with their quotes so the replaced span covers
    # the whole token; strip them before any path resolution, or a spaced
    # path that the renderer displays would be placeholdered here.
    capture = media_ref_of_match(match)
    raw = unquote_media_ref(capture)
    if not raw:
        return _PRESERVE_VERDICT
    # An over-ceiling capture is not a legal MEDIA token (shared lexical
    # contract; see MEDIA_TOKEN_MAX_LENGTH in api/helpers.py). Neither
    # renderer turns it into a media node, so the snapshot must leave the
    # span exactly as prose rather than embed or placeholder it — otherwise
    # the share and the live view disagree about one token.
    if media_token_exceeds_max_length(capture):
        return _PRESERVE_VERDICT
    # Classification happens HERE, on a token that was actually matched, and
    # is the only thing that decides external vs local. The pattern no longer
    # rejects external URLs by non-match, so the scanner can never resume
    # inside a refused token and rewrite the tail of an external URL that
    # itself contains `MEDIA:`. Preserving returns the exact original span, so
    # an exempt external token survives byte-for-byte.
    #
    # But "carries an http(s) scheme" is NOT the same as "safe to publish".
    # The share renderer restores a preserved token into an image URL, so a
    # URL whose own path/query/fragment names a local file, our
    # authenticated /api/media route, or a loopback/private host would
    # either round-trip a host path into the anonymous snapshot or make the
    # viewer's browser issue a same-origin authenticated request. Decide the
    # WHOLE token: preserve it byte-for-byte, or placeholder all of it. A
    # partial rewrite is exactly the bug that let the scanner resume inside
    # a token it had refused.
    if is_external_media_url(raw):
        if public_reference_hides_local_target(raw):
            return _REFUSE_VERDICT
        return _PRESERVE_VERDICT
    return _RefVerdict(_EMBED, raw)


def guard_public_share_references(text: str) -> str:
    """Apply the public-reference decision to *text* with NO filesystem access.

    The guard :func:`_embed_share_media` applies runs when a snapshot is
    CREATED. ``load_share()`` used to return a stored snapshot's ``messages``
    untouched, so every share written before that guard existed stayed on the
    vulnerable path forever: ``static/share.js`` hands each stored
    ``msg.content`` straight to ``renderMd()`` and assigns the result with
    ``innerHTML``, and the live sinks there (``_markdownHref()`` turning
    ``file://`` into ``api/media?path=…&inline=1``; ``_isSafeUrl()``/``_tag()``
    accepting a relative ``api/`` image or link target) are still reachable.

    So the same decision runs on the way OUT. Every token
    :data:`SHARE_REFERENCE_RE` matches is either preserved byte-for-byte or
    replaced whole with :data:`_PLACEHOLDER`. A local ``MEDIA:`` ref is
    placeholdered rather than embedded: reading it would need the filesystem,
    the allowed roots of the originating session are not available at load
    time, and the path itself is untrusted input from a stored file. Already
    guarded snapshots pass through unchanged, because the decision is
    idempotent on its own output.
    """
    if not isinstance(text, str) or not text:
        return text

    def _decide(match: re.Match) -> str:
        verdict = _classify_share_ref(match)
        return match.group(0) if verdict.action == _PRESERVE else _PLACEHOLDER

    return _SHARE_MEDIA_RE.sub(_decide, text)


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

    def _embed_local_ref(raw: str) -> str:
        """The effectful half: resolve *raw*, validate it, inline its bytes."""
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

    def _replace_ref(m: re.Match) -> str:
        verdict = _classify_share_ref(m)
        if verdict.action == _PRESERVE:
            return m.group(0)
        if verdict.action == _REFUSE:
            return _PLACEHOLDER
        return _embed_local_ref(verdict.local_path)

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


def _guarded_public_message(message) -> dict | None:
    """Return *message* with its stored content re-decided for publication.

    A stored snapshot's ``content`` is untrusted text: it may predate the
    public-reference guard entirely. Every message therefore passes through
    :func:`guard_public_share_references` on the way out, and a message whose
    shape is not ``{role, content: str}`` is dropped rather than published.
    """
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    guarded = guard_public_share_references(content)
    return {**message, "content": guarded}


def _public_share_payload(payload: dict) -> dict:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        messages = []
    # The stored snapshot is re-guarded on EVERY read, so a share written before
    # the public-reference guard existed cannot keep serving a live local
    # reference to an anonymous viewer. No filesystem access happens here — see
    # guard_public_share_references().
    guarded = [m for m in map(_guarded_public_message, messages) if m is not None]
    public = {
        "title": str(payload.get("title") or "Untitled"),
        "messages": guarded,
        "message_count": int(payload.get("message_count") or len(guarded)),
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
