import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_async_function(source: str, name: str) -> str:
    marker = f"async function {name}("
    start = source.find(marker)
    assert start >= 0, f"{name} not found"
    params_depth = 0
    body_start = -1
    for idx in range(start, len(source)):
        char = source[idx]
        if char == "(":
            params_depth += 1
        elif char == ")":
            params_depth -= 1
        elif char == "{" and params_depth == 0:
            body_start = idx
            break
    assert body_start >= 0, f"could not find function body for {name}"
    depth = 0
    for idx in range(body_start, len(source)):
        char = source[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"could not find balanced function body for {name}")


def _run_node(script: str) -> dict:
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_list_recovery_updates_authoritative_workspace_and_visible_controls():
    load_dir = _extract_async_function(WORKSPACE_JS, "loadDir")
    script = f"""
const events=[];
const S={{session:{{session_id:'sid-1',workspace:'/deleted'}},_dirCache:{{old:true}}}};
let _wsTreeGen=0;
function bumpWorkspaceTreeGen(){{_wsTreeGen+=1;return _wsTreeGen;}}
function _restoreExpandedDirs(){{events.push(['restore',S.session.workspace]);}}
function _workspaceRouteForPath(){{return '';}}
async function api(){{return {{entries:[{{name:'a'}}],workspace:'/fallback-a',workspace_recovered:true}};}}
function renderBreadcrumb(){{events.push(['breadcrumb',S.session.workspace]);}}
function renderFileTree(){{events.push(['tree',S.session.workspace]);}}
function renderSessionArtifacts(){{}}
function clearPreview(){{}}
function syncWorkspaceDisplays(){{events.push(['display',S.session.workspace]);}}
function syncTerminalButton(){{events.push(['terminal',S.session.workspace]);}}
function showToast(message,duration,kind){{events.push(['toast',message,duration,kind]);}}
function t(key,path){{return key+':'+path;}}
function _refreshGitBadge(){{}}
{load_dir}
(async()=>{{
  await loadDir('.');
  process.stdout.write(JSON.stringify({{workspace:S.session.workspace,entries:S.entries,events,treeGen:_wsTreeGen}}));
}})().catch(err=>{{console.error(err);process.exit(1);}});
"""
    payload = _run_node(script)

    assert payload["workspace"] == "/fallback-a"
    assert payload["entries"] == [{"name": "a"}]
    assert payload["treeGen"] == 0
    assert ["restore", "/fallback-a"] in payload["events"]
    assert ["display", "/fallback-a"] in payload["events"]
    assert ["terminal", "/fallback-a"] in payload["events"]
    assert [
        "toast",
        "workspace_recovered_notice:/fallback-a",
        5000,
        "warning",
    ] in payload["events"]
    assert ["tree", "/fallback-a"] in payload["events"]


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_explicit_workspace_switch_invalidates_an_inflight_recovery_response():
    load_dir = _extract_async_function(WORKSPACE_JS, "loadDir")
    switch_workspace = _extract_async_function(PANELS_JS, "switchToWorkspace")
    script = f"""
const events=[];
const window={{_newChatOnWorkspaceSwitch:false}};
const S={{
  session:{{session_id:'sid-1',workspace:'/deleted',model:'m',model_provider:'p'}},
  messages:[],busy:false,_dirCache:{{}},_expandedDirs:new Set()
}};
let _wsTreeGen=0;
let listCalls=0;
let resolveOldList;
const oldList=new Promise(resolve=>{{resolveOldList=resolve;}});
function bumpWorkspaceTreeGen(){{_wsTreeGen+=1;return _wsTreeGen;}}
function _restoreExpandedDirs(){{}}
function _workspaceRouteForPath(){{return '';}}
async function api(path){{
  if(path.startsWith('/api/list')){{
    listCalls+=1;
    if(listCalls===1)return oldList;
    return {{entries:[{{name:'from-c'}}],workspace:'/explicit-c',workspace_recovered:false}};
  }}
  if(path==='/api/session/update'){{events.push(['update']);return {{session:{{}}}};}}
  throw new Error('unexpected api '+path);
}}
function renderBreadcrumb(){{}}
function renderFileTree(){{events.push(['tree',S.session.workspace]);}}
function renderSessionArtifacts(){{}}
function clearPreview(){{}}
function syncWorkspaceDisplays(){{events.push(['display',S.session.workspace]);}}
function syncTerminalButton(){{events.push(['terminal',S.session.workspace]);}}
function showToast(message,duration,kind){{events.push(['toast',message,duration||null,kind||null]);}}
function t(key,path){{return key+(path?':'+path:'');}}
function _refreshGitBadge(){{}}
function refreshOpenPreviewIfMutated(){{}}
function closeWsDropdown(){{}}
function syncTopbar(){{events.push(['topbar',S.session.workspace]);}}
function setStatus(message){{events.push(['status',message]);}}
function getWorkspaceFriendlyName(path){{return path;}}
function $(id){{return null;}}
let _currentPanel='workspace';
{load_dir}
{switch_workspace}
(async()=>{{
  const staleRequest=loadDir('.');
  await Promise.resolve();
  await switchToWorkspace('/explicit-c','C');
  resolveOldList({{entries:[{{name:'from-a'}}],workspace:'/fallback-a',workspace_recovered:true}});
  await staleRequest;
  process.stdout.write(JSON.stringify({{
    workspace:S.session.workspace,entries:S.entries,events,treeGen:_wsTreeGen,listCalls
  }}));
}})().catch(err=>{{console.error(err);process.exit(1);}});
"""
    payload = _run_node(script)

    assert payload["workspace"] == "/explicit-c"
    assert payload["entries"] == [{"name": "from-c"}]
    assert payload["treeGen"] == 1
    assert payload["listCalls"] == 2
    assert not any(
        event[0] == "toast" and "workspace_recovered_notice" in event[1]
        for event in payload["events"]
    )
