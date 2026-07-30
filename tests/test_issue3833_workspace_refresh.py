"""Regression coverage for #3833: refresh should clear stale expanded subtree cache."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (REPO_ROOT / "static/index.html").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO_ROOT / "static/workspace.js").read_text(encoding="utf-8")


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


def test_refresh_button_uses_workspace_refresh_helper():
    """Workspace refresh should go through a dedicated path that clears cache."""
    assert 'id="btnRefreshPanel"' in INDEX_HTML
    assert 'onclick="if(S.session)refreshWorkspacePanel()"' in INDEX_HTML
    assert "onclick=\"if(S.session)loadDir(S.currentDir)\"" not in INDEX_HTML


def test_refresh_workspace_panel_reloads_current_directory_with_expanded_refresh():
    body = _function_block(WORKSPACE_JS, "refreshWorkspacePanel")
    compact = body.replace(" ", "")
    assert "consttargetDir=S.currentDir||'.';" in compact
    assert "loadDir(targetDir,{refreshExpanded:true});" in compact


def test_load_dir_can_refresh_all_expanded_descendants_when_requested():
    block = _function_block(WORKSPACE_JS, "loadDir")
    compact = block.replace(" ", "")
    assert "constrefreshExpanded=!!(opts&&opts.refreshExpanded);" in compact
    assert "if(!path||path==='.'||refreshExpanded){" in compact
    assert "constexpanded=S._expandedDirs||newSet();" in compact
    assert "constpending=[...expanded].filter(dirPath=>!S._dirCache[dirPath]);" in compact
