"""A shared draft candidate must name a successfully persisted payload (#7824)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = (Path(__file__).parents[1] / 'static/sessions.js').read_text()


@pytest.mark.parametrize('scenario', ['pending', 'success', 'failure', 'erase', 'stale', 'switch', 'immediate', 'submit'])
def test_draft_candidate_requires_successful_current_save(scenario):
    if not shutil.which('node'):
        pytest.skip('node unavailable')
    saves = SRC[SRC.index('function _saveComposerDraft('):SRC.index('// Restore composer draft')]
    start = SRC.index('function _clearComposerDraft(')
    end = SRC.index('\nfunction ', start + 1)
    clear = SRC[start:end]
    script = r'''
let pointer='A', timer=null, _draftSaveTimer=null;
let _newChatDraftSaveGeneration=0;
const _DRAFT_SAVE_DELAY_MS=400;
const S={session:{session_id:'B',message_count:0,title:'New Chat'}};
const requests=[];
const _composerDraftKnownPayloadSessions=new Set();
const _composerDraftFilesForPersist=x=>x||[];
const _clearComposerDraftRestoreSuppression=()=>{};
const _suppressComposerDraftRestoreAfterSubmit=()=>{};
const _rememberNewChatDraftSession=session=>{pointer=session.session_id;};
const _clearRememberedNewChatDraftSession=sid=>{if(pointer===sid)pointer='';};
const setTimeout=fn=>{timer=fn;return fn;};
const clearTimeout=()=>{timer=null;};
function api(url,opts){return new Promise((resolve,reject)=>requests.push({resolve,reject,body:JSON.parse(opts.body)}));}
''' + saves + clear + r'''
(async()=>{
const scenario=SCENARIO;
if(scenario==='immediate')_saveComposerDraftNow('B','draft',[]);
else _saveComposerDraft('B','draft',[]);
const before=pointer;
if(scenario==='pending'){process.stdout.write(JSON.stringify({before,after:pointer}));return;}
if(scenario!=='immediate')timer();
if(scenario==='erase')_saveComposerDraft('B','',[]);
if(scenario==='stale')_saveComposerDraft('B','newer',[]);
if(scenario==='switch')S.session={session_id:'C'};
if(scenario==='submit')_clearComposerDraft('B','draft',[]);
if(scenario==='failure')requests[0].reject(new Error('save failed'));
else requests[0].resolve({});
await new Promise(resolve=>process.nextTick(resolve));
process.stdout.write(JSON.stringify({before,after:pointer}));
})();
'''
    script = script.replace('SCENARIO', json.dumps(scenario))
    result = subprocess.run(['node', '-'], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert actual['before'] == 'A', 'an unpersisted draft displaced the previous restorable draft'
    assert actual['after'] == ('B' if scenario in {'success', 'switch', 'immediate'} else 'A')
