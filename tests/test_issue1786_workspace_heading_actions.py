from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_workspace_heading_is_interactive_root_control():
    """The WORKSPACE panel heading should behave like the breadcrumb root."""
    assert 'id="workspacePanelHeading"' in INDEX_HTML
    assert "bindWorkspaceHeadingActions" in UI_JS
    assert "loadDir('.')" in UI_JS


def test_workspace_heading_context_menu_exposes_root_actions():
    """Right-clicking the heading should expose root-scoped create and utility actions."""
    assert "_showWorkspaceRootContextMenu" in UI_JS
    assert "promptNewFile('.')" in UI_JS
    assert "promptNewFolder('.')" in UI_JS
    assert "'/api/file/reveal'" in UI_JS
    assert "'/api/file/path'" in UI_JS
    assert "path:'.'" in UI_JS.replace(" ", "")
    assert "new_file_prompt" in UI_JS
    assert "new_folder_prompt" in UI_JS
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


def test_new_folder_add_as_workspace_prompt_uses_no_for_cancel_label():
    """The post-create-folder workspace prompt should offer an explicit 'No' option."""
    assert "title:t('folder_add_as_space_title')" in UI_JS
    assert "message:t('folder_add_as_space_msg')" in UI_JS
    assert "confirmLabel:t('folder_add_as_space_btn')" in UI_JS
    assert "cancelLabel:t('status_no')" in UI_JS
