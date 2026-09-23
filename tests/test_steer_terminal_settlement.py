"""All terminal worker exits must close Steer and preserve accepted guidance."""
import pytest
from api import config
from tests import test_steer_worker_boundaries as boundaries

worker_scene = boundaries.worker_scene


@pytest.mark.parametrize('outcome', ['success', 'returned-error', 'exception'])
def test_terminal_exit_fences_before_event_and_keeps_guidance(worker_scene, monkeypatch, outcome):
    scene = worker_scene
    observed = []
    put = scene.events.put_nowait

    def observe(item):
        if item[0] in ('done', 'apperror'):
            observed.append(config.ACTIVE_RUNS.get('run', {}).get('phase'))
        return put(item)

    monkeypatch.setattr(scene.events, 'put_nowait', observe)

    def running():
        scene.agent.session_id = 'compressed-child'
        scene.agent.steer('retained guidance')
        if outcome == 'exception':
            raise RuntimeError('terminal worker failure')
        if outcome == 'returned-error':
            scene.result = {'messages': [], 'error': 'terminal worker failure'}
        else:
            # The real Agent consumes this slot into its returned result.
            text = scene.agent._drain_pending_steer()
            scene.result = {'messages': [
                {'role': 'user', 'content': 'Do the task.'},
                {'role': 'assistant', 'content': 'Finished.'},
            ], 'pending_steer': text}

    scene.on_run = running
    scene.run()
    assert observed and all(phase == 'finalizing' for phase in observed)
    events = list(scene.events.queue)
    leftovers = [data['text'] for event, data in events if event == 'pending_steer_leftover']
    assert leftovers == ['retained guidance']
    assert next(i for i, item in enumerate(events) if item[0] == 'pending_steer_leftover') < next(i for i, item in enumerate(events) if item[0] in ('done', 'apperror'))
    assert scene.agent.pending == []


def test_finalizing_response_preserves_browser_live_stream():
    import subprocess
    from pathlib import Path
    source = (Path(__file__).parents[1] / 'static/commands.js').read_text()
    fn = source[source.index('async function _trySteer('):source.index('async function cmdTitle(')]
    predicate = source[source.index('function _steerFallbackIsDeadRun('):source.index('function _steerOwnerStreamIsCurrent(')]
    script = r'''
const assert=require('node:assert/strict');
const S={session:{session_id:'s',active_stream_id:'run'},activeStreamId:'run',busy:true,pendingFiles:[]};
const INFLIGHT={s:{stream_id:'run'}},inp={value:''};
const $=()=>inp,t=x=>x,showToast=()=>{};
const _steerTextWithPendingFiles=async x=>x,_steerOwnerIsCurrent=()=>true,_steerRestoreText=x=>x;
const _steerFailureMessageKey=x=>x,_showSteerRecovery=()=>{};
const _steerClearCurrentOwnerDeadRun=()=>{S.busy=false;S.activeStreamId=null;delete INFLIGHT.s};
const api=async()=>({accepted:false,fallback:'not_running',stream_id:'run'});
(async()=>{
assert.equal(await _trySteer('guidance',true),false);
assert.equal(S.busy,true);assert.equal(S.activeStreamId,'run');
assert.equal(INFLIGHT.s.stream_id,'run');assert.equal(inp.value,'guidance');
})().catch(e=>{console.error(e);process.exit(1)});
'''
    subprocess.run(['node','-e',fn+predicate+script],check=True,timeout=15)
