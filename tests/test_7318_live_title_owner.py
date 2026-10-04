"""Execute the live title listener and delayed done rebind in Node."""
import json
from pathlib import Path
import subprocess

import pytest


def _build_script(delayed, title_events, malformed_b_title=None):
    source = (Path(__file__).parents[1] / 'static/messages.js').read_text()
    # Extract the pending-title declaration block (both maps), the listener,
    # applySessionTitleUpdate, and the done-rebind drain — all real source.
    decl_start = source.index('    // Stream-local titles survive the delayed done/fade rebind')
    ts = "    source.addEventListener('title_status',"
    end = source.index(ts, decl_start)
    decl_and_listener = source[decl_start:end]
    start = source.index('function applySessionTitleUpdate(')
    end = source.index('\n}\n', start) + 3
    apply = source[start:end]
    # Execute the actual pending-title drain from the done rebind, not a
    # duplicate implementation. It runs after S.session becomes done.session.
    marker = '          if(_pendingTitleUpdates.has(completedSid))'
    drain = ''
    if marker in source:
        start = source.index(marker)
        end = source.index('\n          }', start) + len('\n          }')
        drain = source[start:end]
    b_title = malformed_b_title or 'New Chat'
    script = '''
const assert=require('assert');
const activeSid='A';
const S={session:{session_id:'A',title:'New Chat'}};
const _allSessions=[{session_id:'B',title:''' + json.dumps(b_title) + '''}];
const _sessionTitleProvisionalBySid=new Map();
const _firstUserMessageTitleCandidate=()=>'';
const _sessionTitleLooksDefaultOrProvisional=t=>!t||t==='New Chat';
let onTitle;
const source={addEventListener:(_,fn)=>onTitle=fn};
''' + apply + decl_and_listener + '''
const completedSid='B';
function finishDone(){
 S.session={session_id:'B',title:''' + json.dumps(b_title) + '''};
''' + drain + '''
}
if(!DELAYED) finishDone();
for(const ev of TITLE_EVENTS) onTitle({data:JSON.stringify(ev)});
if(DELAYED) finishDone();
'''
    return script.replace('DELAYED', json.dumps(delayed)).replace('TITLE_EVENTS', json.dumps(title_events))


def _run_node(script):
    return subprocess.run(['node', '-e', script], capture_output=True, text=True)


@pytest.mark.parametrize('delayed', [False, True])
def test_rotated_title_reaches_continuation_after_done(delayed):
    # Stall watchdog pattern: assert the PASS line, not just returncode — a
    # node harness that drains without asserting also exits 0.
    script = _build_script(delayed, [
        {'session_id': 'A', 'target_session_id': 'B', 'title': 'Recovered title'},
        {'session_id': 'unrelated', 'target_session_id': 'B', 'title': 'Wrong'},
    ]) + '''
assert.equal(S.session.session_id,'B');
assert.equal(S.session.title,'Recovered title');
assert.equal(_allSessions[0].title,'Recovered title');
S.session.title='Manual title';
onTitle({data:JSON.stringify({session_id:'A',target_session_id:'B',title:'Overwrite'})});
assert.equal(S.session.title,'Manual title');
console.log('PASS: rotated_title_reaches_continuation');
'''
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert 'PASS: rotated_title_reaches_continuation' in result.stdout


@pytest.mark.parametrize('delayed', [False, True])
def test_malformed_persisted_title_recovery_with_expected_current(delayed):
    """Re-gate finding: on continuation B (after A->B compression) or after a
    reattach, nothing is remembered provisionally and B's persisted title is
    the malformed value — a bare listener-style apply is REFUSED, so the
    recovered title only appears after a full reload. The server now includes
    the title being replaced (`expectedCurrent`) in the title event, and the
    listener/drain forward it. The manual-rename guard must stay intact when
    the expected current title does not match."""
    malformed = 'Title options: 1. Fix login 2. OAuth flow 3. Debug'
    recovered = 'Debug OAuth login redirect'

    # 1. Malformed persisted title + matching expectedCurrent -> accepted.
    script = _build_script(delayed, [
        {'session_id': 'A', 'target_session_id': 'B', 'title': recovered,
         'expectedCurrent': malformed},
    ], malformed_b_title=malformed) + '''
assert.equal(S.session.title,''' + json.dumps(recovered) + ''');
assert.equal(_allSessions[0].title,''' + json.dumps(recovered) + ''');
console.log('PASS: malformed_title_recovered');
'''
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert 'PASS: malformed_title_recovered' in result.stdout

    # 2. Same malformed persisted title, expectedCurrent does NOT match (as if
    #    the user renamed meanwhile): the manual-rename guard must refuse.
    script = _build_script(delayed, [
        {'session_id': 'A', 'target_session_id': 'B', 'title': recovered,
         'expectedCurrent': 'A different current title'},
    ], malformed_b_title=malformed) + '''
assert.equal(S.session.title,''' + json.dumps(malformed) + ''');
console.log('PASS: rename_guard_intact');
'''
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert 'PASS: rename_guard_intact' in result.stdout


def test_mutation_bite_bare_call_refuses_malformed_title():
    """Prove the harness detects the original bug: a title event WITHOUT
    expectedCurrent (the pre-fix server shape) leaves the malformed persisted
    title in place."""
    malformed = 'Title options: 1. Fix login 2. OAuth flow 3. Debug'
    recovered = 'Debug OAuth login redirect'
    script = _build_script(True, [
        {'session_id': 'A', 'target_session_id': 'B', 'title': recovered},
    ], malformed_b_title=malformed) + '''
assert.equal(S.session.title,''' + json.dumps(malformed) + ''');
console.log('PASS: bare_call_refused');
'''
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert 'PASS: bare_call_refused' in result.stdout
