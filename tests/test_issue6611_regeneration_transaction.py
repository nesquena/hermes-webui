import copy
import queue
from types import SimpleNamespace
from pathlib import Path

import pytest

from api.models import Session
from api.session_ops import (
    RegenerationUnavailable,
    RegenerationPlan,
    apply_regeneration_plan,
    plan_regeneration,
    restore_regeneration_state,
    snapshot_regeneration_state,
)

from tests._issue6611_fixture import load_issue6611_fixture


def _session():
    rows = [
        {"role": "user", "content": "prompt", "id": "u1", "_source": "webui"},
        {"role": "assistant", "content": "failed"},
    ]
    return Session(
        session_id="transaction6611",
        messages=copy.deepcopy(rows),
        context_messages=copy.deepcopy(rows),
        workspace="C:/workspace",
    )


def test_plan_installs_rows_and_context_as_one_prepared_pair():
    session = _session()
    plan = plan_regeneration(session)
    assert isinstance(plan, RegenerationPlan)
    assert plan.canonical_rows == session.messages
    assert plan.canonical_context == session.context_messages
    applied, retained_context_user = apply_regeneration_plan(
        session,
        plan,
        return_context_user=True,
    )
    assert applied
    assert retained_context_user is session.context_messages[0]
    assert session.messages == plan.canonical_rows[: plan.truncation_boundary]
    assert session.context_messages == plan.canonical_context[: plan.truncation_boundary]
    assert any(row.get("content") == "prompt" for row in session.context_messages)
    assert [row["role"] for row in session.messages] == ["user"]


def test_apply_consumes_prepared_pair_without_ambient_authority_read(monkeypatch):
    session = _session()
    plan = plan_regeneration(session)
    monkeypatch.setattr(
        "api.session_ops.regeneration_state",
        lambda _session: (_ for _ in ()).throw(AssertionError("ambient read")),
    )
    session.messages[0]["content"] = "changed"
    assert apply_regeneration_plan(session, plan)
    assert session.messages == plan.canonical_rows[: plan.truncation_boundary]


def test_complete_session_snapshot_restores_every_attribute():
    session = _session()
    session.compression_state = {"marker": "kept"}
    session._anchor_scene_index = 7
    before = copy.deepcopy(session.__dict__)
    snapshot = snapshot_regeneration_state(session)
    session.messages.clear()
    session.context_messages.clear()
    session.compression_state["marker"] = "changed"
    session._anchor_scene_index = 99
    restore_regeneration_state(session, snapshot)
    assert session.__dict__ == before


def test_persisted_preacceptance_rollback_restores_exact_snapshot(monkeypatch):
    session = _session()
    snapshot = snapshot_regeneration_state(session)
    persisted = []
    monkeypatch.setattr(Session, "save", lambda self, **_kwargs: persisted.append(copy.deepcopy(self.__dict__)))
    session.active_stream_id = "stale"
    session.pending_user_message = "changed"
    restore_regeneration_state(session, snapshot)
    session.save(touch_updated_at=False)
    assert persisted == [snapshot]


def test_noop_rejection_does_not_need_persisted_rollback():
    session = _session()
    snapshot = snapshot_regeneration_state(session)
    restore_regeneration_state(session, snapshot)
    assert session.__dict__ == snapshot


def test_early_stale_cleanup_mutation_is_restored_before_rejected_start():
    session = _session()
    snapshot = snapshot_regeneration_state(session)
    session.active_stream_id = "stale-stream"
    session.pending_started_at = 123.0
    session.model_explicit_pick_signature = "before"

    def early_cleanup(value):
        value.active_stream_id = None
        value.pending_started_at = None

    early_cleanup(session)
    restore_regeneration_state(session, snapshot)
    assert session.__dict__ == snapshot
    source = (Path(__file__).parents[1] / "api" / "routes.py").read_text(encoding="utf-8")
    chat_start = source.index("def _handle_chat_start(")
    snapshot_offset = source.index("regeneration_snapshot = snapshot_regeneration_state(s)", chat_start)
    cleanup_offset = source.index("_clear_stale_stream_state(s)", snapshot_offset)
    assert snapshot_offset < cleanup_offset
    assert "regeneration_snapshot = snapshot_regeneration_state(s)" in source[cleanup_offset:]
    assert "regeneration_persisted_mutation = False" in source[cleanup_offset:]


def test_locked_stale_plan_does_not_restore_a_request_time_snapshot(monkeypatch):
    from api import routes

    session = _session()
    request_snapshot = copy.deepcopy(session.__dict__)
    session.active_stream_id = "accepted-after-browser-validation"
    current_state = copy.deepcopy(session.__dict__)

    def stale_plan(*_args, **_kwargs):
        raise RegenerationUnavailable("stale_regeneration_revision")

    monkeypatch.setattr("api.session_ops.plan_regeneration", stale_plan)
    result = routes._start_regeneration_stream_locked(
        session,
        turn=SimpleNamespace(revision="old-revision"),
        workspace="C:/workspace",
        model="model",
        model_provider="provider",
        normalized_model=False,
        diag=None,
        goal_related=False,
        source="webui",
        moa_config=None,
        backend_is_gateway=False,
        transaction_snapshot=request_snapshot,
    )
    assert result["code"] == "stale_regeneration_revision"
    assert result["_regeneration_locked_plan_rejected"] is True
    assert session.__dict__ == current_state


def test_locked_unexpected_plan_error_does_not_restore_a_request_time_snapshot(monkeypatch):
    from api import routes

    session = _session()
    request_snapshot = copy.deepcopy(session.__dict__)
    session.active_stream_id = "accepted-after-browser-validation"
    current_state = copy.deepcopy(session.__dict__)

    def unexpected_plan(*_args, **_kwargs):
        raise RuntimeError("plan failed")

    monkeypatch.setattr("api.session_ops.plan_regeneration", unexpected_plan)
    with pytest.raises(RuntimeError, match="plan failed"):
        routes._start_regeneration_stream_locked(
            session,
            turn=SimpleNamespace(revision="old-revision"),
            workspace="C:/workspace",
            model="model",
            model_provider="provider",
            normalized_model=False,
            diag=None,
            goal_related=False,
            source="webui",
            moa_config=None,
            backend_is_gateway=False,
            transaction_snapshot=request_snapshot,
        )
    assert session.__dict__ == current_state


def test_locked_preacceptance_exception_restores_the_transaction_snapshot(monkeypatch):
    from api import routes

    session = _session()
    plan = plan_regeneration(session)
    before = copy.deepcopy(session.__dict__)

    def fail_prepare(*_args, **_kwargs):
        raise RuntimeError("prepare failed")

    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", fail_prepare)
    with pytest.raises(RuntimeError, match="prepare failed") as raised:
        routes._start_regeneration_stream_locked(
            session,
            turn=plan.turn,
            workspace="C:/workspace",
            model="model",
            model_provider="provider",
            normalized_model=False,
            diag=None,
            goal_related=False,
            source="webui",
            moa_config=None,
            backend_is_gateway=False,
            transaction_snapshot=before,
        )
    assert raised.value._regeneration_preacceptance_restored is True
    assert session.__dict__ == before


def test_locked_postacceptance_workspace_exception_does_not_restore_turn(monkeypatch):
    from api import routes
    import api.turn_journal as turn_journal

    session = _session()
    plan = plan_regeneration(session)
    monkeypatch.setattr(routes, "register_session_writeback_owner", lambda *_args: None)
    monkeypatch.setattr(routes, "clear_session_writeback_owner_if_owned", lambda *_args: None)
    monkeypatch.setattr(routes, "register_stream_owner", lambda *_args: None)
    monkeypatch.setattr(routes, "unregister_stream_owner", lambda *_args: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: queue.Queue())
    monkeypatch.setattr(turn_journal, "append_turn_journal_event", lambda *_args, **_kwargs: {"turn_id": "turn-6611"})
    monkeypatch.setattr(Session, "save", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *_args: (_ for _ in ()).throw(RuntimeError("workspace failed")))

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

        def join(self, timeout=None):
            return None

    monkeypatch.setattr(routes.threading, "Thread", FakeThread)
    before = copy.deepcopy(session.__dict__)
    with pytest.raises(RuntimeError, match="workspace failed") as raised:
        routes._start_regeneration_stream_locked(
            session,
            turn=plan.turn,
            workspace="C:/workspace",
            model="model",
            model_provider="provider",
            normalized_model=False,
            diag=None,
            goal_related=False,
            source="webui",
            moa_config=None,
            backend_is_gateway=False,
            transaction_snapshot=before,
        )
    assert raised.value._regeneration_accepted is True
    assert session.active_stream_id is not None
    assert session.__dict__ != before


def test_chat_start_outer_exception_restores_regeneration_preacceptance(monkeypatch):
    from api import routes
    import api.runtime_adapter as runtime_adapter

    session = _session()
    revision = plan_regeneration(session).revision
    before = copy.deepcopy(session.__dict__)
    monkeypatch.setattr(runtime_adapter, "runtime_adapter_runner_enabled", lambda: False)
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_args: (None, None, {}))
    monkeypatch.setattr(routes, "_resolve_chat_workspace_for_regeneration", lambda *_args: "C:/workspace")
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda *_args: False)
    monkeypatch.setattr(routes, "_resolve_compatible_session_model_state", lambda *_args, **_kwargs: ("model", "provider", False))
    monkeypatch.setattr(routes, "_repair_foreign_session_model_provider", lambda *_args, **_kwargs: "provider")
    monkeypatch.setattr(routes, "compression_recovery_payload_for_session", lambda *_args: None)

    def fail_start(*_args, **_kwargs):
        raise RuntimeError("start failed before acceptance")

    monkeypatch.setattr(routes, "_start_run", fail_start)
    with pytest.raises(RuntimeError, match="start failed before acceptance"):
        routes._handle_chat_start(
            None,
            {
                "session_id": session.session_id,
                "regenerate": True,
                "regeneration_revision": revision,
            },
        )
    assert session.__dict__ == before


def test_prepare_mirrors_active_turn_token_to_context_before_timestamp_mutation(monkeypatch):
    from api import routes

    session = _session()
    retained_user = session.messages[0]
    session.context_messages = [
        {"role": "user", "content": "prompt"},
        {"role": "user", "content": "prompt"},
    ]
    retained_context_user = session.context_messages[1]
    monkeypatch.setattr(routes, "register_session_writeback_owner", lambda *_args: None)
    routes._prepare_chat_start_session_for_stream(
        session,
        msg="prompt",
        attachments=[],
        workspace="C:/workspace",
        model="model",
        model_provider="provider",
        stream_id="token-parity-stream",
        started_at=99.0,
        retained_user=retained_user,
        retained_context_user=retained_context_user,
        defer_save=True,
    )
    assert retained_user["timestamp"] == 99.0
    assert "timestamp" not in session.context_messages[0]
    assert "_active_turn_token" not in session.context_messages[0]
    assert retained_context_user["timestamp"] == 99.0
    assert retained_context_user["_active_turn_token"] == retained_user["_active_turn_token"]


def test_prepare_marks_new_fork_turn_in_eager_materialization(monkeypatch):
    from api import routes
    from api.streaming import _materialize_active_turn_user

    session = Session(
        session_id="fork-prepare-6611",
        messages=[],
        context_messages=[],
        session_source="fork",
        parent_session_id="parent-6611",
    )
    monkeypatch.setattr(routes, "register_session_writeback_owner", lambda *_args: None)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "eager")
    routes._prepare_chat_start_session_for_stream(
        session,
        msg="new fork prompt",
        attachments=[],
        workspace="C:/workspace",
        model="model",
        model_provider="provider",
        stream_id="fork-prepare-stream",
        started_at=99.0,
        defer_save=True,
    )
    assert len(session.messages) == 1
    assert session.messages[0]["_fork_child_turn"] == session.session_id

    deferred = Session(
        session_id="fork-deferred-6611",
        messages=[],
        context_messages=[],
        session_source="fork",
        parent_session_id="parent-6611",
    )
    routes._prepare_chat_start_session_for_stream(
        deferred,
        msg="deferred fork prompt",
        attachments=[],
        workspace="C:/workspace",
        model="model",
        model_provider="provider",
        stream_id="fork-deferred-stream",
        started_at=100.0,
        defer_save=True,
    )
    materialized = _materialize_active_turn_user(
        {
            "text": deferred.pending_user_message,
            "source": deferred.pending_user_source,
            "timestamp": deferred.pending_started_at,
            "session_id": deferred.session_id,
        },
        deferred.pending_user_message,
        deferred.pending_user_source,
    )
    assert materialized["_fork_child_turn"] == deferred.session_id


def test_issue_artifact_rows_follow_production_regeneration_and_error_settlement(monkeypatch, tmp_path):
    """Exercise production plan/apply/prepare/materializer/save-reload helpers, not HTTP integration."""
    from api import routes
    from api import models as models_api
    from api.session_ops import apply_regeneration_plan, plan_regeneration
    from api.streaming import _materialize_pending_user_turn_before_error

    rows = load_issue6611_fixture()["rows"]
    session = Session(
        session_id="artifact-production-6611",
        messages=copy.deepcopy(rows),
        context_messages=copy.deepcopy(rows),
        workspace="C:/workspace",
    )
    plan = plan_regeneration(session)
    assert apply_regeneration_plan(session, plan)
    monkeypatch.setattr(routes, "register_session_writeback_owner", lambda *_args: None)
    routes._prepare_chat_start_session_for_stream(
        session,
        msg=rows[0]["content"],
        attachments=[],
        workspace="C:/workspace",
        model="model",
        model_provider="provider",
        stream_id="artifact-production-stream",
        started_at=99.0,
        retained_user=session.messages[-1],
        defer_save=True,
    )
    assert [row["role"] for row in session.messages].count("user") == 1
    assert session.messages[0]["content"] == rows[0]["content"]
    assert _materialize_pending_user_turn_before_error(session) is False
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.pending_user_source = None
    session.messages.append(copy.deepcopy(rows[1]))
    assert [row["role"] for row in session.messages] == ["user", "assistant"]
    assert [row["content"] for row in session.messages if row["role"] == "user"] == [rows[0]["content"]]
    monkeypatch.setattr(models_api, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models_api, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    session.save(touch_updated_at=False)
    reloaded = Session.load(session.session_id)
    assert reloaded is not None
    assert [row["role"] for row in reloaded.messages] == ["user", "assistant"]
    assert [row["content"] for row in reloaded.messages if row["role"] == "user"] == [rows[0]["content"]]


def test_locked_start_always_replans_after_browser_validation():
    source = (Path(__file__).parents[1] / "api" / "routes.py").read_text(encoding="utf-8")
    start = source.index("def _start_regeneration_stream_locked(")
    end = source.index("def _active_run_stream_for_session", start)
    body = source[start:end]
    assert "plan = plan_regeneration(" in body
    assert "expected_revision=turn.revision" in body
    assert "lock_held=True" in body
    assert "hasattr(turn, \"canonical_rows\")" not in body
