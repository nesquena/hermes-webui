"""Regression checks for Issue #1003: Appearance settings autosave.

Focus:
- Theme/Skin/Font size should autosave immediately and only show inline status.
- Appearance changes should not participate in the global unsaved-changes guard.
- Full-save flow should still include font_size.
- /api/settings should accept appearance-only payloads and preserve untouched fields.
"""

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from tests._pytest_port import BASE


BOOT_JS = (Path(__file__).parent.parent / "static" / "boot.js").read_text(
    encoding="utf-8"
)
PANELS_JS = (Path(__file__).parent.parent / "static" / "panels.js").read_text(
    encoding="utf-8"
)
INDEX_HTML = (Path(__file__).parent.parent / "static" / "index.html").read_text(
    encoding="utf-8"
)
I18N_JS = (Path(__file__).parent.parent / "static" / "i18n.js").read_text(
    encoding="utf-8"
)


def _function_block(src: str, name: str) -> str:
    marker = re.search(rf"(^|\n)(?:async\s+)?function\s+{re.escape(name)}\(", src)
    assert marker is not None, f"{name}() not found"
    start = marker.start()
    next_marker = re.search(
        r"\n(?:function\s+\w+\(|async\s+function\s+\w+\()", src[start + 1 :]
    )
    if next_marker:
        end = start + 1 + next_marker.start()
    else:
        end = len(src)
    return src[start:end]


def _post(path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        payload = e.read()
        return json.loads(payload or b"{}"), e.code


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def test_appearance_pickers_schedule_autosave_and_do_not_mark_dirty():
    for fn in ("_pickTheme", "_pickSkin", "_pickFontSize"):
        block = _function_block(BOOT_JS, fn)
        assert "_scheduleAppearanceAutosave()" in block, (
            f"{fn}() should invoke _scheduleAppearanceAutosave()"
        )
        assert "_markSettingsDirty()" not in block, (
            f"{fn}() should not call _markSettingsDirty()"
        )


def test_appearance_revert_preview_no_longer_rolls_back_theme_skin_font():
    block = _function_block(PANELS_JS, "_revertSettingsPreview")
    assert "hermes-theme" not in block
    assert "hermes-skin" not in block
    assert "hermes-font-size" not in block
    assert "_markSettingsDirty()" not in block


def test_appearance_autosave_payload_is_theme_skin_font_only():
    block = _function_block(PANELS_JS, "_appearancePayloadFromUi")
    assert "theme:" in block
    assert "skin:" in block
    assert "font_size:" in block
    assert "language:" not in block
    assert "workspace" not in block
    assert "show_token_usage" not in block
    status_block = _function_block(PANELS_JS, "_setAppearanceAutosaveStatus")
    assert "settings_autosave_saving" in status_block
    assert "settings_autosave_saved" in status_block
    assert "settings_autosave_failed" in status_block
    assert "settings_autosave_retry" in status_block


def test_appearance_autosave_status_line_and_i18n_keys_exist():
    assert 'id="settingsAppearanceAutosaveStatus"' in INDEX_HTML
    required_keys = [
        "settings_autosave_saving",
        "settings_autosave_saved",
        "settings_autosave_failed",
        "settings_autosave_retry",
    ]
    for key in required_keys:
        assert I18N_JS.count(f"{key}:") >= 8, (
            f"{key} must be defined in all LOCALES blocks (found {I18N_JS.count(f'{key}:')})"
        )


def test_full_save_settings_still_includes_font_size():
    block = _function_block(PANELS_JS, "saveSettings")
    compact = block.replace(" ", "")
    assert "body.theme=theme;" in compact
    assert "body.skin=skin;" in compact
    assert "body.font_size=fontSize;" in compact


def test_settings_api_accepts_appearance_only_payload_without_overwriting_other_fields():
    original, status = _get("/api/settings")
    assert status == 200
    snapshot = {
        "theme": original.get("theme"),
        "skin": original.get("skin"),
        "font_size": original.get("font_size", "default"),
        "show_token_usage": original.get("show_token_usage"),
        "show_cli_sessions": original.get("show_cli_sessions"),
        "check_for_updates": original.get("check_for_updates"),
    }
    try:
        d, status = _post(
            "/api/settings",
            {"theme": "system", "skin": "charizard", "font_size": "large"},
        )
        assert status == 200
        assert d.get("theme") == "system"
        assert d.get("skin") == "charizard"
        assert d.get("font_size") == "large"
        reloaded, _ = _get("/api/settings")
        assert reloaded.get("show_token_usage") == snapshot["show_token_usage"]
        assert reloaded.get("show_cli_sessions") == snapshot["show_cli_sessions"]
        assert reloaded.get("check_for_updates") == snapshot["check_for_updates"]
    finally:
        _post(
            "/api/settings",
            {
                "theme": snapshot["theme"],
                "skin": snapshot["skin"],
                "font_size": snapshot["font_size"],
            },
        )
