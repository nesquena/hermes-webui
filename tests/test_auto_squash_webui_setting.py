"""WebUI wiring for automatic snapshot squash after compression."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_preferences_expose_auto_squash_checkbox():
    assert 'id="settingsAutoSquashAfterCompression"' in INDEX
    assert 'data-i18n="settings_label_auto_squash_after_compression"' in INDEX
    assert 'data-i18n="settings_desc_auto_squash_after_compression"' in INDEX


def test_preferences_persist_and_hydrate_auto_squash_setting():
    assert "payload.auto_squash_after_compression=autoSquashAfterCompressionCb.checked" in PANELS
    assert "autoSquashAfterCompressionCb.checked=!!settings.auto_squash_after_compression" in PANELS
    assert "autoSquashAfterCompressionCb.addEventListener('change',()=>{" in PANELS


def test_auto_squash_copy_has_english_and_french_translations():
    assert "settings_label_auto_squash_after_compression: 'Compact compression snapshots automatically'" in I18N
    assert "settings_label_auto_squash_after_compression: 'Compacter automatiquement les snapshots de compression'" in I18N
    assert I18N.count("settings_desc_auto_squash_after_compression:") >= 2
