from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


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


@pytest.fixture(scope="module")
def settled_projection_snapshot() -> dict:
    assert NODE, "node is required for assistant-turn reducer tests"
    script = f"""
const fs=require('fs');
const vm=require('vm');
const src=fs.readFileSync({json.dumps(str(ANCHORS_JS))},'utf8');
const sandbox={{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src,sandbox,{{filename:'assistant_turn_anchors.js'}});
const api=sandbox.window.HermesAssistantTurnAnchors;
const errorStates=['incomplete_final','no_response','error','connection_lost','degraded'];
const preserved={{}};
for(const state of errorStates){{
  const registry=api.createAssistantTurnAnchorRegistry({{
    session_id:'sid-settled-'+state,
    turn_id:'turn-settled-'+state,
  }});
  const result=api.applyAssistantTurnAnchorSourceEvent(registry,{{
    source_type:'settled_message',
    payload:{{
      role:'assistant',
      id:'message-'+state,
      content:'persisted partial answer for '+state,
      _terminal_state:state,
      _error:true,
    }},
  }});
  preserved[state]={{
    applied:result.applied,
    status:registry.anchor.lifecycle.status,
    terminal_state:registry.anchor.lifecycle.terminal_state,
    final_answer:registry.anchor.content.final_answer,
  }};
}}

const successful=api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-settled-success',
  turn_id:'turn-settled-success',
}});
api.applyAssistantTurnAnchorSourceEvent(successful,{{
  source_type:'settled_message',
  payload:{{role:'assistant',id:'message-success',content:'durable successful final'}},
}});

const priorError=api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-prior-error',
  turn_id:'turn-prior-error',
  run_id:'run-prior-error',
}});
api.applyAssistantTurnAnchorSourceEvents(priorError,[
  {{
    event:'apperror',
    event_id:'run-prior-error:1',
    payload:{{type:'error',terminal_state:'error'}},
  }},
  {{
    source_type:'settled_message',
    payload:{{
      role:'assistant',
      id:'message-prior-error',
      content:'readable partial work before failure',
      _terminal_state:'error',
      _error:true,
    }},
  }},
]);

const reloadProjection=api.projectAssistantTurnAnchorSettledMessageFinalAnswer({{
  role:'assistant',
  id:'message-reload',
  content:'reloaded partial final',
  _terminal_state:'no_response',
  _error:true,
}},{{session_id:'sid-reload',raw_idx:4}});

const hydration=api.createAssistantTurnAnchorShadowSnapshot({{
  anchor:{{session_id:'sid-hydration',turn_id:'turn-hydration'}},
  sources:{{
    settled_events:[{{
      source_type:'settled_message',
      payload:{{
        role:'assistant',
        id:'message-hydration',
        content:'hydrated partial final',
        _terminal_state:'connection_lost',
        _error:true,
      }},
    }}],
  }},
}});

console.log(JSON.stringify({{
  preserved,
  successful:{{
    status:successful.anchor.lifecycle.status,
    terminal_state:successful.anchor.lifecycle.terminal_state,
    final_answer:successful.anchor.content.final_answer,
  }},
  prior_error:{{
    status:priorError.anchor.lifecycle.status,
    terminal_state:priorError.anchor.lifecycle.terminal_state,
    final_answer:priorError.anchor.content.final_answer,
  }},
  reload_projection:{{
    applied:reloadProjection.applied,
    status:reloadProjection.registry.anchor.lifecycle.status,
    terminal_state:reloadProjection.registry.anchor.lifecycle.terminal_state,
    final_answer:reloadProjection.final_answer,
  }},
  hydration:{{
    status:hydration.registry.anchor.lifecycle.status,
    terminal_state:hydration.registry.anchor.lifecycle.terminal_state,
    final_answer:hydration.registry.anchor.content.final_answer,
  }},
}}));
"""
    result = subprocess.run(
        [NODE, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "state",
    ["incomplete_final", "no_response", "error", "connection_lost", "degraded"],
)
def test_settled_error_terminal_state_survives_final_answer_projection(
    settled_projection_snapshot: dict, state: str
):
    projected = settled_projection_snapshot["preserved"][state]

    assert projected["applied"] is True
    assert projected["status"] == state
    assert projected["terminal_state"] == state
    assert projected["final_answer"] == f"persisted partial answer for {state}"


def test_genuinely_successful_settled_final_still_promotes_to_completed(
    settled_projection_snapshot: dict,
):
    projected = settled_projection_snapshot["successful"]

    assert projected["status"] == "completed"
    assert projected["terminal_state"] == "completed"
    assert projected["final_answer"] == "durable successful final"


def test_explicit_settled_error_does_not_promote_earlier_error_lifecycle(
    settled_projection_snapshot: dict,
):
    projected = settled_projection_snapshot["prior_error"]

    assert projected["status"] == "error"
    assert projected["terminal_state"] == "error"
    assert projected["final_answer"] == "readable partial work before failure"


def test_reload_and_shadow_hydration_preserve_settled_terminal_truth(
    settled_projection_snapshot: dict,
):
    reload_projection = settled_projection_snapshot["reload_projection"]
    hydration = settled_projection_snapshot["hydration"]

    assert reload_projection["applied"] is True
    assert reload_projection["status"] == "no_response"
    assert reload_projection["terminal_state"] == "no_response"
    assert reload_projection["final_answer"] == "reloaded partial final"
    assert hydration["status"] == "connection_lost"
    assert hydration["terminal_state"] == "connection_lost"
    assert hydration["final_answer"] == "hydrated partial final"


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
