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


SESSION_ANCHOR_SIZE_CONSTANTS = """
const _SESSION_ANCHOR_ACTIVITY_SCENE_MAX_BYTES=256000;
const _SESSION_ANCHOR_OUTCOME_MAX_EVENTS=512;
const _SESSION_ANCHOR_OUTCOME_MAX_BYTES=128000;
"""


MESSAGE_ANCHOR_SIZE_CONSTANTS = """
const _MESSAGE_ANCHOR_ACTIVITY_SCENE_MAX_BYTES=256000;
const _MESSAGE_ANCHOR_OUTCOME_MAX_EVENTS=512;
const _MESSAGE_ANCHOR_OUTCOME_MAX_BYTES=128000;
"""


def _session_anchor_recovery_helper_sources() -> list[str]:
    return [
        _js_function_source(SESSIONS_JS, "_sessionAnchorOutcomeEnvelopeIdentityKey"),
        _js_function_source(SESSIONS_JS, "_sessionAnchorCanonicalOutcomeReason"),
        _js_function_source(SESSIONS_JS, "_sessionAnchorOutcomeTruncationMarker"),
        _js_function_source(SESSIONS_JS, "_anchorActivitySceneStrictIdentity"),
        _js_function_source(SESSIONS_JS, "_anchorActivitySceneHasRecoveryState"),
        _js_function_source(SESSIONS_JS, "_anchorActivitySceneMergeIdentity"),
    ]


def _session_anchor_merge_helper_sources() -> list[str]:
    return [
        SESSION_ANCHOR_SIZE_CONSTANTS,
        *_session_anchor_recovery_helper_sources(),
        _js_function_source(SESSIONS_JS, "_sessionAnchorUtf8ByteLength"),
        _js_function_source(SESSIONS_JS, "_sessionAnchorCompactSceneBytes"),
        _js_function_source(SESSIONS_JS, "_sessionAnchorOutcomeBytes"),
        _js_function_source(SESSIONS_JS, "_sessionAnchorReasonRank"),
        _js_function_source(SESSIONS_JS, "_sessionAnchorMergedReason"),
        _js_function_source(SESSIONS_JS, "_sessionAnchorOutcomeMarker"),
        _js_function_source(SESSIONS_JS, "_sessionAnchorOutcomeSeq"),
        _js_function_source(SESSIONS_JS, "_sessionAnchorOutcomeItems"),
        _js_function_source(SESSIONS_JS, "_sessionAnchorSceneWithOutcomeItems"),
        _js_function_source(SESSIONS_JS, "_sessionAnchorMinimalOutcomeScene"),
        _js_function_source(SESSIONS_JS, "_sessionAnchorBoundedActivityScene"),
        _js_function_source(SESSIONS_JS, "_serverLiveSnapshotToolId"),
        _js_function_source(SESSIONS_JS, "_serverLiveSnapshotInflight"),
        _js_function_source(SESSIONS_JS, "_mergeServerLiveSnapshotOutcomesIntoInflight"),
    ]


def _message_anchor_outcome_helper_sources() -> list[str]:
    return [
        MESSAGE_ANCHOR_SIZE_CONSTANTS,
        _js_function_source(MESSAGES_JS, "_liveAnchorActivitySceneIdentity"),
        _js_function_source(MESSAGES_JS, "_anchorOutcomeEnvelopeIdentityKey"),
        _js_function_source(MESSAGES_JS, "_messageAnchorCanonicalOutcomeReason"),
        _js_function_source(MESSAGES_JS, "_anchorOutcomeTruncationMarker"),
        _js_function_source(MESSAGES_JS, "_liveAnchorStrictActivitySceneIdentity"),
        _js_function_source(MESSAGES_JS, "_applyAnchorRegistryOutcomesFromActivityScene"),
    ]


def _message_anchor_settlement_helper_sources() -> list[str]:
    return [
        *_message_anchor_outcome_helper_sources(),
        _js_function_source(MESSAGES_JS, "_messageAnchorUtf8ByteLength"),
        _js_function_source(MESSAGES_JS, "_messageAnchorCompactSceneBytes"),
        _js_function_source(MESSAGES_JS, "_messageAnchorOutcomeBytes"),
        _js_function_source(MESSAGES_JS, "_messageAnchorOutcomeTruncationMarker"),
        _js_function_source(MESSAGES_JS, "_messageAnchorOutcomeSeq"),
        _js_function_source(MESSAGES_JS, "_messageAnchorSceneOutcomeItems"),
        _js_function_source(MESSAGES_JS, "_messageAnchorSceneWithOutcomeItems"),
        _js_function_source(MESSAGES_JS, "_messageAnchorReasonRank"),
        _js_function_source(MESSAGES_JS, "_messageAnchorMergedReason"),
        _js_function_source(MESSAGES_JS, "_messageAnchorMinimalOutcomeScene"),
        _js_function_source(MESSAGES_JS, "_messageAnchorBoundedActivityScene"),
        _js_function_source(MESSAGES_JS, "_anchorSceneHasWorklogWorthyRows"),
        _js_function_source(MESSAGES_JS, "_anchorSceneHasOwnedOutcomes"),
        _js_function_source(MESSAGES_JS, "_attachProjectedAnchorSceneToLastAssistant"),
    ]


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


def test_snapshot_caps_mixed_reconstructed_outcomes_by_shared_count(monkeypatch):
    from api import routes

    stream_id = "stream_mixed_count_cap"
    session_id = "session_mixed_count_cap"
    events = [
        {
            "seq": 1,
            "event": "artifact_reference",
            "event_id": f"{stream_id}:1",
            "stream_id": stream_id,
            "payload": {"kind": "workspace_file", "path": "reports/one.md"},
        },
        {
            "seq": 2,
            "event": "state_saved",
            "event_id": f"{stream_id}:2",
            "stream_id": stream_id,
            "payload": {
                "session_id": session_id,
                "kind": "memory",
                "action": "saved",
                "name": "shared-cap",
            },
        },
        {
            "seq": 3,
            "event": "artifact_reference",
            "event_id": f"{stream_id}:3",
            "stream_id": stream_id,
            "payload": {"kind": "workspace_file", "path": "reports/three.md"},
        },
    ]
    monkeypatch.setattr(routes, "_RUN_JOURNAL_RECONSTRUCTED_OUTCOME_MAX_EVENTS", 2)
    monkeypatch.setattr(routes, "_RUN_JOURNAL_RECONSTRUCTED_OUTCOME_MAX_BYTES", 1_000_000)
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _stream_id: {
            "session_id": session_id,
            "run_id": stream_id,
            "last_seq": 3,
            "last_event_id": f"{stream_id}:3",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, _run_id: {"events": events},
    )

    scene = routes._run_journal_live_snapshot(stream_id)["anchor_activity_scene"]

    assert [event["event_id"] for event in scene["artifacts"]] == [f"{stream_id}:1"]
    assert [event["event_id"] for event in scene["side_effects"]] == [f"{stream_id}:2"]
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


def test_snapshot_caps_reconstructed_outcomes_by_whole_scene_bytes(monkeypatch):
    from api import routes

    stream_id = "stream_near_scene_limit"
    session_id = "session_near_scene_limit"
    events = [
        {
            "seq": 1,
            "event": "token",
            "event_id": f"{stream_id}:1",
            "stream_id": stream_id,
            "payload": {"text": "x" * 650},
        },
        *[
            {
                "seq": seq,
                "event": "artifact_reference",
                "event_id": f"{stream_id}:{seq}",
                "stream_id": stream_id,
                "payload": {
                    "kind": "workspace_file",
                    "path": f"reports/{seq}.md",
                    "source_tool": "tool-" + ("x" * 256),
                },
            }
            for seq in range(2, 8)
        ],
    ]
    monkeypatch.setattr(routes, "_ANCHOR_ACTIVITY_SCENE_MAX_BYTES", 3_500)
    monkeypatch.setattr(routes, "_RUN_JOURNAL_RECONSTRUCTED_OUTCOME_MAX_EVENTS", 100)
    monkeypatch.setattr(routes, "_RUN_JOURNAL_RECONSTRUCTED_OUTCOME_MAX_BYTES", 1_000_000)
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _stream_id: {
            "session_id": session_id,
            "run_id": stream_id,
            "last_seq": 7,
            "last_event_id": f"{stream_id}:7",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, _run_id: {"events": events},
    )

    scene = routes._run_journal_live_snapshot(stream_id)["anchor_activity_scene"]

    assert scene["activity_rows"], "near-limit visible rows should be retained when they fit"
    assert scene["outcomes_truncated"]["reason"] == "scene_bytes"
    assert scene["outcomes_truncated"]["accepted_count"] == len(scene["artifacts"])
    assert len(scene["artifacts"]) < 6
    assert routes._anchor_activity_scene_encoded_size(scene) <= routes._ANCHOR_ACTIVITY_SCENE_MAX_BYTES
    routes._sanitize_anchor_activity_scene(scene)


def test_snapshot_returns_bounded_degraded_scene_when_base_scene_exceeds_limit(monkeypatch):
    from api import routes

    stream_id = "stream_oversized_base_scene"
    session_id = "session_oversized_base_scene"
    events = [
        {
            "seq": 1,
            "event": "token",
            "event_id": f"{stream_id}:1",
            "stream_id": stream_id,
            "payload": {"text": "x" * 5_000},
        },
        {
            "seq": 2,
            "event": "state_saved",
            "event_id": f"{stream_id}:2",
            "stream_id": stream_id,
            "payload": {
                "session_id": session_id,
                "kind": "memory",
                "action": "saved",
                "name": "oversized",
            },
        },
    ]
    monkeypatch.setattr(routes, "_ANCHOR_ACTIVITY_SCENE_MAX_BYTES", 2_000)
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

    assert scene["activity_rows"] == []
    assert scene["artifacts"] == []
    assert scene["side_effects"] == []
    assert scene["outcomes_truncated"]["reason"] == "scene_bytes"
    assert routes._anchor_activity_scene_encoded_size(scene) <= routes._ANCHOR_ACTIVITY_SCENE_MAX_BYTES
    routes._sanitize_anchor_activity_scene(scene)


def test_snapshot_caps_high_volume_outcomes_under_production_scene_limit(monkeypatch):
    from api import routes

    stream_id = "stream_production_outcome_cap"
    session_id = "session_production_outcome_cap"
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
        for seq in range(1, 5_001)
    ]
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _stream_id: {
            "session_id": session_id,
            "run_id": stream_id,
            "last_seq": 5_000,
            "last_event_id": f"{stream_id}:5000",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, _run_id: {"events": events},
    )

    scene = routes._run_journal_live_snapshot(stream_id)["anchor_activity_scene"]

    assert scene["outcomes_truncated"]["reason"] in {"count", "bytes", "scene_bytes"}
    assert scene["outcomes_truncated"]["accepted_count"] == len(scene["side_effects"])
    assert len(scene["side_effects"]) <= routes._RUN_JOURNAL_RECONSTRUCTED_OUTCOME_MAX_EVENTS
    assert routes._anchor_activity_scene_encoded_size(scene) <= routes._ANCHOR_ACTIVITY_SCENE_MAX_BYTES
    routes._sanitize_anchor_activity_scene(scene)


def test_storage_boundary_caps_more_than_512_tiny_outcomes_under_scene_limit():
    from api import routes

    identity = {
        "session_id": "sid-storage-count",
        "stream_id": "stream-storage-count",
        "run_id": "run-storage-count",
    }
    scene = {
        "version": "activity_scene_v1",
        "mode": "compact_worklog",
        "identity": identity,
        "activity_rows": [],
        "artifacts": [],
        "side_effects": [
            {
                "source_event_type": "state_saved",
                "event_id": f"run-storage-count:{seq}",
                "session_id": identity["session_id"],
                "stream_id": identity["stream_id"],
                "run_id": identity["run_id"],
                "seq": seq,
                "payload": {"kind": "memory", "action": "saved", "name": f"state-{seq}"},
            }
            for seq in range(1, 531)
        ],
    }
    assert routes._anchor_activity_scene_encoded_size(scene) < routes._ANCHOR_ACTIVITY_SCENE_MAX_BYTES

    sanitized = routes._sanitize_anchor_activity_scene(scene)

    assert len(sanitized["side_effects"]) == routes._RUN_JOURNAL_RECONSTRUCTED_OUTCOME_MAX_EVENTS
    assert sanitized["side_effects"][-1]["seq"] == 512
    assert sanitized["outcomes_truncated"] == {
        "reason": "count",
        "accepted_count": 512,
        "max_count": 512,
        "accepted_bytes": sum(
            routes._run_journal_outcome_encoded_size(event)
            for event in sanitized["side_effects"]
        ),
        "max_bytes": 128000,
        "max_scene_bytes": 256000,
    }


def test_storage_boundary_caps_more_than_128kb_outcomes_under_scene_limit():
    from api import routes

    identity = {
        "session_id": "sid-storage-bytes",
        "stream_id": "stream-storage-bytes",
        "run_id": "run-storage-bytes",
    }
    scene = {
        "version": "activity_scene_v1",
        "mode": "compact_worklog",
        "identity": identity,
        "activity_rows": [],
        "artifacts": [
            {
                "source_event_type": "artifact_reference",
                "event_id": f"run-storage-bytes:{seq}",
                "session_id": identity["session_id"],
                "stream_id": identity["stream_id"],
                "run_id": identity["run_id"],
                "seq": seq,
                "payload": {
                    "kind": "workspace_file",
                    "path": f"reports/{seq}.md",
                    "blob": "x" * 9000,
                },
            }
            for seq in range(1, 21)
        ],
        "side_effects": [],
    }
    assert routes._anchor_activity_scene_encoded_size(scene) < routes._ANCHOR_ACTIVITY_SCENE_MAX_BYTES

    sanitized = routes._sanitize_anchor_activity_scene(scene)
    retained_bytes = sum(
        routes._run_journal_outcome_encoded_size(event)
        for event in sanitized["artifacts"]
    )

    assert sanitized["outcomes_truncated"]["reason"] == "bytes"
    assert sanitized["outcomes_truncated"]["accepted_count"] == len(sanitized["artifacts"])
    assert sanitized["outcomes_truncated"]["accepted_bytes"] == retained_bytes
    assert retained_bytes <= routes._RUN_JOURNAL_RECONSTRUCTED_OUTCOME_MAX_BYTES
    assert len(sanitized["artifacts"]) < 20


def test_storage_boundary_canonicalizes_malformed_outcome_marker():
    from api import routes

    scene = {
        "version": "activity_scene_v1",
        "mode": "compact_worklog",
        "identity": {
            "session_id": "sid-storage-marker",
            "stream_id": "stream-storage-marker",
            "run_id": "run-storage-marker",
        },
        "activity_rows": [],
        "artifacts": [],
        "side_effects": [],
        "outcomes_truncated": {
            "reason": "scene_bytes",
            "accepted_count": 0,
            "accepted_bytes": 0,
            "max_count": "bad",
            "max_bytes": 1,
            "max_scene_bytes": 1,
            "extra": "must be dropped",
        },
    }

    sanitized = routes._sanitize_anchor_activity_scene(scene)

    assert sanitized["outcomes_truncated"] == {
        "reason": "scene_bytes",
        "accepted_count": 0,
        "max_count": 512,
        "accepted_bytes": 0,
        "max_bytes": 128000,
        "max_scene_bytes": 256000,
    }


def test_storage_boundary_rejects_oversized_non_outcome_scene(monkeypatch):
    from api import routes

    scene = {
        "version": "activity_scene_v1",
        "mode": "compact_worklog",
        "activity_rows": [
            {
                "row_id": "oversized-prose",
                "role": "prose",
                "text": "x" * 5_000,
            }
        ],
        "artifacts": [],
        "side_effects": [],
    }
    monkeypatch.setattr(routes, "_ANCHOR_ACTIVITY_SCENE_MAX_BYTES", 2_000)

    with pytest.raises(ValueError, match="scene payload is too large"):
        routes._sanitize_anchor_activity_scene(scene)


@pytest.mark.skipif(not NODE, reason="node is required for recovery identity coverage")
def test_backend_stable_scene_identity_passes_frontend_recovery_validation(monkeypatch):
    scene = _stable_outcome_scene(monkeypatch)
    functions = "\n".join(
        _session_anchor_recovery_helper_sources()
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
            _js_function_source(SESSIONS_JS, "_inflightHasVisibleLiveState"),
            *_session_anchor_merge_helper_sources(),
            *_message_anchor_outcome_helper_sources(),
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
  outcomes_truncated:{{reason:'count',accepted_count:2,max_count:2,accepted_bytes:128,max_bytes:256,max_scene_bytes:1024}},
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
const projected=api.projectAssistantTurnAnchorActivityScene(registry,{{mode:'compact_worklog'}});
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
  projected,
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
    assert data["visibleScene"]["outcomes_truncated"]["reason"] == "count"
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
    assert data["projected"]["outcomes_truncated"]["reason"] == "count"
    assert "run_id:_outcomeSceneIdentity.runId" in MESSAGES_JS
    assert "&&!_anchorActivitySceneHasRecoveryState(INFLIGHT[sid].anchorActivityScene)" in SESSIONS_JS.replace("\n", "")


@pytest.mark.skipif(not NODE, reason="node is required for Anchor hydration coverage")
def test_outcome_replay_rejects_per_envelope_identity_mismatches():
    functions = "\n".join(
        _message_anchor_outcome_helper_sources()
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
const missingSceneSessionRegistry=api.createAssistantTurnAnchorRegistry({{session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-current'}});
const missingSceneSession={{
  ...scene,
  identity:{{run_id:'stable-run-1',stream_id:'stream-current'}},
  artifacts:[
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:20',session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-current',seq:20,payload:{{kind:'workspace_file',path:'missing-scene-session.md'}}}},
  ],
  side_effects:[],
}};
assert.strictEqual(
  sandbox._applyAnchorRegistryOutcomesFromActivityScene(
    api,
    missingSceneSessionRegistry,
    missingSceneSession,
    {{session_id:'sid-1',stream_id:'stream-current',run_id:'stable-run-1'}}
  ),
  false
);
assert.strictEqual(missingSceneSessionRegistry.anchor.artifacts.length,0);
const missingSceneStreamRegistry=api.createAssistantTurnAnchorRegistry({{session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-current'}});
const missingSceneStream={{
  ...scene,
  identity:{{session_id:'sid-1',run_id:'stable-run-1'}},
  artifacts:[
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:22',session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-current',seq:22,payload:{{kind:'workspace_file',path:'missing-scene-stream.md'}}}},
  ],
  side_effects:[],
}};
assert.strictEqual(
  sandbox._applyAnchorRegistryOutcomesFromActivityScene(
    api,
    missingSceneStreamRegistry,
    missingSceneStream,
    {{session_id:'sid-1',stream_id:'stream-current',run_id:'stable-run-1'}}
  ),
  false
);
assert.strictEqual(missingSceneStreamRegistry.anchor.artifacts.length,0);
const missingSceneRunRegistry=api.createAssistantTurnAnchorRegistry({{session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-current'}});
const missingSceneRun={{
  ...scene,
  identity:{{session_id:'sid-1',stream_id:'stream-current'}},
  artifacts:[
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:23',session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-current',seq:23,payload:{{kind:'workspace_file',path:'missing-scene-run.md'}}}},
  ],
  side_effects:[],
}};
assert.strictEqual(
  sandbox._applyAnchorRegistryOutcomesFromActivityScene(
    api,
    missingSceneRunRegistry,
    missingSceneRun,
    {{session_id:'sid-1',stream_id:'stream-current',run_id:'stable-run-1'}}
  ),
  false
);
assert.strictEqual(missingSceneRunRegistry.anchor.artifacts.length,0);
const foreignSceneSessionRegistry=api.createAssistantTurnAnchorRegistry({{session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-current'}});
const foreignSceneSession={{
  ...scene,
  identity:{{session_id:'sid-foreign',run_id:'stable-run-1',stream_id:'stream-current'}},
  artifacts:[
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:21',session_id:'sid-foreign',run_id:'stable-run-1',stream_id:'stream-current',seq:21,payload:{{kind:'workspace_file',path:'foreign-scene-session.md'}}}},
  ],
  side_effects:[],
}};
assert.strictEqual(
  sandbox._applyAnchorRegistryOutcomesFromActivityScene(
    api,
    foreignSceneSessionRegistry,
    foreignSceneSession,
    {{session_id:'sid-1',stream_id:'stream-current',run_id:'stable-run-1'}}
  ),
  false
);
assert.strictEqual(foreignSceneSessionRegistry.anchor.artifacts.length,0);
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
        _message_anchor_settlement_helper_sources()
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
registry.anchor.outcomes_truncated={{reason:'scene_bytes',accepted_count:2,max_count:512,accepted_bytes:256,max_bytes:128000,max_scene_bytes:256000}};
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
    assert data["projectedScene"]["outcomes_truncated"]["reason"] == "scene_bytes"
    assert data["promoted"] is False
    assert data["persisted"]["messageIndex"] == 1
    assert data["messageScene"] == data["reloadScene"]
    assert data["messageScene"]["outcomes_truncated"]["accepted_count"] == 2
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


@pytest.mark.skipif(not NODE, reason="node is required for settled Anchor scene coverage")
def test_marker_only_outcome_scene_survives_frontend_settlement():
    functions = "\n".join(
        _message_anchor_settlement_helper_sources()
    )
    script = f"""
const assert=require('assert');
const vm=require('vm');
const sandbox={{window:{{isFinalAnswerOnlyMode:()=>false}},console}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(functions)},sandbox,{{filename:'marker_only_settlement_helpers.js'}});
vm.runInContext(`
const marker={{reason:'scene_bytes',accepted_count:0,max_count:512,accepted_bytes:0,max_bytes:128000,max_scene_bytes:256000}};
var projectedScene={{
  version:'activity_scene_v1',
  mode:'compact_worklog',
  identity:{{session_id:'sid-marker',stream_id:'stream-marker',run_id:'run-marker'}},
  activity_rows:[],
  artifacts:[],
  side_effects:[],
  outcomes_truncated:marker,
}};
var _anchorRegistry={{}};
var streamId='stream-marker';
var persisted=null;
function _projectLiveAnchorActivityScene(){{ return projectedScene; }}
function _completeSettledAnchorSceneForTurn(messages,lastAsstIndex,scene){{
  return {{
    ...scene,
    final_answer:'done',
    final_message_ref:'message-marker',
    activity_rows:Array.isArray(scene.activity_rows)?scene.activity_rows:[],
  }};
}}
function _persistSettledAnchorScene(message,scene,messageIndex){{ persisted={{message,scene,messageIndex}}; }}
const messages=[
  {{role:'user',content:'question'}},
  {{role:'assistant',content:'done'}},
];
const renderedWorklog=_attachProjectedAnchorSceneToLastAssistant(messages);
globalThis.result={{
  renderedWorklog,
  persisted,
  attached:messages[1]._anchor_activity_scene,
}};
`, sandbox, {{filename:'marker_only_settlement_probe.js'}});
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
    assert data["renderedWorklog"] is False
    assert data["persisted"]["messageIndex"] == 1
    assert data["attached"]["activity_rows"] == []
    assert data["attached"]["artifacts"] == []
    assert data["attached"]["side_effects"] == []
    assert data["attached"]["outcomes_truncated"] == {
        "reason": "scene_bytes",
        "accepted_count": 0,
        "max_count": 512,
        "accepted_bytes": 0,
        "max_bytes": 128000,
        "max_scene_bytes": 256000,
    }


@pytest.mark.skipif(not NODE, reason="node is required for settled Anchor scene coverage")
def test_frontend_settlement_caps_tiny_outcomes_under_scene_limit():
    functions = "\n".join(
        _message_anchor_settlement_helper_sources()
    )
    script = f"""
const assert=require('assert');
const vm=require('vm');
const sandbox={{window:{{isFinalAnswerOnlyMode:()=>false}},console,Buffer}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(functions)},sandbox,{{filename:'settlement_count_helpers.js'}});
vm.runInContext(`
const sideEffects=Array.from({{length:520}},(_,idx)=>{{
  const seq=idx+1;
    return {{
      source_event_type:'state_saved',
      event_id:'run-settle-count:'+seq,
      session_id:'sid-settle-count',
      stream_id:'stream-settle-count',
      run_id:'run-settle-count',
      seq,
      payload:{{kind:'memory',action:'saved',name:'state-'+seq}},
    }};
  }});
var projectedScene={{
  version:'activity_scene_v1',
  mode:'compact_worklog',
  identity:{{session_id:'sid-settle-count',stream_id:'stream-settle-count',run_id:'run-settle-count'}},
  activity_rows:[],
  artifacts:[],
  side_effects:sideEffects,
}};
var _anchorRegistry={{}};
var streamId='stream-settle-count';
var persisted=null;
function _projectLiveAnchorActivityScene(){{ return projectedScene; }}
function _completeSettledAnchorSceneForTurn(messages,lastAsstIndex,scene){{ return scene; }}
function _persistSettledAnchorScene(message,scene,messageIndex){{ persisted={{message,scene,messageIndex}}; }}
const messages=[{{role:'assistant',content:'done'}}];
const renderedWorklog=_attachProjectedAnchorSceneToLastAssistant(messages);
globalThis.result={{
  renderedWorklog,
  persisted,
  attached:messages[0]._anchor_activity_scene,
}};
`, sandbox, {{filename:'settlement_count_probe.js'}});
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
    attached = data["attached"]
    assert data["renderedWorklog"] is False
    assert data["persisted"]["messageIndex"] == 0
    assert len(attached["side_effects"]) == 512
    assert attached["side_effects"][0]["seq"] == 1
    assert attached["side_effects"][-1]["seq"] == 512
    assert attached["outcomes_truncated"]["reason"] == "count"
    assert attached["outcomes_truncated"]["accepted_count"] == 512


@pytest.mark.skipif(not NODE, reason="node is required for recovery identity coverage")
def test_outcome_merge_fails_closed_on_missing_or_wrong_scene_identity():
    functions = "\n".join(
        _session_anchor_merge_helper_sources()
    )
    script = f"""
const assert=require('assert');
const vm=require('vm');
const sandbox={{}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(functions)},sandbox,{{filename:'merge_identity_helpers.js'}});
const ownedOutcomeOnly={{
  version:'activity_scene_v1',
  identity:{{session_id:'sid-1',stream_id:'stream-1',run_id:'stable-run-1'}},
  activity_rows:[],
  artifacts:[
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:1',session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-1',seq:1,payload:{{kind:'workspace_file',path:'owned.md'}}}},
  ],
  side_effects:[],
}};
assert.strictEqual(sandbox._anchorActivitySceneHasRecoveryState(ownedOutcomeOnly),true);
const wrongStreamOutcomeOnly={{
  ...ownedOutcomeOnly,
  artifacts:[
    {{source_event_type:'artifact_reference',event_id:'stable-run-1:2',session_id:'sid-1',run_id:'stable-run-1',stream_id:'stream-stale',seq:2,payload:{{kind:'workspace_file',path:'stale.md'}}}},
  ],
}};
assert.strictEqual(sandbox._anchorActivitySceneHasRecoveryState(wrongStreamOutcomeOnly),false);
const missingSceneSessionOutcomeOnly={{
  ...ownedOutcomeOnly,
  identity:{{stream_id:'stream-1',run_id:'stable-run-1'}},
}};
assert.strictEqual(sandbox._anchorActivitySceneHasRecoveryState(missingSceneSessionOutcomeOnly),false);
const missingSceneStreamOutcomeOnly={{
  ...ownedOutcomeOnly,
  identity:{{session_id:'sid-1',run_id:'stable-run-1'}},
}};
assert.strictEqual(sandbox._anchorActivitySceneHasRecoveryState(missingSceneStreamOutcomeOnly),false);
const missingSceneRunOutcomeOnly={{
  ...ownedOutcomeOnly,
  identity:{{session_id:'sid-1',stream_id:'stream-1'}},
}};
assert.strictEqual(sandbox._anchorActivitySceneHasRecoveryState(missingSceneRunOutcomeOnly),false);
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
const missingCachedStream={{
  ...missingCached,
  anchorActivityScene:{{
    ...missingCached.anchorActivityScene,
    identity:{{session_id:'sid-1',run_id:'stable-run-1'}},
  }},
}};
assert.strictEqual(sandbox._mergeServerLiveSnapshotOutcomesIntoInflight(missingCachedStream,server,'stream-1'),false);
assert.strictEqual(missingCachedStream.lastRunJournalSeq,5);
assert.deepStrictEqual(missingCachedStream.anchorActivityScene.artifacts,[]);
const missingCachedRun={{
  ...missingCached,
  anchorActivityScene:{{
    ...missingCached.anchorActivityScene,
    identity:{{session_id:'sid-1',stream_id:'stream-1'}},
  }},
}};
assert.strictEqual(sandbox._mergeServerLiveSnapshotOutcomesIntoInflight(missingCachedRun,server,'stream-1'),false);
assert.strictEqual(missingCachedRun.lastRunJournalSeq,5);
assert.deepStrictEqual(missingCachedRun.anchorActivityScene.artifacts,[]);
const missingJournalStream=sandbox._serverLiveSnapshotInflight({{
  stream_id:'stream-1',
  last_seq:99,
  anchor_activity_scene:{{...journalScene,identity:{{session_id:'sid-1',run_id:'stable-run-1'}}}},
}},[]);
const missingJournalRun=sandbox._serverLiveSnapshotInflight({{
  stream_id:'stream-1',
  last_seq:99,
  anchor_activity_scene:{{...journalScene,identity:{{session_id:'sid-1',stream_id:'stream-1'}}}},
}},[]);
const completeCachedForMissingJournal={{
  ...missingCached,
  anchorActivityScene:{{
    ...missingCached.anchorActivityScene,
    identity:{{session_id:'sid-1',stream_id:'stream-1',run_id:'stable-run-1'}},
  }},
}};
assert.strictEqual(
  sandbox._mergeServerLiveSnapshotOutcomesIntoInflight(completeCachedForMissingJournal,missingJournalStream,'stream-1'),
  false
);
assert.strictEqual(completeCachedForMissingJournal.lastRunJournalSeq,5);
assert.strictEqual(
  sandbox._mergeServerLiveSnapshotOutcomesIntoInflight(completeCachedForMissingJournal,missingJournalRun,'stream-1'),
  false
);
assert.strictEqual(completeCachedForMissingJournal.lastRunJournalSeq,5);
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


@pytest.mark.skipif(not NODE, reason="node is required for recovery budget coverage")
def test_outcome_merge_rebudgets_cached_and_journal_outcomes_under_scene_limit():
    functions = "\n".join(
        _session_anchor_merge_helper_sources()
    )
    script = f"""
const assert=require('assert');
const vm=require('vm');
const sandbox={{Buffer}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(functions)},sandbox,{{filename:'merge_budget_helpers.js'}});
const identity={{session_id:'sid-1',stream_id:'stream-1',run_id:'stable-run-1'}};
function artifact(seq,path,size){{
  return {{
    source_event_type:'artifact_reference',
    event_id:`stable-run-1:${{seq}}`,
    session_id:'sid-1',
    stream_id:'stream-1',
    run_id:'stable-run-1',
    seq,
    payload:{{kind:'workspace_file',path,blob:'a'.repeat(size)}},
  }};
}}
function sideEffect(seq,name,size){{
  return {{
    source_event_type:'state_saved',
    event_id:`stable-run-1:${{seq}}`,
    session_id:'sid-1',
    stream_id:'stream-1',
    run_id:'stable-run-1',
    seq,
    payload:{{kind:'memory',action:'saved',name,blob:'s'.repeat(size)}},
  }};
}}
const cached={{
  streamId:'stream-1',
  lastRunJournalSeq:8,
  lastAssistantText:'local visible row owner',
  messages:[{{role:'assistant',content:'local visible row owner'}}],
  anchorActivityScene:{{
    version:'activity_scene_v1',
    mode:'compact_worklog',
    identity,
    activity_rows:[{{row_id:'cached-prose',role:'prose',text:'r'.repeat(150000)}}],
    artifacts:[artifact(101,'cached-artifact.md',30000)],
    side_effects:[sideEffect(102,'cached-state',30000)],
    outcomes_truncated:{{reason:'count',accepted_count:2,max_count:512,accepted_bytes:60000,max_bytes:128000,max_scene_bytes:256000}},
  }},
}};
const journalScene={{
  version:'activity_scene_v1',
  mode:'compact_worklog',
  identity,
  activity_rows:[],
  artifacts:[artifact(201,'journal-artifact.md',30000)],
  side_effects:[sideEffect(202,'journal-state',30000)],
  outcomes_truncated:{{reason:'bytes',accepted_count:2,max_count:512,accepted_bytes:60000,max_bytes:128000,max_scene_bytes:256000}},
}};
const merged=sandbox._mergeServerLiveSnapshotOutcomesIntoInflight(
  cached,
  {{streamId:'stream-1',lastRunJournalSeq:12,anchorActivityScene:journalScene}},
  'stream-1'
);
assert.strictEqual(merged,true);
const scene=cached.anchorActivityScene;
const retained=[...scene.artifacts,...scene.side_effects];
const marker=scene.outcomes_truncated;
assert.strictEqual(sandbox._sessionAnchorCompactSceneBytes(scene)<=256000,true);
assert.strictEqual(marker.reason,'scene_bytes');
assert.strictEqual(marker.accepted_count,retained.length);
assert.strictEqual(marker.accepted_bytes,sandbox._sessionAnchorOutcomeBytes(retained));
assert.strictEqual(marker.max_count,512);
assert.strictEqual(marker.max_bytes,128000);
assert.strictEqual(marker.max_scene_bytes,256000);
assert.strictEqual(retained.length<4,true);
assert.strictEqual(retained.length>=1,true);
assert.strictEqual(cached.lastRunJournalSeq,12);
console.log(JSON.stringify({{
  retained:retained.length,
  marker,
  bytes:sandbox._sessionAnchorCompactSceneBytes(scene),
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
    assert data["retained"] == data["marker"]["accepted_count"]
    assert data["bytes"] <= 256000


@pytest.mark.skipif(not NODE, reason="node is required for recovery budget coverage")
def test_outcome_merge_caps_tiny_outcomes_and_retains_mixed_sequence_order():
    functions = "\n".join(
        _session_anchor_merge_helper_sources()
    )
    script = f"""
const assert=require('assert');
const vm=require('vm');
const sandbox={{Buffer}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(functions)},sandbox,{{filename:'merge_count_order_helpers.js'}});
const identity={{session_id:'sid-order',stream_id:'stream-order',run_id:'run-order'}};
function artifact(seq){{
  return {{
    source_event_type:'artifact_reference',
    event_id:`run-order:${{seq}}`,
    session_id:identity.session_id,
    stream_id:identity.stream_id,
    run_id:identity.run_id,
    seq,
    payload:{{kind:'workspace_file',path:`reports/${{seq}}.md`}},
  }};
}}
function sideEffect(seq){{
  return {{
    source_event_type:'state_saved',
    event_id:`run-order:${{seq}}`,
    session_id:identity.session_id,
    stream_id:identity.stream_id,
    run_id:identity.run_id,
    seq,
    payload:{{kind:'memory',action:'saved',name:`state-${{seq}}`}},
  }};
}}
const journalScene={{
  version:'activity_scene_v1',
  mode:'compact_worklog',
  identity,
  activity_rows:[],
  artifacts:Array.from({{length:260}},(_,idx)=>artifact(idx*2+1)),
  side_effects:[],
}};
const cached={{
  streamId:'stream-order',
  lastRunJournalSeq:1,
  messages:[{{role:'assistant',content:'local'}}],
  anchorActivityScene:{{
    version:'activity_scene_v1',
    mode:'compact_worklog',
    identity,
    activity_rows:[{{row_id:'cached-prose',role:'prose',text:'local'}}],
    artifacts:[],
    side_effects:Array.from({{length:260}},(_,idx)=>sideEffect(idx*2+2)),
  }},
}};
const merged=sandbox._mergeServerLiveSnapshotOutcomesIntoInflight(
  cached,
  {{streamId:'stream-order',lastRunJournalSeq:520,anchorActivityScene:journalScene}},
  'stream-order'
);
assert.strictEqual(merged,true);
const scene=cached.anchorActivityScene;
const retained=[...scene.artifacts,...scene.side_effects];
const retainedSeqs=retained.map(event=>event.seq).sort((a,b)=>a-b);
assert.strictEqual(retained.length,512);
assert.strictEqual(retainedSeqs[0],1);
assert.strictEqual(retainedSeqs[retainedSeqs.length-1],512);
assert.deepStrictEqual(retainedSeqs,Array.from({{length:512}},(_,idx)=>idx+1));
assert.strictEqual(scene.artifacts.length,256);
assert.strictEqual(scene.side_effects.length,256);
assert.strictEqual(scene.outcomes_truncated.reason,'count');
assert.strictEqual(scene.outcomes_truncated.accepted_count,512);
assert.strictEqual(scene.outcomes_truncated.accepted_bytes,sandbox._sessionAnchorOutcomeBytes(retained));
console.log(JSON.stringify({{
  marker:scene.outcomes_truncated,
  artifactCount:scene.artifacts.length,
  sideEffectCount:scene.side_effects.length,
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
    assert data["marker"]["reason"] == "count"
    assert data["artifactCount"] == 256
    assert data["sideEffectCount"] == 256


@pytest.mark.skipif(not NODE, reason="node is required for Anchor projection coverage")
def test_anchor_projection_caps_outcome_bytes_under_scene_limit():
    script = f"""
const fs=require('fs');
const assert=require('assert');
const vm=require('vm');
const sandbox={{window:{{}},Buffer}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(ANCHORS_JS))},'utf8'),sandbox,{{filename:'assistant_turn_anchors.js'}});
const api=sandbox.window.HermesAssistantTurnAnchors;
const registry=api.createAssistantTurnAnchorRegistry({{session_id:'sid-projection',stream_id:'stream-projection',run_id:'run-projection'}});
for(let seq=1;seq<=520;seq+=1){{
  api.applyAssistantTurnAnchorSourceEvent(
    registry,
    {{
      event:'state_saved',
      event_id:`run-projection:${{seq}}`,
      session_id:'sid-projection',
      stream_id:'stream-projection',
      run_id:'run-projection',
      seq,
      payload:{{kind:'memory',action:'saved',name:`state-${{seq}}`}},
    }},
    {{session_id:'sid-projection',stream_id:'stream-projection',run_id:'run-projection'}}
  );
}}
const projected=api.projectAssistantTurnAnchorActivityScene(registry,{{mode:'compact_worklog'}});
assert.strictEqual(projected.side_effects.length<520,true);
assert.strictEqual(projected.side_effects[0].seq,1);
assert.strictEqual(projected.outcomes_truncated.reason,'bytes');
assert.strictEqual(projected.outcomes_truncated.accepted_count,projected.side_effects.length);
assert.strictEqual(projected.outcomes_truncated.accepted_bytes<=128000,true);
assert.strictEqual(Buffer.byteLength(JSON.stringify(projected),'utf8')<=256000,true);
console.log(JSON.stringify({{
  sideEffects:projected.side_effects.length,
  marker:projected.outcomes_truncated,
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
    assert data["sideEffects"] < 520
    assert data["marker"]["reason"] == "bytes"
    assert data["marker"]["accepted_count"] == data["sideEffects"]


@pytest.mark.skipif(not NODE, reason="node is required for Anchor registry coverage")
def test_stable_recovered_scene_replaces_transport_registry_before_outcomes():
    functions = "\n".join(
        [
            _js_function_source(MESSAGES_JS, "_liveAnchorRegistryIdentity"),
            _js_function_source(MESSAGES_JS, "_liveAnchorRegistryForActivityScene"),
            *_message_anchor_outcome_helper_sources(),
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
