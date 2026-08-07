"""Execute session-rotation tab-state synchronization for both restore paths."""
import json
from pathlib import Path
import subprocess

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", ["completion", "settled"])
@pytest.mark.parametrize("storage_blocked", [False, True])
@pytest.mark.parametrize("url_helper_available", [False, True])
def test_stream_completion_syncs_rotated_session_id_to_tab_state(
    path, storage_blocked, url_helper_available,
):
    """A -> B updates browser anchors even if storage is unavailable.

    Execute the actual rebind-through-tab-sync statements, bounded by the next
    processing stage rather than a character window that unrelated code grows
    past. Transcript helpers are stubs; session assignment and tab writes are not.
    """
    if path == "completion":
        start = MESSAGES_JS.index("S.session=completedSession;")
        end = MESSAGES_JS.index("const _markerOnlyAssistantError=", start)
    else:
        restore_start = MESSAGES_JS.index("async function _restoreSettledSession")
        staged = MESSAGES_JS.index("const _nextMsgs3018=Array.isArray(session.messages)?session.messages:[];", restore_start)
        start = MESSAGES_JS.rindex("S.session=session;", restore_start, staged)
        # Paging/revision bookkeeping now precedes the tab sync. Include the
        # storage and URL writes, stopping before transcript staging begins.
        end = MESSAGES_JS.index("const _stagedMessages=", start)
    block = MESSAGES_JS[start:end]
    script = """
const assert=require('node:assert/strict');
const S={session:{session_id:'A'},messages:[{role:'user',content:'Earlier'}]};
const session={session_id:'B',_messages_offset:900,_messages_truncated:true,message_count:902,messages:[{role:'user',content:'Earlier'},{role:'assistant',content:'Done'}],regeneration_revision:'rev-2'};
const d={session};
const completedSession=session;
const completedSid='B';
const pathName=PATH_VALUE;
const _terminalSessionSameSid=false;
const _mergedTerminalSession=session;
const _currentTerminalMessages=S.messages;
let _messagesTruncated=true,_oldestIdx=8;
const _pendingTitleUpdates=new Map([['B','Recovered title']]);
const _pendingTitleExpectedCurrent=new Map([['B','Old title']]);
const titleUpdates=[];
const applySessionTitleUpdate=(sid,title,options)=>titleUpdates.push([sid,title,options]);
const _carryForwardEphemeralTurnFields=(_, next)=>next;
const _filterRecoveryControlMessages=messages=>messages;
const _attachProjectedAnchorSceneToLastAssistant=()=>{};
// #6112 settle guard (PR #7836) runs inside the completion block; stub it like
// the other transcript helpers so this test stays about tab-state sync.
const _preSettle=[];
const _doneFinalAnswer='';
const _adoptDoneSnapshotMessages=(prev,next)=>({messages:next});
const _adoptRegenerationRevision=payload=>{S.session.regeneration_revision=payload.regeneration_revision;};
const _hydrateTodosFromSession=()=>{};
const clearVisibleMessageRowCache=()=>{};
const preserveVisibleOnShorterTerminalSnapshot=false;
const stored=new Map([['hermes-webui-session','A']]);
const urls=[];
const localStorage={setItem(key,value){
  if(STORAGE_BLOCKED) throw new Error('storage unavailable');
  stored.set(key,value);
}};
const _setActiveSessionUrl=URL_HELPER_AVAILABLE?(sid=>urls.push(sid)):undefined;
""" + block + """
assert.equal(S.session.session_id,'B');
assert.equal(_oldestIdx,900);
assert.equal(_messagesTruncated,true);
assert.equal(stored.get('hermes-webui-session'),STORAGE_BLOCKED?'A':'B');
assert.deepEqual(urls,URL_HELPER_AVAILABLE?['B']:[]);
if(pathName==='completion'){
  assert.equal(S.session.regeneration_revision,'rev-2');
  assert.deepEqual(titleUpdates,[['B','Recovered title',{expectedCurrent:'Old title'}]]);
  assert.equal(_pendingTitleUpdates.has('B'),false);
  assert.equal(_pendingTitleExpectedCurrent.has('B'),false);
}
"""
    script = script.replace("PATH_VALUE", json.dumps(path))
    script = script.replace("STORAGE_BLOCKED", json.dumps(storage_blocked))
    script = script.replace("URL_HELPER_AVAILABLE", json.dumps(url_helper_available))
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
