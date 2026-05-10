"""
Regression tests for GitHub issue #570 follow-up:
PermissionError from SETTINGS_FILE.exists() in Docker UID-mismatch scenarios.

When ~/.hermes is owned by a different UID than the container user (common in
Docker setups), Path.exists() raises PermissionError instead of returning False.
load_settings() must treat that as "file not accessible = use defaults" rather
than propagating the exception up to crash the request handler.
"""

import stat
import api.config as config


def test_load_settings_returns_defaults_when_settings_file_unreadable(
    monkeypatch, tmp_path
):
    """PermissionError from SETTINGS_FILE.exists() must not propagate — return defaults instead.

    Regression for issue #570 comment: Docker UID mismatch caused every request
    to 500 because load_settings() called SETTINGS_FILE.exists() without catching OSError.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    settings_file = state_dir / "settings.json"
    # Create the file then make the parent unreadable so .exists() raises PermissionError
    settings_file.write_text('{"send_key": "ctrl+enter"}', encoding="utf-8")
    state_dir.chmod(stat.S_IWUSR)  # write-only: stat() on children will fail

    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    try:
        result = config.load_settings()
        # Must not raise; must return a dict with default values
        assert isinstance(result, dict)
        assert "send_key" in result
        # The corrupted/inaccessible value should NOT appear — defaults win
        assert result["send_key"] == config._SETTINGS_DEFAULTS["send_key"]
    finally:
        state_dir.chmod(stat.S_IRWXU)  # restore for cleanup


def test_load_settings_returns_defaults_when_exists_raises_permission_error(
    monkeypatch, tmp_path
):
    """Direct simulation: monkeypatch SETTINGS_FILE.exists to raise PermissionError."""
    from unittest import mock

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    settings_file = state_dir / "settings.json"

    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    with mock.patch.object(
        type(settings_file), "exists", side_effect=PermissionError("Permission denied")
    ):
        result = config.load_settings()

    assert isinstance(result, dict)
    assert result["send_key"] == config._SETTINGS_DEFAULTS["send_key"]
