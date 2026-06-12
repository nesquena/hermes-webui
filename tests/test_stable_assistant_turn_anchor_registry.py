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


def _shadow_snapshot() -> dict:
    assert NODE, "node is required for assistant_turn_anchors.js registry tests"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ANCHORS_JS))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;
const shadow = api.createAssistantTurnAnchorShadowSnapshot({{
  anchor:{{
    session_id:'sid-shadow',
    turn_id:'turn-shadow',
  }},
  context:{{
    run_id:'run-shadow',
    stream_id:'stream-shadow',
  }},
  sources:{{
    live_events:[
      {{type:'token', data:'{{"text":"live token"}}', lastEventId:'run-shadow:1', created_at:'2026-06-11T00:00:01Z'}},
    ],
    replay_events:[
      {{event:'token', payload:{{text:'replay duplicate'}}, event_id:'run-shadow:1', seq:1}},
      {{event:'tool_complete', payload:{{tool_call_id:'tool-1', result:'ok'}}, event_id:'run-shadow:2', seq:2}},
    ],
    settled_events:[
      {{source_type:'settled_message', payload:{{role:'assistant', id:'message-shadow', content:'shadow final'}}}},
    ],
    inflight_events:[
      {{source_type:'inflight_snapshot', payload:{{status:'restoring'}}}},
    ],
  }},
}});
console.log(JSON.stringify({{
  version:api.version,
  registry:shadow.registry,
  results:Object.fromEntries(Object.entries(shadow.results).map(([key, value]) => [
    key,
    value.map((item)=>({{applied:item.applied, reason:item.reason}})),
  ])),
}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _hardening_snapshot() -> dict:
    assert NODE, "node is required for assistant_turn_anchors.js registry tests"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ANCHORS_JS))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;

const toolRegistry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-tool',
  turn_id:'turn-tool',
}});
const toolResults = api.applyAssistantTurnAnchorSourceEvents(toolRegistry, [
  {{event:'tool', payload:{{tool_call_id:'call-1', name:'shell'}}}},
  {{event:'tool_update', payload:{{tool_call_id:'call-1', text:'running'}}}},
  {{event:'tool_complete', payload:{{tool_call_id:'call-1', result:'ok'}}}},
  {{event:'token', payload:{{text:'first token'}}}},
  {{event:'token', payload:{{text:'second token'}}}},
]);

const runRegistry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-run',
  turn_id:'turn-run',
  run_id:'run-a',
  stream_id:'stream-a',
}});
const runMismatch = api.applyAssistantTurnAnchorSourceEvent(runRegistry, {{
  event:'token',
  payload:{{text:'wrong run'}},
  event_id:'run-b:1',
  run_id:'run-b',
  seq:1,
}});
const streamAccepted = api.applyAssistantTurnAnchorSourceEvent(runRegistry, {{
  event:'reasoning',
  payload:{{text:'same run new stream'}},
  event_id:'run-a:2',
  run_id:'run-a',
  stream_id:'stream-b',
  seq:2,
}});

const identityRegistry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-freeze',
  turn_id:'turn-freeze',
}});
identityRegistry.identity.session_id = 'mutated';

const metadataRegistry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-meta',
  turn_id:'turn-meta',
}});
const inheritedPayload = Object.create({{
  role:'assistant',
  content:'inherited final should be ignored',
  id:'inherited-message',
  _turnUsage:{{input_tokens:99, output_tokens:99}},
}});
api.applyAssistantTurnAnchorNormalizedEvent(metadataRegistry, {{
  classification:'metadata',
  anchor_event:{{
    session_id:'sid-meta',
    turn_id:'turn-meta',
    source_event_type:'settled_message',
    local_id:'meta-1',
    payload:inheritedPayload,
  }},
}});
api.applyAssistantTurnAnchorNormalizedEvent(metadataRegistry, {{
  classification:'metadata',
  anchor_event:{{
    session_id:'sid-meta',
    turn_id:'turn-meta',
    source_event_type:'settled_message',
    local_id:'meta-2',
    payload:{{
      role:'assistant',
      id:'message-structured',
      content:[
        {{type:'text', text:'structured '}},
        {{type:'text', text:'answer'}},
      ],
      usage:{{input_tokens:1, output_tokens:1}},
      _turnUsage:{{input_tokens:8, output_tokens:13}},
    }},
  }},
}});

console.log(JSON.stringify({{
  version:api.version,
  toolRegistry,
  toolResults:toolResults.map((item)=>({{applied:item.applied, reason:item.reason}})),
  runRegistry,
  runMismatch:{{applied:runMismatch.applied, reason:runMismatch.reason}},
  streamAccepted:{{applied:streamAccepted.applied, reason:streamAccepted.reason}},
  identityRegistry,
  metadataRegistry,
}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _race_snapshot() -> dict:
    assert NODE, "node is required for assistant_turn_anchors.js registry tests"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ANCHORS_JS))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;

function build(order) {{
  const registry = api.createAssistantTurnAnchorRegistry({{
    session_id:'sid-race',
    turn_id:'turn-race',
    run_id:'run-race',
    stream_id:'stream-race',
  }});
  const live = [
    {{event:'token', payload:{{text:'live token'}}, event_id:'run-race:1', run_id:'run-race', seq:1}},
  ];
  const replay = [
    {{event:'token', payload:{{text:'replayed duplicate'}}, event_id:'run-race:1', run_id:'run-race', seq:1}},
    {{event:'tool_complete', payload:{{tool_call_id:'tool-1', result:'ok'}}, event_id:'run-race:2', run_id:'run-race', seq:2}},
    {{event:'done', payload:{{status:'done'}}, event_id:'run-race:3', run_id:'run-race', seq:3, created_at:'2026-06-11T00:00:03Z'}},
  ];
  const settled = [
    {{source_type:'settled_message', payload:{{role:'assistant', id:'message-race', content:'race final', _turnUsage:{{input_tokens:5, output_tokens:8}}}}}},
  ];
  api.applyAssistantTurnAnchorSourceEvents(registry, live);
  if (order === 'replay-first') {{
    api.applyAssistantTurnAnchorSourceEvents(registry, replay);
    api.applyAssistantTurnAnchorSourceEvents(registry, settled);
  }} else {{
    api.applyAssistantTurnAnchorSourceEvents(registry, settled);
    api.applyAssistantTurnAnchorSourceEvents(registry, replay);
  }}
  const anchor = registry.anchor;
  return {{
    stats: registry.stats,
    dedupe_keys: registry.event_index.dedupe_keys,
    activity: anchor.activity_events.map((event) => ({{
      event_id: event.event_id,
      kind: event.kind,
      status: event.status,
      text: event.payload && event.payload.text || null,
      tool_call_id: event.payload && event.payload.tool_call_id || null,
    }})),
    terminal_state: anchor.lifecycle.terminal_state,
    final_answer: anchor.content.final_answer,
    final_message_ref: anchor.content.final_message_ref,
    usage: anchor.usage,
  }};
}}

console.log(JSON.stringify({{
  replayFirst: build('replay-first'),
  settledFirst: build('settled-first'),
}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_registry_owns_one_anchor_and_dedupes_live_plus_replay_events():
    data = _registry_snapshot()
    registry = data["registry"]
    anchor = registry["anchor"]

    assert data["version"] == "slice3-registry-shadow"
    assert [item["reason"] for item in data["results"][:2]] == [None, "duplicate"]
    assert registry["event_index"]["dedupe_keys"][:2] == [
        'event_id:"run-1:1"',
        'event_id:"run-1:2"',
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


def test_registry_replay_and_settlement_order_converge_on_same_anchor_state():
    data = _race_snapshot()
    replay_first = data["replayFirst"]
    settled_first = data["settledFirst"]

    assert replay_first == settled_first
    assert replay_first["stats"]["applied"] == 4
    assert replay_first["stats"]["skipped_duplicate"] == 1
    assert replay_first["dedupe_keys"] == [
        'event_id:"run-race:1"',
        'event_id:"run-race:2"',
        'event_id:"run-race:3"',
    ]
    assert replay_first["activity"] == [
        {
            "event_id": "run-race:1",
            "kind": "process_prose",
            "status": None,
            "text": "live token",
            "tool_call_id": None,
        },
        {
            "event_id": "run-race:2",
            "kind": "tool_completed",
            "status": "completed",
            "text": None,
            "tool_call_id": "tool-1",
        },
        {
            "event_id": "run-race:3",
            "kind": "terminal_status",
            "status": "completed",
            "text": None,
            "tool_call_id": None,
        },
    ]
    assert replay_first["terminal_state"] == "completed"
    assert replay_first["final_answer"] == "race final"
    assert replay_first["final_message_ref"] == "message-race"
    assert replay_first["usage"] == {"input_tokens": 5, "output_tokens": 8}


def test_registry_does_not_destructively_dedupe_seqless_local_tool_lifecycle():
    data = _hardening_snapshot()
    registry = data["toolRegistry"]
    anchor = registry["anchor"]

    assert data["version"] == "slice3-registry-shadow"
    assert data["toolResults"] == [
        {"applied": True, "reason": None},
        {"applied": True, "reason": None},
        {"applied": True, "reason": None},
        {"applied": True, "reason": None},
        {"applied": True, "reason": None},
    ]
    assert registry["event_index"]["dedupe_keys"] == []
    assert registry["stats"]["applied"] == 5
    assert [event["kind"] for event in anchor["activity_events"]] == [
        "tool_started",
        "tool_updated",
        "tool_completed",
        "process_prose",
        "process_prose",
    ]


def test_registry_rejects_cross_run_events_but_allows_stream_reconnects():
    data = _hardening_snapshot()
    registry = data["runRegistry"]

    assert data["runMismatch"] == {"applied": False, "reason": "mismatched_anchor"}
    assert data["streamAccepted"] == {"applied": True, "reason": None}
    assert registry["stats"]["skipped_mismatched"] == 1
    assert registry["stats"]["applied"] == 1
    assert registry["anchor"]["identity"]["run_id"] == "run-a"
    assert registry["anchor"]["identity"]["stream_id"] == "stream-a"
    assert registry["anchor"]["activity_events"][0]["stream_id"] == "stream-b"


def test_registry_identity_copy_and_metadata_reads_are_hardened():
    data = _hardening_snapshot()
    identity_registry = data["identityRegistry"]
    metadata_anchor = data["metadataRegistry"]["anchor"]

    assert identity_registry["identity"]["session_id"] == "sid-freeze"
    assert identity_registry["anchor"]["identity"]["session_id"] == "sid-freeze"
    assert metadata_anchor["content"]["final_answer"] == "structured answer"
    assert metadata_anchor["content"]["final_message_ref"] == "message-structured"
    assert metadata_anchor["usage"] == {"input_tokens": 8, "output_tokens": 13}
    assert len(metadata_anchor["metadata_events"]) == 2


def test_shadow_snapshot_feeds_current_source_families_into_one_registry_owner():
    data = _shadow_snapshot()
    registry = data["registry"]
    anchor = registry["anchor"]

    assert data["version"] == "slice3-registry-shadow"
    assert data["results"]["live"] == [{"applied": True, "reason": None}]
    assert data["results"]["replay"] == [
        {"applied": False, "reason": "duplicate"},
        {"applied": True, "reason": None},
    ]
    assert data["results"]["settled"] == [{"applied": True, "reason": None}]
    assert data["results"]["inflight"] == [{"applied": True, "reason": None}]

    assert registry["stats"]["applied"] == 4
    assert registry["stats"]["skipped_duplicate"] == 1
    assert anchor["identity"]["run_id"] == "run-shadow"
    assert anchor["identity"]["stream_id"] == "stream-shadow"
    assert [event["kind"] for event in anchor["activity_events"]] == [
        "process_prose",
        "tool_completed",
    ]
    assert [event["source_event_type"] for event in anchor["metadata_events"]] == [
        "settled_message",
        "inflight_snapshot",
    ]
    assert anchor["content"]["final_answer"] == "shadow final"


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
        "createAssistantTurnAnchorShadowSnapshot",
    ]
    for helper in helper_names:
        assert helper not in _read(UI_JS)
        assert helper not in _read(SESSIONS_JS)
        assert helper not in _read(MESSAGES_JS)
