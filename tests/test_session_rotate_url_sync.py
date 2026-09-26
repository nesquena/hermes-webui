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
        start = MESSAGES_JS.index("S.session=d.session;")
        end = MESSAGES_JS.index("const _markerOnlyAssistantError=", start)
    else:
        start = MESSAGES_JS.index("S.session=session;")
        # Paging/revision bookkeeping now precedes the tab sync. Include the
        # storage and URL writes, stopping before transcript staging begins.
        end = MESSAGES_JS.index("const _stagedMessages=", start)
    block = MESSAGES_JS[start:end]
    script = """
const assert=require('node:assert/strict');
const S={session:{session_id:'A'},messages:[]};
const session={session_id:'B',messages:[{role:'assistant',content:'Done'}]};
const d={session};
const completedSid='B';
const _pendingTitleUpdates=new Map();
const _carryForwardEphemeralTurnFields=(_, next)=>next;
const _filterRecoveryControlMessages=messages=>messages;
const _attachProjectedAnchorSceneToLastAssistant=()=>{};
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
assert.equal(stored.get('hermes-webui-session'),STORAGE_BLOCKED?'A':'B');
assert.deepEqual(urls,URL_HELPER_AVAILABLE?['B']:[]);
"""
    script = script.replace("STORAGE_BLOCKED", json.dumps(storage_blocked))
    script = script.replace("URL_HELPER_AVAILABLE", json.dumps(url_helper_available))
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
