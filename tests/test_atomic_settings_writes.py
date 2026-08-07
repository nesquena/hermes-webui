"""Regression tests — settings.json is written atomically.

`save_settings` (and the startup default-workspace rewrite) persisted
`settings.json` via a plain `Path.write_text`, which truncates the file in
place.  A crash / full disk / power loss mid-write leaves it truncated or
empty, so the next start loses every persisted setting — theme, workspace,
tab order, and the login `password_hash` (losing the hash also silently
disables auth, but the data-loss regression is the point here).

The write now goes through `_atomic_write_settings_text`: temp file in the
same dir, fsync, `os.replace`.  A failure before the rename leaves the
ORIGINAL file byte-for-byte intact.  These pin the helper directly (success,
mode preservation, crash-safety, symlink write-through) so a refactor can't
reintroduce the truncating plain write.
"""
import builtins
import errno
import json
import os
from pathlib import Path
import threading

import pytest

from api.config import _atomic_write_settings_text


def test_replaces_contents(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text('{"theme": "old"}', encoding="utf-8")

    _atomic_write_settings_text(target, '{"theme": "new"}')

    assert target.read_text(encoding="utf-8") == '{"theme": "new"}'
    # No temp debris after a clean write.
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_creates_new_file(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    _atomic_write_settings_text(target, '{"created": true}')
    assert target.read_text(encoding="utf-8") == '{"created": true}'


def test_preserves_hardened_mode(tmp_path: Path) -> None:
    """A 0600 settings.json (operator-hardened because it holds the password
    hash) must not be loosened to 0644 by the atomic replace."""
    target = tmp_path / "settings.json"
    target.write_text('{"password_hash": "x"}', encoding="utf-8")
    os.chmod(target, 0o600)

    _atomic_write_settings_text(target, '{"password_hash": "y"}')

    assert (os.stat(target).st_mode & 0o777) == 0o600


def test_failed_replace_leaves_original_intact(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "settings.json"
    original = '{"theme": "keep-me"}'
    target.write_text(original, encoding="utf-8")

    def _boom(src, dst):
        raise RuntimeError("simulated crash before rename commits")

    monkeypatch.setattr(os, "replace", _boom)

    import api.config as config

    with pytest.raises(config.SettingsPersistenceError) as caught:
        _atomic_write_settings_text(target, '{"theme": "half-written"}')

    assert caught.value.commit_state == config.SettingsPersistenceError.NOT_COMMITTED
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert "simulated crash" in str(caught.value.__cause__)
    assert target.read_text(encoding="utf-8") == original
    # Temp file cleaned up, not left as debris.
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_writes_through_symlink(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    link_dir = tmp_path / "link"
    real_dir.mkdir()
    link_dir.mkdir()
    target = real_dir / "settings.json"
    link = link_dir / "settings.json"
    target.write_text('{"theme": "old"}', encoding="utf-8")
    link.symlink_to(target)

    _atomic_write_settings_text(link, '{"theme": "new"}')

    # The link stays a link; the referent got the new contents.
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == '{"theme": "new"}'
    assert [p.name for p in link_dir.iterdir()] == ["settings.json"]


def test_save_settings_uses_atomic_writer(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: save_settings must route through the atomic writer, not a
    bare write_text (which a future edit could reintroduce)."""
    import api.config as config

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    calls = []
    real = config._atomic_write_settings_text

    def _spy(path, text):
        calls.append(Path(path))
        return real(path, text)

    monkeypatch.setattr(config, "_atomic_write_settings_text", _spy)

    config.save_settings({"theme": "dark"})

    assert settings_file in calls, "save_settings must use the atomic writer"
    assert settings_file.exists()


def test_concurrent_save_settings_serializes_complete_transactions(
    tmp_path: Path, monkeypatch
) -> None:
    import api.config as config

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    first_at_write = threading.Event()
    second_at_lock_attempt = threading.Event()
    second_at_write = threading.Event()
    second_progress = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    payloads = []
    worker_errors = []
    real_write = config._atomic_write_settings_text
    real_lock = config._SETTINGS_WRITE_LOCK

    class _ObservedLock:
        def acquire(self, *args, **kwargs):
            if threading.current_thread() is second:
                second_at_lock_attempt.set()
                second_progress.set()
            return real_lock.acquire(*args, **kwargs)

        def release(self):
            return real_lock.release()

        def __enter__(self):
            return self.acquire()

        def __exit__(self, exc_type, exc_value, traceback):
            return real_lock.__exit__(exc_type, exc_value, traceback)

    def _coordinated_write(path, text):
        payloads.append(json.loads(text))
        if len(payloads) == 1:
            first_at_write.set()
            if not release_first.wait(timeout=5):
                raise TimeoutError("first writer was not released")
        elif len(payloads) == 2:
            second_at_write.set()
            second_progress.set()
            if not release_second.wait(timeout=5):
                raise TimeoutError("second writer was not released")
        return real_write(path, text)

    def _save(update):
        try:
            config.save_settings(update)
        except BaseException as exc:
            worker_errors.append(exc)

    monkeypatch.setattr(config, "_atomic_write_settings_text", _coordinated_write)
    first = threading.Thread(target=_save, args=({"theme": "light"},))
    second = threading.Thread(target=_save, args=({"show_token_usage": True},))
    monkeypatch.setattr(config, "_SETTINGS_WRITE_LOCK", _ObservedLock())

    coordination_error = None
    try:
        first.start()
        assert first_at_write.wait(timeout=5), "worker A did not reach the writer"
        second.start()
        assert second_progress.wait(timeout=5), "worker B made no observable progress"
        assert second_at_lock_attempt.is_set() and not second_at_write.is_set(), (
            "worker B reached the writer before acquiring the transaction lock"
        )
        release_first.set()
        assert second_at_write.wait(timeout=5), "worker B did not reach the writer"
        release_second.set()
        first.join(timeout=5)
        second.join(timeout=5)
    except BaseException as exc:
        coordination_error = exc
    finally:
        release_first.set()
        release_second.set()
        if first.ident is not None:
            first.join(timeout=5)
        if second.ident is not None:
            second.join(timeout=5)

    if worker_errors:
        raise worker_errors[0]
    if coordination_error is not None:
        raise coordination_error
    assert not first.is_alive(), "worker A did not finish"
    assert not second.is_alive(), "worker B did not finish"
    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert saved["theme"] == "light"
    assert saved["show_token_usage"] is True


class _ObservedTextHandle:
    def __init__(self, handle, events, failure_step=None, failure=None):
        self._handle = handle
        self._events = events
        self._failure_step = failure_step
        self._failure = failure

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *args):
        return self._handle.__exit__(*args)

    def write(self, text):
        self._events.append(("write", text))
        if self._failure_step == "write":
            raise self._failure
        return self._handle.write(text)

    def flush(self):
        self._events.append(("flush",))
        if self._failure_step == "flush":
            raise self._failure
        return self._handle.flush()

    def fileno(self):
        return self._handle.fileno()


def test_fsync_order_includes_referent_parent_after_replace(
    tmp_path: Path, monkeypatch
) -> None:
    import api.config as config

    target = tmp_path / "settings.json"
    target.write_text("old", encoding="utf-8")
    effective_target = target.resolve(strict=False)
    expected_tmp = effective_target.with_name(
        f".{effective_target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    events = []
    directory_fds = set()
    real_open = builtins.open
    real_os_open = os.open
    real_fsync = os.fsync
    real_chmod = os.chmod
    real_replace = os.replace
    real_close = os.close

    def _open(path, *args, **kwargs):
        events.append(("temp_create", Path(path)))
        return _ObservedTextHandle(real_open(path, *args, **kwargs), events)

    def _os_open(path, flags):
        events.append(("parent_open", Path(path), flags))
        fd = real_os_open(path, flags)
        directory_fds.add(fd)
        return fd

    def _fsync(fd):
        events.append(("parent_fsync" if fd in directory_fds else "temp_fsync",))
        return real_fsync(fd)

    def _chmod(path, mode):
        events.append(("mode_apply", Path(path), mode))
        return real_chmod(path, mode)

    def _replace(src, dst):
        events.append(("replace", Path(src), Path(dst)))
        return real_replace(src, dst)

    def _close(fd):
        events.append(("parent_close", fd))
        directory_fds.discard(fd)
        return real_close(fd)

    monkeypatch.setattr(builtins, "open", _open)
    monkeypatch.setattr(os, "open", _os_open)
    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(os, "chmod", _chmod)
    monkeypatch.setattr(os, "replace", _replace)
    monkeypatch.setattr(os, "close", _close)

    config._atomic_write_settings_text(target, "new")

    assert events == [
        ("temp_create", expected_tmp),
        ("write", "new"),
        ("flush",),
        ("temp_fsync",),
        ("mode_apply", expected_tmp, 0o644),
        ("replace", expected_tmp, effective_target),
        ("parent_open", effective_target.parent, os.O_RDONLY | os.O_DIRECTORY),
        ("parent_fsync",),
        ("parent_close", events[-1][1]),
    ]


def test_symlink_fsync_uses_referent_parent(tmp_path: Path, monkeypatch) -> None:
    import api.config as config

    referent_dir = tmp_path / "referent"
    symlink_dir = tmp_path / "symlink"
    referent_dir.mkdir()
    symlink_dir.mkdir()
    referent = referent_dir / "settings.json"
    referent.write_text("old", encoding="utf-8")
    link = symlink_dir / "settings.json"
    link.symlink_to(referent)
    opened_directories = []
    real_os_open = os.open

    def _os_open(path, flags):
        opened_directories.append(Path(path))
        return real_os_open(path, flags)

    monkeypatch.setattr(os, "open", _os_open)

    config._atomic_write_settings_text(link, "new")

    assert link.is_symlink()
    assert referent.read_text(encoding="utf-8") == "new"
    assert opened_directories == [link.resolve(strict=False).parent]
    assert symlink_dir not in opened_directories


def test_mode_discovery_failure_has_not_committed_commit_state(
    tmp_path: Path, monkeypatch
) -> None:
    import api.config as config

    target = tmp_path / "settings.json"
    target.write_text("old-authoritative-bytes", encoding="utf-8")
    failure = OSError(errno.EIO, "injected mode discovery failure")
    real_stat = os.stat

    def _stat(path, *args, **kwargs):
        if Path(path) == target and kwargs.get("follow_symlinks", True):
            raise failure
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", _stat)

    with pytest.raises(config.SettingsPersistenceError) as caught:
        config._atomic_write_settings_text(target, "new-bytes")

    assert caught.value.commit_state == config.SettingsPersistenceError.NOT_COMMITTED
    assert caught.value.__cause__ is failure
    assert str(target) not in str(caught.value)
    assert target.read_text(encoding="utf-8") == "old-authoritative-bytes"
    assert [path.name for path in tmp_path.iterdir()] == ["settings.json"]


@pytest.mark.parametrize("failure_step", ["write", "flush", "file_fsync", "chmod", "replace"])
def test_pre_replace_failures_have_not_committed_commit_state(
    tmp_path: Path, monkeypatch, failure_step: str
) -> None:
    import api.config as config

    target = tmp_path / "settings.json"
    target.write_text("old-authoritative-bytes", encoding="utf-8")
    failure = OSError(errno.EIO, "injected pre-replace failure")
    real_open = builtins.open
    real_fsync = os.fsync
    real_chmod = os.chmod
    real_replace = os.replace

    def _open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return _ObservedTextHandle(handle, [], failure_step, failure)

    def _fsync(fd):
        if failure_step == "file_fsync":
            raise failure
        return real_fsync(fd)

    def _chmod(path, mode):
        if failure_step == "chmod":
            raise failure
        return real_chmod(path, mode)

    def _replace(src, dst):
        if failure_step == "replace":
            raise failure
        return real_replace(src, dst)

    monkeypatch.setattr(builtins, "open", _open)
    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(os, "chmod", _chmod)
    monkeypatch.setattr(os, "replace", _replace)

    with pytest.raises(config.SettingsPersistenceError) as caught:
        config._atomic_write_settings_text(target, "new-bytes")

    assert caught.value.commit_state == config.SettingsPersistenceError.NOT_COMMITTED
    assert caught.value.__cause__ is failure
    assert str(target) not in str(caught.value)
    assert target.read_text(encoding="utf-8") == "old-authoritative-bytes"
    assert [path.name for path in tmp_path.iterdir()] == ["settings.json"]


@pytest.mark.parametrize("failure_step", ["parent_open", "parent_fsync"])
def test_post_replace_failures_have_indeterminate_commit_state(
    tmp_path: Path, monkeypatch, failure_step: str
) -> None:
    import api.config as config

    target = tmp_path / "settings.json"
    target.write_text("old-authoritative-bytes", encoding="utf-8")
    failure = OSError(errno.EIO, "injected post-replace failure")
    real_os_open = os.open
    real_fsync = os.fsync
    real_close = os.close
    directory_fds = set()
    closed_fds = []

    def _os_open(path, flags):
        if failure_step == "parent_open":
            raise failure
        fd = real_os_open(path, flags)
        directory_fds.add(fd)
        return fd

    def _fsync(fd):
        if fd in directory_fds and failure_step == "parent_fsync":
            raise failure
        return real_fsync(fd)

    def _close(fd):
        if fd in directory_fds:
            closed_fds.append(fd)
        return real_close(fd)

    monkeypatch.setattr(os, "open", _os_open)
    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(os, "close", _close)

    with pytest.raises(config.SettingsPersistenceError) as caught:
        config._atomic_write_settings_text(target, "new-authoritative-bytes")

    assert (
        caught.value.commit_state
        == config.SettingsPersistenceError.COMMITTED_OR_INDETERMINATE
    )
    assert caught.value.__cause__ is failure
    assert str(target) not in str(caught.value)
    assert target.read_text(encoding="utf-8") == "new-authoritative-bytes"
    assert [path.name for path in tmp_path.iterdir()] == ["settings.json"]
    assert bool(closed_fds) is (failure_step == "parent_fsync")


def test_missing_o_directory_capability_is_ignored(
    tmp_path: Path, monkeypatch
) -> None:
    import api.config as config

    target = tmp_path / "settings.json"
    target.write_text("old", encoding="utf-8")
    monkeypatch.delattr(os, "O_DIRECTORY")

    config._atomic_write_settings_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize("failure_step", ["parent_open", "parent_fsync"])
@pytest.mark.parametrize(
    "capability_errno",
    [errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF],
)
def test_directory_fsync_capability_errors_are_ignored_and_fd_is_closed(
    tmp_path: Path, monkeypatch, failure_step: str, capability_errno: int
) -> None:
    import api.config as config

    target = tmp_path / "settings.json"
    target.write_text("old", encoding="utf-8")
    failure = OSError(capability_errno, "directory fsync unsupported")
    real_os_open = os.open
    real_fsync = os.fsync
    real_close = os.close
    directory_fds = set()
    closed_fds = []

    def _os_open(path, flags):
        if failure_step == "parent_open":
            raise failure
        fd = real_os_open(path, flags)
        directory_fds.add(fd)
        return fd

    def _fsync(fd):
        if fd in directory_fds and failure_step == "parent_fsync":
            raise failure
        return real_fsync(fd)

    def _close(fd):
        if fd in directory_fds:
            closed_fds.append(fd)
        return real_close(fd)

    monkeypatch.setattr(os, "open", _os_open)
    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(os, "close", _close)

    config._atomic_write_settings_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert bool(closed_fds) is (failure_step == "parent_fsync")
