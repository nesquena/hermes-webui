"""Async delegation wakeups have durable provenance distinct from process notices."""

import threading
from types import SimpleNamespace

from api import background_process as bp
from api import routes, streaming
from api.models import Session, _append_recovered_pending_turn


def test_delegation_delivery_uses_distinct_source(monkeypatch):
    called = []
    done = threading.Event()
    monkeypatch.setattr(routes, "start_session_turn", lambda sid, prompt, **kwargs: (
        called.append((sid, prompt, kwargs.get("source"))),
        done.set(),
        {"stream_id": "stream-one", "_status": 200},
    )[-1])
    monkeypatch.setattr(bp, "_record_async_delegation_accepted", lambda *args, **kwargs: None)
    monkeypatch.setattr(bp, "_retry_unclaimed_async_delegation_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(bp, "release_async_delegation_delivery", lambda *args: None)
    bp._start_async_delegation_wakeup_turn(
        "sid", "child findings", delegation_id="deleg-1", evt={"type": "async_delegation"},
        claim=SimpleNamespace(), process_registry=None,
    )
    assert done.wait(3)
    assert called == [("sid", "child findings", "delegation_wakeup")]


def test_delegation_source_survives_eager_recovery_and_merge():
    source = "delegation_wakeup"
    text = "Child result; ordinary words, not a magic prefix"
    session = Session(session_id="quiet-source", pending_user_message=text, pending_user_source=source)
    recovered = _append_recovered_pending_turn(session)
    assert recovered["_source"] == source
    assert recovered["content"] == text
    eager = Session(session_id="quiet-eager")
    routes._checkpoint_user_message_for_eager_session_save(
        eager, text, [], 1.0, source=source,
    )
    assert eager.messages[0]["_source"] == source
    merged = streaming._merge_display_messages_after_agent_result(
        [], [], [{"role": "assistant", "content": "Synthesized findings"}], text, source=source,
    )
    assert merged[0]["_source"] == source
    assert merged[1]["content"] == "Synthesized findings"


def test_delegation_completion_event_has_explicit_kind():
    child = bp._build_payload({"type": "async_delegation", "delegation_id": "deleg-1"}, "sid")
    process = bp._build_payload({"type": "completion", "session_id": "proc-1"}, "sid")
    assert child["kind"] == "async_delegation"
    assert child["task_id"] == "deleg-1"
    assert "kind" not in process


def test_delegation_source_gets_process_wakeup_pause_semantics(monkeypatch):
    monkeypatch.setattr(routes, "get_session", lambda _sid: SimpleNamespace(
        session_id="sid", workspace="/tmp", model="test", model_provider="test", profile=None,
    ))
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda *_args: "/tmp")
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_args: (None, None, {}))
    monkeypatch.setattr(routes, "_resolve_compatible_session_model_state", lambda *args, **kwargs: ("test", "test", False))
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: threading.RLock())
    monkeypatch.setattr(routes, "clear_process_wakeup_pause_if_model_changed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(routes, "process_wakeup_pause_credential_state_changed", lambda *_args: False)
    monkeypatch.setattr(routes, "process_wakeup_pause_matches", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(routes, "suppress_process_wakeup_for_provider_pause", lambda *_args, **_kwargs: {"classification": "credential_pool_empty"})
    monkeypatch.setattr(routes, "_start_run", lambda *_args, **_kwargs: {"_status": 200, "stream_id": "should-not-run"})
    assert routes.start_session_turn("sid", "child findings", source="delegation_wakeup")["error"] == routes.PROCESS_WAKEUP_PAUSE_ERROR
