"""Regression tests for the default-message-mode boot race (#5167 / #5170).

The Default message mode preference (queue/interrupt/steer) was ignored for any
send that happened before the async boot IIFE resolved `await api('/api/settings')`,
because `window._defaultMessageMode` had NO eager default — it was `undefined`
during the boot window, so the send path's `window._defaultMessageMode||'steer'`
silently fell back. Re-saving from the settings panel set the global
synchronously, which is why "re-save fixes it".

The fix gives `window._defaultMessageMode` a deterministic value at script top,
read synchronously from a localStorage mirror ('hermes-default-message-mode',
with a legacy 'hermes-busy-input-mode' fallback read) that the resolved settings
+ the save handler both keep in sync — mirroring how hermes-lang / hermes-theme
are bootstrapped from a synchronous source.

Reported by @b3nw. Issues: #5167 (boot race), #5170 (autosave + panel-load
mirror). #5145 renamed the setting busy_input_mode -> default_message_mode and
flipped the default to 'steer'; this file was updated for the rename while
preserving the persistence-mirror guarantees (the load-failure path must still
honor the saved preference, not clobber it with a hardcoded default).
"""
from pathlib import Path

ROOT = Path(__file__).parent.parent
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


class TestEagerDefault:
    """window._defaultMessageMode must be set eagerly, before the async settings fetch."""

    def test_eager_default_assigned_at_module_scope(self):
        """An eager top-level assignment must exist so first sends honor the preference."""
        assert "window._defaultMessageMode=_readPersistedDefaultMessageMode()" in BOOT_JS, (
            "boot.js must eagerly initialise window._defaultMessageMode from the persisted "
            "mirror at module scope so sends during the boot window don't default silently"
        )

    def test_eager_default_precedes_async_settings_fetch(self):
        """The eager assignment must appear BEFORE the async boot IIFE's /api/settings await.

        This is the whole point of the fix: the value must be deterministic during
        the window between page load and the settings fetch resolving.
        """
        eager_idx = BOOT_JS.find("window._defaultMessageMode=_readPersistedDefaultMessageMode()")
        assert eager_idx >= 0, "eager default assignment not found"
        # The async IIFE awaits /api/settings; the success-path assignment lives inside it.
        await_idx = BOOT_JS.find("const s=await api('/api/settings')")
        assert await_idx >= 0, "async settings fetch not found"
        assert eager_idx < await_idx, (
            "the eager window._defaultMessageMode default must be set BEFORE the async "
            "/api/settings fetch — otherwise the boot-window race (#5167) persists"
        )

    def test_eager_default_precedes_send_definition(self):
        """The eager default in boot.js loads after messages.js (defer order), but the
        assignment itself must sit at top level so it runs during script evaluation,
        not inside a later-firing callback."""
        eager_idx = BOOT_JS.find("window._defaultMessageMode=_readPersistedDefaultMessageMode()")
        # Must be at the start of a line (top-level statement), not indented inside a fn.
        line_start = BOOT_JS.rfind("\n", 0, eager_idx) + 1
        assert BOOT_JS[line_start:eager_idx].strip() == "", (
            "the eager default must be a top-level statement (not nested in a function "
            "or callback) so it runs during script evaluation"
        )


class TestSyncMirrorHelpers:
    """Helper functions backing the synchronous localStorage mirror."""

    def test_persist_helper_defined_and_exposed(self):
        assert "function _persistDefaultMessageMode(" in BOOT_JS
        assert "window._persistDefaultMessageMode=_persistDefaultMessageMode" in BOOT_JS

    def test_read_helper_defined_and_exposed(self):
        assert "function _readPersistedDefaultMessageMode(" in BOOT_JS
        assert "window._readPersistedDefaultMessageMode=_readPersistedDefaultMessageMode" in BOOT_JS

    def test_mirror_uses_dedicated_localstorage_key(self):
        assert "localStorage.setItem(_DEFAULT_MESSAGE_MODE_KEY" in BOOT_JS, (
            "persist helper must write the mode to the dedicated localStorage key"
        )
        assert "localStorage.getItem(_DEFAULT_MESSAGE_MODE_KEY" in BOOT_JS, (
            "read helper must read the mode from the same localStorage key"
        )
        assert "_DEFAULT_MESSAGE_MODE_KEY='hermes-default-message-mode'" in BOOT_JS, (
            "the dedicated key must be 'hermes-default-message-mode'"
        )

    def test_mirror_reads_legacy_key_as_fallback(self):
        """A pre-rename user's persisted preference (under the old
        'hermes-busy-input-mode' key) must survive the rename via a fallback read."""
        assert "_LEGACY_DEFAULT_MESSAGE_MODE_KEY='hermes-busy-input-mode'" in BOOT_JS, (
            "the legacy localStorage key must still be recognised for back-compat"
        )
        assert "localStorage.getItem(_LEGACY_DEFAULT_MESSAGE_MODE_KEY" in BOOT_JS, (
            "the read helper must fall back to the legacy key so an existing "
            "user's persisted preference survives the #5145 rename"
        )

    def test_normalize_validates_against_known_modes(self):
        """Unknown/garbage persisted values must normalize to the safe default, never crash."""
        assert "function _normalizeDefaultMessageMode(" in BOOT_JS
        assert "['queue','interrupt','steer']" in BOOT_JS

    def test_localstorage_access_is_guarded(self):
        """localStorage can throw (privacy mode / disabled). Helpers must try/catch."""
        idx = BOOT_JS.find("function _persistDefaultMessageMode(")
        body = BOOT_JS[idx:idx + 400]
        assert "try{" in body and "catch(_)" in body, (
            "_persistDefaultMessageMode must guard localStorage access against exceptions"
        )
        idx = BOOT_JS.find("function _readPersistedDefaultMessageMode(")
        body = BOOT_JS[idx:idx + 600]
        assert "try{" in body and "catch(_)" in body, (
            "_readPersistedDefaultMessageMode must guard localStorage access against exceptions"
        )


class TestAssignmentSitesSyncMirror:
    """Every place that sets window._defaultMessageMode must keep the mirror in sync."""

    def test_boot_success_path_persists(self):
        assert "window._defaultMessageMode=_persistDefaultMessageMode(s.default_message_mode||s.busy_input_mode)" in BOOT_JS

    def test_boot_catch_path_reads_persisted(self):
        """The critical #5167/#5170 guarantee: settings-load FAILURE must re-read the
        persisted preference, NOT clobber it with a hardcoded default. A saved
        'steer'/'interrupt'/'queue' must still apply when the server is unreachable."""
        assert "window._defaultMessageMode=_readPersistedDefaultMessageMode()" in BOOT_JS
        # Guard against regression: the catch path must NOT hardcode a literal mode.
        assert "window._defaultMessageMode='steer'" not in BOOT_JS, (
            "the settings-load-failure path must read the persisted preference, not "
            "clobber it with a hardcoded 'steer' that ignores a saved 'interrupt'/'queue'"
        )

    def test_panels_save_persists(self):
        assert "_persistDefaultMessageMode(body.default_message_mode||body.busy_input_mode)" in PANELS_JS, (
            "the settings save handler must persist default_message_mode so the next "
            "reload's eager default reads the saved value"
        )

    def test_panels_save_guards_helper_availability(self):
        """panels.js loads before boot.js (defer order); the save handler must
        guard the helper with typeof so a regression in load order can't throw."""
        idx = PANELS_JS.find("_persistDefaultMessageMode(body.default_message_mode||body.busy_input_mode)")
        assert idx >= 0
        window = PANELS_JS[max(0, idx - 160):idx]
        assert "typeof _persistDefaultMessageMode==='function'" in window, (
            "panels.js save handler must guard _persistDefaultMessageMode with a typeof check"
        )

    def test_panels_autosave_persists(self):
        """#5170: the preferences autosave path must also persist the mode."""
        assert "_persistDefaultMessageMode(_dmm)" in PANELS_JS, (
            "the autosave handler must persist default_message_mode so a reload after "
            "an autosave honors the saved value (#5170)"
        )

    def test_panels_panel_load_persists(self):
        """#5170: opening the settings panel must also mirror the mode into localStorage."""
        assert "_persistDefaultMessageMode(defaultMessageModeSel.value)" in PANELS_JS, (
            "loadSettingsPanel must persist default_message_mode on panel load (#5170)"
        )


class TestReadSitesUnchanged:
    """The defensive read sites must keep their ||'steer' fallback (belt + braces)."""

    def test_messages_read_site_intact(self):
        assert "const defaultMessageMode=window._defaultMessageMode||'steer';" in MESSAGES_JS

    def test_ui_read_site_intact(self):
        assert "window._defaultMessageMode||'steer'" in UI_JS
