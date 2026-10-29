"""Focused browser-state checks for workspace paging consumers."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(name):
    marker = f"async function {name}("
    start = WORKSPACE_JS.index(marker)
    brace = WORKSPACE_JS.index("{", WORKSPACE_JS.index(")", start))
    depth = 0
    for index in range(brace, len(WORKSPACE_JS)):
        if WORKSPACE_JS[index] == "{":
            depth += 1
        elif WORKSPACE_JS[index] == "}":
            depth -= 1
            if depth == 0:
                return WORKSPACE_JS[start:index + 1]
    raise AssertionError(f"Could not find the end of {name}")


def _extract_plain_function(name):
    marker = f"function {name}("
    start = WORKSPACE_JS.index(marker)
    brace = WORKSPACE_JS.index("{", WORKSPACE_JS.index(")", start))
    depth = 0
    for index in range(brace, len(WORKSPACE_JS)):
        if WORKSPACE_JS[index] == "{":
            depth += 1
        elif WORKSPACE_JS[index] == "}":
            depth -= 1
            if depth == 0:
                return WORKSPACE_JS[start:index + 1]
    raise AssertionError(f"Could not find the end of {name}")


def _run_node(script):
    if not NODE:
        pytest.skip("Node.js is unavailable")
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_prefetch_stops_when_workspace_owner_changes_between_pages():
    fetch_all_pages = _extract_function("_fetchAllPages")
    _run_node("""
let profile='profile-a';
const urls=[];
async function api(url){
  urls.push(url);
  profile='profile-b';
  return {entries:[{path:'first'}],has_more:true,cursor:'page-two'};
}
""" + fetch_all_pages + """
(async()=>{
  const result=await _fetchAllPages('/api/list?session_id=s&path=.',()=>profile==='profile-a');
  if(result!==null)throw new Error('stale page chain returned entries');
  if(urls.length!==1)throw new Error(`stale page chain made ${urls.length} requests`);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_load_more_appends_unique_entries_and_uses_the_cursor():
    normalize_path = _extract_plain_function("_normalizeWorkspaceRelPath")
    route_for_path = _extract_plain_function("_workspaceRouteForPath")
    route_for_path_rel = _extract_plain_function("_workspaceRouteForPathRel")
    request_is_current = _extract_plain_function("_workspaceRequestIsCurrent")
    load_more = _extract_function("_loadMoreDir")
    _run_node("""
const S={
  session:{session_id:'session-a',profile:'profile-a',workspace:'/workspace'},
  activeProfile:'profile-a',currentDir:'.',_dirCursor:'cursor-one',_dirHasMore:true,
  _dirLoadingMore:false,entries:[{path:'kept.txt'}],_escapeGrants:{},
};
let _wsTreeGen=4;
const urls=[];
let renders=0;
function _workspaceEscapeGrantForPath(){return null;}
function $(id){return null;}
function renderFileTree(){renders+=1;}
function _workspaceShowListingFailure(error){throw error;}
async function api(url){
  urls.push(url);
  return {entries:[{path:'kept.txt'},{path:'added.txt'}],has_more:false,cursor:null};
}
""" + normalize_path + route_for_path_rel + route_for_path + request_is_current + load_more + """
(async()=>{
  await _loadMoreDir();
  if(urls.length!==1||!urls[0].includes('cursor=cursor-one'))throw new Error('cursor was not sent');
  if(JSON.stringify(S.entries.map(entry=>entry.path))!==JSON.stringify(['kept.txt','added.txt']))throw new Error('entries were not deduplicated and appended');
  if(S._dirHasMore||S._dirCursor!==null||S._dirLoadingMore)throw new Error('completed paging state was not cleared');
  if(renders!==1)throw new Error('the file tree was not repainted');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_load_more_discards_response_after_profile_switch():
    normalize_path = _extract_plain_function("_normalizeWorkspaceRelPath")
    route_for_path = _extract_plain_function("_workspaceRouteForPath")
    route_for_path_rel = _extract_plain_function("_workspaceRouteForPathRel")
    request_is_current = _extract_plain_function("_workspaceRequestIsCurrent")
    load_more = _extract_function("_loadMoreDir")
    _run_node("""
const S={
  session:{session_id:'session-a',profile:'profile-a',workspace:'/workspace'},
  activeProfile:'profile-a',currentDir:'.',_dirCursor:'cursor-one',_dirHasMore:true,
  _dirLoadingMore:false,entries:[{path:'kept.txt'}],_escapeGrants:{},
};
let _wsTreeGen=4;
let renders=0;
function _workspaceEscapeGrantForPath(){return null;}
function $(id){return null;}
function renderFileTree(){renders+=1;}
function _workspaceShowListingFailure(error){throw error;}
async function api(){
  S.activeProfile='profile-b';
  _wsTreeGen+=1;
  S._dirCursor=null;
  S._dirHasMore=false;
  S._dirLoadingMore=false;
  return {entries:[{path:'stale.txt'}],has_more:false,cursor:null};
}
""" + normalize_path + route_for_path_rel + route_for_path + request_is_current + load_more + """
(async()=>{
  await _loadMoreDir();
  if(JSON.stringify(S.entries.map(entry=>entry.path))!==JSON.stringify(['kept.txt']))throw new Error('stale page overwrote the new profile');
  if(renders!==0)throw new Error('stale page repainted the new profile');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
