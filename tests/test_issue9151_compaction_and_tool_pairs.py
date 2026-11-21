"""Spec for PR #9151 round 2 — compression replacement and tool-pair identity.

Review 2026-11-18T03:23:07Z asked for two persistence regressions to be repaired:

1. ``Session.save(append_intent=True)`` treats every completed context as an
   append, so a context that in-place compression deliberately replaced gets the
   discarded baseline rows reinserted by ``_merge_concurrent_message_arrays``.
2. ``_message_rows_match`` ignores ``tool_calls`` / ``tool_call_id``, so two
   branches whose freshly minted integer ids collide conflate an assistant
   invocation row with the other branch's row and persist a tool result without
   its invocation.

These tests drive the real session sidecar (load -> mutate -> save -> durable
reload) and the real production id allocator, with normal-append controls.
"""
from __future__ import annotations

from collections import OrderedDict

import pytest

import api.models as models
from api.models import Session


@pytest.fixture
def temp_session_dir(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr("api.config.SESSION_DIR", session_dir)
    monkeypatch.setattr("api.config.SESSIONS", OrderedDict())
    return session_dir


def _head_rows():
    return [
        {"role": "system", "content": "you are a test agent", "id": 1},
        {"role": "user", "content": "protected question", "id": 2},
    ]


def _discarded_rows(count=8):
    rows = []
    for i in range(count):
        rows.append({
            "role": "assistant" if i % 2 == 0 else "user",
            "content": f"discarded row {i}",
            "id": 10 + i,
        })
    return rows


def _summary_row():
    return {
        "role": "user",
        "content": "[CONTEXT COMPACTION] summary that replaced the old middle",
        "id": 30,
    }


def _new_turn_row():
    return {"role": "assistant", "content": "answer after compaction", "id": 31}


def _contents(rows):
    return [row.get("content") for row in rows]


def test_compaction_replacement_is_not_reconciled_as_an_append(temp_session_dir):
    """Rows compression discarded must not come back on an append-intent save."""
    sid = "sess_comp_replace_1"
    head = _head_rows()
    discarded = _discarded_rows()

    base = Session(session_id=sid)
    base.messages = [dict(r) for r in head + discarded]
    base.context_messages = [dict(r) for r in head + discarded]
    base.save()

    writer = Session.load(sid)
    assert _contents(writer.context_messages) == _contents(head + discarded)

    summary = _summary_row()
    new_turn = _new_turn_row()
    writer.messages = [dict(r) for r in head] + [dict(summary), dict(new_turn)]
    writer.context_messages = [dict(r) for r in head] + [dict(summary), dict(new_turn)]
    writer.compression_anchor_visible_idx = 1
    writer.save(append_intent=True)

    loaded = Session.load(sid)
    assert _contents(loaded.context_messages) == _contents(head + [summary, new_turn])
    assert _contents(loaded.messages) == _contents(head + [summary, new_turn])


def test_compaction_replacement_keeps_a_concurrent_append(temp_session_dir):
    """Replacement is authoritative for baseline rows; a real append survives."""
    sid = "sess_comp_replace_2"
    head = _head_rows()
    discarded = _discarded_rows()

    base = Session(session_id=sid)
    base.messages = [dict(r) for r in head + discarded]
    base.context_messages = [dict(r) for r in head + discarded]
    base.save()

    # Both writers load the same base; the other one saves while we are working.
    writer = Session.load(sid)
    concurrent = Session.load(sid)
    turn = {"role": "user", "content": "turn from another client", "id": 40}
    concurrent.messages.append(dict(turn))
    concurrent.context_messages.append(dict(turn))
    concurrent.save(append_intent=True)

    summary = _summary_row()
    new_turn = _new_turn_row()
    writer.messages = [dict(r) for r in head] + [dict(summary), dict(new_turn)]
    writer.context_messages = [dict(r) for r in head] + [dict(summary), dict(new_turn)]
    writer.compression_anchor_visible_idx = 1
    writer.save(append_intent=True)

    loaded = Session.load(sid)
    for rows in (loaded.context_messages, loaded.messages):
        texts = _contents(rows)
        assert "turn from another client" in texts
        assert not any(str(t or "").startswith("discarded row") for t in texts)
        assert any(str(t or "").startswith("[CONTEXT COMPACTION]") for t in texts)


def test_normal_append_control_still_reconciles_both_writers(temp_session_dir):
    """Control: the append feature itself keeps working (no compression involved)."""
    sid = "sess_comp_control_1"
    base = Session(session_id=sid)
    base.messages = [{"role": "user", "content": "initial question", "id": 1}]
    base.context_messages = [{"role": "user", "content": "initial question", "id": 1}]
    base.save()

    client_a = Session.load(sid)
    client_a.messages.append({"role": "assistant", "content": "answer from A", "id": 2})
    client_a.context_messages.append({"role": "assistant", "content": "answer from A", "id": 2})

    client_b = Session.load(sid)
    client_b.messages.append({"role": "user", "content": "question from B", "id": 2})
    client_b.context_messages.append({"role": "user", "content": "question from B", "id": 2})

    client_a.save(append_intent=True)
    client_b.save(append_intent=True)

    loaded = Session.load(sid)
    texts = _contents(loaded.messages)
    assert "answer from A" in texts
    assert "question from B" in texts


def _invocation(call_id, name="read_file"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{\"path\": \"x\"}"},
        }],
    }


def _tool_result(call_id, payload):
    return {"role": "tool", "content": payload, "tool_call_id": call_id}


def test_colliding_branch_local_ids_keep_both_tool_pairs(temp_session_dir):
    """Two branches mint colliding integer ids; every result must keep its invocation."""
    from api.streaming import _assign_stable_message_ids

    sid = "sess_tool_pair_1"
    base = Session(session_id=sid)
    base.messages = [{"role": "user", "content": "question", "id": 1}]
    base.context_messages = [{"role": "user", "content": "question", "id": 1}]
    base.save()

    client_a = Session.load(sid)
    client_a.messages.append(_invocation("call_A"))
    client_a.messages.append(_tool_result("call_A", "result for A"))
    minted_a = _assign_stable_message_ids(client_a.messages)

    client_b = Session.load(sid)
    client_b.messages.append(_invocation("call_B", name="write_file"))
    client_b.messages.append(_tool_result("call_B", "result for B"))
    minted_b = _assign_stable_message_ids(client_b.messages)

    assert minted_a == 2 and minted_b == 2, "the real allocator must mint colliding ids"
    assert [row.get("id") for row in client_a.messages] == [row.get("id") for row in client_b.messages]

    client_a.save(append_intent=True)
    client_b.save(append_intent=True)

    loaded = Session.load(sid)
    invocations = {
        call["id"]
        for row in loaded.messages
        for call in (row.get("tool_calls") or [])
        if isinstance(call, dict) and call.get("id")
    }
    results = {
        row.get("tool_call_id")
        for row in loaded.messages
        if row.get("tool_call_id")
    }
    assert invocations == {"call_A", "call_B"}
    assert results == {"call_A", "call_B"}
    assert results == invocations


def test_single_writer_tool_pair_control_stays_coherent(temp_session_dir):
    """Control: one writer's invocation/result pair survives a durable reload."""
    from api.streaming import _assign_stable_message_ids

    sid = "sess_tool_pair_control_1"
    base = Session(session_id=sid)
    base.messages = [{"role": "user", "content": "question", "id": 1}]
    base.context_messages = [{"role": "user", "content": "question", "id": 1}]
    base.save()

    writer = Session.load(sid)
    writer.messages.append(_invocation("call_solo"))
    writer.messages.append(_tool_result("call_solo", "solo result"))
    _assign_stable_message_ids(writer.messages)
    writer.save(append_intent=True)

    loaded = Session.load(sid)
    results = {row.get("tool_call_id") for row in loaded.messages if row.get("tool_call_id")}
    invocations = {
        call["id"]
        for row in loaded.messages
        for call in (row.get("tool_calls") or [])
        if isinstance(call, dict) and call.get("id")
    }
    assert results == {"call_solo"}
    assert invocations == {"call_solo"}
