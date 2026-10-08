"""Selected-view authority and SQLite history on both sides of a real Stop."""

import copy

import pytest

from api import models, session_ops
from api.streaming import _new_turn_context_from_messages, _sanitize_messages_for_agent
from tests.test_cancel_restart_journal_recovery import _isolated_state  # noqa: F401
from tests.test_cancelled_journal_owner_occurrences import _recover
from tests.test_cancelled_stream_history_readers import _worker
from tests.test_webui_state_db_reconciliation import _make_state_db


def _contents(rows):
    return [row.get("content") for row in rows]


@pytest.mark.parametrize("projection", ["exact", "copy", "carrierless"])
def test_complete_legacy_clock_collision_history_keeps_cancelled_replay_out(projection):
    older = [
        {"role": "user", "content": "continue", "timestamp": 10},
        {"role": "assistant", "content": "OLDER_ANSWER", "timestamp": 11},
    ]
    owner = older + [
        {"role": "user", "content": "continue", "timestamp": 10},
        {
            "role": "assistant",
            "content": "SAVED_PARTIAL",
            "timestamp": 12,
            "_partial": True,
        },
        {"role": "assistant", "content": "Cancelled", "timestamp": 13, "_error": True},
    ]
    selected = owner if projection == "exact" else copy.deepcopy(owner)
    if projection == "carrierless":
        selected = [row for row in selected if not row.get("_error")]
    state = copy.deepcopy(older) + [
        copy.deepcopy(owner[2]),
        {"role": "assistant", "content": "CANCELLED_RAW_REPLAY", "timestamp": 14},
    ]
    assert models._selected_history_owns_live_partial(selected, owner)
    merged = models.merge_session_messages_append_only(
        selected,
        state,
        incoming_provenance="state_db",
        cancelled_journal_owner_messages=owner,
    )
    history = _sanitize_messages_for_agent(
        _new_turn_context_from_messages(merged, "NEXT_REQUEST")
    )
    assert "CANCELLED_RAW_REPLAY" not in _contents(merged)
    assert "CANCELLED_RAW_REPLAY" not in _contents(history)
    assert _contents(history).count("continue") == 2
    assert "SAVED_PARTIAL" in _contents(history)


@pytest.mark.requires_agent_modules
@pytest.mark.parametrize("placement", ["after-cached", "between-cached"])
@pytest.mark.parametrize("view", ["full", "deferred"])
def test_pre_stop_sqlite_gateway_pair_survives_regeneration_and_real_next_worker(
    tmp_path, monkeypatch, placement, view
):
    sid = "pre-stop-" + placement + "-" + view
    db = tmp_path / "state.db"
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    prior = [
        {"role": "user", "content": "PRIOR_Q", "timestamp": 1},
        {"role": "assistant", "content": "PRIOR_A", "timestamp": 2},
    ]
    gateway = [
        {"role": "user", "content": "GATEWAY_Q_BEFORE_STOP", "timestamp": 3},
        {"role": "assistant", "content": "GATEWAY_A_BEFORE_STOP", "timestamp": 4},
    ]
    cached_tail = (
        [
            {"role": "user", "content": "CACHED_Q", "timestamp": 5},
            {"role": "assistant", "content": "CACHED_A", "timestamp": 6},
        ]
        if placement == "between-cached"
        else []
    )
    session, owner = _recover(sid, prior + cached_tail)
    if view == "deferred":
        session.context_messages = copy.deepcopy(prior + cached_tail)
        session.save(touch_updated_at=False)
    _make_state_db(
        db,
        sid,
        prior
        + gateway
        + cached_tail
        + [
            owner,
            {"role": "assistant", "content": "CANCELLED_RAW_REPLAY", "timestamp": 11},
            {"role": "tool", "content": "CANCELLED_TOOL_REPLAY", "timestamp": 12},
        ],
    )
    expected_prefix = _contents(prior + gateway + cached_tail)
    for prefer_context in (False, True):
        merged = models.reconciled_state_db_messages_for_session(
            session, prefer_context=prefer_context
        )
        assert _contents(merged)[: len(expected_prefix)] == expected_prefix
        assert not {"CANCELLED_RAW_REPLAY", "CANCELLED_TOOL_REPLAY"} & set(
            _contents(merged)
        )
    for rows in session_ops.regeneration_state(session, use_sidecar=True):
        assert _contents(rows)[: len(expected_prefix)] == expected_prefix
    captured = []
    _worker(monkeypatch, tmp_path, session, "NEXT_REQUEST", captured)
    assert _contents(captured[0])[: len(expected_prefix)] == expected_prefix
    assert not {"CANCELLED_RAW_REPLAY", "CANCELLED_TOOL_REPLAY"} & set(
        _contents(captured[0])
    )


def test_prefix_recovery_keeps_every_earlier_cancelled_execution_excluded():
    prior = [{"role": "user", "content": "PRIOR", "timestamp": 1}]
    old = [
        {"role": "user", "content": "OLD_STOP", "timestamp": 2},
        {
            "role": "assistant",
            "content": "OLD_SAVED",
            "timestamp": 3,
            "_recovered_from_cancel_journal": True,
        },
        {"role": "assistant", "content": "Cancelled", "timestamp": 4, "_error": True},
    ]
    new = [
        {"role": "user", "content": "NEW_STOP", "timestamp": 10},
        {
            "role": "assistant",
            "content": "NEW_SAVED",
            "timestamp": 11,
            "_recovered_from_cancel_journal": True,
        },
        {"role": "assistant", "content": "Cancelled", "timestamp": 12, "_error": True},
    ]
    gateway = [
        {"role": "user", "content": "GATEWAY", "timestamp": 5},
        {"role": "assistant", "content": "GATEWAY_ANSWER", "timestamp": 6},
    ]
    state = (
        prior
        + [
            old[0],
            {"role": "assistant", "content": "OLD_RAW", "timestamp": 3},
            {"role": "tool", "content": "OLD_RAW_TOOL", "timestamp": 4},
        ]
        + gateway
        + [new[0], {"role": "assistant", "content": "NEW_RAW", "timestamp": 11}]
    )
    merged = models.merge_session_messages_append_only(
        prior + old + new, state, incoming_provenance="state_db"
    )
    assert not {"OLD_RAW", "OLD_RAW_TOOL", "NEW_RAW"} & set(_contents(merged))
    assert _contents(merged).count("GATEWAY_ANSWER") == 1
    assert _contents(merged).index("OLD_SAVED") < _contents(merged).index("GATEWAY")


@pytest.mark.parametrize("shape", ["tool-mismatch", "split-block"])
def test_prefix_recovery_cannot_rebind_or_split_saved_tool_block(shape):
    call = {
        "role": "assistant",
        "content": "",
        "timestamp": 2,
        "tool_calls": [
            {
                "id": "saved-call",
                "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            }
        ],
    }
    tool = {
        "role": "tool",
        "content": "SAVED_RESULT",
        "tool_call_id": "saved-call",
        "timestamp": 4,
    }
    prior = [{"role": "user", "content": "PRIOR", "timestamp": 1}, call, tool]
    owner = [
        {"role": "user", "content": "STOP", "timestamp": 10},
        {
            "role": "assistant",
            "content": "STOP_SAVED",
            "timestamp": 11,
            "_recovered_from_cancel_journal": True,
        },
        {"role": "assistant", "content": "Cancelled", "timestamp": 12, "_error": True},
    ]
    state_call = copy.deepcopy(call)
    gap = [{"role": "user", "content": "GAP", "timestamp": 3}]
    if shape == "tool-mismatch":
        state_call["tool_calls"][0]["id"] = "foreign-call"
        gap = [
            {
                "role": "tool",
                "content": "FOREIGN_RESULT",
                "tool_call_id": "foreign-call",
                "timestamp": 3,
            }
        ]
    state = [
        prior[0],
        state_call,
        *gap,
        tool,
        owner[0],
        {"role": "assistant", "content": "RAW", "timestamp": 11},
    ]
    merged = models.merge_session_messages_append_only(
        prior + owner, state, incoming_provenance="state_db"
    )
    assert merged == prior + owner


@pytest.mark.parametrize(
    "shape",
    [
        "older-only",
        "reordered",
        "partial-removed",
        "foreign-token",
        "foreign-id",
        "invalid-clock",
    ],
)
def test_legacy_clock_collision_copy_requires_complete_compatible_saved_history(shape):
    owner = [
        {"role": "user", "content": "continue", "timestamp": 10},
        {"role": "assistant", "content": "OLDER", "timestamp": 11},
        {"role": "user", "content": "continue", "timestamp": 10},
        {"role": "assistant", "content": "PARTIAL", "timestamp": 12, "_partial": True},
        {"role": "assistant", "content": "Cancelled", "timestamp": 13, "_error": True},
    ]
    selected = copy.deepcopy(owner)
    if shape == "older-only":
        selected = selected[:2]
    elif shape == "reordered":
        selected[0], selected[1] = selected[1], selected[0]
    elif shape == "partial-removed":
        selected[3].pop("_partial")
    elif shape == "foreign-token":
        owner[2]["_active_turn_token"] = "current"
        selected[2]["_active_turn_token"] = "foreign"
    elif shape == "foreign-id":
        owner[2]["_state_db_row_id"] = 42
        selected[2]["_state_db_row_id"] = 43
    else:
        selected[2]["timestamp"] = True
    assert not models._selected_history_owns_live_partial(selected, owner)


def test_duplicate_selected_private_ids_cannot_authorize_prefix_gap():
    local = [
        {"role": "user", "content": "PRIOR", "timestamp": 1, "_state_db_row_id": 1},
        {
            "role": "assistant",
            "content": "PRIOR_ANSWER",
            "timestamp": 2,
            "_state_db_row_id": 1,
        },
    ]
    owner = [
        {"role": "user", "content": "STOP", "timestamp": 10},
        {
            "role": "assistant",
            "content": "SAVED",
            "timestamp": 11,
            "_recovered_from_cancel_journal": True,
        },
        {"role": "assistant", "content": "Cancelled", "timestamp": 12, "_error": True},
    ]
    source = [
        local[0],
        {**local[1], "_state_db_row_id": 2},
        {"role": "user", "content": "GAP", "timestamp": 3},
        owner[0],
    ]
    assert (
        models.merge_session_messages_append_only(
            local + owner, source, incoming_provenance="state_db"
        )
        == local + owner
    )


@pytest.mark.parametrize("shape", ["cross-id-conflict", "ambiguous-clock"])
def test_conflicting_prefix_occurrences_cannot_be_spliced_beside_saved_rows(shape):
    local = [
        {"role": "user", "content": "PRIOR", "timestamp": 1},
        {
            "role": "assistant",
            "content": "SAVED",
            "timestamp": 2,
            "_state_db_row_id": 2,
        },
    ]
    owner = [
        {"role": "user", "content": "STOP", "timestamp": 10},
        {
            "role": "assistant",
            "content": "STOP_SAVED",
            "timestamp": 11,
            "_recovered_from_cancel_journal": True,
        },
        {"role": "assistant", "content": "Cancelled", "timestamp": 12, "_error": True},
    ]
    if shape == "cross-id-conflict":
        source = [local[0], {**local[1], "content": "FOREIGN", "timestamp": 3}]
    else:
        source = [local[0], local[1], {**local[1], "_state_db_row_id": 3}]
    source += [{"role": "user", "content": "GAP", "timestamp": 4}, owner[0]]
    assert (
        models.merge_session_messages_append_only(
            local + owner, source, incoming_provenance="state_db"
        )
        == local + owner
    )


@pytest.mark.requires_agent_modules
@pytest.mark.parametrize("old_kind", ["journal", "live-partial"])
@pytest.mark.parametrize("interleaved", [False, True])
def test_actual_two_stops_do_not_promote_older_cancelled_raw_prefix(
    tmp_path, monkeypatch, old_kind, interleaved
):
    from api import config, streaming
    from api.run_journal import RunJournalWriter
    from tests.test_cancel_restart_journal_recovery import (
        _start_cancelled_turn,
        _simulate_restart,
    )

    sid = "author-multistop-" + old_kind + str(interleaved)
    db = tmp_path / "state.db"
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    prior = [
        {"role": "user", "content": "PRIOR_Q", "timestamp": 1},
        {"role": "assistant", "content": "PRIOR_A", "timestamp": 2},
    ]
    oldstream = sid + "-old"
    s = _start_cancelled_turn(sid, oldstream)
    s.pending_started_at = 3.0
    s.pending_user_message = "OLD_STOP"
    s.messages = copy.deepcopy(prior)
    s.context_messages = copy.deepcopy(prior)
    s.save()
    if old_kind == "live-partial":
        config.STREAM_PARTIAL_TEXT[oldstream] = "OLD_SAVED_OUTPUT"
    assert streaming.cancel_stream(oldstream)
    if old_kind == "journal":
        RunJournalWriter(sid, oldstream).append_sse_event(
            "token", {"text": "OLD_SAVED_OUTPUT"}
        )
    _simulate_restart()
    older = models.get_session(sid)
    old_messages = copy.deepcopy(older.messages)
    old_context = [
        copy.deepcopy(row) for row in older.messages if not row.get("_error")
    ]
    old_owner = next(row for row in reversed(old_messages) if row.get("role") == "user")
    local_only = (
        [
            {"role": "user", "content": "LOCAL_AFTER_OLD_Q", "timestamp": 8},
            {"role": "assistant", "content": "LOCAL_AFTER_OLD_A", "timestamp": 9},
        ]
        if interleaved
        else []
    )
    old_messages.extend(copy.deepcopy(local_only))
    old_context.extend(copy.deepcopy(local_only))
    newstream = sid + "-new"
    s = _start_cancelled_turn(sid, newstream)
    s.pending_started_at = 10.0
    s.pending_user_message = "NEW_STOP"
    s.messages = old_messages
    s.context_messages = old_context
    s.save()
    assert streaming.cancel_stream(newstream)
    RunJournalWriter(sid, newstream).append_sse_event(
        "token", {"text": "NEW_SAVED_OUTPUT"}
    )
    _simulate_restart()
    s = models.get_session(sid)
    new_owner = next(row for row in reversed(s.messages) if row.get("role") == "user")
    _make_state_db(
        db,
        sid,
        [
            *prior,
            {
                "role": "user",
                "content": "OLD_STOP",
                "timestamp": old_owner["timestamp"],
            },
            {"role": "assistant", "content": "OLD_CANCELLED_RAW", "timestamp": 4},
            {
                "role": "tool",
                "content": "OLD_CANCELLED_TOOL_RAW",
                "timestamp": 5,
                "tool_call_id": "old-raw-id",
            },
            {"role": "user", "content": "BETWEEN_STOPS_GATEWAY_Q", "timestamp": 6},
            {"role": "assistant", "content": "BETWEEN_STOPS_GATEWAY_A", "timestamp": 7},
            {
                "role": "user",
                "content": "NEW_STOP",
                "timestamp": new_owner["timestamp"],
            },
            {"role": "assistant", "content": "NEW_CANCELLED_RAW", "timestamp": 11},
        ],
    )
    display = models.reconciled_state_db_messages_for_session(s)
    context = models.reconciled_state_db_messages_for_session(s, prefer_context=True)
    captured = []
    _worker(monkeypatch, tmp_path, s, "NEXT", captured)
    history = captured[0]
    for name, rows in [
        ("display", display),
        ("context", context),
        ("provider", history),
    ]:
        text = [row.get("content") for row in rows]
        assert not {
            "OLD_CANCELLED_RAW",
            "OLD_CANCELLED_TOOL_RAW",
            "NEW_CANCELLED_RAW",
        } & set(text), (name, text)
        assert text.count("OLD_SAVED_OUTPUT") == text.count("NEW_SAVED_OUTPUT") == 1, (
            name,
            text,
        )
        assert (
            text.count("BETWEEN_STOPS_GATEWAY_Q")
            == text.count("BETWEEN_STOPS_GATEWAY_A")
            == 1
        ), (name, text)
        assert text.index("OLD_STOP") < text.index("OLD_SAVED_OUTPUT") < text.index(
            "BETWEEN_STOPS_GATEWAY_Q"
        ), (name, text)
        if interleaved:
            assert (
                text.index("BETWEEN_STOPS_GATEWAY_A")
                < text.index("LOCAL_AFTER_OLD_Q")
                < text.index("LOCAL_AFTER_OLD_A")
                < text.index("NEW_STOP")
            ), (name, text)


@pytest.mark.requires_agent_modules
@pytest.mark.parametrize("placement", ["tail", "between-anchors"])
@pytest.mark.parametrize("shape", ["earlier", "later", "native-tool", "late-output", "multiple"])
def test_partially_mirrored_pre_stop_turns_keep_execution_order_in_real_worker(
    tmp_path, monkeypatch, placement, shape
):
    sid = "author-prestop-interleaved-" + placement + "-" + shape
    db = tmp_path / "state.db"
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    prior = [
        {"role": "user", "content": "SHARED_Q", "timestamp": 1},
        {"role": "assistant", "content": "SHARED_A", "timestamp": 2},
    ]
    sqlite_only = [
        {"role": "user", "content": "SQLITE_ONLY_Q", "timestamp": 3},
        {"role": "assistant", "content": "SQLITE_ONLY_A", "timestamp": 4},
    ]
    local_only = [
        {"role": "user", "content": "SIDECAR_ONLY_Q", "timestamp": 5},
        {"role": "assistant", "content": "SIDECAR_ONLY_A", "timestamp": 6},
    ]
    if shape == "later":
        sqlite_only[0]["timestamp"], sqlite_only[1]["timestamp"] = 5, 6
        local_only[0]["timestamp"], local_only[1]["timestamp"] = 3, 4
    if shape == "native-tool":
        local_only[1:1] = [
            {
                "role": "assistant", "content": "LOCAL_CALL", "timestamp": 5.1,
                "tool_calls": [{"id": "local-call", "type": "function", "function": {
                    "name": "read", "arguments": "{}",
                }}],
            },
            {
                "role": "tool", "content": "LOCAL_RESULT", "timestamp": 5.2,
                "tool_call_id": "local-call",
            },
        ]
    if shape == "late-output":
        prior.append({"role": "assistant", "content": "PRIOR_LATE_OUTPUT", "timestamp": 99})
    second = (
        [
            {"role": "user", "content": "SQLITE_SECOND_Q", "timestamp": 6.1},
            {"role": "assistant", "content": "SQLITE_SECOND_A", "timestamp": 6.2},
        ]
        if shape == "multiple"
        else []
    )
    later = (
        [
            {"role": "user", "content": "LATER_Q", "timestamp": 7},
            {"role": "assistant", "content": "LATER_A", "timestamp": 8},
        ]
        if placement == "between-anchors"
        else []
    )
    session, owner = _recover(sid, prior + local_only + later)
    _make_state_db(
        db,
        sid,
        prior
        + sqlite_only
        + second
        + later
        + [owner, {"role": "assistant", "content": "CANCELLED_RAW", "timestamp": 11}],
    )
    ordered = local_only + sqlite_only if shape == "later" else sqlite_only + local_only
    expected = _contents(prior + ordered + second + later)
    projections = [
        models.reconciled_state_db_messages_for_session(
            session, prefer_context=prefer_context
        )
        for prefer_context in (False, True)
    ]
    captured = []
    _worker(monkeypatch, tmp_path, session, "NEXT_REQUEST", captured)
    for merged in [*projections, captured[0]]:
        assert _contents(merged)[: len(expected)] == expected
        assert "CANCELLED_RAW" not in _contents(merged)
    if shape == "native-tool":
        call_index = _contents(captured[0]).index("LOCAL_CALL")
        assert captured[0][call_index + 1]["tool_call_id"] == "local-call"


@pytest.mark.requires_agent_modules
@pytest.mark.parametrize("clock", [3, False, None])
def test_ambiguous_pre_stop_user_clocks_do_not_authorize_interleaving(
    tmp_path, monkeypatch, clock
):
    sid = "author-prestop-ambiguous-" + str(clock)
    db = tmp_path / "state.db"
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    prior = [
        {"role": "user", "content": "SHARED_Q", "timestamp": 1},
        {"role": "assistant", "content": "SHARED_A", "timestamp": 2},
    ]
    local = [
        {"role": "user", "content": "LOCAL_ONLY_Q", "timestamp": clock},
        {"role": "assistant", "content": "LOCAL_ONLY_A", "timestamp": 6},
    ]
    session, owner = _recover(sid, prior + local)
    _make_state_db(db, sid, [
        *prior,
        {"role": "user", "content": "SQLITE_ONLY_Q", "timestamp": 3},
        {"role": "assistant", "content": "SQLITE_ONLY_A", "timestamp": 4},
        owner,
        {"role": "assistant", "content": "CANCELLED_RAW", "timestamp": 11},
    ])
    projections = [models.reconciled_state_db_messages_for_session(
        session, prefer_context=prefer_context
    ) for prefer_context in (False, True)]
    captured = []
    _worker(monkeypatch, tmp_path, session, "NEXT_REQUEST", captured)
    for rows in [*projections, captured[0]]:
        text = _contents(rows)
        assert text.count("LOCAL_ONLY_Q") == 1
        assert "SQLITE_ONLY_Q" not in text
        assert "CANCELLED_RAW" not in text


@pytest.mark.requires_agent_modules
def test_shared_assistant_anchor_cannot_reparent_distinct_user_turns(
    tmp_path, monkeypatch
):
    sid = "author-prestop-conflicting-anchor-owner"
    db = tmp_path / "state.db"
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    prior = [
        {"role": "user", "content": "SHARED_Q", "timestamp": 1},
        {"role": "assistant", "content": "SHARED_A", "timestamp": 2},
    ]
    answer = {"role": "assistant", "content": "SAME_ANSWER", "timestamp": 6}
    local = [{"role": "user", "content": "LOCAL_ONLY_Q", "timestamp": 5}, answer]
    session, owner = _recover(sid, prior + local)
    _make_state_db(db, sid, [
        *prior,
        {"role": "user", "content": "SQLITE_ONLY_Q", "timestamp": 3},
        answer,
        owner,
        {"role": "assistant", "content": "CANCELLED_RAW", "timestamp": 11},
    ])
    projections = [models.reconciled_state_db_messages_for_session(
        session, prefer_context=prefer_context
    ) for prefer_context in (False, True)]
    captured = []
    _worker(monkeypatch, tmp_path, session, "NEXT_REQUEST", captured)
    for rows in [*projections, captured[0]]:
        text = _contents(rows)
        assert "SQLITE_ONLY_Q" not in text
        assert text.count("SAME_ANSWER") == 1
        assert text.index("LOCAL_ONLY_Q") + 1 == text.index("SAME_ANSWER")
        assert "CANCELLED_RAW" not in text
