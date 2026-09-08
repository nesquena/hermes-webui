"""Composed owner and cron-poller proof for issue #7257.

The harness stubs only the final presentation seam; real service-worker registration,
OS delivery, and OS-level tag collapse remain unreached.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
MESSAGES = (ROOT / "static/messages.js").read_text(encoding="utf-8")
PANELS = (ROOT / "static/panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")

# This is the exact hidden-document guard from origin/master at the respec base.
# Pinning the small reproduction fixture keeps CI independent of git ref depth.
BASE_POLLER = """function startCronPolling(){
    if(_cronPollTimer) return;
    _cronPollTimer=setInterval(async()=>{
      if(document.hidden) return;  // don't poll when tab is in background
      try{
        const pollGeneration=_cronPollGeneration;
        const data=await api(`/api/crons/recent?since=${_cronPollSince}`);
        if(pollGeneration!==_cronPollGeneration) return;
        if(data.completions&&data.completions.length>0){
          for(const c of data.completions){
            if(c.toast_notifications !== false){
              showToast(t('cron_completion_status', c.name, c.status==='error' ? t('status_failed') :
t('status_completed')),4000);
            }
            _cronPollSince=Math.max(_cronPollSince,c.completed_at);
            if(c.job_id) _cronNewJobIds.add(String(c.job_id));
            if(c.session_id && typeof _markSessionCompletionUnreadIfBackground === 'function'){
              const activeProfile=(typeof S!=='undefined'&&S&&S.activeProfile)||'default';
              _markSessionCompletionUnreadIfBackground(c.session_id, c.message_count, {
                source:'cron',
                profile:activeProfile,
              });
            }
          }
          // _cronUnreadCount is derived from _cronNewJobIds.size in updateCronBadge.
          updateCronBadge();
        }
      }catch(e){}
    },30000);
  }"""


def _function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace = source.index("{", source.index(")", start))
    depth = 0
    for index in range(brace, len(source)):
        depth += source[index] == "{"
        depth -= source[index] == "}"
        if depth == 0:
            return source[start : index + 1]
    raise AssertionError(name)


_owner_start = MESSAGES.index("let _desktopBackgroundedForNotifications=false;")
_owner_end = MESSAGES.index("function _isSessionCurrentPane", _owner_start)
OWNER = "const _STREAM_NOTIFICATION_BACKGROUND={};" + MESSAGES[_owner_start:_owner_end] + "\n" + "\n".join(
    _function(MESSAGES, name)
    for name in ("_isBrowserNotificationReady", "_notificationOptions", "_completionNotificationPreviewText", "_showPwaNotification", "sendBrowserNotification")
)
POLLER = _function(PANELS, "startCronPolling")


def _run_base_head_reproduction() -> dict:
    script = f"""
const vm=require('vm'),owner={json.dumps(OWNER)},base={json.dumps(BASE_POLLER)},head={json.dumps(POLLER)};
async function run(source,isHead) {{
  let timer,apiCalls=0,presentations=0;
  function Notification(title,options) {{ presentations++; }}
  Notification.permission='granted';
  const registration={{active:null}}; registration.active=registration;
  registration.getNotifications=()=>Promise.resolve([]);
  registration.showNotification=()=>{{presentations++;return Promise.resolve();}};
  const context={{document:{{hidden:true}},window:{{_notificationsEnabled:true,location:{{origin:'https://example.test',href:'https://example.test/'}},addEventListener:()=>{{}}}},Notification,
    _cronPollTimer:null,_cronPollSince:0,_cronPollGeneration:0,_cronNewJobIds:new Set(),
    S:{{activeProfile:'profile-a',session:null}},setInterval:cb=>{{timer=cb;return 1;}},
    api:async()=>{{apiCalls++;return {{completions:[{{name:'Nightly',status:'success',completed_at:42,job_id:'job-1',toast_notifications:true}}]}};}},
    navigator:{{serviceWorker:{{getRegistration:()=>Promise.resolve(registration)}}}},location:{{origin:'https://example.test',href:'https://example.test/'}},
    _sessionUrlForSid:sid=>'/session/'+sid,_appRootPath:()=>'/app/',assistantDisplayName:()=> 'Hermes',
    requestNotificationPermission:()=>Promise.resolve('granted'),showToast:()=>{{}},updateCronBadge:()=>{{}},
    t:(key,...args)=>key+':'+args.join('|'),
    setTimeout,clearTimeout,Promise,console,globalThis:null}};
  context.globalThis=context; context.window.Notification=Notification; vm.createContext(context);vm.runInContext(owner,context);vm.runInContext(source,context);vm.runInContext('startCronPolling()',context);await timer();return {{apiCalls,presentations}};
}}
Promise.all([run(base,false),run(head,true)]).then(([baseResult,headResult])=>process.stdout.write(JSON.stringify({{base:baseResult,head:headResult}}))).catch(e=>{{console.error(e);process.exit(1)}});
"""
    return json.loads(subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True).stdout)


def _run_equal_timestamp_cursor(include_earlier: bool = False) -> dict:
    earlier = "  {name:'Earlier',status:'success',completed_at:99,job_id:'job-earlier',session_id:'',message_count:0,toast_notifications:true},\n" if include_earlier else ""
    script = f"""
const vm=require('vm'),owner={json.dumps(OWNER)},poller={json.dumps(POLLER)};
const rows=[
{earlier}  {{name:'Job A',status:'success',completed_at:100,job_id:'job-a',session_id:'',message_count:0,toast_notifications:true}},
  {{name:'Job B',status:'success',completed_at:100,job_id:'job-b',session_id:'',message_count:0,toast_notifications:true}},
  {{name:'Job C',status:'success',completed_at:101,job_id:'job-c',session_id:'',message_count:0,toast_notifications:true}},
];
const registry=new Map(),queries=[],responses=[],outcomes=[],alerts=[];
let failedWorker=false,failedDirect=false;
function Notification(title,options) {{
  if(options.tag.endsWith('-job-b-100')&&!failedDirect) {{ failedDirect=true; throw new Error('direct failed'); }}
  alerts.push({{title,options}});
}}
Notification.permission='granted';
const registration={{active:null}}; registration.active=registration;
registration.getNotifications=({{tag}})=>Promise.resolve(registry.has(tag)?[registry.get(tag)]:[]);
registration.showNotification=(title,options)=>{{
  if(options.tag.endsWith('-job-b-100')&&!failedWorker) {{ failedWorker=true; return Promise.reject(new Error('worker failed')); }}
  registry.set(options.tag,{{title,options}}); alerts.push({{title,options}}); return Promise.resolve();
}};
const window={{_notificationsEnabled:true,location:{{origin:'https://example.test',href:'https://example.test/'}},Notification,addEventListener:()=>{{}}}};
const context={{window,document:{{hidden:true,baseURI:'https://example.test/'}},Notification,
  navigator:{{serviceWorker:{{getRegistration:()=>Promise.resolve(registration)}}}},location:window.location,
  S:{{activeProfile:'profile-a',session:null}},_cronPollTimer:null,_cronPollSince:0,_cronPollGeneration:0,_cronNewJobIds:new Set(),
  _sessionUrlForSid:s=>'/session/'+s,_appRootPath:()=>'/app/',assistantDisplayName:()=> 'Hermes',
  t:(key,...args)=>key+':'+args.join('|'),showToast:()=>{{}},updateCronBadge:()=>{{}},
  _markSessionCompletionUnreadIfBackground:()=>{{}},setInterval:cb=>{{context.timer=cb;return 1;}},
  setTimeout,clearTimeout,Promise,console,globalThis:null}};
context.globalThis=context; context.window.Notification=Notification; vm.createContext(context);
vm.runInContext(owner,context);
const realSend=context.sendBrowserNotification;
context.sendBrowserNotification=(...args)=>realSend(...args).then(outcome=>{{outcomes.push({{tag:args[2].tag,outcome}});return outcome;}});
vm.runInContext(poller,context);
vm.runInContext('startCronPolling()',context);
context.api=async url=>{{
  const since=Number(new URL(url,'https://example.test').searchParams.get('since'));
  queries.push(since);
  const completions=rows.filter(row=>row.completed_at>since);
  responses.push(completions.length);
  return {{completions}};
}};
async function main() {{
  await context.timer();
  const afterFirst={{since:context._cronPollSince,queries:queries.slice(),responses:responses.slice(),alerts:alerts.length}};
  await context.timer();
  process.stdout.write(JSON.stringify({{afterFirst,queries,responses,alerts,outcomes,since:context._cronPollSince,tags:[...registry.keys()]}}));
}}
main().catch(e=>{{console.error(e);process.exit(1)}});
    """
    result = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def _run(case: str) -> dict:
    script = f"""
const vm=require('vm');
const owner={json.dumps(OWNER)};
const poller={json.dumps(POLLER)};
const caseName={json.dumps(case)};
const registry=new Map(),shown=[],toasts=[],marks=[];
let badgeCalls=0,getNotificationCalls=0,ownerCalls=0,permissionRequests=0;
let alertCount=0,failPresentation=caseName==='failure'||caseName==='ordered';
const shouldFail=options=>failPresentation&&(caseName!=='ordered'||String(options&&options.tag).endsWith('-42'));
function Notification(title,options) {{ if(shouldFail(options)) throw new Error('native seam failed'); shown.push({{title,options}}); alertCount++; }}
Notification.permission='granted';
const registration={{active:null}}; registration.active=registration;
registration.getNotifications=({{tag}})=>{{getNotificationCalls++;return Promise.resolve(registry.has(tag)?[registry.get(tag)]:[]);}};
registration.showNotification=(title,options)=>{{
  if(caseName==='worker-fallback') return Promise.reject(new Error('worker failed'));
  if(shouldFail(options)) return Promise.reject(new Error('worker failed'));
  const existing=registry.get(options.tag); registry.set(options.tag,{{title,options}}); shown.push({{title,options}});
  if(caseName==='force-hidden-transition'&&shown.length===1) {{ document.hidden=false; window.__hermesSetBackgrounded(false); }}
  if(caseName==='permission-transition'&&shown.length===1) Notification.permission='default';
  if(caseName==='stale-after-notification') context._cronPollGeneration++;
  if(caseName==='partial-failure') failPresentation=true;
  if(!existing || options.renotify) alertCount++; return Promise.resolve();
}};
const document={{hidden:!['foreground','desktop','desktop-transition','desktop-unavailable'].includes(caseName),baseURI:'https://example.test/',hasFocus:()=>false}};
const window={{_notificationsEnabled:true,location:{{origin:'https://example.test',href:'https://example.test/'}},addEventListener:()=>{{}}}};
const context={{window,document,Notification,navigator:{{serviceWorker:{{getRegistration:()=>Promise.resolve(registration)}}}},location:window.location,
  S:{{activeProfile:'profile-a',session:{{session_id:'sess-active'}}}},_sessionUrlForSid:sid=>'/session/'+sid,_appRootPath:()=>'/app/',assistantDisplayName:()=> 'Hermes',
  t:(key,...args)=>key+':'+args.join('|'),showToast:msg=>toasts.push(msg),updateNotificationPermissionStatus:()=>{{}},requestNotificationPermission:()=>{{permissionRequests++;return caseName==='permission-reject'?Promise.reject(new Error('permission request failed')):Promise.resolve('granted');}},
  setTimeout,clearTimeout,Promise,console,globalThis:null}};
context.globalThis=context; context.window.Notification=Notification; vm.createContext(context); vm.runInContext(owner,context);
const realSendBrowserNotification=context.sendBrowserNotification;
context.sendBrowserNotification=(...args)=>{{ownerCalls++;return realSendBrowserNotification(...args);}};
function setupPoller(completions) {{
  let timer=null,apiCalls=0,resolveApi,rejectApi;
  context._cronPollSince=0; context._cronPollTimer=null; context._cronPollGeneration=0; context._cronNewJobIds=new Set(); context.updateCronBadge=()=>{{badgeCalls++;if(caseName==='badge-failure')throw new Error('badge failed');}};
  context._markSessionCompletionUnreadIfBackground=(...args)=>marks.push(args);
  context.api=()=>{{apiCalls++;return new Promise((resolve,reject)=>{{resolveApi=()=>resolve({{completions}});rejectApi=()=>reject(new Error('rejected api'));}});}};
  context.setInterval=cb=>{{timer={{callback:cb}};context._cronPollTimer=timer;return timer;}};
  vm.runInContext(poller,context); return {{get timer(){{return timer;}},get apiCalls(){{return apiCalls;}},resolve:()=>resolveApi(),reject:()=>rejectApi()}};
}}
async function main() {{
  let firstTick=null;
  if(caseName==='permission-reject') {{ Notification.permission='default'; return {{result:await vm.runInContext("sendBrowserNotification('title','body',{{forceHidden:true}})",context)}}; }}
  if(caseName.startsWith('owner-')&&caseName!=='owner-no-dedupe') {{
    if(caseName==='owner-no-session') context.S.session=null;
    const options=caseName==='owner-empty'?{{sid:'',tag:'hermes-cron-profile-a-job-1-42',renotify:false}}:caseName==='owner-false'?{{renotify:false}}:{{}};
    return {{result:vm.runInContext(`_notificationOptions('body',${{JSON.stringify(options)}})`,context)}};
  }}
  if(caseName==='owner-no-dedupe') {{
    registry.set('hermes-sess-active',{{title:'existing',options:{{tag:'hermes-sess-active'}}}});
    const result=await vm.runInContext("sendBrowserNotification('title','body',{{force:true}})",context);
    return {{result,ownerCalls,getNotificationCalls,shown}};
  }}
  const all=[{{name:'Nightly',status:'success',completed_at:42,job_id:'job-1',session_id:caseName==='sessionless'?'':'sid-1',message_count:7,toast_notifications:caseName!=='muted'&&caseName!=='after-unavailable-muted',output_preview:caseName==='preview'?'  Preview   text\\nwith   spacing   '+'x'.repeat(150):caseName==='empty-preview'?'':undefined}},{{name:'Later',status:'success',completed_at:43,job_id:'job-1',session_id:'',message_count:0,toast_notifications:true}}];
  const completions=caseName==='ordered'?all.slice().reverse():all.slice(0,(caseName==='later'||caseName==='partial-failure'||caseName==='force-hidden-transition'||caseName==='permission-transition')?2:1);
  const p=setupPoller(completions); vm.runInContext('startCronPolling()',context);
  if(caseName==='desktop'||caseName==='desktop-unavailable') {{ document.hidden=false; window.__hermesSetBackgrounded(true); }} if(caseName==='unavailable'||caseName==='desktop-unavailable') window._notificationsEnabled=false;
  if(caseName==='unavailable'||caseName==='desktop-unavailable') {{ await p.timer.callback(); return {{apiCalls:p.apiCalls,shown,toasts,marks,since:context._cronPollSince,ids:[...context._cronNewJobIds],registry:[...registry.keys()],alerts:alertCount,badgeCalls,ownerCalls,getNotificationCalls}}; }}
  if(caseName==='no-query') delete registration.getNotifications;
  if(caseName==='rejected-api') {{ const first=p.timer.callback(); await Promise.resolve(); p.reject(); await first; const second=p.timer.callback(); p.resolve(); await second; }}
  else if(caseName==='overlap') {{ const first=p.timer.callback(); const second=p.timer.callback(); p.resolve(); await Promise.all([first,second]); }}
  else if(caseName==='badge-failure') {{ const first=p.timer.callback(); p.resolve(); await first; const second=p.timer.callback(); p.resolve(); await second; }}
  else {{ const pending=p.timer.callback(); await Promise.resolve(); if(caseName==='desktop-transition') window.__hermesSetBackgrounded(true); if(caseName==='after-unavailable'||caseName==='after-unavailable-muted') window._notificationsEnabled=false; if(caseName==='profile-transition') context.S.activeProfile='profile-b'; if(caseName==='stale') context._cronPollGeneration++; p.resolve(); await pending; if(caseName==='failure') {{firstTick={{since:context._cronPollSince,ids:[...context._cronNewJobIds],marks:marks.length}}; failPresentation=false;const retry=p.timer.callback();p.resolve();await retry; }} }}
  return {{apiCalls:p.apiCalls,shown,toasts,marks,since:context._cronPollSince,ids:[...context._cronNewJobIds],registry:[...registry.keys()],alerts:alertCount,badgeCalls,ownerCalls,getNotificationCalls,permissionRequests,firstTick}};
}}
main().then(v=>process.stdout.write(JSON.stringify(v))).catch(e=>{{console.error(e);process.exit(1)}});
"""
    result = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def _run_two_realms(simultaneous: bool) -> dict:
    script = f"""
const vm=require('vm'),owner={json.dumps(OWNER)},registry=new Map(),alerts=[];const simultaneous={json.dumps(simultaneous)};let audible=0;
let entered=0,release;
const bothEntered=new Promise(resolve=>release=resolve);
function makeRealm(id) {{
  const Notification=function(){{}}; Notification.permission='granted';
  const reg={{active:null}}; reg.active=reg;
  reg.getNotifications=({{tag}})=>{{entered++;if(!simultaneous)return Promise.resolve(registry.has(tag)?[registry.get(tag)]:[]);if(entered===2) release();return bothEntered.then(()=>registry.has(tag)?[registry.get(tag)]:[]);}};
  reg.showNotification=(title,options)=>{{const existed=registry.has(options.tag);registry.set(options.tag,{{title,options,realm:id}});alerts.push({{id,options}});if(!existed||options.renotify) audible++;return Promise.resolve();}};
  const window={{_notificationsEnabled:true,location:{{origin:'https://example.test',href:'https://example.test/'}},Notification}};
  const context={{window,document:{{hidden:true}},Notification,navigator:{{serviceWorker:{{getRegistration:()=>Promise.resolve(reg)}}}},location:window.location,S:{{session:null}},assistantDisplayName:()=> 'Hermes',_appRootPath:()=>'/app/',_sessionUrlForSid:s=>'/session/'+s,setTimeout,Promise,console}};
  context.globalThis=context;vm.createContext(context);vm.runInContext(owner,context);return context;
}}
async function main() {{const a=makeRealm('realm-a'),b=makeRealm('realm-b');
 const opts={{forceHidden:true,sid:'',tag:'hermes-cron-profile-a-job-1-42',renotify:false,dedupe:true}};
 let first,second;
 if(simultaneous) {{
   const firstPromise=vm.runInContext(`sendBrowserNotification('Nightly','done',${{JSON.stringify(opts)}})`,a);
   const secondPromise=vm.runInContext(`sendBrowserNotification('Nightly','done',${{JSON.stringify(opts)}})`,b);
   [first,second]=await Promise.all([firstPromise,secondPromise]);
 }} else {{
   first=await vm.runInContext(`sendBrowserNotification('Nightly','done',${{JSON.stringify(opts)}})`,a);
   second=await vm.runInContext(`sendBrowserNotification('Nightly','done',${{JSON.stringify(opts)}})`,b);
 }}
 process.stdout.write(JSON.stringify({{first,second,entered,audible,alerts,records:[...registry.values()]}}));}}
main().catch(e=>{{console.error(e);process.exit(1)}});
"""
    return json.loads(subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True).stdout)


def test_issue_repro_hidden_completion_reaches_presentation_owner():
    result = _run("hidden")
    assert result["since"] == 42 and result["toasts"] == [] and result["shown"] and result["getNotificationCalls"] > 0
    assert result["shown"][0]["options"]["renotify"] is False


def test_issue_reproduction_base_fails_head_reaches_presentation_owner():
    result = _run_base_head_reproduction()
    assert result == {"base": {"apiCalls": 0, "presentations": 0}, "head": {"apiCalls": 1, "presentations": 1}}


def test_equal_timestamp_failure_keeps_the_group_eligible_for_retry():
    result = _run_equal_timestamp_cursor()
    assert result["afterFirst"] == {"since": 0, "queries": [0], "responses": [3], "alerts": 1}
    assert result["queries"] == [0, 0]
    assert result["responses"] == [3, 3]
    assert result["since"] == 101
    assert len(result["tags"]) == 3 and len(result["alerts"]) == 3
    assert sum(item["options"]["tag"].endswith("-job-a-100") for item in result["alerts"]) == 1
    assert any(
        item["tag"].endswith("-job-a-100") and item["outcome"].get("alreadyDisplayed") is True
        for item in result["outcomes"]
    )
    assert all(
        any(item["tag"].endswith(f"-{job_id}-{timestamp}") and item["outcome"].get("delivered") is True for item in result["outcomes"])
        for job_id, timestamp in (("job-b", 100), ("job-c", 101))
    )


def test_completed_group_progress_survives_a_later_equal_timestamp_failure():
    result = _run_equal_timestamp_cursor(include_earlier=True)
    assert result["afterFirst"]["since"] == 99
    assert result["queries"] == [0, 99]
    assert result["since"] == 101
    assert len(result["tags"]) == 4 and len(result["alerts"]) == 4


def test_desktop_background_recomputed_before_and_after_await():
    result = _run("desktop")
    assert result["toasts"] == [] and result["since"] == 42 and result["alerts"] == 1


def test_desktop_background_transition_after_await_uses_canonical_state():
    result = _run("desktop-transition")
    assert result["toasts"] == [] and result["since"] == 42 and result["shown"]


def test_visible_desktop_background_with_notifications_off_skips_request_and_preserves_backlog():
    result = _run("desktop-unavailable")
    assert result["apiCalls"] == 0 and result["since"] == 0 and result["ids"] == []


def test_after_await_readiness_loss_preserves_backlog_and_badge():
    result = _run("after-unavailable")
    assert result["since"] == 0 and result["ids"] == [] and result["marks"] == [] and result["badgeCalls"] == 0 and result["ownerCalls"] == 0


def test_after_await_readiness_loss_blocks_muted_completion_effects():
    result = _run("after-unavailable-muted")
    assert result["since"] == 0 and result["ids"] == [] and result["marks"] == [] and result["badgeCalls"] == 0


def test_background_delivery_unavailable_preserves_backlog():
    result = _run("unavailable")
    assert result["apiCalls"] == 0 and result["since"] == 0 and result["ids"] == []


def test_explicit_empty_sid_never_inherits_active_session():
    result = _run("owner-empty")["result"]
    assert result["data"]["url"] == "https://example.test/app/" and result["renotify"] is False
    assert result["tag"] == "hermes-cron-profile-a-job-1-42"


def test_truthy_session_keeps_deep_link_and_unread_boundary():
    result = _run("hidden")
    assert len(result["marks"]) == 1 and result["marks"][0][0] == "sid-1" and result["marks"][0][2]["profile"] == "profile-a"
    assert "/session/sid-1" in result["shown"][0]["options"]["data"]["url"]
    assert result["shown"][0]["options"]["tag"] == "hermes-cron-profile-a-job-1-42"


def test_two_realms_same_identity_short_circuit_displayed_record():
    result = _run_two_realms(False)
    assert result["first"]["delivered"] is True and result["second"]["alreadyDisplayed"] is True
    assert len(result["records"]) == 1 and len(result["alerts"]) == 1


def test_two_realms_same_identity_replace_silently():
    result = _run_two_realms(True)
    assert result["audible"] == 1 and len(result["records"]) == 1
    assert all(row["options"]["renotify"] is False for row in result["alerts"])


def test_later_completed_at_remains_presentable():
    result = _run("later")
    assert result["since"] == 43 and len(result["registry"]) == 2


def test_get_notifications_absent_falls_through_to_show():
    assert _run("no-query")["shown"]


def test_worker_failure_falls_back_to_direct_notification():
    result = _run("worker-fallback")
    assert result["since"] == 42 and result["shown"]


def test_failed_presentation_retries_without_cursor_advance():
    result = _run("failure")
    assert result["firstTick"] == {"since": 0, "ids": [], "marks": 0}
    assert result["since"] == 42 and result["alerts"] == 1


def test_successful_completion_updates_badge_before_later_retryable_failure():
    result = _run("partial-failure")
    assert result["since"] == 42 and result["badgeCalls"] == 1


def test_rejected_api_clears_in_flight_and_next_request_succeeds():
    result = _run("rejected-api")
    assert result["apiCalls"] == 2 and result["since"] == 42


def test_overlapping_polls_keep_one_request_in_flight():
    result = _run("overlap")
    assert result["apiCalls"] == 1 and result["since"] == 42


def test_earlier_failure_blocks_later_completion():
    assert _run("ordered")["since"] == 0


def test_muted_completion_consumes_without_alert():
    result = _run("muted")
    assert result["shown"] == [] and result["since"] == 42


def test_foreground_completion_keeps_toast_only():
    result = _run("foreground")
    assert result["shown"] == [] and result["toasts"]


def test_stale_generation_drops_every_effect():
    result = _run("stale")
    assert result["shown"] == [] and result["since"] == 0 and result["ids"] == []


def test_stale_generation_after_presentation_drops_local_effects():
    result = _run("stale-after-notification")
    assert result["shown"] and result["since"] == 0 and result["ids"] == [] and result["marks"] == []


def test_sessionless_poller_uses_root_url_and_no_session_unread():
    result = _run("sessionless")
    options = result["shown"][0]["options"]
    assert options["data"]["url"] == "https://example.test/app/"
    assert options["tag"] == "hermes-cron-profile-a-job-1-42" and result["marks"] == []


def test_request_permission_rejection_returns_reduced_outcome():
    result = _run("permission-reject")["result"]
    assert result == {"delivered": False, "alreadyDisplayed": False}


def test_existing_caller_defaults_preserve_omitted_sid_and_renotify():
    result = _run("owner-omitted")["result"]
    assert result["tag"] == "hermes-sess-active" and result["renotify"] is True


def test_omitted_sid_without_active_session_uses_current_location():
    result = _run("owner-no-session")["result"]
    assert result["data"]["url"] == "https://example.test/"


def test_explicit_renotify_false_is_preserved():
    assert _run("owner-false")["result"]["renotify"] is False


def test_owner_without_dedupe_does_not_query_displayed_records():
    result = _run("owner-no-dedupe")
    assert result["result"] == {"delivered": True, "alreadyDisplayed": False}
    assert result["getNotificationCalls"] == 0


def test_profile_capture_survives_change_during_api_await():
    result = _run("profile-transition")
    assert result["marks"][0][2]["profile"] == "profile-a"
    assert "profile-a" in result["shown"][0]["options"]["tag"]


def test_force_hidden_keeps_multi_completion_delivery_after_return_to_foreground():
    result = _run("force-hidden-transition")
    assert result["since"] == 43 and len(result["shown"]) == 2


def test_mid_batch_permission_loss_preserves_backlog_without_prompting():
    result = _run("permission-transition")
    assert result["since"] == 42 and len(result["shown"]) == 1 and result["permissionRequests"] == 0


def test_badge_failure_does_not_stick_in_flight_guard():
    result = _run("badge-failure")
    assert result["apiCalls"] == 2 and result["since"] == 42


def test_completion_preview_uses_owner_shaping_and_status_fallback():
    preview = _run("preview")["shown"][0]
    empty = _run("empty-preview")["shown"][0]
    absent = _run("hidden")["shown"][0]
    expected_preview = ("Preview text with spacing " + "x" * 150)[:100] + "…"
    assert preview["title"] == "Nightly" and preview["options"]["body"] == expected_preview
    assert empty["title"] == "Nightly" and empty["options"]["body"].startswith("cron_completion_status:")
    assert absent["title"] == "Nightly" and absent["options"]["body"].startswith("cron_completion_status:")


class _JSONHandler:
    def __init__(self):
        self.status = None
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass


def _payload(handler):
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _stub_cron_jobs(monkeypatch, *, output_dir=None, jobs=None):
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    if output_dir is not None:
        cron_jobs.OUTPUT_DIR = output_dir
    if jobs is not None:
        cron_jobs.list_jobs = lambda include_disabled=True: jobs
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)


def test_recent_success_preview_uses_latest_output_at_or_before_completion(monkeypatch, tmp_path):
    import api.routes as routes

    out_dir = tmp_path / "cron-out" / "job42"
    out_dir.mkdir(parents=True)
    files = {
        "old.md": (90, "## Response\nold response\n"),
        "new.md": (100, "## Response\nnew response\n"),
        "later.md": (110, "## Response\nlater response\n"),
    }
    for name, (mtime, content) in files.items():
        output = out_dir / name
        output.write_text(content, encoding="utf-8")
        os.utime(output, (mtime, mtime))
    jobs = [{"id": "job42", "name": "Job 42", "last_run_at": 100, "last_status": "ok"}]
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out", jobs=jobs)

    handler = _JSONHandler()
    routes._handle_cron_recent(handler, SimpleNamespace(query="since=0"))

    assert handler.status == 200
    completion = _payload(handler)["completions"][0]
    assert completion["output_preview"] == "new response"
    assert "later response" not in completion["output_preview"]
    assert set(completion) == {
        "job_id",
        "name",
        "status",
        "completed_at",
        "toast_notifications",
        "session_id",
        "output_preview",
    }


@pytest.mark.parametrize("case", ["missing", "unreadable", "empty", "metadata"])
def test_recent_preview_omits_unsafe_or_empty_output(monkeypatch, tmp_path, case):
    import api.routes as routes

    output_dir = tmp_path / "cron-out"
    output_file = output_dir / "job42" / "run.md"
    if case != "missing":
        output_file.parent.mkdir(parents=True)
        if case == "empty":
            content = "## Response\n"
        elif case == "metadata":
            content = "# Cron Job: Job 42\n\n**Job ID:** job42\n**Status:** ok\n"
        else:
            content = "## Response\nbody\n"
        output_file.write_text(content, encoding="utf-8")
        os.utime(output_file, (100, 100))
    if case == "unreadable":
        original_read_text = Path.read_text

        def unreadable(path, *args, **kwargs):
            if path == output_file:
                raise OSError("permission denied")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", unreadable)
    jobs = [{"id": "job42", "name": "Job 42", "last_run_at": 100, "last_status": "ok"}]
    _stub_cron_jobs(monkeypatch, output_dir=output_dir, jobs=jobs)

    handler = _JSONHandler()
    routes._handle_cron_recent(handler, SimpleNamespace(query="since=0"))

    assert handler.status == 200
    completion = _payload(handler)["completions"][0]
    assert "output_preview" not in completion


def test_recent_preview_reads_no_agent_output_after_separator(monkeypatch, tmp_path):
    import api.routes as routes

    output_dir = tmp_path / "cron-out" / "job42"
    output_dir.mkdir(parents=True)
    output = output_dir / "run.md"
    output.write_text(
        "# Cron Job: Job 42\n\n**Job ID:** job42\n**Mode:** no_agent (script)\n\n---\n\nscript output\n",
        encoding="utf-8",
    )
    os.utime(output, (100, 100))
    jobs = [{"id": "job42", "name": "Job 42", "last_run_at": 100, "last_status": "ok"}]
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out", jobs=jobs)

    handler = _JSONHandler()
    routes._handle_cron_recent(handler, SimpleNamespace(query="since=0"))

    assert handler.status == 200
    completion = _payload(handler)["completions"][0]
    assert completion["output_preview"] == "script output"


def test_recent_error_completion_keeps_projected_fields_without_preview(monkeypatch, tmp_path):
    import api.routes as routes

    output_dir = tmp_path / "cron-out" / "job42"
    output_dir.mkdir(parents=True)
    output = output_dir / "run.md"
    output.write_text("## Response\nold output\n", encoding="utf-8")
    os.utime(output, (100, 100))
    jobs = [{"id": "job42", "name": "Job 42", "last_run_at": 100, "last_status": "error", "toast_notifications": False}]
    _stub_cron_jobs(monkeypatch, output_dir=tmp_path / "cron-out", jobs=jobs)

    handler = _JSONHandler()
    routes._handle_cron_recent(handler, SimpleNamespace(query="since=0"))

    assert handler.status == 200
    completion = _payload(handler)["completions"][0]
    assert completion == {
        "job_id": "job42",
        "name": "Job 42",
        "status": "error",
        "completed_at": 100.0,
        "toast_notifications": False,
        "session_id": "",
    }
