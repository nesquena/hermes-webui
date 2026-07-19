"""Extension sidecar proxy authentication (token-v1).

A loopback-sidecar extension runs a stdlib HTTP server on 127.0.0.1:<port>. The
browser reaches it ONLY through core's consent-gated proxy
(``/api/extensions/{id}/sidecar/*``). But the loopback port is reachable by any
local process, and the proxy strips every inbound credential before forwarding
(see ``_extension_sidecar_proxy_request_headers`` in ``api/routes.py``), so the
sidecar cannot tell a proxied request from a direct one.

This module mints a per-extension shared secret that core injects on every
forwarded request (header ``X-Hermes-Sidecar-Token``) and the sidecar validates.
It converts "anyone who can send a loopback TCP packet" (other-UID users, host
containers, sandboxed network-only processes) into "processes that can read the
user's state dir" — the same protection level core's own signing key already has
(``.pbkdf2_key`` / ``.signing_key`` are 0600 files in the same directory).

SCOPE (be honest — this is documented in the contract too): the token does NOT
defend against arbitrary same-UID code. A same-user process can read the token,
read core's signing key, or just run the sidecar's underlying tool directly. No
mechanism available here (token, HMAC, nonce, UDS with 0600) changes that.

Design notes (from the reviewed design doc, §9.2):
  * Per-extension token file under ``STATE_DIR/sidecar-auth/<ext-id>.token``.
  * Unlike ``auth._load_key``, we NEVER return an in-memory-only token: if the
    file cannot be persisted+re-read, we report "unavailable" so core fails
    closed (503) rather than injecting a secret no sidecar can ever read.
  * The token is re-read from disk per request (mtime-cached) so rotation /
    deletion takes effect with no restart, and a cached OLD token can't keep
    validating after the file changes.
"""
from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

from api.config import STATE_DIR

# Extension ids are validated upstream (``_valid_extension_id``); re-assert a
# strict grammar here so this module is safe to call directly and a bad id can
# never escape the token directory.
import re as _re
_EXT_ID_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_TOKEN_DIR_NAME = "sidecar-auth"
_TOKEN_BYTES = 32  # secrets.token_urlsafe(32) -> ~43 url-safe chars

# Per-request read cache: ext_id -> (token, mtime_ns, size). Guarded by _LOCK.
_LOCK = threading.Lock()
_CACHE: Dict[str, Tuple[str, int, int]] = {}


def _token_dir() -> Path:
    return STATE_DIR / _TOKEN_DIR_NAME


def _token_path(ext_id: str) -> Path:
    return _token_dir() / f"{ext_id}.token"


def _valid_ext_id(ext_id: object) -> bool:
    return isinstance(ext_id, str) and bool(_EXT_ID_RE.match(ext_id))


def _read_token_file(path: Path) -> Optional[str]:
    """Return the stripped token text, or None on any problem (fail closed)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    tok = raw.strip()
    return tok or None


def ensure_token(ext_id: str) -> Optional[str]:
    """Get-or-create the per-extension token, returning it ONLY if it is durably
    persisted and re-readable from disk. Returns None if it can't be persisted
    (caller must then treat the sidecar proxy as unavailable / 503) — we never
    hand back an ephemeral token a sidecar could never read.

    Idempotent + atomic: safe to call at startup, on gallery install, and on
    consent grant. Concurrent callers converge on one file.
    """
    if not _valid_ext_id(ext_id):
        return None
    path = _token_path(ext_id)

    existing = _read_token_file(path)
    if existing is not None:
        return existing

    with _LOCK:
        # Re-check under lock — another thread may have just created it.
        existing = _read_token_file(path)
        if existing is not None:
            return existing
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        tmp: Optional[Path] = None
        try:
            d = _token_dir()
            d.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass  # best-effort dir hardening; file perms are the real guard
            # Atomic create: write a unique temp then rename into place.
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, token.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError:
            # Persistence failed — clean up temp and report unavailable. Do NOT
            # return the in-memory token (that is the _load_key trap: core would
            # inject a secret the sidecar can never read → permanent 401 that
            # looks like a token mismatch).
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return None
        # Confirm by RE-READING from disk — only a persisted, readable token is
        # ever returned/injected.
        persisted = _read_token_file(path)
        if persisted is not None:
            _CACHE[ext_id] = (persisted, *_stat_key(path))
        return persisted


def _stat_key(path: Path) -> Tuple[int, int]:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def current_token(ext_id: str) -> Optional[str]:
    """Return the current on-disk token for validation/injection, re-reading the
    file when its mtime/size changed since last read (so rotation + deletion take
    effect immediately and a stale cached token stops validating). Does NOT mint.
    Returns None when no token exists (fail closed)."""
    if not _valid_ext_id(ext_id):
        return None
    path = _token_path(ext_id)
    key = _stat_key(path)
    if key == (0, 0):
        with _LOCK:
            _CACHE.pop(ext_id, None)
        return None
    with _LOCK:
        cached = _CACHE.get(ext_id)
        if cached is not None and (cached[1], cached[2]) == key:
            return cached[0]
    tok = _read_token_file(path)
    with _LOCK:
        if tok is None:
            _CACHE.pop(ext_id, None)
        else:
            _CACHE[ext_id] = (tok, *key)
    return tok


def reset_token(ext_id: str) -> Optional[str]:
    """Rotate: delete then re-mint. Returns the new token or None on failure."""
    if not _valid_ext_id(ext_id):
        return None
    with _LOCK:
        _CACHE.pop(ext_id, None)
        try:
            _token_path(ext_id).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return None
    return ensure_token(ext_id)
