from __future__ import annotations

from api import models, routes
from api.run_journal import RunJournalWriter


def _snapshot(tmp_path, monkeypatch, payloads):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)

    session_id = "state-saved-recovery"
    stream_id = "stream-state-saved-recovery"
    writer = RunJournalWriter(session_id, stream_id, session_dir=session_dir)
    for payload in payloads:
        writer.append_sse_event("state_saved", payload)
    snapshot = routes._run_journal_live_snapshot(stream_id)
    assert snapshot is not None
    return session_id, stream_id, snapshot


def test_state_saved_replays_from_real_run_journal_into_anchor_side_effects(
    tmp_path, monkeypatch
):
    session_id, stream_id, snapshot = _snapshot(
        tmp_path,
        monkeypatch,
        [{
            "session_id": "state-saved-recovery",
            "kind": "skill",
            "action": "updated",
            "name": "release-notes",
            "body": "must not survive replay",
            "path": "/private/skill/path",
        }],
    )

    scene = snapshot["anchor_activity_scene"]
    assert all(
        row.get("source_event_type") != "state_saved"
        for row in scene["activity_rows"]
        if isinstance(row, dict)
    )
    assert scene["side_effects"] == [{
        "source_event_type": "state_saved",
        "event_id": f"{stream_id}:1",
        "session_id": session_id,
        "run_id": stream_id,
        "stream_id": stream_id,
        "seq": 1,
        "payload": {
            "session_id": session_id,
            "kind": "skill",
            "action": "updated",
            "name": "release-notes",
        },
    }]


def test_state_saved_replay_rejects_foreign_and_nonproducer_payloads(
    tmp_path, monkeypatch
):
    session_id, stream_id, snapshot = _snapshot(
        tmp_path,
        monkeypatch,
        [
            {
                "session_id": "foreign-session",
                "kind": "skill",
                "action": "updated",
                "name": "foreign",
            },
            {
                "session_id": "state-saved-recovery",
                "kind": "memory",
                "action": "updated",
            },
            {
                "session_id": "state-saved-recovery",
                "kind": "skill",
                "action": "deleted",
                "name": "deleted-skill",
            },
            {
                "session_id": "state-saved-recovery",
                "kind": "unknown",
                "action": "saved",
            },
            {
                "session_id": "state-saved-recovery",
                "kind": "memory",
                "action": "saved",
                "secret": "do-not-project",
            },
        ],
    )

    assert snapshot["anchor_activity_scene"]["side_effects"] == [{
        "source_event_type": "state_saved",
        "event_id": f"{stream_id}:5",
        "session_id": session_id,
        "run_id": stream_id,
        "stream_id": stream_id,
        "seq": 5,
        "payload": {
            "session_id": session_id,
            "kind": "memory",
            "action": "saved",
        },
    }]




def test_state_saved_canonicalizer_requires_exact_journal_identity():
    session_id = "state-saved-recovery"
    stream_id = "stream-state-saved-recovery"
    base = {
        "seq": 1,
        "event": "state_saved",
        "event_id": f"{stream_id}:1",
        "run_id": stream_id,
        "session_id": session_id,
        "payload": {
            "session_id": session_id,
            "kind": "skill",
            "action": "updated",
            "name": "release-notes",
        },
    }

    accepted = routes._run_journal_state_saved_side_effect(
        base,
        session_id=session_id,
        stream_id=stream_id,
        run_id=stream_id,
    )
    assert accepted is not None

    malformed = [
        {**base, "seq": True},
        {**base, "session_id": "foreign-session"},
        {**base, "run_id": "foreign-run", "event_id": "foreign-run:1"},
        {**base, "event_id": f"{stream_id}:2"},
        {
            **base,
            "payload": {
                **base["payload"],
                "session_id": "foreign-session",
            },
        },
        {
            **base,
            "payload": {
                **base["payload"],
                "name": "x" * (routes._RUN_JOURNAL_STATE_SAVED_MAX_NAME_BYTES + 1),
            },
        },
    ]
    for event in malformed:
        assert routes._run_journal_state_saved_side_effect(
            event,
            session_id=session_id,
            stream_id=stream_id,
            run_id=stream_id,
        ) is None
def test_state_saved_replay_is_count_bounded(tmp_path, monkeypatch):
    payloads = [
        {
            "session_id": "state-saved-recovery",
            "kind": "skill",
            "action": "updated",
            "name": f"skill-{index:02d}",
        }
        for index in range(routes._RUN_JOURNAL_STATE_SAVED_MAX_EVENTS + 8)
    ]
    _session_id, _stream_id, snapshot = _snapshot(tmp_path, monkeypatch, payloads)

    effects = snapshot["anchor_activity_scene"]["side_effects"]
    assert len(effects) == routes._RUN_JOURNAL_STATE_SAVED_MAX_EVENTS
    assert [effect["payload"]["name"] for effect in effects] == [
        f"skill-{index:02d}"
        for index in range(routes._RUN_JOURNAL_STATE_SAVED_MAX_EVENTS)
    ]


def test_state_saved_side_effect_survives_runtime_snapshot_transport_projection(
    tmp_path, monkeypatch
):
    _session_id, _stream_id, snapshot = _snapshot(
        tmp_path,
        monkeypatch,
        [{
            "session_id": "state-saved-recovery",
            "kind": "memory",
            "action": "saved",
        }],
    )

    projected = routes._runtime_journal_snapshot_for_session_payload(snapshot)
    assert projected["anchor_activity_scene"]["side_effects"] == (
        snapshot["anchor_activity_scene"]["side_effects"]
    )
