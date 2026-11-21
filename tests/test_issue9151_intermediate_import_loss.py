"""Spec for PR #9151 round 3 — an intermediate save must not delete imported rows.

Review 2026-11-18T19:18:18Z reproduced one persistence regression on top of the
repaired compression/tool-pair round:

The streaming worker freezes its input history before the early
``save(append_intent=True)``. A competing writer that appends after that freeze is
correctly imported by the early save, but ``Session.save()`` then advances the
"loaded" arrays to include the imported rows. The agent still answers from the
earlier history, so settlement rebuilds the arrays without the competing turn;
the final append-intent save now sees those rows as locally removed and drops
them from both durable projections.

These tests drive the real streaming worker, settlement, save and durable reload,
with a forwarding wrapper that injects the competing writer at a deterministic
point (immediately before an append-intent save). The controls prove the repair
does not disable loaded-base suppression globally, which is what keeps the
in-place compression replacement from reinserting discarded rows.
"""
from __future__ import annotations

import copy
import queue

import pytest

from api import config, models, profiles, routes, streaming
from api.models import Session
from tests.test_cancel_restart_journal_recovery import _isolated_state  # noqa: F401

PRIOR = [
    {"role": "user", "content": "Q1", "timestamp": 1},
    {"role": "assistant", "content": "A1", "timestamp": 2},
]


class _EchoAgent:
    """Modeled agent: echoes the frozen history plus the run's own turn."""

    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id")
        self.context_compressor = None
        self.ephemeral_system_prompt = None

    def run_conversation(self, user_message, system_message,
                         conversation_history, task_id, persist_user_message):
        return {
            "completed": True,
            "final_response": "OUR_ANSWER",
            "messages": copy.deepcopy(conversation_history) + [
                {"role": "user", "content": persist_user_message},
                {"role": "assistant", "content": "OUR_ANSWER"},
            ],
        }


def _contents(rows):
    return [row.get("content") for row in (rows or [])]


def _prepare_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _EchoAgent)
    # The modeled agent ignores session_db; without this stub the real worker
    # opens the live Agent-schema state.db at the default profile home.
    monkeypatch.setattr(streaming, "_build_session_db_for_stream", lambda _: None)
    monkeypatch.setattr(streaming, "resolve_model_provider",
                        lambda *a, **k: ("test-model", None, None))
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *a, **k: [])
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)


def _seed_session(sid):
    session = Session(session_id=sid)
    session.messages = copy.deepcopy(PRIOR)
    session.context_messages = copy.deepcopy(PRIOR)
    session.save(touch_updated_at=False)
    return session


def _append_as_another_writer(sid, tag, base_timestamp, real_save):
    """A second, independently loaded writer appends one user/assistant pair."""
    other = Session.load(sid)
    competing = [
        {"role": "user", "content": f"{tag}_Q", "timestamp": base_timestamp},
        {"role": "assistant", "content": f"{tag}_A", "timestamp": base_timestamp + 1},
    ]
    other.messages = list(other.messages) + copy.deepcopy(competing)
    other.context_messages = list(other.context_messages) + copy.deepcopy(competing)
    real_save(other, touch_updated_at=False, append_intent=True)
    return competing


def _run_worker(tmp_path, session, prompt):
    stream = session.session_id + "-stream"
    routes._prepare_chat_start_session_for_stream(
        session, msg=prompt, attachments=[], workspace=str(tmp_path),
        model="test-model", model_provider=None, stream_id=stream, started_at=100)
    models.SESSIONS[session.session_id] = session
    config.STREAMS[stream] = queue.Queue()
    streaming._run_agent_streaming(
        session.session_id, prompt, "test-model", str(tmp_path), stream, [])
    models.SESSIONS.clear()
    return Session.load(session.session_id)


@pytest.mark.requires_agent_modules
def test_concurrent_append_during_the_run_survives_settlement(tmp_path, monkeypatch):
    """The competing turn imported by the early save must not be deleted later."""
    _prepare_worker(tmp_path, monkeypatch)
    sid = "issue9151r3-intermediate-import"
    session = _seed_session(sid)

    real_save = models.Session.save
    calls = {"append": 0, "early": None}

    def wrapper(self, *args, **kwargs):
        first = bool(kwargs.get("append_intent")) and calls["append"] == 0
        if first:
            calls["append"] = 1
            other = Session.load(sid)
            competing = [
                {"role": "user", "content": "COMPETING_Q", "timestamp": 50},
                {"role": "assistant", "content": "COMPETING_A", "timestamp": 51},
            ]
            other.messages = list(other.messages) + copy.deepcopy(competing)
            other.context_messages = list(other.context_messages) + copy.deepcopy(competing)
            real_save(other, touch_updated_at=False, append_intent=True)
        out = real_save(self, *args, **kwargs)
        if first:
            calls["early"] = _contents(self.messages)
        return out

    monkeypatch.setattr(models.Session, "save", wrapper)
    reloaded = _run_worker(tmp_path, session, "OUR_Q")
    messages = _contents(reloaded.messages)
    context = _contents(reloaded.context_messages)

    # The early save really imported the competing turn before settlement.
    assert calls["early"] is not None, calls
    assert "COMPETING_Q" in calls["early"], calls["early"]
    assert "COMPETING_A" in calls["early"], calls["early"]
    # Settlement and the final append-intent save must not treat the import as
    # a deliberate local removal.
    assert "COMPETING_Q" in messages, messages
    assert "COMPETING_A" in messages, messages
    assert messages.count("COMPETING_Q") == 1, messages
    assert "COMPETING_Q" in context, context
    assert "COMPETING_A" in context, context
    assert context.count("COMPETING_Q") == 1, context
    # The run's own turn survives.
    assert "OUR_Q" in messages, messages
    assert "OUR_ANSWER" in messages, messages
    assert "OUR_Q" in context, context


@pytest.mark.requires_agent_modules
def test_two_intermediate_saves_do_not_grant_deletion_authority(tmp_path, monkeypatch):
    """Both competing turns survive, whichever append-intent save imported them."""
    _prepare_worker(tmp_path, monkeypatch)
    sid = "issue9151r3-two-imports"
    session = _seed_session(sid)

    real_save = models.Session.save
    calls = {"append": 0}

    def wrapper(self, *args, **kwargs):
        if kwargs.get("append_intent"):
            index = calls["append"]
            calls["append"] += 1
            if index == 0:
                _append_as_another_writer(sid, "COMPETING", 50, real_save)
            elif index == 1:
                _append_as_another_writer(sid, "LATE", 60, real_save)
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(models.Session, "save", wrapper)
    reloaded = _run_worker(tmp_path, session, "OUR_Q")
    messages = _contents(reloaded.messages)
    context = _contents(reloaded.context_messages)

    assert calls["append"] >= 2, calls
    for projection in (messages, context):
        assert "COMPETING_Q" in projection, projection
        assert "LATE_Q" in projection, projection
        assert projection.count("COMPETING_Q") == 1, projection
        assert projection.count("LATE_Q") == 1, projection
        assert "OUR_ANSWER" in projection, projection


@pytest.mark.requires_agent_modules
def test_no_concurrent_writer_control_keeps_the_run_turn(tmp_path, monkeypatch):
    """Control: with no competing writer the run's own turn persists unchanged."""
    _prepare_worker(tmp_path, monkeypatch)
    sid = "issue9151r3-no-concurrent"
    session = _seed_session(sid)
    reloaded = _run_worker(tmp_path, session, "OUR_Q")

    assert _contents(reloaded.messages) == ["Q1", "A1", "OUR_Q", "OUR_ANSWER"]
    assert _contents(reloaded.context_messages) == ["Q1", "A1", "OUR_Q", "OUR_ANSWER"]


def test_append_intent_still_drops_base_rows_removed_locally(tmp_path, monkeypatch):
    """Control: loaded-base suppression stays on for deliberate local removals.

    This is the in-place compression contract: rows the writer loaded and then
    replaced must not be reinserted as a concurrent append.
    """
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")
    sid = "issue9151r3-compression-control"
    base_rows = [
        {"role": "user", "content": "Q1", "timestamp": 1},
        {"role": "assistant", "content": "A1", "timestamp": 2},
        {"role": "user", "content": "DISCARDED_Q", "timestamp": 3},
        {"role": "assistant", "content": "DISCARDED_A", "timestamp": 4},
    ]
    seed = Session(session_id=sid)
    seed.messages = copy.deepcopy(base_rows)
    seed.context_messages = copy.deepcopy(base_rows)
    seed.save(touch_updated_at=False)

    writer = Session.load(sid)
    writer.messages = copy.deepcopy(base_rows[:2])
    writer.context_messages = copy.deepcopy(base_rows[:2])
    writer.save(append_intent=True)

    models.SESSIONS.clear()
    reloaded = Session.load(sid)
    assert _contents(reloaded.messages) == ["Q1", "A1"]
    assert _contents(reloaded.context_messages) == ["Q1", "A1"]
