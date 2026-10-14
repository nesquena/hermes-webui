"""Behavioral coverage for streaming skill provenance."""

import threading
from contextlib import nullcontext

import api.streaming as streaming
from api.models import Session


class _Session:
    def __init__(self, session_id="stream-session", read_only=False):
        self.session_id = session_id
        self.read_only = read_only
        self.active_stream_id = "stream-session"
        self.clear_generation = None
        self.skill_provenance = {}
        self.saved = []

    def record_skill_usage(self, names):
        changed = False
        for name in names if isinstance(names, (list, tuple, set)) else [names]:
            if isinstance(name, str) and name:
                self.skill_provenance[name] = self.skill_provenance.get(name, 0) + 1
                changed = True
        return changed

    def save(self, **kwargs):
        self.saved.append(kwargs)


def _record(*args, **kwargs):
    recorder = streaming._record_streaming_skill_usage
    kwargs.setdefault("stream_id", "stream-session")
    kwargs.setdefault("clear_generation", None)
    return recorder(*args, **kwargs)


def test_successful_skill_view_records_live_session(monkeypatch):
    session = _Session()
    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _sid: nullcontext())

    assert _record("stream-session", "terminal", {"success": True, "name": "forged"}) is False
    assert _record("stream-session", "skill_view", {"success": False, "name": "failed"}) is False
    assert _record("stream-session", "skill_view", {"success": True, "name": "review"}) is True
    assert session.skill_provenance == {"review": 1}
    assert session.saved == [{"touch_updated_at": False, "skip_index": True}]


def test_non_skill_failed_malformed_and_ephemeral_results_do_not_record(monkeypatch):
    session = _Session()
    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _sid: nullcontext())
    cases = [
        ("terminal", {"success": True, "name": "other"}, {}),
        ("skill_view", {"success": False, "name": "failed"}, {}),
        ("skill_view", {"success": True, "error": "failed", "name": "errored"}, {}),
        ("skill_view", "not json", {}),
        ("skill_view", {"success": True, "name": "ephemeral"}, {"ephemeral": True}),
    ]
    for tool_name, result, options in cases:
        assert _record("stream-session", tool_name, result, **options) is False
    assert session.skill_provenance == {}
    assert session.saved == []


def test_streaming_recorder_does_not_resurrect_deleted_owner_or_break_completion(monkeypatch):
    live = _Session()
    monkeypatch.setattr(
        streaming,
        "get_session",
        lambda _sid: (_ for _ in ()).throw(KeyError("deleted")),
    )
    assert _record("stream-session", "skill_view", {"success": True, "name": "review"}) is False
    assert live.skill_provenance == {}

    saved_failure = _Session()
    saved_failure.save = lambda **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable"))
    monkeypatch.setattr(streaming, "get_session", lambda _sid: saved_failure)
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _sid: nullcontext())
    assert _record("stream-session", "skill_view", {"success": True, "name": "review"}) is False
    assert saved_failure.skill_provenance == {"review": 1}


def test_streaming_read_only_owner_is_unchanged(monkeypatch):
    session = _Session(read_only=True)
    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _sid: nullcontext())
    assert _record("stream-session", "skill_view", {"success": True, "name": "review"}) is False
    assert session.skill_provenance == {}


def test_streaming_recorder_rejects_old_run_and_clear_generation(monkeypatch):
    session = _Session()
    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _sid: nullcontext())

    session.active_stream_id = "successor-stream"
    assert _record(
        "stream-session",
        "skill_view",
        {"success": True, "name": "old-run"},
        stream_id="old-stream",
    ) is False

    session.active_stream_id = "stream-session"
    session.clear_generation = "after-clear"
    assert _record(
        "stream-session",
        "skill_view",
        {"success": True, "name": "pre-clear"},
        clear_generation=None,
    ) is False
    assert session.skill_provenance == {}
    assert session.saved == []


def test_streaming_persists_a_real_session_sidecar(monkeypatch, tmp_path):
    import api.models as models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    session = Session(session_id="stream-sidecar")
    session.active_stream_id = "stream-sidecar-run"
    session.save(touch_updated_at=False, skip_index=True)
    monkeypatch.setattr(streaming, "get_session", lambda sid: Session.load(sid))
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _sid: nullcontext())
    assert _record(
        "stream-sidecar",
        "skill_view",
        '{"success":true,"name":"review"}',
        stream_id="stream-sidecar-run",
    ) is True
    assert Session.load("stream-sidecar").skill_provenance == {"review": 1}


def test_returned_auth_retry_allows_skill_completion_and_concurrent_stop(tmp_path, monkeypatch):
    from tests.test_compression_snapshot_revision import _install_streaming_session

    sid = "issue6593-self-heal-lock"
    stream_id = "stream-issue6593-self-heal-lock"
    _session, _events = _install_streaming_session(
        monkeypatch,
        tmp_path,
        sid=sid,
        stream_id=stream_id,
        messages=[],
        context_messages=[],
    )
    checks = {}
    journal_events = []
    monkeypatch.setattr(
        streaming,
        "append_turn_journal_event_for_stream",
        lambda *_args: journal_events.append(_args[-1]),
    )

    class AuthThenRetryAgent:
        runs = 0

        def __init__(self, session_id=None, tool_complete_callback=None, **_kwargs):
            self.session_id = session_id
            self.tool_complete_callback = tool_complete_callback
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def interrupt(self, _message):
            return None

        def run_conversation(self, **kwargs):
            type(self).runs += 1
            history = list(kwargs.get("conversation_history") or [])
            if type(self).runs == 1:
                return {
                    "messages": history,
                    "error": {
                        "type": "authentication_error",
                        "status_code": 401,
                        "message": "token invalid",
                    },
                }

            callback_done = threading.Event()

            def complete_skill_view():
                try:
                    self.tool_complete_callback(
                        "skill-call",
                        "skill_view",
                        {},
                        {"success": True, "name": "review"},
                    )
                finally:
                    callback_done.set()

            callback_thread = threading.Thread(target=complete_skill_view, daemon=True)
            callback_thread.start()
            callback_thread.join(timeout=5)
            checks["callback_finished_during_retry"] = callback_done.is_set()

            stop_done = threading.Event()

            def stop_stream():
                try:
                    checks["stop_result"] = streaming.cancel_stream(stream_id)
                finally:
                    stop_done.set()

            stop_thread = threading.Thread(target=stop_stream, daemon=True)
            stop_thread.start()
            stop_thread.join(timeout=5)
            checks["stop_finished_during_retry"] = stop_done.is_set()
            checks["threads"] = (callback_thread, stop_thread)
            return {
                "completed": True,
                "final_response": "recovered",
                "messages": history + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "recovered"},
                ],
            }

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: AuthThenRetryAgent)
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
    for thread in checks.get("threads", ()):
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in checks["threads"])

    assert AuthThenRetryAgent.runs == 2
    assert checks["callback_finished_during_retry"] is True
    assert checks["stop_finished_during_retry"] is True
    assert checks["stop_result"] is True
    assert any(event.get("event") == "interrupted" for event in journal_events)
    persisted = Session.load(sid)
    assert persisted.skill_provenance == {"review": 1}
    assert persisted.active_stream_id is None
    assert not any(
        message.get("content") == "recovered"
        for message in persisted.messages
        if isinstance(message, dict)
    )


def test_failed_retry_after_followup_admission_preserves_followup(tmp_path, monkeypatch):
    import api.config as config
    from tests.test_compression_snapshot_revision import _install_streaming_session

    sid = "issue6593-followup-writeback"
    stream_id = "stream-issue6593-followup-writeback"
    followup_id = "stream-issue6593-followup"
    session, _events = _install_streaming_session(
        monkeypatch,
        tmp_path,
        sid=sid,
        stream_id=stream_id,
        messages=[],
        context_messages=[],
    )
    monkeypatch.setattr(config, "SESSION_WRITEBACK_OWNERS", {})
    retry_entered = threading.Event()
    retry_can_raise = threading.Event()
    journal_events = []
    monkeypatch.setattr(
        streaming,
        "append_turn_journal_event_for_stream",
        lambda *_args: journal_events.append(_args[-1]),
    )

    class AuthThenRaisingRetryAgent:
        runs = 0

        def __init__(self, session_id=None, **_kwargs):
            self.session_id = session_id
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def interrupt(self, _message):
            return None

        def run_conversation(self, **kwargs):
            type(self).runs += 1
            if type(self).runs == 1:
                return {
                    "messages": list(kwargs.get("conversation_history") or []),
                    "error": {
                        "type": "authentication_error",
                        "status_code": 401,
                        "message": "token invalid",
                    },
                }
            retry_entered.set()
            if not retry_can_raise.wait(timeout=10):
                raise TimeoutError("retry test was not released")
            raise RuntimeError("refreshed credential also failed")

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: AuthThenRaisingRetryAgent)
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
    worker_errors = []

    def run_worker():
        try:
            streaming._run_agent_streaming(
                session_id=sid,
                msg_text="original turn",
                model="test-model",
                workspace=str(tmp_path),
                stream_id=stream_id,
                attachments=[],
            )
        except Exception as exc:
            worker_errors.append(exc)

    worker = threading.Thread(target=run_worker, daemon=True)
    worker.start()
    assert retry_entered.wait(timeout=5)

    stop_done = threading.Event()
    stop_result = []

    def stop_stream():
        try:
            stop_result.append(streaming.cancel_stream(stream_id))
        finally:
            stop_done.set()

    stop = threading.Thread(target=stop_stream, daemon=True)
    stop.start()
    stop.join(timeout=5)
    stopped_before_retry_raise = stop_done.is_set()
    followup_admitted_before_retry_raise = False
    if stopped_before_retry_raise and stop_result == [True]:
        with streaming._get_session_agent_lock(sid):
            session.active_stream_id = followup_id
            session.pending_user_message = "follow-up turn"
            session.pending_attachments = [{"name": "follow-up.txt"}]
            session.pending_started_at = 20.0
            session.pending_user_source = "webui"
            config.register_session_writeback_owner(sid, followup_id)
            session.save(touch_updated_at=False)
        followup_admitted_before_retry_raise = True

    retry_can_raise.set()
    worker.join(timeout=10)
    stop.join(timeout=5)

    assert not worker.is_alive()
    assert not stop.is_alive()
    assert not worker_errors
    assert AuthThenRaisingRetryAgent.runs == 2
    assert stopped_before_retry_raise is True
    assert followup_admitted_before_retry_raise is True
    persisted = Session.load(sid)
    assert persisted.active_stream_id == followup_id
    assert config.session_writeback_owner(sid) == followup_id
    assert persisted.pending_user_message == "follow-up turn"
    assert persisted.pending_attachments == [{"name": "follow-up.txt"}]
    assert not any(
        message.get("content") == "Authentication failed"
        for message in persisted.messages
        if isinstance(message, dict)
    )
    assert any(event.get("event") == "interrupted" for event in journal_events)
