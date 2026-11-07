"""#7653 re-gate round 14 — hydration keeps one card per occurrence (item 3 tail).

The 10-19 re-gate (item 3) found a reused tool id still collapsing to one card
after reload, in three places: the saved summary, the journal replay, and the
render/dedup key. Round 11 threaded an occurrence through the paired live
events and the browser key; this round closes the server tail:

- ``_run_journal_live_snapshot`` (journal replay) now carries the event's
  ``occurrence`` onto the rebuilt call, so a reused id's second occurrence
  survives a replay as its own row instead of an occurrence-less one.
- ``_anchor_scene_row_key`` scopes the row key by the OWNING ASSISTANT
  MESSAGE (``tool:<tid>@<owner>``), so two occurrences of one tid — the
  reviewer's ``echo FIRST`` then ``exit 3`` on one ``call_0`` — no longer
  merge into the first card and take its verdict upgrade.

That owner scoping surfaced one shape the round had to keep working: the
runtime journal's persisted rows carry NO owner (the stream never learned
one) while the hydration rebuild always names one, so the two spellings of the
SAME occurrence never share a key. ``_anchor_scene_row_tid_fallback_index``
merges them when the tid has exactly one entry in the seen map, and refuses
when the id is genuinely reused (several entries) — which is the case the
owner scoping exists for.

These tests drive the REAL ``_anchor_scene_row_owner_index``,
``_anchor_scene_row_key``, ``_anchor_scene_row_tid_fallback_index`` and
``_hydrate_anchor_activity_scenes`` (api/routes.py) plus the real journal
replay (``_run_journal_live_snapshot``).
"""

from __future__ import annotations

import json

from api import routes


# ---------------------------------------------------------------------------
# _anchor_scene_row_owner_index
# ---------------------------------------------------------------------------


def test_owner_index_reads_top_level_group_and_payload():
    top = routes._anchor_scene_row_owner_index({"assistant_msg_idx": 3})
    group = routes._anchor_scene_row_owner_index({"group": {"assistant_msg_idx": 5}})
    payload = routes._anchor_scene_row_owner_index({"payload": {"assistant_msg_idx": 7}})
    assert (top, group, payload) == (3, 5, 7)


def test_owner_index_prefers_top_level_then_group_then_payload():
    row = {
        "assistant_msg_idx": 1,
        "group": {"assistant_msg_idx": 2},
        "payload": {"assistant_msg_idx": 3},
    }
    assert routes._anchor_scene_row_owner_index(row) == 1
    assert routes._anchor_scene_row_owner_index({"group": {"assistant_msg_idx": 2}, "payload": {"assistant_msg_idx": 3}}) == 2


def test_owner_index_rejects_non_dicts_bools_and_junk():
    assert routes._anchor_scene_row_owner_index(None) is None
    assert routes._anchor_scene_row_owner_index("tool:call-1") is None
    assert routes._anchor_scene_row_owner_index({"assistant_msg_idx": True}) is None
    assert routes._anchor_scene_row_owner_index({"assistant_msg_idx": "abc"}) is None
    assert routes._anchor_scene_row_owner_index({}) is None


# ---------------------------------------------------------------------------
# _anchor_scene_row_key — owner scoping
# ---------------------------------------------------------------------------


def _tool_row(tid: str, owner=None, *, occurrence=None) -> dict:
    row = {
        "role": "tool",
        "kind": "tool_completed",
        "tool_call_id": tid,
        "name": "terminal",
        "done": True,
    }
    if owner is not None:
        row["group"] = {"assistant_msg_idx": owner}
    if occurrence is not None:
        row.setdefault("payload", {})["occurrence"] = occurrence
    return row


def test_row_key_scopes_by_owner_and_keeps_bare_for_ownerless():
    assert routes._anchor_scene_row_key(_tool_row("call-1", owner=1)) == "tool:call-1@1"
    assert routes._anchor_scene_row_key(_tool_row("call-1", owner=4)) == "tool:call-1@4"
    assert routes._anchor_scene_row_key(_tool_row("call-1")) == "tool:call-1"


def test_row_key_same_owner_same_key_different_owner_different_key():
    first = routes._anchor_scene_row_key(_tool_row("call_0", owner=1))
    same_occurrence = routes._anchor_scene_row_key(_tool_row("call_0", owner=1))
    second_occurrence = routes._anchor_scene_row_key(_tool_row("call_0", owner=4))
    assert first == same_occurrence  # the same occurrence seen twice still collapses
    assert first != second_occurrence  # two occurrences of one tid do NOT collapse


# ---------------------------------------------------------------------------
# _anchor_scene_row_tid_fallback_index — bare <-> owner merge chain
# ---------------------------------------------------------------------------


def test_tid_fallback_merges_bare_row_into_the_single_owner_scoped_entry():
    seen = {"tool:call-1@1": 0}
    assert routes._anchor_scene_row_tid_fallback_index("tool:call-1", seen) == 0


def test_tid_fallback_merges_owner_scoped_row_into_the_single_bare_entry():
    seen = {"tool:call-1": 2}
    assert routes._anchor_scene_row_tid_fallback_index("tool:call-1@1", seen) == 2


def test_tid_fallback_refuses_when_the_id_is_genuinely_reused():
    seen = {"tool:call_0@1": 0, "tool:call_0@4": 1}
    assert routes._anchor_scene_row_tid_fallback_index("tool:call-0", seen) is None
    assert routes._anchor_scene_row_tid_fallback_index("tool:call_0", seen) is None


def test_tid_fallback_ignores_non_tool_keys_and_other_tids():
    seen = {"tool:call-1@1": 0, "prose:abc": 1}
    assert routes._anchor_scene_row_tid_fallback_index("prose:abc", seen) is None
    assert routes._anchor_scene_row_tid_fallback_index("tool:call-2", seen) is None
    assert routes._anchor_scene_row_tid_fallback_index("", seen) is None


# ---------------------------------------------------------------------------
# _hydrate_anchor_activity_scenes — end-to-end merge semantics
# ---------------------------------------------------------------------------


def _journal_scene_with_one_tool(tid: str = "call-1") -> dict:
    """A runtime-journal scene: its tool row carries NO owner (stream shape)."""
    return {
        "activity_rows": [
            {
                "role": "tool",
                "kind": "tool_completed",
                "status": "completed",
                "tool_call_id": tid,
                "name": "terminal",
                "args": {"command": "pytest"},
                "done": True,
            }
        ]
    }


def _messages_for_turn(tid: str = "call-1", assistant_idx: int = 1) -> list:
    return [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "progress",
            "tool_calls": [
                {
                    "id": tid,
                    "name": "terminal",
                    "args": {"command": "pytest"},
                    "preview": "pytest",
                    "snippet": "ok",
                }
            ],
        },
        {"role": "tool", "tool_call_id": tid, "content": "ok"},
        {"role": "assistant", "content": "tail"},
        {"role": "assistant", "content": "Final answer."},
    ]


def _hydrate_one_turn(scene: dict, messages: list, *, final_idx: int = 4) -> dict:
    message_ref = routes._assistant_anchor_scene_message_ref(messages[final_idx])
    records = {
        message_ref: {
            "version": "anchor_activity_scene_record_v1",
            "message_index": final_idx,
            "message_ref": message_ref,
            "stream_id": "stream-round14",
            "scene": json.loads(json.dumps(scene)),
        }
    }
    hydrated = routes._hydrate_anchor_activity_scenes(messages, records)
    return hydrated[final_idx]["_anchor_activity_scene"]


def test_hydration_merges_the_ownerless_runtime_row_into_the_rebuilt_occurrence():
    """The round-14 regression: a runtime persisted row (no owner) and the
    hydration rebuild (owner 1) are the SAME occurrence and must stay one card."""
    settled = _hydrate_one_turn(_journal_scene_with_one_tool(), _messages_for_turn())
    tool_rows = [r for r in settled["activity_rows"] if r.get("role") == "tool"]
    assert len(tool_rows) == 1, f"expected one tool card, got {len(tool_rows)}"
    assert tool_rows[0]["payload"].get("tid") == "call-1"


def test_hydration_keeps_two_occurrences_of_one_tid_apart():
    """Item 3 itself: two owners of one tid are two occurrences — two cards."""
    scene = {
        "activity_rows": [
            {
                "role": "tool",
                "kind": "tool_completed",
                "status": "completed",
                "tool_call_id": "call_0",
                "name": "terminal",
                "args": {"command": "echo FIRST"},
                "group": {"assistant_msg_idx": 1},
                "done": True,
            },
            {
                "role": "tool",
                "kind": "tool_completed",
                "status": "error",
                "tool_call_id": "call_0",
                "name": "terminal",
                "args": {"command": "exit 3"},
                "group": {"assistant_msg_idx": 4},
                "done": True,
            },
        ]
    }
    messages = _messages_for_turn(tid="call_0")
    messages[1]["tool_calls"] = [
        {"id": "call_0", "name": "terminal", "args": {"command": "echo FIRST"}, "snippet": "FIRST"}
    ]
    # a second assistant owning the same tid
    messages.insert(
        4,
        {
            "role": "assistant",
            "content": "again",
            "tool_calls": [
                {"id": "call_0", "name": "terminal", "args": {"command": "exit 3"}, "snippet": "3"}
            ],
        },
    )
    messages.append({"role": "assistant", "content": "Final answer."})
    settled = _hydrate_one_turn(scene, messages, final_idx=5)
    tool_rows = [r for r in settled["activity_rows"] if r.get("role") == "tool"]
    assert len(tool_rows) == 2, f"expected two tool cards, got {len(tool_rows)}"
    keys = {routes._anchor_scene_row_key(r) for r in tool_rows}
    assert keys == {"tool:call_0@1", "tool:call_0@4"}


# ---------------------------------------------------------------------------
# journal replay carries the occurrence
# ---------------------------------------------------------------------------


def test_journal_replay_carries_the_occurrence_onto_the_rebuilt_call(tmp_path, monkeypatch):
    from api import models
    from api.run_journal import RunJournalWriter

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)

    stream_id = "stream-round14-replay"
    writer = RunJournalWriter("sess-round14", stream_id, session_dir=session_dir)
    writer.append_sse_event(
        "tool",
        {"name": "terminal", "tid": "call_0", "occurrence": 0, "args": {"command": "echo FIRST"}},
    )
    writer.append_sse_event(
        "tool_complete",
        {"name": "terminal", "tid": "call_0", "occurrence": 0, "preview": "FIRST"},
    )
    writer.append_sse_event(
        "tool",
        {"name": "terminal", "tid": "call_0", "occurrence": 1, "args": {"command": "exit 3"}},
    )
    writer.append_sse_event(
        "tool_complete",
        {"name": "terminal", "tid": "call_0", "occurrence": 1, "preview": "3", "is_error": True},
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    assert snapshot is not None
    scene = snapshot["anchor_activity_scene"]
    tool_rows = [r for r in scene["activity_rows"] if r.get("role") == "tool"]
    occurrences = sorted(
        (r.get("payload") or {}).get("occurrence") for r in tool_rows
    )
    assert occurrences == [0, 1], (
        "the journal replay must carry each event's occurrence, so a reused id's "
        f"second occurrence survives as its own row (got {occurrences})"
    )
