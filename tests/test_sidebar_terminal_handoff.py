"""Sidebar metadata must not retire a still-open, exact chat transport.

The live SSE and /api/sessions responses are independently scheduled. An idle
sidebar row can arrive before the terminal frame carrying the Anchor scene.
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.test_issue2454_active_session_spinner import _function_body

ROOT = Path(__file__).resolve().parents[1]


def _run_case(operation, transport, *, absent=False, close_after=False):
    source = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
    names = ["_isServerIdleSessionRow", "_reconcileActiveSessionIdleStateFromList", "_purgeStaleInflightEntries", "_dropStaleOptimisticSessionRow"]
    if "function _hasOwnedOpenLiveStream(" in source:
        names.append("_hasOwnedOpenLiveStream")
    parameters = {
        "_isServerIdleSessionRow": "s",
        "_reconcileActiveSessionIdleStateFromList": "serverRows",
        "_hasOwnedOpenLiveStream": "sid",
        "_dropStaleOptimisticSessionRow": "sid",
    }
    functions = "\n".join(
        f"function {name}({parameters.get(name, '')}) {{"
        + _function_body(source, f"function {name}(") + "}"
        for name in names
    )
    script = """
const S={session:{session_id:'current',active_stream_id:'turn-1'},busy:true,activeStreamId:'turn-1'};
const INFLIGHT={current:{streamId:'turn-1',messages:[{role:'assistant',content:'work so far'}]}};
const original=INFLIGHT.current;
const LIVE_STREAMS={current:TRANSPORT};
const _sessionStreamingById=new Map();
const calls=[];
const idle={session_id:'current',is_streaming:false,active_stream_id:null,pending_user_message:null};
const _allSessions=ABSENT?[]:[idle];
function clearInflightState(){calls.push('clear');}
function _forgetObservedStreamingSession(){}
function hideApprovalCard(){}
function hideLiveRunStatus(){}
function clearLiveToolCards(){calls.push('clear-dom');}
function updateSendBtn(){}
function _scheduleActiveSessionIdleReload(){calls.push('reload');}
FUNCTIONS
function run(){if(OPERATION==='idle')_reconcileActiveSessionIdleStateFromList([idle]);else if(OPERATION==='optimistic')_dropStaleOptimisticSessionRow('current');else _purgeStaleInflightEntries();}
run();
const beforeClose={busy:S.busy,stream:S.activeStreamId,retained:INFLIGHT.current===original,calls:[...calls]};
if(CLOSE_AFTER){LIVE_STREAMS.current.source.readyState=2;run();}
console.log(JSON.stringify({beforeClose,afterClose:{retained:!!INFLIGHT.current,busy:S.busy}}));
"""
    script = script.replace("TRANSPORT", json.dumps(transport)).replace("ABSENT", json.dumps(absent))
    script = script.replace("FUNCTIONS", functions).replace("OPERATION", json.dumps(operation))
    script = script.replace("CLOSE_AFTER", json.dumps(close_after))
    node = shutil.which("node")
    if not node:
        pytest.skip("node required")
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("operation", ["idle", "purge", "optimistic"])
def test_current_open_transport_keeps_terminal_handoff_and_recovers_after_close(operation):
    result = _run_case(operation, {"streamId": "turn-1", "source": {"readyState": 1}}, close_after=True)
    assert result["beforeClose"] == {"busy": True, "stream": "turn-1", "retained": True, "calls": []}
    assert result["afterClose"]["retained"] is False
    if operation == "idle":
        assert result["afterClose"]["busy"] is False


@pytest.mark.parametrize("operation", ["idle", "purge", "optimistic"])
@pytest.mark.parametrize("transport", [
    None,
    {"streamId": "turn-1", "source": None},
    {"streamId": "turn-1", "source": {"readyState": 0}},
    {"streamId": "turn-1", "source": {"readyState": 2}},
    {"streamId": "older-turn", "source": {"readyState": 1}},
])
def test_missing_closed_connecting_or_wrong_transport_does_not_block_idle_recovery(operation, transport):
    result = _run_case(operation, transport)
    assert result["beforeClose"]["retained"] is False
    if operation == "idle":
        assert result["beforeClose"]["busy"] is False
        assert "reload" in result["beforeClose"]["calls"]


def test_current_open_transport_survives_a_filtered_sidebar_list():
    result = _run_case("purge", {"streamId": "turn-1", "source": {"readyState": 1}}, absent=True)
    assert result["beforeClose"]["retained"] is True
