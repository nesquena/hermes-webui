"""Execute the live title listener and delayed done rebind in Node."""
import json
from pathlib import Path
import subprocess

import pytest


def _build_script(delayed, title_events, malformed_b_title=None, active_sid='A', run_done=True):
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
    open_title = b_title if active_sid == 'B' else 'New Chat'
    script = '''
const assert=require('assert');
const activeSid=''' + json.dumps(active_sid) + ''';
const S={session:{session_id:''' + json.dumps(active_sid) + ''',title:''' + json.dumps(open_title) + '''}};
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
if(RUN_DONE&&!DELAYED) finishDone();
for(const ev of TITLE_EVENTS) onTitle({data:JSON.stringify(ev)});
if(RUN_DONE&&DELAYED) finishDone();
'''
    return script.replace('DELAYED', json.dumps(delayed)).replace('TITLE_EVENTS', json.dumps(title_events)).replace('RUN_DONE', json.dumps(run_done))


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


@pytest.mark.parametrize('delayed', [False, True])
def test_reattached_continuation_listener_accepts_target_keyed_event(delayed):
    """Re-gate finding ([SILENT, Codex]): after A→B compression the server
    emits the title event with session_id: A, but the reattached listener runs
    with activeSid: B and returned before ever checking expectedCurrent — B's
    malformed title stayed in place (master updates B; this head did not).

    The server now keys the event on the title TARGET (B) and carries the
    stream OWNER (A) as stream_owner_session_id, and the listener accepts
    either id. The deterministic probe: a B-active listener with the new
    B-keyed event updates B; a genuinely foreign stream (no id match) is
    still rejected."""
    malformed = 'Title options: 1. Fix login 2. OAuth flow 3. Debug'
    recovered = 'Debug OAuth login redirect'

    # 1. New server shape: session_id=B (target), stream_owner_session_id=A.
    #    The reattached listener (activeSid=B) must accept it and update B.
    #    RED-BEFORE: the old listener hard-rejects any event whose session_id
    #    is not activeSid — a B-active listener dropped the A-keyed event the
    #    old server sent, and a B-keyed event never existed. This case exercises
    #    both halves: only the new listener accepts the target-keyed event, and
    #    the owner fallback keeps pre-rotation A-captured listeners working.
    script = _build_script(delayed, [
        {'session_id': 'B', 'stream_owner_session_id': 'A',
         'target_session_id': 'B', 'title': recovered,
         'expectedCurrent': malformed},
    ], malformed_b_title=malformed, active_sid='B') + '''
assert.equal(S.session.session_id,'B');
assert.equal(S.session.title,''' + json.dumps(recovered) + ''');
assert.equal(_allSessions[0].title,''' + json.dumps(recovered) + ''');
// The owner fallback must ALSO deliver A-keyed events to an A-active listener.
console.log('PASS: reattached_b_updated');
'''
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert 'PASS: reattached_b_updated' in result.stdout

    # 2. Owner fallback: an A-active mid-stream listener (the pre-rotation
    #    capture) must still accept the event via stream_owner_session_id —
    #    this is the case the old session_id-keyed guard served. finishDone()
    #    is NOT called here (mid-stream listener, still on A).
    script = _build_script(delayed, [
        {'session_id': 'B', 'stream_owner_session_id': 'A',
         'target_session_id': 'B', 'title': recovered,
         'expectedCurrent': 'New Chat'},
    ], active_sid='A', run_done=False) + '''
assert.equal(S.session.session_id,'A');
// The target is B: the sidebar row updates, the open session (A) does not.
assert.equal(_allSessions[0].title,''' + json.dumps(recovered) + ''');
console.log('PASS: owner_fallback_still_updates');
'''
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert 'PASS: owner_fallback_still_updates' in result.stdout

    # 2. Mutation bite: the OLD server shape (session_id=A only) must still be
    #    rejected by the B-active listener — proving the old shape was the bug
    #    and the new guard still rejects genuinely foreign streams.
    script = _build_script(delayed, [
        {'session_id': 'A', 'target_session_id': 'B', 'title': recovered,
         'expectedCurrent': malformed},
    ], malformed_b_title=malformed, active_sid='B') + '''
assert.equal(S.session.title,''' + json.dumps(malformed) + ''');
console.log('PASS: old_shape_rejected_by_reattached_listener');
'''
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert 'PASS: old_shape_rejected_by_reattached_listener' in result.stdout

    # 3. A genuinely foreign stream id updates nothing.
    script = _build_script(delayed, [
        {'session_id': 'unrelated', 'stream_owner_session_id': 'unrelated',
         'target_session_id': 'B', 'title': recovered,
         'expectedCurrent': malformed},
    ], malformed_b_title=malformed, active_sid='B') + '''
assert.equal(S.session.title,''' + json.dumps(malformed) + ''');
console.log('PASS: foreign_stream_rejected');
'''
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert 'PASS: foreign_stream_rejected' in result.stdout
