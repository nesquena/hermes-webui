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

These tests pin the helper and all four config.yaml callers across success,
fault, metadata, symlink, permission, and concurrency paths so a future
refactor cannot silently reintroduce the truncating plain-write.
"""

import errno
import os
import shutil
import stat
import subprocess
import sys
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


@pytest.mark.skipif(
    not all(hasattr(os, name) for name in ("getxattr", "listxattr", "setxattr")),
    reason="extended attributes are unavailable on this platform",
)
def test_atomic_write_preserves_existing_user_xattr(tmp_path: Path) -> None:
    """Replacing config contents must not discard administrator metadata."""
    target = tmp_path / "config.yaml"
    target.write_text("old: true\n", encoding="utf-8")
    attribute = "user.hermes_review"
    value = b"preserve-me"
    try:
        os.setxattr(target, attribute, value)
    except OSError as exc:
        if exc.errno in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}:
            pytest.skip("test filesystem does not support user extended attributes")
        raise

    _atomic_write_text(target, "new: true\n")

    assert attribute in os.listxattr(target)
    assert os.getxattr(target, attribute) == value


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="xattr probe unavailable")
def test_unsupported_xattr_probe_keeps_atomic_replace(
    tmp_path: Path, monkeypatch
) -> None:
    """Filesystems without xattr support retain the normal atomic path."""
    target = tmp_path / "config.yaml"
    target.write_text("old: true\n", encoding="utf-8")
    replace_calls = 0
    real_replace = os.replace

    def _unsupported_xattrs(_path) -> list[str]:
        raise OSError(errno.ENOTSUP, "xattrs unsupported")

    def _recording_replace(src, dst) -> None:
        nonlocal replace_calls
        replace_calls += 1
        real_replace(src, dst)

    monkeypatch.setattr(os, "listxattr", _unsupported_xattrs)
    monkeypatch.setattr(os, "replace", _recording_replace)

    _atomic_write_text(target, "new: true\n")

    assert target.read_text(encoding="utf-8") == "new: true\n"
    assert replace_calls == 1


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="xattr probe unavailable")
def test_xattr_probe_failure_does_not_silently_drop_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    """Unexpected metadata-read failures abort before touching the old file."""
    target = tmp_path / "config.yaml"
    target.write_text("old: true\n", encoding="utf-8")

    def _denied_xattrs(_path) -> list[str]:
        raise PermissionError("xattr access denied")

    monkeypatch.setattr(os, "listxattr", _denied_xattrs)

    with pytest.raises(PermissionError, match="xattr access denied"):
        _atomic_write_text(target, "new: true\n")

    assert target.read_text(encoding="utf-8") == "old: true\n"
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]


@pytest.mark.skipif(
    not shutil.which("getfacl") or not shutil.which("setfacl"),
    reason="getfacl/setfacl are unavailable",
)
def test_atomic_write_preserves_existing_posix_acl(tmp_path: Path) -> None:
    """POSIX access ACLs, commonly stored as xattrs, survive a rewrite."""
    target = tmp_path / "config.yaml"
    target.write_text("old: true\n", encoding="utf-8")
    subprocess.run(
        ["setfacl", "-m", "u:12345:r--", str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    expected_acl = subprocess.run(
        ["getfacl", "-cp", str(target)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    _atomic_write_text(target, "new: true\n")

    actual_acl = subprocess.run(
        ["getfacl", "-cp", str(target)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert actual_acl == expected_acl


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


def test_forced_fallback_serializes_writers_and_syncs_only_complete_versions(
    tmp_path: Path, monkeypatch
) -> None:
    """The non-atomic compatibility path must not interleave WebUI writers."""
    target = tmp_path / "config.yaml"
    payloads = [
        f"writer: {index}\npadding: {'x' * 20_000}\n".encode()
        for index in range(6)
    ]
    valid_versions = set(payloads)
    observed_at_sync: list[bytes] = []
    active_writers = 0
    max_active_writers = 0
    state_lock = threading.Lock()
    real_fdopen = os.fdopen
    real_fsync = os.fsync

    from api import paths

    def _force_fallback(*_args, **_kwargs):
        raise PermissionError("simulated non-writable parent")

    class _SlowWriter:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __enter__(self):
            nonlocal active_writers, max_active_writers
            self._wrapped.__enter__()
            with state_lock:
                active_writers += 1
                max_active_writers = max(max_active_writers, active_writers)
            return self

        def __exit__(self, *args):
            nonlocal active_writers
            try:
                return self._wrapped.__exit__(*args)
            finally:
                with state_lock:
                    active_writers -= 1

        def write(self, value: str) -> None:
            midpoint = len(value) // 2
            self._wrapped.write(value[:midpoint])
            self._wrapped.flush()
            time.sleep(0.01)
            self._wrapped.write(value[midpoint:])

        def flush(self) -> None:
            self._wrapped.flush()

        def fileno(self) -> int:
            return self._wrapped.fileno()

    def _slow_fdopen(fd: int, *args, **kwargs):
        return _SlowWriter(real_fdopen(fd, *args, **kwargs))

    def _observing_fsync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            observed_at_sync.append(target.read_bytes())
        real_fsync(fd)

    target.write_bytes(payloads[0])
    monkeypatch.setattr(paths.tempfile, "mkstemp", _force_fallback)
    monkeypatch.setattr(os, "fdopen", _slow_fdopen)
    monkeypatch.setattr(os, "fsync", _observing_fsync)

    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        list(pool.map(lambda payload: _atomic_write_text(target, payload.decode()), payloads))

    assert max_active_writers == 1
    assert len(observed_at_sync) == len(payloads)
    assert all(observed in valid_versions for observed in observed_at_sync)
    assert target.read_bytes() in valid_versions


def test_new_files_follow_the_current_umask_without_a_global_probe(tmp_path: Path) -> None:
    """New config files use the kernel's current umask without mutating it.

    Importing this helper can occur on a request thread, so caching a mode by
    calling ``os.umask`` at import time is still racy. A subprocess isolates the
    process-global umask and proves two writes after import follow two different
    active masks.
    """
    script = """
import os
import stat
import sys
from pathlib import Path
from api.paths import _atomic_write_text

base = Path(sys.argv[1])
os.umask(0o077)
_atomic_write_text(base / 'private.yaml', 'private: true\\n')
os.umask(0o022)
_atomic_write_text(base / 'shared.yaml', 'shared: true\\n')
print(oct(stat.S_IMODE(os.stat(base / 'private.yaml').st_mode)))
print(oct(stat.S_IMODE(os.stat(base / 'shared.yaml').st_mode)))
"""

    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == ["0o600", "0o644"]


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


def test_atomic_write_preserves_hard_link_aliases(tmp_path: Path) -> None:
    """Path.write_text updates a shared inode instead of severing hard links."""
    target = tmp_path / "config.yaml"
    alias = tmp_path / "shared-config.yaml"
    target.write_text("model:\n  default: old\n", encoding="utf-8")
    os.link(target, alias)
    inode = os.stat(target).st_ino

    _atomic_write_text(target, "model:\n  default: new\n")

    assert target.read_text(encoding="utf-8") == "model:\n  default: new\n"
    assert alias.read_text(encoding="utf-8") == "model:\n  default: new\n"
    assert os.stat(target).st_ino == inode
    assert os.stat(alias).st_ino == inode
    assert os.stat(target).st_nlink == 2


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
def test_writable_parent_with_readonly_file_raises_and_keeps_bytes(tmp_path: Path) -> None:
    """A deliberately locked config (0444) in a writable directory must keep
    rejecting writes: without the target-writability probe, atomic replace
    creates a fresh temp inode and renames over the read-only file."""
    cfg_dir = tmp_path / "open-dir"
    cfg_dir.mkdir()
    target = cfg_dir / "config.yaml"
    original = "model:\n  default: keep-me\n"
    target.write_text(original, encoding="utf-8")
    os.chmod(target, 0o444)
    os.chmod(cfg_dir, 0o755)
    try:
        with pytest.raises(PermissionError):
            _atomic_write_text(target, "model:\n  default: new\n")
        assert target.read_text(encoding="utf-8") == original
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o444
    finally:
        os.chmod(target, 0o644)


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
