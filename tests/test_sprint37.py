"""
Sprint 37 Tests: Workspace panel open/closed state persists across refreshes via localStorage.
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent
BOOT_JS   = (REPO_ROOT / "static" / "boot.js").read_text()
HTML      = (REPO_ROOT / "static" / "index.html").read_text()


# ── Persistence: save on change ───────────────────────────────────────────────

def test_workspace_panel_saves_to_localstorage():
    """_setWorkspacePanelMode must call localStorage.setItem with hermes-webui-workspace-panel."""
    assert "hermes-webui-workspace-panel" in BOOT_JS, \
        "boot.js must use localStorage key 'hermes-webui-workspace-panel' to persist panel state"


def test_workspace_panel_save_inside_set_mode():
    """localStorage.setItem for panel state must live inside _setWorkspacePanelMode."""
    fn_idx = BOOT_JS.find("function _setWorkspacePanelMode(")
    fn_end = BOOT_JS.find("\n}", fn_idx) + 2
    fn_body = BOOT_JS[fn_idx:fn_end]
    assert "hermes-webui-workspace-panel" in fn_body, \
        "localStorage save must be inside _setWorkspacePanelMode so every state change is captured"


def test_workspace_panel_saves_open_value():
    """When the panel is open, localStorage must be set to 'open'."""
    fn_idx = BOOT_JS.find("function _setWorkspacePanelMode(")
    fn_end = BOOT_JS.find("\n}", fn_idx) + 2
    fn_body = BOOT_JS[fn_idx:fn_end]
    assert "'open'" in fn_body or '"open"' in fn_body, \
        "_setWorkspacePanelMode must store 'open' for an open panel state"


def test_workspace_panel_saves_closed_value():
    """When the panel is closed, localStorage must be set to 'closed'."""
    fn_idx = BOOT_JS.find("function _setWorkspacePanelMode(")
    fn_end = BOOT_JS.find("\n}", fn_idx) + 2
    fn_body = BOOT_JS[fn_idx:fn_end]
    assert "'closed'" in fn_body or '"closed"' in fn_body, \
        "_setWorkspacePanelMode must store 'closed' for a closed panel state"


# ── Persistence: restore on boot ─────────────────────────────────────────────

def test_workspace_panel_restored_on_boot():
    """Boot IIFE must read hermes-webui-workspace-panel from localStorage and restore the mode."""
    # Find the boot IIFE (the async IIFE at the bottom of boot.js)
    iife_idx = BOOT_JS.rfind("(async function")
    if iife_idx < 0:
        iife_idx = BOOT_JS.rfind("(async()=>{")
    iife_body = BOOT_JS[iife_idx:]
    assert "hermes-webui-workspace-panel" in iife_body, \
        "Boot IIFE must read 'hermes-webui-workspace-panel' from localStorage to restore panel state on load"


def test_workspace_panel_restore_sets_browse_mode():
    """When localStorage says 'open', boot must set _workspacePanelMode to 'browse' before syncing."""
    iife_idx = BOOT_JS.rfind("(async function")
    if iife_idx < 0:
        iife_idx = BOOT_JS.rfind("(async()=>{")
    iife_body = BOOT_JS[iife_idx:]
    # The restore block must assign _workspacePanelMode = 'browse'
    assert "_workspacePanelMode='browse'" in iife_body or "_workspacePanelMode = 'browse'" in iife_body, \
        "Boot must set _workspacePanelMode='browse' when restoring an open panel"


def test_workspace_panel_restore_before_sync():
    """Restore must happen before syncWorkspacePanelState() so the state drives the initial render."""
    iife_idx = BOOT_JS.rfind("(async function")
    if iife_idx < 0:
        iife_idx = BOOT_JS.rfind("(async()=>{")
    iife_body = BOOT_JS[iife_idx:]
    restore_pos = iife_body.find("hermes-webui-workspace-panel")
    sync_pos    = iife_body.find("syncWorkspacePanelState()")
    assert restore_pos >= 0, "restore read must be present in boot IIFE"
    assert sync_pos >= 0,    "syncWorkspacePanelState call must be present in boot IIFE"
    assert restore_pos < sync_pos, \
        "Workspace panel restore must happen BEFORE syncWorkspacePanelState() so the correct mode is applied"
