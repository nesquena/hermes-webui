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
let _wsTreeLoadSeq=0;
let _wsTreeEntriesSeq=0;
let _wsTreeCacheEpoch=0;
const _wsTreeCacheListedAt=new Map();
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
let _wsTreeLoadSeq=0;
let _wsTreeEntriesSeq=0;
let _wsTreeCacheEpoch=0;
const _wsTreeCacheListedAt=new Map();
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


def _expanded_folder_refresh_race(order: str) -> dict:
    """Drive the real loadDir: an older root load (A) and a refresh (B) over one expanded folder.

    A gets its root listing, renders it, and waits on its prefetch of `docs`. B is the
    refresh after a workspace write: it clears the folder cache and waits on its own root
    listing. `order` says which arrives next: A's `docs` listing (requested before the
    write) or B's root listing. A may still show its listing, but B fetches the folder.
    """
    load_dir = _extract_async_function(WORKSPACE_JS, "loadDir")
    script = f"""
const S={{session:{{session_id:'sid-1',workspace:'/ws'}},_dirCache:{{}},_expandedDirs:new Set(['docs'])}};
let _wsTreeGen=0;
let _wsTreeLoadSeq=0;
let _wsTreeEntriesSeq=0;
let _wsTreeCacheEpoch=0;
const _wsTreeCacheListedAt=new Map();
const calls={{root:0,docs:0}};
let resolveADocs, resolveBRoot;
function _restoreExpandedDirs(){{}}
function _workspaceRouteForPath(path){{return path&&path!=='.'?'/dir/'+path:'';}}
async function api(url){{
  if(url==='/dir/docs'){{
    calls.docs+=1;
    if(calls.docs===1)return new Promise(resolve=>{{resolveADocs=resolve;}});
    return {{entries:[{{name:'after_write.txt'}}]}};
  }}
  if(url.startsWith('/api/list')){{
    calls.root+=1;
    if(calls.root===1)return {{entries:[{{name:'root-before-write'}}]}};
    return new Promise(resolve=>{{resolveBRoot=resolve;}});
  }}
  throw new Error('unexpected api '+url);
}}
function renderBreadcrumb(){{}}
function renderFileTree(){{}}
function renderSessionArtifacts(){{}}
function clearPreview(){{}}
function refreshOpenPreviewIfMutated(){{}}
function showToast(){{}}
function t(key){{return key;}}
function _refreshGitBadge(){{}}
function $(){{return null;}}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
{load_dir}
(async()=>{{
  const loadA=loadDir('.');
  await settle();  // A has rendered its root and is waiting on its docs prefetch
  const loadB=loadDir('.',{{refreshExpanded:true}});
  await settle();  // B has cleared the cache and is waiting on its root listing
  if({order!r}==='a-docs-first'){{
    resolveADocs({{entries:[{{name:'before_write.txt'}}]}});
    await settle();
    resolveBRoot({{entries:[{{name:'root-after-write'}}]}});
  }}else{{
    resolveBRoot({{entries:[{{name:'root-after-write'}}]}});
    await settle();
    resolveADocs({{entries:[{{name:'before_write.txt'}}]}});
  }}
  await Promise.all([loadA,loadB]);
  process.stdout.write(JSON.stringify({{entries:S.entries,docs:S._dirCache.docs||null,calls}}));
}})().catch(err=>{{console.error(err);process.exit(1);}});
"""
    return _run_node(script)


@pytest.mark.skipif(NODE is None, reason="node is required")
@pytest.mark.parametrize("order", ["a-docs-first", "b-root-first"])
def test_refresh_refetches_a_folder_an_older_load_listed_before_its_clear(order):
    payload = _expanded_folder_refresh_race(order)

    assert payload["entries"] == [{"name": "root-after-write"}]
    # The folder shows what it holds after the write, from the refresh's own request.
    assert payload["docs"] == [{"name": "after_write.txt"}]
    assert payload["calls"] == {"root": 2, "docs": 2}


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_folder_listing_requested_after_a_newer_refresh_cleared_the_cache_is_kept():
    """A refresh (B) starts while an older root load (A) is still waiting on its root.

    A's folder request goes out after B cleared the cache, so it sees the same state B
    will. A keeps it, its completing render shows the folder (and keeps keyboard focus
    inside it), and B does not fetch the folder again.
    """
    load_dir = _extract_async_function(WORKSPACE_JS, "loadDir")
    script = f"""
const S={{session:{{session_id:'sid-1',workspace:'/ws'}},_dirCache:{{}},_expandedDirs:new Set(['docs'])}};
let _wsTreeGen=0;
let _wsTreeLoadSeq=0;
let _wsTreeEntriesSeq=0;
let _wsTreeCacheEpoch=0;
const _wsTreeCacheListedAt=new Map();
const calls={{root:0,docs:0}};
let resolveARoot, resolveBRoot;
function _restoreExpandedDirs(){{}}
function _workspaceRouteForPath(path){{return path&&path!=='.'?'/dir/'+path:'';}}
async function api(url){{
  if(url==='/dir/docs'){{calls.docs+=1;return {{entries:[{{name:'current.txt'}}]}};}}
  if(url.startsWith('/api/list')){{
    calls.root+=1;
    if(calls.root===1)return new Promise(resolve=>{{resolveARoot=resolve;}});
    return new Promise(resolve=>{{resolveBRoot=resolve;}});
  }}
  throw new Error('unexpected api '+url);
}}
function renderBreadcrumb(){{}}
function renderFileTree(){{}}
function renderSessionArtifacts(){{}}
function clearPreview(){{}}
function refreshOpenPreviewIfMutated(){{}}
function showToast(){{}}
function t(key){{return key;}}
function _refreshGitBadge(){{}}
function $(){{return null;}}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
{load_dir}
(async()=>{{
  const loadA=loadDir('.');
  await settle();
  const loadB=loadDir('.',{{refreshExpanded:true}});
  await settle();  // B has cleared the cache; both root listings are pending
  resolveARoot({{entries:[{{name:'root'}}]}});
  await loadA;     // A rendered its root, then asked for docs after B's clear
  const docsAfterA=S._dirCache.docs||null;
  resolveBRoot({{entries:[{{name:'root'}}]}});
  await loadB;
  process.stdout.write(JSON.stringify({{docsAfterA,docs:S._dirCache.docs||null,calls}}));
}})().catch(err=>{{console.error(err);process.exit(1);}});
"""
    payload = _run_node(script)

    assert payload["docsAfterA"] == [{"name": "current.txt"}]
    assert payload["docs"] == [{"name": "current.txt"}]
    assert payload["calls"] == {"root": 2, "docs": 1}


@pytest.mark.skipif(NODE is None, reason="node is required")
@pytest.mark.parametrize("order", ["b-docs-first", "a-docs-first"])
def test_folder_listing_from_before_a_recovery_clear_never_outlives_it(order):
    """B renders and asks for `docs`; then the older load A's root reports a workspace
    recovery, clears the cache again and asks for `docs` in the recovered workspace.
    Whichever `docs` listing lands first, the folder ends up with A's, requested after
    the latest clear."""
    load_dir = _extract_async_function(WORKSPACE_JS, "loadDir")
    script = f"""
const S={{session:{{session_id:'sid-1',workspace:'/ws'}},_dirCache:{{}},_expandedDirs:new Set(['docs'])}};
let _wsTreeGen=0;
let _wsTreeLoadSeq=0;
let _wsTreeEntriesSeq=0;
let _wsTreeCacheEpoch=0;
const _wsTreeCacheListedAt=new Map();
const calls={{root:0,docs:0}};
let resolveARoot, resolveADocs, resolveBDocs;
function _restoreExpandedDirs(){{}}
function _workspaceRouteForPath(path){{return path&&path!=='.'?'/dir/'+path:'';}}
async function api(url){{
  if(url==='/dir/docs'){{
    calls.docs+=1;
    if(calls.docs===1)return new Promise(resolve=>{{resolveBDocs=resolve;}});
    return new Promise(resolve=>{{resolveADocs=resolve;}});
  }}
  if(url.startsWith('/api/list')){{
    calls.root+=1;
    if(calls.root===1)return new Promise(resolve=>{{resolveARoot=resolve;}});
    return {{entries:[{{name:'root'}}]}};
  }}
  throw new Error('unexpected api '+url);
}}
function renderBreadcrumb(){{}}
function renderFileTree(){{}}
function renderSessionArtifacts(){{}}
function clearPreview(){{}}
function refreshOpenPreviewIfMutated(){{}}
function showToast(){{}}
function t(key){{return key;}}
function _refreshGitBadge(){{}}
function $(){{return null;}}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
{load_dir}
(async()=>{{
  const loadA=loadDir('.');
  await settle();
  const loadB=loadDir('.');
  await settle();  // B rendered and is waiting on docs from the old workspace
  resolveARoot({{entries:[{{name:'root'}}],workspace:'/fallback',workspace_recovered:true}});
  await settle();  // A cleared the cache again and is waiting on docs in /fallback
  const old={{entries:[{{name:'old-workspace.txt'}}]}};
  const recovered={{entries:[{{name:'recovered.txt'}}]}};
  if({order!r}==='b-docs-first'){{
    resolveBDocs(old);
    await settle();
    resolveADocs(recovered);
  }}else{{
    resolveADocs(recovered);
    await settle();
    resolveBDocs(old);
  }}
  await Promise.all([loadA,loadB]);
  process.stdout.write(JSON.stringify({{workspace:S.session.workspace,docs:S._dirCache.docs||null,calls}}));
}})().catch(err=>{{console.error(err);process.exit(1);}});
"""
    payload = _run_node(script)

    assert payload["workspace"] == "/fallback"
    assert payload["docs"] == [{"name": "recovered.txt"}]
    assert payload["calls"] == {"root": 2, "docs": 2}
