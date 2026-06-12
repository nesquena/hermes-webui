"""Pin the full behavioral contract for the Worklog expanded-default setting."""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _function_body(src, name):
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1:idx]
    raise AssertionError(f"function {name} body not found")


def test_setting_in_defaults():
    src = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
    assert '"worklog_details_expanded_default"' in src or "'worklog_details_expanded_default'" in src, \
        "worklog_details_expanded_default must exist in _SETTINGS_DEFAULTS"
    # Verify default is False
    assert re.search(r'["\']worklog_details_expanded_default["\']:\s*False', src), \
        "worklog_details_expanded_default default must be False (collapsed)"


def test_setting_in_bool_keys():
    src = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
    assert re.search(r'_SETTINGS_BOOL_KEYS\b.*?worklog_details_expanded_default', src, re.DOTALL), \
        "worklog_details_expanded_default must appear inside _SETTINGS_BOOL_KEYS (not just anywhere in config.py)"


def test_legacy_activity_feed_setting_migrates_without_remaining_primary_semantics():
    src = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
    assert '"activity_feed_expanded_default"' in src, \
        "config.py should still accept the legacy key as a migration alias"
    assert re.search(r'_SETTINGS_LEGACY_DROP_KEYS\b.*?activity_feed_expanded_default', src, re.DOTALL), \
        "The legacy Activity Feed key should be dropped from primary settings after migration"
    assert 'settings["worklog_details_expanded_default"] = bool(' in src, \
        "load_settings should migrate legacy Activity Feed values into the Worklog details key"
    assert 'settings.pop("activity_feed_expanded_default", None)' in src, \
        "save_settings should not persist the legacy Activity Feed key"


def test_legacy_activity_feed_setting_migrates_on_load_and_save(monkeypatch, tmp_path):
    from api import config

    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"activity_feed_expanded_default": True}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    loaded = config.load_settings()
    assert loaded["worklog_details_expanded_default"] is True
    assert "activity_feed_expanded_default" not in loaded

    saved = config.save_settings({"activity_feed_expanded_default": False})
    assert saved["worklog_details_expanded_default"] is False
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["worklog_details_expanded_default"] is False
    assert "activity_feed_expanded_default" not in on_disk


def test_boot_initializes_window_flag():
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    assert "worklog_details_expanded_default" in src, \
        "boot.js must initialize window._worklogDetailsExpandedByDefault from settings"
    assert "s.activity_feed_expanded_default" in src, \
        "boot.js should tolerate old servers that still return the legacy key"


def test_ensure_activity_group_checks_flag():
    src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    assert "_worklogDetailsExpandedByDefault" in src, \
        "ensureActivityGroup must check window._worklogDetailsExpandedByDefault"


def test_setting_controls_worklog_item_details_not_only_outer_group():
    src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    assert "function _worklogDetailsExpandedDefault()" in src, \
        "Worklog detail cards should share a single expanded-default helper"

    thinking_fn = _function_body(src, "_thinkingCardHtml")
    assert "_worklogDetailsExpandedDefault()" in thinking_fn, \
        "Thinking cards should respect the Worklog details default"

    legacy_thinking_fn = _function_body(src, "_thinkingMarkup")
    assert "_worklogDetailsExpandedDefault()" in legacy_thinking_fn, \
        "Thinking update fallback markup should not overwrite the Worklog details default"
    assert "!isSimplifiedToolCalling()" not in legacy_thinking_fn, \
        "The deprecated compact-tool toggle should not keep dead branches in Thinking markup"

    tool_fn = _function_body(src, "buildToolCard")
    assert "_worklogDetailsExpandedDefault()" in tool_fn and "openClass" in tool_fn, \
        "Tool cards should respect the Worklog details default when they have detail content"

    grouped_tools_fn = _function_body(src, "_syncToolRowsContainer")
    assert "const shouldOpen=_worklogDetailsExpandedDefault()" in grouped_tools_fn, \
        "Multi-tool Worklog groups should respect the Worklog details default"


def test_setting_toggle_applies_to_existing_worklog_details():
    ui_src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    helper = _function_body(ui_src, "_applyWorklogDetailsExpandedDefault")
    assert "scope.querySelectorAll('.thinking-card')" in helper, \
        "Toggling the setting should update existing Thinking cards"
    assert "scope.querySelectorAll('.tool-card')" in helper and ".tool-card-detail" in helper, \
        "Toggling the setting should update existing Tool cards that have details"
    assert "data-tool-worklog-tool-group" in helper and "aria-expanded" in helper, \
        "Toggling the setting should update existing multi-tool Worklog groups"

    panels_src = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    onchange_block = re.search(
        r"worklogDetailsExpandedCb\.onchange=function\(\)\{(?P<body>.*?)\n\s*\};",
        panels_src,
        re.DOTALL,
    )
    assert onchange_block, "Worklog detail checkbox should have an onchange handler"
    assert "_applyWorklogDetailsExpandedDefault()" in onchange_block.group("body"), \
        "Changing the Worklog detail setting should apply the new default immediately"


def test_appearance_autosave_does_not_reapply_worklog_details_over_manual_state():
    panels_src = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    autosave_fn = _function_body(panels_src, "_autosaveAppearanceSettings")
    assert "_worklogDetailsExpandedByDefault" in autosave_fn, \
        "Autosave should still reconcile the stored Worklog default flag"
    assert "_applyWorklogDetailsExpandedDefault()" not in autosave_fn, \
        "Autosave responses must not overwrite per-turn manual Worklog expand/collapse choices"


def test_per_turn_override():
    src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    assert re.search(r"savedState\s*===\s*['\"]closed['\"]", src), \
        "ensureActivityGroup must check savedState==='closed' so per-turn persistence overrides the global default"


def test_collapse_class_applied():
    src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    assert "tool-call-group-collapsed" in src, \
        "The collapsed class 'tool-call-group-collapsed' must be present in ui.js for the activity group"


def test_settings_checkbox_exists():
    src = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "settings_label_worklog_details_expanded_default" in src, \
        "index.html must have a settings checkbox with data-i18n for worklog_details_expanded_default"
    assert "Open Worklog details automatically" in src, \
        "The setting copy should describe Worklog details, not the old Activity Feed wording"
    assert "Worklog details stay folded by default" in src, \
        "The setting copy must make the off/default state folded"


def test_panels_wiring():
    src = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    assert "worklog_details_expanded_default" in src, \
        "panels.js must read/write the worklog_details_expanded_default setting"
    assert "activity_feed_expanded_default: worklogDetailsExpanded" in src, \
        "panels.js should include a legacy POST alias so old servers can persist the setting during rolling updates"


def test_i18n_keys():
    src = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    assert "settings_label_worklog_details_expanded_default" in src, \
        "i18n.js must have the label key for the setting"
    assert "settings_desc_worklog_details_expanded_default" in src, \
        "i18n.js must have the description key for the setting"
