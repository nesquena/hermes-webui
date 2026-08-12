import copy
import json
import threading

import pytest


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

    # save() never rebinds or mutates the list visible to active workers.
    assert [message["id"] for message in session.messages] == [1701, 1702, 1701, 1702]
    persisted = json.loads((session_dir / "save-barrier.json").read_text(encoding="utf-8"))
    assert [message["id"] for message in persisted["messages"]] == [1701, 1702]
    assert persisted["message_count"] == 2


def test_save_snapshot_does_not_lose_concurrent_alias_append(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "index.json")

    first = _incomplete_reasoning_only(1701, reasoning="first")
    session = models.Session(session_id="concurrent-alias", messages=[first, dict(first)])
    live_alias = session.messages
    concurrent = {"role": "user", "content": "arrived during save", "id": 1702}
    real_collapse = models._collapse_duplicate_incomplete_message_ids

    def collapse_then_append(messages):
        collapsed = real_collapse(messages)
        live_alias.append(concurrent)
        return collapsed

    monkeypatch.setattr(models, "_collapse_duplicate_incomplete_message_ids", collapse_then_append)
    session.save(skip_index=True)

    assert session.messages is live_alias
    assert session.messages[-1] == concurrent
    first_payload = json.loads((session_dir / "concurrent-alias.json").read_text(encoding="utf-8"))
    assert [message["id"] for message in first_payload["messages"]] == [1701]

    monkeypatch.setattr(models, "_collapse_duplicate_incomplete_message_ids", real_collapse)
    session.save(skip_index=True)
    second_payload = json.loads((session_dir / "concurrent-alias.json").read_text(encoding="utf-8"))
    assert [message["id"] for message in second_payload["messages"]] == [1701, 1702]


def test_recovery_does_not_resurrect_duplicate_incomplete_backup(tmp_path):
    from api.session_recovery import inspect_session_recovery_status, recover_session

    session_path = tmp_path / "repaired.json"
    backup_path = tmp_path / "repaired.json.bak"
    first = _incomplete_reasoning_only(1701, reasoning="first")
    second = _incomplete_reasoning_only(1702, reasoning="second")
    session_path.write_text(json.dumps({"messages": [first, second]}), encoding="utf-8")
    backup_path.write_text(
        json.dumps({"messages": [first, second, dict(first), dict(second)]}),
        encoding="utf-8",
    )

    status = inspect_session_recovery_status(session_path)
    assert status["live_messages"] == 2
    assert status["bak_messages"] == 2
    assert status["recommend"] == "no_action"
    assert recover_session(session_path)["restored"] is False


def test_recovery_still_restores_unique_backup_excess(tmp_path):
    from api.session_recovery import inspect_session_recovery_status

    session_path = tmp_path / "repaired.json"
    backup_path = tmp_path / "repaired.json.bak"
    first = _incomplete_reasoning_only(1701, reasoning="first")
    unique = {"role": "user", "content": "must survive", "id": 1702}
    session_path.write_text(json.dumps({"messages": [first]}), encoding="utf-8")
    backup_path.write_text(json.dumps({"messages": [first, unique]}), encoding="utf-8")

    status = inspect_session_recovery_status(session_path)
    assert status["live_messages"] == 1
    assert status["bak_messages"] == 2
    assert status["recommend"] == "restore"


def test_typed_incomplete_ids_round_trip_without_cross_type_collision():
    from api.models import (
        _collapse_duplicate_incomplete_message_ids,
        _strict_incomplete_message_id_key,
    )

    # Each admissible scalar type carries its own deletion authority.
    assert _strict_incomplete_message_id_key("abc") == ("str", "abc")
    assert _strict_incomplete_message_id_key(1701) == ("int", 1701)
    assert _strict_incomplete_message_id_key(1.5) == ("float", 1.5)

    # 1 vs "1" vs 1.0 vs True vs "True" vs b"1" are DISTINCT rows: none may
    # collapse into another's bucket, and the bytes id must round-trip intact.
    typed_ids = [1, "1", 1.0, True, "True", b"1"]
    rows = [_incomplete_reasoning_only(message_id) for message_id in typed_ids]
    collapsed, _ = _collapse_duplicate_incomplete_message_ids(rows)
    assert len(collapsed) == len(rows)
    kept = [message["id"] for message in collapsed]
    assert sum(1 for k in kept if type(k) is int and k == 1) == 1
    assert sum(1 for k in kept if type(k) is str and k == "1") == 1
    assert sum(1 for k in kept if type(k) is float and k == 1.0) == 1
    assert sum(1 for k in kept if type(k) is str and k == "True") == 1
    assert sum(1 for k in kept if type(k) is bool and k is True) == 1
    assert sum(1 for k in kept if type(k) is bytes and k == b"1") == 1

    # Same-type replays of an admissible id still collapse exactly as before.
    for message_id in (1, "1", 1.5):
        pair = [_incomplete_reasoning_only(message_id), _incomplete_reasoning_only(message_id)]
        deduped, changed = _collapse_duplicate_incomplete_message_ids(pair)
        assert changed is True
        assert len(deduped) == 1
        assert deduped[0]["id"] == message_id
        assert type(deduped[0]["id"]) is type(message_id)


def test_incomplete_id_rejects_bool_container_subclass_and_non_finite():
    from api.models import _strict_incomplete_message_id_key as key

    class StrId(str):
        pass

    class IntId(int):
        pass

    assert key(True) is None
    assert key(False) is None
    assert key(None) is None
    assert key("") is None
    assert key(["1"]) is None
    assert key({"id": 1}) is None
    assert key(("1",)) is None
    assert key(b"1") is None
    assert key(StrId("1")) is None
    assert key(IntId(1)) is None
    assert key(float("nan")) is None
    assert key(float("inf")) is None
    assert key(float("-inf")) is None


def test_mixed_type_backup_rows_remain_independently_recoverable(tmp_path):
    from api.session_recovery import inspect_session_recovery_status

    session_path = tmp_path / "mixed.json"
    backup_path = tmp_path / "mixed.json.bak"
    int_row = _incomplete_reasoning_only(1)
    str_row = _incomplete_reasoning_only("1")
    session_path.write_text(json.dumps({"messages": [int_row]}), encoding="utf-8")
    backup_path.write_text(
        json.dumps({"messages": [int_row, str_row]}), encoding="utf-8"
    )

    status = inspect_session_recovery_status(session_path)
    assert status["live_messages"] == 1
    # "1" is NOT a duplicate-only replay of 1: the distinct backup row keeps
    # its recovery authority instead of being classified as a replay of 1.
    assert status["bak_messages"] == 2
    assert status["recommend"] == "restore"


def test_save_deep_isolates_retained_rows_from_concurrent_mutation(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "index.json")

    session = models.Session(
        session_id="deep-isolation",
        messages=[_incomplete_reasoning_only(1701)],
    )
    live_row = session.messages[0]
    real_deepcopy = copy.deepcopy

    def deepcopy_then_mutate(obj, *args, **kwargs):
        snapshot = real_deepcopy(obj, *args, **kwargs)
        if isinstance(obj, list) and obj and obj[0] is live_row:
            # A worker mutates the LIVE nested dict in the window between the
            # collapse scan and json.dumps; the persisted payload must not
            # observe it.
            live_row["codex_reasoning_items"][0]["encrypted_content"] = "MUTATED"
        return snapshot

    monkeypatch.setattr(models.copy, "deepcopy", deepcopy_then_mutate)
    session.save(skip_index=True)

    persisted = json.loads((session_dir / "deep-isolation.json").read_text(encoding="utf-8"))
    assert persisted["messages"][0]["codex_reasoning_items"][0]["encrypted_content"] == "opaque"
    assert session.messages[0]["codex_reasoning_items"][0]["encrypted_content"] == "MUTATED"


def test_save_owns_snapshot_before_duplicate_selection(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "index.json")

    first = _incomplete_reasoning_only(1701, reasoning="first")
    duplicate = copy.deepcopy(first)
    session = models.Session(session_id="selection-isolation", messages=[first, duplicate])
    real_collapse = models._collapse_duplicate_incomplete_message_ids
    selection_done = threading.Event()
    mutation_done = threading.Event()

    def mutate_after_selection():
        assert selection_done.wait(timeout=2)
        first["codex_reasoning_items"][0]["encrypted_content"] = "MUTATED"
        mutation_done.set()

    worker = threading.Thread(target=mutate_after_selection)
    worker.start()

    def collapse_then_mutate(messages):
        selected = real_collapse(messages)
        # Pause save after selection while a scheduled writer mutates the live
        # row.  This is exactly before the old post-selection deepcopy.
        selection_done.set()
        assert mutation_done.wait(timeout=2)
        return selected

    monkeypatch.setattr(models, "_collapse_duplicate_incomplete_message_ids", collapse_then_mutate)
    session.save(skip_index=True)
    worker.join(timeout=2)
    assert not worker.is_alive()

    persisted = json.loads((session_dir / "selection-isolation.json").read_text(encoding="utf-8"))
    assert len(persisted["messages"]) == 1
    assert persisted["messages"][0]["codex_reasoning_items"][0]["encrypted_content"] == "opaque"


def test_save_writes_index_from_same_collapsed_snapshot(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = tmp_path / "index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)

    session = models.Session(
        session_id="index-parity",
        messages=[_incomplete_reasoning_only(1701, timestamp=100)],
    )
    session.save()  # full-rebuild path creates the index

    # A replayed duplicate carrying a LATER timestamp lands in the live list.
    session.messages.append(_incomplete_reasoning_only(1701, timestamp=999))
    session._metadata_message_count = 2
    session.save()  # fast path: _write_session_index(updates=[self])

    sidecar = json.loads((session_dir / "index-parity.json").read_text(encoding="utf-8"))
    assert sidecar["message_count"] == 1
    assert len(sidecar["messages"]) == 1

    index_entries = json.loads(index_file.read_text(encoding="utf-8"))
    entry = next(e for e in index_entries if e["session_id"] == "index-parity")
    # The sidebar index must reflect the SAME collapsed snapshot: no count of
    # 2, and no adoption of the dropped duplicate's later timestamp.
    assert entry["message_count"] == sidecar["message_count"] == 1
    assert entry["last_message_at"] == 100


@pytest.mark.parametrize("first_generation", ["one", "two"])
def test_same_sid_saves_publish_one_complete_generation_in_both_orders(
    tmp_path, monkeypatch, first_generation
):
    from api import models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)

    one = models.Session(
        session_id="shared-save-authority",
        title="generation-one",
        model="model-one",
        messages=[{"role": "user", "content": "one", "timestamp": 100}],
    )
    two = models.Session(
        session_id="shared-save-authority",
        title="generation-two",
        model="model-two",
        messages=[
            {"role": "user", "content": "one", "timestamp": 100},
            {"role": "assistant", "content": "two", "timestamp": 200},
        ],
    )
    generations = {"one": one, "two": two}
    second_generation = "two" if first_generation == "one" else "one"
    first_reached_index = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    real_write_index = models._write_session_index
    first_thread_id = {"value": None}

    def gated_write_index(*args, **kwargs):
        if threading.get_ident() == first_thread_id["value"]:
            first_reached_index.set()
            assert release_first.wait(timeout=2)
        return real_write_index(*args, **kwargs)

    monkeypatch.setattr(models, "_write_session_index", gated_write_index)

    def save_first():
        first_thread_id["value"] = threading.get_ident()
        generations[first_generation].save(touch_updated_at=False)

    def save_second():
        second_started.set()
        generations[second_generation].save(touch_updated_at=False)

    first = threading.Thread(target=save_first)
    second = threading.Thread(target=save_second)
    first.start()
    assert first_reached_index.wait(timeout=2)
    second.start()
    assert second_started.wait(timeout=2)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()

    sidecar = json.loads((session_dir / "shared-save-authority.json").read_text(encoding="utf-8"))
    index = json.loads(index_file.read_text(encoding="utf-8"))
    row = next(entry for entry in index if entry["session_id"] == "shared-save-authority")
    expected = generations[second_generation]
    assert sidecar["title"] == row["title"] == expected.title
    assert sidecar["model"] == row["model"] == expected.model
    assert sidecar["message_count"] == row["message_count"] == len(expected.messages)
    assert row["user_message_count"] == expected._compute_user_message_count(expected.messages)
    assert row["last_message_at"] == expected.messages[-1]["timestamp"]


def test_recovery_restores_the_collapsed_backup_payload(tmp_path):
    from api.session_recovery import inspect_session_recovery_status, recover_session

    session_path = tmp_path / "restored.json"
    backup_path = tmp_path / "restored.json.bak"
    first = _incomplete_reasoning_only(1701, reasoning="first")
    unique = {"role": "user", "content": "must survive", "id": 1702}
    session_path.write_text(json.dumps({"messages": [first]}), encoding="utf-8")
    backup_path.write_text(
        json.dumps({"messages": [first, dict(first), unique], "message_count": 3}),
        encoding="utf-8",
    )

    status = inspect_session_recovery_status(session_path)
    assert status["recommend"] == "restore"
    result = recover_session(session_path)
    assert result["restored"] is True

    # The restore writes the SAME effective payload _msg_count() evaluated:
    # the duplicate-only replay is NOT resurrected, the unique row survives,
    # and message_count is recomputed from the collapsed payload.
    restored = json.loads(session_path.read_text(encoding="utf-8"))
    assert restored["messages"] == [first, unique]
    assert restored["message_count"] == 2


def test_reconciliation_and_persistence_share_incomplete_eligibility():
    from api.models import _incomplete_reasoning_message_id
    from api.streaming import _message_identity

    def reconciliation_incomplete_key(message):
        identity = _message_identity(message)
        if (
            isinstance(identity, tuple)
            and len(identity) == 4
            and str(identity[3]).startswith("__incomplete_message_id__")
        ):
            return identity[3]
        return None

    base = _incomplete_reasoning_only(1701)
    structured_blank = {
        **base,
        "content": [{"type": "output_text", "text": "  <think>hidden</think>  "}],
    }
    structured_visible = {
        **base,
        "content": [{"type": "output_text", "text": "visible answer"}],
    }
    cases_eligible = [
        base,
        structured_blank,
        {**base, "id": 1},
        {**base, "id": "1"},
        {**base, "id": 1.5},
    ]
    cases_ineligible = [
        structured_visible,
        {**base, "tool_call_id": "call_1"},
        {**base, "tool_calls": [{"id": "call_1"}]},
        {**base, "id": True},
        {**base, "id": ""},
        {**base, "id": None},
        {**base, "id": ["1"]},
        {**base, "id": float("nan")},
    ]

    partial_same_id_different_reasoning = [
        {**base, "_partial": True, "reasoning": "first"},
        {**base, "_partial": True, "reasoning": "second"},
    ]
    partial_different_ids_same_reasoning = [
        {**base, "_partial": True, "id": 1701, "reasoning": "same"},
        {**base, "_partial": True, "id": 1702, "reasoning": "same"},
    ]

    # Blank content (plain or structured), tool-call identity, and malformed
    # ids are classified identically by both layers: neither may drop a row
    # the other considers distinct.
    for message in cases_eligible:
        assert _incomplete_reasoning_message_id(message) is not None
        assert reconciliation_incomplete_key(message) is not None
    for message in cases_ineligible:
        assert _incomplete_reasoning_message_id(message) is None
        assert reconciliation_incomplete_key(message) is None
    assert (
        reconciliation_incomplete_key(partial_same_id_different_reasoning[0])
        == reconciliation_incomplete_key(partial_same_id_different_reasoning[1])
    )
    assert (
        reconciliation_incomplete_key(partial_different_ids_same_reasoning[0])
        != reconciliation_incomplete_key(partial_different_ids_same_reasoning[1])
    )
