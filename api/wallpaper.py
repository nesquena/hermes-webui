"""Wallpaper storage and serving.

Stores a single user-uploaded image at STATE_DIR/wallpaper-<hash>.{ext}.
The path is recorded in settings.json under 'wallpaper_file'. Magic-byte
validation rejects anything that isn't a true JPEG/PNG/WebP, regardless
of the client-supplied Content-Type.

Public API:
  save_wallpaper(raw_body) -> {'file': filename}
  delete_wallpaper() -> None
  get_wallpaper_path() -> Path | None
  read_wallpaper() -> (bytes, mime, etag)
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Tuple

from api.config import MAX_WALLPAPER_BYTES, STATE_DIR
from api.config import load_settings, save_settings

logger = logging.getLogger(__name__)


# Magic-byte signatures. WebP requires the offset-8 'WEBP' check;
# 'RIFF' alone matches AVI / WAV containers.
_JPEG_MAGIC = b'\xff\xd8\xff'
_PNG_MAGIC = b'\x89PNG\r\n\x1a\n'

# (extension, mime) per detected format
_FORMATS = {
    'jpeg': ('.jpg', 'image/jpeg'),
    'png': ('.png', 'image/png'),
    'webp': ('.webp', 'image/webp'),
}


def _sniff(raw: bytes) -> str | None:
    """Return 'jpeg' | 'png' | 'webp' | None based on magic bytes.

    Does not trust client Content-Type. Bytes 8..11 == 'WEBP' is required
    for WebP detection (RIFF prefix alone is ambiguous with AVI/WAV).
    """
    if raw[:3] == _JPEG_MAGIC:
        return 'jpeg'
    if raw[:8] == _PNG_MAGIC:
        return 'png'
    if len(raw) >= 12 and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return 'webp'
    return None


def save_wallpaper(raw_body: bytes) -> dict:
    """Validate, hash, and persist a wallpaper. Replaces the previous one.

    Raises ValueError on size cap or magic-byte failure.
    """
    if len(raw_body) > MAX_WALLPAPER_BYTES:
        raise ValueError(
            f'wallpaper file too large (max {MAX_WALLPAPER_BYTES // 1_000_000}MB)'
        )

    fmt = _sniff(raw_body)
    if fmt is None:
        raise ValueError('wallpaper must be JPEG, PNG, or WebP')

    ext, _mime = _FORMATS[fmt]
    digest = hashlib.sha1(raw_body).hexdigest()[:8]
    filename = f'wallpaper-{digest}{ext}'

    # Single-slot replacement: remove any existing wallpaper file(s) first.
    _purge_old_files()

    target = STATE_DIR / filename
    target.write_bytes(raw_body)

    save_settings({'wallpaper_file': filename})

    return {'file': filename}


def delete_wallpaper() -> None:
    """Remove the current wallpaper file and clear settings reference.

    Idempotent: safe to call when no wallpaper is set.
    """
    _purge_old_files()
    save_settings({'wallpaper_file': None})


def get_wallpaper_path() -> Path | None:
    """Return the current wallpaper file path, or None if not set / missing."""
    settings = load_settings()
    name = settings.get('wallpaper_file')
    if not name:
        return None
    path = STATE_DIR / name
    return path if path.exists() else None


def read_wallpaper() -> Tuple[bytes, str, str]:
    """Read the current wallpaper. Returns (bytes, mime, etag).

    Raises FileNotFoundError if no wallpaper is set.
    """
    path = get_wallpaper_path()
    if path is None:
        raise FileNotFoundError('no wallpaper set')

    raw = path.read_bytes()
    fmt = _sniff(raw)
    if fmt is None:
        # Should be impossible if the file got through save_wallpaper, but
        # defend against a manually-edited settings.json pointing at junk.
        raise FileNotFoundError('wallpaper file is not a recognized image')
    _ext, mime = _FORMATS[fmt]

    # The hash in the filename is the etag (filename includes sha1[:8]).
    etag = path.stem.removeprefix('wallpaper-')
    return raw, mime, etag


def _purge_old_files() -> None:
    """Remove any wallpaper-*.{jpg,png,webp} files in STATE_DIR.

    Used on upload (replace old before writing new) and on delete.
    """
    for pattern in ('wallpaper-*.jpg', 'wallpaper-*.png', 'wallpaper-*.webp'):
        for p in STATE_DIR.glob(pattern):
            try:
                p.unlink()
            except OSError as e:
                logger.warning('failed to unlink old wallpaper %s: %s', p, e)
