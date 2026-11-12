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


def test_eager_checkpoint_stamps_fork_ownership_for_delegation_wakeup():
    """Re-gate must-fix (eager mode): the eager checkpoint must carry the
    fork-ownership proof for a fork-internal delegation wakeup at chat-start,
    not only at settlement — regeneration before the turn settles otherwise
    hits regeneration_read_only (403). The row keeps ``_source:
    delegation_wakeup`` (the hidden-row predicate) AND gains
    ``_fork_child_turn`` (the ownership proof the gate reads), in both the
    create and reuse branches."""
    from api.session_ops import _selected_regeneration_turn_owned

    # Create branch: no existing row — the checkpoint appends a new one.
    create_session = Session(
        session_id="fork-child-eager",
        session_source="fork",
        parent_session_id="parent-7882",
    )
    routes._checkpoint_user_message_for_eager_session_save(
        create_session,
        "[ASYNC DELEGATION COMPLETE d1] internal handoff",
        [],
        1781024055.0,
        source="delegation_wakeup",
    )
    row = create_session.messages[0]
    assert row["_source"] == "delegation_wakeup", "hidden-row predicate must keep keying on _source"
    assert row["_fork_child_turn"] == "fork-child-eager"
    # The REAL gate must accept the checkpoint row (regeneration authorized).
    assert _selected_regeneration_turn_owned(create_session, row) is True

    # Reuse branch: the latest user row already holds the same text — the
    # stamp must land there too.
    reuse_session = Session(
        session_id="fork-child-eager-2",
        session_source="fork",
        parent_session_id="parent-7882",
    )
    reuse_session.messages = [
        {"role": "user", "content": "[ASYNC DELEGATION COMPLETE d1] internal handoff"}
    ]
    routes._checkpoint_user_message_for_eager_session_save(
        reuse_session,
        "[ASYNC DELEGATION COMPLETE d1] internal handoff",
        [],
        1781024056.0,
        source="delegation_wakeup",
    )
    reuse_row = reuse_session.messages[0]
    assert reuse_row["_fork_child_turn"] == "fork-child-eager-2"

    # A webui-session wakeup is NOT fork-owned: no proof stamp, gate rejects.
    webui_session = Session(
        session_id="webui-eager",
        session_source="webui",
    )
    routes._checkpoint_user_message_for_eager_session_save(
        webui_session,
        "[ASYNC DELEGATION COMPLETE d2] internal handoff",
        [],
        1781024057.0,
        source="delegation_wakeup",
    )
    assert "_fork_child_turn" not in webui_session.messages[0]


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
