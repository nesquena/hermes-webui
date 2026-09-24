"""Composed prompt-owner and real delivery regressions for #7493."""
from tests.test_pwa_notification_controls import MESSAGES_JS, _extract_fn, _run_node


def run(body, extra=()):
    names = ['_promptNotifyKey', '_retirePromptNotifyKey', '_notifyPromptCard',
             '_approvalPromptGeneration', '_bumpApprovalPromptGeneration',
             '_clarifyPromptGeneration', '_bumpClarifyPromptGeneration',
             '_rememberApprovalPending', '_clearApprovalPendingForSession',
             '_clearClarifyPendingForSession', '_approvalDismissKey', '_legacyApprovalDismissKey',
             '_getDismissedApprovals', '_isApprovalDismissed', '_markApprovalDismissed',
             '_unmarkApprovalDismissed', 'dismissApprovalCard',
             '_startApprovalFallbackPoll', 'showApprovalCard',
             'activeSessionHasPendingPromptAttention']
    source = '\n'.join(_extract_fn(MESSAGES_JS, n) for n in names + list(extra))
    return _run_node('''
const _promptNotifySeen = new Map();
const _approvalPendingBySession = new Map(), _clarifyPendingBySession = new Map();
const _approvalPromptGenerationBySession = new Map(), _clarifyPromptGenerationBySession = new Map();
const _DISMISSED_APPROVALS_KEY = 'dismissed';
const store = new Map();
const localStorage = {getItem:k=>store.get(k), setItem:(k,v)=>store.set(k,v)};
const S = {busy:true,session:{session_id:'s'}};
const _approvalPromptBelongsToActiveSession=()=>false;
let _approvalSessionId = 's', _approvalCurrentId = 'id';
let _approvalFallbackPollInFlight=false, _approvalPollTimer;
let _clarifyPollingSessionId, _clarifyPollTimer, _clarifyFallbackPollInFlight=false;
const _promptActiveSessionId=()=> 's';
const _approvalPollingSessionMissingOrMismatched=()=>false;
const _hideApprovalCardIfOwner=()=>{};
const stopApprovalPolling=()=>{}, stopApprovalPollingForSession=()=>{};
const hideApprovalCard=()=>{};
const setInterval=()=>1;
const _isSessionActivelyViewed=()=>false;
const flush=()=>new Promise(r=>setImmediate(r));
''' + source + '\n(async()=>{\n' + body + '\n})().catch(e=>{console.error(e);process.exit(1)});')


def test_legacy_dismissal_does_not_transfer_to_distinct_full_owner():
    result = run(r'''
const pending={approval_id:'id',run_id:'run-2',_gateway_mirror_token:'token-2'};
const legacy='s'+'\0'+'id';
store.set(_DISMISSED_APPROVALS_KEY,JSON.stringify([legacy]));
const dismissed=_isApprovalDismissed('s',pending);
const keys=JSON.parse(store.get(_DISMISSED_APPROVALS_KEY));
console.log(JSON.stringify({dismissed,legacyPresent:keys.includes(legacy),fullPresent:keys.includes(_approvalDismissKey('s',pending))}));
''')
    assert result == dict(dismissed=False, legacyPresent=False, fullPresent=False)



def test_legacy_dismissal_migrates_when_no_stronger_owner_exists():
    result = run(r"""
const pending={approval_id:"id"};
const legacy="s"+"\0"+"id";
store.set(_DISMISSED_APPROVALS_KEY,JSON.stringify([legacy]));
const dismissed=_isApprovalDismissed("s",pending);
const keys=JSON.parse(store.get(_DISMISSED_APPROVALS_KEY));
console.log(JSON.stringify({dismissed,legacyPresent:keys.includes(legacy),fullPresent:keys.includes(_approvalDismissKey("s",pending))}));
""")
    assert result == dict(dismissed=True, legacyPresent=False, fullPresent=True)

def test_dismiss_poll_resolution_and_new_run_owner():
    result = run('''
const sent=[];
global.sendBrowserNotification=()=>{sent.push(1);return true};
const a={_session_id:'s',approval_id:'id',run_id:'r1',_gateway_mirror_token:'t1'};
global.showApprovalForSession=(sid,p)=>showApprovalCard(p,1);
showApprovalForSession('s',a);
dismissApprovalCard();
const dismissed=_isApprovalDismissed('s',a);
if(activeSessionHasPendingPromptAttention())throw Error('dismissed attention remains');
showApprovalForSession('s',a);
const quiet=sent.length;
const b={...a,run_id:'r2',_gateway_mirror_token:'t2'};
showApprovalForSession('s',b);
const replacement=sent.length;
dismissApprovalCard();
global.api=async()=>({pending:null});
_startApprovalFallbackPoll('s');await flush();
showApprovalForSession('s',b);
console.log(JSON.stringify({dismissed,quiet,replacement,reused:sent.length}));
''')
    assert result == dict(dismissed=True, quiet=1, replacement=2, reused=3)


def test_empty_map_terminal_clear_fences_inflight_poll():
    result = run('''
let resolve;const shown=[];
global.api=()=>new Promise(r=>resolve=r);
global.showApprovalForSession=(sid,p)=>shown.push(p);
_startApprovalFallbackPoll('s');
_clearApprovalPendingForSession('s');
_clearClarifyPendingForSession('s');
resolve({pending:{approval_id:'stale'}});await flush();
console.log(JSON.stringify({shown,clarifyGeneration:_clarifyPromptGeneration('s')}));
''')
    assert result == dict(shown=[], clarifyGeneration=1)


def test_real_delivery_denied_failed_and_inflight_retry():
    result = run('''
const window=global.window={_notificationsEnabled:true};
let attempts=0,fail=false;
window.Notification=global.Notification=function(){attempts++;if(fail)throw Error('unavailable')};
Notification.permission='denied';
global.assistantDisplayName=()=> 'Hermes';
global._notificationOptions=()=>({});
Object.defineProperty(global,'navigator',{value:{serviceWorker:{getRegistration:async()=>({active:true,showNotification:async()=>{throw Error('SW failed')}})}}});
const p={approval_id:'id'};
_notifyPromptCard('approval','s',p);await flush();
Notification.permission='granted';fail=true;
_notifyPromptCard('approval','s',p);await flush();
const failedConsumed=_promptNotifySeen.size;
fail=false;
_notifyPromptCard('approval','s',p);
_notifyPromptCard('approval','s',p);await flush();
_notifyPromptCard('approval','s',p);await flush();
console.log(JSON.stringify({attempts,failedConsumed,consumed:_promptNotifySeen.size}));
''', ('sendBrowserNotification', '_showPwaNotification'))
    assert result == dict(attempts=2, failedConsumed=0, consumed=1)


def test_empty_clarify_poll_cannot_resurrect_after_terminal_clear():
    result = run('''
let _clarifyPollingSessionId, _clarifyPollTimer, _clarifyFallbackPollInFlight=false;
let resolve;const shown=[];
global.api=()=>new Promise(r=>resolve=r);
global.showClarifyForSession=(sid,p)=>shown.push(p);
_startClarifyFallbackPoll('s');
_clearClarifyPendingForSession('s');
resolve({pending:{clarify_id:'stale'}});await flush();
console.log(JSON.stringify({shown}));
''', ('_startClarifyFallbackPoll',))
    assert result == dict(shown=[])



def test_default_permission_is_auto_requested_once_not_every_poll_tick():
    """A pending prompt re-surfaces on every 1.5s poll tick; with permission still
    'default' (a second browser/device) the automatic, non-gesture request must be
    made at most once per page, so the user is not re-prompted and re-toasted
    "notifications denied" on every tick. The explicit Send-test path still asks."""
    result = run(r'''
const window=global.window={_notificationsEnabled:true};
let requests=0; const toasts=[];
global.t=(k)=>k;
global.showToast=(msg)=>toasts.push(msg);
global.updateNotificationPermissionStatus=()=>{};
window.Notification=global.Notification=function(){};
Notification.permission='default';
Notification.requestPermission=()=>{requests++;return Promise.resolve('default');};
global.assistantDisplayName=()=> 'Hermes';
global._notificationOptions=()=>({});
Object.defineProperty(global,'navigator',{value:{}});
const p={approval_id:'id'};
for (let i=0;i<5;i++){ _notifyPromptCard('approval','s',p); await flush(); await flush(); }
const autoRequests=requests, autoToasts=toasts.length, seenAfterAuto=_promptNotifySeen.size;
await sendBrowserNotification('Test','body',{force:true}); await flush();
console.log(JSON.stringify({autoRequests,autoToasts,seenAfterAuto,forcedRequests:requests-autoRequests}));
''', ('sendBrowserNotification', '_showPwaNotification', 'requestNotificationPermission'))
    assert result == dict(autoRequests=1, autoToasts=1, seenAfterAuto=0, forcedRequests=1)
