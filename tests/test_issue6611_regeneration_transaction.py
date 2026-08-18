import copy
from pathlib import Path

from api.models import Session
from api.session_ops import (
    RegenerationPlan,
    apply_regeneration_plan,
    plan_regeneration,
    restore_regeneration_state,
    snapshot_regeneration_state,
)


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
    assert apply_regeneration_plan(session, plan)
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


def test_locked_start_always_replans_after_browser_validation():
    source = (Path(__file__).parents[1] / "api" / "routes.py").read_text(encoding="utf-8")
    start = source.index("def _start_regeneration_stream_locked(")
    end = source.index("def _active_run_stream_for_session", start)
    body = source[start:end]
    assert "plan = plan_regeneration(" in body
    assert "expected_revision=turn.revision" in body
    assert "lock_held=True" in body
    assert "hasattr(turn, \"canonical_rows\")" not in body
