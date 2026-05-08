from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_workspace_heading_is_interactive_root_control():
    """The WORKSPACE panel heading should behave like the breadcrumb root."""
    assert 'id="workspacePanelHeading"' in INDEX_HTML
    assert "bindWorkspaceHeadingActions" in UI_JS
    assert "loadDir('.')" in UI_JS


def test_workspace_heading_context_menu_exposes_root_reveal_and_copy_path():
    """Right-clicking the heading should expose root-scoped Reveal and Copy path actions."""
    assert "_showWorkspaceRootContextMenu" in UI_JS
    assert "'/api/file/reveal'" in UI_JS
    assert "'/api/file/path'" in UI_JS
    assert "path:'.'" in UI_JS.replace(" ", "")
    assert "copy_file_path" in UI_JS
    assert "reveal_in_finder" in UI_JS
