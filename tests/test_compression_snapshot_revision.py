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

        def run_conversation(self, **_kwargs):
            nonlocal calls
            calls += 1
            if self.stream_delta_callback:
                self.stream_delta_callback("Partial work completed before the conflict.")
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
        and "Partial work completed" in str(message.get("content") or "")
        for message in reloaded.messages
    )
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
        "stale_non_prefix_without_current_user",
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
            if second_result != "recovered":
                if second_result == "stale_non_prefix_without_current_user":
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
                else:
                    stale_messages = history + [
                        {"role": "user", "content": "new webui turn"},
                        {"role": "assistant", "content": "partial stale"},
                    ]
                stale_result = {
                    "completed": False,
                    "final_response": (
                        ""
                        if second_result
                        in (
                            "stale_without_current_assistant",
                            "stale_non_prefix_compacted",
                            "stale_non_prefix_without_current_user",
                        )
                        else "partial stale"
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
                "messages": history + [{"role": "assistant", "content": "recovered"}],
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
        ):
            assert not partials
            assert not any(
                message.get("content") == "prior answer" and message.get("_partial") is True
                for message in reloaded.messages
            )
        else:
            assert [message.get("content") for message in partials] == ["partial stale"]
        assert reloaded.messages[-1]["_error"] is True
        assert "next message" in reloaded.messages[-1]["content"].lower()
