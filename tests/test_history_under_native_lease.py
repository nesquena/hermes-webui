"""Rolling-compatible WebUI history preparation inside native admission."""
import pytest

from api import streaming


def test_loader_is_deferred_and_reconciles_fresh_model_context():
    current = [{"role": "assistant", "content": "before"}]
    calls = []

    def refresh():
        calls.append("read")
        return list(current), {"session_id": "s"}

    def project(messages):
        calls.append("project")
        return [{**m, "content": m["content"].upper()} for m in messages]

    def run(*, conversation_history_loader=None, **kw):
        return conversation_history_loader("s")

    loader = lambda key: streaming._load_history_after_admission("s", key, refresh, project)
    kwargs = streaming._build_run_conversation_kwargs(
        run, user_message="next", system_message=None, conversation_history=[],
        conversation_history_revision={"session_id": "s"}, task_id="s",
        persist_user_message="next", persist_user_timestamp=None,
        conversation_history_loader=loader)
    assert calls == []
    current.append({"role": "assistant", "content": "latest external reply"})
    assert run(**kwargs)[-1]["content"] == "LATEST EXTERNAL REPLY"
    assert calls == ["read", "project"]


@pytest.mark.parametrize("actual, revision", [("tip", {"session_id": "s"}), ("s", None), ("s", {"session_id": "other"})])
def test_loader_refuses_unknown_or_rotated_context(actual, revision):
    with pytest.raises(RuntimeError):
        streaming._load_history_after_admission("s", actual, lambda: ([], revision), lambda x: x)


@pytest.mark.requires_agent_modules
def test_real_native_lease_fences_webui_refresh(tmp_path):
    import inspect
    from types import SimpleNamespace
    admit_durable_turn_lease = pytest.importorskip("agent.turn_facade_lease").admit_durable_turn_lease
    from hermes_state import SessionDB

    if "conversation_history_loader" not in inspect.signature(admit_durable_turn_lease).parameters:
        pytest.skip("requires Agent's optional under-lease history loader")
    db = SessionDB(tmp_path / "state.db")
    peer = SessionDB(tmp_path / "state.db")
    db.create_session("s", source="webui")
    db.append_message("s", "user", "old")
    initial = db.get_messages_as_conversation("s")
    peer.append_message("s", "assistant", "external reply completed after snapshot")
    import threading
    agent = SimpleNamespace(_session_db=db, _persist_disabled=False, session_id="s",
                            _liveness_activity_lock=threading.RLock)
    admitted = None

    def refresh():
        assert not peer.try_acquire_session_turn_lease("s", "competitor")
        return db.get_messages_as_conversation("s"), {"session_id": "s"}

    try:
        admitted = admit_durable_turn_lease(
            agent, session_id="s", relay_turn_id="fixture", task_context={"platform": "webui"},
            conversation_history=initial,
            conversation_history_loader=lambda key: streaming._load_history_after_admission(
                "s", key, refresh, lambda messages: messages),
        )
        assert admitted.lease is not None
        assert admitted.conversation_history[-1]["content"] == "external reply completed after snapshot"
    finally:
        if admitted is not None and admitted.lease is not None:
            admitted.lease.release()
        peer.release_session_turn_lease("s", "competitor")
        peer.close()
        db.close()


@pytest.mark.requires_agent_modules
@pytest.mark.parametrize("anchor_state", ["removed", "missing", "valid"])
def test_real_reconciler_refuses_unverifiable_compressed_context(tmp_path, monkeypatch, anchor_state):
    import inspect
    import threading
    from types import SimpleNamespace
    from api import models
    admit = pytest.importorskip("agent.turn_facade_lease").admit_durable_turn_lease
    from hermes_state import SessionDB
    if "conversation_history_loader" not in inspect.signature(admit).parameters:
        pytest.skip("requires Agent's optional under-lease history loader")
    db = SessionDB(tmp_path / "state.db")
    peer = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")
    db.create_session("s", source="webui")
    db.append_message("s", "user", "old question", timestamp=1.0)
    db.append_message("s", "assistant", "old anchor", timestamp=2.0)
    old = [{"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] old local summary"}]
    session = SimpleNamespace(session_id="s", profile=None, messages=[], context_messages=old,
                              compression_anchor_message_key={"role": "assistant", "text": "old anchor", "ts": 2.0})
    if anchor_state == "removed":
        peer.archive_and_compact("s", [{"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] new durable summary"}])
    elif anchor_state == "missing":
        session.compression_anchor_message_key = None
    peer.append_message("s", "user", "external question")
    peer.append_message("s", "assistant", "fresh external reply")
    agent = SimpleNamespace(_session_db=db, _persist_disabled=False, session_id="s",
                            _liveness_activity_lock=threading.RLock)
    admitted = None

    def refresh():
        assert not peer.try_acquire_session_turn_lease("s", "competitor")
        kwargs = {"prefer_context": True, "with_revision": True,
                  "state_messages": models.get_state_db_session_messages("s", with_revision=True)}
        kwargs["require_reconciled"] = True
        snapshot = models.reconciled_state_db_messages_for_session(session, **kwargs)
        return snapshot.messages, snapshot.revision

    def enter():
        return admit(agent, session_id="s", relay_turn_id="fixture", task_context={"platform": "webui"},
                     conversation_history=old, conversation_history_loader=lambda key:
                     streaming._load_history_after_admission("s", key, refresh, lambda messages: messages))
    try:
        if anchor_state == "valid":
            admitted = enter()
            assert admitted.conversation_history[-1]["content"] == "fresh external reply"
        else:
            with pytest.raises(RuntimeError, match="compression_anchor"):
                admitted = enter()
    finally:
        if admitted is not None and admitted.lease is not None:
            admitted.lease.release()
        assert peer.try_acquire_session_turn_lease("s", "after-test")
        peer.release_session_turn_lease("s", "after-test")
        peer.close()
        db.close()


@pytest.mark.parametrize("anchor_state", ["removed", "missing", "valid"])
def test_strict_reconciliation_without_agent_checkout(anchor_state):
    """Exercise WebUI's real refusal path without the optional Agent fixture."""
    from types import SimpleNamespace
    from api import models

    summary = {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] local summary"}
    anchor = {"role": "assistant", "content": "old anchor", "timestamp": 2.0}
    latest = {"role": "assistant", "content": "fresh external reply", "timestamp": 4.0}
    anchor_key = {"role": "assistant", "text": "old anchor", "ts": 2.0}
    session = SimpleNamespace(
        session_id="s", messages=[], context_messages=[summary],
        compression_anchor_message_key=None if anchor_state == "missing" else anchor_key,
    )
    messages = [latest] if anchor_state == "removed" else [anchor, latest]
    revision = {"session_id": "s"}
    durable = models.StateDBSessionMessagesSnapshot(messages=messages, revision=revision)

    def reconcile():
        return models.reconciled_state_db_messages_for_session(
            session, prefer_context=True, state_messages=durable,
            require_reconciled=True, with_revision=True,
        )

    if anchor_state == "valid":
        result = reconcile()
        assert [message["content"] for message in result.messages] == [summary["content"], latest["content"]]
        assert result.revision == revision
    else:
        expected = "compression_anchor_missing" if anchor_state == "missing" else "compression_anchor_unverifiable"
        with pytest.raises(RuntimeError, match=expected):
            reconcile()


def test_old_agent_does_not_receive_loader():
    def old(user_message, system_message, conversation_history, task_id, persist_user_message):
        return conversation_history
    kwargs = streaming._build_run_conversation_kwargs(
        old, user_message="next", system_message=None, conversation_history=[],
        conversation_history_revision=None, task_id="s", persist_user_message="next",
        persist_user_timestamp=None, conversation_history_loader=lambda key: pytest.fail("eager read"))
    assert "conversation_history_loader" not in kwargs
    assert old(**kwargs) == []
