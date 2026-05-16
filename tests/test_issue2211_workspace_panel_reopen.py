"""Regression coverage for issue #2211 workspace panel reopen affordance."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_workspace_panel_has_edge_reopen_toggle_outside_hidden_panel():
    assert 'id="btnWorkspacePanelEdgeToggle"' in HTML
    assert 'class="workspace-panel-edge-toggle' in HTML
    assert 'onclick="toggleWorkspacePanel(true)"' in HTML
    edge_idx = HTML.index('id="btnWorkspacePanelEdgeToggle"')
    aside_idx = HTML.index('<aside class="rightpanel">')
    assert edge_idx < aside_idx, "reopen control must remain clickable when .rightpanel is collapsed"


def test_workspace_panel_edge_toggle_only_shows_when_panel_closed_on_desktop():
    assert 'html[data-workspace-panel="closed"] .workspace-panel-edge-toggle' in CSS
    assert 'html[data-workspace-panel="open"] .workspace-panel-edge-toggle' in CSS
    assert '@media(max-width:900px)' in CSS and '.workspace-panel-edge-toggle{display:none!important;}' in CSS


def test_workspace_panel_sync_updates_edge_toggle_state_and_accessibility():
    assert "edgeToggleBtn: $('btnWorkspacePanelEdgeToggle')" in BOOT_JS
    assert "edgeToggleBtn.classList.toggle('active',isOpen)" in BOOT_JS
    assert "edgeToggleBtn.setAttribute('aria-expanded',isOpen?'true':'false')" in BOOT_JS
    assert "edgeToggleBtn.disabled=!canBrowse" in BOOT_JS


def test_changelog_mentions_workspace_panel_reopen_affordance():
    assert "#2211" in CHANGELOG
    assert "workspace panel" in CHANGELOG.lower()
    assert "reopen" in CHANGELOG.lower()
