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
    params = source.index("(", start)
    depth = 0
    close = -1
    for index in range(params, len(source)):
        char = source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close = index
                break
    assert close != -1, f"unterminated JavaScript parameters: {name}"
    brace = source.index("{", close)
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
        {
            "seq": 9,
            "event": "artifact_reference",
            "session_id": "foreign_session",
            "event_id": f"{stream_id}:9",
            "payload": {"kind": "workspace_file", "path": "foreign-session.md"},
        },
        {
            "seq": 10,
            "event": "state_saved",
            "session_id": "foreign_session",
            "event_id": f"{stream_id}:10",
            "payload": {
                "session_id": session_id,
                "kind": "skill",
                "action": "updated",
                "name": "foreign-envelope",
            },
        },
    ]

    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _stream_id: {
            "session_id": session_id,
            "run_id": stream_id,
            "last_seq": 10,
            "last_event_id": f"{stream_id}:10",
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
            "session_id": session_id,
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
            "session_id": session_id,
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


def _stable_outcome_scene(monkeypatch):
    from api import routes

    stream_id = "stream_transport"
    run_id = "run_stable"
    session_id = "session_stable"
    events = [
        {
            "seq": 1,
            "event": "token",
            "event_id": f"{stream_id}:1",
            "payload": {"text": "visible progress"},
        },
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
            "run_id": run_id,
            "last_seq": 5,
            "last_event_id": f"{run_id}:5",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, _run_id: {"events": events},
    )

    return routes._run_journal_live_snapshot(stream_id)["anchor_activity_scene"]


def test_snapshot_preserves_stable_run_identity_for_outcomes(monkeypatch):
    scene = _stable_outcome_scene(monkeypatch)

    assert scene["identity"]["run_id"] == "run_stable"
    assert scene["identity"]["stream_id"] == "stream_transport"
    assert scene["artifacts"] == [
        {
            "source_event_type": "artifact_reference",
            "event_id": "run_stable:4",
            "session_id": "session_stable",
            "run_id": "run_stable",
            "stream_id": "stream_transport",
            "seq": 4,
            "created_at": 104.0,
            "payload": {"kind": "workspace_file", "path": "reports/final.md"},
        }
    ]
    assert scene["side_effects"] == []


def test_snapshot_rejects_outcomes_that_would_steer_scene_owner(monkeypatch):
    from api import routes

    stream_id = "stream_transport"
    session_id = "session_owner"
    foreign_run_id = "run_stable_from_outcome"
    events = [
        {
            "seq": 1,
            "event": "token",
            "event_id": f"{stream_id}:1",
            "payload": {"text": "visible progress"},
        },
        {
            "seq": 4,
            "event": "artifact_reference",
            "event_id": f"{foreign_run_id}:4",
            "run_id": foreign_run_id,
            "stream_id": stream_id,
            "payload": {"kind": "workspace_file", "path": "reports/foreign.md"},
        },
        {
            "seq": 5,
            "event": "state_saved",
            "event_id": f"{stream_id}:5",
            "stream_id": stream_id,
            "payload": {
                "session_id": session_id,
                "kind": "skill",
                "action": "updated",
                "name": "owned-stream-state",
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
            "last_event_id": f"{stream_id}:5",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, _run_id: {"events": events},
    )

    scene = routes._run_journal_live_snapshot(stream_id)["anchor_activity_scene"]

    assert scene["identity"]["run_id"] == stream_id
    assert scene["artifacts"] == []
    assert [event["event_id"] for event in scene["side_effects"]] == [f"{stream_id}:5"]


def test_snapshot_caps_reconstructed_outcomes_by_count(monkeypatch):
    from api import routes

    stream_id = "stream_outcome_count_cap"
    session_id = "session_count_cap"
    events = [
        {
            "seq": seq,
            "event": "state_saved",
            "event_id": f"{stream_id}:{seq}",
            "stream_id": stream_id,
            "payload": {
                "session_id": session_id,
                "kind": "skill",
                "action": "updated",
                "name": f"state-{seq}",
            },
        }
        for seq in range(1, 6)
    ]
    monkeypatch.setattr(routes, "_RUN_JOURNAL_RECONSTRUCTED_OUTCOME_MAX_EVENTS", 2)
    monkeypatch.setattr(routes, "_RUN_JOURNAL_RECONSTRUCTED_OUTCOME_MAX_BYTES", 1_000_000)
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _stream_id: {
            "session_id": session_id,
            "run_id": stream_id,
            "last_seq": 5,
            "last_event_id": f"{stream_id}:5",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, _run_id: {"events": events},
    )

    scene = routes._run_journal_live_snapshot(stream_id)["anchor_activity_scene"]

    assert [event["event_id"] for event in scene["side_effects"]] == [
        f"{stream_id}:1",
        f"{stream_id}:2",
    ]
    assert scene["outcomes_truncated"]["reason"] == "count"
    assert scene["outcomes_truncated"]["accepted_count"] == 2
    routes._sanitize_anchor_activity_scene(scene)


def test_snapshot_caps_reconstructed_outcomes_by_encoded_bytes(monkeypatch):
    from api import routes

    stream_id = "stream_outcome_byte_cap"
    session_id = "session_byte_cap"
    events = [
        {
            "seq": 1,
            "event": "artifact_reference",
            "event_id": f"{stream_id}:1",
            "stream_id": stream_id,
            "payload": {
                "kind": "workspace_file",
                "path": "reports/a.md",
                "source_tool": "write_file",
            },
        },
        {
            "seq": 2,
            "event": "artifact_reference",
            "event_id": f"{stream_id}:2",
            "stream_id": stream_id,
            "payload": {
                "kind": "workspace_file",
                "path": "reports/b.md",
                "source_tool": "x" * 512,
            },
        },
    ]
    monkeypatch.setattr(routes, "_RUN_JOURNAL_RECONSTRUCTED_OUTCOME_MAX_EVENTS", 10)
    first = routes._run_journal_outcome_event(
        events[0],
        event_name="artifact_reference",
        session_id=session_id,
        stream_id=stream_id,
        fallback_run_id=stream_id,
    )
    assert first is not None
    monkeypatch.setattr(
        routes,
        "_RUN_JOURNAL_RECONSTRUCTED_OUTCOME_MAX_BYTES",
        routes._run_journal_outcome_encoded_size(first) + 10,
    )
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _stream_id: {
            "session_id": session_id,
            "run_id": stream_id,
            "last_seq": 2,
            "last_event_id": f"{stream_id}:2",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, _run_id: {"events": events},
    )

    scene = routes._run_journal_live_snapshot(stream_id)["anchor_activity_scene"]

    assert [event["event_id"] for event in scene["artifacts"]] == [f"{stream_id}:1"]
    assert scene["outcomes_truncated"]["reason"] == "bytes"
    assert scene["outcomes_truncated"]["accepted_count"] == 1
    routes._sanitize_anchor_activity_scene(scene)


@pytest.mark.skipif(not NODE, reason="node is required for recovery identity coverage")
def test_backend_stable_scene_identity_passes_frontend_recovery_validation(monkeypatch):
    scene = _stable_outcome_scene(monkeypatch)
    functions = "\n".join(
        [
            _js_function_source(SESSIONS_JS, "_sessionAnchorOutcomeEnvelopeIdentityKey"),
            _js_function_source(SESSIONS_JS, "_anchorActivitySceneHasRecoveryState"),
        ]
    )
    script = f"""
const vm=require('vm');
const sandbox={{}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(functions)},sandbox,{{filename:'recovery_identity_helpers.js'}});
console.log(JSON.stringify({{
  runId:{json.dumps(scene["identity"]["run_id"])},
  streamId:{json.dumps(scene["identity"]["stream_id"])},
  recoverable:sandbox._anchorActivitySceneHasRecoveryState({json.dumps(scene)}),
}}));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "runId": "run_stable",
        "streamId": "stream_transport",
        "recoverable": True,
    }


def test_snapshot_rejects_legacy_outcome_prefix_that_conflicts_with_authoritative_run(monkeypatch):
    from api import routes

    stream_id = "stream_transport"
    run_id = "run_stable"
    session_id = "session_mixed"
    events = [
        {
            "seq": 1,
            "event": "token",
            "event_id": f"{stream_id}:1",
            "payload": {"text": "visible progress"},
        },
        {
            "seq": 4,
            "event": "artifact_reference",
            "event_id": f"{run_id}:4",
            "run_id": run_id,
            "stream_id": stream_id,
            "payload": {"kind": "workspace_file", "path": "reports/final.md"},
        },
        {
            "seq": 5,
            "event": "state_saved",
            "event_id": f"{stream_id}:5",
            "stream_id": stream_id,
            "payload": {
                "session_id": session_id,
                "kind": "skill",
                "action": "updated",
                "name": "release-notes",
            },
        },
    ]
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _stream_id: {
            "session_id": session_id,
            "run_id": run_id,
            "last_seq": 5,
            "last_event_id": f"{stream_id}:5",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, _run_id: {"events": events},
    )

    scene = routes._run_journal_live_snapshot(stream_id)["anchor_activity_scene"]

    assert scene["identity"]["run_id"] == run_id
    assert scene["identity"]["stream_id"] == stream_id
    assert scene["activity_rows"]
    assert all(row["run_id"] == run_id for row in scene["activity_rows"])
    assert all(row["identity"]["run_id"] == run_id for row in scene["activity_rows"])
    outcomes = [*scene["artifacts"], *scene["side_effects"]]
    assert [outcome["run_id"] for outcome in outcomes] == [run_id]
    assert [outcome["event_id"] for outcome in outcomes] == [f"{run_id}:4"]


@pytest.mark.skipif(not NODE, reason="node is required for Anchor hydration coverage")
def test_outcome_only_scene_enters_inflight_and_replay_dedupes_in_real_registry():
    functions = "\n".join(
        [
            _js_function_source(SESSIONS_JS, "_sessionAnchorOutcomeEnvelopeIdentityKey"),
            _js_function_source(SESSIONS_JS, "_anchorActivitySceneHasRecoveryState"),
            _js_function_source(SESSIONS_JS, "_anchorActivitySceneMergeIdentity"),
            _js_function_source(SESSIONS_JS, "_inflightHasVisibleLiveState"),
            _js_function_source(SESSIONS_JS, "_serverLiveSnapshotToolId"),
            _js_function_source(SESSIONS_JS, "_serverLiveSnapshotInflight"),
            _js_function_source(SESSIONS_JS, "_mergeServerLiveSnapshotOutcomesIntoInflight"),
            _js_function_source(MESSAGES_JS, "_liveAnchorActivitySceneIdentity"),
            _js_function_source(MESSAGES_JS, "_anchorOutcomeEnvelopeIdentityKey"),
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
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:7',session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-1',seq:7,created_at:107,payload:{{kind:'workspace_file',path:'reports/final.md',source_tool:'write_file'}}}},
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:7',session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-1',seq:7,created_at:107,payload:{{kind:'workspace_file',path:'reports/duplicate.md',source_tool:'write_file'}}}},
    {{source_event_type:'state_saved',event_id:'stable-run-1:8',run_id:'stable-run-1',stream_id:'stream-1',seq:8,payload:{{kind:'memory'}}}},
  ],
  side_effects:[
    {{source_event_type:'state_saved',event_id:'stable-run-1:9',session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-1',seq:9,created_at:109,payload:{{session_id:'sid-1',kind:'skill',action:'created',name:'release-notes'}}}},
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
    identity:{{session_id:'sid-1',stream_id:'stream-1',run_id:'stable-run-1'}},
    activity_rows:[{{row_id:'local-prose',role:'prose'}}],
    artifacts:[],
    side_effects:[],
  }},
}};
const mergedVisible=sandbox._mergeServerLiveSnapshotOutcomesIntoInflight(visibleInflight,inflight,'stream-1');
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
    assert "run_id:_outcomeSceneIdentity.runId" in MESSAGES_JS
    assert "&&!_anchorActivitySceneHasRecoveryState(INFLIGHT[sid].anchorActivityScene)" in SESSIONS_JS.replace("\n", "")


@pytest.mark.skipif(not NODE, reason="node is required for Anchor hydration coverage")
def test_outcome_replay_rejects_per_envelope_identity_mismatches():
    functions = "\n".join(
        [
            _js_function_source(MESSAGES_JS, "_liveAnchorActivitySceneIdentity"),
            _js_function_source(MESSAGES_JS, "_anchorOutcomeEnvelopeIdentityKey"),
            _js_function_source(MESSAGES_JS, "_applyAnchorRegistryOutcomesFromActivityScene"),
        ]
    )
    script = f"""
const fs=require('fs');
const assert=require('assert');
const vm=require('vm');
const sandbox={{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(ANCHORS_JS))},'utf8'),sandbox,{{filename:'assistant_turn_anchors.js'}});
vm.runInContext({json.dumps(functions)},sandbox,{{filename:'outcome_envelope_probe.js'}});
const api=sandbox.window.HermesAssistantTurnAnchors;
const registry=api.createAssistantTurnAnchorRegistry({{session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-current'}});
const scene={{
  version:'activity_scene_v1',
  identity:{{session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-current'}},
  activity_rows:[],
  artifacts:[
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:7',session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-current',seq:7,payload:{{kind:'workspace_file',path:'owned.md'}}}},
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:8',session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-stale',seq:8,payload:{{kind:'workspace_file',path:'wrong-stream.md'}}}},
    {{source_event_type:'artifact_reference',event_id:'other-run:9',session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-current',seq:9,payload:{{kind:'workspace_file',path:'wrong-event-run.md'}}}},
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:10',session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-current',seq:11,payload:{{kind:'workspace_file',path:'wrong-seq.md'}}}},
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:13',run_id:'stable-run-1',stream_id:'stream-current',seq:13,payload:{{kind:'workspace_file',path:'missing-session.md'}}}},
  ],
  side_effects:[
    {{source_event_type:'state_saved',event_id:'stable-run-1:12',session_id:'sid-foreign',run_id:'stable-run-1',stream_id:'stream-current',seq:12,payload:{{session_id:'sid-1',kind:'skill',action:'updated',name:'foreign-envelope'}}}},
  ],
}};
const accepted=sandbox._applyAnchorRegistryOutcomesFromActivityScene(
  api,
  registry,
  scene,
  {{session_id:'sid-1',stream_id:'stream-current',run_id:'stable-run-1'}}
);
assert.strictEqual(accepted,true);
assert.strictEqual(JSON.stringify(registry.anchor.artifacts.map(event=>event.payload.path)),JSON.stringify(['owned.md']));
assert.strictEqual(JSON.stringify(registry.anchor.side_effects),JSON.stringify([]));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not NODE, reason="node is required for settled Anchor scene coverage")
def test_projected_outcomes_survive_settlement_and_reload():
    functions = "\n".join(
        [
            _js_function_source(MESSAGES_JS, "_liveAnchorActivitySceneIdentity"),
            _js_function_source(MESSAGES_JS, "_anchorOutcomeEnvelopeIdentityKey"),
            _js_function_source(MESSAGES_JS, "_applyAnchorRegistryOutcomesFromActivityScene"),
            _js_function_source(MESSAGES_JS, "_anchorSceneHasWorklogWorthyRows"),
            _js_function_source(MESSAGES_JS, "_anchorSceneHasOwnedOutcomes"),
            _js_function_source(MESSAGES_JS, "_attachProjectedAnchorSceneToLastAssistant"),
        ]
    )
    script = f"""
const fs=require('fs');
const assert=require('assert');
const vm=require('vm');
const sandbox={{window:{{isFinalAnswerOnlyMode:()=>false}},console}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(ANCHORS_JS))},'utf8'),sandbox,{{filename:'assistant_turn_anchors.js'}});
vm.runInContext({json.dumps(functions)},sandbox,{{filename:'settled_outcome_helpers.js'}});
vm.runInContext(`
const api=window.HermesAssistantTurnAnchors;
const registry=api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-settled',
  stream_id:'stream-settled',
  run_id:'run-settled',
}});
api.applyAssistantTurnAnchorSourceEvents(registry, [
  {{event:'artifact_reference', payload:{{path:'reports/final.md', kind:'workspace_file'}}, event_id:'run-settled:7', seq:7}},
  {{event:'state_saved', payload:{{kind:'memory', name:'settled-state'}}, event_id:'run-settled:8', seq:8}},
], {{session_id:'sid-settled', stream_id:'stream-settled', run_id:'run-settled'}});
var projectedScene=api.projectAssistantTurnAnchorActivityScene(registry, {{mode:'compact_worklog'}});
var _anchorRegistry=registry;
var streamId='stream-settled';
var persisted=null;
function _projectLiveAnchorActivityScene(){{ return projectedScene; }}
function _completeSettledAnchorSceneForTurn(messages,lastAsstIndex,scene){{
  return {{
    ...scene,
    final_message_ref:'message-final',
    activity_rows:Array.isArray(scene.activity_rows)?scene.activity_rows:[],
  }};
}}
function _persistSettledAnchorScene(message,scene,messageIndex){{ persisted={{message,scene,messageIndex}}; }}
const messages=[
  {{role:'user',content:'write the report'}},
  {{role:'assistant',content:'done'}},
];
const promoted=_attachProjectedAnchorSceneToLastAssistant(messages);
const reloadScene=JSON.parse(JSON.stringify(messages[1]._anchor_activity_scene));
const reloadRegistry=api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-settled',
  stream_id:'stream-settled',
  run_id:'run-settled',
}});
const hydrated=_applyAnchorRegistryOutcomesFromActivityScene(
  api,
  reloadRegistry,
  reloadScene,
  {{session_id:'sid-settled', stream_id:'stream-settled', run_id:'run-settled'}}
);
globalThis.result={{
  projectedScene,
  promoted,
  persisted,
  messageScene:messages[1]._anchor_activity_scene,
  reloadScene,
  hydrated,
  reloadRegistry,
}};
`, sandbox, {{filename:'settled_outcome_probe.js'}});
console.log(JSON.stringify(sandbox.result));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["projectedScene"]["activity_rows"] == []
    assert data["projectedScene"]["artifacts"][0]["payload"] == {
        "kind": "workspace_file",
        "path": "reports/final.md",
    }
    assert data["projectedScene"]["side_effects"][0]["payload"] == {
        "kind": "memory",
        "name": "settled-state",
    }
    assert data["promoted"] is False
    assert data["persisted"]["messageIndex"] == 1
    assert data["messageScene"] == data["reloadScene"]
    assert data["reloadScene"]["activity_rows"] == []
    assert [event["payload"]["path"] for event in data["reloadScene"]["artifacts"]] == [
        "reports/final.md"
    ]
    assert [event["payload"]["name"] for event in data["reloadScene"]["side_effects"]] == [
        "settled-state"
    ]
    assert data["hydrated"] is True
    assert [event["payload"]["path"] for event in data["reloadRegistry"]["anchor"]["artifacts"]] == [
        "reports/final.md"
    ]
    assert [
        event["payload"]["name"]
        for event in data["reloadRegistry"]["anchor"]["side_effects"]
    ] == ["settled-state"]


@pytest.mark.skipif(not NODE, reason="node is required for recovery identity coverage")
def test_outcome_merge_fails_closed_on_missing_or_wrong_scene_identity():
    functions = "\n".join(
        [
            _js_function_source(SESSIONS_JS, "_sessionAnchorOutcomeEnvelopeIdentityKey"),
            _js_function_source(SESSIONS_JS, "_anchorActivitySceneHasRecoveryState"),
            _js_function_source(SESSIONS_JS, "_anchorActivitySceneMergeIdentity"),
            _js_function_source(SESSIONS_JS, "_serverLiveSnapshotToolId"),
            _js_function_source(SESSIONS_JS, "_serverLiveSnapshotInflight"),
            _js_function_source(SESSIONS_JS, "_mergeServerLiveSnapshotOutcomesIntoInflight"),
        ]
    )
    script = f"""
const assert=require('assert');
const vm=require('vm');
const sandbox={{}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(functions)},sandbox,{{filename:'merge_identity_helpers.js'}});
const journalScene={{
  version:'activity_scene_v1',
  identity:{{session_id:'sid-1',stream_id:'stream-1',run_id:'stable-run-1'}},
  activity_rows:[],
  artifacts:[
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:99',session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-1',seq:99,payload:{{kind:'workspace_file',path:'owned.md'}}}},
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:100',session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-stale',seq:100,payload:{{kind:'workspace_file',path:'stale.md'}}}},
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:101',run_id:'stable-run-1',stream_id:'stream-1',seq:101,payload:{{kind:'workspace_file',path:'missing-session.md'}}}},
  ],
  side_effects:[],
}};
const server=sandbox._serverLiveSnapshotInflight({{
  stream_id:'stream-1',
  last_seq:99,
  anchor_activity_scene:journalScene,
}},[]);
const missingCached={{
  streamId:'stream-1',
  lastRunJournalSeq:5,
  lastAssistantText:'local',
  messages:[{{role:'assistant',content:'local'}}],
  anchorActivityScene:{{
    version:'activity_scene_v1',
    identity:{{}},
    activity_rows:[{{row_id:'cached-prose'}}],
    artifacts:[],
    side_effects:[],
  }},
}};
assert.strictEqual(sandbox._mergeServerLiveSnapshotOutcomesIntoInflight(missingCached,server,'stream-1'),false);
assert.strictEqual(missingCached.lastRunJournalSeq,5);
assert.deepStrictEqual(missingCached.anchorActivityScene.artifacts,[]);
const compatible={{
  ...missingCached,
  lastRunJournalSeq:6,
  anchorActivityScene:{{
    version:'activity_scene_v1',
    identity:{{session_id:'sid-1',stream_id:'stream-1',run_id:'stable-run-1'}},
    activity_rows:[{{row_id:'cached-prose'}}],
    artifacts:[],
    side_effects:[],
  }},
}};
assert.strictEqual(sandbox._mergeServerLiveSnapshotOutcomesIntoInflight(compatible,server,'stream-2'),false);
assert.strictEqual(compatible.lastRunJournalSeq,6);
const foreignSession={{
  ...compatible,
  anchorActivityScene:{{
    ...compatible.anchorActivityScene,
    identity:{{session_id:'sid-foreign',stream_id:'stream-1',run_id:'stable-run-1'}},
  }},
}};
assert.strictEqual(sandbox._mergeServerLiveSnapshotOutcomesIntoInflight(foreignSession,server,'stream-1'),false);
assert.strictEqual(foreignSession.lastRunJournalSeq,6);
const owned={{
  streamId:'stream-1',
  lastRunJournalSeq:6,
  lastAssistantText:'local',
  messages:[{{role:'assistant',content:'local'}}],
  anchorActivityScene:{{
    version:'activity_scene_v1',
    identity:{{session_id:'sid-1',stream_id:'stream-1',run_id:'stable-run-1'}},
    activity_rows:[{{row_id:'cached-prose'}}],
    artifacts:[],
    side_effects:[],
  }},
}};
assert.strictEqual(sandbox._mergeServerLiveSnapshotOutcomesIntoInflight(owned,server,'stream-1'),true);
assert.strictEqual(JSON.stringify(owned.anchorActivityScene.artifacts.map(event=>event.payload.path)),JSON.stringify(['owned.md']));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not NODE, reason="node is required for Anchor registry coverage")
def test_stable_recovered_scene_replaces_transport_registry_before_outcomes():
    functions = "\n".join(
        [
            _js_function_source(MESSAGES_JS, "_liveAnchorActivitySceneIdentity"),
            _js_function_source(MESSAGES_JS, "_liveAnchorRegistryIdentity"),
            _js_function_source(MESSAGES_JS, "_liveAnchorRegistryForActivityScene"),
            _js_function_source(MESSAGES_JS, "_anchorOutcomeEnvelopeIdentityKey"),
            _js_function_source(MESSAGES_JS, "_applyAnchorRegistryOutcomesFromActivityScene"),
        ]
    )
    script = f"""
const fs=require('fs');
const assert=require('assert');
const vm=require('vm');
const sandbox={{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(ANCHORS_JS))},'utf8'),sandbox,{{filename:'assistant_turn_anchors.js'}});
vm.runInContext({json.dumps(functions)},sandbox,{{filename:'stable_registry_helpers.js'}});
const api=sandbox.window.HermesAssistantTurnAnchors;
const registryMap=new Map();
const transportRegistry=api.createAssistantTurnAnchorRegistry({{session_id:'sid-1',stream_id:'stream-1',run_id:'stream-1'}});
api.applyAssistantTurnAnchorSourceEvent(
  transportRegistry,
  {{source_event_type:'tool',local_id:'legacy-tool',name:'terminal',run_id:'stream-1',stream_id:'stream-1'}},
  {{session_id:'sid-1',stream_id:'stream-1',run_id:'stream-1'}}
);
registryMap.set('stream-1',transportRegistry);
const scene={{
  version:'activity_scene_v1',
  identity:{{session_id:'sid-1',stream_id:'stream-1',run_id:'stable-run-1'}},
  activity_rows:[{{row_id:'tool:legacy',role:'tool',run_id:'stream-1',stream_id:'stream-1'}}],
  artifacts:[
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:7',session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-1',seq:7,payload:{{kind:'workspace_file',path:'reports/final.md'}}}},
  ],
  side_effects:[],
}};
const registry=sandbox._liveAnchorRegistryForActivityScene(
  api,
  registryMap,
  'stream-1',
  'sid-1',
  scene,
  registryMap.get('stream-1')
);
assert.notStrictEqual(registry,transportRegistry);
assert.strictEqual(registryMap.get('stream-1'),registry);
const registryIdentity=sandbox._liveAnchorRegistryIdentity(registry);
assert.strictEqual(registryIdentity.sessionId,'sid-1');
assert.strictEqual(registryIdentity.runId,'stable-run-1');
assert.strictEqual(registryIdentity.streamId,'stream-1');
assert.strictEqual(
  sandbox._applyAnchorRegistryOutcomesFromActivityScene(api,registry,scene,{{session_id:'sid-1',stream_id:'stream-1',run_id:'stable-run-1'}}),
  true
);
assert.strictEqual(registry.stats.skipped_mismatched,0);
assert.strictEqual(registry.anchor.artifacts.length,1);
"""
    result = subprocess.run(
        [NODE, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_session_recovery_paths_wire_reconstructed_outcomes_into_anchor_state():
    load_session = _js_function_source(SESSIONS_JS, "loadSession")
    attach_live_stream = _js_function_source(MESSAGES_JS, "attachLiveStream")
    compact_load = "".join(load_session.split())
    compact_attach = "".join(attach_live_stream.split())

    snapshot = "_serverLiveSnapshotInflight(S.session.runtime_journal_snapshot,S.session.pending_attachments||[])"
    merge = "_mergeServerLiveSnapshotOutcomesIntoInflight(INFLIGHT[sid],serverLiveSnapshot,activeStreamId)"
    assert snapshot in compact_load
    assert merge in compact_load
    assert compact_load.index(snapshot) < compact_load.index(merge)

    hydrate_rows = "_hydrateAnchorRegistryFromActivityScene(INFLIGHT[activeSid]&&INFLIGHT[activeSid].anchorActivityScene)"
    hydrate_outcomes = "_applyAnchorRegistryOutcomesFromActivityScene("
    assert hydrate_rows in compact_attach
    assert hydrate_outcomes in compact_attach
    assert compact_attach.index(hydrate_rows) < compact_attach.index(hydrate_outcomes)
    assert "{session_id:activeSid,stream_id:_outcomeSceneIdentity.streamId,run_id:_outcomeSceneIdentity.runId}" in compact_attach
