from __future__ import annotations

import json
from collections import OrderedDict

import pytest


@pytest.fixture
def temp_session_dir(tmp_path, monkeypatch):
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    return session_dir


def test_exact_stable_replay_guard_preserves_distinct_and_unstable_rows():
    from api.models import _deduplicate_exact_stable_messages

    persisted = {
        "role": "assistant",
        "content": "",
        "id": "assistant-event-1",
        "timestamp": 123.456,
        "reasoning": "working",
    }
    enriched = {**persisted, "_turnDuration": 2.5}
    same_text_without_identity = {"role": "assistant", "content": "same answer"}
    messages = [
        persisted,
        {"role": "user", "content": "continue", "timestamp": 124.0},
        persisted.copy(),
        enriched,
        enriched.copy(),
        same_text_without_identity,
        same_text_without_identity.copy(),
    ]

    guarded, removed = _deduplicate_exact_stable_messages(messages)

    assert removed == 2
    assert guarded == [
        persisted,
        messages[1],
        enriched,
        same_text_without_identity,
        same_text_without_identity,
    ]


@pytest.mark.parametrize("blank_value", ["", "   ", None])
def test_exact_stable_replay_guard_treats_blank_identity_fields_as_unstable(blank_value):
    from api.models import _deduplicate_exact_stable_messages

    blank_identity = {
        "role": "assistant",
        "content": "same answer",
        "id": blank_value,
        "timestamp": blank_value,
        "_ts": blank_value,
    }

    guarded, removed = _deduplicate_exact_stable_messages(
        [blank_identity, blank_identity.copy()]
    )

    assert guarded == [blank_identity, blank_identity]
    assert removed == 0


def test_exact_stable_replay_guard_falls_back_from_blank_timestamp_to_ts():
    from api.models import _deduplicate_exact_stable_messages

    persisted = {
        "role": "assistant",
        "content": "same answer",
        "id": "assistant-event-ts-fallback",
        "timestamp": "",
        "_ts": "persisted-event-ts",
    }

    guarded, removed = _deduplicate_exact_stable_messages(
        [persisted, persisted.copy()]
    )

    assert guarded == [persisted]
    assert removed == 1


def test_exact_stable_replay_guard_preserves_same_id_at_different_timestamps():
    from api.models import _deduplicate_exact_stable_messages

    first = {
        "role": "assistant",
        "content": "same answer",
        "id": "assistant-event-reused-id",
        "timestamp": 125.5,
    }
    later = {**first, "timestamp": 126.5}

    guarded, removed = _deduplicate_exact_stable_messages([first, later])

    assert guarded == [first, later]
    assert removed == 0


@pytest.mark.parametrize(
    "partial_identity",
    [
        {"id": "assistant-event-without-timestamp", "timestamp": ""},
        {"id": "", "timestamp": 125.5},
        {"id": "", "timestamp": "", "_ts": "persisted-event-ts"},
    ],
)
def test_exact_stable_replay_guard_requires_both_id_and_timestamp(partial_identity):
    from api.models import _deduplicate_exact_stable_messages

    persisted = {
        "role": "assistant",
        "content": "same answer",
        **partial_identity,
    }

    guarded, removed = _deduplicate_exact_stable_messages(
        [persisted, persisted.copy()]
    )

    assert guarded == [persisted, persisted]
    assert removed == 0


def test_session_save_repairs_live_and_backup_exact_replays(temp_session_dir):
    from api.models import Session
    from api.session_recovery import inspect_session_recovery_status

    repeated = {
        "role": "assistant",
        "content": "final",
        "id": "assistant-event-2",
        "timestamp": 200.25,
    }
    enriched = {**repeated, "content": "updated final"}
    session = Session(
        session_id="exact-replay-save",
        title="Replay guard",
        workspace="",
        model="test-model",
        model_provider="test-provider",
        created_at=1.0,
        updated_at=2.0,
        messages=[repeated, repeated.copy(), enriched],
    )
    session.path.write_text(
        json.dumps(
            {
                "session_id": session.session_id,
                "message_count": 3,
                "messages": session.messages,
            }
        ),
        encoding="utf-8",
    )

    session.save(skip_index=True)

    persisted = json.loads(session.path.read_text(encoding="utf-8"))
    backup_path = session.path.with_suffix(".json.bak")
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    assert persisted["messages"] == [repeated, enriched]
    assert persisted["message_count"] == 2
    assert backup["messages"] == persisted["messages"]
    assert backup["message_count"] == persisted["message_count"] == 2
    assert inspect_session_recovery_status(session.path)["recommend"] == "no_action"


def test_backup_keeps_distinct_existing_rows_while_removing_exact_replays(temp_session_dir):
    from api.models import Session

    repeated = {
        "role": "assistant",
        "content": "final",
        "id": "assistant-event-3",
        "timestamp": 300.25,
    }
    additional = {
        "role": "user",
        "content": "must survive via backup",
        "id": "user-event-4",
        "timestamp": 301.25,
    }
    current = {
        "role": "user",
        "content": "different current row",
        "id": "user-event-5",
        "timestamp": 302.25,
    }
    session = Session(
        session_id="exact-replay-save-with-extra-existing",
        title="Replay guard backup",
        messages=[repeated, repeated.copy(), current],
    )
    session.path.write_text(
        json.dumps(
            {
                "session_id": session.session_id,
                "message_count": 3,
                "messages": [repeated, repeated.copy(), additional],
            }
        ),
        encoding="utf-8",
    )

    session.save(skip_index=True)

    backup = json.loads(session.path.with_suffix(".json.bak").read_text(encoding="utf-8"))
    assert backup["messages"] == [repeated, additional]
    assert backup["message_count"] == 2


def test_save_leaves_preexisting_stale_duplicate_backup_untouched(temp_session_dir):
    from api.models import Session

    repeated = {
        "role": "assistant",
        "content": "stable",
        "id": "assistant-event-6",
        "timestamp": 400.25,
    }
    current = {
        "role": "user",
        "content": "current",
        "id": "user-event-7",
        "timestamp": 401.25,
    }
    session = Session(
        session_id="exact-replay-stale-backup",
        title="Replay guard stale backup",
        messages=[repeated, current],
    )
    session.path.write_text(
        json.dumps(
            {
                "session_id": session.session_id,
                "message_count": 2,
                "messages": [repeated, current],
            }
        ),
        encoding="utf-8",
    )
    stale_backup = {
        "session_id": session.session_id,
        "message_count": 3,
        "messages": [repeated, repeated.copy(), current],
    }
    backup_path = session.path.with_suffix(".json.bak")
    backup_path.write_text(json.dumps(stale_backup), encoding="utf-8")
    before = backup_path.read_bytes()

    session.save(skip_index=True)

    assert backup_path.read_bytes() == before
    persisted = json.loads(session.path.read_text(encoding="utf-8"))
    assert persisted["messages"] == [repeated, current]
    assert persisted["message_count"] == 2
