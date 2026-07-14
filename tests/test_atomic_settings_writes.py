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
import os
from pathlib import Path

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

    with pytest.raises(RuntimeError, match="simulated crash"):
        _atomic_write_settings_text(target, '{"theme": "half-written"}')

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
