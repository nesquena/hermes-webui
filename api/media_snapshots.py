"""Per-message media snapshots: freeze file bytes at message-settle time.

Problem
-------
``/api/media`` serves a file's CURRENT bytes.  Since the ETag-revalidation
work (#6922) made browsers revalidate on every use, an in-place overwrite of
``report.html`` also rewrites every historical chat preview that referenced
it — the user loses the old/new comparison they had when the agent emitted
the file twice under the same name.

This module is the storage half of the fix.  When a turn settles
(``api/streaming.py``), every local-file ``MEDIA:`` reference in the new
assistant messages is snapshotted into a content-addressed store:

    <STATE_DIR>/media_snapshots/<sha256>.snap

Content addressing gives free dedup (an unchanged file re-settles to the
same digest and the copy is skipped) and makes every stored object
IMMUTABLE — the serving side can therefore cache snapshots aggressively.
Messages carry a ``_media_snapshots`` annotation mapping absolute path to
digest (sidecar JSON preserves extra message fields); the frontend appends
``&snap=<digest>`` to ``/api/media`` URLs and ``api/routes.py`` serves the
stored bytes instead of the live file.

Security model
--------------
* Digests are validated by whole-string ``fullmatch`` against ``[0-9a-f]{64}``
  before any path is built — a crafted ``snap=`` value cannot traverse out of
  the store.
* Capture uses the SAME deny predicate as the ``/api/media`` serve path
  (``routes._media_deny_reason``): anything the endpoint refuses to serve is
  never captured in the first place.
* Every captured digest carries a server-owned source-path binding; a digest
  is only ever served back for the exact canonical path it was captured from,
  so it can never act as a bearer capability through a different allowed path.
* The store lives under STATE_DIR; capture is restricted to files that are
  regular files within caller-approved roots (the caller reuses the same
  allow-list reasoning as ``/api/media`` — this module never decides what a
  user may see, only stores bytes it is handed).
* Writes are tmp-file + fsync + rename: a crash never leaves a torn
  snapshot that would serve corrupt bytes forever (content addressing means
  a torn file could never be overwritten with good bytes later).

Caps
----
* Per-file cap (default 50 MB): larger files are not snapshotted; previews
  for them gracefully degrade to the live file.
* Total store cap (default 2 GB): when exceeded after a capture, oldest
  snapshots (by mtime) are evicted until the store fits.  Eviction only
  degrades OLD previews to live-file behaviour; it never corrupts anything.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path

logger = logging.getLogger("hermes.webui")

# Strict whole-string shape.  ``\\Z`` (not ``$``) so a terminal newline cannot
# sneak past the gate; ``fullmatch`` is used at call sites.
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")

# Default caps.  Overridable via env var for operators with unusual disks.
DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024          # 50 MB per snapshot
DEFAULT_TOTAL_CAP_BYTES = 2 * 1024 * 1024 * 1024   # 2 GB total store

_SNAPSHOT_DIR_ENV = "HERMES_WEBUI_MEDIA_SNAPSHOT_DIR"

# Capture and eviction run on the streaming worker thread; serialize them so
# two concurrent settles cannot race the same tmp file or the quota scan.
_LOCK = threading.Lock()

def media_capture_allowed(path: Path) -> bool:
    """Allow-list predicate for snapshot capture (same gate as serve).

    True only when ``path`` is a regular file inside an allowed root and NOT
    inside a denied Hermes-internal state location.  The deny half is the
    SAME predicate the ``/api/media`` serve path uses (``routes._media_deny_reason``,
    the #3234 state/profile deny set): anything the endpoint would refuse to
    serve is never captured in the first place — capture and serve can never
    diverge on what is denied.  Any failure mode returns False —
    snapshotting is best-effort durability, never a reason to widen file
    access.
    """
    import stat as stat_mod

    try:
        resolved = path.resolve()
        st = resolved.stat()
    except OSError:
        return False
    if not stat_mod.S_ISREG(st.st_mode):
        return False

    within_any_root = any(
        _path_within(resolved, root) for root in _allowed_roots_for_capture()
    )
    if not within_any_root:
        return False
    # Authoritative deny parity with the serve path (#6979 Round 2 MUST-FIX 1):
    # STATE_DIR subdirs, webui_state subdirs, named-profile roots, secret
    # basenames and the snapshot store itself are all denied exactly as the
    # route denies them.
    try:
        from api.routes import _media_deny_reason

        if _media_deny_reason(resolved):
            return False
    except Exception:
        return False  # fail closed: never snapshot when the gate is unclear
    return True


def _allowed_roots_for_capture() -> list[Path]:
    """Roots capture is permitted in — same shape as ``/api/media``'s list."""
    roots: list[Path] = []
    home = Path(os.path.expanduser("~"))
    hermes_home = Path(os.getenv("HERMES_HOME", str(home / ".hermes"))).expanduser()
    for candidate in (hermes_home, Path("/tmp"), home / ".hermes"):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved not in roots:
            roots.append(resolved)
    try:
        from api.workspace import get_last_workspace

        ws = Path(get_last_workspace()).resolve()
        if ws.is_dir() and ws not in roots:
            roots.append(ws)
    except Exception:
        pass
    extra = os.environ.get("MEDIA_ALLOWED_ROOTS", "").strip()
    if extra:
        for root in extra.split(os.pathsep):
            root = root.strip()
            if not root:
                continue
            try:
                rp = Path(root).resolve()
            except OSError:
                continue
            if rp.is_dir() and rp not in roots:
                roots.append(rp)
    return roots


def _path_within(child: Path, root: Path) -> bool:
    try:
        child.resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def resolve_media_ref(raw_ref: str) -> Path | None:
    """Map a raw ``MEDIA:`` token to an absolute local path, or None.

    Only bare local paths and ``file://`` URLs resolve; http(s)/data/other
    schemes return None (nothing to snapshot — they are not server files).
    ``~`` is expanded.  No existence check: callers decide whether absence
    matters (capture skips missing files).
    """
    ref = str(raw_ref or "").strip()
    if not ref:
        return None
    if ref.startswith("data:") or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", ref):
        if ref.lower().startswith("file://"):
            from urllib.parse import unquote, urlparse

            try:
                parsed = urlparse(ref)
                ref = unquote(parsed.path or "")
            except Exception:
                ref = ref[len("file://"):]
        else:
            return None
    if not ref or ref.startswith(("http:", "https:")):
        return None
    try:
        return Path(ref).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _default_snapshot_dir() -> Path:
    from api.config import STATE_DIR

    return Path(STATE_DIR) / "media_snapshots"


def get_snapshot_dir() -> Path:
    """Snapshot store root (created lazily by capture)."""
    override = os.getenv(_SNAPSHOT_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return _default_snapshot_dir()


def is_valid_digest(digest: str) -> bool:
    """Strict digest shape check — the ONLY gate before path construction.

    ``fullmatch`` with a ``\\Z``-anchored pattern: ``$`` would also match
    before a terminal newline, letting ``<64 hex>\\n`` slip through.
    """
    try:
        return _DIGEST_RE.fullmatch(str(digest or "")) is not None
    except (TypeError, ValueError):
        return False


def snapshot_path_for_digest(digest: str) -> Path | None:
    """Return the on-disk path for a digest, or None if absent/invalid.

    Never creates anything; serving uses this to decide snapshot vs live-file
    fallback.
    """
    if not is_valid_digest(digest):
        return None
    candidate = get_snapshot_dir() / f"{digest}.snap"
    return candidate if candidate.is_file() else None


def _binding_path_for_digest(digest: str) -> Path:
    """Sidecar holding the source-path set for a digest (``<digest>.src.json``)."""
    return get_snapshot_dir() / f"{digest}.src.json"


def _record_source_binding(digest: str, source: Path) -> None:
    """Persist the server-owned canonical source-path ↔ digest association.

    A digest is only ever served back for the EXACT path it was captured from
    (see :func:`snapshot_servable_for_path`): a digest must never become a
    bearer capability readable through any other allowed path.  The sidecar is
    a tiny path list, rewritten tmp+rename atomically.  Caller holds
    ``_LOCK`` (capture is serialized), so concurrent settles cannot race it.
    """
    try:
        binding_file = _binding_path_for_digest(digest)
        sources: set[str] = set()
        try:
            data = json.loads(binding_file.read_text(encoding="utf-8"))
            sources = set(data.get("sources", []))
        except (OSError, ValueError, TypeError):
            sources = set()
        sources.add(str(Path(source).resolve()))
        tmp = binding_file.with_name(binding_file.name + ".tmp")
        tmp.write_text(
            json.dumps({"digest": digest, "sources": sorted(sources)}, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, binding_file)
    except OSError as exc:
        logger.debug("media snapshot source binding failed for %s: %s", source, exc)


def snapshot_servable_for_path(digest: str, target: Path) -> bool:
    """True only when ``digest`` was captured from canonical path ``target``.

    Serve-side half of the source-path binding (#6979 Round 2 MUST-FIX 1):
    the snapshot branch serves a digest only for the exact authorized path it
    was captured from, so replaying a digest through a different (allowed)
    path can never read stored bytes the normal ``path=`` gate would refuse.
    A missing/invalid sidecar returns False — the caller falls back to the
    live file.
    """
    if not is_valid_digest(digest):
        return False
    try:
        want = str(target.resolve())
    except OSError:
        return False
    try:
        data = json.loads(_binding_path_for_digest(digest).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return want in set(data.get("sources", []))


def _total_cap_bytes() -> int:
    try:
        return max(0, int(os.getenv("HERMES_WEBUI_MEDIA_SNAPSHOT_CAP_BYTES", "")))
    except ValueError:
        return DEFAULT_TOTAL_CAP_BYTES


def _max_file_bytes() -> int:
    try:
        return max(0, int(os.getenv("HERMES_WEBUI_MEDIA_SNAPSHOT_MAX_FILE_BYTES", "")))
    except ValueError:
        return DEFAULT_MAX_FILE_BYTES


def _store_size_and_entries(directory: Path) -> tuple[int, list[tuple[float, int, Path]]]:
    """Scan the store: (total bytes, [(mtime, size, path), ...])."""
    total = 0
    entries: list[tuple[float, int, Path]] = []
    try:
        for child in directory.iterdir():
            if child.suffix != ".snap":
                continue
            try:
                st = child.stat()
            except OSError:
                continue
            total += st.st_size
            entries.append((st.st_mtime, st.st_size, child))
    except OSError:
        pass
    return total, entries


def _enforce_quota_locked(directory: Path) -> None:
    """Evict oldest snapshots until the store is under the total cap.

    Caller holds ``_LOCK``.  Eviction is best-effort: an unlink failure is
    logged and skipped, never raised into the settle path.
    """
    cap = _total_cap_bytes()
    total, entries = _store_size_and_entries(directory)
    if total <= cap:
        return
    entries.sort()  # oldest mtime first
    for _mtime, size, path in entries:
        if total <= cap:
            break
        try:
            path.unlink()
            total -= size
            logger.info("media snapshot quota: evicted %s (%d bytes)", path.name, size)
            # Drop the digest's source-binding sidecar with its blob.
            try:
                _binding_path_for_digest(path.stem).unlink()
            except OSError:
                pass
        except OSError as exc:
            logger.debug("media snapshot eviction failed for %s: %s", path, exc)


def capture_snapshot(source: Path, *, max_file_bytes: int | None = None) -> str | None:
    """Copy ``source`` into the content-addressed store; return its digest.

    Returns None (caller falls back to live-file previews) when:
    * the file is missing / not a regular file / unreadable,
    * the file exceeds the per-file cap,
    * any I/O error occurs mid-copy (the torn tmp is removed).

    Never raises — snapshotting is a durability enhancement and must not be
    able to break the settle path.
    """
    cap = _max_file_bytes() if max_file_bytes is None else max_file_bytes
    try:
        st = source.stat()
    except OSError:
        return None
    import stat as stat_mod

    if not stat_mod.S_ISREG(st.st_mode):
        return None
    if cap and st.st_size > cap:
        return None

    with _LOCK:
        directory = get_snapshot_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None

        digest = hashlib.sha256()
        tmp_path: Path | None = None
        try:
            tmp_path = directory / f".tmp.{os.getpid()}.{threading.get_ident()}"
            with open(source, "rb") as src, open(tmp_path, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            hex_digest = digest.hexdigest()
            final_path = directory / f"{hex_digest}.snap"
            if final_path.exists():
                # Content already stored (dedup) — drop the duplicate copy.
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            else:
                os.replace(tmp_path, final_path)
            tmp_path = None
            # Server-owned source-path binding: this digest may only be served
            # back for THIS canonical path (see snapshot_servable_for_path).
            _record_source_binding(hex_digest, source)
            _enforce_quota_locked(directory)
            return hex_digest
        except OSError as exc:
            logger.debug("media snapshot capture failed for %s: %s", source, exc)
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            return None


def annotate_media_snapshots(
    messages: list,
    *,
    resolve_ref=None,
    allowed_predicate=None,
) -> int:
    """Scan settled messages and snapshot every local-file MEDIA: reference.

    Writes a ``_media_snapshots`` dict ({absolute path: digest}) onto each
    assistant message that carries at least one local-file ``MEDIA:`` ref.
    Messages whose refs are already fully annotated are skipped (idempotent
    across repeated settles).  A recorded digest is FINAL: even if its blob is
    later evicted by quota, a re-settle must not re-capture the CURRENT live
    bytes and silently rebind the historical message — evicted blobs degrade
    to live-file serving instead (#6979 Round 2 SHOULD-FIX).

    ``resolve_ref(raw_ref) -> Path | None`` maps a raw MEDIA token to an
    absolute file path (defaults to :func:`resolve_media_ref`); refs it cannot
    resolve (remote URLs, data: URIs) are ignored.  ``allowed_predicate(path)
    -> bool`` applies the same allow/deny reasoning as ``/api/media``
    (defaults to :func:`media_capture_allowed`) so the store never receives
    bytes the endpoint would not serve.

    Returns the number of new snapshots captured (0 on a repeat settle).
    """
    import re as _re

    if resolve_ref is None:
        resolve_ref = resolve_media_ref
    if allowed_predicate is None:
        allowed_predicate = media_capture_allowed
    media_re = _re.compile(r"MEDIA:([^\s\)\]]+)")
    captured = 0
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or "MEDIA:" not in content:
            continue
        refs = media_re.findall(content)
        if not refs:
            continue
        existing = msg.get("_media_snapshots")
        snaps = dict(existing) if isinstance(existing, dict) else {}
        changed = False
        for raw_ref in refs:
            if resolve_ref is not None:
                try:
                    path = resolve_ref(raw_ref)
                except Exception:
                    path = None
            else:
                path = None
            if path is None:
                continue
            # Index by BOTH the resolved absolute path and the raw token as the
            # frontend embeds it (file:// unwrapped, ~/ kept verbatim). One of
            # the two always matches the path= query param in the rendered URL.
            keys = [str(path)]
            if raw_ref not in keys:
                keys.append(raw_ref)
            # A recorded digest is FINAL once stamped (blob presence is NOT
            # re-checked): quota eviction must not cause a re-settle to
            # re-capture the current live bytes and rebind the historical
            # message — that would defeat per-message immutability and thrash
            # the store with re-capture I/O. Evicted blobs simply fall back to
            # live-file serving on the serve side.
            pending = [k for k in keys if not (snaps.get(k) and is_valid_digest(snaps[k]))]
            if not pending:
                continue  # already stored under every key — zero-I/O fast path
            if allowed_predicate is not None:
                try:
                    if not allowed_predicate(path):
                        continue
                except Exception:
                    continue
            digest = capture_snapshot(path)
            if digest:
                for k in keys:
                    snaps[k] = digest
                changed = True
                captured += 1
        if changed:
            msg["_media_snapshots"] = snaps
    return captured
