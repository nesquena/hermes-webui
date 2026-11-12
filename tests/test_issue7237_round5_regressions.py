"""#7237 round 5 — the two regressions the Oct 11 re-gate reproduced.

The 10/11 re-gate replayed the production-composed probes on head
``369431458``: current head **11 failed / 3 passed**, prior-head control
**1 failed / 13 passed**. Both new regression families share one root
cause, and the reviewer supplied a scratch-verified repair direction
(47 tests, including the prior court checks, on that scratch).

**Regression 1 — adjacent assistants lose the next exchange.**
``_sanitize_messages_for_agent`` ran ``_merge_agent_replay_bare_assistant_rows``
BEFORE ``_sanitize_messages_for_api``. The cancelled-history block list
(``_replay_merge_blocked_by_cancelled_history``) then exempted rows whose
provenance the sanitizer would have stripped — so the outbound snapshot
kept adjacent bare assistants as separate rows while the installed Agent
still merges them. The reviewer's controlled replay: the answer to
``ANSWER follow-up`` was gone from the reloaded context.

**Regression 2 — verification supersession is defeated.**
The same pre-sanitizer assistant merge CONCATENATES a provisional
``verification_required`` row with its final replacement. The sanitizer's
own pass-0 merge (which owns the supersession rule: a
``verification_required`` row is REPLACED, not unioned) then never sees the
pair, so the provisional text and its finish reason survive.

The reviewer's direction: keep the user-row normalization where it is,
move the bare-assistant normalization AFTER ``_sanitize_messages_for_api``.
That keeps the block list reading raw provenance (the user merge does not
need it), lets the sanitizer's pass-0 own the assistant-adjacency
contract including supersession, and re-applies the assistant merge only
to the rows the sanitizer deliberately left as a bare pair.

These tests drive the REAL ``_sanitize_messages_for_agent`` and the real
merge/block helpers out of ``api/streaming.py``.
"""

from __future__ import annotations

import copy

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _user(text):
    return {"role": "user", "content": text}


def _assistant(text, **extra):
    row = {"role": "assistant", "content": text}
    row.update(extra)
    return row


def _sanitize(messages, **kwargs):
    from api.streaming import _sanitize_messages_for_agent

    return _sanitize_messages_for_agent(copy.deepcopy(messages), **kwargs)


# ---------------------------------------------------------------------------
# Regression 2: verification supersession.
# ---------------------------------------------------------------------------


def test_a_verification_candidate_is_replaced_not_concatenated():
    """The reviewer's regression 2: provisional + final must not concatenate.

    A ``verification_required`` row followed by its final replacement is a
    supersession: the final row REPLACES the provisional one. The
    pre-sanitizer merge concatenated them, so the surviving row carried the
    provisional text AND its finish reason, and the sanitizer's own
    supersession rule never saw the pair.
    """
    messages = [
        _user("q"),
        _assistant("provisional answer", finish_reason="verification_required"),
        _assistant("final answer", finish_reason="stop"),
        _user("next"),
    ]
    out = _sanitize(messages, normalize_bare_assistant_adjacency=True)
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 1, (
        f"the superseded provisional row must be REPLACED, leaving one "
        f"assistant row; got {assistants}"
    )
    survivor = assistants[0]
    assert survivor.get("content") == "final answer", (
        f"the survivor must be the FINAL candidate, not a concatenation: "
        f"{survivor.get('content')!r}"
    )
    assert survivor.get("finish_reason") != "verification_required", (
        f"the provisional finish reason must not survive the supersession: "
        f"{survivor.get('finish_reason')!r}"
    )


def test_ordinary_adjacent_assistants_still_merge():
    """Control: the adjacency contract the pre-sanitizer merge existed for.

    Two plain adjacent assistant rows must still collapse into one turn, or
    the Agent replay diverges from the installed Agent's own repair.
    """
    messages = [
        _user("q"),
        _assistant("first part"),
        _assistant("second part"),
        _user("next"),
    ]
    out = _sanitize(messages, normalize_bare_assistant_adjacency=True)
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 1, (
        f"two plain adjacent assistants must still merge; got {assistants}"
    )
    content = assistants[0].get("content") or ""
    assert "first part" in content and "second part" in content, (
        f"the merged row must carry both texts; got {content!r}"
    )


def test_the_flag_off_keeps_adjacent_assistants_separate():
    """Control: the #8034 parity contract — the flag still gates the merge."""
    messages = [
        _user("q"),
        _assistant("first part"),
        _assistant("second part"),
        _user("next"),
    ]
    out = _sanitize(messages, normalize_bare_assistant_adjacency=False)
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 2, (
        f"without the flag the rows must stay separate turns (#8034); got "
        f"{assistants}"
    )


# ---------------------------------------------------------------------------
# Regression 1: adjacent assistants keep the next exchange addressable.
# ---------------------------------------------------------------------------


def _settle_and_cold_load(
    previous_context, agent_returned, msg_text, projection,
    session_id, tmp_path, monkeypatch,
):
    """Drive the real settle core, real session save and real cold reload.

    Returns the ``context_messages`` of the cold-loaded session — the exact
    rows a restarting server would feed the next model call.
    """
    from api.streaming import _dedupe_replayed_context_messages

    settled, _protected = _dedupe_replayed_context_messages(
        list(previous_context),
        list(agent_returned),
        msg_text,
        projected_history=list(projection),
    )
    import api.models as models

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    session = models.Session(
        session_id=session_id,
        workspace=str(tmp_path),
        messages=list(settled),
        context_messages=list(settled),
        model="test-model",
        model_provider="test-provider",
    )
    session.save(touch_updated_at=False)
    reloaded = models.Session.load(session_id)
    assert reloaded is not None, "the cold reload must find the persisted session"
    return reloaded.context_messages


def test_a_cancelled_history_row_keeps_the_next_exchange_addressable(
    tmp_path, monkeypatch
):
    """The reviewer's regression 1: the follow-up ANSWER must survive.

    Round-6 rewrite (greptile P2 "Lost replies go unchecked"): the round-5
    test only asserted the follow-up PROMPT survived the projection — never
    the answer. A cancelled-history splice leaves rows with durable
    provenance (``_state_db_row_id``) and a float clock. Round 6 removed the
    raw-provenance exemption from the OUTBOUND snapshot (the installed Agent
    merges the pair unconditionally), so addressability is proven on the
    DURABLE side: both raw rows stay individually present in the persisted
    context, and the follow-up's answer survives the real settle + save +
    cold-load.
    """
    messages = [
        _user("first question"),
        _assistant("first answer"),
        _assistant(
            "late answer continuation",
            _state_db_row_id=41,
            timestamp=1.5,
        ),
        _user("follow-up"),
    ]
    out = _sanitize(messages, normalize_bare_assistant_adjacency=True)
    # The outbound snapshot mirrors the installed Agent's unconditional
    # merge: the cancelled-history pair collapses into one turn.
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 1, (
        f"the outbound snapshot must merge the pair the installed Agent "
        f"merges; got {assistants}"
    )
    assert "first answer" in assistants[0].get("content", ""), (
        f"the merged row must carry the first answer; got {assistants[0]}"
    )
    assert "late answer continuation" in assistants[0].get("content", ""), (
        f"the merged row must carry the continuation; got {assistants[0]}"
    )
    # The provenance that makes the rows addressable is not leaked to the
    # wire: the sanitizer still strips it.
    for row in out:
        assert "_state_db_row_id" not in row, (
            f"internal provenance must not reach the provider payload: {row}"
        )
    # The installed Agent returns the merged projection plus the exchange.
    agent_returned = [
        _user("first question"),
        _assistant("first answer\nlate answer continuation"),
        _user("follow-up"),
        _assistant("THE FOLLOW-UP ANSWER"),
    ]
    persisted = _settle_and_cold_load(
        messages, agent_returned, "follow-up", out,
        "r5_cancelled_history_answer_retention", tmp_path, monkeypatch,
    )
    # Durable addressability: both raw rows remain individually present.
    raw_rows = [
        m for m in persisted
        if m.get("role") == "assistant"
        and m.get("content") in ("first answer", "late answer continuation")
    ]
    assert len(raw_rows) == 2, (
        "the durable raw rows must stay individually addressable in the "
        f"persisted context; got {persisted}"
    )
    assert any(m.get("_state_db_row_id") == 41 for m in raw_rows), (
        "the cancelled-history row must keep its provenance in the durable "
        f"context; got {persisted}"
    )
    # The follow-up prompt AND its answer survive the cold reload.
    assert any(
        m.get("role") == "user" and m.get("content") == "follow-up"
        for m in persisted
    ), f"the follow-up prompt must survive; got {persisted}"
    assert any(
        m.get("role") == "assistant" and m.get("content") == "THE FOLLOW-UP ANSWER"
        for m in persisted
    ), (
        "the follow-up ANSWER must survive the settle + save + cold-load "
        f"(greptile P2); got {persisted}"
    )


def test_a_float_clock_row_is_not_treated_as_a_merge_blocker_only(
    tmp_path, monkeypatch
):
    """Round-6 rewrite: durable raw rows are addressable WITHOUT the block list.

    The round-5 test pinned ``_replay_merge_blocked_by_cancelled_history``
    keeping a float-clock assistant pair separate in the OUTBOUND snapshot.
    Round 6 removes that exemption — the installed Agent merges the pair
    unconditionally, so outbound segmentation was never durable
    addressability, and the reviewer's scratch fixed both blockers by
    removing the handoff. Addressability is now proven on the DURABLE side:
    the raw float-clock rows remain individually present, each with its own
    clock, in the persisted context after the real settle + save + cold-load,
    while the outbound snapshot merges them.
    """
    prev = _assistant("answer", timestamp=1.0)
    msg = _assistant("continuation", timestamp=2.0)
    messages = [_user("q"), prev, msg, _user("follow-up")]
    out = _sanitize(messages, normalize_bare_assistant_adjacency=True)
    # Outbound: the pair merges unconditionally (no raw-provenance block
    # list may keep it separate).
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 1, (
        f"the outbound snapshot must merge the float-clock pair; got {assistants}"
    )
    # The installed Agent returns the merged projection plus the exchange.
    agent_returned = [
        _user("q"),
        _assistant("answer\ncontinuation"),
        _user("follow-up"),
        _assistant("THE ANSWER"),
    ]
    persisted = _settle_and_cold_load(
        messages, agent_returned, "follow-up", out,
        "r5_float_clock_durable_addressability", tmp_path, monkeypatch,
    )
    # Durable addressability: both raw rows survive individually, each with
    # its own float clock, in the persisted context.
    clock_rows = [
        m for m in persisted
        if m.get("role") == "assistant"
        and isinstance(m.get("timestamp"), float)
        and m.get("content") in ("answer", "continuation")
    ]
    assert len(clock_rows) == 2, (
        "the durable float-clock rows must stay individually addressable in "
        f"the persisted context; got {persisted}"
    )
    assert {m.get("content") for m in clock_rows} == {"answer", "continuation"}, (
        f"both raw rows must be present with their own clocks; got {clock_rows}"
    )
    # And the exchange survives the cold reload.
    assert any(
        m.get("role") == "assistant" and m.get("content") == "THE ANSWER"
        for m in persisted
    ), f"the answer must survive the settle + save + cold-load; got {persisted}"


# ---------------------------------------------------------------------------
# Source shape: the ordering the reviewer's direction requires.
# ---------------------------------------------------------------------------


def test_the_assistant_merge_runs_after_the_api_sanitizer():
    """The ordering pin: the sanitizer's pass-0 owns supersession.

    The reviewer's scratch moved the bare-assistant normalization after
    ``_sanitize_messages_for_api`` so the pass-0 merge (which replaces a
    ``verification_required`` row instead of unioning it) sees the original
    pair. If the merge moves back before the sanitizer, regression 2
    reappears.
    """
    from api.streaming import _sanitize_messages_for_agent

    import inspect

    src = inspect.getsource(_sanitize_messages_for_agent)
    sanitizer_at = src.index("_sanitize_messages_for_api(")
    assistant_merge_at = src.index("_merge_agent_replay_bare_assistant_rows(")
    user_merge_at = src.index("_merge_agent_replay_bare_user_rows(")
    assert assistant_merge_at > sanitizer_at, (
        "the bare-assistant normalization must run AFTER the API sanitizer "
        "so the pass-0 merge owns the verification supersession"
    )
    assert user_merge_at < sanitizer_at, (
        "the user-row normalization stays BEFORE the sanitizer (the "
        "reviewer's direction)"
    )
