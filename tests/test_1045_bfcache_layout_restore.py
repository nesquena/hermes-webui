"""
Tests for issue #1045 — bfcache layout broken on tab restore.

When the browser restores a page from bfcache (event.persisted === true),
the async boot IIFE does not re-run. The existing pageshow handler (added for
#822) only cleared the session search field and re-rendered the session list.
This left the rail, topbar, workspace panel, and resize handles in the stale
bfcache DOM state, producing a broken layout.

Fix: extend the pageshow handler to also call syncTopbar, syncWorkspacePanelState,
_initResizePanels, and startGatewaySSE — all guarded so missing helpers degrade.
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent


def _boot_js() -> str:
    return (ROOT / "static" / "boot.js").read_text(encoding="utf-8")


class TestBfcacheLayoutRestore:
    def test_pageshow_calls_sync_topbar(self):
        """pageshow handler must call syncTopbar() on bfcache restore."""
        src = _boot_js()
        # Find the pageshow listener block
        ps_idx = src.find("window.addEventListener('pageshow'")
        assert ps_idx != -1, "pageshow listener not found in boot.js"
        handler_body = src[ps_idx:ps_idx + 1600]
        assert "syncTopbar" in handler_body, (
            "pageshow handler must call syncTopbar() to restore topbar state after bfcache"
        )

    def test_pageshow_calls_sync_workspace_panel_state(self):
        """pageshow handler must call syncWorkspacePanelState()."""
        src = _boot_js()
        ps_idx = src.find("window.addEventListener('pageshow'")
        handler_body = src[ps_idx:ps_idx + 1600]
        assert "syncWorkspacePanelState" in handler_body, (
            "pageshow handler must call syncWorkspacePanelState() on bfcache restore"
        )

    def test_pageshow_calls_init_resize_panels(self):
        """pageshow handler must call _initResizePanels()."""
        src = _boot_js()
        ps_idx = src.find("window.addEventListener('pageshow'")
        handler_body = src[ps_idx:ps_idx + 1600]
        assert "_initResizePanels" in handler_body, (
            "pageshow handler must call _initResizePanels() to restore panel resize state"
        )

    def test_pageshow_calls_start_gateway_sse(self):
        """pageshow handler must call startGatewaySSE() to reconnect the dead SSE connection."""
        src = _boot_js()
        ps_idx = src.find("window.addEventListener('pageshow'")
        handler_body = src[ps_idx:ps_idx + 1600]
        assert "startGatewaySSE" in handler_body, (
            "pageshow handler must restart gateway SSE (bfcache-persisted connections are dead)"
        )

    def test_pageshow_still_clears_session_search(self):
        """pageshow handler must still clear #sessionSearch (original #822 fix preserved)."""
        src = _boot_js()
        ps_idx = src.find("window.addEventListener('pageshow'")
        handler_body = src[ps_idx:ps_idx + 1600]
        assert "sessionSearch" in handler_body, (
            "pageshow handler must still clear #sessionSearch (regression: #822 fix must be preserved)"
        )

    def test_pageshow_still_calls_render_session_list_from_cache(self):
        """pageshow handler must still call renderSessionListFromCache()."""
        src = _boot_js()
        ps_idx = src.find("window.addEventListener('pageshow'")
        handler_body = src[ps_idx:ps_idx + 1600]
        assert "renderSessionListFromCache" in handler_body, (
            "pageshow handler must still call renderSessionListFromCache() (regression: #822 fix)"
        )

    def test_new_calls_are_guarded_with_typeof(self):
        """New calls in the pageshow handler must be typeof-guarded for safe degradation."""
        src = _boot_js()
        ps_idx = src.find("window.addEventListener('pageshow'")
        handler_body = src[ps_idx:ps_idx + 1600]
        # Each of the new calls must be guarded
        for fn in ("syncTopbar", "syncWorkspacePanelState", "_initResizePanels", "startGatewaySSE",
                   "closeModelDropdown", "closeReasoningDropdown", "closeWsDropdown", "closeProfileDropdown"):
            assert f"typeof {fn} === 'function'" in handler_body, (
                f"{fn}() call in pageshow handler must be guarded with typeof === 'function'"
            )

    def test_pageshow_closes_all_dropdowns(self):
        """pageshow handler must close all known dropdowns to reset frozen bfcache popover state."""
        src = _boot_js()
        ps_idx = src.find("window.addEventListener('pageshow'")
        handler_body = src[ps_idx:ps_idx + 1600]
        for fn in ("closeModelDropdown", "closeReasoningDropdown", "closeWsDropdown", "closeProfileDropdown"):
            assert fn in handler_body, (
                f"pageshow handler must call {fn}() to dismiss any dropdown left open by bfcache"
            )

    def test_dropdowns_closed_before_layout_sync(self):
        """Dropdown closes must come before layout sync calls (clean state first)."""
        src = _boot_js()
        ps_idx = src.find("window.addEventListener('pageshow'")
        handler_body = src[ps_idx:ps_idx + 1600]
        close_idx = handler_body.find("closeModelDropdown")
        sync_idx = handler_body.find("syncTopbar")
        assert close_idx != -1 and sync_idx != -1, "Both close and sync calls must be present"
        assert close_idx < sync_idx, (
            "Dropdown close calls must appear before layout sync calls in the pageshow handler"
        )

