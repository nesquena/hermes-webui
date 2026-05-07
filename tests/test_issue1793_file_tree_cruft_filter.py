from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_workspace_panel_has_show_hidden_files_toggle():
    """File-tree cruft must be recoverable via an explicit user toggle."""
    assert 'id="workspaceShowHiddenFiles"' in INDEX_HTML
    assert "toggleWorkspaceHiddenFiles" in UI_JS
    assert "workspace_show_hidden_files" in I18N_JS


def test_file_tree_filters_common_cruft_by_default():
    """macOS/Windows/VCS/cache noise should not render by default."""
    assert "WORKSPACE_HIDDEN_FILE_NAMES" in UI_JS
    for name in [".DS_Store", "Thumbs.db", "Desktop.ini", ".git", "__pycache__", "node_modules"]:
        assert name in UI_JS
    assert "_visibleWorkspaceEntries" in UI_JS
    assert "S.showHiddenWorkspaceFiles" in UI_JS
    assert "_workspaceShouldHideEntry" in UI_JS


def test_hidden_file_toggle_invalidates_tree_render_without_refetch():
    """The toggle should re-render cached entries instead of changing workspace state."""
    assert "function toggleWorkspaceHiddenFiles" in UI_JS
    assert "renderFileTree()" in UI_JS[UI_JS.index("function toggleWorkspaceHiddenFiles"):]
    assert "localStorage.setItem('hermes-workspace-show-hidden-files'" in UI_JS
