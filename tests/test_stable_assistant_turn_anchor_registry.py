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


def _event_listener_body(src: str, event_name: str) -> str:
    start = src.index(f"source.addEventListener('{event_name}'")
    end = src.find("\n    source.addEventListener(", start + 1)
    if end < 0:
        end = src.find("\n    source.onerror", start + 1)
    if end < 0:
        end = src.find("\n  }catch", start + 1)
    if end < 0:
        end = len(src)
    assert end > start
    return src[start:end]


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

def _activity_scene_snapshot() -> dict:
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
  session_id:'sid-scene',
  turn_id:'turn-scene',
}});
api.applyAssistantTurnAnchorSourceEvents(registry, [
  {{event:'token', payload:{{text:'progress'}}, event_id:'run-scene:1', seq:1}},
  {{event:'reasoning', payload:{{text:'private thinking'}}, event_id:'run-scene:2', seq:2}},
  {{event:'tool', payload:{{
    tool_call_id:'tool-1',
    name:'terminal',
    args:{{command:'rg anchor static'}},
    preview:'rg anchor static',
    activityBurstId:7,
    activitySegmentSeq:3,
    assistant_msg_idx:12,
    started_at:1781200000
  }}, event_id:'run-scene:3', seq:3}},
  {{event:'tool_update', payload:{{
    tool_call_id:'tool-1',
    name:'terminal',
    text:'running',
    preview:'searching workspace',
    activityBurstId:7,
    activitySegmentSeq:3,
    assistant_msg_idx:12
  }}, event_id:'run-scene:4', seq:4}},
  {{event:'tool_complete', payload:{{
    tool_call_id:'tool-1',
    name:'terminal',
    result:'done',
    output:'done',
    snippet:'done',
    is_error:false,
    duration:1.25,
    activityBurstId:7,
    activitySegmentSeq:3,
    assistant_msg_idx:12
  }}, event_id:'run-scene:5', seq:5}},
  {{event:'done', payload:{{}}, event_id:'run-scene:6', seq:6}},
  {{source_type:'settled_message', payload:{{role:'assistant', id:'message-scene', content:'final answer'}}}},
], {{run_id:'run-scene', stream_id:'stream-scene'}});
const compact = api.projectAssistantTurnAnchorActivityScene(registry, {{mode:'compact_worklog'}});
const transparent = api.projectAssistantTurnAnchorActivityScene(registry.anchor, {{mode:'transparent_stream'}});
const empty = api.projectAssistantTurnAnchorActivityScene(null, {{mode:'transparent_stream'}});
const seqlessRegistry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-seqless',
  turn_id:'turn-seqless',
}});
api.applyAssistantTurnAnchorSourceEvents(seqlessRegistry, [
  {{event:'tool', payload:{{tool_call_id:'tool-same', name:'terminal'}}}},
  {{event:'tool_update', payload:{{tool_call_id:'tool-same', text:'running'}}}},
  {{event:'tool_complete', payload:{{tool_call_id:'tool-same', result:'done'}}}},
]);
const seqless = api.projectAssistantTurnAnchorActivityScene(seqlessRegistry, {{mode:'compact_worklog'}});
const zeroRegistry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-zero',
  turn_id:'turn-zero',
}});
api.applyAssistantTurnAnchorSourceEvents(zeroRegistry, [
  {{event:'tool', payload:{{
    tool_call_id:'tool-zero',
    name:'terminal',
    activityBurstId:0,
    activitySegmentSeq:0,
    assistant_msg_idx:0
  }}, event_id:'run-zero:0', seq:0}},
]);
const zero = api.projectAssistantTurnAnchorActivityScene(zeroRegistry, {{mode:'compact_worklog'}});
console.log(JSON.stringify({{
  version:api.version,
  compact,
  transparent,
  empty,
  seqless,
  zero,
}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _final_projection_snapshot() -> dict:
    assert NODE, "node is required for assistant_turn_anchors.js registry tests"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ANCHORS_JS))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;
const projected = api.projectAssistantTurnAnchorSettledMessageFinalAnswer({{
  role:'assistant',
  id:'message-final',
  content:'raw content should be replaced by render-preserved content',
  _turnUsage:{{input_tokens:8, output_tokens:13}},
}}, {{
  session_id:'sid-project',
  raw_idx:7,
  content:'line one\\nline two',
}});
const projectedByRawIdx = api.projectAssistantTurnAnchorSettledMessageFinalAnswer({{
  role:'assistant',
  content:'message without id',
}}, {{
  session_id:'sid-project',
  raw_idx:11,
  content:'raw index final',
}});
const missingSession = api.projectAssistantTurnAnchorSettledMessageFinalAnswer({{
  role:'assistant',
  id:'message-missing-session',
  content:'final',
}}, {{}});
const nonAssistant = api.projectAssistantTurnAnchorSettledMessageFinalAnswer({{
  role:'user',
  id:'message-user',
  content:'user text',
}}, {{
  session_id:'sid-project',
}});
console.log(JSON.stringify({{
  version:api.version,
  projected,
  projectedByRawIdx,
  missingSession,
  nonAssistant,
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

    assert data["version"] == "slice5-activity-scene"
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

    assert data["version"] == "slice5-activity-scene"
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

    assert data["version"] == "slice5-activity-scene"
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

def test_activity_scene_projects_current_activity_events_for_both_render_modes():
    data = _activity_scene_snapshot()
    compact = data["compact"]
    transparent = data["transparent"]

    assert data["version"] == "slice5-activity-scene"
    assert compact["version"] == "activity_scene_v1"
    assert transparent["version"] == "activity_scene_v1"
    assert compact["mode"] == "compact_worklog"
    assert transparent["mode"] == "transparent_stream"
    assert compact["final_answer"] == "final answer"
    assert transparent["final_answer"] == "final answer"
    assert compact["terminal_state"] == "completed"
    assert transparent["terminal_state"] == "completed"

    compact_rows = compact["activity_rows"]
    transparent_rows = transparent["activity_rows"]
    assert [row["row_id"] for row in compact_rows] == [
        "run-scene:1",
        "run-scene:2",
        "run-scene:3",
        "run-scene:4",
        "run-scene:5",
        "run-scene:6",
    ]
    assert [row["row_id"] for row in transparent_rows] == [
        row["row_id"] for row in compact_rows
    ]
    assert [row["kind"] for row in compact_rows] == [
        "process_prose",
        "reasoning",
        "tool_started",
        "tool_updated",
        "tool_completed",
        "terminal_status",
    ]
    assert [row["role"] for row in compact_rows] == [
        "prose",
        "thinking",
        "tool",
        "tool",
        "tool",
        "terminal",
    ]
    assert [row["display_hint"] for row in compact_rows] == [
        "main_prose",
        "collapsed_thinking",
        "tool_row",
        "tool_row",
        "tool_row",
        "terminal_status_row",
    ]
    assert all(row["display_hint"] == "chronological_activity" for row in transparent_rows)
    assert compact_rows[0]["text"] == "progress"
    assert compact_rows[0]["tool_call_id"] is None
    assert compact_rows[2]["tool_call_id"] == "tool-1"
    assert compact_rows[4]["text"] == "done"
    assert compact_rows[1]["thinking"] == {
        "text": "private thinking",
        "preview": "private thinking",
        "dedupe_key": "thinking:private thinking",
    }
    assert compact_rows[2]["group"] == {
        "group_key": "segment:3",
        "activity_burst_id": 7,
        "activity_segment_seq": 3,
        "assistant_msg_idx": 12,
    }
    assert compact_rows[2]["tool"] == {
        "id": "tool-1",
        "name": "terminal",
        "args": {"command": "rg anchor static"},
        "preview": "rg anchor static",
        "snippet": "",
        "result": None,
        "output": None,
        "done": False,
        "is_error": False,
        "duration": None,
        "started_at": 1781200000,
        "signature": 'terminal|tool-1|{"command":"rg anchor static"}',
    }
    assert compact_rows[4]["tool"]["done"] is True
    assert compact_rows[4]["tool"]["is_error"] is False
    assert compact_rows[4]["tool"]["duration"] == 1.25
    assert compact_rows[4]["tool"]["snippet"] == "done"
    assert compact_rows[4]["display_hints"] == {
        "compact_worklog": "tool_row",
        "transparent_stream": "chronological_activity",
    }
    seqless_ids = [row["row_id"] for row in data["seqless"]["activity_rows"]]
    assert len(seqless_ids) == len(set(seqless_ids))
    assert seqless_ids == [
        "tool-same:tool:0",
        "tool-same:tool_update:1",
        "tool-same:tool_complete:2",
    ]
    assert data["zero"]["activity_rows"][0]["group"] == {
        "group_key": "segment:0",
        "activity_burst_id": 0,
        "activity_segment_seq": 0,
        "assistant_msg_idx": 0,
    }


def test_activity_scene_is_renderer_neutral_and_empty_safe():
    data = _activity_scene_snapshot()
    compact = data["compact"]
    empty = data["empty"]

    assert "final answer" not in [row["text"] for row in compact["activity_rows"]]
    assert compact["identity"]["session_id"] == "sid-scene"
    assert compact["identity"]["run_id"] == "run-scene"
    assert compact["identity"]["stream_id"] == "stream-scene"
    assert empty == {
        "version": "activity_scene_v1",
        "mode": "transparent_stream",
        "identity": {"source_message_refs": []},
        "lifecycle": {},
        "final_answer": "",
        "final_message_ref": None,
        "terminal_state": None,
        "activity_rows": [],
    }


def test_final_projection_routes_settled_assistant_message_through_anchor_owner():
    data = _final_projection_snapshot()
    projected = data["projected"]
    registry = projected["registry"]
    anchor = registry["anchor"]

    assert data["version"] == "slice5-activity-scene"
    assert projected["applied"] is True
    assert projected["reason"] is None
    assert projected["final_message_ref"] == "message-final"
    assert projected["final_answer"] == "line one\nline two"
    assert registry["stats"]["applied"] == 1
    assert anchor["identity"]["session_id"] == "sid-project"
    assert anchor["content"]["final_answer"] == "line one\nline two"
    assert anchor["content"]["final_message_ref"] == "message-final"
    assert anchor["usage"] == {"input_tokens": 8, "output_tokens": 13}
    assert [event["source_event_type"] for event in anchor["metadata_events"]] == [
        "settled_message",
    ]
    assert data["projectedByRawIdx"]["final_message_ref"] == "raw_idx:11"
    assert data["projectedByRawIdx"]["registry"]["anchor"]["identity"][
        "source_message_refs"
    ] == ["raw_idx:11"]
    anchor_src = _read(ANCHORS_JS)
    assert "if(!result.applied)" in anchor_src


def test_final_projection_is_scoped_to_settled_assistant_messages():
    data = _final_projection_snapshot()

    assert data["missingSession"] == {
        "applied": False,
        "reason": "missing_session",
        "final_answer": "",
        "final_message_ref": None,
        "registry": None,
    }
    assert data["nonAssistant"] == {
        "applied": False,
        "reason": "non_assistant",
        "final_answer": "",
        "final_message_ref": None,
        "registry": None,
    }


def test_render_messages_uses_anchor_projection_only_for_settled_final_prose():
    src = _read(UI_JS)
    start = src.index("function renderMessages")
    end = src.index("function _toolDisplayName", start)
    render_body = src[start:end]

    flatten_idx = render_body.index("content=content.filter(p=>p&&p.type==='text')")
    projection_idx = render_body.index("_assistantTurnAnchorSettledFinalAnswer(m, content")
    thinking_idx = render_body.index("_extractInlineThinkingFromContentForRender(content")

    assert flatten_idx < projection_idx < thinking_idx
    assert "if(m.role==='assistant'&&!m._live&&typeof content==='string'){" in render_body
    assert "createAssistantTurnAnchorRegistry" not in render_body
    assert "applyAssistantTurnAnchorSourceEvent" not in render_body
    assert "_assistantTurnAnchorSettledFinalAnswerWarned" in src
    assert "console.warn('assistant turn anchor settled-final projection failed',err)" in src


def test_registry_instances_do_not_share_owner_state():
    data = _registry_snapshot()
    isolated = data["isolated"]

    assert isolated["identity"]["turn_id"] == "turn-2"
    assert isolated["event_index"]["dedupe_keys"] == []
    assert isolated["stats"]["applied"] == 0
    assert isolated["anchor"]["activity_events"] == []

def test_slice5_scene_projection_does_not_wire_activity_scene_into_rendering_hot_paths():
    scene_helper = "projectAssistantTurnAnchorActivityScene"
    for helper in [
        "applyAssistantTurnAnchorNormalizedEvent",
        "applyAssistantTurnAnchorSourceEvents",
        "createAssistantTurnAnchorShadowSnapshot",
        scene_helper,
    ]:
        assert helper not in _read(UI_JS)
        assert helper not in _read(SESSIONS_JS)
        assert helper not in _read(MESSAGES_JS)
    assert "projectAssistantTurnAnchorSettledMessageFinalAnswer" in _read(UI_JS)
    assert scene_helper not in _read(UI_JS)
    assert scene_helper not in _read(SESSIONS_JS)
    assert scene_helper not in _read(MESSAGES_JS)


def test_slice6_live_shadow_feed_wires_non_token_events_without_renderer_scene_consumption():
    src = _read(MESSAGES_JS)
    helper_body = src.split("function _applyToAnchor", 1)[1].split(
        "function _mergeSettledToolCallsWithLiveMetadata", 1
    )[0]

    assert "window._liveAnchorRegistries=window._liveAnchorRegistries||new Map()" in src
    assert "_anchorRegistryMap.get(streamId)" in src
    assert "_anchorRegistryMap.set(streamId,_anchorRegistry)" in src
    assert "createAssistantTurnAnchorRegistry" in src
    assert "applyAssistantTurnAnchorSourceEvent" in src
    assert "const eventId=(sseEvent&&sseEvent.lastEventId)||raw.event_id||raw.lastEventId||raw.last_event_id||'';" in helper_body
    assert helper_body.index("...raw,") < helper_body.index("source_event_type:sourceEventType")

    for event_name in [
        "interim_assistant",
        "tool",
        "tool_complete",
        "approval",
        "clarify",
        "goal_continue",
        "pending_steer_leftover",
        "compressing",
        "compressed",
        "apperror",
        "cancel",
    ]:
        assert f"_applyToAnchor('{event_name}'" in _event_listener_body(src, event_name)

    token_body = _event_listener_body(src, "token")
    assert "_applyToAnchor" not in token_body
    reasoning_body = _event_listener_body(src, "reasoning")
    assert "_applyToAnchor" not in reasoning_body
    assert "function _flushReasoningToAnchor()" in src
    assert "_applyToAnchor('reasoning',{" in src
    assert "local_id:'live-reasoning'" in src
    error_body = _event_listener_body(src, "error")
    assert "_applyToAnchor('error'" not in error_body
    assert "_flushReasoningToAnchor();" in error_body
    assert "_scheduleAnchorRegistryCleanup(120000);" in error_body
    assert "_handleStreamError(source)" in error_body
    assert "projectAssistantTurnAnchorActivityScene" not in src

    tool_body = _event_listener_body(src, "tool")
    assert tool_body.index("upsertLiveToolCall(d,'start')") < tool_body.index(
        "_applyToAnchor('tool'"
    )
    done_body = _event_listener_body(src, "done")
    assert "_applyToAnchor('done',{" in done_body
    assert "usage:d.usage||null" in done_body
    assert "created_at:d.created_at||null" in done_body
    assert "_applyToAnchor('done',{...d" not in done_body
    assert "_flushReasoningToAnchor();" in done_body
    assert "_scheduleAnchorRegistryCleanup();" in done_body
    assert "lastAsst._anchor_stream_id=streamId" in done_body
    assert "'_anchor_stream_id'" in src
    assert src.index("'_anchor_stream_id'") < src.index("function _carryForwardEphemeralTurnFields")
