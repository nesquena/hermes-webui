from pathlib import Path


BOOT_JS = Path("static/boot.js").read_text(encoding="utf-8")
WORKSPACE_JS = Path("static/workspace.js").read_text(encoding="utf-8")


def _function_block(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name}() not found"
    params_end = src.find("){", start)
    assert params_end != -1, f"{name}() body not found"
    brace = params_end + 1
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name}() body did not close")


def test_clear_preview_can_keep_preview_only_panel_open_for_directory_navigation():
    """#1785: leaving preview via a directory breadcrumb should switch to browse mode, not close."""
    block = _function_block(BOOT_JS, "clearPreview")
    assert "keepPanelOpen" in block, (
        "clearPreview() needs an explicit keep-open option so breadcrumb/directory "
        "navigation can leave preview-only mode without closing the workspace panel."
    )
    assert "_workspacePanelMode==='preview'&&!keepPanelOpen" in block.replace(" ", ""), (
        "Preview-only close behavior should remain for the X button, but must be gated "
        "off when directory navigation requests keepPanelOpen."
    )
    assert "openWorkspacePanel('browse')" in block or '_setWorkspacePanelMode("browse")' in block, (
        "When keepPanelOpen is requested from preview-only mode, clearPreview() should "
        "transition the workspace panel to browse mode so the root listing remains visible."
    )


def test_load_dir_keeps_workspace_panel_open_when_clearing_preview():
    """#1785: loadDir('.') from the ~ breadcrumb should reveal the listing, not collapse the panel."""
    block = _function_block(WORKSPACE_JS, "loadDir")
    assert "clearPreview({keepPanelOpen:true})" in block.replace(" ", ""), (
        "Directory navigation clears previews as part of showing the file tree; that clear "
        "must keep the workspace panel open for breadcrumb navigation from preview mode."
    )


def test_file_preview_breadcrumb_uses_directory_navigation_for_root():
    block = _function_block(WORKSPACE_JS, "renderFileBreadcrumb")
    assert "loadDir('.')" in block, "The preview root breadcrumb should navigate to the workspace root."
    assert "clearPreview(); loadDir('.')" not in block, (
        "The preview root breadcrumb should not do a close-style preview clear before "
        "directory navigation; loadDir() owns the keep-open preview clear."
    )
