"""Regression tests for the busy_input_mode boot race (#5167).

The Busy input mode preference (queue/interrupt/steer) was ignored for any send
that happened before the async boot IIFE resolved `await api('/api/settings')`,
because `window._busyInputMode` had NO eager default — it was `undefined` during
the boot window, so the send path's `window._busyInputMode||'queue'` silently
fell back to 'queue'. Re-saving from the settings panel set the global
synchronously, which is why "re-save fixes it".

The fix gives `window._busyInputMode` a deterministic value at script top, read
synchronously from a localStorage mirror ('hermes-busy-input-mode') that the
resolved settings + the save handler both keep in sync — mirroring how
hermes-lang / hermes-theme are bootstrapped from a synchronous source.

Reported by @b3nw. Issue: #5167.
"""
from pathlib import Path

ROOT = Path(__file__).parent.parent
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


class TestEagerDefault:
    """window._busyInputMode must be set eagerly, before the async settings fetch."""

    def test_eager_default_assigned_at_module_scope(self):
        """An eager top-level assignment must exist so first sends honor the preference."""
        assert "window._busyInputMode=_readPersistedBusyInputMode()" in BOOT_JS, (
            "boot.js must eagerly initialise window._busyInputMode from the persisted "
            "mirror at module scope so sends during the boot window don't default to queue"
        )

    def test_eager_default_precedes_async_settings_fetch(self):
        """The eager assignment must appear BEFORE the async boot IIFE's /api/settings await.

        This is the whole point of the fix: the value must be deterministic during
        the window between page load and the settings fetch resolving.
        """
        eager_idx = BOOT_JS.find("window._busyInputMode=_readPersistedBusyInputMode()")
        assert eager_idx >= 0, "eager default assignment not found"
        # The async IIFE awaits /api/settings; the success-path assignment lives inside it.
        await_idx = BOOT_JS.find("const s=await api('/api/settings')")
        assert await_idx >= 0, "async settings fetch not found"
        assert eager_idx < await_idx, (
            "the eager window._busyInputMode default must be set BEFORE the async "
            "/api/settings fetch — otherwise the boot-window race (#5167) persists"
        )

    def test_eager_default_precedes_send_definition(self):
        """The eager default in boot.js loads after messages.js (defer order), but the
        assignment itself must sit at top level so it runs during script evaluation,
        not inside a later-firing callback."""
        eager_idx = BOOT_JS.find("window._busyInputMode=_readPersistedBusyInputMode()")
        # Must be at the start of a line (top-level statement), not indented inside a fn.
        line_start = BOOT_JS.rfind("\n", 0, eager_idx) + 1
        assert BOOT_JS[line_start:eager_idx].strip() == "", (
            "the eager default must be a top-level statement (not nested in a function "
            "or callback) so it runs during script evaluation"
        )


class TestSyncMirrorHelpers:
    """Helper functions backing the synchronous localStorage mirror."""

    def test_persist_helper_defined_and_exposed(self):
        assert "function _persistBusyInputMode(" in BOOT_JS
        assert "window._persistBusyInputMode=_persistBusyInputMode" in BOOT_JS

    def test_read_helper_defined_and_exposed(self):
        assert "function _readPersistedBusyInputMode(" in BOOT_JS
        assert "window._readPersistedBusyInputMode=_readPersistedBusyInputMode" in BOOT_JS

    def test_mirror_uses_dedicated_localstorage_key(self):
        assert "localStorage.setItem('hermes-busy-input-mode'" in BOOT_JS, (
            "persist helper must write the busy mode to a dedicated localStorage key"
        )
        assert "localStorage.getItem('hermes-busy-input-mode')" in BOOT_JS, (
            "read helper must read the busy mode from the same localStorage key"
        )

    def test_normalize_validates_against_known_modes(self):
        """Unknown/garbage persisted values must normalize to 'queue', never crash."""
        assert "function _normalizeBusyInputMode(" in BOOT_JS
        assert "['queue','interrupt','steer']" in BOOT_JS

    def test_localstorage_access_is_guarded(self):
        """localStorage can throw (privacy mode / disabled). Helpers must try/catch."""
        idx = BOOT_JS.find("function _persistBusyInputMode(")
        body = BOOT_JS[idx:idx + 400]
        assert "try{" in body and "catch(_)" in body, (
            "_persistBusyInputMode must guard localStorage access against exceptions"
        )
        idx = BOOT_JS.find("function _readPersistedBusyInputMode(")
        body = BOOT_JS[idx:idx + 400]
        assert "try{" in body and "catch(_)" in body, (
            "_readPersistedBusyInputMode must guard localStorage access against exceptions"
        )


class TestAssignmentSitesSyncMirror:
    """Every place that sets window._busyInputMode must keep the mirror in sync."""

    def test_boot_success_path_persists(self):
        assert "window._busyInputMode=_persistBusyInputMode(s.busy_input_mode)" in BOOT_JS

    def test_boot_catch_path_reads_persisted(self):
        assert "window._busyInputMode=_readPersistedBusyInputMode()" in BOOT_JS

    def test_panels_save_persists(self):
        assert "_persistBusyInputMode(body.busy_input_mode)" in PANELS_JS, (
            "the settings save handler must persist busy_input_mode so the next "
            "reload's eager default reads the saved value"
        )

    def test_panels_save_guards_helper_availability(self):
        """panels.js loads before boot.js (defer order); the save handler must
        guard the helper with typeof so a regression in load order can't throw."""
        idx = PANELS_JS.find("_persistBusyInputMode(body.busy_input_mode)")
        assert idx >= 0
        window = PANELS_JS[max(0, idx - 120):idx]
        assert "typeof _persistBusyInputMode==='function'" in window, (
            "panels.js save handler must guard _persistBusyInputMode with a typeof check"
        )


class TestReadSitesUnchanged:
    """The defensive read sites must keep their ||'queue' fallback (belt + braces)."""

    def test_messages_read_site_intact(self):
        assert "const busyMode=window._busyInputMode||'queue';" in MESSAGES_JS

    def test_ui_read_site_intact(self):
        assert "const busyMode=window._busyInputMode||'queue';" in UI_JS
