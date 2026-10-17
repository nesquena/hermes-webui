"""Execute the settings operation owner with deferred network responses."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _vision_source():
    source = (ROOT / "static" / "panels.js").read_text()
    return source[source.index("function _invalidateVisionCapabilityFirst(){"):source.index("async function _loadAuxiliaryModels(){")]


def _switch_source():
    source = (ROOT / "static" / "panels.js").read_text()
    start = source.index("async function switchToProfile(name) {")
    end = source.index("\n}\n", source.index("} finally {", start))
    return source[start:end + 3]


def test_vision_operations_fail_closed_and_serialize():
    if not shutil.which("node"):
        pytest.skip("node required")
    node = shutil.which("node")
    assert node is not None  # for type-checkers; guarded above
    source = _vision_source()
    script = r"""
const assert=require('node:assert/strict');
const S={activeProfile:'A'};
const cb={checked:false,disabled:true,addEventListener(_,fn){this.change=fn;}};
const $=()=>cb, t=k=>k, toasts=[], showToast=x=>toasts.push(x);
const requests=[];
function api(url,opts){return new Promise((resolve,reject)=>requests.push({opts,resolve,reject}));}
""" + source + r"""
(async()=>{
 let load=_loadVisionCapabilityFirst();
 assert.equal(cb.disabled,true);
 requests[0].reject(Error('offline')); await load;
 assert.equal(cb.disabled,true); assert.equal(cb.checked,false);
 assert.equal(toasts.at(-1),'settings_vcf_load_failed');
 load=_loadVisionCapabilityFirst();
 requests[1].resolve({vision_capability_first:true}); await load;
 assert.equal(cb.checked,true); assert.equal(cb.disabled,false);
 cb.checked=false; cb.change();
 assert.equal(cb.disabled,true);
 const save=cb._vcfPending;
 cb.checked=true; cb.change(); // synthetic change cannot bypass disabled control
 const reopen=_loadVisionCapabilityFirst();
 assert.equal(requests.length,3); // no overlapping GET or POST
 assert.deepEqual(JSON.parse(requests[2].opts.body),{enabled:false,profile:'A'});
 requests[2].reject(Error('offline')); await save; await reopen;
 assert.equal(cb.checked,true); assert.equal(cb.disabled,false);
 cb.checked=false; cb.change();
 requests[3].resolve({vision_capability_first:false}); await cb._vcfPending;
 assert.equal(cb.checked,false); assert.equal(cb.disabled,false);
 load=_loadVisionCapabilityFirst();
 requests[4].resolve({vision_capability_first:'yes'}); await load;
 assert.equal(cb.disabled,true);
 // A pending read must not be reused when Settings reopens under B.
 load=_loadVisionCapabilityFirst(); // A retry remains pending
 const staleA=load;
 S.activeProfile='B'; _invalidateVisionCapabilityFirst();
 const loadB=_loadVisionCapabilityFirst();
 assert.equal(requests.length,7);
 requests[5].resolve({vision_capability_first:false}); await staleA;
 assert.equal(cb.disabled,true); assert.equal(cb._vcfProfile,'B');
 requests[6].resolve({vision_capability_first:true}); await loadB;
 assert.equal(cb.checked,true); assert.equal(cb.disabled,false);
 // A late B save, including failure, cannot write the newly active A UI.
 cb.checked=false; cb.change();
 const saveB=cb._vcfPending;
 S.activeProfile='A'; _invalidateVisionCapabilityFirst();
 const loadA=_loadVisionCapabilityFirst();
 requests[7].reject(Error('offline')); await saveB;
 assert.equal(cb._vcfProfile,'A'); assert.equal(cb.disabled,true);
 requests[8].resolve({vision_capability_first:false}); await loadA;
 assert.equal(cb.checked,false); assert.equal(cb.disabled,false);
 // A -> B -> A with the original A read unresolved must still reject it.
 _invalidateVisionCapabilityFirst();
 const oldA=_loadVisionCapabilityFirst();
 S.activeProfile='B'; _invalidateVisionCapabilityFirst();
 S.activeProfile='A'; _invalidateVisionCapabilityFirst();
 const freshA=_loadVisionCapabilityFirst();
 requests[9].resolve({vision_capability_first:true}); await oldA;
 assert.equal(cb.disabled,true);
 requests[10].resolve({vision_capability_first:false}); await freshA;
 assert.equal(cb.checked,false); assert.equal(cb.disabled,false);
 console.log('PASS: failed GET/retry, serialized saves/reopen, rollback, malformed load, profile races');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_profile_switch_with_settings_open_reloads_vision_setting():
    """A switch while Settings is open must not leave the checkbox dead.

    Regression for the #7503 greptile finding: ``switchToProfile`` invalidates
    the vision checkbox (disabled, cleared) on a successful switch, but nothing
    reloaded the incoming profile's value while the Settings panel was open, so
    the control stayed unusable until Settings was closed and reopened. The fix
    reloads in place when the panel is open. This test executes the real
    ``switchToProfile`` and vision loader together with deferred replies and
    fails on the pre-fix head (no GET after the switch; checkbox stays
    disabled).
    """
    if not shutil.which("node"):
        pytest.skip("node required")
    node = shutil.which("node")
    assert node is not None  # for type-checkers; guarded above
    vision = _vision_source()
    switch = _switch_source()
    script = (
        r"""
const assert=require('node:assert/strict');
const S={activeProfile:'A',activeProfileIsDefault:false,messages:[],session:{session_id:'s1',workspace:null,profile:'A'}};
const cb={checked:true,disabled:false,addEventListener(_,fn){this.change=fn;},classList:{add(){},remove(){},toggle(){}},style:{}};
const chip={classList:{add(){},remove(){},toggle(){}},disabled:false};
const $=id=>id==='settingsVisionCapabilityFirst'?cb:(id==='profileChip'?chip:null);
const t=k=>k, toasts=[]; const showToast=x=>toasts.push(x);
let _currentPanel='settings';
const requests=[];
function api(url,opts){return new Promise((resolve,reject)=>requests.push({url,opts,resolve,reject}));}
let _profileSwitchGeneration=0;
const noop=()=>{};
// switchToProfile's optional cross-module hooks (same typeof-guards as the source)
const _invalidateSessionListRenders=noop;
const _setProfileSwitchListEmbargo=noop;
const showSessionListSkeleton=noop;
const bumpWorkspaceTreeGen=noop;
const showWorkspaceTreeSkeleton=noop;
const clearWorkspaceTreeSkeleton=noop;
const closeSessionActionMenu=noop;
const animateNextSessionListRefresh=noop;
const _renamingSid=null;
let _skillsData=null, _workspaceList=null;
const applyBotName=noop;
const _clearPersistedModelState=noop;
const startGatewaySSE=noop;
const _applyModelToDropdown=()=> 'm';
const _modelStateForSelect=()=>({model:'m',model_provider:'p'});
const refreshProfileTransitionReasoningChip=noop;
const syncTopbar=noop;
const renderSessionList=async()=>{};
const newSession=async()=>{};
const loadDir=async()=>{};
const _openProfileSwitchSessionBrowser=noop;
const _profileSwitchPanelLoad=async()=>{};
const _refreshProfileSwitchBackground=noop;
const _resetCronUnreadForProfileSwitch=noop;
const esc=x=>x;
"""
        + vision
        + "\n"
        + switch
        + r"""
let finished=false;
const watchdog=setTimeout(()=>{ if(!finished){console.error('script stalled (deferred reply never resolved)');process.exit(1);} },5000);
(async()=>{
 // Settings is open; profile A's value is confirmed and the checkbox is usable.
 const loadA=_loadVisionCapabilityFirst();
 requests.shift().resolve({vision_capability_first:true});
 await loadA;
 assert.equal(cb.checked,true); assert.equal(cb.disabled,false);
 // Switch to B while Settings stays open. Resolve the switch POST, then await.
 const sw=switchToProfile('B');
 requests.shift().resolve({active:'B',is_default:false});
 const switched=await sw;
 assert.equal(switched,true);
 assert.equal(S.activeProfile,'B');
 assert.equal(cb._vcfProfile,'B');
 // The reload must have issued a fresh GET for the incoming profile…
 const reload=cb._vcfPending;
 assert.ok(reload,'no vision GET issued after the switch — checkbox stays disabled until Settings is reopened');
 // …and until it resolves the checkbox stays disabled (fail closed)…
 assert.equal(cb.disabled,true);
 // …then the incoming profile's value paints and re-enables the control.
 requests.shift().resolve({vision_capability_first:false});
 await reload;
 assert.equal(cb.checked,false); assert.equal(cb.disabled,false);
 finished=true; clearTimeout(watchdog);
 console.log('PASS: open-Settings switch reloads the vision setting and re-enables the checkbox');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    # The PASS line must actually print — a silently-drained event loop also
    # exits 0, so returncode alone is a false-green oracle here.
    assert "PASS: open-Settings switch reloads" in result.stdout, result.stdout + result.stderr


def _workspace_api_source():
    """Slice the REAL api() helper out of static/workspace.js.

    The slice runs from the `async function api(` marker to the next
    top-level function boundary (line-anchored, so braces inside string
    literals or regexes cannot break it).
    """
    source = (ROOT / "static" / "workspace.js").read_text()
    start = source.index("async function api(")
    end = source.index("\nfunction recordClientSSEError", start)
    return source[start:end]


def test_retried_vision_save_cannot_write_across_profile_switch():
    """#7503 behavioral: drive the REAL api() helper (with its TypeError retry
    and fresh-cookie re-read) plus the real vision save/load code through the
    exact race the gate review reproduced.

    Sequence: enable on profile A, the first POST REACHES the server and
    commits but its response is lost (TypeError), the user switches to B
    during the retry backoff (fresh cookies), and the retried POST carries
    A's bound profile. The server answers 409 without writing, and the client
    reacts by reloading the now-active profile's value. Switching back to A
    reloads A's committed value.

    Fails on the pre-fix head: the POST body carries no profile, the stubbed
    server cannot distinguish the retry from a fresh B save, and the client
    shows a generic failure instead of reloading.
    """
    if not shutil.which("node"):
        pytest.skip("node required")
    node = shutil.which("node")
    assert node is not None  # for type-checkers; guarded above
    vision = _vision_source()
    api_src = _workspace_api_source()
    script = (
        r"""
const assert=require('node:assert/strict');
const S={activeProfile:'vcf-a'};
const cb={checked:false,disabled:true,addEventListener(_,fn){this.change=fn;}};
const $=()=>cb, t=k=>k, toasts=[]; const showToast=x=>toasts.push(x);
// The browser's cookie jar: fetch attaches it fresh on EVERY attempt, so a
// retry after a switch carries the NEW profile's cookie.
let cookieJar='hermes_profile=vcf-a';
const document={ get baseURI(){return 'http://test/';}, get cookie(){return cookieJar;} };
const location={href:'http://test/'};
// Deferred-response server stub, dispatched BY METHOD (GET payloads vs POST
// handlers) so call ordering can never steal another call's scripted reply.
// Writes COMMIT before a lost response, so the retry replay is
// distinguishable from the original save.
const calls=[]; const writes=[];
const getPayloads=[]; const postHandlers=[];
global.fetch=async function(url,opts){
  const method=((opts&&opts.method)||'GET').toUpperCase();
  const body=opts&&opts.body?JSON.parse(opts.body):null;
  const call={url:String(url),method:method,cookie:cookieJar,body:body};
  calls.push(call);
  let r;
  if(method==='POST'){
    const handler=postHandlers.shift();
    if(!handler) throw new Error('no scripted POST handler');
    r=await handler(call);
    if(r&&r.write){ writes.push(r.write); }
  }else{
    const payload=getPayloads.shift();
    if(payload===undefined) throw new Error('no scripted GET payload');
    r={payload:payload};
  }
  if(r&&r.networkLoss) throw new TypeError('Failed to fetch');
  const payload=(r&&r.payload)!==undefined?r.payload:{};
  return {
    ok:!r||!r.status||(r.status>=200&&r.status<300),
    status:r&&r.status||200,
    statusText:r&&r.statusText||'OK',
    headers:{get:()=> 'application/json'},
    text:async()=>JSON.stringify(payload),
    json:async()=>payload,
  };
};
"""
        + api_src
        + "\n"
        + vision
        + r"""
let finished=false;
const watchdog=setTimeout(()=>{ if(!finished){console.error('script stalled (deferred reply never resolved)');process.exit(1);} },10000);
(async()=>{
 // 1. Load A's value (GET, cookie A).
 getPayloads.push({vision_capability_first:false});
 const loadA=_loadVisionCapabilityFirst();
 await loadA;
 assert.equal(cb.checked,false); assert.equal(cb.disabled,false);
 // 2. Enable on A. Script BOTH POST outcomes BEFORE triggering the change —
 //    the change handler runs synchronously into the first fetch. The first
 //    attempt commits the write and loses its response (TypeError → api()
 //    retries with fresh cookies); the retried POST is rejected with 409.
 postHandlers.push(async(call)=>({networkLoss:true,write:{cookie:call.cookie,body:call.body}}));
 postHandlers.push(async()=>({status:409,statusText:'Conflict',payload:{error:'vision-capability-first: profile changed since the save started; reload the setting'}}));
 cb.checked=true; cb.change();
 const save=cb._vcfPending;
 // 3. During the retry backoff the user switches to B: the switch invalidates
 //    the checkbox state and the browser's next request carries B's cookie.
 //    (The open-Settings reload itself is covered by the previous test.)
 setTimeout(()=>{
   S.activeProfile='vcf-b'; _invalidateVisionCapabilityFirst();
   cookieJar='hermes_profile=vcf-b';
 },50);
 // 4+5. The client reacts to the 409 by reloading the ACTIVE profile's value
 //    (GET, cookie B) — B's stored false paints.
 getPayloads.push({vision_capability_first:false});
 await save;
 // The 409-triggered reload may still be in flight; await it before asserting.
 await cb._vcfPending;
 // The original POST must have committed exactly one write, into A only…
 assert.equal(calls[1].method,'POST');
 assert.equal(calls[1].cookie,'hermes_profile=vcf-a');
 assert.deepEqual(calls[1].body,{enabled:true,profile:'vcf-a'});
 assert.deepEqual(writes,[{cookie:'hermes_profile=vcf-a',body:{enabled:true,profile:'vcf-a'}}],
   'exactly one write must have committed, and only into profile A');
 // …and the retry carried B's fresh cookie with A's bound profile.
 assert.equal(calls[2].method,'POST');
 assert.equal(calls[2].cookie,'hermes_profile=vcf-b');
 assert.deepEqual(calls[2].body,{enabled:true,profile:'vcf-a'});
 // The active profile's stored value painted and re-enabled the control.
 assert.equal(calls[3].method,'GET');
 assert.equal(calls[3].cookie,'hermes_profile=vcf-b');
 assert.equal(cb._vcfProfile,'vcf-b');
 assert.equal(cb.checked,false);
 assert.equal(cb.disabled,false);
 // 6. Switch back to A: A's committed true reloads from the server.
 S.activeProfile='vcf-a'; _invalidateVisionCapabilityFirst();
 cookieJar='hermes_profile=vcf-a';
 getPayloads.push({vision_capability_first:true});
 const loadBack=_loadVisionCapabilityFirst();
 await loadBack;
 assert.equal(calls[4].cookie,'hermes_profile=vcf-a');
 assert.equal(cb.checked,true); assert.equal(cb.disabled,false);
 finished=true; clearTimeout(watchdog);
 console.log('PASS: retried save 409s across the switch, B unchanged, committed A value reloads');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: retried save 409s across the switch" in result.stdout, result.stdout + result.stderr


def test_vision_save_profile_mismatch_rejected_before_write():
    """#7503, server half over the REAL route: a save whose bound profile
    differs from the request's resolved profile must be rejected with 409
    BEFORE any write, same-profile saves keep working (positive control), and
    unbound saves are rejected with 400.

    Note: the test server pins HERMES_CONFIG_PATH to one shared config.yaml
    (the pre-existing #8032 behavior this PR does not change), so per-profile
    homes cannot be observed over HTTP here. The no-write proof is therefore
    value-based: with the stored value at true, a MISMATCHED POST carrying
    enabled=false must leave the stored value at true — an ungated write would
    have flipped it.
    """
    import json
    import urllib.error
    import urllib.request

    from tests._pytest_port import BASE

    def _post(body, profile):
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if profile:
            headers["Cookie"] = f"hermes_profile={profile}"
        req = urllib.request.Request(BASE + "/api/vision-capability-first", data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read()), r.status
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read()), e.code
            except Exception:
                return {}, e.code

    def _get(profile):
        headers = {"Cookie": f"hermes_profile={profile}"}
        req = urllib.request.Request(BASE + "/api/vision-capability-first", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status

    a, b = "vcf-prgate-a", "vcf-prgate-b"
    # Seed a known false state through an own-profile save.
    body, status = _post({"enabled": False, "profile": a}, a)
    assert status == 200, f"seeding failed: {status} {body}"
    assert _get(a)[0]["vision_capability_first"] is False

    # 1. Positive control: a same-profile save commits.
    body, status = _post({"enabled": True, "profile": a}, a)
    assert status == 200, f"same-profile save failed: {status} {body}"
    assert body["vision_capability_first"] is True
    assert _get(a)[0]["vision_capability_first"] is True

    # 2. THE RACE: a save started on A is retried after the browser switched
    #    to B — the cookie resolves B, the body still binds A, and the retried
    #    payload (false) differs from the stored value (true). Must 409
    #    BEFORE writing.
    body, status = _post({"enabled": False, "profile": a}, b)
    assert status == 409, f"cross-profile retry must 409, got {status}: {body}"
    # No write happened: the stored value the retry tried to flip is untouched.
    assert _get(a)[0]["vision_capability_first"] is True, (
        "the mismatched retry must be rejected before any write"
    )
    assert _get(b)[0]["vision_capability_first"] is True, (
        "the mismatched retry must be rejected before any write (B resolves the same shared config)"
    )

    # 3. Positive control: B's own save is accepted and commits.
    body, status = _post({"enabled": False, "profile": b}, b)
    assert status == 200, f"same-profile save for B failed: {status} {body}"
    assert _get(b)[0]["vision_capability_first"] is False

    # 4. Unbound saves are rejected with 400 (old clients cannot silently
    #    regress the ownership contract).
    body, status = _post({"enabled": True}, a)
    assert status == 400, f"missing profile binding must 400, got {status}: {body}"
    body, status = _post({"enabled": True, "profile": "   "}, a)
    assert status == 400, f"empty profile binding must 400, got {status}: {body}"


def test_late_profile_mismatch_409_does_not_clobber_new_profile_operations():
    """#7503 greptile P1: a LATE 409 from profile A's save arrives after the
    user switched to B and B already has a save in flight. The 409 handler must
    react only while it still OWNS the checkbox — clearing _vcfPending/_vcfToken
    unconditionally drops B's in-flight save token, so B's save reply is ignored
    and the checkbox is stranded inconsistent with the persisted value.

    Fails on the pre-fix head: the 409 branch cleared the shared pending/token
    state and fired a replacement load, so this script sees an extra GET and
    B's confirmed value never paints.
    """
    if not shutil.which("node"):
        pytest.skip("node required")
    node = shutil.which("node")
    assert node is not None  # for type-checkers; guarded above
    source = _vision_source()
    script = r"""
const assert=require('node:assert/strict');
const S={activeProfile:'A'};
const cb={checked:false,disabled:true,addEventListener(_,fn){this.change=fn;}};
const $=()=>cb, t=k=>k, toasts=[], showToast=x=>toasts.push(x);
const requests=[];
function api(url,opts){return new Promise((resolve,reject)=>requests.push({opts,resolve,reject}));}
""" + source + r"""
(async()=>{
 // 1. A's stored value loads and enables the control.
 let load=_loadVisionCapabilityFirst();
 requests[0].resolve({vision_capability_first:false}); await load;
 assert.equal(cb.disabled,false); assert.equal(cb.checked,false);
 // 2. The user saves on A; the response is lost in flight (will 409 late).
 cb.checked=true; cb.change();
 const saveA=cb._vcfPending;
 assert.equal(cb.disabled,true);
 // 3. During A's pending save the user switches to B and B's value loads.
 S.activeProfile='B'; _invalidateVisionCapabilityFirst();
 const loadB=_loadVisionCapabilityFirst();
 assert.equal(requests.length,3); // GET A, POST A, GET B
 requests[2].resolve({vision_capability_first:false}); await loadB;
 assert.equal(cb._vcfProfile,'B'); assert.equal(cb.checked,false); assert.equal(cb.disabled,false);
 // 4. B saves too — this operation now owns the checkbox.
 cb.checked=true; cb.change();
 const saveB=cb._vcfPending;
 assert.equal(cb.disabled,true);
 // 5. A's LATE 409 arrives (on A's retried POST). It must not clear B's
 //    pending save or its token (that made B's reply ignored and stranded the
 //    checkbox), and must not fire a replacement load while B's save is still
 //    in flight.
 requests[1].reject(Object.assign(new Error('conflict'),{status:409}));
 await saveA;
 assert.equal(requests.length,4, 'a late foreign 409 must not trigger an immediate replacement load');
 assert.equal(cb._vcfPending,saveB, "B's in-flight save keeps ownership of the control");
 // 6. B's save completes and its confirmed value paints — the reply was not
 //    dropped by the late 409.
 requests[3].resolve({vision_capability_first:true});
 await saveB;
 assert.equal(cb.checked,true, "B's confirmed save reply paints despite the late 409");
 // 6b. The deferred refresh (after B's save settled) reloads B's stored value;
 //     it starts before this continuation resumes, so just await it.
 assert.equal(requests.length,5, 'the deferred post-save refresh ran once');
 const refresh=cb._vcfPending;
 requests[4].resolve({vision_capability_first:true});
 await refresh;
 assert.equal(cb.checked,true); assert.equal(cb.disabled,false);
 // 7. Positive control: a 409 while STILL on the saving profile reloads that
 //    profile's stored value (the owned path keeps working).
 cb.checked=false; cb.change();
 const saveB2=cb._vcfPending;
 requests[5].reject(Object.assign(new Error('conflict'),{status:409}));
 await saveB2;
 assert.equal(requests.length,7, 'an owned 409 reloads the active profile value');
 const reload=requests[6];
 assert.equal(reload.opts,undefined,'the owned 409 replacement is a GET, not a POST');
 reload.resolve({vision_capability_first:true});
 await cb._vcfPending;
 assert.equal(cb.checked,true); assert.equal(cb.disabled,false);
 console.log('PASS: late foreign 409 leaves B ops intact; owned 409 still reloads');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: late foreign 409 leaves B ops intact" in result.stdout, result.stdout + result.stderr
