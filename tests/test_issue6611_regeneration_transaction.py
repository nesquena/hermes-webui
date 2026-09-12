import copy
import queue
import threading
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


def _session(session_id="transaction6611"):
    rows = [
        {"role": "user", "content": "prompt", "id": "u1", "_source": "webui"},
        {"role": "assistant", "content": "failed"},
    ]
    return Session(
        session_id=session_id,
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


def test_locked_stale_plan_does_not_restore_a_request_time_snapshot(monkeypatch):
    from api import routes

    session = _session()
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
    )
    assert result["code"] == "stale_regeneration_revision"
    assert session.__dict__ == current_state


def test_locked_unexpected_plan_error_does_not_restore_a_request_time_snapshot(monkeypatch):
    from api import routes

    session = _session()
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
    )
    assert session.__dict__ == current_state


def test_regeneration_retired_run_journal_authority_rejects_before_mutation(monkeypatch):
    from api import routes, run_journal

    session = _session("regeneration-retired-before-mutation")
    plan = plan_regeneration(session)
    before = copy.deepcopy(session.__dict__)

    def retired(_session_id):
        raise run_journal.RunJournalRetiredAuthorityError("retired")

    monkeypatch.setattr(run_journal, "validate_run_journal_session_activation", retired)
    result = routes._start_regeneration_stream_locked(
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
    )

    assert result == {
        "error": "retired",
        "type": "run_journal_authority_unavailable",
        "_status": 409,
    }
    assert session.__dict__ == before


def test_regeneration_activation_retirement_restores_before_worker_or_journal(monkeypatch):
    from api import routes, run_journal, turn_journal

    session = _session("regeneration-retired-after-prepare")
    plan = plan_regeneration(session)
    before = copy.deepcopy(session.__dict__)

    monkeypatch.setattr(run_journal, "validate_run_journal_session_activation", lambda _sid: None)

    def retired(_session_id):
        raise run_journal.RunJournalRetiredAuthorityError("retired after prepare")

    monkeypatch.setattr(run_journal, "activate_run_journal_session", retired)
    monkeypatch.setattr(
        turn_journal,
        "append_turn_journal_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("journal submitted")),
    )
    monkeypatch.setattr(
        routes.threading,
        "Thread",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("worker created")),
    )

    result = routes._start_regeneration_stream_locked(
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
    )

    assert result == {
        "error": "retired after prepare",
        "type": "run_journal_authority_unavailable",
        "_status": 409,
    }
    assert session.__dict__ == before


def test_regeneration_passes_active_run_journal_incarnation_to_worker(monkeypatch):
    from api import routes, run_journal, turn_journal

    session = _session("regeneration-incarnation-worker")
    plan = plan_regeneration(session)
    worker_called = threading.Event()
    captured = {}

    monkeypatch.setattr(run_journal, "validate_run_journal_session_activation", lambda _sid: None)
    monkeypatch.setattr(run_journal, "activate_run_journal_session", lambda _sid: "incarnation-6611")
    monkeypatch.setattr(routes, "register_session_writeback_owner", lambda *_args: None)
    monkeypatch.setattr(routes, "register_stream_owner", lambda *_args: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: queue.Queue())
    monkeypatch.setattr(routes, "STREAMS", {})
    monkeypatch.setattr(
        turn_journal,
        "append_turn_journal_event",
        lambda *_args, **_kwargs: {"turn_id": "turn-6611"},
    )
    monkeypatch.setattr(Session, "save", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *_args: None)

    def worker(*_args, **kwargs):
        captured.update(kwargs)
        worker_called.set()

    monkeypatch.setattr(routes, "_run_agent_streaming", worker)
    result = routes._start_regeneration_stream_locked(
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
    )

    assert result["turn_id"] == "turn-6611"
    assert worker_called.wait(1)
    assert captured["run_journal_incarnation"] == "incarnation-6611"


def test_locked_preacceptance_exception_restores_the_transaction_snapshot(monkeypatch):
    from api import routes

    session = _session()
    plan = plan_regeneration(session)
    before = copy.deepcopy(session.__dict__)

    def fail_prepare(*_args, **_kwargs):
        raise RuntimeError("prepare failed")

    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", fail_prepare)
    with pytest.raises(RuntimeError, match="prepare failed"):
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
        )
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
        )
    assert raised.value._regeneration_accepted is True
    assert session.active_stream_id is not None
    assert session.__dict__ != before


def test_chat_start_losing_regeneration_preserves_locked_send_winner(monkeypatch, tmp_path):
    from api import routes
    from api import models as models_api
    import api.runtime_adapter as runtime_adapter

    session = _session("route-race-6611")
    session.model_explicit_pick_signature = "before-regeneration"
    revision = plan_regeneration(session).revision
    monkeypatch.setattr(models_api, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models_api, "SESSION_INDEX_FILE", tmp_path / "_index.json")
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

    winner_started = threading.Event()
    winner_finished = threading.Event()

    def lose_after_locked_send(*_args, **_kwargs):
        def normal_send_winner():
            with routes._get_session_agent_lock(session.session_id):
                session.active_stream_id = "winner-stream"
                session.pending_user_message = "winner prompt"
                session.pending_started_at = 222.0
                session.pending_user_source = "webui"
                session.save(touch_updated_at=False)
                winner_started.set()
                winner_finished.set()

        thread = threading.Thread(target=normal_send_winner)
        thread.start()
        assert winner_started.wait(1)
        assert winner_finished.wait(1)
        thread.join(timeout=1)
        return {
            "error": "stale regeneration revision",
            "code": "stale_regeneration_revision",
            "_status": 409,
        }

    captured = {}
    monkeypatch.setattr(routes, "_start_run", lose_after_locked_send)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, data, status=200, **_kwargs: captured.update(data=data, status=status),
    )
    routes._handle_chat_start(
        None,
        {
            "session_id": session.session_id,
            "regenerate": True,
            "regeneration_revision": revision,
            "explicit_model_pick": True,
        },
    )
    assert captured["status"] == 409
    assert (
        session.active_stream_id,
        session.pending_user_message,
        session.pending_started_at,
        session.pending_user_source,
    ) == ("winner-stream", "winner prompt", 222.0, "webui")
    assert session.model_explicit_pick_signature == "before-regeneration"
    reloaded = Session.load(session.session_id)
    assert (
        reloaded.active_stream_id,
        reloaded.pending_user_message,
        reloaded.pending_started_at,
        reloaded.pending_user_source,
    ) == ("winner-stream", "winner prompt", 222.0, "webui")


def test_regeneration_lock_winner_blocks_a_later_normal_send(monkeypatch):
    from api import routes

    session = _session()
    accepted = threading.Event()
    normal_checked = threading.Event()
    normal_result = {}

    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args: None)

    def accepted_regeneration(value, **_kwargs):
        value.active_stream_id = "accepted-regeneration"
        value.pending_user_message = "regenerated prompt"
        accepted.set()

        def ordinary_send_observer():
            with routes._get_session_agent_lock(value.session_id):
                normal_result["active_stream_id"] = value.active_stream_id
                normal_result["pending_user_message"] = value.pending_user_message
                normal_checked.set()

        thread = threading.Thread(target=ordinary_send_observer)
        thread.start()
        assert not normal_checked.wait(0.05)
        return {"stream_id": "accepted-regeneration", "session_id": value.session_id}

    monkeypatch.setattr(routes, "_start_regeneration_stream_locked", accepted_regeneration)
    result = routes._start_chat_stream_for_session(
        session,
        msg="ignored",
        attachments=[],
        workspace="C:/workspace",
        model="model",
        model_provider="provider",
        external_runtime_owned=False,
        regeneration=SimpleNamespace(revision="revision"),
    )

    assert accepted.is_set()
    assert result["stream_id"] == "accepted-regeneration"
    assert normal_checked.wait(1)
    assert normal_result == {
        "active_stream_id": "accepted-regeneration",
        "pending_user_message": "regenerated prompt",
    }


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


def test_regeneration_preview_has_no_request_snapshot_or_outer_restore_owner():
    source = (Path(__file__).parents[1] / "api" / "routes.py").read_text(encoding="utf-8")
    chat_start = source.index("def _handle_chat_start(")
    chat_end = source.index("def _resolve_chat_workspace_with_recovery", chat_start)
    body = source[chat_start:chat_end]
    assert "regeneration_snapshot = snapshot_regeneration_state" not in body
    assert "transaction_snapshot" not in body
    assert "_restore_regeneration_preacceptance" not in body
    regeneration_preview = body[body.index('if body.get("regenerate") is True:'):body.index('diag.stage("normalize_message")')]
    assert "_clear_stale_stream_state" not in regeneration_preview
    assert "model_explicit_pick_signature" not in regeneration_preview


def test_concurrent_normal_winner_survives_regeneration_409_in_memory_and_after_reload(monkeypatch, tmp_path):
    from api import routes
    from api import models as models_api

    session = _session("race-6611")
    session.active_stream_id = "stale-stream"
    session.pending_user_message = "stale prompt"
    session.pending_started_at = 111.0
    session.pending_user_source = "webui"
    monkeypatch.setattr(models_api, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models_api, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    session.save(touch_updated_at=False)

    winner_started = threading.Event()
    winner_finished = threading.Event()

    def stale_plan(*_args, **_kwargs):
        def normal_winner():
            winner_started.set()
            session.active_stream_id = "winner-stream"
            session.pending_user_message = "winner prompt"
            session.pending_started_at = 222.0
            session.pending_user_source = "webui"
            session.save(touch_updated_at=False)
            winner_finished.set()

        threading.Thread(target=normal_winner).start()
        assert winner_started.wait(1)
        assert winner_finished.wait(1)
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
    )
    assert result["_status"] == 409
    assert (session.active_stream_id, session.pending_user_message, session.pending_started_at, session.pending_user_source) == (
        "winner-stream", "winner prompt", 222.0, "webui"
    )
    reloaded = Session.load(session.session_id)
    assert (reloaded.active_stream_id, reloaded.pending_user_message, reloaded.pending_started_at, reloaded.pending_user_source) == (
        "winner-stream", "winner prompt", 222.0, "webui"
    )
