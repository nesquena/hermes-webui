"""Durable transcript revision coverage for WebUI compression handoff."""

import queue
import sqlite3
import sys
import types
from collections import OrderedDict

import pytest

from api import models, streaming
from api.models import Session

pytestmark = pytest.mark.requires_agent_modules


@pytest.mark.parametrize("revision", [None, {"session_id": "session-1"}])
def test_run_conversation_revision_kwarg_is_omitted_for_legacy_agent(revision):
    class LegacyAgent:
        def run_conversation(
            self,
            user_message,
            system_message,
            conversation_history,
            task_id,
            persist_user_message,
        ):
            return None

    kwargs = {}
    supported = streaming._add_supported_run_conversation_kwarg(
        LegacyAgent().run_conversation,
        kwargs,
        "conversation_history_revision",
        revision,
    )

    assert supported is False
    assert "conversation_history_revision" not in kwargs


@pytest.mark.parametrize("revision", [None, {"session_id": "session-1"}])
@pytest.mark.parametrize("mode", ["explicit", "variadic"])
def test_run_conversation_revision_kwarg_is_added_for_capable_agent(revision, mode):
    class ExplicitAgent:
        def run_conversation(self, *, conversation_history_revision=None):
            return None

    class VariadicAgent:
        def run_conversation(self, **kwargs):
            return None

    agent = ExplicitAgent() if mode == "explicit" else VariadicAgent()
    kwargs = {}
    supported = streaming._add_supported_run_conversation_kwarg(
        agent.run_conversation,
        kwargs,
        "conversation_history_revision",
        revision,
    )

    assert supported is True
    assert kwargs["conversation_history_revision"] is revision


def test_build_run_conversation_kwargs_omits_revision_for_strict_legacy_agent():
    class LegacyAgent:
        def run_conversation(
            self,
            user_message,
            system_message,
            conversation_history,
            task_id,
            persist_user_message,
            persist_user_timestamp,
        ):
            return None

    agent = LegacyAgent()
    kwargs = streaming._build_run_conversation_kwargs(
        agent.run_conversation,
        user_message="hello",
        system_message="system",
        conversation_history=[{"role": "user", "content": "prior"}],
        conversation_history_revision={"session_id": "session-1"},
        task_id="session-1",
        persist_user_message="hello",
        persist_user_timestamp=123.0,
    )

    assert kwargs == {
        "user_message": "hello",
        "system_message": "system",
        "conversation_history": [{"role": "user", "content": "prior"}],
        "task_id": "session-1",
        "persist_user_message": "hello",
        "persist_user_timestamp": 123.0,
    }


def _make_state_db(path, sid, rows):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT,
                content TEXT,
                timestamp REAL,
                active INTEGER
            )
            """
        )
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp, active) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    sid,
                    row["role"],
                    row["content"],
                    row.get("timestamp"),
                    row.get("active", 1),
                )
                for row in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _append_state_row(path, sid, *, role, content, timestamp, active=1):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, role, content, timestamp, active),
        )
        conn.commit()
    finally:
        conn.close()


def _install_streaming_session(monkeypatch, tmp_path, *, sid, stream_id, messages, context_messages):
    import api.config as config

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    index_file = session_dir / "_index.json"

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(streaming, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")

    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()

    session = Session(
        session_id=sid,
        title="Durable revision",
        workspace=str(tmp_path),
        model="test-model",
        messages=list(messages),
        context_messages=list(context_messages),
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "new webui turn"
    session.pending_started_at = 10.0
    session.save(touch_updated_at=False)
    models.SESSIONS[sid] = session
    streaming.SESSIONS[sid] = session

    event_queue = queue.Queue()
    config.STREAMS[stream_id] = event_queue
    streaming.STREAMS[stream_id] = event_queue

    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *_args, **_kwargs: ("test-model", "test-provider", None),
    )
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *_args, **_kwargs: [])

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.__dict__["SessionDB"] = lambda *_args, **_kwargs: object()
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)

    return session, event_queue


def _drain_events(event_queue):
    events = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    return events


def test_state_db_reader_returns_active_messages_with_matching_revision(tmp_path, monkeypatch):
    sid = "revision-reader"
    db_path = tmp_path / "state.db"
    _make_state_db(
        db_path,
        sid,
        [
            {"role": "user", "content": "inactive", "timestamp": 1.0, "active": 0},
            {"role": "user", "content": "active user", "timestamp": 2.0},
            {"role": "assistant", "content": "active answer", "timestamp": 3.0},
        ],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    snapshot = models.get_state_db_session_messages(sid, with_revision=True)

    assert [message["content"] for message in snapshot.messages] == [
        "active user",
        "active answer",
    ]
    assert snapshot.revision == {
        "session_id": sid,
        "active_message_count": 2,
        "max_active_message_id": 3,
    }
    assert isinstance(models.get_state_db_session_messages(sid), list)


def test_state_db_reader_revision_includes_api_content_digest(
    tmp_path, monkeypatch
):
    """The WebUI revision must carry the Agent's model-facing sidecar fence."""
    sid = "revision-api-content-digest"
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
            "role TEXT, content TEXT, timestamp REAL, active INTEGER, "
            "api_content TEXT)"
        )
        conn.execute(
            "INSERT INTO messages "
            "(session_id, role, content, timestamp, active, api_content) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, "user", "clean", 1.0, 1, "wire-v1"),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    first = models.get_state_db_session_messages(sid, with_revision=True)
    assert first.revision == {
        "session_id": sid,
        "active_message_count": 1,
        "max_active_message_id": 1,
        "active_rows_digest": (
            "50101e75ded18d78bc21646a6fcd4cddb8f07901838635dda6eac85d219c1359"
        ),
    }

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE messages SET api_content = ? WHERE session_id = ?",
            ("wire-v2", sid),
        )
        conn.commit()
    finally:
        conn.close()

    second = models.get_state_db_session_messages(sid, with_revision=True)
    assert second.revision == {
        "session_id": sid,
        "active_message_count": 1,
        "max_active_message_id": 1,
        "active_rows_digest": (
            "de45994b9f134be94b99ce03a47da3b553c86a27e5cdcfd67885644a8372102a"
        ),
    }


def test_state_db_revision_is_unavailable_when_selected_row_has_null_active(
    tmp_path, monkeypatch
):
    sid = "revision-null-active"
    db_path = tmp_path / "state.db"
    _make_state_db(
        db_path,
        sid,
        [{"role": "user", "content": "legacy active row", "timestamp": 1.0, "active": None}],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    snapshot = models.get_state_db_session_messages(sid, with_revision=True)

    assert [message["content"] for message in snapshot.messages] == ["legacy active row"]
    assert snapshot.revision is None


def test_state_db_revision_is_unavailable_without_active_column(tmp_path, monkeypatch):
    sid = "revision-no-active-column"
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
            "role TEXT, content TEXT, timestamp REAL)"
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, "user", "legacy schema row", 1.0),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    snapshot = models.get_state_db_session_messages(sid, with_revision=True)

    assert [message["content"] for message in snapshot.messages] == ["legacy schema row"]
    assert snapshot.revision is None


def test_snapshot_reader_missing_explicit_profile_does_not_fall_back_to_active_db(
    tmp_path,
    monkeypatch,
):
    sid = "missing-profile-snapshot"
    foreign_db = tmp_path / "active-home" / "state.db"
    foreign_db.parent.mkdir()
    _make_state_db(
        foreign_db,
        sid,
        [{"role": "user", "content": "foreign profile row", "timestamp": 1.0}],
    )
    missing_home = tmp_path / "missing-profile"
    monkeypatch.setattr(models, "_get_profile_home", lambda _profile: missing_home)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: foreign_db)

    snapshot = models.get_state_db_session_messages(
        sid,
        profile="missing",
        with_revision=True,
    )

    assert snapshot.messages == []
    assert snapshot.revision is None


def test_reconciled_reader_missing_explicit_profile_does_not_fall_back_to_active_db(
    tmp_path,
    monkeypatch,
):
    sid = "missing-profile-reconcile"
    foreign_db = tmp_path / "active-home" / "state.db"
    foreign_db.parent.mkdir()
    _make_state_db(
        foreign_db,
        sid,
        [{"role": "user", "content": "foreign profile row", "timestamp": 1.0}],
    )
    missing_home = tmp_path / "missing-profile"
    monkeypatch.setattr(models, "_get_profile_home", lambda _profile: missing_home)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: foreign_db)
    session = Session(
        session_id=sid,
        profile="missing",
        messages=[],
        context_messages=[],
    )

    reconciled = models.reconciled_state_db_messages_for_session(
        session,
        with_revision=True,
    )

    assert reconciled.messages == []
    assert reconciled.revision is None


def test_stale_partial_requires_authoritative_current_turn_boundary():
    session = Session(
        session_id="partial-authority",
        messages=[],
        context_messages=[],
    )
    result = {
        "partial": True,
        "messages": [
            {"role": "user", "content": "repeat prompt"},
            {"role": "assistant", "content": "historical answer"},
            {"role": "user", "content": "later unrelated turn"},
            {"role": "assistant", "content": "later answer"},
        ]
    }
    identity = {
        "token": "stream:123",
        "text": "repeat prompt",
        # This index belonged to the replaced projection but now happens to
        # point at an identical historical prompt. It is not current-turn proof.
        "current_turn_user_idx": 0,
        "turn_id": "session:turn-current",
    }

    streaming._append_result_partial_on_error(
        session,
        result,
        [{"role": "user", "content": "unrelated baseline"}],
        "repeat prompt",
        active_turn_identity=identity,
    )

    assert not any(message.get("_partial") for message in session.messages)


def test_stale_non_prefix_partial_uses_token_and_stops_at_next_user():
    session = Session(
        session_id="stale-partial-token-boundary",
        messages=[],
        context_messages=[],
    )
    result = {
        "partial": True,
        "messages": [
            {"role": "user", "content": "historical"},
            {"role": "assistant", "content": "historical answer"},
            {
                "role": "user",
                "content": "current prompt",
                "_active_turn_token": "current-turn-token",
            },
            {"role": "assistant", "content": "current partial"},
            {"role": "user", "content": "later turn"},
            {"role": "assistant", "content": "later answer"},
        ],
    }

    appended = streaming._append_result_partial_on_error(
        session,
        result,
        [{"role": "user", "content": "different baseline"}],
        "current prompt",
        active_turn_identity={
            "token": "current-turn-token",
            "turn_id": "turn-current",
            "current_turn_user_idx": 2,
            "text": "current prompt",
        },
    )

    assert appended is not None
    assert appended["content"] == "current partial"
    assert [message["content"] for message in session.messages] == [
        "current partial"
    ]


def test_non_prefix_partial_uses_agent_turn_boundary_without_webui_token():
    session = Session(session_id="agent-boundary-partial", messages=[], context_messages=[])
    result = {
        "partial": True,
        "turn_id": "agent-turn-42",
        "current_turn_user_idx": 2,
        "messages": [
            {"role": "user", "content": "repeat prompt"},
            {"role": "assistant", "content": "historical answer"},
            {"role": "user", "content": "  repeat   prompt  "},
            {"role": "assistant", "content": "current partial"},
            {"role": "user", "content": "later turn"},
            {"role": "assistant", "content": "later answer"},
        ],
    }
    identity = streaming._resolve_active_turn_authority(
        {"token": "webui-token", "text": "repeat prompt", "turn_id": "", "current_turn_user_idx": None},
        result=result,
    )

    appended = streaming._append_result_partial_on_error(
        session, result, [{"role": "user", "content": "replaced baseline"}],
        "repeat prompt", active_turn_identity=identity,
    )

    assert appended["content"] == "current partial"


@pytest.mark.parametrize("terminal", ["returned", "raised"])
def test_non_prefix_self_heal_accepts_agent_turn_boundary_without_webui_token(terminal):
    result = {
        "completed": True,
        "turn_id": "agent-turn-43",
        "current_turn_user_idx": 2,
        "messages": [
            {"role": "user", "content": "repeat prompt"},
            {"role": "assistant", "content": "historical answer"},
            {"role": "user", "content": " repeat\n prompt "},
            {"role": "assistant", "content": f"healed after {terminal}"},
            {"role": "user", "content": "later turn"},
            {"role": "assistant", "content": "later answer"},
        ],
    }
    identity = streaming._resolve_active_turn_authority(
        {"token": "webui-token", "text": "repeat prompt", "turn_id": "", "current_turn_user_idx": None},
        result=result,
    )

    assert streaming._self_heal_result_succeeded(
        result, [{"role": "user", "content": "replaced baseline"}], identity, "repeat prompt"
    )


def test_non_prefix_agent_boundary_rejects_wrong_normalized_user():
    result = {
        "completed": True,
        "turn_id": "agent-turn-stale",
        "current_turn_user_idx": 0,
        "messages": [
            {"role": "user", "content": "different prompt"},
            {"role": "assistant", "content": "historical answer"},
        ],
    }
    identity = streaming._resolve_active_turn_authority(
        {"token": "absent", "text": "repeat prompt", "turn_id": "", "current_turn_user_idx": None},
        result=result,
    )

    assert not streaming._self_heal_result_succeeded(
        result, [{"role": "user", "content": "baseline"}], identity, "repeat prompt"
    )


def test_result_partial_does_not_mutate_identical_prior_turn():
    identity = {"token": "current-turn-token"}
    session = Session(
        session_id="result-partial-identical-content",
        messages=[
            {"role": "user", "content": "historical"},
            {"role": "assistant", "content": "same answer"},
            {
                "role": "user",
                "content": "current prompt",
                "_active_turn_token": identity["token"],
            },
        ],
        context_messages=[],
    )

    appended = streaming._append_result_partial_on_error(
        session,
        {
            "partial": True,
            "messages": [
                {
                    "role": "user",
                    "content": "current prompt",
                    "_active_turn_token": identity["token"],
                },
                {"role": "assistant", "content": "same answer"},
            ],
        },
        [{"role": "user", "content": "different baseline"}],
        "current prompt",
        active_turn_identity=identity,
    )

    assert appended is session.messages[-1]
    assert appended["content"] == "same answer"
    assert appended["_partial"] is True
    assert session.messages[1] == {"role": "assistant", "content": "same answer"}


def test_stream_partial_does_not_mutate_identical_prior_turn(monkeypatch):
    identity = {"token": "current-turn-token"}
    session = Session(
        session_id="stream-partial-identical-content",
        messages=[
            {"role": "user", "content": "historical"},
            {"role": "assistant", "content": "same answer"},
            {
                "role": "user",
                "content": "current prompt",
                "_active_turn_token": identity["token"],
            },
        ],
        context_messages=[],
    )
    stream_id = "stream-identical-content"
    monkeypatch.setitem(streaming.STREAM_PARTIAL_TEXT, stream_id, "same answer")
    monkeypatch.setitem(streaming.STREAM_REASONING_TEXT, stream_id, "")
    monkeypatch.setitem(streaming.STREAM_LIVE_TOOL_CALLS, stream_id, [])

    appended = streaming._snapshot_and_append_partial_on_error(
        session,
        stream_id,
        active_turn_identity=identity,
    )

    assert appended is session.messages[-1]
    assert appended["content"] == "same answer"
    assert appended["_partial"] is True
    assert session.messages[1] == {"role": "assistant", "content": "same answer"}


def test_resolved_boundary_is_rebound_to_fresh_self_heal_agent():
    class HealedAgent:
        _persist_user_message_idx = 2
        _current_turn_id = "fresh-heal-turn"

    stale_identity = {
        "token": "webui-token",
        "text": "current prompt",
        "current_turn_user_idx": 7,
        "turn_id": "failed-agent-turn",
        "agent_turn_boundary_resolved": True,
        "agent_turn_boundary_source": "agent",
    }

    resolved = streaming._resolve_active_turn_authority(
        stale_identity,
        result={"messages": []},
        agent=HealedAgent(),
    )

    assert resolved["current_turn_user_idx"] == 2
    assert resolved["turn_id"] == "fresh-heal-turn"
    assert resolved["agent_turn_boundary_source"] == "agent"


@pytest.mark.parametrize(
    ("live_snapshot", "result_snapshot", "expected"),
    [
        ("partial response with newer suffix", "partial response", "partial response with newer suffix"),
        ("partial response", "partial response with newer suffix", "partial response with newer suffix"),
    ],
)
def test_differing_live_and_result_snapshots_are_exact_once(
    monkeypatch,
    live_snapshot,
    result_snapshot,
    expected,
):
    token = "current-turn-token"
    stream_id = "differing-partial-sources"
    session = Session(
        session_id="differing-partial-sources",
        messages=[
            {"role": "user", "content": "historical"},
            {"role": "assistant", "content": "historical answer"},
            {
                "role": "user",
                "content": "current prompt",
                "_active_turn_token": token,
            },
        ],
        context_messages=[],
    )
    result = {
        "partial": True,
        "turn_id": "agent-current-turn",
        "current_turn_user_idx": 1,
        "messages": [
            {"role": "assistant", "content": "[compacted] history"},
            {"role": "user", "content": " current\n prompt "},
            {"role": "assistant", "content": result_snapshot},
            {"role": "user", "content": "later turn"},
            {"role": "assistant", "content": "later wrong"},
        ],
    }
    identity = streaming._resolve_active_turn_authority(
        {"token": token, "text": "current prompt"},
        result=result,
    )
    monkeypatch.setitem(streaming.STREAM_PARTIAL_TEXT, stream_id, live_snapshot)
    monkeypatch.setitem(streaming.STREAM_REASONING_TEXT, stream_id, "")
    monkeypatch.setitem(streaming.STREAM_LIVE_TOOL_CALLS, stream_id, [])

    live_row = streaming._snapshot_and_append_partial_on_error(
        session,
        stream_id,
        active_turn_identity=identity,
    )
    result_row = streaming._append_result_partial_on_error(
        session,
        result,
        [{"role": "user", "content": "replaced baseline"}],
        "current prompt",
        active_turn_identity=identity,
    )

    partials = [message for message in session.messages if message.get("_partial")]
    assert result_row is live_row
    assert [message["content"] for message in partials] == [expected]
    assert "later wrong" not in [message.get("content") for message in session.messages]


def test_reconciled_compressed_projection_keeps_durable_snapshot_revision(tmp_path, monkeypatch):
    sid = "revision-reconcile"
    db_path = tmp_path / "state.db"
    _make_state_db(
        db_path,
        sid,
        [
            {"role": "user", "content": "durable user", "timestamp": 1.0},
            {"role": "assistant", "content": "durable answer", "timestamp": 2.0},
            {"role": "user", "content": "durable follow-up", "timestamp": 3.0},
        ],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    compacted_context = [
        {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY]\nCompressed task summary.",
            "timestamp": 4.0,
        }
    ]
    session = Session(
        session_id=sid,
        messages=[{"role": "user", "content": "full visible transcript"}],
        context_messages=compacted_context,
    )

    state_snapshot = models.get_state_db_session_messages(sid, with_revision=True)
    reconciled = models.reconciled_state_db_messages_for_session(
        session,
        prefer_context=True,
        state_messages=state_snapshot,
        with_revision=True,
    )

    assert reconciled.messages == compacted_context
    assert len(reconciled.messages) < reconciled.revision["active_message_count"]
    assert reconciled.revision == state_snapshot.revision


def test_webui_run_passes_revision_from_original_snapshot_when_sqlite_changes_before_agent(
    tmp_path, monkeypatch
):
    import api.profiles as profiles

    sid = "revision-stream"
    stream_id = "stream-revision"
    # Keep a conflicting default-profile DB to prove the worker uses the
    # session's explicit profile rather than process-global active state.
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [{"role": "user", "content": "wrong profile row", "timestamp": 1.0}],
    )
    profile_home = tmp_path / "named-profile-home"
    profile_home.mkdir()
    db_path = profile_home / "state.db"
    _make_state_db(
        db_path,
        sid,
        [
            {"role": "user", "content": "durable user", "timestamp": 1.0},
            {"role": "assistant", "content": "durable answer", "timestamp": 2.0},
            {"role": "user", "content": "durable follow-up", "timestamp": 3.0},
        ],
    )
    compacted_context = [
        {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY]\nCompressed task summary.",
            "timestamp": 4.0,
        }
    ]
    session, _event_queue = _install_streaming_session(
        monkeypatch,
        tmp_path,
        sid=sid,
        stream_id=stream_id,
        messages=[{"role": "user", "content": "visible user", "timestamp": 1.0}],
        context_messages=compacted_context,
    )
    session.profile = "named-profile"
    session.save(touch_updated_at=False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _profile: profile_home)
    monkeypatch.setattr(profiles, "get_profile_runtime_env", lambda _home: {})

    original_reconcile = streaming.reconciled_state_db_messages_for_session
    reconciliation_calls = 0

    def reconcile_then_mutate(*args, **kwargs):
        nonlocal reconciliation_calls
        result = original_reconcile(*args, **kwargs)
        reconciliation_calls += 1
        if reconciliation_calls == 1:
            _append_state_row(
                db_path,
                sid,
                role="assistant",
                content="concurrent durable row",
                timestamp=5.0,
            )
        return result

    monkeypatch.setattr(
        streaming,
        "reconciled_state_db_messages_for_session",
        reconcile_then_mutate,
    )
    captured = {}

    class FakeAgent:
        def __init__(self, session_id=None, **_kwargs):
            self.session_id = session_id
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            captured.update(kwargs)
            return {
                "completed": True,
                "final_response": "ok",
                "messages": [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "ok"},
                ],
            }

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)

    streaming._run_agent_streaming(
        session_id=sid,
        msg_text="new webui turn",
        model="test-model",
        workspace=str(tmp_path),
        stream_id=stream_id,
        attachments=[],
    )

    assert captured["task_id"] == sid
    assert captured["conversation_history_revision"] == {
        "session_id": sid,
        "active_message_count": 3,
        "max_active_message_id": 3,
    }
    assert captured["conversation_history_revision"]["active_message_count"] == 3
    assert len(models.get_state_db_session_messages(sid, profile="named-profile")) == 4


def test_webui_run_missing_explicit_profile_passes_no_foreign_revision(
    tmp_path,
    monkeypatch,
):
    import api.profiles as profiles

    sid = "missing-profile-stream"
    stream_id = "stream-missing-profile"
    foreign_db = tmp_path / "state.db"
    _make_state_db(
        foreign_db,
        sid,
        [{"role": "user", "content": "foreign profile row", "timestamp": 1.0}],
    )
    missing_home = tmp_path / "missing-profile-home"
    session, event_queue = _install_streaming_session(
        monkeypatch,
        tmp_path,
        sid=sid,
        stream_id=stream_id,
        messages=[{"role": "user", "content": "local projection", "timestamp": 1.0}],
        context_messages=[],
    )
    session.profile = "missing"
    session.save(touch_updated_at=False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _profile: missing_home)
    monkeypatch.setattr(profiles, "get_profile_runtime_env", lambda _home: {})
    captured = {}

    class FakeAgent:
        def __init__(self, session_id=None, **_kwargs):
            self.session_id = session_id
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            captured.update(kwargs)
            return {
                "completed": True,
                "final_response": "ok",
                "messages": [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "ok"},
                ],
            }

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)

    streaming._run_agent_streaming(
        session_id=sid,
        msg_text="new webui turn",
        model="test-model",
        workspace=str(tmp_path),
        stream_id=stream_id,
        attachments=[],
    )

    assert captured["conversation_history_revision"] is None
    events = _drain_events(event_queue)
    assert not any(event == "apperror" for event, _payload in events)


@pytest.mark.parametrize("delivery", ["exception", "result"])
def test_stale_compression_snapshot_emits_actionable_error_without_replaying_turn(
    tmp_path, monkeypatch, delivery
):
    sid = f"revision-stale-{delivery}"
    stream_id = f"stream-revision-stale-{delivery}"
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [{"role": "user", "content": "durable user", "timestamp": 1.0}],
    )
    session, event_queue = _install_streaming_session(
        monkeypatch,
        tmp_path,
        sid=sid,
        stream_id=stream_id,
        messages=[],
        context_messages=[],
    )
    calls = 0

    class CompressionSnapshotStaleError(RuntimeError):
        pass

    compression_module = types.ModuleType("agent.conversation_compression")
    compression_module.__dict__["CompressionSnapshotStaleError"] = CompressionSnapshotStaleError
    monkeypatch.setitem(sys.modules, "agent.conversation_compression", compression_module)

    class FakeAgent:
        def __init__(self, session_id=None, stream_delta_callback=None, **_kwargs):
            self.session_id = session_id
            self.stream_delta_callback = stream_delta_callback
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self._last_error = None
            self._persist_user_message_idx = 0
            self._current_turn_id = f"paired-turn-{delivery}"

        def run_conversation(self, **_kwargs):
            nonlocal calls
            calls += 1
            if self.stream_delta_callback:
                self.stream_delta_callback(
                    "Partial work completed before the conflict with a newer suffix."
                )
            if delivery == "exception":
                raise CompressionSnapshotStaleError(
                    "expected revision count=1 max_id=1; observed count=2 max_id=9"
                )
            return {
                "completed": False,
                "final_response": "Partial work completed before the conflict.",
                "messages": [
                    {"role": "user", "content": "new webui turn"},
                    {
                        "role": "assistant",
                        "content": "Partial work completed before the conflict.",
                    },
                ],
                "error": "compression_snapshot_stale",
                "partial": True,
                "failed": False,
                "compression_snapshot_stale": True,
            }

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)

    streaming._run_agent_streaming(
        session_id=sid,
        msg_text="new webui turn",
        model="test-model",
        workspace=str(tmp_path),
        stream_id=stream_id,
        attachments=[],
    )

    events = _drain_events(event_queue)
    apperrors = [payload for event, payload in events if event == "apperror"]
    assert calls == 1
    assert apperrors and apperrors[-1]["type"] == "compression_snapshot_stale"
    assert "next message" in apperrors[-1]["hint"].lower()
    assert "expected revision" not in apperrors[-1]["message"].lower()
    assert not any(event == "done" for event, _payload in events)

    reloaded = Session.load(sid)
    assert reloaded is not None
    assert reloaded.active_stream_id is None
    assert reloaded.pending_user_message is None
    assert any(
        message.get("_partial")
        and message.get("content")
        == "Partial work completed before the conflict with a newer suffix."
        for message in reloaded.messages
    )
    partial_messages = [
        message for message in reloaded.messages if message.get("_partial")
    ]
    assert len(partial_messages) == 1, [
        (
            message.get("role"),
            message.get("content"),
            message.get("_active_turn_token"),
            message.get("_partial"),
        )
        for message in reloaded.messages
    ]
    assert sum(
        "Partial work completed" in str(message.get("content") or "")
        for message in reloaded.messages
        if message.get("role") == "assistant"
    ) == 1
    assert reloaded.messages[-1]["_error"] is True
    assert "next message" in reloaded.messages[-1]["content"].lower()
    assert session.active_stream_id is None


@pytest.mark.parametrize("failure_mode", ["result", "exception"])
@pytest.mark.parametrize(
    "second_result",
    [
        "recovered",
        "stale_error",
        "stale_flag",
        "stale_without_current_assistant",
        "stale_non_prefix_compacted",
        "stale_non_prefix_owned_partial",
        "stale_non_prefix_without_current_user",
        "stale_non_prefix_repeated_prompt_without_authority",
        "no_response_after_streamed_token",
    ],
)
def test_auth_self_heal_refreshes_revision_after_first_agent_persists_user(
    tmp_path, monkeypatch, failure_mode, second_result
):
    sid = f"revision-self-heal-{failure_mode}-{second_result}"
    stream_id = f"stream-revision-self-heal-{failure_mode}-{second_result}"
    db_path = tmp_path / "state.db"
    prior_messages = [
        {"role": "user", "content": "prior user", "timestamp": 1.0},
        {"role": "assistant", "content": "prior answer", "timestamp": 2.0},
    ]
    _make_state_db(db_path, sid, prior_messages)
    _session, event_queue = _install_streaming_session(
        monkeypatch,
        tmp_path,
        sid=sid,
        stream_id=stream_id,
        messages=prior_messages,
        context_messages=prior_messages,
    )
    revisions = []

    class AuthThenRecoverAgent:
        runs = 0

        def __init__(self, session_id=None, **_kwargs):
            self.session_id = session_id
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self._last_error = None
            self.stream_delta_callback = _kwargs.get("stream_delta_callback")

        def run_conversation(self, **kwargs):
            type(self).runs += 1
            revisions.append(kwargs["conversation_history_revision"])
            history = list(kwargs.get("conversation_history") or [])
            if type(self).runs == 1:
                _append_state_row(
                    db_path,
                    sid,
                    role="user",
                    content=kwargs["persist_user_message"],
                    timestamp=3.0,
                )
                if failure_mode == "exception":
                    raise RuntimeError("401 unauthorized")
                return {
                    "messages": history,
                    "error": {
                        "type": "authentication_error",
                        "status_code": 401,
                        "message": "token invalid",
                    },
                }
            if second_result == "no_response_after_streamed_token":
                if self.stream_delta_callback is not None:
                    self.stream_delta_callback("streamed but not finalized")
                return {"completed": True, "messages": history}
            if second_result != "recovered":
                if second_result == "stale_non_prefix_owned_partial":
                    stale_messages = [
                        {"role": "assistant", "content": "compacted history"},
                        {
                            "role": "user",
                            "content": "new webui turn",
                            "_active_turn_token": streaming.build_active_turn_token(
                                stream_id, 10.0
                            ),
                        },
                        {"role": "assistant", "content": "owned partial"},
                        {"role": "user", "content": "later user"},
                        {"role": "assistant", "content": "later wrong"},
                    ]
                elif second_result == "stale_non_prefix_without_current_user":
                    stale_messages = [
                        {"role": "user", "content": "unrelated historical user"},
                        {"role": "assistant", "content": "historical answer"},
                    ]
                elif second_result == "stale_non_prefix_compacted":
                    # Compacted/replayed result that REPLACES the pre-call
                    # baseline instead of appending to it: historical
                    # assistant rows (including "prior answer") sit before the
                    # last current-user row and one of them lands in the
                    # numeric suffix messages[len(baseline):]. There is no
                    # assistant row for the current turn.
                    stale_messages = [
                        {
                            "role": "assistant",
                            "content": "[compacted] summary of earlier context",
                        },
                        {"role": "user", "content": "prior user"},
                        {"role": "assistant", "content": "intermediate answer"},
                        {"role": "assistant", "content": "prior answer"},
                        {"role": "user", "content": "new webui turn"},
                    ]
                elif second_result == "stale_without_current_assistant":
                    stale_messages = history + [
                        {"role": "user", "content": "new webui turn"}
                    ]
                elif second_result == "stale_non_prefix_repeated_prompt_without_authority":
                    stale_messages = [
                        {"role": "user", "content": "new webui turn"},
                        {"role": "assistant", "content": "prior answer"},
                    ]
                else:
                    stale_messages = history + [
                        {"role": "user", "content": "new webui turn"},
                        {"role": "assistant", "content": "partial stale"},
                    ]
                stale_result = {
                    "completed": False,
                    "final_response": (
                        "owned partial"
                        if second_result == "stale_non_prefix_owned_partial"
                        else (
                            ""
                            if second_result
                            in (
                                "stale_without_current_assistant",
                                "stale_non_prefix_compacted",
                                "stale_non_prefix_without_current_user",
                                "stale_non_prefix_repeated_prompt_without_authority",
                            )
                            else "partial stale"
                        )
                    ),
                    "messages": stale_messages,
                    "partial": True,
                    "failed": False,
                }
                if second_result == "stale_error":
                    stale_result["error"] = "compression_snapshot_stale"
                elif second_result == "stale_flag":
                    stale_result["compression_snapshot_stale"] = True
                else:
                    stale_result["error"] = "compression_snapshot_stale"
                    stale_result["compression_snapshot_stale"] = True
                return stale_result
            return {
                "completed": True,
                "final_response": "recovered",
                "messages": history + [
                    {"role": "user", "content": "new webui turn"},
                    {"role": "assistant", "content": "recovered"},
                ],
            }

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: AuthThenRecoverAgent)
    monkeypatch.setattr(
        streaming,
        "_attempt_credential_self_heal",
        lambda *_args, **_kwargs: {
            "api_key": "refreshed-key",
            "provider": "test-provider",
            "base_url": None,
            "credential_pool": None,
        },
    )

    streaming._run_agent_streaming(
        session_id=sid,
        msg_text="new webui turn",
        model="test-model",
        workspace=str(tmp_path),
        stream_id=stream_id,
        attachments=[],
    )

    assert revisions == [
        {
            "session_id": sid,
            "active_message_count": 2,
            "max_active_message_id": 2,
        },
        {
            "session_id": sid,
            "active_message_count": 3,
            "max_active_message_id": 3,
        },
    ]
    events = _drain_events(event_queue)
    apperrors = [payload for event, payload in events if event == "apperror"]
    reloaded = Session.load(sid)
    assert reloaded is not None
    assert AuthThenRecoverAgent.runs == 2
    if second_result == "recovered":
        assert not apperrors
        assert any(message.get("content") == "recovered" for message in reloaded.messages)
    elif second_result == "no_response_after_streamed_token":
        assert len(apperrors) == 1
        assert apperrors[0]["type"] == "auth_mismatch"
        assert not any(event == "done" for event, _payload in events)
        assert reloaded.active_stream_id is None
        assert reloaded.pending_user_message is None
    else:
        assert len(apperrors) == 1
        assert apperrors[0]["type"] == "compression_snapshot_stale"
        assert "next message" in apperrors[0]["hint"].lower()
        assert not any(event == "done" for event, _payload in events)
        assert reloaded.active_stream_id is None
        assert reloaded.pending_user_message is None
        assert reloaded.pending_attachments == []
        partials = [
            message
            for message in reloaded.messages
            if message.get("_partial") is True
        ]
        if second_result in (
            "stale_without_current_assistant",
            "stale_non_prefix_compacted",
            "stale_non_prefix_without_current_user",
            "stale_non_prefix_repeated_prompt_without_authority",
        ):
            assert not partials
            assert not any(
                message.get("content") == "prior answer" and message.get("_partial") is True
                for message in reloaded.messages
            )
        else:
            expected_partial = (
                "owned partial"
                if second_result == "stale_non_prefix_owned_partial"
                else "partial stale"
            )
            assert [message.get("content") for message in partials] == [expected_partial]
            if second_result == "stale_non_prefix_owned_partial":
                assert not any(
                    message.get("content") == "later wrong"
                    for message in reloaded.messages
                )
        assert reloaded.messages[-1]["_error"] is True
        assert "next message" in reloaded.messages[-1]["content"].lower()


def _repeated_prompt_collision_result_messages(prompt, *, current_answer):
    """Non-prefix Agent projection where BOTH candidate positions are user rows
    with the same normalized text.

    Against the 2-row heal baseline (prior user + prior assistant) the legacy
    shifted probe ``current_turn_user_idx - len(previous_context)`` = 3 - 2 = 1
    lands on the HISTORICAL same-text user row, which is followed only by
    historical assistant prose. The declared Agent index 3 addresses
    ``result["messages"]`` directly and lands on the CURRENT same-text user
    row, followed only by current output (when any exists).
    """
    rows = [
        {"role": "assistant", "content": "[compacted] summary of earlier context"},
        {"role": "user", "content": prompt},  # shifted probe target (historical)
        {"role": "assistant", "content": "historical answer"},  # historical-only prose
        {"role": "user", "content": prompt},  # exact Agent index target (current)
    ]
    if current_answer is not None:
        rows.append({"role": "assistant", "content": current_answer})
    return rows


@pytest.mark.parametrize("terminal", ["returned", "raised"])
@pytest.mark.parametrize(
    "collision",
    [
        "current_output_after_exact_row",
        "historical_output_after_shifted_row_only",
        "historical_only_stale_partial",
    ],
)
def test_self_heal_repeated_prompt_never_accepts_shifted_historical_row(
    tmp_path, monkeypatch, terminal, collision
):
    """Required regression (2026-08-13 review): one declared index domain.

    A repeated prompt makes both legacy candidate positions (shifted
    ``current_turn_user_idx - len(previous_context)`` and exact
    ``current_turn_user_idx``) same-text user rows in the healed non-prefix
    projection. Historical assistant output follows only the shifted row;
    current output follows only the exact row. Composed through BOTH
    production self-heal entries — returned auth-failure result and raised
    401 — the historical row must never be accepted by
    ``_self_heal_result_succeeded`` nor persisted by
    ``_append_result_partial_on_error``; settlement/done/apperror must fire
    exactly once.
    """
    prompt = "new webui turn"
    sid = f"repeat-collision-{terminal}-{collision}"
    stream_id = f"stream-{sid}"
    db_path = tmp_path / "state.db"
    prior_messages = [
        {"role": "user", "content": prompt, "timestamp": 1.0},
        {"role": "assistant", "content": "historical answer", "timestamp": 2.0},
    ]
    _make_state_db(db_path, sid, prior_messages)
    session, event_queue = _install_streaming_session(
        monkeypatch,
        tmp_path,
        sid=sid,
        stream_id=stream_id,
        messages=prior_messages,
        context_messages=prior_messages,
    )
    session.pending_user_message = prompt

    class RepeatedPromptAgent:
        runs = 0

        def __init__(self, session_id=None, **_kwargs):
            self.session_id = session_id
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self._last_error = None
            self.stream_delta_callback = _kwargs.get("stream_delta_callback")

        def run_conversation(self, **kwargs):
            type(self).runs += 1
            history = list(kwargs.get("conversation_history") or [])
            if type(self).runs == 1:
                _append_state_row(
                    db_path,
                    sid,
                    role="user",
                    content=kwargs["persist_user_message"],
                    timestamp=3.0,
                )
                if terminal == "raised":
                    raise RuntimeError("401 unauthorized")
                return {
                    "messages": history,
                    "error": {
                        "type": "authentication_error",
                        "status_code": 401,
                        "message": "token invalid",
                    },
                }
            # Heal retry: the 2-row refreshed baseline makes the legacy
            # shifted probe (3 - 2 = 1) collide with the historical user row.
            heal = {
                "turn_id": "agent-heal-turn",
                "current_turn_user_idx": 3,
                "messages": _repeated_prompt_collision_result_messages(
                    prompt,
                    current_answer=(
                        "healed current answer"
                        if collision == "current_output_after_exact_row"
                        else None
                    ),
                ),
            }
            if collision == "current_output_after_exact_row":
                heal.update(
                    {"completed": True, "final_response": "healed current answer"}
                )
            elif collision == "historical_output_after_shifted_row_only":
                # Silent success shape: if the shifted historical row were
                # accepted, "historical answer" would satisfy the retry.
                heal.update({"completed": True, "final_response": ""})
            else:
                # Stale partial shape: if the shifted historical row were
                # accepted, "historical answer" would persist as the current
                # turn's partial.
                heal.update(
                    {
                        "completed": False,
                        "partial": True,
                        "failed": False,
                        "final_response": "",
                        "error": "compression_snapshot_stale",
                        "compression_snapshot_stale": True,
                    }
                )
            return heal

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: RepeatedPromptAgent)
    monkeypatch.setattr(
        streaming,
        "_attempt_credential_self_heal",
        lambda *_args, **_kwargs: {
            "api_key": "refreshed-key",
            "provider": "test-provider",
            "base_url": None,
            "credential_pool": None,
        },
    )

    streaming._run_agent_streaming(
        session_id=sid,
        msg_text=prompt,
        model="test-model",
        workspace=str(tmp_path),
        stream_id=stream_id,
        attachments=[],
    )

    assert RepeatedPromptAgent.runs == 2
    events = _drain_events(event_queue)
    done_events = [payload for event, payload in events if event == "done"]
    apperrors = [payload for event, payload in events if event == "apperror"]
    reloaded = Session.load(sid)
    assert reloaded is not None

    # Exact-once turn settlement in every arm: the pending turn is consumed.
    assert reloaded.active_stream_id is None
    assert reloaded.pending_user_message is None
    assert reloaded.pending_attachments == []

    current_turn_assistants = [
        str(message.get("content") or "")
        for message in reloaded.messages[2:]
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]
    if collision == "current_output_after_exact_row":
        # Success arm: exactly one done, no apperror, and the settled current
        # turn carries only the exact-row answer — persisted exactly once.
        assert len(done_events) == 1
        assert not apperrors
        assert current_turn_assistants == ["healed current answer"]
        done_messages = done_events[0]["session"]["messages"]
        assert [
            (message["role"], message.get("content")) for message in done_messages
        ] == [
            ("user", prompt),
            ("assistant", "historical answer"),
            ("user", prompt),
            ("assistant", "healed current answer"),
        ]
        assert not any(message.get("_error") for message in reloaded.messages)
    else:
        # Failure arms: the shifted historical row must never satisfy the
        # retry — exactly one apperror, never a done.
        assert not done_events
        assert len(apperrors) == 1
        if collision == "historical_output_after_shifted_row_only":
            assert apperrors[0]["type"] == "auth_mismatch"
        else:
            assert apperrors[0]["type"] == "compression_snapshot_stale"
        # Historical prose is never accepted as this turn's output nor
        # persisted as its partial.
        assert not any(
            message.get("_partial") for message in reloaded.messages
        )
        assert "historical answer" not in current_turn_assistants
        assert reloaded.messages[-1]["_error"] is True
    # The historical assistant row itself survives untouched exactly once, at
    # its original position in the transcript.
    assert [
        str(message.get("content") or "")
        for message in reloaded.messages
        if isinstance(message, dict) and message.get("role") == "assistant"
    ].count("historical answer") == 1
