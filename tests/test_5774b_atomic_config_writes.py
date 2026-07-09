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

These tests pin the helper directly (both success and mid-write-failure paths)
so a future refactor can't silently reintroduce the truncating plain-write.
"""

import os
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
