"""#7237 round 6 — raw-provenance blocker handoff removed; durable preservation.

Round-6 maintainer blockers (nesquena-hermes 2026-10-12) on head ``48b71cb56``:

**Blocker 1 — adjacent assistant answer lost across save/cold-reload.**
The round-5 ``_blocked_pairs`` handoff preserved an outbound exemption the
installed Agent does not honor: a plain float-clock historical assistant pair
was kept as separate rows in the producer-normalized snapshot, the installed
repair merges that pair unconditionally, the strict-prefix proof failed, and
the follow-up's ANSWER was gone after a real save/cold-reload while the
historical rows and the ``follow-up`` prompt survived.

**Blocker 2 — raw blocker position misattached after the sanitizer replaced
the provisional row.** Sequence user ``question`` → assistant
``provisional`` (``finish_reason='verification_required'``) → assistant
``final`` → assistant ``continuation``: the sanitizer REPLACES the
provisional row, so the raw ``(1,2)`` block index now names the
``final``+``continuation`` pair and keeps it separate while the installed
repair merges it — strict-prefix proof fails and the next exchange's
``follow-up`` / ``next answer`` disappear. (Reviewer's independent replay:
new head FAIL, prior head PASS.)

The fix removes the handoff entirely: the outbound merge mirrors the
installed Agent unconditionally, and durable raw-row addressability is
proven separately on the settle/persist side.

These tests drive the REAL projection (``_sanitize_messages_for_agent``), the
REAL settle core (``_dedupe_replayed_context_messages`` /
``_proven_current_turn_suffix``), the REAL session persistence
(``models.Session.save``) and a REAL cold reload (``models.Session.load``) —
the same functions the streaming worker composes. The installed-Agent half
of the protocol (its unconditional repair merge) is simulated with an
explicit inline merge so the simulation never shares code with the
projection under test.
"""

from __future__ import annotations

import copy

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _user(text, **extra):
    row = {"role": "user", "content": text}
    row.update(extra)
    return row


def _assistant(text, **extra):
    row = {"role": "assistant", "content": text}
    row.update(extra)
    return row


def _sanitize(messages, **kwargs):
    from api.streaming import _sanitize_messages_for_agent

    return _sanitize_messages_for_agent(copy.deepcopy(messages), **kwargs)


def _installed_agent_merge(messages):
    """Mirror the installed Agent's ``repair_message_sequence`` pass-0 on the
    rows this process actually handed it.

    Written inline so the simulation is independent of the projection code
    under test: a ``verification_required`` provisional row is REPLACED by
    its final successor, and every other adjacent bare assistant pair is
    merged unconditionally (joined with a literal ``\\n``, carrying the later
    row's ``reasoning_content`` when the survivor lacks one, dropping the
    survivor's ``api_content`` sidecar when the visible content changed).
    """
    merged: list = []
    for msg in messages:
        if not isinstance(msg, dict):
            merged.append(msg)
            continue
        prev = merged[-1] if merged and isinstance(merged[-1], dict) else None
        if (
            prev is not None
            and prev.get("role") == "assistant"
            and msg.get("role") == "assistant"
        ):
            if prev.get("finish_reason") in ("verification_required", "verify_hook_continue"):
                # Superseded candidate: the final row replaces it.
                merged[-1] = msg
                continue
            survivor = copy.deepcopy(prev)
            prev_content = survivor.get("content")
            new_content = msg.get("content")
            content_rewritten = False
            if isinstance(prev_content, str) and isinstance(new_content, str):
                joined = "\n".join(
                    part for part in (prev_content.strip(), new_content.strip()) if part
                )
                if joined:
                    survivor["content"] = joined
                    content_rewritten = joined != prev_content
            elif not prev_content and new_content is not None:
                survivor["content"] = new_content
                content_rewritten = new_content != prev_content
            if content_rewritten:
                survivor.pop("api_content", None)
            if not survivor.get("reasoning_content") and msg.get("reasoning_content"):
                survivor["reasoning_content"] = msg.get("reasoning_content")
            merged[-1] = survivor
            continue
        merged.append(msg)
    return merged


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


# ---------------------------------------------------------------------------
# Blocker 1: a plain float-clock historical assistant pair loses the next
# exchange's ANSWER across save/cold-reload.
# ---------------------------------------------------------------------------


def test_float_clock_assistant_pair_keeps_the_follow_up_answer_across_cold_load(
    tmp_path, monkeypatch
):
    """Round-6 blocker 1 reproduction (reviewer's controlled replay).

    The raw history holds a plain adjacent assistant pair whose rows carry
    ordinary float clocks (the sqlite REAL round-trip). The round-5 block
    list exempted that pair from the outbound merge; the installed Agent
    merges it unconditionally; the returned prefix then diverges from the
    sent projection and the settle fails closed — dropping ``ANSWER follow-up``
    from the persisted context while the historical rows and the
    ``follow-up`` prompt survive.
    """
    previous_context = [
        _user("first question"),
        _assistant("first answer", timestamp=1.5),
        _assistant("late answer continuation", timestamp=2.5),
        _user("ANSWER follow-up"),
    ]
    # What the streaming worker actually sends the Agent.
    projection = _sanitize(previous_context, normalize_bare_assistant_adjacency=True)
    # What the installed Agent returns: its repair merged the float-clock
    # pair, then the follow-up exchange.
    agent_returned = _installed_agent_merge(projection) + [
        _assistant("THE FOLLOW-UP ANSWER"),
    ]

    persisted = _settle_and_cold_load(
        previous_context, agent_returned, "ANSWER follow-up", projection,
        "r6_float_clock_answer_retention", tmp_path, monkeypatch,
    )

    assert any(
        m.get("role") == "user" and m.get("content") == "ANSWER follow-up"
        for m in persisted
    ), (
        "the follow-up prompt must survive the settle + save + cold-load; "
        f"got {persisted}"
    )
    assert any(
        m.get("role") == "assistant" and m.get("content") == "THE FOLLOW-UP ANSWER"
        for m in persisted
    ), (
        "the follow-up ANSWER must survive the settle + save + cold-load — "
        "the round-5 raw-provenance exemption must not leak into the outbound "
        f"snapshot and fail the strict-prefix proof; got {persisted}"
    )


# ---------------------------------------------------------------------------
# Blocker 2: raw blocker positions misattach after the sanitizer replaces the
# provisional row — the next exchange disappears.
# ---------------------------------------------------------------------------


def test_supersession_continuation_schedule_keeps_the_next_exchange(
    tmp_path, monkeypatch
):
    """Round-6 blocker 2 reproduction (reviewer's controlled replay).

    Four-row schedule user ``question`` → assistant ``provisional``
    (``finish_reason='verification_required'``) → assistant ``final`` →
    assistant ``continuation``. The sanitizer REPLACES the provisional row,
    so the raw ``(1,2)`` block index misattaches to the projected
    ``final``+``continuation`` pair; the installed repair merges that pair
    anyway and the strict-prefix proof fails. The next exchange's
    ``follow-up`` / ``next answer`` must survive and the strict-prefix proof
    must pass.
    """
    previous_context = [
        _user("question"),
        _assistant(
            "provisional answer",
            finish_reason="verification_required",
            timestamp=1.5,
        ),
        _assistant("final answer", timestamp=2.0),
        _assistant("continuation", timestamp=3.0),
    ]
    projection = _sanitize(previous_context, normalize_bare_assistant_adjacency=True)
    # The installed Agent merged the final+continuation pair and answered.
    agent_returned = _installed_agent_merge(projection) + [
        _user("follow-up"),
        _assistant("next answer"),
    ]

    from api.streaming import _proven_current_turn_suffix

    proven = _proven_current_turn_suffix(projection, agent_returned)
    assert proven is not None, (
        "the strict-prefix proof must pass: the outbound snapshot must mirror "
        "the installed Agent's unconditional merge so the returned prefix "
        "matches the sent projection exactly"
    )

    persisted = _settle_and_cold_load(
        previous_context, agent_returned, "follow-up", projection,
        "r6_supersession_continuation_exchange", tmp_path, monkeypatch,
    )
    assert any(
        m.get("role") == "user" and m.get("content") == "follow-up"
        for m in persisted
    ), f"the follow-up prompt must survive; got {persisted}"
    assert any(
        m.get("role") == "assistant" and m.get("content") == "next answer"
        for m in persisted
    ), f"the next answer must survive; got {persisted}"


# ---------------------------------------------------------------------------
# Source shape: the raw→projected blocker handoff is gone for good.
# ---------------------------------------------------------------------------


def test_the_raw_provenance_blocker_handoff_is_removed():
    """Round-6 source shape: no ``_blocked_pairs`` handoff may exist.

    The outbound merge mirrors the installed Agent unconditionally; durable
    raw-row addressability is a property of the settle/persist side, never
    of the producer-normalized snapshot.
    """
    import inspect

    import api.streaming as S

    assert not hasattr(S, "_replay_merge_blocked_by_cancelled_history"), (
        "the raw-provenance block predicate must be removed with the handoff "
        "(reviewer's direction: separate durable raw-row addressability from "
        "the producer-normalized outbound snapshot)"
    )
    src = inspect.getsource(S._sanitize_messages_for_agent)
    assert "_blocked_pairs" not in src, (
        "the projection must not pre-compute a blocked-pair set on RAW rows; "
        "raw indices do not survive the projection's merges/replacements"
    )
    merge_src = inspect.getsource(S._merge_agent_replay_bare_assistant_rows)
    assert "blocked_pairs" not in merge_src, (
        "the bare-assistant merge must take no blocked-pair parameter"
    )
