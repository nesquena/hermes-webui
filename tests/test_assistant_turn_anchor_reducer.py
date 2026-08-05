from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
ANCHORS_JS = REPO / "static" / "assistant_turn_anchors.js"
NODE = shutil.which("node")


def _terminal_snapshot(events: list[dict]) -> dict:
    assert NODE, "node is required for assistant-turn reducer tests"
    script = f"""
const fs=require('fs');
const vm=require('vm');
const src=fs.readFileSync({json.dumps(str(ANCHORS_JS))},'utf8');
const sandbox={{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src,sandbox,{{filename:'assistant_turn_anchors.js'}});
const api=sandbox.window.HermesAssistantTurnAnchors;
const registry=api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-terminal',run_id:'run-terminal',stream_id:'run-terminal'
}});
api.applyAssistantTurnAnchorSourceEvents(registry,{json.dumps(events)});
console.log(JSON.stringify({{
  terminal_state:registry.anchor.lifecycle.terminal_state,
  status:registry.anchor.lifecycle.status,
  final_answer:registry.anchor.content.final_answer,
  transport_events:registry.anchor.transport_events.length,
}}));
"""
    result = subprocess.run(
        [NODE, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_first_semantic_terminal_cannot_be_reversed_by_conflicting_late_event():
    failure_then_done = _terminal_snapshot(
        [
            {
                "event": "apperror",
                "payload": {
                    "type": "incomplete_final",
                    "terminal_state": "incomplete_final",
                },
                "event_id": "run-terminal:1",
            },
            {
                "event": "done",
                "payload": {"terminal_state": "completed"},
                "event_id": "run-terminal:2",
            },
        ]
    )
    done_then_failure = _terminal_snapshot(
        [
            {
                "event": "done",
                "payload": {"terminal_state": "completed"},
                "event_id": "run-terminal:1",
            },
            {
                "event": "apperror",
                "payload": {"type": "error", "terminal_state": "error"},
                "event_id": "run-terminal:2",
            },
        ]
    )

    assert failure_then_done["terminal_state"] == "incomplete_final"
    assert done_then_failure["terminal_state"] == "completed"


def test_stream_end_is_transport_only():
    snapshot = _terminal_snapshot(
        [
            {
                "event": "stream_end",
                "payload": {},
                "event_id": "run-terminal:1",
            }
        ]
    )
    assert snapshot["terminal_state"] is None
    assert snapshot["transport_events"] == 1


def test_committed_settled_final_converges_over_incomplete_live_terminal():
    incomplete = {
        "event": "apperror",
        "payload": {
            "type": "incomplete_final",
            "terminal_state": "incomplete_final",
        },
        "event_id": "run-terminal:1",
    }
    settled = {
        "source_type": "settled_message",
        "payload": {
            "role": "assistant",
            "id": "final-message",
            "content": "durably saved final",
        },
    }

    replay_first = _terminal_snapshot([incomplete, settled])
    settled_first = _terminal_snapshot([settled, incomplete])
    assert replay_first == settled_first
    assert replay_first["terminal_state"] == "completed"
    assert replay_first["final_answer"] == "durably saved final"
