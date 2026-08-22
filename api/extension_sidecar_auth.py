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
  * The token is re-read from disk per request (fingerprint-cached on
    inode/mtime/ctime/size) so rotation / deletion takes effect with no restart,
    and a stale cached token can't keep validating after the file changes.
  * Cross-process mint writes a unique temporary file, then publishes it with
    an atomic ``os.link`` create-or-fail operation. Concurrent first-mints
    converge on a single winning file; losers re-read the winner rather than
    clobbering it.
"""
from __future__ import annotations

import os
import re
import secrets
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

# Keep this grammar identical to ``_EXTENSION_ID_RE`` in ``api/extensions.py`` —
# a narrower pattern here silently 503s legally-named extensions (uppercase,
# dots, up to 128 chars). The pattern is filesystem-safe as a filename: no path
# separators, cannot start with a dot, so no dotfile / ".."-prefix tricks.
_EXT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Injected/validated tokens are exactly secrets.token_urlsafe output: url-safe
# base64 alphabet. Reject anything else (bounds length, and stops a malformed
# token file's contents from reaching a header where a ValueError could echo it).
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")

_TOKEN_DIR_NAME = "sidecar-auth"
_TOKEN_BYTES = 32  # secrets.token_urlsafe(32) -> ~43 url-safe chars

# Per-request read cache: ext_id -> (token, fingerprint). Guarded by _LOCK.
_LOCK = threading.Lock()
_CACHE: Dict[str, Tuple[str, Tuple]] = {}


def _token_dir() -> Path:
    # Resolve STATE_DIR dynamically (mirrors ``_extension_state_dir`` in
    # api/extensions.py) so tests and relocated installs see the current dir
    # rather than the value cached at import time.
    from api.config import STATE_DIR
    return STATE_DIR / _TOKEN_DIR_NAME


def _token_path(ext_id: str) -> Path:
    return _token_dir() / f"{ext_id}.token"


def _valid_ext_id(ext_id: object) -> bool:
    # Use fullmatch (not match) so a trailing newline can't slip through: in
    # Python's ``re`` a ``$`` anchor also matches just before a final ``\n``,
    # so ``match`` would accept ``"templates\n"`` and _token_path would build a
    # filename containing a literal newline. fullmatch anchors truly to the end.
    # Validate the exact value that _token_path() uses. Trimming only for the
    # check would accept one ID but construct a filename from a different one.
    return isinstance(ext_id, str) and bool(_EXT_ID_RE.fullmatch(ext_id))


def _fingerprint(path: Path) -> Optional[Tuple]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_dev, st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def _read_token_file(path: Path) -> Optional[str]:
    """Return the token text iff it is present and well-formed, else None
    (fail closed). Only a valid token-v1 shape is ever returned, so malformed
    file contents can never reach a header. Thin wrapper over the stable reader."""
    tok, _fp = _stable_read(path)
    return tok


def _stable_read(path: Path) -> Tuple[Optional[str], Optional[Tuple]]:
    """Read the token together with a fingerprint that is verified to bracket the
    read (fingerprint before AND after; retry on any mid-read change). Guarantees
    the returned (token, fingerprint) pair reflects a single consistent on-disk
    state — never new content under an old stat key, and never a value that
    changed underneath us. Returns (None, None) when absent/malformed/racing."""
    for _ in range(4):  # bounded retry; a file being rotated settles fast
        fp_before = _fingerprint(path)
        if fp_before is None:
            return (None, None)
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return (None, None)
        fp_after = _fingerprint(path)
        if fp_after != fp_before:
            continue  # changed mid-read — retry for a consistent snapshot
        tok = raw.strip()
        if not tok or not _TOKEN_RE.match(tok):
            return (None, None)
        return (tok, fp_before)
    return (None, None)  # never settled — fail closed


def ensure_token(ext_id: str) -> Optional[str]:
    """Get-or-create the per-extension token, returning it ONLY if it is durably
    persisted and re-readable from disk. Returns None if it can't be persisted
    (caller must then treat the sidecar proxy as unavailable / 503) — we never
    hand back an ephemeral token a sidecar could never read.

    Idempotent + atomic across processes: the token is written to a unique temp
    file, then published with an atomic hard-link create-or-fail operation, so
    the final path is only ever visible fully written and is never clobbered.
    One concurrent writer wins; every loser reads that persisted winner.
    """
    if not _valid_ext_id(ext_id):
        return None
    path = _token_path(ext_id)

    existing = _read_token_file(path)
    if existing is not None:
        return existing

    with _LOCK:
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
            # Write to a unique temp, fsync, then publish with atomic
            # create-or-fail (os.link, the repo's TOCTOU-safe idiom — see
            # session_recovery.py:627). Unlike os.replace, link does NOT clobber:
            # if another process already published, ours fails, we drop our temp,
            # and everyone reads the single winning file. This closes both the
            # empty-file-exposure race AND the cross-process clobber race.
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, token.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(str(tmp), str(path))
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            except FileExistsError:
                pass  # another writer won; we return the winner via _stable_read below
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                tmp = None
        except OSError:
            # Persistence failed → report unavailable. Do NOT return the
            # in-memory token (the _load_key trap: core would inject a secret the
            # sidecar can never read → permanent 401 that looks like a mismatch).
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return None
        # Return whatever is durably on disk now — under a concurrent mint this
        # may be another writer's token, which is correct (all readers converge
        # on the winning file). Only a persisted, well-formed token is returned.
        persisted, fp = _stable_read(path)
        if persisted is not None and fp is not None:
            _CACHE[ext_id] = (persisted, fp)
        return persisted


def current_token(ext_id: str) -> Optional[str]:
    """Return the current on-disk token for validation/injection, re-reading the
    file when its fingerprint changed since last read — so rotation + deletion
    take effect immediately and a stale cached token stops validating. Does NOT
    mint. Returns None when no token exists (fail closed)."""
    if not _valid_ext_id(ext_id):
        return None
    path = _token_path(ext_id)
    fp = _fingerprint(path)
    if fp is None:
        with _LOCK:
            _CACHE.pop(ext_id, None)
        return None
    with _LOCK:
        cached = _CACHE.get(ext_id)
        if cached is not None and cached[1] == fp:
            return cached[0]
    tok, tok_fp = _stable_read(path)  # bracketed read: content matches tok_fp
    with _LOCK:
        if tok is None or tok_fp is None:
            _CACHE.pop(ext_id, None)
        else:
            _CACHE[ext_id] = (tok, tok_fp)
    return tok


def reset_token(ext_id: str) -> Optional[str]:
    """Rotate: delete then re-mint. Returns the new token or None on failure.
    (Recovery entry point; also usable by a future 'reset token' action.)"""
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
