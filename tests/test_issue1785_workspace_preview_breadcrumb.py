import json
import shutil
import subprocess
from pathlib import Path

import pytest


BOOT_JS = Path("static/boot.js").read_text(encoding="utf-8")
WORKSPACE_JS = Path("static/workspace.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


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


def _async_function_block(src: str, name: str) -> str:
    marker = f"async function {name}("
    start = src.find(marker)
    assert start != -1, f"{name}() not found"
    params_depth = 0
    body_start = -1
    for idx in range(start, len(src)):
        char = src[idx]
        if char == "(":
            params_depth += 1
        elif char == ")":
            params_depth -= 1
        elif char == "{" and params_depth == 0:
            body_start = idx
            break
    assert body_start >= 0, f"{name}() body not found"
    depth = 0
    for idx in range(body_start, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name}() body did not close")


def _run_node(script: str) -> dict:
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


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


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_load_dir_ignores_stale_session_results():
    load_dir = _async_function_block(WORKSPACE_JS, "loadDir")
    request_is_current = _function_block(WORKSPACE_JS, "_workspaceRequestIsCurrent")
    script = f"""
const S={{
  session:{{session_id:'sid-old',profile:'profile-a',workspace:'/old'}},
  activeProfile:'profile-a',entries:[{{name:'current.txt'}}],_dirCache:{{}}
}};
let _wsTreeGen=0;
let resolveOldList;
const oldList=new Promise(resolve=>{{resolveOldList=resolve;}});
function bumpWorkspaceTreeGen(){{_wsTreeGen+=1;return _wsTreeGen;}}
function _restoreExpandedDirs(){{}}
function _workspaceRouteForPath(){{return '/api/list';}}
async function api(){{return oldList;}}
function renderBreadcrumb(){{}}
function renderFileTree(){{}}
function renderSessionArtifacts(){{}}
function clearPreview(){{}}
function syncWorkspaceDisplays(){{}}
function syncTerminalButton(){{}}
function showToast(){{}}
function t(key){{return key;}}
function _refreshGitBadge(){{}}
{request_is_current}
{load_dir}
(async()=>{{
  const pending=loadDir('.');
  await Promise.resolve();
  S.session={{session_id:'sid-new',profile:'profile-b',workspace:'/new'}};
  S.activeProfile='profile-b';
  resolveOldList({{entries:[{{name:'stale.txt'}}],workspace:'/recovered',workspace_recovered:true}});
  await pending;
  process.stdout.write(JSON.stringify({{workspace:S.session.workspace,entries:S.entries}}));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    payload = _run_node(script)

    assert payload == {"workspace": "/new", "entries": [{"name": "current.txt"}]}


def test_file_preview_breadcrumb_uses_directory_navigation_for_root():
    block = _function_block(WORKSPACE_JS, "renderFileBreadcrumb")
    assert "loadDir('.')" in block, "The preview root breadcrumb should navigate to the workspace root."
    assert "clearPreview(); loadDir('.')" not in block, (
        "The preview root breadcrumb should not do a close-style preview clear before "
        "directory navigation; loadDir() owns the keep-open preview clear."
    )
