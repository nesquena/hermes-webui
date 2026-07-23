"""Shared path helpers for Hermes WebUI.

Keep low-level filesystem defaults here instead of in ``api.config`` so modules
that need the default Hermes home can import them without triggering config's
larger startup side effects.
"""

import errno
import os
import secrets
import stat
import tempfile
import threading
from pathlib import Path

HOME = Path.home()


def _create_atomic_temp_file(write_path: Path, *, existing: bool) -> tuple[int, str]:
    """Create a same-directory temp file without reading the process umask.

    ``tempfile.mkstemp`` deliberately creates files as ``0600``. For a brand-new
    config, use ``os.open(..., 0666)`` instead so the kernel applies the active
    process umask as it would for ``Path.write_text``. Reading umask by calling
    ``os.umask`` is unsafe because that syscall mutates process-wide state, and
    imports may happen after the threaded server has started.
    """
    if existing:
        return tempfile.mkstemp(
            dir=str(write_path.parent), prefix=f".{write_path.name}_", suffix=".tmp"
        )

    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    for _ in range(100):
        tmp = write_path.parent / f".{write_path.name}_{secrets.token_hex(16)}.tmp"
        try:
            return os.open(tmp, flags, 0o666), str(tmp)
        except FileExistsError:
            continue
    raise FileExistsError("unable to allocate unique config temporary file")

# In-place compatibility writes cannot use rename isolation. Serialize every
# such write in this process so concurrent WebUI saves cannot interleave their
# truncate/write/fsync sequences. A single lock avoids a permanent per-path
# lock cache for arbitrary profile/config locations.
_IN_PLACE_WRITE_LOCK = threading.RLock()


def _fsync_directory(directory: Path) -> None:
    """Persist a completed rename on filesystems that support directory fsync."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
    except PermissionError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
    finally:
        os.close(fd)


def _has_extended_attributes(path: Path) -> bool:
    """Return whether *path* has xattrs that an inode replacement would discard."""
    listxattr = getattr(os, "listxattr", None)
    if listxattr is None:
        return False
    try:
        return bool(listxattr(path))
    except OSError as exc:
        unsupported_errnos = {
            errno.ENOSYS,
            errno.ENOTSUP,
            getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
        }
        if exc.errno in unsupported_errnos:
            return False
        raise


def _require_writable_target(write_path: Path) -> os.stat_result | None:
    """Reject writes to a read-only target before any replacement work.

    ``os.replace`` happily swaps a fresh inode over a ``0444`` file when the
    directory is writable, which would silently defeat a deliberately locked
    config. The old in-place ``Path.write_text`` raised ``PermissionError``
    there; preserve that contract with a non-truncating ``O_WRONLY`` probe,
    letting the failure propagate. Returns the ``fstat`` of the inode the
    probe actually opened (so the caller's metadata describes the verified
    file even if a concurrent writer replaced it since its own ``stat``), or
    ``None`` if the target vanished concurrently.
    """
    try:
        probe_fd = os.open(write_path, os.O_WRONLY)
    except FileNotFoundError:
        return None
    try:
        return os.fstat(probe_fd)
    finally:
        os.close(probe_fd)


def _atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace *path* with *text*.

    Writes to a temp file in the same directory, flushes + ``os.fsync``, then
    ``os.replace``s it into place, so a crash (or exception) mid-write can never
    truncate an existing file — the old contents stay intact until the rename
    commits the new ones in one step.  Mirrors the tempfile+fsync+os.replace
    pattern already used for ``.env`` and cost snapshots in ``api.providers``;
    extracted here so ``config.yaml`` / profile ``config.yaml`` writers can share
    it without pulling in env-file-specific logic.

    Permissions and ownership are preserved: ``os.replace`` carries the temp
    file's metadata onto the target, and ``tempfile.mkstemp`` starts with the
    writer's owner/group and mode ``0600``. Without correcting those values,
    every rewrite could silently reset a shared config's group and tighten a
    group/other-readable ``config.yaml`` (commonly ``0644``/``0664``) down to
    owner-only. Existing files copy uid/gid and mode onto the temp descriptor;
    new files use the active process umask, exactly as a normal
    ``open(..., "w")`` would.
    (Unlike ``.env``, ``config.yaml`` holds no secrets and is not meant to be
    forced to ``0600``.)

    Symlinks and hard links keep the same follow-through semantics as
    ``Path.write_text``: writing ``config.yaml`` through a symlink updates the
    referent, while writing an inode with multiple hard links updates every
    alias rather than severing the selected path. A symlink target change
    observed before commit is rejected rather than updating a stale referent.

    The temp+rename dance needs WRITE PERMISSION ON THE DIRECTORY, which the
    plain ``Path.write_text`` it replaced did not: hardened deployments
    (``HERMES_CONFIG_PATH`` pointing into a root-owned, read-only dir with a
    writable ``config.yaml`` inside) would otherwise lose the ability to save
    settings at all. When temp creation is denied, or the original uid/gid
    cannot be transferred to the temp inode, we fall back to a guarded
    descriptor write only if the target is still the regular-file inode
    inspected above. Hard-linked files and files with extended attributes use
    that same path because replacing their directory entry would respectively
    break ``Path.write_text`` compatibility or discard metadata such as POSIX
    ACLs. These in-place writes are serialized across process-local WebUI
    writers. They give up crash-atomicity (and raw concurrent readers may
    observe the write in progress) only where atomic replace with preserved
    semantics is impossible. Other ``PermissionError``s still propagate.
    Successful renames fsync the parent directory where supported.

    The caller is responsible for ensuring ``path.parent`` exists.
    """
    path = Path(path)
    symlink_target = path.resolve(strict=False) if path.is_symlink() else None
    write_path = symlink_target or path
    try:
        existing_stat = os.stat(write_path)
    except FileNotFoundError:
        existing_stat = None
    mode = stat.S_IMODE(existing_stat.st_mode) if existing_stat else None

    def _verify_symlink_target() -> None:
        if symlink_target is not None and path.resolve(strict=False) != symlink_target:
            raise RuntimeError("config symlink target changed during atomic write")

    def _write_in_place() -> None:
        with _IN_PLACE_WRITE_LOCK:
            if existing_stat is None or not stat.S_ISREG(existing_stat.st_mode):
                raise PermissionError(f"Cannot replace config path: {write_path}")
            _verify_symlink_target()
            fallback_fd = os.open(write_path, os.O_WRONLY)
            owns_fallback_fd = True
            try:
                opened_stat = os.fstat(fallback_fd)
                if (opened_stat.st_dev, opened_stat.st_ino) != (
                    existing_stat.st_dev,
                    existing_stat.st_ino,
                ):
                    raise PermissionError("config target changed before fallback write")
                _verify_symlink_target()
                os.ftruncate(fallback_fd, 0)
                fallback_file = os.fdopen(fallback_fd, "w", encoding=encoding)
                owns_fallback_fd = False
                with fallback_file:
                    fallback_file.write(text)
                    fallback_file.flush()
                    os.fsync(fallback_file.fileno())
            finally:
                if owns_fallback_fd:
                    os.close(fallback_fd)

    if existing_stat is not None and stat.S_ISREG(existing_stat.st_mode):
        probed_stat = _require_writable_target(write_path)
        if probed_stat is not None and stat.S_ISREG(probed_stat.st_mode):
            existing_stat = probed_stat
            mode = stat.S_IMODE(existing_stat.st_mode)
        else:
            existing_stat = probed_stat
            mode = None
        if existing_stat is not None and (
            existing_stat.st_nlink > 1 or _has_extended_attributes(write_path)
        ):
            _write_in_place()
            return

    try:
        fd, tmp = _create_atomic_temp_file(write_path, existing=existing_stat is not None)
    except PermissionError:
        _write_in_place()
        return
    owns_fd = True
    try:
        if existing_stat is not None and hasattr(os, "fchown"):
            try:
                os.fchown(fd, existing_stat.st_uid, existing_stat.st_gid)
            except (OSError, NotImplementedError) as exc:
                unsupported_ownership_errnos = {
                    errno.EINVAL,
                    errno.ENOSYS,
                    errno.ENOTSUP,
                }
                if (
                    isinstance(exc, OSError)
                    and not isinstance(exc, PermissionError)
                    and exc.errno not in unsupported_ownership_errnos
                ):
                    raise
                os.close(fd)
                owns_fd = False
                os.unlink(tmp)
                _write_in_place()
                return
        if mode is not None and hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        elif mode is not None:
            os.chmod(tmp, mode)
        f = os.fdopen(fd, "w", encoding=encoding)
        owns_fd = False
        with f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _verify_symlink_target()
        os.replace(tmp, write_path)
        _fsync_directory(write_path.parent)
    except BaseException:
        if owns_fd:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _hermes_home_has_webui_state(base: Path) -> bool:
    """Return True when *base* holds real WebUI state under its ``webui/`` dir.

    Used only on Windows to detect a pre-v0.51.134 install at the legacy
    ``%USERPROFILE%\\.hermes`` location so we don't strand the user's existing
    sessions/pins/settings when the default moved to ``%LOCALAPPDATA%\\hermes``
    (#2905).

    We intentionally check ONLY WebUI-owned artifacts (the ``webui/`` subtree),
    NOT agent-owned files like ``config.yaml`` / ``auth.json``.  The agent has
    defaulted to ``%LOCALAPPDATA%\\hermes`` on Windows since before #2897, so a
    long-time agent user who never ran WebUI at the legacy location would have a
    stray ``auth.json`` there — keying on that would wrongly divert a *fresh*
    WebUI install to the legacy dir.  Only ``webui/`` state is what actually
    gets stranded by the move, so it is the correct and narrow signal.
    Cheap stat-only checks; never raises.
    """
    try:
        if not base.is_dir():
            return False
        markers = (
            base / "webui" / "sessions",        # WebUI session store
            base / "webui" / "settings.json",   # WebUI UI settings + pins
            base / "webui",                     # WebUI state dir at all
        )
        return any(m.exists() for m in markers)
    except OSError:
        return False


def _platform_default_hermes_home() -> Path:
    """Return the platform-aware default Hermes home when HERMES_HOME is unset.

    Native Windows Hermes Agent installs default to %LOCALAPPDATA%\\hermes,
    while POSIX installs use ~/.hermes.

    Windows migration safety (#2905): v0.51.134 moved the Windows default from
    ``%USERPROFILE%\\.hermes`` to ``%LOCALAPPDATA%\\hermes`` to match the agent.
    Upgrading users whose WebUI state still lives at the old location saw an
    empty app (sessions/pins/settings "lost" — actually just at an address the
    new build no longer reads).  To avoid stranding that data, prefer the
    legacy ``%USERPROFILE%\\.hermes`` ONLY when it is populated AND the new
    ``%LOCALAPPDATA%\\hermes`` location is not yet established.  This is a
    non-destructive, self-healing fallback: no files are moved, and once the
    new location has state (fresh installs, or users who set HERMES_HOME) the
    legacy path is never preferred.  Explicit HERMES_HOME / HERMES_WEBUI_STATE_DIR
    overrides take precedence upstream and are unaffected.
    """
    if os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            new_home = Path(local_app_data) / "hermes"
            legacy_home = HOME / ".hermes"
            # Only fall back to the legacy home if it actually holds state and
            # the new location has not been established yet — the exact
            # post-upgrade fingerprint from #2905.
            if (
                legacy_home != new_home
                and not _hermes_home_has_webui_state(new_home)
                and _hermes_home_has_webui_state(legacy_home)
            ):
                return legacy_home
            return new_home
    return HOME / ".hermes"
