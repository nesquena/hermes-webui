"""Regression coverage for run-journal outcome reconstruction (#6212)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
ANCHORS_JS = REPO / "static" / "assistant_turn_anchors.js"
NODE = shutil.which("node")


def _js_function_source(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def test_snapshot_reconstructs_bounded_outcome_envelopes_without_activity_rows(monkeypatch):
    from api import routes

    stream_id = "run_outcomes"
    session_id = "session_outcomes"
    artifact = {
        "seq": 1,
        "event": "artifact_reference",
        "event_id": f"{stream_id}:1",
        "created_at": 101.25,
        "payload": {
            "kind": "workspace_file",
            "path": "reports/final.md",
            "source_tool": "write_file",
            "tool_call_id": "call-write",
            "content": "must not survive snapshot reconstruction",
            "absolute_path": "/private/workspace/reports/final.md",
        },
    }
    state_saved = {
        "seq": 3,
        "event": "state_saved",
        "event_id": f"{stream_id}:3",
        "created_at": 103.5,
        "payload": {
            "session_id": session_id,
            "kind": "skill",
            "action": "updated",
            "name": "release-notes",
            "body": "must not survive snapshot reconstruction",
        },
    }
    events = [
        artifact,
        {**artifact, "payload": {**artifact["payload"], "path": "duplicate.md"}},
        {
            "seq": 2,
            "event": "artifact_reference",
            "event_id": f"{stream_id}:2",
            "payload": {"kind": "workspace_file", "path": "../escape.md"},
        },
        state_saved,
        dict(state_saved),
        {
            "seq": 4,
            "event": "state_saved",
            "event_id": f"{stream_id}:4",
            "payload": {
                "session_id": "foreign_session",
                "kind": "memory",
                "action": "saved",
            },
        },
        {
            "seq": 5,
            "event": "artifact_reference",
            "event_id": f"{stream_id}:5",
            "payload": "not-an-object",
        },
        {
            "seq": 6,
            "event": "artifact_reference",
            "event_id": "foreign-run:6",
            "payload": {"kind": "workspace_file", "path": "foreign.md"},
        },
        {
            "seq": 7,
            "event": "artifact_reference",
            "event_id": f"{stream_id}:7",
            "payload": {
                "kind": "workspace_file",
                "path": r"reports\..\secret.md",
            },
        },
        {
            "seq": 8.5,
            "event": "artifact_reference",
            "event_id": f"{stream_id}:8",
            "payload": {"kind": "workspace_file", "path": "fractional-seq.md"},
        },
    ]

    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _stream_id: {
            "session_id": session_id,
            "run_id": stream_id,
            "last_seq": 8,
            "last_event_id": f"{stream_id}:8",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, _run_id: {"events": events},
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    scene = snapshot["anchor_activity_scene"]

    assert scene["activity_rows"] == []
    assert scene["artifacts"] == [
        {
            "source_event_type": "artifact_reference",
            "event_id": f"{stream_id}:1",
            "run_id": stream_id,
            "stream_id": stream_id,
            "seq": 1,
            "created_at": 101.25,
            "payload": {
                "kind": "workspace_file",
                "path": "reports/final.md",
                "source_tool": "write_file",
                "tool_call_id": "call-write",
            },
        }
    ]
    assert scene["side_effects"] == [
        {
            "source_event_type": "state_saved",
            "event_id": f"{stream_id}:3",
            "run_id": stream_id,
            "stream_id": stream_id,
            "seq": 3,
            "created_at": 103.5,
            "payload": {
                "session_id": session_id,
                "kind": "skill",
                "action": "updated",
                "name": "release-notes",
            },
        }
    ]
    assert "content" not in json.dumps(scene)
    assert "absolute_path" not in json.dumps(scene)
    assert "body" not in json.dumps(scene)


def test_snapshot_preserves_stable_run_identity_for_outcomes(monkeypatch):
    from api import routes

    stream_id = "stream_transport"
    run_id = "run_stable"
    session_id = "session_stable"
    events = [
        {
            "seq": 4,
            "event": "artifact_reference",
            "event_id": f"{run_id}:4",
            "run_id": run_id,
            "stream_id": stream_id,
            "created_at": 104.0,
            "payload": {"kind": "workspace_file", "path": "reports/final.md"},
        },
        {
            "seq": 5,
            "event": "state_saved",
            "event_id": f"{run_id}:5",
            "run_id": "foreign-run",
            "stream_id": stream_id,
            "payload": {
                "session_id": session_id,
                "kind": "memory",
                "action": "saved",
            },
        },
    ]
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _stream_id: {
            "session_id": session_id,
            "run_id": stream_id,
            "last_seq": 5,
            "last_event_id": f"{run_id}:5",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, _run_id: {"events": events},
    )

    scene = routes._run_journal_live_snapshot(stream_id)["anchor_activity_scene"]

    assert scene["artifacts"] == [
        {
            "source_event_type": "artifact_reference",
            "event_id": f"{run_id}:4",
            "run_id": run_id,
            "stream_id": stream_id,
            "seq": 4,
            "created_at": 104.0,
            "payload": {"kind": "workspace_file", "path": "reports/final.md"},
        }
    ]
    assert scene["side_effects"] == []


@pytest.mark.skipif(not NODE, reason="node is required for Anchor hydration coverage")
def test_outcome_only_scene_enters_inflight_and_replay_dedupes_in_real_registry():
    functions = "\n".join(
        [
            _js_function_source(SESSIONS_JS, "_anchorOutcomeEnvelopeIdentityKey"),
            _js_function_source(SESSIONS_JS, "_anchorActivitySceneHasRecoveryState"),
            _js_function_source(SESSIONS_JS, "_inflightHasVisibleLiveState"),
            _js_function_source(SESSIONS_JS, "_serverLiveSnapshotToolId"),
            _js_function_source(SESSIONS_JS, "_serverLiveSnapshotInflight"),
            _js_function_source(SESSIONS_JS, "_mergeServerLiveSnapshotOutcomesIntoInflight"),
            _js_function_source(MESSAGES_JS, "_applyAnchorRegistryOutcomesFromActivityScene"),
        ]
    )
    script = f"""
const fs=require('fs');
const vm=require('vm');
const sandbox={{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(ANCHORS_JS))},'utf8'),sandbox,{{filename:'assistant_turn_anchors.js'}});
vm.runInContext({json.dumps(functions)},sandbox,{{filename:'outcome_hydration_helpers.js'}});
const api=sandbox.window.HermesAssistantTurnAnchors;
const scene={{
  version:'activity_scene_v1',
  mode:'compact_worklog',
  identity:{{session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-1'}},
  activity_rows:[],
  artifacts:[
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:7',run_id:'stable-run-1',stream_id:'stream-1',seq:7,created_at:107,payload:{{kind:'workspace_file',path:'reports/final.md',source_tool:'write_file'}}}},
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:7',run_id:'stable-run-1',stream_id:'stream-1',seq:7,created_at:107,payload:{{kind:'workspace_file',path:'reports/duplicate.md',source_tool:'write_file'}}}},
    {{source_event_type:'state_saved',event_id:'stable-run-1:8',run_id:'stable-run-1',stream_id:'stream-1',seq:8,payload:{{kind:'memory'}}}},
  ],
  side_effects:[
    {{source_event_type:'state_saved',event_id:'stable-run-1:9',run_id:'stable-run-1',stream_id:'stream-1',seq:9,created_at:109,payload:{{session_id:'sid-1',kind:'skill',action:'created',name:'release-notes'}}}},
    null,
  ],
}};
const inflight=sandbox._serverLiveSnapshotInflight({{
  last_seq:9,
  messages:[],
  tool_calls:[],
  anchor_activity_scene:scene,
}},[]);
const visibleInflight={{
  messages:[{{role:'assistant',content:'local live progress'}}],
  lastAssistantText:'local live progress',
  lastRunJournalSeq:4,
  anchorActivityScene:{{
    version:'activity_scene_v1',
    identity:{{session_id:'sid-1',stream_id:'stream-1'}},
    activity_rows:[{{row_id:'local-prose',role:'prose'}}],
    artifacts:[],
    side_effects:[],
  }},
}};
const mergedVisible=sandbox._mergeServerLiveSnapshotOutcomesIntoInflight(visibleInflight,inflight);
const registry=api.createAssistantTurnAnchorRegistry({{session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-1'}});
const context={{session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-1'}};
const first=sandbox._applyAnchorRegistryOutcomesFromActivityScene(api,registry,scene,context);
const second=sandbox._applyAnchorRegistryOutcomesFromActivityScene(api,registry,scene,context);
console.log(JSON.stringify({{
  inflight:!!inflight,
  outcomeOnlyIsVisible:sandbox._inflightHasVisibleLiveState(inflight),
  mergedVisible,
  visibleText:visibleInflight.lastAssistantText,
  visibleCursor:visibleInflight.lastRunJournalSeq,
  visibleScene:visibleInflight.anchorActivityScene,
  first,
  second,
  artifacts:registry.anchor.artifacts,
  sideEffects:registry.anchor.side_effects,
  stats:registry.stats,
}}));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["inflight"] is True
    assert data["outcomeOnlyIsVisible"] is False
    assert data["mergedVisible"] is True
    assert data["visibleText"] == "local live progress"
    assert data["visibleCursor"] == 9
    assert data["visibleScene"]["activity_rows"] == [
        {"row_id": "local-prose", "role": "prose"}
    ]
    assert data["visibleScene"]["identity"] == {
        "session_id": "sid-1",
        "stream_id": "stream-1",
        "run_id": "stable-run-1",
    }
    assert len(data["visibleScene"]["artifacts"]) == 1
    assert len(data["visibleScene"]["side_effects"]) == 1
    assert data["first"] is True
    assert data["second"] is True
    assert [event["payload"]["path"] for event in data["artifacts"]] == [
        "reports/final.md"
    ]
    assert [event["payload"]["name"] for event in data["sideEffects"]] == [
        "release-notes"
    ]
    assert data["stats"]["applied"] == 2
    assert data["stats"]["skipped_duplicate"] == 4
    assert "run_id:_outcomeSceneRunId" in MESSAGES_JS
    assert "&&!_anchorActivitySceneHasRecoveryState(INFLIGHT[sid].anchorActivityScene)" in SESSIONS_JS.replace("\n", "")
