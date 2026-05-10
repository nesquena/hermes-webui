"""Regression checks for Issue #1003 Phase 2: Preferences settings autosave (PR #1369).

Mirrors the structure of test_1003_appearance_autosave.py to verify the
preferences-panel autosave pattern is wired correctly:

- All 14 preference fields use _schedulePreferencesAutosave (not _markSettingsDirty)
- Password field MUST still call _markSettingsDirty (security: never autosave)
- _preferencesPayloadFromUi covers all 14 fields
- _setPreferencesAutosaveStatus uses the shared i18n keys
- Status div exists in static/index.html
- _autosavePreferencesSettings clears the dirty flag and hides the unsaved bar
"""

import re
from pathlib import Path

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
    end = start + 1 + next_marker.start() if next_marker else len(src)
    return src[start:end]


def _load_settings_panel_block() -> str:
    return _function_block(PANELS_JS, "loadSettingsPanel")


# ── Field-by-field autosave wiring ───────────────────────────────────────

PREFERENCE_FIELDS_AUTOSAVE = [
    # (DOM id, field name in _preferencesPayloadFromUi)
    ("settingsSendKey", "send_key"),
    ("settingsLanguage", "language"),
    ("settingsShowTokenUsage", "show_token_usage"),
    ("settingsShowTps", "show_tps"),
    ("settingsSimplifiedToolCalling", "simplified_tool_calling"),
    ("settingsShowCliSessions", "show_cli_sessions"),
    ("settingsSyncInsights", "sync_to_insights"),
    ("settingsCheckUpdates", "check_for_updates"),
    ("settingsSoundEnabled", "sound_enabled"),
    ("settingsNotificationsEnabled", "notifications_enabled"),
    ("settingsSidebarDensity", "sidebar_density"),
    ("settingsAutoTitleRefresh", "auto_title_refresh_every"),
    ("settingsBusyInputMode", "busy_input_mode"),
    ("settingsBotName", "bot_name"),
]


def test_all_14_preference_fields_have_autosave_payload_entries():
    """_preferencesPayloadFromUi must include all 14 preference fields."""
    block = _function_block(PANELS_JS, "_preferencesPayloadFromUi")
    for dom_id, field in PREFERENCE_FIELDS_AUTOSAVE:
        assert f"$('{dom_id}')" in block, (
            f"_preferencesPayloadFromUi missing reference to {dom_id}"
        )
        assert f"payload.{field}=" in block, (
            f"_preferencesPayloadFromUi missing payload assignment for {field}"
        )


def test_preference_fields_use_schedule_autosave_not_mark_dirty():
    """All 13 listener attachments (excluding bot_name's debounce wrapper) must
    use _schedulePreferencesAutosave. bot_name uses a wrapper but still
    eventually calls _schedulePreferencesAutosave."""
    panel = _load_settings_panel_block()
    # Each field should have at least one addEventListener call wired to the autosave
    # path. We check that for each non-password/non-model field, the dirty marker
    # has been replaced.
    for dom_id, _field in PREFERENCE_FIELDS_AUTOSAVE:
        if dom_id == "settingsBotName":
            # Bot name uses a 500ms wrapper that calls _schedulePreferencesAutosave
            # via setTimeout. The wrapper itself is in the loadSettingsPanel block.
            assert "_schedulePreferencesAutosave" in panel, (
                "_schedulePreferencesAutosave must be referenced for bot_name flow"
            )
            continue
        # For other fields: search the field's block for the addEventListener call
        # and verify it points to _schedulePreferencesAutosave.
        # We use a context window around the dom_id to find the listener.
        idx = panel.find(f"$('{dom_id}')")
        assert idx != -1, f"{dom_id} not loaded in loadSettingsPanel"
        # Window of next ~600 chars covers the .addEventListener call
        window = panel[idx : idx + 600]
        assert "addEventListener" in window, f"{dom_id} has no addEventListener"
        assert "_schedulePreferencesAutosave" in window, (
            f"{dom_id} listener should call _schedulePreferencesAutosave (Phase 2 #1003)"
        )
        assert "_markSettingsDirty" not in window, (
            f"{dom_id} should not call _markSettingsDirty (Phase 2 autosaves it)"
        )


def test_password_still_uses_mark_dirty():
    """SECURITY INVARIANT: password field must NEVER autosave; it must still
    call _markSettingsDirty so user explicitly clicks Save Settings."""
    panel = _load_settings_panel_block()
    idx = panel.find("$('settingsPassword')")
    assert idx != -1, "settingsPassword field not loaded"
    window = panel[idx : idx + 400]
    assert "_markSettingsDirty" in window, (
        "Password field MUST call _markSettingsDirty (security: never autosave passwords)"
    )
    assert "_schedulePreferencesAutosave" not in window, (
        "Password field MUST NOT call _schedulePreferencesAutosave (security)"
    )


def test_autosave_clears_dirty_flag_and_hides_unsaved_bar():
    """_autosavePreferencesSettings must clear the dirty flag and hide the
    unsaved-changes bar on success — but ONLY when password and model are
    not pending. Q1 from Opus pre-release review of v0.50.250."""
    block = _function_block(PANELS_JS, "_autosavePreferencesSettings")
    # Must check pwField/modelSel state before clearing dirty + hiding bar
    assert "settingsPassword" in block, (
        "_autosavePreferencesSettings must check the password field before "
        "clearing _settingsDirty (Opus SHOULD-FIX Q1: autosave was clobbering "
        "pending password edits)"
    )
    assert "settingsModel" in block, (
        "_autosavePreferencesSettings must check the model selector before "
        "clearing _settingsDirty (autosave was clobbering pending model changes)"
    )
    assert "_settingsHermesDefaultModelOnOpen" in block, (
        "_autosavePreferencesSettings must compare the model selector value "
        "against the on-open snapshot to detect a pending change"
    )
    # The clear-and-hide block must be conditional, not unconditional
    compact = block.replace(" ", "").replace("\n", "")
    assert (
        "if(!pwDirty&&!modelDirty)" in compact or "if(pwDirty||modelDirty)" in compact
    ), (
        "_autosavePreferencesSettings must guard the dirty-clear and bar-hide "
        "with a condition that defers when a manual field has pending edits"
    )


def test_status_div_exists_in_index_html():
    """The status div must be present in index.html for status feedback."""
    assert 'id="settingsPreferencesAutosaveStatus"' in INDEX_HTML


def test_set_status_uses_shared_i18n_keys():
    """_setPreferencesAutosaveStatus must use the shared i18n keys from Phase 1."""
    block = _function_block(PANELS_JS, "_setPreferencesAutosaveStatus")
    for key in [
        "settings_autosave_saving",
        "settings_autosave_saved",
        "settings_autosave_failed",
        "settings_autosave_retry",
    ]:
        assert key in block, f"_setPreferencesAutosaveStatus must use '{key}'"


def test_retry_function_exists_and_falls_back_gracefully():
    """_retryPreferencesAutosave must exist and use the saved retry payload (or
    rebuild from UI if unavailable)."""
    block = _function_block(PANELS_JS, "_retryPreferencesAutosave")
    assert "_settingsPreferencesAutosaveRetryPayload" in block, (
        "Retry must reference the stored payload"
    )
    assert "_preferencesPayloadFromUi" in block, (
        "Retry must fall back to rebuilding from UI when no stored payload"
    )
    assert "_autosavePreferencesSettings" in block, (
        "Retry must invoke _autosavePreferencesSettings"
    )


def test_debounce_cancels_pending_timer_on_rapid_input():
    """_schedulePreferencesAutosave must clear any in-flight timer before
    setting a new one — otherwise rapid changes queue up multiple POSTs."""
    block = _function_block(PANELS_JS, "_schedulePreferencesAutosave")
    assert "clearTimeout(_settingsPreferencesAutosaveTimer)" in block, (
        "_schedulePreferencesAutosave must clearTimeout the prior timer"
    )
    assert "350" in block, (
        "_schedulePreferencesAutosave must use 350ms debounce (matching Phase 1)"
    )


def test_phase1_appearance_autosave_still_passes():
    """Sanity: Phase 2 must not break Phase 1's pattern. The Appearance autosave
    functions and i18n keys must still exist."""
    assert "function _appearancePayloadFromUi" in PANELS_JS
    assert "function _autosaveAppearanceSettings" in PANELS_JS
    assert "function _scheduleAppearanceAutosave" in PANELS_JS
    assert 'id="settingsAppearanceAutosaveStatus"' in INDEX_HTML
