"""Mixed-ingress profile-switch ownership regression for PR #6828."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> dict:
    result = subprocess.run(
        [NODE], input=source, cwd=ROOT, capture_output=True,
        encoding="utf-8", text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_mixed_profile_switch_ingresses_publish_only_final_owner():
    source = f"""
const uiSrc = {UI_JS!r};
const panelsSrc = {PANELS_JS!r};
const sessionsSrc = {SESSIONS_JS!r};
function extractFunc(src, name) {{
  const plain='function '+name, asyncName='async function '+name;
  const start=src.includes(asyncName)?src.indexOf(asyncName):src.indexOf(plain);
  if(start<0) throw new Error(name+' not found');
  let i=src.indexOf('{{',start),depth=1;i++;
  while(depth>0&&i<src.length){{if(src[i]==='{{')depth++;else if(src[i]==='}}')depth--;i++;}}
  return src.slice(start,i);
}}
function deferred(){{let resolve;const promise=new Promise(r=>{{resolve=r;}});return{{promise,resolve}};}}
async function waitFor(predicate,label){{
  for(let i=0;i<200;i++){{if(predicate())return;await Promise.resolve();}}
  throw new Error('timed out waiting for '+label);
}}
function makeEl(){{return{{style:{{}},disabled:false,classList:{{add(){{}},remove(){{}},toggle(){{}}}},setAttribute(){{}},querySelectorAll(){{return[];}}}};}}
const els={{composerReasoningWrap:makeEl(),composerReasoningLabel:makeEl(),composerReasoningChip:makeEl(),composerMobileReasoningAction:makeEl()}};
global.$=id=>els[id]||null;
global.document={{title:''}};
global.localStorage={{removeItem(){{}}}};
global.t=(_,name)=>name||'';
global.assistantDisplayName=()=> 'Hermes';
global._highlightReasoningOption=()=>{{}};
global._applyReasoningOptions=()=>{{}};
global._applyReasoningChip=()=>{{}};
global._applyModelToDropdown=model=>model;
global._modelStateForSelect=(_,model)=>({{model,model_provider:null}});
global._invalidateSessionListRenders=()=>{{}};
global._setProfileSwitchListEmbargo=()=>{{}};
global.showSessionListSkeleton=()=>{{}};
global.bumpWorkspaceTreeGen=()=>{{}};
global.closeSessionActionMenu=()=>{{}};
global.applyBotName=()=>{{}};
global._clearPersistedModelState=()=>{{}};
global._resetCronUnreadForProfileSwitch=()=>{{}};
global.animateNextSessionListRefresh=()=>{{}};
global.showToast=()=>{{}};
global._profileSwitchPanelLoad=async()=>{{}};
global._refreshProfileSwitchBackground=()=>{{}};
global._profileSwitchOpeningExistingSession=false;
global._workspacePanelMode='closed';
global._renamingSid=null;
var _profileSwitchGeneration=0;
var _skillsData=null,_workspaceList=null;
var _currentReasoningEffort=null,_currentReasoningEffortsSupported=null,_currentReasoningToggleSupported=undefined;
var _profileTransitionReasoningContext=null,_lastReasoningFetchKey=null,_reasoningFetchSeq=0;
var _profileTransitionOwnerSeq=0,_profileTransitionOwner=null,_profileTransitionPostTail=Promise.resolve();
eval(extractFunc(uiSrc,'_beginProfileTransitionOwner'));
eval(extractFunc(uiSrc,'_acceptProfileTransitionOwner'));
eval(extractFunc(uiSrc,'_cancelProfileTransitionOwner'));
eval(extractFunc(uiSrc,'_isProfileTransitionOwner'));
eval(extractFunc(uiSrc,'_currentProfileTransitionOwner'));
eval(extractFunc(uiSrc,'_postProfileTransition'));
eval(extractFunc(uiSrc,'fetchReasoningChip'));
eval(extractFunc(uiSrc,'refreshReasoningPreferencesForRender'));
eval(extractFunc(uiSrc,'refreshProfileTransitionReasoningChip'));
eval(extractFunc(panelsSrc,'switchToProfile'));
eval(extractFunc(sessionsSrc,'_switchProfileForSessionLoad'));

async function scenario(finalOwner){{
  global.S={{activeProfile:'default',activeProfileIsDefault:true,session:{{session_id:'existing',profile:'default'}},messages:[{{role:'user',content:'x'}}]}};
  global.window={{}};
  const posts=new Map(),prefs=new Map();
  const events={{profiles:[],defaults:[],commentary:[],sse:[],lists:[],sessions:[]}};
  let active='default';
  Object.defineProperty(S,'activeProfile',{{get(){{return active;}},set(v){{active=v;events.profiles.push(v);}},configurable:true}});
  let defaultModel=null;
  Object.defineProperty(window,'_defaultModel',{{get(){{return defaultModel;}},set(v){{defaultModel=v;events.defaults.push(v);}},configurable:true}});
  let commentary=null;
  Object.defineProperty(window,'_showCommentary',{{get(){{return commentary;}},set(v){{commentary=v;events.commentary.push({{value:v,owner:_currentProfileTransitionOwner()?.profile||null}});}},configurable:true}});
  global.startGatewaySSE=()=>events.sse.push(S.activeProfile);
  global.syncTopbar=()=>{{}};
  global.renderSessionList=async()=>{{events.lists.push(S.activeProfile);}};
  global.newSession=async()=>{{events.sessions.push(S.activeProfile);S.session={{session_id:'new-'+S.activeProfile,profile:S.activeProfile}};}};
  global.api=(url,opts)=>{{
    if(url==='/api/profile/switch'){{const name=JSON.parse(opts.body).name,d=deferred();posts.set(name,d);return d.promise;}}
    if(url.startsWith('/api/reasoning')){{const model=new URL('https://x'+url).searchParams.get('model'),d=deferred();prefs.set(model,d);return d.promise;}}
    throw new Error('unexpected API '+url);
  }};
  let canonical,recovery;
  if(finalOwner==='A'){{
    canonical=switchToProfile('B');
    await waitFor(()=>posts.has('B'),'B profile POST');
    recovery=_switchProfileForSessionLoad('A');
  }}else{{
    recovery=_switchProfileForSessionLoad('A');
    await waitFor(()=>posts.has('A'),'A profile POST');
    canonical=switchToProfile('B');
  }}
  if(finalOwner==='A'){{
    posts.get('B').resolve({{active:'B',is_default:false,default_model:'B-model'}});
    await waitFor(()=>posts.has('A'),'A profile POST');
    posts.get('A').resolve({{active:'A',is_default:false,default_model:'A-model'}});
    await waitFor(()=>prefs.has('A-model'),'A preference request');
    prefs.get('A-model').resolve({{show_commentary:false,reasoning_effort:''}});
  }}else{{
    posts.get('A').resolve({{active:'A',is_default:false,default_model:'A-model'}});
    await waitFor(()=>posts.has('B'),'B profile POST');
    posts.get('B').resolve({{active:'B',is_default:false,default_model:'B-model'}});
    await waitFor(()=>prefs.has('B-model'),'B preference request');
    prefs.get('B-model').resolve({{show_commentary:true,reasoning_effort:''}});
  }}
  const [canonicalResult,recoveryResult]=await Promise.all([canonical,recovery]);
  return{{finalOwner,active:S.activeProfile,defaultModel:window._defaultModel,commentary:window._showCommentary,session:S.session?.session_id,canonicalResult,recoveryOwner:recoveryResult?.profile||null,events}};
}}
async function preferenceOwnerFenceWithoutSequenceHelp(){{
  global.S={{activeProfile:'default'}};
  global.window={{_showCommentary:'sentinel'}};
  const response=deferred();
  global.api=()=>response.promise;
  const ownerA=_beginProfileTransitionOwner('A','test-A');
  _acceptProfileTransitionOwner(ownerA,'A');
  const pending=fetchReasoningChip('?model=A-model',ownerA);
  const ownerB=_beginProfileTransitionOwner('B','test-B');
  _acceptProfileTransitionOwner(ownerB,'B');
  response.resolve({{show_commentary:true,reasoning_effort:''}});
  const applied=await pending;
  return{{applied,commentary:window._showCommentary,currentOwner:_currentProfileTransitionOwner()?.profile,ownerB:ownerB.profile}};
}}
(async()=>{{console.log(JSON.stringify({{
  recoveryWins:await scenario('A'),
  canonicalWins:await scenario('B'),
  preferenceFence:await preferenceOwnerFenceWithoutSequenceHelp(),
}}));}})().catch(e=>{{console.error(e);process.exit(1);}});
"""
    payload = _run_node(source)
    recovery = payload["recoveryWins"]
    assert recovery["active"] == "A"
    assert recovery["defaultModel"] == "A-model"
    assert recovery["commentary"] is False
    assert recovery["session"] == "existing"
    assert recovery["canonicalResult"] is False
    assert recovery["recoveryOwner"] == "A"
    assert recovery["events"]["profiles"] == ["A"]
    assert recovery["events"]["defaults"] == ["A-model"]
    assert recovery["events"]["sse"] == ["A"]
    assert recovery["events"]["lists"] == ["A"]
    assert recovery["events"]["sessions"] == []
    assert recovery["events"]["commentary"][-1] == {"value": False, "owner": "A"}

    canonical = payload["canonicalWins"]
    assert canonical["active"] == "B"
    assert canonical["defaultModel"] == "B-model"
    assert canonical["commentary"] is True
    assert canonical["session"] == "new-B"
    assert canonical["canonicalResult"] is True
    assert canonical["recoveryOwner"] is None
    assert canonical["events"]["profiles"] == ["B"]
    assert canonical["events"]["defaults"] == ["B-model"]
    assert canonical["events"]["sse"] == ["B"]
    assert canonical["events"]["lists"] == ["B"]
    assert canonical["events"]["sessions"] == ["B"]
    assert canonical["events"]["commentary"][-1] == {"value": True, "owner": "B"}

    assert payload["preferenceFence"] == {
        "applied": False,
        "commentary": "sentinel",
        "currentOwner": "B",
        "ownerB": "B",
    }


def test_stale_profile_panel_response_cannot_publish_cache_or_dom():
    source = f"""
const uiSrc={UI_JS!r},panelsSrc={PANELS_JS!r};
function extractFunc(src,name){{
  const marker='function '+name,asyncMarker='async function '+name;
  const start=src.includes(asyncMarker)?src.indexOf(asyncMarker):src.indexOf(marker);
  if(start<0)throw new Error(name+' not found');
  let i=src.indexOf('{{',start),depth=1;i++;
  while(depth>0&&i<src.length){{if(src[i]==='{{')depth++;else if(src[i]==='}}')depth--;i++;}}
  return src.slice(start,i);
}}
var _profileTransitionOwnerSeq=0,_profileTransitionOwner=null,_profileTransitionPostTail=Promise.resolve();
eval(extractFunc(uiSrc,'_beginProfileTransitionOwner'));
eval(extractFunc(uiSrc,'_acceptProfileTransitionOwner'));
eval(extractFunc(uiSrc,'_isProfileTransitionOwner'));
eval(extractFunc(panelsSrc,'_profilePanelTransitionCurrent'));
eval(extractFunc(panelsSrc,'loadSkills'));
let resolveSkills;
global.api=()=>new Promise(resolve=>{{resolveSkills=resolve;}});
global.$=()=>({{innerHTML:''}});
global.renderSkills=skills=>published.push(skills.map(s=>s.name));
var _skillsData=null;
var _collapsedCats=new Set();
const published=[];
(async()=>{{
  const ownerB=_beginProfileTransitionOwner('B','canonical');
  _acceptProfileTransitionOwner(ownerB,'B');
  const pending=loadSkills(ownerB);
  const ownerA=_beginProfileTransitionOwner('A','session-load');
  _acceptProfileTransitionOwner(ownerA,'A');
  resolveSkills({{skills:[{{name:'B-only',category:'test'}}]}});
  await pending;
  console.log(JSON.stringify({{
    currentOwner:_profileTransitionOwner.profile,
    ownerA:ownerA.profile,
    cache:_skillsData,
    published,
  }}));
}})().catch(e=>{{console.error(e);process.exit(1);}});
"""
    assert _run_node(source) == {
        "currentOwner": "A",
        "ownerA": "A",
        "cache": None,
        "published": [],
    }


def test_every_profile_panel_loader_fences_async_publication():
    def function_source(source: str, name: str) -> str:
        markers = (f"async function {name}", f"function {name}")
        start = next((source.index(m) for m in markers if m in source), -1)
        assert start >= 0, name
        params_end = source.index(")", start)
        brace = source.index("{", params_end)
        depth = 1
        pos = brace + 1
        while depth and pos < len(source):
            if source[pos] == "{":
                depth += 1
            elif source[pos] == "}":
                depth -= 1
            pos += 1
        return source[start:pos]

    for name in (
        "loadSkills", "loadCronProfiles", "loadCrons", "loadKanban",
        "loadKanbanStats", "loadKanbanBoards", "loadNotesSources",
        "loadMemory", "loadWorkspaceList", "loadWorkspacesPanel",
        "loadProfilesPanel", "_profileSwitchPanelLoad",
        "_refreshProfileSwitchBackground",
    ):
        assert "_profilePanelTransitionCurrent" in function_source(PANELS_JS, name), name

    model_loader = function_source(UI_JS, "populateModelDropdown")
    assert "transitionOwner" in model_loader
    assert "transitionCurrent()" in model_loader

    live_models = function_source(UI_JS, "_fetchLiveModels")
    assert "transitionOwner" in live_models
    assert "transitionCurrent()" in live_models

    for name in ("newSession", "_runRenderSessionListRefresh", "loadSession", "_ensureMessagesLoaded"):
        nested = function_source(SESSIONS_JS, name)
        assert "transitionOwner" in nested, name

    workspace_loader = function_source(WORKSPACE_JS, "loadDir")
    assert "transitionOwner" in workspace_loader
    assert "transitionCurrent()" in workspace_loader

    assert "transitionOwner" in BOOT_JS[BOOT_JS.index("const _hydrateModelDropdown="):BOOT_JS.index("window._modelDropdownReady=null", BOOT_JS.index("const _hydrateModelDropdown="))]
