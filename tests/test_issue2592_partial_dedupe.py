import json


def _incomplete_reasoning_only(message_id, *, reasoning="encrypted reasoning", timestamp=123):
    return {
        "id": message_id,
        "role": "assistant",
        "content": "",
        "timestamp": timestamp,
        "finish_reason": "incomplete",
        "reasoning": reasoning,
        "codex_reasoning_items": [{"type": "reasoning", "encrypted_content": "opaque"}],
    }


def _tool_partial(reasoning="same reasoning", args=None, *, timestamp=123):
    return {
        "role": "assistant",
        "content": "",
        "_partial": True,
        "timestamp": timestamp,
        "reasoning": reasoning,
        "_partial_tool_calls": [
            {
                "name": "execute_code",
                "args": args or {"code": "raise RuntimeError('boom')"},
                "done": True,
                "is_error": True,
                "duration": 3.87,
            }
        ],
    }


def test_tool_only_partial_dedupe_uses_reasoning_and_tool_signature():
    from api.streaming import _partial_marker_already_present

    existing = [
        {"role": "user", "content": "run this"},
        _tool_partial(),
        {"role": "assistant", "content": "**Task cancelled.**", "_error": True},
    ]

    assert _partial_marker_already_present(existing, _tool_partial(), before_idx=2)
    assert not _partial_marker_already_present(
        existing,
        _tool_partial(args={"code": "print('different tool body')"}),
        before_idx=2,
    )


def test_tool_only_partial_dedupe_is_scoped_to_current_user_turn():
    from api.streaming import _partial_marker_already_present

    existing = [
        {"role": "user", "content": "first run"},
        _tool_partial(),
        {"role": "assistant", "content": "**Task cancelled.**", "_error": True},
        {"role": "user", "content": "repeat it"},
    ]

    assert not _partial_marker_already_present(existing, _tool_partial(), before_idx=len(existing))


def test_session_load_collapses_adjacent_duplicate_partials(tmp_path, monkeypatch):
    import api.models as models

    sid = "abc123"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")

    payload = {
        "session_id": sid,
        "title": "bloated partials",
        "workspace": str(tmp_path),
        "model": "gpt-5.5",
        "created_at": 100.0,
        "updated_at": 200.0,
        "messages": [
            {"role": "user", "content": "run this"},
            _tool_partial(timestamp=123),
            _tool_partial(timestamp=123),
            _tool_partial(timestamp=123),
            {"role": "assistant", "content": "**Task cancelled.**", "_error": True},
        ],
        "tool_calls": [],
    }
    (session_dir / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")

    loaded = models.Session.load(sid)

    assert loaded is not None
    assert sum(1 for message in loaded.messages if message.get("_partial")) == 1
    persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert sum(1 for message in persisted["messages"] if message.get("_partial")) == 1
    assert persisted["updated_at"] == 200.0
    assert (session_dir / f"{sid}.json.bak").exists()


def test_reasoning_only_incomplete_identity_uses_stable_message_id():
    from api.streaming import _message_identity

    first = _incomplete_reasoning_only(1701)
    replay = _incomplete_reasoning_only(1701, timestamp=999)
    distinct = _incomplete_reasoning_only(1702)

    assert _message_identity(first) == _message_identity(replay)
    assert _message_identity(first) != _message_identity(distinct)
    assert _message_identity({"role": "assistant", "content": "", "finish_reason": "incomplete"}) is None


def test_session_load_collapses_non_adjacent_duplicate_incomplete_ids(tmp_path, monkeypatch):
    import api.models as models

    sid = "fd05-copy"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    first = _incomplete_reasoning_only(1701, reasoning="first")
    second = _incomplete_reasoning_only(1702, reasoning="second")
    payload = {
        "session_id": sid,
        "title": "FD05 duplicated incomplete responses",
        "workspace": str(tmp_path),
        "model": "gpt-5.6",
        "created_at": 100.0,
        "updated_at": 200.0,
        "messages": [
            {"role": "user", "content": "run this"},
            first,
            second,
            dict(first),
            dict(second),
            dict(first),
            dict(second),
            {"role": "assistant", "content": "final visible answer"},
        ],
        "tool_calls": [],
    }
    (session_dir / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")

    loaded = models.Session.load(sid)

    assert loaded is not None
    assert [message.get("id") for message in loaded.messages if message.get("id")] == [1701, 1702]
    persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert [message.get("id") for message in persisted["messages"] if message.get("id")] == [1701, 1702]
    assert persisted["updated_at"] == 200.0
    assert (session_dir / f"{sid}.json.bak").exists()


def test_context_dedupe_is_idempotent_for_alternating_incomplete_ids():
    from api.streaming import _deduplicate_context_messages

    first = _incomplete_reasoning_only(1701, reasoning="first")
    second = _incomplete_reasoning_only(1702, reasoning="second")
    messages = [first, second, dict(first), dict(second)] * 10

    once = _deduplicate_context_messages(messages)
    twice = _deduplicate_context_messages(once)

    assert [message["id"] for message in once] == [1701, 1702]
    assert twice == once


def test_display_merge_dedupes_incomplete_ids_after_state_db_reconciliation():
    from api.streaming import _merge_display_messages_after_agent_result

    first = _incomplete_reasoning_only(1701, reasoning="first")
    second = _incomplete_reasoning_only(1702, reasoning="second")
    user = {"role": "user", "content": "next", "id": 1703}
    answer = {"role": "assistant", "content": "done", "id": 1704, "finish_reason": "stop"}

    merged = _merge_display_messages_after_agent_result(
        [first, second, dict(first), dict(second)],
        [first, second],
        [first, second, dict(first), dict(second), user, answer],
        "next",
    )

    ids = [message.get("id") for message in merged]
    assert ids.count(1701) == 1
    assert ids.count(1702) == 1
    assert ids[-2:] == [1703, 1704]


def test_save_is_a_final_idempotent_barrier_for_incomplete_message_ids(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "index.json")

    first = _incomplete_reasoning_only(1701, reasoning="first")
    second = _incomplete_reasoning_only(1702, reasoning="second")
    session = models.Session(
        session_id="save-barrier",
        messages=[first, second, dict(first), dict(second)],
    )

    session.save(skip_index=True)
    session.save(skip_index=True)

    assert [message["id"] for message in session.messages] == [1701, 1702]
    persisted = json.loads(session.path.read_text(encoding="utf-8"))
    assert [message["id"] for message in persisted["messages"]] == [1701, 1702]
