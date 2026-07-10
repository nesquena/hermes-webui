"""Regression tests for atomic config.yaml / profile config.yaml writes.

Pre-fix behaviour: ``_save_yaml_config_file`` (api.config) and the profile
model-config writers (api.profiles) persisted YAML via a plain
``Path.write_text``.  A crash — or any exception — after ``open(..., "w")``
truncated the target file but before the full payload was flushed left the
live ``config.yaml`` truncated / corrupt, so the next agent or WebUI start
would fail to parse it (an availability regression, not a disclosure one).

Fix: a shared ``api.paths._atomic_write_text`` helper writes to a temp file in
the same directory, ``fsync``s, then ``os.replace``s it into place.  Because
``os.replace`` is atomic, a failure at any point before the rename commits
leaves the ORIGINAL file byte-for-byte intact, and a success swaps in the new
contents in a single step.

These tests pin the helper and all three config.yaml callers across success,
fault, metadata, symlink, permission, and concurrency paths so a future
refactor cannot silently reintroduce the truncating plain-write.
"""

import errno
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from api.paths import _atomic_write_text


def test_atomic_write_replaces_contents(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("model:\n  default: old\n", encoding="utf-8")

    _atomic_write_text(target, "model:\n  default: new\n")

    assert target.read_text(encoding="utf-8") == "model:\n  default: new\n"
    # No temp files left lying around after a clean write.
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]


def test_atomic_write_creates_new_file(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    assert not target.exists()

    _atomic_write_text(target, "created: true\n")

    assert target.read_text(encoding="utf-8") == "created: true\n"


def test_atomic_write_preserves_existing_permissions(tmp_path: Path) -> None:
    """Rewriting a 0644/0664 config.yaml must not tighten it to 0600.

    ``tempfile.mkstemp`` hard-codes 0600 and ``os.replace`` carries the temp
    file's mode onto the target, so without an explicit chmod every save would
    silently strip group/other read from a world-readable ``config.yaml`` (the
    live homelab install ships 0644, profiles 0664).  config.yaml holds no
    secrets, so that tightening is a real regression, not a hardening.
    """
    target = tmp_path / "config.yaml"
    target.write_text("model:\n  default: old\n", encoding="utf-8")
    os.chmod(target, 0o644)

    _atomic_write_text(target, "model:\n  default: new\n")

    assert target.read_text(encoding="utf-8") == "model:\n  default: new\n"
    assert (os.stat(target).st_mode & 0o777) == 0o644

    # A 0664 (group-writable profile config) survives its mode too.
    os.chmod(target, 0o664)
    _atomic_write_text(target, "model:\n  default: newer\n")
    assert (os.stat(target).st_mode & 0o777) == 0o664

    # Preserve special permission bits too; replacing the inode must not
    # silently discard an administrator's setgid policy on a shared config.
    os.chmod(target, 0o2664)
    _atomic_write_text(target, "model:\n  default: newest\n")
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o2664


@pytest.mark.skipif(not hasattr(os, "chown"), reason="POSIX ownership semantics")
def test_atomic_write_preserves_existing_group(tmp_path: Path) -> None:
    """Replacing the inode must not silently reset an existing config's group."""
    supplementary_groups = [gid for gid in os.getgroups() if gid != os.getegid()]
    if not supplementary_groups:
        pytest.skip("requires a supplementary group distinct from the effective gid")

    target = tmp_path / "config.yaml"
    target.write_text("model:\n  default: old\n", encoding="utf-8")
    expected_gid = supplementary_groups[0]
    os.chown(target, -1, expected_gid)

    _atomic_write_text(target, "model:\n  default: new\n")

    assert os.stat(target).st_gid == expected_gid


@pytest.mark.skipif(not hasattr(os, "fchown"), reason="POSIX ownership semantics")
@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("simulated ownership denial"),
        OSError(errno.EINVAL, "simulated unsupported ownership transfer"),
    ],
)
def test_fchown_denial_falls_back_without_temp_debris(
    tmp_path: Path, monkeypatch, failure: OSError
) -> None:
    """A writer unable to transfer ownership must keep the old inode contract."""
    target = tmp_path / "config.yaml"
    target.write_text("old: true\n", encoding="utf-8")

    def _deny_fchown(*_args) -> None:
        raise failure

    monkeypatch.setattr(os, "fchown", _deny_fchown)

    _atomic_write_text(target, "new: true\n")

    assert target.read_text(encoding="utf-8") == "new: true\n"
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]


def test_atomic_write_without_posix_fd_metadata_helpers(
    tmp_path: Path, monkeypatch
) -> None:
    """Windows-like platforms still use same-directory atomic replacement."""
    target = tmp_path / "config.yaml"
    target.write_text("old: true\n", encoding="utf-8")
    replace_calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    monkeypatch.delattr(os, "fchown", raising=False)
    monkeypatch.delattr(os, "fchmod", raising=False)

    def _recording_replace(src, dst) -> None:
        replace_calls.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _recording_replace)

    _atomic_write_text(target, "new: true\n")

    assert target.read_text(encoding="utf-8") == "new: true\n"
    assert len(replace_calls) == 1
    assert replace_calls[0][0].parent == target.parent
    assert replace_calls[0][1] == target
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]


def test_atomic_write_fsyncs_file_and_parent_directory(
    tmp_path: Path, monkeypatch
) -> None:
    """Durability requires syncing both payload bytes and the committed rename."""
    target = tmp_path / "config.yaml"
    target.write_text("old: true\n", encoding="utf-8")
    synced_types: list[str] = []
    real_fsync = os.fsync

    def _recording_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        synced_types.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _recording_fsync)

    _atomic_write_text(target, "new: true\n")

    assert synced_types == ["file", "directory"]


def test_fdopen_failure_closes_temp_descriptor(tmp_path: Path, monkeypatch) -> None:
    """A wrapper-construction failure must not leak the mkstemp descriptor."""
    target = tmp_path / "config.yaml"
    target.write_text("old: true\n", encoding="utf-8")
    captured_fd: list[int] = []
    real_mkstemp = __import__("tempfile").mkstemp

    def _capturing_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        captured_fd.append(fd)
        return fd, name

    def _failing_fdopen(*_args, **_kwargs):
        raise OSError("simulated fdopen failure")

    from api import paths

    monkeypatch.setattr(paths.tempfile, "mkstemp", _capturing_mkstemp)
    monkeypatch.setattr(os, "fdopen", _failing_fdopen)

    with pytest.raises(OSError, match="simulated fdopen failure"):
        _atomic_write_text(target, "new: true\n")

    assert len(captured_fd) == 1
    with pytest.raises(OSError) as exc_info:
        os.fstat(captured_fd[0])
    assert exc_info.value.errno == errno.EBADF
    assert target.read_text(encoding="utf-8") == "old: true\n"


def test_concurrent_writers_expose_only_complete_versions(tmp_path: Path) -> None:
    """Concurrent saves may be last-writer-wins, but never partial or mixed."""
    target = tmp_path / "config.yaml"
    original = b"model:\n  default: original\n"
    payloads = [
        f"model:\n  default: writer-{index}\n  padding: {'x' * 200_000}\n".encode()
        for index in range(8)
    ]
    target.write_bytes(original)
    valid_versions = {original, *payloads}
    invalid_versions: list[bytes] = []
    reading = threading.Event()
    reading.set()

    def _observe() -> None:
        while reading.is_set():
            observed = target.read_bytes()
            if observed not in valid_versions:
                invalid_versions.append(observed)
                return
            time.sleep(0.001)

    observer = threading.Thread(target=_observe)
    observer.start()
    try:
        with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
            list(pool.map(lambda payload: _atomic_write_text(target, payload.decode()), payloads))
    finally:
        reading.clear()
        observer.join()

    assert invalid_versions == []
    assert target.read_bytes() in payloads
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]


def test_new_file_uses_cached_umask_mode(tmp_path: Path, monkeypatch) -> None:
    """A freshly created file uses _NEW_FILE_MODE (umask-adjusted 0666), not 0600.

    The mode is cached at import (the process umask is read once while the module
    is single-threaded), so this pins the cached value rather than probing umask
    per call. 0o644 = 0o666 & ~0o022, the common server umask.
    """
    from api import paths

    monkeypatch.setattr(paths, "_NEW_FILE_MODE", 0o644)
    target = tmp_path / "config.yaml"
    assert not target.exists()

    _atomic_write_text(target, "created: true\n")

    assert target.exists()
    assert (os.stat(target).st_mode & 0o777) == 0o644


def test_probe_umask_is_read_once_at_import() -> None:
    """_NEW_FILE_MODE is a plausible umask-derived file mode, cached at import."""
    from api import paths

    # A file mode: no execute/setuid bits beyond rw for the three classes, and
    # never more permissive than 0o666 (umask only ever clears bits).
    assert 0 <= paths._NEW_FILE_MODE <= 0o666
    assert paths._NEW_FILE_MODE & 0o111 == 0  # no execute bits on a data file


def test_atomic_write_follows_config_symlink(tmp_path: Path) -> None:
    """Writing through a config.yaml symlink updates the target, not the link.

    ``Path.write_text`` follows symlinks.  The atomic rewrite must preserve that
    contract because ``HERMES_CONFIG_PATH`` and profile config paths may point at
    a shared config via symlink; replacing the symlink itself would silently
    sever the user's chosen config location.
    """
    target_dir = tmp_path / "target"
    link_dir = tmp_path / "link"
    target_dir.mkdir()
    link_dir.mkdir()
    target = target_dir / "config.yaml"
    link = link_dir / "config.yaml"
    target.write_text("model:\n  default: old\n", encoding="utf-8")
    os.chmod(target, 0o644)
    link.symlink_to(target)

    _atomic_write_text(link, "model:\n  default: new\n")

    assert link.is_symlink()
    assert os.readlink(link) == str(target)
    assert target.read_text(encoding="utf-8") == "model:\n  default: new\n"
    assert link.read_text(encoding="utf-8") == "model:\n  default: new\n"
    assert (os.stat(target).st_mode & 0o777) == 0o644
    assert [p.name for p in link_dir.iterdir()] == ["config.yaml"]


def test_symlink_retarget_during_write_aborts_without_touching_either_target(
    tmp_path: Path, monkeypatch
) -> None:
    """A concurrent symlink retarget must not commit to the stale referent."""
    old_target = tmp_path / "old.yaml"
    new_target = tmp_path / "new.yaml"
    link = tmp_path / "config.yaml"
    old_target.write_text("old-target: true\n", encoding="utf-8")
    new_target.write_text("new-target: true\n", encoding="utf-8")
    link.symlink_to(old_target)
    real_fsync = os.fsync
    retargeted = False

    def _retarget_after_payload_sync(fd: int) -> None:
        nonlocal retargeted
        real_fsync(fd)
        if not retargeted and stat.S_ISREG(os.fstat(fd).st_mode):
            link.unlink()
            link.symlink_to(new_target)
            retargeted = True

    monkeypatch.setattr(os, "fsync", _retarget_after_payload_sync)

    with pytest.raises(RuntimeError, match="symlink target changed"):
        _atomic_write_text(link, "replacement: true\n")

    assert link.resolve() == new_target
    assert old_target.read_text(encoding="utf-8") == "old-target: true\n"
    assert new_target.read_text(encoding="utf-8") == "new-target: true\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "config.yaml",
        "new.yaml",
        "old.yaml",
    ]


def test_partial_temp_write_failure_leaves_old_file_intact(
    tmp_path: Path, monkeypatch
) -> None:
    """A failure after writing some temp bytes must preserve valid old YAML."""
    target = tmp_path / "config.yaml"
    original = "model:\n  default: keep-me\n"
    target.write_text(original, encoding="utf-8")

    class _PartialWriter:
        def __init__(self, fd: int):
            self.fd = fd

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            os.close(self.fd)

        def write(self, value: str) -> None:
            os.write(self.fd, value[:8].encode("utf-8"))
            raise OSError("simulated partial temp write")

    def _partial_fdopen(fd: int, *_args, **_kwargs):
        return _PartialWriter(fd)

    monkeypatch.setattr(os, "fdopen", _partial_fdopen)

    with pytest.raises(OSError, match="simulated partial temp write"):
        _atomic_write_text(target, "model:\n  default: replacement\n")

    assert target.read_text(encoding="utf-8") == original
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]


def test_encoding_and_newline_bytes_match_path_write_text(tmp_path: Path) -> None:
    """The atomic helper preserves the prior writer's encoding/newline contract."""
    target = tmp_path / "config.yaml"
    expected = tmp_path / "expected.yaml"
    text = "greeting: Grüß dich\r\nnext: line\n"
    expected.write_text(text, encoding="utf-16")

    _atomic_write_text(target, text, encoding="utf-16")

    assert target.read_bytes() == expected.read_bytes()


def test_symlink_chain_updates_final_referent(tmp_path: Path) -> None:
    target = tmp_path / "real.yaml"
    middle = tmp_path / "middle.yaml"
    outer = tmp_path / "config.yaml"
    target.write_text("old: true\n", encoding="utf-8")
    middle.symlink_to(target)
    outer.symlink_to(middle)

    _atomic_write_text(outer, "new: true\n")

    assert outer.is_symlink()
    assert middle.is_symlink()
    assert target.read_text(encoding="utf-8") == "new: true\n"


def test_failed_write_leaves_old_file_intact(tmp_path: Path, monkeypatch) -> None:
    """A crash at the os.replace step must not touch the original file."""
    target = tmp_path / "config.yaml"
    original = "model:\n  default: keep-me\n"
    target.write_text(original, encoding="utf-8")

    boom = RuntimeError("simulated crash mid-write")

    def _failing_replace(src, dst):
        raise boom

    monkeypatch.setattr(os, "replace", _failing_replace)

    with pytest.raises(RuntimeError, match="simulated crash mid-write"):
        _atomic_write_text(target, "model:\n  default: half-written\n")

    # Original config survives untouched — the whole point of the fix.
    assert target.read_text(encoding="utf-8") == original
    # And the temp file was cleaned up rather than left as debris.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "config.yaml"]
    assert leftovers == []


def test_failed_write_through_symlink_leaves_link_and_target_intact(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed symlink write must not replace the symlink or truncate target."""
    target_dir = tmp_path / "target"
    link_dir = tmp_path / "link"
    target_dir.mkdir()
    link_dir.mkdir()
    target = target_dir / "config.yaml"
    link = link_dir / "config.yaml"
    original = "model:\n  default: keep-me\n"
    target.write_text(original, encoding="utf-8")
    link.symlink_to(target)

    boom = RuntimeError("simulated crash mid-write")

    def _failing_replace(src, dst):
        raise boom

    monkeypatch.setattr(os, "replace", _failing_replace)

    with pytest.raises(RuntimeError, match="simulated crash mid-write"):
        _atomic_write_text(link, "model:\n  default: half-written\n")

    assert link.is_symlink()
    assert os.readlink(link) == str(target)
    assert target.read_text(encoding="utf-8") == original
    assert [p.name for p in link_dir.iterdir()] == ["config.yaml"]
    assert [p.name for p in target_dir.iterdir()] == ["config.yaml"]


@pytest.mark.parametrize(
    "writer", ["main", "onboarding", "profile_endpoint", "profile_defaults"]
)
def test_each_config_writer_preserves_old_bytes_when_replace_fails(
    tmp_path: Path, monkeypatch, writer: str
) -> None:
    """Pin every config.yaml writer to the shared atomic failure contract."""
    original = b"model:\n  default: keep-me\n"
    target = tmp_path / "config.yaml"
    target.write_bytes(original)

    def _failing_replace(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _failing_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        if writer == "main":
            from api import config

            config._save_yaml_config_file(target, {"model": {"default": "new"}})
        elif writer == "onboarding":
            from api import onboarding

            onboarding._save_yaml_config(
                target, {"model": {"default": "replacement-model"}}
            )
        else:
            from api import profiles

            if writer == "profile_endpoint":
                profiles._write_endpoint_to_config(tmp_path, base_url="https://example.invalid")
            else:
                profiles._write_model_defaults_to_config(
                    tmp_path, default_model="replacement-model"
                )

    assert target.read_bytes() == original


_ROOT_SKIP = pytest.mark.skipif(
    getattr(os, "geteuid", lambda: -1)() == 0,
    reason="root bypasses directory permission bits",
)


@_ROOT_SKIP
def test_readonly_parent_with_writable_file_falls_back_to_write_through(
    tmp_path: Path,
) -> None:
    """A locked-down config dir with a writable config.yaml must still save.

    Hardened deployments (documented ``HERMES_CONFIG_PATH`` layouts) keep the
    containing directory read-only while leaving ``config.yaml`` itself
    writable.  The old ``Path.write_text`` only needed file write permission;
    ``mkstemp(dir=parent)`` needs DIRECTORY write permission, so without the
    fallback every settings/onboarding/profile save would start failing there.
    """
    cfg_dir = tmp_path / "locked"
    cfg_dir.mkdir()
    target = cfg_dir / "config.yaml"
    target.write_text("model:\n  default: old\n", encoding="utf-8")
    os.chmod(target, 0o644)
    os.chmod(cfg_dir, 0o555)
    try:
        _atomic_write_text(target, "model:\n  default: new\n")

        assert target.read_text(encoding="utf-8") == "model:\n  default: new\n"
        # In-place write-through keeps the file's mode and leaves no debris.
        assert (os.stat(target).st_mode & 0o777) == 0o644
        assert [p.name for p in cfg_dir.iterdir()] == ["config.yaml"]
    finally:
        os.chmod(cfg_dir, 0o755)


@_ROOT_SKIP
def test_writable_unreadable_parent_still_commits_atomically(tmp_path: Path) -> None:
    """A best-effort directory fsync must not reject a writable 0333 parent."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    target = cfg_dir / "config.yaml"
    target.write_text("old: true\n", encoding="utf-8")
    os.chmod(cfg_dir, 0o333)
    try:
        _atomic_write_text(target, "new: true\n")
        assert target.read_text(encoding="utf-8") == "new: true\n"
    finally:
        os.chmod(cfg_dir, 0o755)


def test_fallback_rejects_target_swap_before_open(tmp_path: Path, monkeypatch) -> None:
    """The non-atomic fallback must not overwrite a swapped-in unrelated inode."""
    target = tmp_path / "config.yaml"
    victim = tmp_path / "victim.yaml"
    target.write_text("original: true\n", encoding="utf-8")
    victim.write_text("victim: keep\n", encoding="utf-8")

    from api import paths

    def _swap_then_deny(*_args, **_kwargs):
        target.unlink()
        target.symlink_to(victim)
        raise PermissionError("simulated non-writable parent")

    monkeypatch.setattr(paths.tempfile, "mkstemp", _swap_then_deny)

    with pytest.raises(PermissionError, match="changed before fallback"):
        _atomic_write_text(target, "victim: overwritten\n")

    assert target.is_symlink()
    assert victim.read_text(encoding="utf-8") == "victim: keep\n"


@_ROOT_SKIP
def test_readonly_parent_without_writable_target_still_raises(
    tmp_path: Path,
) -> None:
    """The fallback is scoped to existing writable files, not a broad catch.

    With no target file to write through (or an unwritable one), swallowing
    the ``PermissionError`` would turn a genuinely misconfigured deployment
    into a silent no-op save — the error must keep propagating.
    """
    cfg_dir = tmp_path / "locked"
    cfg_dir.mkdir()
    missing = cfg_dir / "config.yaml"
    os.chmod(cfg_dir, 0o555)
    try:
        with pytest.raises(PermissionError):
            _atomic_write_text(missing, "model:\n  default: new\n")
        assert not missing.exists()
    finally:
        os.chmod(cfg_dir, 0o755)


@_ROOT_SKIP
def test_readonly_parent_with_unwritable_file_still_raises(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "locked"
    cfg_dir.mkdir()
    target = cfg_dir / "config.yaml"
    original = "model:\n  default: keep-me\n"
    target.write_text(original, encoding="utf-8")
    os.chmod(target, 0o444)
    os.chmod(cfg_dir, 0o555)
    try:
        with pytest.raises(PermissionError):
            _atomic_write_text(target, "model:\n  default: new\n")
        assert target.read_text(encoding="utf-8") == original
    finally:
        os.chmod(cfg_dir, 0o755)
        os.chmod(target, 0o644)
