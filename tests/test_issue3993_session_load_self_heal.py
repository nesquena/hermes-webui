"""Regression coverage for preserving a boot restore after transient errors."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = REPO / "static" / "sessions.js"
NODE = shutil.which("node")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _run_load_session_failures(*, statuses, sid, route, saved, current_sid=None,
                               run_boot_catch=False, switch_status=None) -> dict:
    """Run the real loadSession() error path with browser-owned restore state."""
    assert NODE, "node is required"
    js = _read(SESSIONS_JS)
    load_start = js.index("async function loadSession(sid){")
    load_end = js.index("\n// ── Handoff hint logic", load_start)
    route_start = js.index("function _sessionIdFromLocation(){")
    route_end = js.index("\nfunction _composerPrefillIntentFromLocation", route_start)
    mismatch_start = js.index("function _sessionProfileMismatchFromError(e){")
    mismatch_end = js.index("\n}", mismatch_start) + 2

    boot = _read(REPO / "static" / "boot.js")
    load_call = boot.index("await loadSession(saved, {preserveActiveInput:true});")
    catch_start = boot.index("\n    catch(", load_call)
    catch_open = boot.index("{", catch_start)
    catch_close = boot.index("}", catch_open)
    spec = json.dumps({
        "load": js[load_start:load_end],
        "routeParser": js[route_start:route_end],
        "profileMismatchParser": js[mismatch_start:mismatch_end],
        "bootCatch": boot[catch_open + 1:catch_close],
        "statuses": statuses,
        "sid": sid,
        "route": route,
        "saved": saved,
        "currentSid": current_sid,
        "runBootCatch": run_boot_catch,
        "switchStatus": switch_status,
    })
    script = f"""
const vm = require('node:vm');
const spec = {spec};
const location = new URL(spec.route, 'http://example.test');
const replaced = [];
const history = {{replaceState(_state, _title, path){{
  replaced.push(path);
  const next = new URL(path, location.origin);
  location.pathname = next.pathname;
  location.search = next.search;
  location.hash = next.hash;
}}}};
const values = new Map();
if (spec.saved !== null) values.set('hermes-webui-session', spec.saved);
const localStorage = {{
  getItem(key){{return values.has(key) ? values.get(key) : null;}},
  setItem(key, value){{values.set(key, String(value));}},
  removeItem(key){{values.delete(key);}}
}};
const requests = [];
const errors = [];
const startedSessionStreams = [];
const noop = () => {{}};
const msgInner = {{innerHTML:''}};
const context = vm.createContext({{
  S: {{session:spec.currentSid ? {{session_id:spec.currentSid}} : null, messages:[], toolCalls:[]}},
  INFLIGHT:{{}},
  window: {{location, history}}, history, localStorage, URL, URLSearchParams,
  _loadingSessionId:null, _loadSessionGeneration:0,
  _appRootPath:()=> '/',
  _switchProfileForSessionLoad:async()=>{{const error=new Error('profile switch failed');error.status=spec.switchStatus;throw error;}},
  _rearmActiveSessionStream:noop, _updateYoloPill:noop,
  stopApprovalPolling:noop, hideApprovalCard:noop, clearCompressionUi:noop,
  _clearSameSessionForceReloadHint:noop, showToast:noop,
  startSessionStream:(sid)=>startedSessionStreams.push(sid),
  _saveComposerDraftNow:async()=>{{}},
  $:(id)=>id==='msgInner' ? msgInner : {{value:''}},
  api:async(path)=>{{
    requests.push(new URL(path, location.origin).searchParams.get('session_id'));
    const error=new Error('metadata failed');
    error.status=spec.statuses[requests.length-1];
    if(error.status===409) error.body=JSON.stringify({{code:'session_profile_mismatch',profile:'other-profile'}});
    if(error.status===401) return undefined;
    throw error;
  }}
}});
vm.runInContext(spec.profileMismatchParser + '\\n' + spec.routeParser + '\\n' + spec.load, context);
(async()=>{{
  for(let i=0;i<spec.statuses.length;i++){{
    const retrySid=context._sessionIdFromLocation() || localStorage.getItem('hermes-webui-session');
    const requestedSid=i===0 ? spec.sid : retrySid;
    if(!requestedSid) break;
    try{{await context.loadSession(requestedSid);}}
    catch(error){{
      errors.push({{status:error.status||null, message:error.message}});
      if(spec.runBootCatch) new Function('localStorage','e',spec.bootCatch)(localStorage,error);
    }}
  }}
  process.stdout.write(JSON.stringify({{
    requests,
    errors,
    activeSid:context.S.session&&context.S.session.session_id||null,
    startedSessionStreams,
    saved:localStorage.getItem('hermes-webui-session'),
    routeSid:context._sessionIdFromLocation(),
    pathname:location.pathname,
    replaced
  }}));
}})().catch(error=>{{process.stderr.write(error.stack);process.exit(1);}});
"""
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, f"node failed: {out.stderr}"
    return json.loads(out.stdout)


def _run_continuation_restore(responses, *, attempts=1, current_sid=None,
                              lineage_parent=None, lineage_child=None) -> dict:
    """Run loadSession() through continuation metadata and composer restoration."""
    assert NODE, "node is required"
    js = _read(SESSIONS_JS)
    load_start = js.index("async function loadSession(sid){")
    load_end = js.index("\n// ── Handoff hint logic", load_start)
    route_start = js.index("function _sessionIdFromLocation(){")
    route_end = js.index("\nfunction _composerPrefillIntentFromLocation", route_start)
    mismatch_start = js.index("function _sessionProfileMismatchFromError(e){")
    mismatch_end = js.index("\n}", mismatch_start) + 2
    draft_start = js.index("function _restoreComposerDraft(draft, targetSid, opts={}) {")
    draft_end = js.index("\n}\n\n// Clear the saved draft", draft_start) + 2
    spec = json.dumps({
        "load": js[load_start:load_end],
        "routeParser": js[route_start:route_end],
        "profileMismatchParser": js[mismatch_start:mismatch_end],
        "draftRestore": js[draft_start:draft_end],
        "responses": responses,
        "attempts": attempts,
        "currentSid": current_sid,
        "lineageParent": lineage_parent,
        "lineageChild": lineage_child,
    })
    script = f"""
const vm=require('node:vm');
const spec={spec};
const location=new URL('/session/parent-session','http://example.test');
const history={{pushState(_state,_title,path){{
  const next=new URL(path,location.origin);
  location.pathname=next.pathname;location.search=next.search;location.hash=next.hash;
}},replaceState(_state,_title,path){{
  const next=new URL(path,location.origin);
  location.pathname=next.pathname;location.search=next.search;location.hash=next.hash;
}}}};
const values=new Map([['hermes-webui-session','parent-session']]);
const localStorage={{
  getItem(key){{return values.has(key)?values.get(key):null;}},
  setItem(key,value){{values.set(key,String(value));}},
  removeItem(key){{values.delete(key);}}
}};
const composer={{value:''}};
const requests=[];
const requestStates=[];
const profileSwitches=[];
const noop=()=>{{}};
const context=vm.createContext({{
  S:{{session:spec.currentSid?{{session_id:spec.currentSid}}:null,messages:[],toolCalls:[],pendingFiles:[],activeProfile:'initial-profile'}},INFLIGHT:{{}},
  window:{{location,history}},history,localStorage,URL,URLSearchParams,
  document:{{baseURI:'http://example.test/'}},
  _loadingSessionId:null,_loadSessionGeneration:0,
  _appRootPath:()=> '/',
  _resolveSessionIdFromSidebarLineage:(sid)=>spec.lineageParent&&sid===spec.lineageParent?spec.lineageChild:sid,
  _setActiveSessionUrl(sid){{history.pushState({{session_id:sid}},'',`/session/${{encodeURIComponent(sid)}}`);}},
  _ensureMessagesLoaded:async(sid)=>{{context.S.messages=[{{role:'assistant',content:`loaded ${{sid}}`}}];}},
  _composerDraftHasPayload:(text,files)=>!!String(text||'').trim()||(Array.isArray(files)&&files.length>0),
  _isComposerDraftRestoreSuppressed:()=>false,
  _clearComposerDraftRestoreSuppression:noop,
  _rearmActiveSessionStream:noop,_updateYoloPill:noop,
  stopApprovalPolling:noop,hideApprovalCard:noop,stopSessionStream:noop,
  stopClarifyPolling:noop,hideClarifyCard:noop,clearCompressionUi:noop,
  _saveComposerDraftNow:async()=>{{}},
  _switchProfileForSessionLoad:async(profile)=>{{profileSwitches.push(profile);context.S.activeProfile=profile;}},
  _clearSameSessionForceReloadHint:noop,
  _clearDeferredActiveSessionExternalRefresh:noop,
  _resolveSessionModelForDisplaySoon:noop,
  _acknowledgeSessionVisit:noop,
  _serverLiveSnapshotInflight:()=>null,_selectLiveRecoveryInflight:()=>null,
  _mergePendingSessionMessage:()=>false,
  _applyPendingSessionModelForSession:noop,
  _deferWorkspaceRefreshForSession:noop,
  startSessionStream:noop,startApprovalPolling:noop,
  updateQueueBadge:noop,updateSendBtn:noop,setStatus:noop,setBusy:noop,
  setComposerStatus:noop,syncTopbar:noop,renderMessages:noop,
  _isMessagingSession:()=>false,_hideHandoffHint:noop,
  $:(id)=>id==='msg'?composer:(id==='msgInner'?{{innerHTML:''}}:null),
  autoResize:noop,showToast:noop,
  api:async(path)=>{{
    const sid=new URL(path,location.origin).searchParams.get('session_id');
    requests.push(sid);
    requestStates.push({{
      sid,
      saved:localStorage.getItem('hermes-webui-session'),
      routeSid:context._sessionIdFromLocation()
    }});
    const response=spec.responses.shift();
    if(!response) throw new Error('Unexpected metadata request for '+sid);
    if(response.status){{
      const error=new Error('metadata failed');error.status=response.status;
      if(response.status===409) error.body=JSON.stringify({{code:'session_profile_mismatch',profile:response.profile||'other-profile'}});
      throw error;
    }}
    return {{session:response.session}};
  }}
}});
vm.runInContext(spec.draftRestore,context);
vm.runInContext(spec.profileMismatchParser+'\\n'+spec.routeParser+'\\n'+spec.load,context);
(async()=>{{
  const errors=[];
  const attemptStates=[];
  for(let i=0;i<spec.attempts;i++){{
    const sid=context._sessionIdFromLocation()||localStorage.getItem('hermes-webui-session');
    try{{await context.loadSession(sid,{{preserveActiveInput:true}});}}
    catch(error){{errors.push(error.status||null);}}
    attemptStates.push({{
      requests:requests.slice(),
      sessionSid:context.S.session&&context.S.session.session_id||null,
      composer:composer.value,
      saved:localStorage.getItem('hermes-webui-session'),
      routeSid:context._sessionIdFromLocation()
    }});
  }}
  process.stdout.write(JSON.stringify({{
    requests,
    requestStates,
    profileSwitches,
    errors,
    attemptStates,
    sessionSid:context.S.session&&context.S.session.session_id||null,
    messages:context.S.messages,
    composer:composer.value,
    saved:localStorage.getItem('hermes-webui-session'),
    routeSid:context._sessionIdFromLocation(),
    pathname:location.pathname
  }}));
}})().catch(error=>{{process.stderr.write(error.stack);process.exit(1);}});
"""
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, f"node failed: {out.stderr}"
    return json.loads(out.stdout)


def test_transient_metadata_failure_keeps_valid_restore_for_retry():
    """A one-time server failure leaves route/localStorage available to boot again."""
    data = _run_load_session_failures(
        statuses=[500, 500], sid="valid-session", route="/session/valid-session",
        saved="valid-session",
    )
    assert data["requests"] == ["valid-session", "valid-session"]
    assert data["errors"] == []
    assert data["saved"] == "valid-session"
    assert data["routeSid"] == "valid-session"


def test_missing_continuation_restores_valid_parent_and_its_draft():
    """A dead continuation hint falls back once to its still-valid parent."""
    data = _run_continuation_restore([
        {"session": {
            "session_id": "parent-session",
            "continuation_session_id": "missing-child",
            "composer_draft": {"text": "parent draft", "files": []},
            "active_stream_id": None,
        }},
        {"status": 404},
        {"session": {
            "session_id": "parent-session",
            "continuation_session_id": "missing-child",
            "composer_draft": {"text": "parent draft", "files": []},
            "active_stream_id": None,
        }},
    ])
    assert data["errors"] == []
    assert data["requests"] == ["parent-session", "missing-child", "parent-session"]
    assert data["requestStates"] == [
        {"sid": "parent-session", "saved": "parent-session", "routeSid": "parent-session"},
        {"sid": "missing-child", "saved": "parent-session", "routeSid": "parent-session"},
        {"sid": "parent-session", "saved": "parent-session", "routeSid": "parent-session"},
    ]
    assert data["sessionSid"] == "parent-session"
    assert data["messages"] == [{"role": "assistant", "content": "loaded parent-session"}]
    assert data["composer"] == "parent draft"
    assert data["saved"] == "parent-session"
    assert data["routeSid"] == "parent-session"
    assert data["pathname"] == "/session/parent-session"


def test_lineage_resolved_parent_404_falls_back_and_restores_parent_draft():
    """A missing visible child falls back to the raw parent requested at boot."""
    parent = {
        "session_id": "parent-session",
        "composer_draft": {"text": "parent draft", "files": []},
        "active_stream_id": None,
    }
    data = _run_continuation_restore(
        [{"status": 404}, {"session": parent}],
        lineage_parent="parent-session", lineage_child="child-session",
    )
    assert data["errors"] == []
    assert data["requests"] == ["child-session", "parent-session"]
    assert data["sessionSid"] == "parent-session"
    assert data["messages"] == [{"role": "assistant", "content": "loaded parent-session"}]
    assert data["composer"] == "parent draft"
    assert data["saved"] == "parent-session"
    assert data["routeSid"] == "parent-session"
    assert data["pathname"] == "/session/parent-session"


def test_active_session_load_falls_back_to_parent_when_continuation_is_missing():
    """A missing continuation must not strand the active pane on its prior session."""
    parent = {
        "session_id": "parent-session",
        "continuation_session_id": "missing-child",
        "composer_draft": {"text": "parent draft", "files": []},
        "active_stream_id": None,
    }
    data = _run_continuation_restore([
        {"session": parent}, {"status": 404}, {"session": parent},
    ], current_sid="active-session")
    assert data["requests"] == ["parent-session", "missing-child", "parent-session"]
    assert data["sessionSid"] == "parent-session"
    assert data["messages"] == [{"role": "assistant", "content": "loaded parent-session"}]
    assert data["composer"] == "parent draft"
    assert data["saved"] == "parent-session"
    assert data["routeSid"] == "parent-session"


def test_missing_parent_after_continuation_fallback_clears_matching_restore_state():
    """P's own 404 also clears P after C's 404 while another session is active."""
    parent = {
        "session_id": "parent-session",
        "continuation_session_id": "missing-child",
        "composer_draft": {"text": "parent draft", "files": []},
        "active_stream_id": None,
    }
    data = _run_continuation_restore([
        {"session": parent}, {"status": 404}, {"status": 404},
    ], current_sid="active-a")
    assert data["requests"] == ["parent-session", "missing-child", "parent-session"]
    assert data["saved"] is None
    assert data["routeSid"] is None
    assert data["pathname"] == "/"
    assert data["sessionSid"] == "active-a"


def test_profile_retry_keeps_continuation_parent_fallback_intent():
    """A profile retry's new generation still falls back to P after C returns 404."""
    parent = {
        "session_id": "parent-session",
        "continuation_session_id": "child-session",
        "composer_draft": {"text": "parent draft", "files": []},
        "active_stream_id": None,
    }
    data = _run_continuation_restore([
        {"session": parent},
        {"status": 409, "profile": "continuation-profile"},
        {"status": 404},
        {"status": 409, "profile": "initial-profile"},
        {"session": parent},
    ], current_sid="active-session")
    assert data["requests"] == [
        "parent-session", "child-session", "child-session", "parent-session", "parent-session",
    ]
    assert data["profileSwitches"] == ["continuation-profile", "initial-profile"]
    assert data["sessionSid"] == "parent-session"
    assert data["composer"] == "parent draft"
    assert data["saved"] == "parent-session"
    assert data["routeSid"] == "parent-session"


def test_failed_continuation_metadata_stays_retryable_without_parent_fallback():
    """A transient child 500 preserves P and the next restore retries P→C."""
    parent = {
        "session_id": "parent-session",
        "continuation_session_id": "child-session",
        "composer_draft": {"text": "parent draft", "files": []},
        "active_stream_id": None,
    }
    child = {
        "session_id": "child-session",
        "composer_draft": {"text": "child draft", "files": []},
        "active_stream_id": None,
    }
    data = _run_continuation_restore([
        {"session": parent}, {"status": 500},
        {"session": parent}, {"session": child},
    ], attempts=2)
    assert data["errors"] == []
    assert data["requests"] == [
        "parent-session", "child-session", "parent-session", "child-session",
    ]
    assert data["sessionSid"] == "child-session"
    assert data["composer"] == "child draft"
    assert data["saved"] == "child-session"
    assert data["routeSid"] == "child-session"
    assert data["attemptStates"][0] == {
        "requests": ["parent-session", "child-session"],
        "sessionSid": None,
        "composer": "",
        "saved": "parent-session",
        "routeSid": "parent-session",
    }


def test_failed_profile_switch_keeps_original_metadata_status():
    """A secondary switch 404 must not turn the original metadata 409 into a
    missing-session result and clear the restore target."""
    data = _run_load_session_failures(
        statuses=[409], sid="valid-session", route="/session/valid-session",
        saved="valid-session", switch_status=404,
    )
    assert data["requests"] == ["valid-session"]
    assert data["errors"] == []
    assert data["saved"] == "valid-session"
    assert data["routeSid"] == "valid-session"


def test_unauthorized_metadata_response_keeps_restore_for_relogin():
    """The API's 401 redirect path still returns without clearing restore state."""
    data = _run_load_session_failures(
        statuses=[401], sid="valid-session", route="/session/valid-session",
        saved="valid-session",
    )
    assert data["requests"] == ["valid-session"]
    assert data["errors"] == []
    assert data["saved"] == "valid-session"
    assert data["routeSid"] == "valid-session"


def test_active_session_missing_target_clears_matching_route_and_saved_pointer():
    """An owned 404 clears its stale target even when another session is active."""
    data = _run_load_session_failures(
        statuses=[404], sid="missing-parent", route="/session/missing-parent",
        saved="missing-parent", current_sid="active-a",
    )
    assert data["requests"] == ["missing-parent"]
    assert data["saved"] is None
    assert data["routeSid"] is None
    assert data["pathname"] == "/"
    assert data["activeSid"] == "active-a"
    assert data["startedSessionStreams"] == ["active-a"]


def test_stale_load_guard_precedes_404_cleanup():
    """A superseded load must bail before mutating route or saved-session state."""
    js = _read(SESSIONS_JS)
    load_start = js.index("async function loadSession(sid){")
    load_end = js.index("\n// ── Handoff hint logic", load_start)
    block = js[load_start:load_end]
    catch_idx = block.index("} catch(e) {")
    guard = "if (!_isCurrentLoad()) {"
    clear_idx = block.index("localStorage.removeItem('hermes-webui-session')", catch_idx)
    guard_idx = block.rfind(guard, catch_idx, clear_idx)
    assert guard_idx < clear_idx
    assert "_rearmActiveSessionStream()" in block[guard_idx:guard_idx + 120]
