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


def test_workspace_heading_affordance_requires_workspace():
    """The heading should only advertise button behavior when a workspace exists."""
    heading_line = next(line for line in INDEX_HTML.splitlines() if 'id="workspacePanelHeading"' in line)
    assert 'role="button"' not in heading_line
    assert 'tabindex="0"' not in heading_line
    assert "_syncWorkspaceHeadingState" in UI_JS
    assert "heading.classList.toggle('workspace-panel-heading--enabled',enabled)" in UI_JS
    assert "heading.setAttribute('role','button')" in UI_JS
    assert "heading.setAttribute('tabindex','0')" in UI_JS
    assert "heading.removeAttribute('role')" in UI_JS
    assert "heading.removeAttribute('tabindex')" in UI_JS
    assert "if(!(S.session&&S.session.workspace)) return;" in UI_JS
    assert "typeof _syncWorkspaceHeadingState==='function'" in UI_JS

    context_idx = UI_JS.find("heading.oncontextmenu")
    guard_idx = UI_JS.find("if(!(S.session&&S.session.workspace)) return;", context_idx)
    prevent_idx = UI_JS.find("e.preventDefault()", context_idx)
    assert context_idx < guard_idx < prevent_idx
