"""#7237 round 7 — review re-gate findings (nesquena-hermes 2026-10-14).

**Finding 1 — sanitizer-created adjacent users lose the current answer from
model context.** Schedule ``user U1`` → empty partial assistant → ``user U2``
→ ``assistant A2``: the outbound sanitizer removes the empty assistant,
leaving adjacent ``U1``/``U2``. Native repair folds them into one
``U1\\n\\nU2`` user row; the settle projection normalized only assistant
adjacency, so the strict-prefix proof refused the genuine suffix and the
current exchange (``U3`` / ``ANSWER3``) vanished from cold
``context_messages`` while surviving in ``messages``.

These tests drive the REAL projection (``_sanitize_messages_for_agent``), the
REAL settle projection (``_agent_replay_settle_projection``), the REAL settle
core (``_dedupe_replayed_context_messages``) and a REAL cold reload
(``models.Session.load``).
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


def _settle_and_cold_load(
    previous_context, agent_returned, msg_text, projection,
    session_id, tmp_path, monkeypatch,
):
    """Drive the real settle core, real session save and real cold reload."""
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
    session_dir.mkdir(parents=True, exist_ok=True)
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
    return reloaded


def test_sanitizer_created_adjacent_users_keep_the_current_exchange(
    tmp_path, monkeypatch
):
    """Reviewer finding 1 reproduction (real components, no simulation)."""
    from api.streaming import (
        _agent_replay_settle_projection,
        _proven_current_turn_suffix,
    )

    previous_context = [
        _user("U1"),
        _assistant("", _partial=True),
        _user("U2"),
        _assistant("A2"),
    ]
    projection = _sanitize(
        previous_context, normalize_bare_assistant_adjacency=True
    )
    assert [row.get("role") for row in projection] == [
        "user", "user", "assistant",
    ], (
        "precondition: the sanitizer must remove the empty partial and leave "
        f"adjacent user rows; got {projection}"
    )

    settle_projection = _agent_replay_settle_projection(projection)
    # The installed Agent folds the adjacent users (pass-3, literal "\n\n"
    # join) and appends the current exchange.
    agent_returned = [
        _user("U1\n\nU2"),
        _assistant("A2"),
        _user("U3"),
        _assistant("ANSWER3"),
    ]
    proven = _proven_current_turn_suffix(settle_projection, agent_returned)
    assert proven is not None, (
        "the strict-prefix proof must pass: the settle projection must fold "
        "the sanitizer-created adjacent user rows exactly like the installed "
        "repair does, or ownership refuses the genuine suffix and the reply "
        f"is lost; settle projection was {settle_projection}"
    )
    assert [r.get("content") for r in proven] == ["U3", "ANSWER3"]

    reloaded = _settle_and_cold_load(
        previous_context, agent_returned, "U3", settle_projection,
        "r7_adjacent_users_answer_retention", tmp_path, monkeypatch,
    )
    for rows in (reloaded.messages, reloaded.context_messages):
        contents = [r.get("content") for r in rows]
        assert "U3" in contents, (
            f"the current prompt must survive on both surfaces; got {contents}"
        )
        assert "ANSWER3" in contents, (
            "the current ANSWER must survive on both the display and the "
            f"model-facing context; got {contents}"
        )


def test_alternating_history_control_keeps_the_current_exchange(
    tmp_path, monkeypatch
):
    """Positive control: an ordinary alternating history keeps its exchange."""
    from api.streaming import _agent_replay_settle_projection

    previous_context = [_user("Q1"), _assistant("A1")]
    projection = _sanitize(
        previous_context, normalize_bare_assistant_adjacency=True
    )
    settle_projection = _agent_replay_settle_projection(projection)
    agent_returned = [
        *copy.deepcopy(projection),
        _user("U3"),
        _assistant("ANSWER3"),
    ]
    reloaded = _settle_and_cold_load(
        previous_context, agent_returned, "U3", settle_projection,
        "r7_alternating_control", tmp_path, monkeypatch,
    )
    for rows in (reloaded.messages, reloaded.context_messages):
        contents = [r.get("content") for r in rows]
        assert "Q1" in contents and "A1" in contents, contents
        assert "U3" in contents and "ANSWER3" in contents, contents


def test_settle_projection_folds_users_like_the_installed_repair():
    """Unit guard: the settle projection models ALL native repair passes."""
    from api.streaming import _agent_replay_settle_projection

    sent = [
        _user("U1"),
        _user("U2"),
        _assistant("A2"),
    ]
    projected = _agent_replay_settle_projection(sent)
    assert [r.get("content") for r in projected] == ["U1\n\nU2", "A2"], (
        "adjacent plain user rows must fold into one row joined with a "
        f"literal blank line; got {projected}"
    )


# ---------------------------------------------------------------------------
# Finding 2: an independently settled repeated answer keeps both occurrences
# across later turns (outbound history and cold context_messages).
# ---------------------------------------------------------------------------


def _save_and_reload(messages, session_id, tmp_path, monkeypatch):
    import api.models as models

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_dir / "state.db")
    session = models.Session(
        session_id=session_id,
        workspace=str(tmp_path),
        messages=list(messages),
        context_messages=list(messages),
        model="test-model",
        model_provider="test-provider",
    )
    session.save(touch_updated_at=False)
    reloaded = models.Session.load(session_id)
    assert reloaded is not None, "the cold reload must find the persisted session"
    return reloaded


def _build_next_turn_context(session, msg_text):
    """Drive the real pre-turn build: reconcile → new-turn → dedupe."""
    import api.models as models
    from api.streaming import (
        _deduplicate_context_messages,
        _new_turn_context_from_messages,
    )

    reconciled = models.reconciled_state_db_messages_for_session(
        session, prefer_context=True
    )
    return _deduplicate_context_messages(
        _new_turn_context_from_messages(reconciled, msg_text)
    )


def test_repeated_answer_occurrence_survives_later_turns(tmp_path, monkeypatch):
    """Reviewer finding 2: re-ask → save/reload → unrelated turn → save/reload.

    With a prior Q/A the user re-asks the same question and the answer repeats
    verbatim; both assistant occurrences are independently owned (stable ids)
    and must survive the next turn's projection AND the cold reload — a
    content-only collapse treats the second occurrence as replay.
    """
    from api.streaming import (
        _agent_replay_settle_projection,
        _dedupe_replayed_context_messages,
        _deduplicate_context_messages,
    )

    previous_context = [
        _user("question", id=1, timestamp=1),
        _assistant("answer", id=2, timestamp=2),
    ]
    # ── Turn 2: re-ask, repeat answer. Real settle core + cold reload.
    projection = _sanitize(previous_context, normalize_bare_assistant_adjacency=True)
    agent_returned = [
        *copy.deepcopy(projection),
        _user("question", id=3, timestamp=3, _active_turn_token="t2:3"),
        _assistant("answer", id=4, timestamp=4),
    ]
    settled, protected = _dedupe_replayed_context_messages(
        previous_context, agent_returned, "question",
        projected_history=_agent_replay_settle_projection(projection),
    )
    settled = _deduplicate_context_messages(settled, protected)
    assert [r.get("content") for r in settled].count("answer") == 2, (
        "both assistant occurrences must initially survive the admitting "
        f"settle; got {settled}"
    )
    session = _save_and_reload(settled, "r7_repeated_answer", tmp_path, monkeypatch)

    # ── Turn 3 (unrelated R/B): the build must keep BOTH occurrences.
    turn3_context = _build_next_turn_context(session, "R")
    assert [r.get("content") for r in turn3_context].count("answer") == 2, (
        "both independently owned assistant occurrences must survive the "
        "later turn's outbound build — content-only collapse treated the "
        f"second one as replay; got {turn3_context}"
    )
    projection3 = _sanitize(turn3_context, normalize_bare_assistant_adjacency=True)
    returned3 = [
        *copy.deepcopy(projection3),
        _user("R", id=5, timestamp=5, _active_turn_token="t3:5"),
        _assistant("R_ANSWER", id=6, timestamp=6),
    ]
    settled3, protected3 = _dedupe_replayed_context_messages(
        turn3_context, returned3, "R",
        projected_history=_agent_replay_settle_projection(projection3),
    )
    settled3 = _deduplicate_context_messages(settled3, protected3)
    reloaded3 = _save_and_reload(settled3, "r7_repeated_answer", tmp_path, monkeypatch)
    final_contents = [r.get("content") for r in reloaded3.context_messages]
    assert final_contents.count("answer") == 2, (
        "both historical occurrences must survive the cold reload; got "
        f"{final_contents}"
    )
    assert final_contents.count("question") == 2, final_contents
    assert "R" in final_contents and "R_ANSWER" in final_contents, final_contents
