"""Behavioral coverage for #6911 intentional message-shrink generations."""
from __future__ import annotations

import json

import pytest

_VALID_GENERATION_A = "0123456789ab4cde8f0123456789abcd"
_VALID_GENERATION_B = "fedcba9876544c21b0fedcba98765432"


def _msg(role: str, content: str, ts: float, mid: str) -> dict:
    return {"id": mid, "role": role, "content": content, "timestamp": ts}


def _history() -> list[dict]:
    return [
        _msg("user", "first", 1.0, "u1"),
        _msg("assistant", "reply first", 2.0, "a1"),
        _msg("user", "second", 3.0, "u2"),
        _msg("assistant", "reply second", 4.0, "a2"),
    ]


def _seed_session_dir(monkeypatch, tmp_path):
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    return models, session_dir


def _write_json(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def _live_and_backup(sid: str, live_generation, backup_generation):
    live = {
        "session_id": sid,
        "messages": [_msg("user", "live", 1.0, "u1")],
        "context_messages": [_msg("user", "live", 1.0, "cu1")],
        "intentional_shrink_generation": live_generation,
    }
    backup = {
        "session_id": sid,
        "messages": [
            _msg("user", "live", 1.0, "u1"),
            _msg("assistant", "reply", 2.0, "a1"),
        ],
        "context_messages": [
            _msg("user", "live", 1.0, "cu1"),
            _msg("assistant", "reply", 2.0, "ca1"),
        ],
        "intentional_shrink_generation": backup_generation,
    }
    return live, backup


def test_undo_shrink_is_not_revived_by_startup_recovery(monkeypatch, tmp_path):
    models, session_dir = _seed_session_dir(monkeypatch, tmp_path)
    from api.session_ops import undo_last
    from api.session_recovery import recover_all_sessions_on_startup

    session = models.Session(
        session_id="issue6911_undo",
        messages=_history(),
        context_messages=_history(),
    )
    session.save()

    undo_last(session.session_id)

    live = models.Session.load(session.session_id)
    assert live is not None
    assert [row["content"] for row in live.messages] == ["first", "reply first"]
    assert isinstance(live.intentional_shrink_generation, str)
    assert live.intentional_shrink_generation
    assert session.path.with_suffix(".json.bak").exists()

    result = recover_all_sessions_on_startup(session_dir)
    assert result["restored"] == 0
    assert [row["content"] for row in models.Session.load(session.session_id).messages] == [
        "first",
        "reply first",
    ]


def test_retry_shrink_is_not_revived_by_startup_recovery(monkeypatch, tmp_path):
    models, session_dir = _seed_session_dir(monkeypatch, tmp_path)
    from api.session_ops import retry_last
    from api.session_recovery import recover_all_sessions_on_startup

    session = models.Session(
        session_id="issue6911_retry",
        messages=_history(),
        context_messages=_history(),
    )
    session.save()

    result = retry_last(session.session_id)

    assert result["removed_count"] == 2
    live = models.Session.load(session.session_id)
    assert live is not None
    assert [row["content"] for row in live.messages] == ["first", "reply first"]
    assert isinstance(live.intentional_shrink_generation, str)
    assert live.intentional_shrink_generation

    recovery = recover_all_sessions_on_startup(session_dir)
    assert recovery["restored"] == 0
    assert [row["content"] for row in models.Session.load(session.session_id).messages] == [
        "first",
        "reply first",
    ]


def test_truncate_chokepoint_shrink_is_not_revived_by_startup_recovery(monkeypatch, tmp_path):
    models, session_dir = _seed_session_dir(monkeypatch, tmp_path)
    from api.session_ops import truncate_session_at_keep
    from api.session_recovery import recover_all_sessions_on_startup

    session = models.Session(
        session_id="issue6911_truncate",
        messages=_history(),
        context_messages=_history(),
    )
    session.save()

    truncate_session_at_keep(session, 2)
    session.save()

    live = models.Session.load(session.session_id)
    assert live is not None
    assert len(live.messages) == 2
    assert isinstance(live.intentional_shrink_generation, str)
    assert live.intentional_shrink_generation

    recovery = recover_all_sessions_on_startup(session_dir)
    assert recovery["restored"] == 0
    assert len(models.Session.load(session.session_id).messages) == 2


def test_generation_save_load_round_trip_is_in_metadata_prefix(monkeypatch, tmp_path):
    models, _session_dir = _seed_session_dir(monkeypatch, tmp_path)
    generation = _VALID_GENERATION_A
    session = models.Session(
        session_id="issue6911_round_trip",
        messages=_history(),
        intentional_shrink_generation=generation,
    )
    session.save()

    persisted = json.loads(session.path.read_text(encoding="utf-8"))
    assert persisted["intentional_shrink_generation"] == generation
    assert list(persisted).index("intentional_shrink_generation") < list(persisted).index("messages")
    loaded = models.Session.load(session.session_id)
    metadata = models.Session.load_metadata_only(session.session_id)
    assert loaded.intentional_shrink_generation == generation
    assert metadata.intentional_shrink_generation == generation


def test_no_actual_message_shrink_keeps_generation(monkeypatch, tmp_path):
    models, _session_dir = _seed_session_dir(monkeypatch, tmp_path)
    from api.session_ops import truncate_session_at_keep

    session = models.Session(
        session_id="issue6911_no_shrink",
        messages=_history(),
        intentional_shrink_generation=_VALID_GENERATION_A,
    )
    session.save()

    truncate_session_at_keep(session, len(session.messages))

    assert session.intentional_shrink_generation == _VALID_GENERATION_A
    session.save()
    assert not session.path.with_suffix(".json.bak").exists()


@pytest.mark.parametrize("invalid_generation", [None, "", " ", 0, [], {}])
def test_missing_or_invalid_live_generation_fails_open(tmp_path, invalid_generation):
    from api.session_recovery import recover_session

    sid = f"issue6911_invalid_{type(invalid_generation).__name__}"
    live, backup = _live_and_backup(sid, invalid_generation, _VALID_GENERATION_B)
    live_path = tmp_path / f"{sid}.json"
    bak_path = live_path.with_suffix(".json.bak")
    _write_json(live_path, live)
    _write_json(bak_path, backup)

    result = recover_session(live_path)

    assert result["restored"] is True
    assert json.loads(live_path.read_text(encoding="utf-8"))["messages"] == backup["messages"]


def test_same_generation_backup_from_real_save_remains_recoverable(monkeypatch, tmp_path):
    """A later non-intentional shrink must keep the generation and recover."""
    models, session_dir = _seed_session_dir(monkeypatch, tmp_path)
    from api.session_recovery import recover_all_sessions_on_startup

    sid = "issue6911_same_generation"
    generation = _VALID_GENERATION_A
    session = models.Session(
        session_id=sid,
        messages=_history(),
        context_messages=_history(),
        intentional_shrink_generation=generation,
    )
    session.save()

    # Model an unrelated data-loss shrink that did not come through the
    # intentional truncate chokepoint: Session.save() must still create a
    # same-generation rescue backup that remains recoverable.
    session.messages = session.messages[:2]
    session.context_messages = session.context_messages[:2]
    session.save()

    live_path = session.path
    bak_path = live_path.with_suffix(".json.bak")
    assert bak_path.exists()
    assert json.loads(live_path.read_text(encoding="utf-8"))["intentional_shrink_generation"] == generation
    assert json.loads(bak_path.read_text(encoding="utf-8"))["intentional_shrink_generation"] == generation

    result = recover_all_sessions_on_startup(session_dir)

    assert result["restored"] == 1
    assert json.loads(live_path.read_text(encoding="utf-8"))["messages"] == _history()


@pytest.mark.parametrize("invalid_generation", ["", " ", 0, [], {}])
def test_invalid_backup_generation_fails_open(tmp_path, invalid_generation):
    from api.session_recovery import recover_session

    sid = "issue6911_invalid_backup_generation"
    live, backup = _live_and_backup(sid, _VALID_GENERATION_A, invalid_generation)
    live_path = tmp_path / f"{sid}.json"
    _write_json(live_path, live)
    _write_json(live_path.with_suffix(".json.bak"), backup)

    result = recover_session(live_path)

    assert result["restored"] is True
    assert json.loads(live_path.read_text(encoding="utf-8"))["messages"] == backup["messages"]


def test_different_generation_backup_is_intentional_and_not_restored(tmp_path):
    from api.session_recovery import inspect_session_recovery_status, recover_session

    sid = "issue6911_different_generation"
    live, backup = _live_and_backup(sid, _VALID_GENERATION_A, _VALID_GENERATION_B)
    live_path = tmp_path / f"{sid}.json"
    _write_json(live_path, live)
    _write_json(live_path.with_suffix(".json.bak"), backup)

    status = inspect_session_recovery_status(live_path)
    result = recover_session(live_path)

    assert status["recommend"] == "no_action"
    assert status["intentional_message_shrink"] is True
    assert result["restored"] is False
    assert json.loads(live_path.read_text(encoding="utf-8"))["messages"] == live["messages"]


@pytest.mark.parametrize(
    ("field", "malformed_generation"),
    [
        ("live", "0123456789AB4CDE8F0123456789ABCD"),
        ("backup", "0123456789AB4CDE8F0123456789ABCD"),
        ("live", "0123456789ab4cde8f0123456789abcd0"),
        ("backup", "0123456789ab4cde8f0123456789abcd0"),
        ("live", "0123456789ab4cde8f0123456789abc"),
        ("backup", "0123456789ab4cde8f0123456789abc"),
        ("live", "0123456789ab5cde8f0123456789abcd"),
        ("backup", "0123456789ab5cde8f0123456789abcd"),
        ("live", "0123456789ab4cdecf0123456789abcd"),
        ("backup", "0123456789ab4cdecf0123456789abcd"),
    ],
)
def test_malformed_nonblank_generation_fails_open_to_restore(
    tmp_path, field, malformed_generation,
):
    """Malformed nonblank UUID4 markers must not suppress recovery."""
    from api.session_recovery import recover_session

    sid = "issue6911_malformed_nonblank"
    live_generation = malformed_generation if field == "live" else _VALID_GENERATION_A
    backup_generation = malformed_generation if field == "backup" else _VALID_GENERATION_B
    live, backup = _live_and_backup(sid, live_generation, backup_generation)
    live_path = tmp_path / f"{sid}.json"
    _write_json(live_path, live)
    _write_json(live_path.with_suffix(".json.bak"), backup)

    result = recover_session(live_path)

    assert result["restored"] is True
    assert json.loads(live_path.read_text(encoding="utf-8"))["messages"] == backup["messages"]
