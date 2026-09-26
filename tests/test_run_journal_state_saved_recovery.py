from __future__ import annotations

import copy

import pytest

import api.models as models
import api.routes as routes
from api.run_journal import RunJournalWriter


@pytest.fixture(autouse=True)
def _isolated_session_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    yield


def test_live_snapshot_recovers_real_state_saved_events():
    session_id = "state_saved_recovery_session"
    run_id = "state_saved_recovery_run"
    writer = RunJournalWriter(session_id, run_id)

    first = writer.append_sse_event(
        "state_saved",
        {
            "session_id": session_id,
            "kind": "memory",
            "action": "saved",
            "private_body": "must not enter the anchor scene",
        },
    )
    second = writer.append_sse_event(
        "state_saved",
        {
            "session_id": session_id,
            "kind": "skill",
            "action": "updated",
            "name": "release-notes",
            "private_body": "must not enter the anchor scene",
        },
    )

    snapshot = routes._run_journal_live_snapshot(run_id)
    assert snapshot is not None
    scene = snapshot["anchor_activity_scene"]

    assert scene["side_effects"] == [
        {
            "source_event_type": "state_saved",
            "event_id": first["event_id"],
            "session_id": session_id,
            "run_id": run_id,
            "stream_id": run_id,
            "seq": first["seq"],
            "created_at": first["created_at"],
            "payload": {
                "session_id": session_id,
                "kind": "memory",
                "action": "saved",
            },
        },
        {
            "source_event_type": "state_saved",
            "event_id": second["event_id"],
            "session_id": session_id,
            "run_id": run_id,
            "stream_id": run_id,
            "seq": second["seq"],
            "created_at": second["created_at"],
            "payload": {
                "session_id": session_id,
                "kind": "skill",
                "action": "updated",
                "name": "release-notes",
            },
        },
    ]


@pytest.mark.parametrize(
    ("mutation",),
    [
        (lambda event: event.update({"session_id": "foreign_session"}),),
        (lambda event: event.update({"run_id": "foreign_run"}),),
        (lambda event: event.update({"event_id": "foreign_run:1"}),),
        (lambda event: event.update({"event_id": "state_saved_recovery_run:99"}),),
        (lambda event: event.update({"seq": 1.5}),),
        (lambda event: event.update({"seq": True}),),
        (lambda event: event.update({"payload": "not-an-object"}),),
        (
            lambda event: event.update(
                {
                    "payload": {
                        "session_id": "foreign_session",
                        "kind": "memory",
                        "action": "saved",
                    }
                }
            ),
        ),
        (
            lambda event: event.update(
                {
                    "payload": {
                        "session_id": "state_saved_recovery_session",
                        "kind": "",
                        "action": "saved",
                    }
                }
            ),
        ),
        (
            lambda event: event.update(
                {
                    "payload": {
                        "session_id": "state_saved_recovery_session",
                        "kind": "memory",
                        "action": "",
                    }
                }
            ),
        ),
    ],
)
def test_state_saved_canonicalizer_rejects_untrusted_identity_or_shape(mutation):
    session_id = "state_saved_recovery_session"
    run_id = "state_saved_recovery_run"
    event = {
        "version": 1,
        "event_id": f"{run_id}:1",
        "seq": 1,
        "run_id": run_id,
        "session_id": session_id,
        "event": "state_saved",
        "type": "state_saved",
        "created_at": 123.5,
        "payload": {
            "session_id": session_id,
            "kind": "memory",
            "action": "saved",
        },
    }
    mutation(event)

    assert routes._run_journal_state_saved_side_effect(
        event,
        session_id=session_id,
        run_id=run_id,
        stream_id=run_id,
    ) is None


def test_state_saved_canonicalizer_dedupes_by_authoritative_event_id(monkeypatch):
    session_id = "state_saved_dedupe_session"
    run_id = "state_saved_dedupe_run"
    writer = RunJournalWriter(session_id, run_id)
    event = writer.append_sse_event(
        "state_saved",
        {
            "session_id": session_id,
            "kind": "skill",
            "action": "updated",
            "name": "same",
        },
    )

    duplicate = copy.deepcopy(event)
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, _run_id: {"events": [event, duplicate]},
    )

    snapshot = routes._run_journal_live_snapshot(run_id)
    assert snapshot is not None
    assert [item["event_id"] for item in snapshot["anchor_activity_scene"]["side_effects"]] == [
        event["event_id"]
    ]


def test_state_saved_recovery_caps_event_count_and_marks_truncation():
    session_id = "state_saved_count_budget_session"
    run_id = "state_saved_count_budget_run"
    writer = RunJournalWriter(session_id, run_id)

    for index in range(routes._RUN_JOURNAL_STATE_SAVED_MAX_EVENTS + 1):
        writer.append_sse_event(
            "state_saved",
            {
                "session_id": session_id,
                "kind": "skill",
                "action": "updated",
                "name": f"skill-{index}",
            },
        )

    scene = routes._run_journal_live_snapshot(run_id)["anchor_activity_scene"]

    assert len(scene["side_effects"]) == routes._RUN_JOURNAL_STATE_SAVED_MAX_EVENTS
    assert scene["side_effects_truncated"] is True
    assert scene["side_effects"][-1]["payload"]["name"] == (
        f"skill-{routes._RUN_JOURNAL_STATE_SAVED_MAX_EVENTS - 1}"
    )


def test_state_saved_recovery_caps_encoded_bytes_and_marks_truncation():
    session_id = "state_saved_byte_budget_session"
    run_id = "state_saved_byte_budget_run"
    writer = RunJournalWriter(session_id, run_id)

    for index in range(routes._RUN_JOURNAL_STATE_SAVED_MAX_EVENTS):
        writer.append_sse_event(
            "state_saved",
            {
                "session_id": session_id,
                "kind": "skill",
                "action": "updated",
                "name": f"{index:03d}-" + ("x" * 500),
            },
        )

    scene = routes._run_journal_live_snapshot(run_id)["anchor_activity_scene"]
    side_effects = scene["side_effects"]
    encoded_bytes = sum(
        len(
            routes.json.dumps(
                item,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        for item in side_effects
    )

    assert len(side_effects) < routes._RUN_JOURNAL_STATE_SAVED_MAX_EVENTS
    assert encoded_bytes <= routes._RUN_JOURNAL_STATE_SAVED_MAX_BYTES
    assert scene["side_effects_truncated"] is True
