"""Observable regressions for cron refresh races raised in PR #6767."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

NODE = shutil.which("node")
PANELS = Path(__file__).resolve().parents[1] / "static" / "panels.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")

HARNESS = r"""
const assert=require('assert');
const src=require('fs').readFileSync(PANELS_PATH,'utf8');
function extract(name){
 const start=src.search(new RegExp('(async\\s+)?function\\s+'+name+'\\s*\\('));
 assert(start>=0,name);let i=src.indexOf('{',start)+1,depth=1;
 while(depth&&i<src.length){if(src[i]==='{')depth++;else if(src[i]==='}')depth--;i++;}
 return src.slice(start,i);
}
function node(){return {innerHTML:'',style:{},children:[],classList:{add(){},toggle(){}},appendChild(x){this.children.push(x);},addEventListener(){}};}
const box=node(),button=node(),output=node();output.innerHTML='Loaded output';output.open=true;
const fields={cronList:box,cronRefreshBtn:button,cronDetailRuns:output,cronFormName:{value:'Saved'},cronFormSchedule:{value:'0 8 * * *'},cronFormPrompt:{value:'Do work'},cronFormError:node()};
function $(id){return fields[id]||null;}
const document={createElement:node};
let _cronList=[{id:'existing',name:'Existing'}],_cronOtherProfileCount=0,_showAllCronProfiles=false,_cronLoadGeneration=0,_cronAcceptedLoadGeneration=0;
const _cronNewJobIds=new Set(['newer']);
let _currentCronDetail='existing',_currentCronDetailKey='existing',_cronMode='detail',_editingCronId=null,_cronPreFormDetail=null,_cronIsDuplicate=false,_cronSelectedSkills=[];
let renders=0,clears=0,opened=null,writes=[],renderedJob=null;const pending=[];
let _cronDetailRefreshKey = null,_cronLatestLoadSettled=Promise.resolve();
const localStorage = {getItem(){return null;},setItem(){}};
let _currentPanel = 'tasks';
function api(url,opts){if(opts){writes.push(url);return Promise.resolve({id:'saved'});}return new Promise((resolve,reject)=>pending.push({resolve,reject}));}
async function loadCronProfiles(){} function loadCronGatewayNotice(){}
function esc(s){return String(s);} function t(s){return s;} function showToast(){}
function _cronStatusMeta(){return {state:'active',label:'on',listClass:'active'};}
function _cronItemId(j){return 'cron-'+j.id;} function _cronJobKey(j){return j.id;}
function _cronProfileLabel(p){return p;} function _cronOwnerProfileName(){return 'default';}
function _appendCronProfileToggle(){} function _clearCronDetail(){clears++;output.innerHTML='';output.open=false;}
function _renderCronDetail(job){renders++;renderedJob=job;output.innerHTML='Rebuilt';output.open=false;}
function openCronDetail(job){if(typeof job==='string')job=_cronList.find(j=>j.id===job);if(!job)return;opened=job;_cronMode='detail';}
function loadCrons(){return Promise.resolve();}
eval(extract('loadCrons'));eval(extract('saveCronForm'));eval(extract('cancelCronForm'));
eval(extract('cronPause'));eval(extract('cronResume'));
async function waitRequests(n){for(let i=0;i<50&&pending.length<n;i++)await Promise.resolve();assert.equal(pending.length,n);}
"""


def run_js(body, setup=""):
    script = HARNESS.replace("PANELS_PATH", json.dumps(str(PANELS)))
    script += setup
    script += """
const testTimeout=setTimeout(()=>{console.error('unfinished test body');process.exit(1);},5000);
(async()=>{""" + body + """})().then(()=>clearTimeout(testTimeout)).catch(e=>{clearTimeout(testTimeout);console.error(e);process.exit(1);});"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("rejects", [False, True])
def test_superseded_load_preserves_new_list_and_dom(rejects):
    settle = "pending[0].reject(new Error('old timeout'));" if rejects else "pending[0].resolve({jobs:[{id:'stale',name:'Stale'}]});"
    run_js("""
const old=loadCrons(true),fresh=loadCrons();await waitRequests(2);
pending[1].resolve({jobs:[{id:'newer',name:'Newer'}]});const freshData=await fresh;
const html=box.innerHTML,children=box.children.slice();
""" + settle + """
const oldData=await old;assert.deepEqual(_cronList.map(j=>j.id),['newer']);
assert.equal(box.innerHTML,html);assert.deepEqual(box.children,children);
assert.equal(button.disabled,false);assert.equal(button.style.opacity,'');
assert.equal(freshData.jobs[0].id,'newer');
""" + ("assert.equal(oldData,undefined);" if rejects else "assert.equal(oldData.jobs[0].id,'stale');"))


def test_current_load_failure_displays_error():
    run_js("const p=loadCrons(true);await waitRequests(1);pending[0].reject(new Error('current timeout'));await p;assert(box.innerHTML.includes('current timeout'));assert.equal(button.disabled,false);")


def test_background_refresh_keeps_existing_output_open_and_marks_list():
    jobs = [{"id": "existing", "name": "Existing"}, {"id": "newer", "name": "Newer"}]
    run_js("const p=loadCrons(false,{background:true});await waitRequests(1);pending[0].resolve({jobs:" + json.dumps(jobs) + "});const data=await p;assert.equal(renders,0);assert.equal(clears,0);assert.equal(output.innerHTML,'Loaded output');assert.equal(output.open,true);assert.deepEqual(_cronList,data.jobs);assert(box.children[1].innerHTML.includes('cron-new-dot'));")


@pytest.mark.parametrize("jobs", [[], [{"id": "other", "name": "Other"}]])
@pytest.mark.parametrize("mode", ["detail", "create", "edit"])
def test_background_refresh_clears_removed_selection_but_preserves_forms(jobs, mode):
    run_js("_cronMode=" + json.dumps(mode) + ";const p=loadCrons(false,{background:true});await waitRequests(1);pending[0].resolve({jobs:" + json.dumps(jobs) + "});await p;assert.equal(clears," + ("1" if mode == "detail" else "0") + ");")


@pytest.mark.parametrize("action,state", [("cronPause", "paused"), ("cronResume", "active")])
@pytest.mark.parametrize("older_first", [False, True])
def test_action_refresh_survives_background_supersession(action, state, older_first):
    new_job = json.dumps({"id": "existing", "name": "Existing", "state": state})
    old_job = json.dumps({"id": "existing", "name": "Older response", "state": "outdated"})
    settle_old = "pending[0].resolve({jobs:[" + old_job + "]});await action;"
    settle_new = "pending[1].resolve({jobs:[" + new_job + "]});await poll;"
    run_js("const action=" + action + "('existing');await waitRequests(1);"
           "const poll=loadCrons(false,{background:true});await waitRequests(2);" +
           (settle_old + settle_new if older_first else settle_new + settle_old) + """
assert.equal(renders,1);assert.equal(renderedJob.state,""" + json.dumps(state) + """);
assert.equal(_cronDetailRefreshKey,null);
// Subsequent unrelated background ticks must not rebuild it again.
const next=loadCrons(false,{background:true});await waitRequests(3);
pending[2].resolve({jobs:[""" + new_job + """]});await next;assert.equal(renders,1);
""")


@pytest.mark.parametrize("mode,key", [("edit", "existing"), ("create", "existing"), ("detail", "other")])
def test_pending_refresh_does_not_override_new_selection_or_form(mode, key):
    run_js("""
const old=loadCrons();await waitRequests(1);
_cronMode=""" + json.dumps(mode) + ";_currentCronDetailKey=" + json.dumps(key) + ";" + """
const poll=loadCrons(false,{background:true});await waitRequests(2);
pending[1].resolve({jobs:[{id:'existing',name:'Existing'},{id:'other',name:'Other'}]});await poll;
pending[0].resolve({jobs:[{id:'existing',name:'Old'}]});await old;
assert.equal(renders,0);assert.equal(_cronDetailRefreshKey,null);
""")


def test_failed_latest_refresh_retries_once_without_another_completion():
    run_js("""
const old=cronPause('existing');await waitRequests(1);
const poll=loadCrons(false,{background:true});await waitRequests(2);
pending[1].reject(new Error('temporary'));await waitRequests(3);
pending[0].resolve({jobs:[{id:'existing',name:'Old'}]});await old;
assert.equal(renders,0);assert.equal(_cronDetailRefreshKey,'existing');
pending[2].resolve({jobs:[{id:'existing',name:'Current',state:'paused'}]});await poll;
assert.equal(renders,1);assert.equal(renderedJob.state,'paused');assert.equal(_cronDetailRefreshKey,null);
""")


def test_failed_foreground_retry_is_bounded():
    run_js("""
const old=cronPause('existing');await waitRequests(1);
const poll=loadCrons(false,{background:true});await waitRequests(2);
pending[1].reject(new Error('503'));await waitRequests(3);
pending[2].reject(new Error('retry failed'));await poll;
pending[0].resolve({jobs:[{id:'existing',name:'Old'}]});await old;
assert.equal(pending.length,3);assert(box.innerHTML.includes('retry failed'));
""")


@pytest.mark.parametrize("cancel", ["_currentPanel='chat';", "_cronMode='edit';", "_cronDetailRefreshKey=null;"])
def test_background_failure_does_not_retry_cancelled_or_hidden_intent(cancel):
    run_js("""
const old=loadCrons();await waitRequests(1);
const poll=loadCrons(false,{background:true});await waitRequests(2);
""" + cancel + """
pending[1].reject(new Error('503'));await poll;assert.equal(pending.length,2);
pending[0].resolve({jobs:[{id:'existing'}]});await old;
""")


REAL_DETAIL = r"""
for (const id of ['taskDetailTitle','taskDetailBody','taskDetailEmpty',
  'btnRunTaskDetail','btnPauseTaskDetail','btnResumeTaskDetail','btnEditTaskDetail',
  'btnDuplicateTaskDetail','btnDeleteTaskDetail','btnCancelTaskDetail','btnSaveTaskDetail']) fields[id]=node();
const expansionStorage=new Map();
localStorage.getItem=key=>expansionStorage.get(key)||null;
localStorage.setItem=(key,value)=>expansionStorage.set(key,value);
function jsArg(s){return JSON.stringify(s);}
function _cronProfileTitle(p){return p||'Default';}
function _cronProfileName(p){return p||'default';}
function _isCronNeedsAttention(job){return job.state==='needs_attention';}
function _isCronScheduleError(job){return job.state==='schedule_error';}
function _loadCronDetailRuns(){} function _stopCronWatch(){}
function _findCronJob(job){return typeof job==='object'?job:_cronList.find(j=>j.id===job);}
function _clearCronUnreadForJob(){} function _checkCronWatchOnDetail(){}
function _closeMobileSidebarAfterPanelSelection(){}
document.querySelectorAll=()=>[];
eval(['_renderCronDetail','toggleCronPromptExpanded','_setCronHeaderButtons',
 '_cronPanelExpandKey','_cronExpansionGet','_cronExpansionSet','_cronStatusMeta',
 '_isCronScriptJob','_cronModeLabel','_cronOutputTitle','_cronAgentPromptCardHtml',
 '_clearCronDetail','openCronDetail'].map(extract).join('\n'));
function assertControls(state){
 assert.equal(_currentCronDetail.state,state);
 assert(fields.taskDetailBody.innerHTML.includes('cron_status_'+state));
 assert.equal(fields.btnPauseTaskDetail.style.display,state==='paused'?'none':'');
 assert.equal(fields.btnResumeTaskDetail.style.display,state==='paused'?'':'none');
}
"""


@pytest.mark.parametrize("action,state,initial", [("cronPause", "paused", "active"), ("cronResume", "active", "paused")])
@pytest.mark.parametrize("older_first", [False, True])
@pytest.mark.parametrize("expanded", [False, True])
def test_prompt_toggle_preserves_action_refresh_with_real_renderer(action, state, initial, older_first, expanded):
    job = {"id": "existing", "name": "Existing", "prompt": "Task prompt", "state": initial}
    fresh_job = dict(job, state=state)
    older = "pending[0].resolve({jobs:" + json.dumps([fresh_job]) + "});await action;"
    latest = "pending[1].resolve({jobs:" + json.dumps([fresh_job]) + "});await poll;"
    run_js("_renderCronDetail(" + json.dumps(job) + ");" +
           "_cronExpansionSet(_cronPanelExpandKey('existing','prompt')," + json.dumps(expanded) + ");" +
           "const action=" + action + "('existing');await waitRequests(1);" +
           "toggleCronPromptExpanded('existing');assertControls(" + json.dumps(initial) + ");" +
           "assert.equal(_cronExpansionGet(_cronPanelExpandKey('existing','prompt'))," + json.dumps(not expanded) + ");" +
           "const poll=loadCrons(false,{background:true});await waitRequests(2);" +
           (older + latest if older_first else latest + older) +
           "assertControls(" + json.dumps(state) + ");assert.equal(_cronDetailRefreshKey,null);", setup=REAL_DETAIL)


@pytest.mark.parametrize("cancel", ["_renderCronDetail({id:'other',state:'active',prompt:'Other'});", "_clearCronDetail();"])
def test_real_detail_cancellation_clears_pending_intent(cancel):
    run_js("_renderCronDetail({id:'existing',state:'active'});" +
           "const action=cronPause('existing');await waitRequests(1);assert.equal(_cronDetailRefreshKey,'existing');" +
           cancel + "assert.equal(_cronDetailRefreshKey,null);" +
           "pending[0].resolve({jobs:[{id:'other',state:'active'}]});await action;", setup=REAL_DETAIL)


def test_user_requested_refresh_still_updates_detail():
    run_js("const p=loadCrons();await waitRequests(1);pending[0].resolve({jobs:[{id:'existing',name:'Updated'}]});await p;assert.equal(renders,1);")


@pytest.mark.parametrize("action,state,initial", [("cronPause", "paused", "active"), ("cronResume", "active", "paused")])
def test_action_refresh_survives_away_and_back(action, state, initial):
    job = {"id": "existing", "name": "Existing", "state": initial}
    other = {"id": "other", "name": "Other", "state": "active"}
    fresh = dict(job, state=state)
    run_js("_cronList=" + json.dumps([job, other]) + ";openCronDetail('existing');" +
           "const action=" + action + "('existing');await waitRequests(1);" +
           "openCronDetail('other');openCronDetail('existing');" +
           "const poll=loadCrons(false,{background:true});await waitRequests(2);" +
           "pending[1].resolve({jobs:" + json.dumps([fresh, other]) + "});await poll;" +
           "assertControls(" + json.dumps(state) + ");" +
           "for(const req of pending)req.resolve({jobs:" + json.dumps([fresh, other]) + "});await action;", setup=REAL_DETAIL)


def test_background_refresh_preserves_list_scroll_at_response_time():
    run_js("""
let html='';box.scrollTop=40;
Object.defineProperty(box,'innerHTML',{get(){return html;},set(value){html=value;box.scrollTop=0;}});
const poll=loadCrons(false,{background:true});await waitRequests(1);
box.scrollTop=160; // reader scrolled while the request was in flight
pending[0].resolve({jobs:[{id:'existing',name:'Existing'}]});await poll;
assert.equal(box.scrollTop,160);
""")


@pytest.mark.parametrize("editing", [False, True])
def test_save_does_not_select_job_from_superseded_response(editing):
    prefix = "_cronMode=" + json.dumps("edit" if editing else "create") + ";" + ("_editingCronId='saved';" if editing else "")
    run_js(prefix + "const save=saveCronForm();await waitRequests(1);const poll=loadCrons(false,{background:true});await waitRequests(2);pending[1].resolve({jobs:[{id:'existing',name:'Existing'}]});await poll;pending[0].resolve({jobs:[{id:'existing',name:'Existing'},{id:'saved',name:'Saved'}]});await waitRequests(3);pending[2].resolve({jobs:[{id:'existing',name:'Existing'}]});await save;assert.equal(opened,null);assert.equal(writes.length,1);")


@pytest.mark.parametrize("editing", [False, True])
@pytest.mark.parametrize("accepted_first", [False, True])
def test_current_save_opens_job_from_superseding_accepted_list(editing, accepted_first):
    prefix = "_cronMode=" + json.dumps("edit" if editing else "create") + ";" + ("_editingCronId='saved';" if editing else "")
    accepted_jobs = "[{id:'existing',name:'Existing'},{id:'saved',name:'Accepted saved'}]"
    stale_jobs = "[{id:'existing',name:'Existing'},{id:'saved',name:'Stale saved'}]"
    ordering = (
        f"pending[1].resolve({{jobs:{accepted_jobs}}});await poll;"
        f"pending[0].resolve({{jobs:{stale_jobs}}});await waitRequests(3);"
        f"pending[2].resolve({{jobs:{accepted_jobs}}});"
        if accepted_first
        else
        f"pending[0].resolve({{jobs:{stale_jobs}}});await waitRequests(3);"
        f"pending[1].resolve({{jobs:{accepted_jobs}}});await poll;"
        f"pending[2].resolve({{jobs:{accepted_jobs}}});"
    )
    run_js(prefix + """
const save=saveCronForm();await waitRequests(1);
const poll=loadCrons(false,{background:true});await waitRequests(2);
""" + ordering + """
await save;
assert.equal(opened.id,'saved');assert.equal(opened.name,'Accepted saved');assert.equal(writes.length,1);
""")


@pytest.mark.parametrize("editing", [False, True])
@pytest.mark.parametrize("accepted_first", [False, True])
def test_current_save_survives_second_supersession(editing, accepted_first):
    prefix = "_cronMode=" + json.dumps("edit" if editing else "create") + ";" + ("_editingCronId='saved';" if editing else "")
    saved = "[{id:'existing',name:'Existing'},{id:'saved',name:'Accepted saved'}]"
    run_js(prefix + """
const save=saveCronForm();await waitRequests(1); // save-owned list read A
const firstPoll=loadCrons(false,{background:true});await waitRequests(2); // B supersedes A
pending[1].resolve({jobs:""" + saved + """});await firstPoll;
pending[0].resolve({jobs:""" + saved + """});await waitRequests(3); // recovery C
const secondPoll=loadCrons(false,{background:true});await waitRequests(4); // D supersedes C
""" + (
        "pending[3].resolve({jobs:" + saved + "});await secondPoll;"
        "pending[2].resolve({jobs:" + saved + "});"
        if accepted_first
        else
        "pending[2].resolve({jobs:" + saved + "});"
        "pending[3].resolve({jobs:" + saved + "});await secondPoll;"
    ) + """
await save;
assert.equal(opened.id,'saved');assert.equal(opened.name,'Accepted saved');
assert.equal(writes.length,1);
""")


@pytest.mark.parametrize("editing", [False, True])
def test_current_save_adopts_saved_job_from_fourth_awaited_reader(editing):
    prefix = "_cronMode=" + json.dumps("edit" if editing else "create") + ";" + ("_editingCronId='saved';" if editing else "")
    saved = "[{id:'existing',name:'Existing'},{id:'saved',name:'Accepted saved'}]"
    run_js(prefix + """
const save=saveCronForm();await waitRequests(1); // save-owned list read A
const firstPoll=loadCrons(false,{background:true});await waitRequests(2); // B supersedes A
pending[1].resolve({jobs:[{id:'existing',name:'Existing'}]});await firstPoll;
pending[0].resolve({jobs:""" + saved + """});await waitRequests(3); // recovery C
const readers=[];
readers.push(loadCrons(false,{background:true}));await waitRequests(4); // D supersedes C
pending[2].resolve({jobs:""" + saved + """});
for(let index=3;index<6;index++){
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(clears,0);assert.equal(opened,null);
  readers.push(loadCrons(false,{background:true}));await waitRequests(index+2);
  pending[index].resolve({jobs:[{id:'existing',name:'Existing'}]});
}
await new Promise(resolve=>setImmediate(resolve));
assert.equal(clears,0);assert.equal(opened,null);
pending[6].resolve({jobs:""" + saved + """}); // G is fourth awaited reader
await Promise.all(readers);await save;
assert.equal(opened.id,'saved');assert.equal(opened.name,'Accepted saved');
assert.equal(clears,0);assert.equal(writes.length,1);
""")


@pytest.mark.parametrize("editing", [False, True])
def test_successful_write_closes_form_after_bounded_reconciliation_failure(editing):
    prefix = "_cronMode=" + json.dumps("edit" if editing else "create") + ";" + ("_editingCronId='saved';" if editing else "")
    run_js(prefix + """
const save=saveCronForm();await waitRequests(1);
pending[0].resolve({jobs:[{id:'existing',name:'Existing'}]});
await waitRequests(2);
pending[1].resolve({jobs:[{id:'existing',name:'Still missing saved task'}]});
await save;
assert.equal(opened,null);assert.equal(clears,1);assert.equal(writes.length,1);
""")


@pytest.mark.parametrize("editing", [False, True])
@pytest.mark.parametrize("replacement", ["deleted", "selection", "new_form", "panel"])
def test_save_completion_preserves_newer_view(editing, replacement):
    prefix = "_cronMode=" + json.dumps("edit" if editing else "create") + ";" + ("_editingCronId='saved';" if editing else "")
    switch = {
        "deleted": "_cronMode='detail';_currentCronDetail=null;_currentCronDetailKey=null;",
        "selection": "_cronMode='detail';_currentCronDetail='other';_currentCronDetailKey='other';",
        "new_form": "fields.cronFormError=node();_editingCronId='other';",
        "panel": "_currentPanel='chat';",
    }[replacement]
    run_js(prefix + "const save=saveCronForm();await waitRequests(1);" + switch + """
const newer=loadCrons(false,{background:true});await waitRequests(2);
pending[1].resolve({jobs:[{id:'other',name:'Other'}]});await newer;
pending[0].resolve({jobs:[{id:'saved',name:'Deleted saved job'}]});await save;
assert.equal(opened,null);
assert.deepEqual(_cronList.map(j=>j.id),['other']);
""" + ("assert.equal(_editingCronId,'other');" if replacement == "new_form" else ""))


@pytest.mark.parametrize("editing", [False, True])
def test_current_save_selects_job_from_accepted_read(editing):
    prefix = "_cronMode=" + json.dumps("edit" if editing else "create") + ";" + ("_editingCronId='saved';" if editing else "")
    run_js(prefix + """
const save=saveCronForm();await waitRequests(1);
pending[0].resolve({jobs:[{id:'saved',name:'Saved'}]});await save;
assert.equal(opened.id,'saved');assert.equal(_cronMode,'detail');
""")


@pytest.mark.parametrize("editing", [False, True])
@pytest.mark.parametrize(
    "action,state,initial",
    [("cronPause", "paused", "active"), ("cronResume", "active", "paused")],
)
def test_save_background_refresh_honors_selected_action_refresh(
    editing, action, state, initial
):
    prefix = (
        "_cronMode="
        + json.dumps("edit" if editing else "create")
        + ";"
        + ("_editingCronId='saved';" if editing else "")
    )
    run_js(
        prefix
        + """
const saved={id:'saved',name:'Saved',state:'active',prompt:'Saved prompt'};
const other={id:'other',name:'Other',state:"""
        + json.dumps(initial)
        + """,prompt:'Other prompt'};
const freshOther={...other,state:"""
        + json.dumps(state)
        + """};
_cronList=[saved,other];_currentCronDetailKey='saved';_currentCronDetail=saved;
let renderCalls=0;
const productionRender=_renderCronDetail;
_renderCronDetail=(job)=>{renderCalls++;return productionRender(job);};
let settleSave;
const originalApi=api;
api=function(url,opts){
  if(opts && (url==='/api/crons/create'||url==='/api/crons/update')){
    writes.push(url);
    return new Promise(resolve=>{settleSave=resolve;});
  }
  return originalApi(url,opts);
};
const save=saveCronForm();
openCronDetail('other');
const rendersBefore=renderCalls;
const actionPromise="""
        + action
        + """('other');
await waitRequests(1);
assert.equal(_cronDetailRefreshKey,'other');
settleSave({id:'saved'});
await waitRequests(2);
pending[1].resolve({jobs:[saved,freshOther]});
await save;
assert.equal(renderCalls,rendersBefore+1);
assertControls("""
        + json.dumps(state)
        + """);
assert.equal(_cronDetailRefreshKey,null);
pending[0].resolve({jobs:[saved,other]});
await actionPromise;
assertControls("""
        + json.dumps(state)
        + """);
""",
        setup=REAL_DETAIL,
    )


@pytest.mark.parametrize("editing", [False, True])
def test_successful_write_refreshes_list_in_background_after_navigation(editing):
    prefix = "_cronMode=" + json.dumps("edit" if editing else "create") + ";" + ("_editingCronId='saved';" if editing else "")
    run_js(prefix + """
let settleWrite;
const originalApi=api;
api=function(url,opts){if(opts){assert(opts);return new Promise(resolve=>{settleWrite=resolve;});}return originalApi(url,opts);};
const saved={id:'saved',name:'Saved',state:'active',prompt:'Saved prompt'};
const other={id:'other',name:'Other',state:'paused',prompt:'Other prompt'};
_cronList=[saved,other];_currentCronDetailKey='saved';_currentCronDetail=saved;
const realLoad=loadCrons;let loadOptions;
loadCrons=(...args)=>{loadOptions=args[1];return realLoad(...args);};
const save=saveCronForm();
openCronDetail('other');
const visibleBefore=fields.taskDetailBody.innerHTML;
settleWrite({id:'saved'});
await waitRequests(1);
pending[0].resolve({jobs:[{...saved,name:'Updated saved'},{...other,name:'Other'}]});
await save;
assert.equal(loadOptions.background,true);
assert.equal(_cronList[0].name,'Updated saved');
assert.equal(_currentCronDetail.id,'other');
assert.equal(_currentCronDetail.name,'Other');
assert.equal(fields.taskDetailBody.innerHTML,visibleBefore);
assert.equal(fields.btnResumeTaskDetail.style.display,'');
""", setup=REAL_DETAIL)


def test_edit_cancel_during_write_refreshes_visible_saved_detail():
    run_js("""
const saved={id:'saved',name:'Existing',state:'active',prompt:'Old prompt'};
_cronList=[saved];_renderCronDetail(saved);
_cronMode='edit';_editingCronId='saved';_cronPreFormDetail=saved;
let settleWrite;
const originalApi=api;
api=function(url,opts){if(opts)return new Promise(resolve=>{settleWrite=resolve;});return originalApi(url,opts);};
const realLoad=loadCrons;let loadOptions;
loadCrons=(...args)=>{loadOptions=args[1];return realLoad(...args);};
const save=saveCronForm();
cancelCronForm();
assert.equal(_cronMode,'read');assert.equal(_currentCronDetail.name,'Existing');
settleWrite({id:'saved'});await waitRequests(1);
pending[0].resolve({jobs:[{...saved,name:'Renamed',prompt:'New prompt'}]});
await save;
assert.equal(!!(loadOptions&&loadOptions.background),false);
assert.equal(_currentCronDetail.name,'Renamed');
assert(fields.taskDetailBody.innerHTML.includes('New prompt'));
assert.equal(fields.btnPauseTaskDetail.style.display,'');
""", setup=REAL_DETAIL)


def test_edit_cancel_uses_row_from_accepted_completion_refresh():
    run_js("""
const saved={id:'saved',name:'Existing',state:'active',prompt:'Old prompt'};
_cronList=[saved];_renderCronDetail(saved);
_cronMode='edit';_editingCronId='saved';_cronPreFormDetail=saved;
let settleWrite;
const originalApi=api;
api=function(url,opts){if(opts){writes.push(url);return new Promise(resolve=>{settleWrite=resolve;});}return originalApi(url,opts);};
const save=saveCronForm();
settleWrite({id:'saved'});await waitRequests(1); // save-owned list read
const poll=loadCrons(false,{background:true});await waitRequests(2);
const renamed={...saved,name:'Renamed',prompt:'New prompt'};
pending[1].resolve({jobs:[renamed]});await poll;
assert.equal(_cronList[0].name,'Renamed');assert.equal(_cronMode,'edit');
cancelCronForm();
pending[0].resolve({jobs:[renamed]});await save;
assert.equal(_currentCronDetail.name,'Renamed');
assert(fields.taskDetailBody.innerHTML.includes('New prompt'));
assert.equal(writes.length,1);
""", setup=REAL_DETAIL)


def test_edit_cancel_refresh_preserves_another_task_selected_during_read():
    run_js("""
const saved={id:'saved',name:'Existing',state:'active',prompt:'Old prompt'};
const other={id:'other',name:'Other',state:'active',prompt:'Other prompt'};
_cronList=[saved,other];
let renderCalls=0;
const productionRender=_renderCronDetail;
_renderCronDetail=(job)=>{renderCalls++;return productionRender(job);};
_renderCronDetail(saved);
_cronMode='edit';_editingCronId='saved';_cronPreFormDetail=saved;
let settleWrite;
const originalApi=api;
api=function(url,opts){if(opts)return new Promise(resolve=>{settleWrite=resolve;});return originalApi(url,opts);};
const save=saveCronForm();
cancelCronForm();
settleWrite({id:'saved'});await waitRequests(1);
openCronDetail(other);
output.innerHTML='Other loaded output';output.open=true;
const detailBefore=fields.taskDetailBody.innerHTML;
const rendersBefore=renderCalls;
pending[0].resolve({jobs:[{...saved,name:'Renamed',prompt:'New prompt'},other]});
await save;
assert.equal(_currentCronDetail.id,'other');
assert.equal(fields.taskDetailBody.innerHTML,detailBefore);
assert.equal(renderCalls,rendersBefore);
assert.equal(output.innerHTML,'Other loaded output');
assert.equal(output.open,true);
""", setup=REAL_DETAIL)
