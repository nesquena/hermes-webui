"""Phase 0 contract tests for Stable Assistant Turn Anchors (#3926).

The first implementation slice was intentionally non-visual. Later slices keep
the same inventory contract while adding narrow, tested wiring points.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ANCHORS_JS = REPO / "static" / "assistant_turn_anchors.js"
INDEX_HTML = REPO / "static" / "index.html"
MESSAGES_JS = REPO / "static" / "messages.js"
UI_JS = REPO / "static" / "ui.js"
SESSIONS_JS = REPO / "static" / "sessions.js"
SW_JS = REPO / "static" / "sw.js"
PHASE0_DOC = REPO / "docs" / "architecture" / "stable-assistant-turn-anchor-phase0.md"
NODE = shutil.which("node")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _anchor_api_snapshot() -> dict:
    assert NODE, "node is required for assistant_turn_anchors.js helper tests"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ANCHORS_JS))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;
const anchor = api.createAssistantTurnAnchorSeed({{
  session_id:'sid-1',
  stream_id:'stream-1',
  run_id:'run-1',
  source_message_refs:['m1'],
}});
const out = {{
  version: api.version,
  kinds: api.activityEventKinds,
  layers: api.stateLayers,
  classifications: api.sourceEventClassification,
  classificationOrder: api.classificationOrder,
  terminalStates: api.terminalStates,
  terminalAliases: {{
    done: api.normalizeAssistantTurnAnchorTerminalState('done'),
    cancel: api.normalizeAssistantTurnAnchorTerminalState('cancel'),
    apperror: api.normalizeAssistantTurnAnchorTerminalState('apperror'),
    interruptedByUser: api.normalizeAssistantTurnAnchorTerminalState('interrupted-by-user'),
    lostBookkeeping: api.normalizeAssistantTurnAnchorTerminalState('lost_worker_bookkeeping'),
    maxIterations: api.normalizeAssistantTurnAnchorTerminalState('max_iterations'),
    unknown: api.normalizeAssistantTurnAnchorTerminalState('unknown'),
  }},
  tokenKind: api.classifyAssistantTurnAnchorSourceEvent('token').kind,
  streamEndClass: api.classifyAssistantTurnAnchorSourceEvent('stream_end').classification,
  unknownClass: api.classifyAssistantTurnAnchorSourceEvent('unknown_future').classification,
  eventIdKey: api.assistantTurnAnchorEventDedupeKey({{event_id:'run-1:2', text:'same'}}),
  runSeqKey: api.assistantTurnAnchorEventDedupeKey({{run_id:'run-1', seq:2, timestamp:123}}),
  localKey: api.assistantTurnAnchorEventDedupeKey({{session_id:'sid-1', source_event_type:'token', local_id:'local-1', seq:2, content:'ignored'}}),
  localNoSeqKey: api.assistantTurnAnchorEventDedupeKey({{session_id:'sid-1', source_event_type:'token', local_id:'local-1', content:'ignored'}}),
  zeroSeqKey: api.assistantTurnAnchorEventDedupeKey({{run_id:'run-1', seq:0, session_id:'sid-1', local_id:'local-1'}}),
  nanSeqKey: api.assistantTurnAnchorEventDedupeKey({{run_id:'run-1', seq:NaN, session_id:'sid-1', local_id:'local-1'}}),
  emptySeqKey: api.assistantTurnAnchorEventDedupeKey({{run_id:'run-1', seq:'', session_id:'sid-1', local_id:'local-1'}}),
  emptyKey: api.assistantTurnAnchorEventDedupeKey({{content:'visible text only', timestamp:123}}),
  artifactIsActivityKind: api.isAssistantTurnAnchorActivityKind('artifact_reference'),
  terminalIsActivityKind: api.isAssistantTurnAnchorActivityKind('terminal_status'),
  anchor,
}};
console.log(JSON.stringify(out));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_phase0_scaffold_is_loaded_before_current_rendering_modules():
    html = _read(INDEX_HTML)
    anchor_pos = html.index('static/assistant_turn_anchors.js?v=__WEBUI_VERSION__')
    ui_pos = html.index('static/ui.js?v=__WEBUI_VERSION__')
    sessions_pos = html.index('static/sessions.js?v=__WEBUI_VERSION__')
    messages_pos = html.index('static/messages.js?v=__WEBUI_VERSION__')

    assert anchor_pos < ui_pos < sessions_pos < messages_pos
    ui_src = _read(UI_JS)
    assert "'./static/assistant_turn_anchors.js' + VQ" in _read(SW_JS)
    assert "projectAssistantTurnAnchorSettledMessageFinalAnswer" in ui_src
    assert "createAssistantTurnAnchorRegistry" not in ui_src
    assert "applyAssistantTurnAnchorSourceEvent" not in ui_src
    assert "HermesAssistantTurnAnchors" not in _read(SESSIONS_JS)
    messages_src = _read(MESSAGES_JS)
    assert "window._liveAnchorRegistries" in messages_src
    assert "createAssistantTurnAnchorRegistry" in messages_src
    assert "applyAssistantTurnAnchorSourceEvent" in messages_src
    assert "projectAssistantTurnAnchorActivityScene" not in messages_src


def test_phase0_inventory_names_current_state_layers_in_authority_order():
    data = _anchor_api_snapshot()
    layer_ids = [layer["id"] for layer in data["layers"]]
    assert layer_ids == [
        "event_envelope",
        "run_journal",
        "settled_transcript",
        "S.messages",
        "INFLIGHT",
        "stream_closure",
        "live_dom",
    ]
    ranks = [layer["authorityRank"] for layer in data["layers"]]
    assert ranks == sorted(ranks)
    assert data["layers"][0]["role"] == "durable_identity"
    assert data["layers"][-1]["role"] == "renderer_output"


def test_phase0_classifies_all_current_live_to_final_sources():
    data = _anchor_api_snapshot()
    classifications = data["classifications"]
    required = {
        "token": ("activity", "process_prose"),
        "interim_assistant": ("activity", "process_prose"),
        "reasoning": ("activity", "reasoning"),
        "tool": ("activity", "tool_started"),
        "tool_complete": ("activity", "tool_completed"),
        "tool_update": ("activity", "tool_updated"),
        "compressing": ("activity", "lifecycle_status"),
        "compressed": ("activity", "lifecycle_status"),
        "approval": ("activity", "control_boundary"),
        "clarify": ("activity", "control_boundary"),
        "pending_steer_leftover": ("activity", "control_boundary"),
        "goal_continue": ("activity", "control_boundary"),
        "done": ("activity", "terminal_status"),
        "cancel": ("activity", "terminal_status"),
        "error": ("activity", "terminal_status"),
        "apperror": ("activity", "terminal_status"),
        "stream_end": ("transport", None),
        "runtime_journal_snapshot": ("metadata", None),
        "inflight_snapshot": ("metadata", None),
        "settled_message": ("metadata", None),
    }
    for source, expected in required.items():
        item = classifications[source]
        assert (item["classification"], item["kind"]) == expected

    assert data["tokenKind"] == "process_prose"
    assert data["streamEndClass"] == "transport"
    assert data["unknownClass"] == "excluded"
    assert data["artifactIsActivityKind"] is False
    assert data["terminalIsActivityKind"] is True
    for item in classifications.values():
        if item["classification"] == "activity":
            assert item["kind"] in data["kinds"]
        elif item["kind"] is not None:
            assert item["kind"] not in data["kinds"]


def test_phase0_dedupe_prefers_event_envelope_not_visible_text_or_timestamps():
    data = _anchor_api_snapshot()
    assert data["eventIdKey"] == 'event_id:"run-1:2"'
    assert data["runSeqKey"] == 'run_seq:["run-1","2"]'
    assert data["localKey"] == 'local:["sid-1","token","local-1","2"]'
    assert data["localNoSeqKey"] == ""
    assert data["zeroSeqKey"] == 'run_seq:["run-1","0"]'
    assert data["nanSeqKey"] == 'run_seq:["run-1","NaN"]'
    assert data["emptySeqKey"] == ""
    assert data["emptyKey"] == ""

    helper_src = _read(ANCHORS_JS).split("function assistantTurnAnchorEventDedupeKey", 1)[1]
    helper_src = helper_src.split("function classifyAssistantTurnAnchorSourceEvent", 1)[0]
    assert "event_id" in helper_src
    assert "run_id" in helper_src
    assert "seq" in helper_src
    assert "text" not in helper_src
    assert "content" not in helper_src
    assert "timestamp" not in helper_src
    assert "created_at" not in helper_src


def test_phase0_exports_terminal_state_contract_and_aliases():
    data = _anchor_api_snapshot()
    assert data["terminalStates"] == {
        "completed": "completed",
        "cancelled": "cancelled",
        "interrupted": "interrupted",
        "no_response": "no_response",
        "tool_limit_reached": "tool_limit_reached",
        "compression_exhausted": "compression_exhausted",
        "connection_lost": "connection_lost",
        "degraded": "degraded",
        "error": "error",
    }
    assert data["terminalAliases"] == {
        "done": "completed",
        "cancel": "cancelled",
        "apperror": "error",
        "interruptedByUser": "interrupted",
        "lostBookkeeping": "connection_lost",
        "maxIterations": "tool_limit_reached",
        "unknown": None,
    }


def test_phase0_anchor_seed_matches_rfc_shape_without_registering_state():
    data = _anchor_api_snapshot()
    anchor = data["anchor"]
    assert anchor["identity"]["session_id"] == "sid-1"
    assert anchor["identity"]["run_id"] == "run-1"
    assert anchor["identity"]["stream_id"] == "stream-1"
    assert anchor["lifecycle"]["status"] == "created"
    assert anchor["content"]["final_answer"] == ""
    assert anchor["activity_events"] == []
    assert anchor["artifacts"] == []
    assert anchor["side_effects"] == []
    assert "presentation_state" not in anchor


def test_phase0_inventory_doc_matches_scaffold_contract():
    doc = _read(PHASE0_DOC)
    for marker in [
        "RuntimeAdapter / run-journal Event Envelope",
        "Run journal replay events",
        "Server settled transcript",
        "`S.messages`",
        "`INFLIGHT`",
        "Stream closure state",
        "Live DOM",
        "Slice 7 Dual-Run Reconciler",
        "`HermesAssistantTurnAnchors.reconcileAssistantTurnAnchorActivityScene()`",
        "`activity_scene_reconciliation_v1`",
        "Dedupe Invariant",
        "`event_id`",
        "`run_id + seq`",
        "`session_id + source_event_type + local_id + seq`",
    ]:
        assert marker in doc
