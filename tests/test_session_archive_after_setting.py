"""Backend contract tests for the session archive cutoff setting."""

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_archive_after_setting_defaults_and_validation_contract():
    import api.config as config

    assert config._SETTINGS_DEFAULTS["session_archive_after_days"] == 7
    assert config._SETTINGS_INT_CHOICES["session_archive_after_days"] == {7, 14, 30, 90}
    assert "session_archive_after_days" in config._SETTINGS_ALLOWED_KEYS
    assert "session_archive_after_days" not in config._SETTINGS_BOOL_KEYS
    assert "session_archive_after_days" not in config._SETTINGS_ENUM_VALUES
    assert "session_archive_after_days" not in config._SETTINGS_INT_RANGES


def test_archive_after_setting_persists_integer_choices(monkeypatch, tmp_path):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    saved = config.save_settings({"session_archive_after_days": 14})
    assert saved["session_archive_after_days"] == 14
    assert json.loads(settings_path.read_text(encoding="utf-8"))["session_archive_after_days"] == 14

    saved = config.save_settings({"session_archive_after_days": "30"})
    assert saved["session_archive_after_days"] == 30
    assert json.loads(settings_path.read_text(encoding="utf-8"))["session_archive_after_days"] == 30


def test_archive_after_setting_rejects_invalid_values_preserving_previous(monkeypatch, tmp_path):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    assert config.save_settings({"session_archive_after_days": 14})["session_archive_after_days"] == 14

    for bad_value in (8, 0, "bad"):
        saved = config.save_settings({"session_archive_after_days": bad_value})
        assert saved["session_archive_after_days"] == 14
        assert json.loads(settings_path.read_text(encoding="utf-8"))["session_archive_after_days"] == 14


def test_load_settings_normalizes_invalid_archive_after_value(monkeypatch, tmp_path):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"session_archive_after_days": "bad"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    loaded = config.load_settings()
    assert loaded["session_archive_after_days"] == 7


def test_archive_cutoff_setting_is_registered_and_wired_through_ui():
    assert '"session_archive_after_days": 7' in CONFIG_PY
    assert '"session_archive_after_days": {7, 14, 30, 90}' in CONFIG_PY

    assert 'id="settingsArchiveAfterDays"' in INDEX_HTML
    assert 'data-i18n="settings_label_archive_after_days"' in INDEX_HTML
    assert 'data-i18n="settings_desc_archive_after_days"' in INDEX_HTML

    assert "payload.session_archive_after_days=parseInt(archiveAfterSel.value,10)" in PANELS_JS
    assert "settings.session_archive_after_days" in PANELS_JS

    assert "const _archiveAfterRaw=s.session_archive_after_days==null?7:s.session_archive_after_days;" in BOOT_JS
    assert "window._sessionArchiveAfterDays=Number.isFinite(_archiveAfterParsed)&&_archiveAfterParsed>=0?_archiveAfterParsed:7" in BOOT_JS

    for key in (
        "settings_label_archive_after_days",
        "settings_desc_archive_after_days",
        "settings_archive_after_days_7",
        "settings_archive_after_days_14",
        "settings_archive_after_days_30",
        "settings_archive_after_days_90",
    ):
        assert key in I18N_JS
