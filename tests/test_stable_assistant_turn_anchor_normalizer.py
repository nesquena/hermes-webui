"""Slice 2 normalizer tests for Stable Assistant Turn Anchors (#3926)."""
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


def _normalizer_snapshot() -> dict:
    assert NODE, "node is required for assistant_turn_anchors.js normalizer tests"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ANCHORS_JS))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;
const context = {{
  session_id:'sid-1',
  turn_id:'turn-1',
  run_id:'run-1',
  stream_id:'stream-1',
}};
const liveToken = api.normalizeAssistantTurnAnchorSourceEvent({{
  type:'token',
  data:JSON.stringify({{text:'hello', session_id:'sid-1'}}),
  lastEventId:'run-1:7',
}}, context);
const replayToken = api.normalizeAssistantTurnAnchorSourceEvent({{
  event:'token',
  payload:{{text:'hello', session_id:'sid-1'}},
  event_id:'run-1:7',
  seq:7,
}}, context);
const settled = api.normalizeAssistantTurnAnchorSourceEvent({{
  source_type:'settled_message',
  payload:{{
    role:'assistant',
    content:'final',
    reasoning:'trace',
    _turnUsage:{{input_tokens:3, output_tokens:5}},
  }},
}}, context);
const artifact = api.normalizeAssistantTurnAnchorSourceEvent({{
  source_type:'artifact_reference',
  payload:{{path:'result.txt', kind:'workspace_file'}},
  event_id:'run-1:8',
}}, context);
const sideEffect = api.normalizeAssistantTurnAnchorSourceEvent({{
  type:'state_saved',
  payload:{{kind:'memory', name:'saved-state'}},
  event_id:'run-1:9',
}}, context);
const transport = api.normalizeAssistantTurnAnchorSourceEvent({{
  type:'stream_end',
  data:'{{"session_id":"sid-1"}}',
  event_id:'run-1:10',
}}, context);
const unknown = api.normalizeAssistantTurnAnchorSourceEvent({{
  type:'unknown_future_event',
  payload:{{text:'ignored'}},
}}, context);
const deduped = api.normalizeAssistantTurnAnchorSourceEvents([
  {{type:'token', data:'{{"text":"live"}}', lastEventId:'run-1:11'}},
  {{event:'token', payload:{{text:'replay'}}, event_id:'run-1:11', seq:11}},
  {{event:'reasoning', payload:{{text:'thinking'}}, event_id:'run-1:12', seq:12}},
], context);
const invalidPayload = api.normalizeAssistantTurnAnchorSourceEvent({{
  type:'token',
  data:'plain text chunk',
  event_id:'run-1:13',
}}, context);
const directParsedPayload = api.normalizeAssistantTurnAnchorSourceEvent({{
  source_type:'token',
  text:'direct parsed payload',
  event_id:'run-1:14',
  seq:14,
}}, context);
console.log(JSON.stringify({{
  version: api.version,
  liveToken,
  replayToken,
  settled,
  artifact,
  sideEffect,
  transport,
  unknown,
  deduped,
  invalidPayload,
  directParsedPayload,
}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_normalizer_maps_live_and_replay_to_same_anchor_event_identity():
    data = _normalizer_snapshot()
    live = data["liveToken"]
    replay = data["replayToken"]

    assert data["version"] == "slice2-normalizer"
    assert live["classification"] == "activity"
    assert live["dedupe_key"] == "event_id:run-1:7"
    assert replay["dedupe_key"] == live["dedupe_key"]
    assert live["anchor_event"]["kind"] == "process_prose"
    assert live["anchor_event"]["source_event_type"] == "token"
    assert live["anchor_event"]["session_id"] == "sid-1"
    assert live["anchor_event"]["turn_id"] == "turn-1"
    assert live["anchor_event"]["run_id"] == "run-1"
    assert live["anchor_event"]["stream_id"] == "stream-1"
    assert live["anchor_event"]["seq"] == 7
    assert live["anchor_event"]["payload"] == {"session_id": "sid-1", "text": "hello"}


def test_normalizer_keeps_artifact_side_effect_metadata_and_transport_distinct():
    data = _normalizer_snapshot()

    settled = data["settled"]
    assert settled["classification"] == "metadata"
    assert settled["anchor_event"]["kind"] is None
    assert settled["anchor_event"]["source_event_type"] == "settled_message"
    assert settled["anchor_event"]["payload"]["role"] == "assistant"
    assert settled["anchor_event"]["payload"]["_turnUsage"] == {"input_tokens": 3, "output_tokens": 5}

    artifact = data["artifact"]
    assert artifact["classification"] == "artifact"
    assert artifact["anchor_event"]["kind"] == "artifact_reference"
    assert artifact["anchor_event"]["payload"] == {"kind": "workspace_file", "path": "result.txt"}

    side_effect = data["sideEffect"]
    assert side_effect["classification"] == "side_effect"
    assert side_effect["anchor_event"]["kind"] is None
    assert side_effect["anchor_event"]["payload"] == {"kind": "memory", "name": "saved-state"}

    transport = data["transport"]
    assert transport["classification"] == "transport"
    assert transport["anchor_event"]["kind"] is None
    assert transport["anchor_event"]["status"] == "transport_closed"


def test_normalizer_excludes_unknown_sources_and_sanitizes_non_json_data():
    data = _normalizer_snapshot()

    assert data["unknown"] == {
        "classification": "excluded",
        "source_event_type": "unknown_future_event",
        "anchor_event": None,
        "dedupe_key": "",
    }
    assert data["invalidPayload"]["anchor_event"]["payload"] == {"text": "plain text chunk"}
    assert data["invalidPayload"]["dedupe_key"] == "event_id:run-1:13"
    assert data["directParsedPayload"]["anchor_event"]["payload"] == {"text": "direct parsed payload"}
    assert data["directParsedPayload"]["anchor_event"]["seq"] == 14


def test_batch_normalizer_dedupes_live_plus_replay_by_event_envelope():
    data = _normalizer_snapshot()
    deduped = data["deduped"]

    assert [item["dedupe_key"] for item in deduped] == [
        "event_id:run-1:11",
        "event_id:run-1:12",
    ]
    assert [item["anchor_event"]["kind"] for item in deduped] == [
        "process_prose",
        "reasoning",
    ]
    assert deduped[0]["anchor_event"]["payload"] == {"text": "live"}


def test_slice2_normalizer_is_still_unwired_from_rendering_hot_paths():
    helper_names = [
        "normalizeAssistantTurnAnchorSourceEvent",
        "normalizeAssistantTurnAnchorSourceEvents",
    ]
    for helper in helper_names:
        assert helper not in _read(UI_JS)
        assert helper not in _read(SESSIONS_JS)
        assert helper not in _read(MESSAGES_JS)
