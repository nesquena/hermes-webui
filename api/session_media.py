"""Private, file-backed storage for large native image content parts.

Keeping a multi-megabyte ``data:image/...;base64`` string in both ``messages``
and ``context_messages`` makes every session read and JSON response pay for the
same bytes again. This module stores only verified raster images as immutable
session attachments and leaves a small, opaque ``webui-media://`` reference in
the transcript. The reference is expanded only at the model-call boundary.
"""
import base64
import binascii
import hashlib
import os
import re
import threading
from pathlib import Path

from api.config import STATE_DIR


_MIN_EXTERNALIZED_BYTES = 64 * 1024
_MEDIA_SCHEME = "webui-media://"
_REF_RE = re.compile(r"^webui-media://([a-f0-9]{64}\.(?:png|jpe?g|gif|webp|bmp))$")
_MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
}
_EXTENSION_MIMES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}


def _attachment_root() -> Path:
    """Mirror the upload inbox root without importing api.upload circularly."""
    override = os.getenv("HERMES_WEBUI_ATTACHMENT_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (STATE_DIR / "attachments").resolve()


def _session_media_dir(session_id: str) -> Path:
    """Return the private artifact directory shared with session uploads."""
    safe_id = re.sub(r"[^\w.\-]", "_", str(session_id or "session"))[:120]
    root = _attachment_root()
    target = (root / safe_id / "session-media").resolve()
    if not target.is_relative_to(root):
        raise ValueError("Invalid session media directory")
    return target


def _is_expected_raster_bytes(mime: str, raw: bytes) -> bool:
    if mime == "image/png":
        return raw.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return raw.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return raw.startswith((b"GIF87a", b"GIF89a"))
    if mime == "image/webp":
        return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    if mime == "image/bmp":
        return raw.startswith(b"BM")
    return False


def _decode_raster_data_uri(url, *, min_bytes: int = 0):
    """Return ``(mime, bytes)`` for a valid raster base64 data URI, else None."""
    if not isinstance(url, str) or not url[:5].lower() == "data:":
        return None
    try:
        header, encoded = url.split(",", 1)
    except ValueError:
        return None
    tokens = header[5:].split(";")
    if not tokens or "base64" not in {token.strip().lower() for token in tokens[1:]}:
        return None
    mime = tokens[0].strip().lower()
    if mime not in _MIME_EXTENSIONS:
        return None
    # Standard base64 is four characters per three input bytes. Avoid decoding
    # a small image only to discover that it stays inline below the threshold.
    if min_bytes and len(encoded) < 4 * ((min_bytes + 2) // 3):
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not _is_expected_raster_bytes(mime, raw):
        return None
    return mime, raw


def _write_media(session_id: str, mime: str, raw: bytes) -> str:
    """Persist immutable media and return its opaque reference URL."""
    digest = hashlib.sha256(raw).hexdigest()
    filename = f"{digest}.{_MIME_EXTENSIONS[mime]}"
    root = _session_media_dir(session_id)
    root.mkdir(parents=True, exist_ok=True)
    target = root / filename
    target_is_verified = False
    try:
        if target.is_file():
            existing = target.read_bytes()
            target_is_verified = (
                hashlib.sha256(existing).hexdigest() == digest
                and _is_expected_raster_bytes(mime, existing)
            )
    except OSError:
        target_is_verified = False
    if not target_is_verified:
        tmp = root / f".{filename}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            # Same digest means same bytes, so replacing a concurrent writer is
            # safe and avoids a partially written stable filename.
            os.replace(tmp, target)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return _MEDIA_SCHEME + filename


def _read_verified_media_reference(session_id: str, filename: str) -> tuple[str, bytes]:
    """Read one reference and verify its type and content-addressed name."""
    match = _REF_RE.fullmatch(_MEDIA_SCHEME + filename)
    if not match:
        raise ValueError("Invalid session media reference")
    root = _session_media_dir(session_id)
    path = (root / match.group(1)).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Invalid session media path")
    raw = path.read_bytes()
    mime = _EXTENSION_MIMES.get(path.suffix.lower().lstrip("."))
    if not mime or not _is_expected_raster_bytes(mime, raw):
        raise ValueError(f"Session media type verification failed for {filename}")
    expected_digest = filename.split(".", 1)[0]
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError(f"Session media digest verification failed for {filename}")
    return mime, raw


def clone_session_media_references(value, source_session_id: str, destination_session_id: str) -> int:
    """Give *destination_session_id* verified ownership of compact references.

    The structured value is not mutated. Callers must invoke this before
    committing copied history under a different session id so every persisted
    ``webui-media://`` reference resolves independently of the source session.
    """
    if source_session_id == destination_session_id:
        return 0
    filenames = set()

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        image = node.get("image_url")
        if node.get("type") == "image_url" and isinstance(image, dict):
            url = image.get("url")
            match = _REF_RE.fullmatch(url) if isinstance(url, str) else None
            if match:
                filenames.add(match.group(1))
                return
        for child in node.values():
            visit(child)

    visit(value)
    for filename in sorted(filenames):
        mime, raw = _read_verified_media_reference(source_session_id, filename)
        cloned_reference = _write_media(destination_session_id, mime, raw)
        if cloned_reference != _MEDIA_SCHEME + filename:
            raise ValueError(f"Session media reference changed while cloning {filename}")
        _read_verified_media_reference(destination_session_id, filename)
    return len(filenames)


def externalize_large_session_media(value, session_id: str, *, min_bytes: int = _MIN_EXTERNALIZED_BYTES) -> int:
    """Replace large structured raster data URLs in *value* in place.

    Only canonical OpenAI-style ``image_url`` content parts are touched. Plain
    text, SVG and malformed/foreign data URLs remain byte-for-byte unchanged.
    Returns the number of parts externalized.
    """
    changed = 0

    def visit(node):
        nonlocal changed
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        image = node.get("image_url")
        if node.get("type") == "image_url" and isinstance(image, dict):
            decoded = _decode_raster_data_uri(image.get("url"), min_bytes=min_bytes)
            if decoded is not None:
                mime, raw = decoded
                if len(raw) >= min_bytes:
                    image["url"] = _write_media(session_id, mime, raw)
                    changed += 1
                    return
        for child in node.values():
            visit(child)

    visit(value)
    return changed


def hydrate_session_media_urls(value, session_id: str):
    """Return a deep copy whose valid private references are data URLs again."""
    import copy

    hydrated = copy.deepcopy(value)
    root = _session_media_dir(session_id)

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        image = node.get("image_url")
        if node.get("type") == "image_url" and isinstance(image, dict):
            url = image.get("url")
            match = _REF_RE.fullmatch(url) if isinstance(url, str) else None
            if match:
                path = (root / match.group(1)).resolve()
                try:
                    if path.is_relative_to(root) and path.is_file():
                        raw = path.read_bytes()
                        mime = _EXTENSION_MIMES.get(path.suffix.lower().lstrip("."))
                        if mime and _is_expected_raster_bytes(mime, raw):
                            image["url"] = "data:%s;base64,%s" % (
                                mime, base64.b64encode(raw).decode("ascii")
                            )
                except OSError:
                    # Preserve a missing/corrupt reference rather than replacing
                    # it with unrelated bytes.
                    pass
                return
        for child in node.values():
            visit(child)

    visit(hydrated)
    return hydrated
