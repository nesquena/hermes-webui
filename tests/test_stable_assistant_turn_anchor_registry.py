"""Slice 3 registry tests for Stable Assistant Turn Anchors (#3926)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ANCHORS_JS = REPO / "static" / "assistant_turn_anchors.js"
MESSAGES_JS = REPO / "static" / "messages.js"
UI_JS = REPO / "static" / "ui.js"
SESSIONS_JS = REPO / "static" / "sessions.js"
NODE = shutil.which("node")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _registry_snapshot() -> dict:
    assert NODE, "node is required for assistant_turn_anchors.js registry tests"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ANCHORS_JS))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;
const registry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-1',
  turn_id:'turn-1',
}});
const results = api.applyAssistantTurnAnchorSourceEvents(registry, [
  {{type:'token', data:'{{"text":"live token"}}', lastEventId:'run-1:1', created_at:'2026-06-11T00:00:01Z'}},
  {{event:'token', payload:{{text:'replay token'}}, event_id:'run-1:1', seq:1}},
  {{event:'reasoning', payload:{{text:'thinking'}}, event_id:'run-1:2', seq:2}},
  {{event:'artifact_reference', payload:{{path:'answer.txt', kind:'workspace_file'}}, event_id:'run-1:3', seq:3}},
  {{event:'state_saved', payload:{{kind:'memory', name:'session-state'}}, event_id:'run-1:4', seq:4}},
  {{event:'stream_end', payload:{{}}, event_id:'run-1:5', seq:5}},
  {{event:'done', payload:{{}}, event_id:'run-1:6', seq:6, created_at:'2026-06-11T00:00:06Z'}},
  {{source_type:'settled_message', payload:{{role:'assistant', id:'message-final', content:'final answer', _turnUsage:{{input_tokens:8, output_tokens:13}}}}}},
], {{run_id:'run-1', stream_id:'stream-1'}});

const isolated = api.createAssistantTurnAnchorRegistry({{session_id:'sid-1', turn_id:'turn-2'}});
api.applyAssistantTurnAnchorSourceEvent(registry, {{
  event:'token',
  payload:{{text:'wrong session', session_id:'sid-2'}},
  event_id:'run-1:7',
  seq:7,
}}, {{run_id:'run-1'}});

console.log(JSON.stringify({{
  version:api.version,
  registry,
  isolated,
  results:results.map((item)=>({{applied:item.applied, reason:item.reason}})),
}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_registry_owns_one_anchor_and_dedupes_live_plus_replay_events():
    data = _registry_snapshot()
    registry = data["registry"]
    anchor = registry["anchor"]

    assert data["version"] == "slice3-registry"
    assert [item["reason"] for item in data["results"][:2]] == [None, "duplicate"]
    assert registry["event_index"]["dedupe_keys"][:2] == [
        "event_id:run-1:1",
        "event_id:run-1:2",
    ]
    assert registry["stats"]["applied"] == 7
    assert registry["stats"]["skipped_duplicate"] == 1
    assert registry["stats"]["skipped_mismatched"] == 1

    assert anchor["identity"]["session_id"] == "sid-1"
    assert anchor["identity"]["turn_id"] == "turn-1"
    assert anchor["identity"]["run_id"] == "run-1"
    assert anchor["identity"]["stream_id"] == "stream-1"
    assert [event["kind"] for event in anchor["activity_events"]] == [
        "process_prose",
        "reasoning",
        "terminal_status",
    ]
    assert anchor["activity_events"][0]["payload"] == {"text": "live token"}


def test_registry_routes_activity_artifacts_side_effects_metadata_and_transport():
    data = _registry_snapshot()
    anchor = data["registry"]["anchor"]

    assert len(anchor["artifacts"]) == 1
    assert anchor["artifacts"][0]["source_event_type"] == "artifact_reference"
    assert anchor["artifacts"][0]["payload"] == {
        "kind": "workspace_file",
        "path": "answer.txt",
    }
    assert len(anchor["side_effects"]) == 1
    assert anchor["side_effects"][0]["source_event_type"] == "state_saved"
    assert len(anchor["metadata_events"]) == 1
    assert anchor["metadata_events"][0]["source_event_type"] == "settled_message"
    assert len(anchor["transport_events"]) == 1
    assert anchor["transport_events"][0]["source_event_type"] == "stream_end"


def test_registry_updates_lifecycle_and_settled_final_projection():
    data = _registry_snapshot()
    anchor = data["registry"]["anchor"]

    assert anchor["lifecycle"]["status"] == "completed"
    assert anchor["lifecycle"]["terminal_state"] == "completed"
    assert anchor["lifecycle"]["started_at"] == "2026-06-11T00:00:01Z"
    assert anchor["lifecycle"]["completed_at"] == "2026-06-11T00:00:06Z"
    assert anchor["content"]["final_answer"] == "final answer"
    assert anchor["content"]["final_message_ref"] == "message-final"
    assert anchor["usage"] == {"input_tokens": 8, "output_tokens": 13}


def test_registry_instances_do_not_share_owner_state():
    data = _registry_snapshot()
    isolated = data["isolated"]

    assert isolated["identity"]["turn_id"] == "turn-2"
    assert isolated["event_index"]["dedupe_keys"] == []
    assert isolated["stats"]["applied"] == 0
    assert isolated["anchor"]["activity_events"] == []


def test_slice3_registry_is_still_unwired_from_rendering_hot_paths():
    helper_names = [
        "createAssistantTurnAnchorRegistry",
        "applyAssistantTurnAnchorNormalizedEvent",
        "applyAssistantTurnAnchorSourceEvent",
        "applyAssistantTurnAnchorSourceEvents",
    ]
    for helper in helper_names:
        assert helper not in _read(UI_JS)
        assert helper not in _read(SESSIONS_JS)
        assert helper not in _read(MESSAGES_JS)
