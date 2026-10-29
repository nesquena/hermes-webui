"""
Hermes Web UI -- public read-only share snapshots.

Stores a sanitized, immutable snapshot of a conversation under STATE_DIR/shares.
The snapshot is intentionally narrower than a full session export so public
links do not leak local workspace paths, profile details, or raw tool payloads.
"""

from __future__ import annotations

import base64
import errno
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
from api.helpers import _json_response_body, redact_session_data, split_media_token_ref
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
    body = _json_response_body(payload, pretty=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f"{path.stem}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
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


# Regex matching local MEDIA:<path> references — same pattern that
# _inlineMediaHtmlForRef() in ui.js handles when rendering messages.
# Excludes MEDIA: followed by http/https URLs so external images pass
# through unchanged.  file:// references are NOT matched here — they are
# always rejected at the public-share boundary (absolute, un-scoped).
# `data:` URIs DO match, and _replace_ref() always replaces them with
# _INLINE_DATA_PLACEHOLDER without touching the filesystem: feeding a multi-KB
# base64 blob to Path(...).resolve()/stat() raised OSError ENAMETOOLONG (errno
# 36) and 500'd share creation (#7949). _sanitize_message() already removes
# them in _placeholder_inline_data_uris(); the branch keeps direct callers safe.
_SHARE_MEDIA_RE = re.compile(
    r"MEDIA:(?!https?://)([^\s\)\]>]+)"
)

# Content-supplied inline data: URIs are never published in a public share.
# Their bytes are opaque to the credential redactor (base64 or %-encoding), and
# an image that renders hides whatever follows a valid header, so a public
# share replaces every one of them with this placeholder. Only the server-side
# embed of verified local files (_embed_share_media) may add data: URIs, and
# it runs after the placeholder pass.
_INLINE_DATA_PLACEHOLDER = "[*Inline attachment omitted from public share*]"
# Catch-all: a "data:" that starts a URI (followed by a type/subtype, ";" or
# ",") is removed through the end of its token, the first whitespace, angle
# bracket, paren, square bracket or backtick. Parameters are not parsed, so
# colon-bearing and quoted parameter forms are covered too, and each match
# consumes its whole token, which keeps the scan linear. Prose such as
# "data: 5 rows", "metadata:" or "data:key,value" is left alone.
_CONTENT_DATA_URI_RE = re.compile(
    r"\bdata:(?=[a-z0-9!#$&^_.+-]+/|[;,])[^\s<>()\[\]`]*",
    re.IGNORECASE,
)
# A whole markdown image whose destination is a data: URI, including the
# <angle-bracket> form and an optional title. The alt text stops at the next
# "![" and the destination at an unbalanced paren or newline, which keeps the
# scan linear; anything this misses is still caught by the passes below.
# "(?:<\s*)?" rather than "<?\s*": two adjacent optional whitespace runs
# backtrack quadratically on a long whitespace run.
_CONTENT_MD_DATA_IMAGE_RE = re.compile(
    r"!\[(?:[^\]!]|!(?!\[))*\]\(\s*(?:<\s*)?data:(?:[^()\n]|\([^()\n]*\))*\)",
    re.IGNORECASE,
)
# The share renderer's own image rule (renderMd matches ![alt](data:image/...)
# up to the first ")" across newlines, and shows a 64-character stub when the
# URI fails its strict check). Applied only to the text up to the last ")" so
# every opener finds a closer, which keeps it linear.
_CONTENT_MD_DATA_IMAGE_LOOSE_RE = re.compile(
    r"!\[(?:[^\]!]|!(?!\[))*\]\(data:image/[^)]*\)",
    re.IGNORECASE,
)
# A raw or entity-escaped <img ...> tag (renderMd decodes &lt;/&gt; first).
# The body stops only at ">" / "&gt;", exactly like renderMd's own tag
# recognizers (<img\b[^>]*> and <\/?[a-z][^>]*>), so a "<" or "&lt;" inside an
# attribute value cannot hide a tag from this pass that the renderer will
# still consume (and drop, together with any payload left in its attributes).
_CONTENT_IMG_TAG_RE = re.compile(
    r"(?:<|&lt;)\s*img\b(?:[^>&]|&(?!gt;))*(?:>|&gt;)",
    re.IGNORECASE,
)
# Quoted values of attributes that only ever hold text. An <img> is replaced
# when it contains a URI-shaped data: anywhere, or any "data:" outside these
# values, so an https image whose alt text says "metadata:" or "data: 5 rows"
# keeps rendering while every other data: still fails closed.
_IMG_TEXT_ATTR_RE = re.compile(
    r"(?<![\w:-])(?:alt|title|aria-label|aria-description)\s*=\s*(?:\"[^\"]*\"|'[^']*')",
    re.IGNORECASE,
)
_DATA_SCHEME_RE = re.compile(r"data:", re.IGNORECASE)
# A "data:" that starts a URI even inside a text attribute. Whitespace after
# the scheme is allowed because the browser's data: URL processor strips it.
# Used to replace the whole tag, so a newline-split URI cannot leave its
# payload behind in an alt or title value.
_DATA_URI_START_RE = re.compile(
    r"\bdata:\s*(?=[a-z0-9!#$&^_.+-]+/|[;,])", re.IGNORECASE
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


def _placeholder_inline_data_uris(text: str) -> str:
    """Replace every content-supplied inline ``data:`` URI with a placeholder.

    Covers markdown images (inline, ``<angle>``, titled), raw and
    entity-escaped ``<img>`` tags, ``MEDIA:data:`` tokens, and, as a final
    catch-all, a ``data:`` URI anywhere else (reference definitions, links,
    code, prose), whatever its declared type or letter case. Whole constructs
    are replaced first so the placeholder reads cleanly; the catch-all
    guarantees no data: URI survives. Never decodes or re-encodes a payload.

    Must run BEFORE :func:`_embed_share_media`, whose verified local-file
    ``<img src="data:...">`` output is the only data: URI a share may contain.
    """
    if not isinstance(text, str) or not text or not _DATA_SCHEME_RE.search(text):
        return text

    def _img_tag(m: re.Match) -> str:
        tag = m.group(0)
        if _DATA_URI_START_RE.search(tag):
            return _INLINE_DATA_PLACEHOLDER
        rest = _IMG_TEXT_ATTR_RE.sub("", tag)
        return _INLINE_DATA_PLACEHOLDER if _DATA_SCHEME_RE.search(rest) else tag

    def _media_ref(m: re.Match) -> str:
        parts = split_media_token_ref(m.string, m)
        if not parts or parts[0].strip()[:5].lower() != "data:":
            return m.group(0)
        return _INLINE_DATA_PLACEHOLDER + parts[1]

    # Applied only up to the last ">" or "&gt;" so every opener finds a
    # closer, which keeps the scan linear on unclosed "<img" runs. "&gt;"
    # contains no ">" character, so both are needed.
    entity_end = text.rfind("&gt;")
    cut = max(text.rfind(">") + 1, entity_end + 4 if entity_end >= 0 else 0)
    if cut:
        text = _CONTENT_IMG_TAG_RE.sub(_img_tag, text[:cut]) + text[cut:]
    text = _CONTENT_MD_DATA_IMAGE_RE.sub(_INLINE_DATA_PLACEHOLDER, text)
    cut = text.rfind(")") + 1
    if cut:
        text = (
            _CONTENT_MD_DATA_IMAGE_LOOSE_RE.sub(_INLINE_DATA_PLACEHOLDER, text[:cut])
            + text[cut:]
        )
    text = _SHARE_MEDIA_RE.sub(_media_ref, text)
    return _CONTENT_DATA_URI_RE.sub(_INLINE_DATA_PLACEHOLDER, text)


def _embed_share_media(text: str, *, allowed_roots: tuple[Path, ...] = ()) -> str:
    """Find local MEDIA: references and replace them with inline <img> tags.

    Only paths that name a regular file inside at least one of
    *allowed_roots* are honoured.  Absolute paths outside the roots,
    ``file://`` URIs, ``..`` components, ANY symlinked component below a
    root, non-image MIME types, and files larger than
    ``_SHARE_EMBED_MAX_BYTES`` are all replaced with a static placeholder —
    no file content leaves the server.

    On POSIX the file is opened by walking from ``/`` through the resolved
    allowed root and on to the file one component at a time with
    ``O_NOFOLLOW``; its type, size and bytes all come from that SAME
    descriptor, so a concurrent writer inside a root cannot swap a directory
    or the file between validation and read (#6126).  On Windows the file is
    opened once and the OPENED handle's final path must lie inside the
    resolved root before anything is read from it.  Anything unverifiable
    fails closed.

    This runs BEFORE :func:`_redact_share_paths` so the concrete file path
    is still available for the allowed-roots check.
    """
    if not isinstance(text, str) or not text:
        return text

    windows = _share_platform_is_windows()
    configured = tuple(r for r in allowed_roots if r)
    allowed = tuple(Path(r).resolve() for r in configured)
    root_spellings = _share_root_spellings(configured, allowed, windows=windows)

    def _replace_ref(m: re.Match) -> str:
        parts = split_media_token_ref(text, m)
        if not parts:
            return m.group(0)
        raw, suffix = parts
        raw = raw.strip()
        if not raw:
            return m.group(0)

        # --- Inline data: URI — never embedded, never resolved as a path ------
        if raw[:5].lower() == "data:":
            return _INLINE_DATA_PLACEHOLDER + suffix

        # --- Resolve and validate against allowed roots -----------------------
        candidates = _share_ref_candidates(raw, allowed, root_spellings, windows=windows)
        if not candidates:
            return _PLACEHOLDER + suffix
        first_root, first_names = candidates[0]
        name = first_names[-1]

        # --- MIME allow-list (images only) ------------------------------------
        mime_type, _ = mimetypes.guess_type(os.path.join(str(first_root), *first_names))
        if not mime_type or mime_type not in _SHARE_ALLOWED_MIME_TYPES:
            return _PLACEHOLDER + suffix

        # --- Size guard + bounded read from ONE verified descriptor (#6126) ---
        data = _read_share_media(candidates, _SHARE_EMBED_MAX_BYTES, windows=windows)
        if data is None:
            return _PLACEHOLDER + suffix

        # --- Embed as base64 <img> -------------------------------------------
        try:
            # Content-based MIME validation: verify the actual file header
            # matches the claimed MIME type — catches extension-spoofed files
            # (e.g. a script renamed to .png).
            if not _check_image_magic(data, mime_type):
                return _PLACEHOLDER + suffix
            # Sanitise SVG content before embedding — SVG can carry
            # <script> elements and on* event handlers that could leak
            # credentials in the context of a public share page.
            if mime_type == "image/svg+xml":
                data = _sanitize_svg_bytes(data)
            b64 = base64.b64encode(data).decode("ascii")
            # HTML-escape the filename so a crafted name like
            # '"><script>alert(1)</script>' cannot break out of the
            # attribute and inject script into the share page.
            safe_name = html.escape(name, quote=True)
            return (
                f'<img src="data:{mime_type};base64,{b64}"'
                f' class="msg-media-img" alt="{safe_name}"'
                f' loading="lazy">'
            ) + suffix
        except (OSError, MemoryError):
            return _PLACEHOLDER + suffix

    return _SHARE_MEDIA_RE.sub(_replace_ref, text)


def _share_platform_is_windows() -> bool:
    """True on native Windows, where shares use the verify-after-open reader."""
    return os.name == "nt"


def _share_root_spellings(configured, allowed, *, windows: bool = False) -> list[tuple[Path, list[str]]]:
    """Lexical spellings of each allowed root as ``(resolved root, components)``.

    The resolved spelling and the configured absolute spelling both map onto
    the resolved root, so an absolute ref written against either works.  The
    root itself is trusted configuration; only names BELOW it are
    attacker-influenced, and those are never followed.  On Windows the
    components are ``normcase``-d (matching is case-insensitive, as before)
    and a ``\\\\?\\`` spelling is also accepted without its prefix.
    """
    import ntpath

    abspath = ntpath.abspath if windows else os.path.abspath
    spellings: list[tuple[Path, list[str]]] = []
    for conf, root in zip(configured, allowed, strict=True):
        texts = [str(root), abspath(str(conf))]
        if windows:
            texts += [plain for plain in map(_share_win_plain_path, texts) if plain]
        for text in texts:
            parts = _share_path_components(text, windows=windows)
            if parts is None or (windows and not ntpath.splitdrive(text)[0]):
                continue  # never widen a root to "/" on an odd spelling
            if windows:
                parts = [ntpath.normcase(part) for part in parts]
            if (root, parts) not in spellings:
                spellings.append((root, parts))
    return spellings


def _share_ref_candidates(raw: str, allowed, root_spellings, *, windows: bool = False) -> list[tuple[Path, list[str]]]:
    """Map *raw* to ``(allowed root, component names)`` pairs, lexically.

    No filesystem access happens here; the file is opened by
    :func:`_read_share_media`.

    - ``file://`` is always rejected (absolute, un-scoped).
    - Absolute paths (``/…``, ``~…``; on Windows also ``C:\\…`` and UNC)
      must sit lexically under a root spelling, by whole components.
    - Relative paths are anchored at each allowed root in turn so they
      don't silently anchor to the server's process CWD.
    - ``..`` refuses the whole ref; empty and ``.`` components are dropped.
    """
    import ntpath

    if raw.startswith("file://"):
        return []

    # Defensive length / newline guard (#7949). A real local attachment
    # path is short; an over-long or newline-bearing token (e.g. a
    # data:...;base64,<blob> URI that slipped past the caller) is never a
    # valid file and must never be handed to the filesystem (an
    # over-length name raises OSError ENAMETOOLONG, errno 36).
    # 4096 comfortably exceeds any real PATH_MAX-bounded attachment path.
    if len(raw) > 4096 or "\n" in raw or "\x00" in raw:
        return []

    if windows:
        absolute = raw[:1] in ("/", "\\", "~") or bool(ntpath.splitdrive(raw)[0])
    else:
        absolute = raw.startswith("/") or raw.startswith("~")

    # --- Absolute paths: lexical allow-list check against root spellings ----
    if absolute:
        try:
            # ``~user`` expansion looks the name up in the password
            # database; a malformed name (e.g. a lone surrogate) raises
            # UnicodeEncodeError, which must become the placeholder, not
            # fail the whole share snapshot (#6126 round 1).
            expanded = (ntpath.expanduser if windows else os.path.expanduser)(raw)
        except (OSError, ValueError, RuntimeError):
            return []
        if windows:
            if expanded.replace("/", "\\").startswith(("\\\\?\\", "\\\\.\\")):
                # ``\\?\C:\…`` / ``\\?\UNC\…`` -> plain; device namespaces -> refuse.
                expanded = _share_win_plain_path(expanded) or ""
            drive, rest = ntpath.splitdrive(expanded)
            anchored = bool(drive) and rest[:1] in ("/", "\\")
        else:
            anchored = os.path.isabs(expanded)
        parts = _share_path_components(expanded, windows=windows) if anchored else None
        if not parts:
            return []
        key = [ntpath.normcase(part) for part in parts] if windows else parts
        candidates = []
        for root, root_parts in root_spellings:
            depth = len(root_parts)
            if len(parts) > depth and key[:depth] == root_parts:
                candidates.append((root, parts[depth:]))
        return candidates

    # --- Relative paths: try each allowed root as the anchor -----------------
    parts = _share_path_components(raw, windows=windows)
    if not parts:
        return []
    return [(root, parts) for root in allowed]


def _share_path_components(path: str, *, windows: bool = False) -> list[str] | None:
    """Split a path into plain names for a descriptor walk.

    Empty and ``.`` components are dropped.  Any ``..`` refuses the whole
    path (``None``): it is never resolved, lexically or by the kernel (#6126).
    On Windows both separators split and a drive (``C:``) or UNC share
    (``\\\\server\\share``) is kept as the first component.
    """
    if windows:
        import ntpath

        drive, rest = ntpath.splitdrive(path)
        names = re.split(r"[\\/]", rest)
    else:
        drive, names = "", path.split("/")
    parts = [part for part in names if part not in ("", ".")]
    if ".." in parts:
        return None
    return [drive, *parts] if drive else parts


def _share_nofollow_open_supported() -> bool:
    """True when a rooted, descriptor-relative, no-follow open is available.

    The public-share embedder refuses every local file when this is False
    instead of falling back to a racy pathname read (#6126).  Evaluated per
    call (it is cheap) so it always reflects the live ``os`` module.
    """
    return (
        hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and os.open in getattr(os, "supports_dir_fd", ())
    )


# Open errors that mean "nothing usable is there" and may fall through to the
# next allowed root: missing, not a directory, a symlink refused by O_NOFOLLOW
# (ELOOP; EMLINK on FreeBSD), or a name this filesystem cannot hold
# (ENAMETOOLONG, e.g. eCryptfs encrypted names; #6126 round 4).  Anything else (EMFILE, ENFILE, EIO,
# ESTALE, ...) means something may be there that we failed to read, so it is
# terminal: falling through would substitute another root's namesake
# (#6126 round 3).
_SHARE_MISS_ERRNOS = frozenset(
    e
    for e in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENAMETOOLONG, getattr(errno, "EMLINK", None))
    if e is not None
)


class _ShareMediaRefused(Exception):
    """Terminal refusal of a share media ref: emit the placeholder and do NOT
    try the ref under another allowed root (#6126 round 1)."""


def _share_dir_open_flags() -> int:
    """Open flags for the directory descriptors of the no-follow walk.

    On Linux these are ``O_PATH`` descriptors: they need only SEARCH
    permission on the parent, exactly like a pathname lookup, so an image
    beneath an execute-only (``0111``) directory or root still embeds.  They
    can only serve as ``dir_fd`` anchors; the final file is still opened with
    read flags.  Elsewhere (no ``O_PATH``) the directory must be readable
    (#6126 round 1).
    """
    import sys

    flags = os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    if sys.platform.startswith("linux") and hasattr(os, "O_PATH"):
        return flags | os.O_PATH
    # macOS defines O_SEARCH (search-only directory open); prefer it so an
    # execute-only directory keeps working without read permission (#6126 r2).
    o_search = getattr(os, "O_SEARCH", 0)
    if o_search:
        return flags | o_search
    return flags | os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)


def _share_openat_dir(dir_fd: int, name: str, dir_flags: int) -> int:
    """Open directory *name* relative to *dir_fd* and return the new descriptor.

    *dir_fd* is closed only once the new descriptor exists, so on an exception
    the caller still owns (and closes) exactly one descriptor.
    """
    next_fd = os.open(name, dir_flags, dir_fd=dir_fd)
    try:
        os.close(dir_fd)
    except OSError:
        pass
    return next_fd


def _open_share_file_nofollow(root: Path, names: list[str], dir_flags: int, file_flags: int) -> int | None:
    """Open ``root/names...`` without following any symlink at or below *root*.

    *root* is the already-resolved (realpath) allowed root.  It is NOT opened
    by pathname: the walk starts at ``/`` and opens every component of the
    resolved root relative to the previous directory descriptor with
    ``O_NOFOLLOW``, so an ancestor of a nested root (an attachments root inside
    a writable workspace) swapped for a symlink after resolution fails the
    walk instead of redirecting the root descriptor (#6126 round 1).  A root
    configured through a symlink still works because its realpath has none.
    The names below the root are then walked the same way.  Returns the final
    descriptor (caller closes it) or ``None``.
    """
    root_text = str(root)
    root_names = _share_path_components(root_text)
    if root_names is None or not root_text.startswith("/"):
        return None
    try:
        dir_fd = os.open("/", dir_flags)
    except PermissionError:
        return None
    except OSError as exc:
        if exc.errno in _SHARE_MISS_ERRNOS:
            return None
        raise _ShareMediaRefused from None  # EMFILE, EIO, ...: terminal (#6126 r4)
    except ValueError:
        return None
    try:
        try:
            for name in root_names:
                dir_fd = _share_openat_dir(dir_fd, name, dir_flags)
            # An O_PATH/O_SEARCH descriptor opens even a root we cannot SEARCH
            # (mode 000).  Probe "." through the pinned root so an unsearchable
            # root stays a miss that falls through to the next allowed root, as
            # pathname reads did; denials BELOW a searchable root remain
            # terminal (#6126 r2).
            probe_fd = os.open(".", dir_flags, dir_fd=dir_fd)
        except PermissionError:
            return None  # root unreachable or unsearchable: a miss
        except OSError as exc:
            if exc.errno in _SHARE_MISS_ERRNOS:
                return None  # root missing or redirected: a miss
            raise _ShareMediaRefused from None  # EMFILE, EIO, ...: terminal (#6126 r3)
        except ValueError:
            return None
        try:
            os.close(probe_fd)
        except OSError:
            pass  # a close failure is not a lookup result
        try:
            for name in names[:-1]:
                dir_fd = _share_openat_dir(dir_fd, name, dir_flags)
            if os.stat in os.supports_dir_fd and os.stat in os.supports_follow_symlinks:
                # Skip a socket, FIFO, device or symlink without opening it, as
                # master's is_file() probe did: opening a socket fails (ENXIO,
                # EOPNOTSUPP) and opening a device can have side effects.  The
                # no-follow open + fstat below stay authoritative (#6126 r4).
                import stat as _stat

                if not _stat.S_ISREG(os.stat(names[-1], dir_fd=dir_fd, follow_symlinks=False).st_mode):
                    return None
            return os.open(names[-1], file_flags, dir_fd=dir_fd)
        except PermissionError:
            # EACCES/EPERM below the root: something is there but cannot be
            # read.  Terminal, so a namesake in a later root is never
            # substituted for it (#6126 round 1).
            raise _ShareMediaRefused from None
        except OSError as exc:
            if exc.errno in _SHARE_MISS_ERRNOS:
                return None  # missing, or a symlinked component: a miss
            raise _ShareMediaRefused from None  # EMFILE, EIO, ...: terminal (#6126 r3)
        except ValueError:
            return None
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


def _read_share_media_nofollow(candidates, limit: int) -> bytes | None:
    """Return the bytes of the first regular file among *candidates*.

    Each candidate is ``(allowed root, component names)``.  The file is opened
    with :func:`_open_share_file_nofollow`; type and size come from ``fstat``
    on that descriptor and at most ``limit + 1`` bytes are read from it, so a
    file that grows after ``fstat`` is refused rather than embedded (#6126).
    Like the old ``is_file()`` probe, an entry that cannot be opened (missing,
    symlinked component) or is not a regular file falls through to the next
    root.  Permission denied below a root is terminal (never substitute a
    later root's namesake); an unsupported platform, an oversize file or a
    read error also return ``None`` and the caller emits the placeholder.
    """
    if not _share_nofollow_open_supported():
        return None
    import stat as _stat

    common = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    dir_flags = _share_dir_open_flags()
    file_flags = common | getattr(os, "O_NOCTTY", 0)  # O_NONBLOCK: never block on a FIFO
    for root, names in candidates:
        if not names:
            continue
        try:
            fd = _open_share_file_nofollow(root, names, dir_flags, file_flags)
        except _ShareMediaRefused:
            return None
        if fd is None:
            continue
        try:
            st = os.fstat(fd)
            if not _stat.S_ISREG(st.st_mode):
                continue
            return _share_read_fd_capped(fd, st.st_size, limit)
        except (OSError, MemoryError):
            return None
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    return None


def _share_read_fd_capped(fd: int, size: int, limit: int) -> bytes | None:
    """Read a regular file of ``fstat`` size *size* from *fd*, at most *limit* bytes.

    Refuses (``None``) when *size* is over the cap, and reads at most
    ``limit + 1`` bytes from the SAME descriptor so a file that grows after
    ``fstat`` is refused rather than embedded (#6126).
    """
    if size > limit:
        return None
    chunks = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    return data if len(data) <= limit else None


def _read_share_media(candidates, limit: int, *, windows: bool = False) -> bytes | None:
    """Platform switch for the share media reader (#6126)."""
    if windows:
        return _read_share_media_windows(candidates, limit)
    return _read_share_media_nofollow(candidates, limit)


# --- Windows: verify-after-open (#6126 round 1) --------------------------------
#
# Windows has no ``dir_fd``/``O_NOFOLLOW`` walk.  Instead the file is opened ONCE
# (following whatever junctions/symlinks the path holds), and the path of the
# object that was ACTUALLY opened -- ``GetFinalPathNameByHandleW`` on the
# descriptor's handle -- must lie inside the trusted resolved root by whole
# components before ``fstat`` and the capped read happen on that same
# descriptor.  A swap before the open is caught by the final-path check; a swap
# after it cannot change the opened object.  Anything unverifiable (API
# failure, device or volume-GUID namespace, unexpected shape, any exception)
# fails closed to the placeholder.

_SHARE_WIN_DRIVE_RE = re.compile(r"[A-Za-z]:\\")
# DOS device names are devices in every directory (``C:\\ws\\nul.png`` is NUL
# on older Windows); never open them.
_SHARE_WIN_DEVICE_STEM_RE = re.compile(
    r"(?i)(con|prn|aux|nul|clock\$|conin\$|conout\$|(com|lpt)[0-9\u00b9\u00b2\u00b3])"
)


def _share_win_plain_path(path: str) -> str | None:
    """Return a Win32 path as a plain ``C:\\…`` or ``\\\\server\\share\\…`` path.

    Strips the ``\\\\?\\`` prefix (``\\\\?\\UNC\\`` maps to ``\\\\``).  Returns
    ``None`` for device (``\\\\.\\``), volume-GUID, ``GLOBALROOT``, NT
    (``\\??\\``), relative, drive-relative or rooted-without-drive paths,
    and for any ``.``/``..`` component: those are unverifiable.
    """
    if not isinstance(path, str) or not path or "\x00" in path:
        return None
    path = path.replace("/", "\\")
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
        if not _SHARE_WIN_DRIVE_RE.match(path):
            return None  # \\?\Volume{…}\, \\?\GLOBALROOT\…: not a DOS path
    if path.startswith("\\\\"):
        server, _, rest = path[2:].partition("\\")
        share = rest.partition("\\")[0]
        if server in ("", ".", "?") or not share:
            return None  # \\.\device, a leftover namespace, or a bare server
    elif not _SHARE_WIN_DRIVE_RE.match(path):
        return None
    if any(part in (".", "..") for part in path.split("\\")):
        return None
    return path


def _share_win_canonical(path: str) -> str | None:
    """``normcase``-d plain form of *path* without a trailing separator, or None."""
    import ntpath

    plain = _share_win_plain_path(path)
    if plain is None:
        return None
    # ntpath.normcase IS os.path.normcase on Windows; named explicitly so the
    # comparison is identical when the Windows branch is unit-tested elsewhere.
    return ntpath.normcase(plain).rstrip("\\")


def _share_win_is_within(final_path: str, trusted_root: str) -> bool:
    """True when *final_path* lies strictly inside canonical *trusted_root*.

    Component boundary: ``C:\\a\\bc\\x.png`` is NOT inside ``C:\\a\\b``.
    """
    final = _share_win_canonical(final_path)
    return bool(final) and final.startswith(trusted_root + "\\") and len(final) > len(trusted_root) + 1


def _share_win_name_ok(name: str) -> bool:
    """A single plain file/dir name: no separator, drive/stream colon or device."""
    if not name or any(ch in name for ch in ("\\", "/", ":")):
        return False
    return not _SHARE_WIN_DEVICE_STEM_RE.fullmatch(name.split(".", 1)[0].rstrip(" "))


def _share_win_final_path_api():
    """``kernel32.GetFinalPathNameByHandleW`` with explicit prototypes."""
    import ctypes
    from ctypes import wintypes

    fn = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    fn.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    fn.restype = wintypes.DWORD
    return fn


def _share_win_final_path(handle) -> str | None:
    """Final normalized DOS path of an open handle, or ``None`` on any failure.

    ``FILE_NAME_NORMALIZED | VOLUME_NAME_DOS`` (flags ``0``): junctions and
    symlinks are resolved and the result usually carries a ``\\\\?\\`` prefix.
    """
    import ctypes

    fn = _share_win_final_path_api()
    size = 512
    for _ in range(3):
        buf = ctypes.create_unicode_buffer(size)
        needed = int(fn(handle, buf, size, 0))
        if needed <= 0:
            return None  # API failure
        if needed < size:
            return buf[:needed]
        if needed > 65536:
            return None
        size = needed + 1  # too small: ``needed`` includes the terminator
    return None


def _read_share_media_windows(candidates, limit: int) -> bytes | None:
    """Windows reader: open once, verify the OPENED file, read the same fd.

    For each ``(resolved root, names)`` candidate the file is opened with
    ``O_RDONLY|O_BINARY``.  A missing file, or an entry an advisory ``stat``
    shows is not a regular file (a directory: ``_wopen`` reports EACCES for
    one), falls through to the next root without being opened; every other
    outcome is final: the handle's final path must be inside the
    root (component boundary, ``normcase`` on both sides), ``fstat`` must
    report a regular file, and then at most ``limit + 1`` bytes are read from
    that descriptor.  Any exception or unverifiable result returns ``None``
    (placeholder); the descriptor is always closed.
    """
    import stat as _stat

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    for root, names in candidates:
        if not names:
            continue
        root_text = str(root)
        trusted = _share_win_canonical(root_text)
        if trusted is None or not all(_share_win_name_ok(name) for name in names):
            return None
        path = root_text.rstrip("\\/") + "\\" + "\\".join(names)
        try:
            # Advisory only: containment, type and size are verified on the
            # OPENED handle below (#6126 r5).
            if not _stat.S_ISREG(os.stat(path).st_mode):
                continue  # a directory or device where the image should be: a miss
            fd = os.open(path, flags)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except Exception:
            return None  # permission denied, invalid name, ...: terminal
        try:
            import msvcrt

            final_path = _share_win_final_path(msvcrt.get_osfhandle(fd))
            if final_path is None or not _share_win_is_within(final_path, trusted):
                return None
            st = os.fstat(fd)
            if not _stat.S_ISREG(st.st_mode):
                continue
            return _share_read_fd_capped(fd, st.st_size, limit)
        except Exception:
            return None
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    return None


def _sanitize_message(message: dict, *, redact_paths=(), allowed_roots: tuple[Path, ...] = ()) -> dict | None:
    if not isinstance(message, dict):
        return None
    role = str(message.get("role") or "").strip().lower()
    if role not in {"user", "assistant"}:
        return None
    # Public boundary: hidden internal rows (delegation_wakeup handoffs) are
    # private model plumbing and must never appear in a share
    # (#quiet-delegation gate review).
    from api.process_event_utils import is_hidden_transcript_row

    if is_hidden_transcript_row(message):
        return None
    text = _share_message_text(message)
    if not text:
        return None
    # ALWAYS-ON hardening for the public boundary, independent of any setting:
    # (1) force credential redaction, (2) placeholder content-supplied inline
    # data: URIs, (3) embed allowed local media, (4) strip known local paths.
    text = _force_redact_credentials(text)
    # The placeholder pass runs on exactly the text the embed receives, so no
    # content-supplied data: URI reaches the snapshot or can pose as embed
    # output, and the embed's own <img src="data:..."> is never rewritten.
    text = _placeholder_inline_data_uris(text)
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
    title = _placeholder_inline_data_uris(title)
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
