"""Production-composed contract tests for issue #6610 RESPEC-4."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import pytest


def _message(content="provider failed", **extra):
    row = {"role": "assistant", "content": content, "_error": True,
           "_provider_error_type": "error", "timestamp": 7}
    row.update(extra)
    return row


class _FakeSession:
    def __init__(self, messages, **flags):
        self.session_id = flags.pop("session_id", "issue6610-session")
        self.messages = messages
        self.profile = "default"
        self.session_source = "webui"
        self.source_tag = "webui"
        self.raw_source = "webui"
        self.is_cli_session = False
        self.read_only = False
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_started_at = None
        self.updated_at = 11.0
        self.save_count = 0
        self.save_error = None
        for key, value in flags.items():
            setattr(self, key, value)

    def save(self, **kwargs):
        self.save_count += 1
        if self.save_error:
            raise self.save_error


def _plan(session, index=0):
    from api.session_ops import provider_error_dismissal_plan
    return provider_error_dismissal_plan(session, index)


def test_reference_is_fixed_size_for_long_ascii_and_multibyte_content():
    from api.session_ops import provider_error_dismissal_plan
    for content in ["a" * 8191, "b" * 8192, "c" * 9000, "x" * 65536, "é" * 9000]:
        session = _FakeSession([_message(content)])
        plan = provider_error_dismissal_plan(session, 0)
        assert len(plan.dismiss_ref) == 64 and plan.dismiss_ref == plan.dismiss_ref.lower()


def test_dismissal_changes_only_owner_and_uses_private_save_flags():
    from api.session_ops import apply_provider_error_dismissal
    session = _FakeSession([{"role": "user", "content": "prompt"}, _message(), {"role": "assistant", "content": "later"}])
    plan = _plan(session, 1)
    with patch.object(session, "save", wraps=session.save) as save:
        apply_provider_error_dismissal(session, plan.dismiss_ref)
    assert session.messages[1]["_dismissed"] is True
    assert session.messages[0]["content"] == "prompt" and session.messages[2]["content"] == "later"
    save.assert_called_once_with(touch_updated_at=False, skip_index=True)
    assert session.updated_at == 11.0


@pytest.mark.parametrize("initial", [None, False])
def test_forced_save_failure_restores_exact_absent_or_false_marker(initial):
    from api.session_ops import ProviderErrorDismissalUnavailable, apply_provider_error_dismissal
    row = _message()
    if initial is not None:
        row["_dismissed"] = initial
    session = _FakeSession([row])
    session.save_error = OSError("sidecar unavailable")
    plan = _plan(session)
    before = copy.deepcopy(session.__dict__)
    with pytest.raises(ProviderErrorDismissalUnavailable):
        apply_provider_error_dismissal(session, plan.dismiss_ref)
    assert session.messages == before["messages"]
    assert session.updated_at == before["updated_at"]
    session.save_error = None
    session.save()
    assert session.messages[0].get("_dismissed") is initial


def test_stale_duplicate_cross_session_and_malformed_references_fail_closed():
    from api.session_ops import ProviderErrorDismissalUnavailable, apply_provider_error_dismissal
    first = _FakeSession([_message("same")])
    second = _FakeSession([_message("same")], session_id="other")
    first_plan = _plan(first)
    with pytest.raises(ProviderErrorDismissalUnavailable):
        apply_provider_error_dismissal(second, first_plan.dismiss_ref)
    first.messages.insert(0, {"role": "user", "content": "stale"})
    with pytest.raises(ProviderErrorDismissalUnavailable):
        apply_provider_error_dismissal(first, first_plan.dismiss_ref)
    with pytest.raises(ProviderErrorDismissalUnavailable):
        apply_provider_error_dismissal(first, "x" * 64)


def test_control_imported_busy_and_ambiguous_rows_have_no_capability():
    from api.session_ops import provider_error_dismissal_ref
    for row in (_message(provider_details_label="Cancellation details"), _message(type="interrupted"), _message(_compressionRecovery={"kind": "retry"})):
        assert provider_error_dismissal_ref(_FakeSession([row]), 0) is None
    assert provider_error_dismissal_ref(_FakeSession([_message()], is_cli_session=True), 0) is None
    assert provider_error_dismissal_ref(_FakeSession([_message()], active_stream_id="run"), 0) is None
    assert provider_error_dismissal_ref(_FakeSession([_message()], pending_started_at=12.0), 0) is None
    assert provider_error_dismissal_ref(_FakeSession([_message()], pending_attachments=[{"name": "x"}]), 0) is None
    unknown = _FakeSession([_message()])
    unknown.session_source = unknown.raw_source = unknown.source_tag = None
    assert provider_error_dismissal_ref(unknown, 0) is None


def test_blank_sidecar_source_uses_state_db_webui_authority():
    from api.session_ops import provider_error_dismissal_ref

    session = _FakeSession([_message()])
    session.session_source = session.raw_source = session.source_tag = None
    with patch("api.routes._state_db_session_source", return_value="webui"):
        assert provider_error_dismissal_ref(session, 0)


def test_sidecar_source_conflict_with_state_db_fails_closed():
    from api.session_ops import provider_error_dismissal_ref

    session = _FakeSession([_message()])
    with patch("api.routes._state_db_session_source", return_value="subagent"):
        assert provider_error_dismissal_ref(session, 0) is None


def test_webui_sidecar_source_is_compatible_with_gateway_state_db_source():
    from api.session_ops import provider_error_dismissal_ref

    session = _FakeSession([_message()])
    with patch("api.routes._state_db_session_source", return_value="api_server"):
        assert provider_error_dismissal_ref(session, 0)


def test_gateway_state_db_source_without_webui_sidecar_provenance_fails_closed():
    from api.session_ops import provider_error_dismissal_ref

    session = _FakeSession([_message()])
    session.session_source = session.raw_source = session.source_tag = None
    with patch("api.routes._state_db_session_source", return_value="api_server"):
        assert provider_error_dismissal_ref(session, 0) is None


def test_webui_chat_start_persists_gateway_ownership_marker(tmp_path, monkeypatch):
    from api import models
    from api.models import Session
    import api.routes as routes

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "index.json")
    monkeypatch.setattr(routes, "register_session_writeback_owner", lambda *args, **kwargs: None)
    session = Session(session_id="issue6610-gateway-marker", workspace=tmp_path)
    routes._prepare_chat_start_session_for_stream(
        session,
        msg="gateway marker",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
        stream_id="issue6610-gateway-stream",
        started_at=123.0,
    )
    assert session.session_source == session.raw_source == session.source_tag == "webui"
    assert session.source_label == "WebUI"
    reloaded = models.Session.load(session.session_id)
    assert reloaded is not None
    assert reloaded.session_source == reloaded.raw_source == reloaded.source_tag == "webui"
    assert reloaded.source_label == "WebUI"


def test_dismiss_route_accepts_webui_sidecar_over_gateway_state_source():
    import api.routes as routes

    session = _FakeSession([_message()])
    with (
        patch.object(routes, "get_session", return_value=session),
        patch.object(routes, "_lookup_cli_session_metadata", return_value={
            "source_tag": "api_server",
            "raw_source": "api_server",
            "session_source": "api",
        }),
        patch.object(routes, "_state_db_session_source", return_value="api_server"),
    ):
        assert routes._dismiss_error_source_rejection(session.session_id, None) is None


def test_fork_source_remains_authoritative_over_webui_state_mirror():
    from api.session_ops import provider_error_dismissal_ref

    session = _FakeSession([_message()])
    session.session_source = session.raw_source = session.source_tag = "fork"
    with patch("api.routes._state_db_session_source", return_value="webui"):
        assert provider_error_dismissal_ref(session, 0)


def test_missing_compression_parent_fails_closed_without_key_error():
    from api.session_ops import provider_error_dismissal_ref

    session = _FakeSession([_message()], parent_session_id="missing-parent")
    with patch("api.session_ops.get_session", side_effect=KeyError("missing-parent")):
        assert provider_error_dismissal_ref(session, 0) is None


def test_repeated_reference_is_idempotent_after_the_row_is_dismissed():
    from api.session_ops import apply_provider_error_dismissal

    session = _FakeSession([_message()])
    plan = _plan(session)
    first = apply_provider_error_dismissal(session, plan.dismiss_ref)
    second = apply_provider_error_dismissal(session, plan.dismiss_ref)
    assert first.dismiss_ref == second.dismiss_ref == plan.dismiss_ref
    assert session.save_count == 1


def test_reference_requires_stable_id_and_complete_row_digest():
    from api.session_ops import project_provider_error_dismissal_capabilities

    first = _message("first", id="same-id")
    second = _message("second", id="same-id")
    session = _FakeSession([first, second])
    projected = project_provider_error_dismissal_capabilities(
        session,
        {"messages": [copy.deepcopy(second)]},
    )
    assert projected["messages"][0]["_provider_error_dismiss_ref"]
    projected["messages"][0]["content"] = "changed"
    assert "_provider_error_dismiss_ref" not in project_provider_error_dismissal_capabilities(
        session,
        projected,
    )["messages"][0]


def test_conflicting_stable_id_aliases_fail_closed():
    from api.session_ops import project_provider_error_dismissal_capabilities

    session = _FakeSession([_message(id="one", message_id="two")])
    projected = project_provider_error_dismissal_capabilities(
        session,
        {"messages": copy.deepcopy(session.messages)},
    )
    assert "_provider_error_dismiss_ref" not in projected["messages"][0]


@pytest.mark.parametrize("row", [
    _message(id="", message_id="valid"),
    _message(id="   "),
    _message(id=1, message_id=True),
])
def test_malformed_stable_id_aliases_fail_closed(row):
    from api.session_ops import project_provider_error_dismissal_capabilities

    session = _FakeSession([row])
    projected = project_provider_error_dismissal_capabilities(
        session,
        {"messages": copy.deepcopy(session.messages)},
    )
    assert "_provider_error_dismiss_ref" not in projected["messages"][0]


def test_anchor_scene_transport_fields_do_not_change_owner_digest():
    from api.session_ops import project_provider_error_dismissal_capabilities

    session = _FakeSession([_message()])
    projected = project_provider_error_dismissal_capabilities(
        session,
        {"messages": [{**copy.deepcopy(session.messages[0]), "_anchor_activity_scene": {"version": 1}, "_anchor_stream_id": "run"}]},
    )
    assert projected["messages"][0]["_provider_error_dismiss_ref"]


def test_state_db_api_content_does_not_change_owner_digest():
    from api.session_ops import project_provider_error_dismissal_capabilities

    session = _FakeSession([_message(api_content={"private": True})])
    projected = project_provider_error_dismissal_capabilities(
        session,
        {"messages": [{**copy.deepcopy(session.messages[0]), "api_content": {"private": True}}]},
    )
    assert projected["messages"][0]["_provider_error_dismiss_ref"]


def test_conflicting_state_db_api_content_fails_closed():
    from api.session_ops import project_provider_error_dismissal_capabilities

    session = _FakeSession([_message()])
    projected = project_provider_error_dismissal_capabilities(
        session,
        {"messages": [{**copy.deepcopy(session.messages[0]), "api_content": {"private": True}}]},
    )
    assert "_provider_error_dismiss_ref" not in projected["messages"][0]


def test_dismissed_projection_uses_existing_marker_and_reference_only():
    from api.session_ops import project_provider_error_dismissal_capabilities

    session = _FakeSession([_message(_dismissed=True)])
    projected = project_provider_error_dismissal_capabilities(
        session,
        {"messages": copy.deepcopy(session.messages)},
    )
    row = projected["messages"][0]
    assert row["_dismissed"] is True
    assert row["_provider_error_dismiss_ref"]
    assert "_provider_error_dismissed" not in row


def test_unsaved_dismissed_marker_is_not_projected(tmp_path, monkeypatch):
    from api import models
    from api.session_ops import project_provider_error_dismissal_capabilities

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    session = models.Session(
        session_id="issue6610-unsaved-marker",
        workspace=tmp_path,
        messages=[_message()],
        source_tag="webui",
    )
    session.save(touch_updated_at=False, skip_index=True)
    loaded = models.Session.load(session.session_id)
    loaded.messages[0]["_dismissed"] = True
    projected = project_provider_error_dismissal_capabilities(
        loaded,
        {"messages": copy.deepcopy(loaded.messages)},
    )
    assert projected["messages"][0].get("_dismissed") is None
    assert projected["messages"][0]["_provider_error_dismiss_ref"]


def test_compression_lineage_projects_and_mutates_the_physical_owner():
    from api.session_ops import apply_provider_error_dismissal, project_provider_error_dismissal_capabilities

    parent = _FakeSession([_message("parent")], session_id="parent-session")
    parent.pre_compression_snapshot = True
    child = _FakeSession([_message("child")], session_id="child-session", parent_session_id=parent.session_id)
    with patch("api.session_ops.get_session", side_effect=lambda sid: parent if sid == parent.session_id else child):
        projected = project_provider_error_dismissal_capabilities(
            child,
            {"messages": copy.deepcopy(parent.messages + child.messages)},
        )
        ref = projected["messages"][0]["_provider_error_dismiss_ref"]
        plan = apply_provider_error_dismissal(child, ref)
    assert plan.owner_session_id == parent.session_id
    assert parent.messages[0]["_dismissed"] is True
    assert child.messages[0].get("_dismissed") is None


def test_cumulative_compression_child_owns_rows_replayed_from_parent():
    from api.session_ops import project_provider_error_dismissal_capabilities

    parent = _FakeSession([_message("same")], session_id="parent-cumulative")
    parent.pre_compression_snapshot = True
    child = _FakeSession(
        [copy.deepcopy(parent.messages[0]), _message("later")],
        session_id="child-cumulative",
        parent_session_id=parent.session_id,
    )
    with patch("api.session_ops.get_session", return_value=parent):
        projected = project_provider_error_dismissal_capabilities(
            child,
            {"messages": copy.deepcopy(child.messages)},
        )
    assert projected["messages"][0]["_provider_error_dismiss_ref"]
    assert projected["messages"][1]["_provider_error_dismiss_ref"]


def test_compression_lineage_deduplicates_aliased_agent_locks():
    from api import config
    from api.session_ops import apply_provider_error_dismissal, project_provider_error_dismissal_capabilities

    parent = _FakeSession([_message("parent")], session_id="parent-lock")
    parent.pre_compression_snapshot = True
    child = _FakeSession([_message("child")], session_id="child-lock", parent_session_id=parent.session_id)
    with patch("api.session_ops.get_session", side_effect=lambda sid: parent if sid == parent.session_id else child):
        projected = project_provider_error_dismissal_capabilities(
            child,
            {"messages": copy.deepcopy(parent.messages + child.messages)},
        )
        ref = projected["messages"][0]["_provider_error_dismiss_ref"]
        lock = config._get_session_agent_lock(parent.session_id)
        config._alias_session_agent_lock(parent.session_id, child.session_id, lock)
        try:
            plan = apply_provider_error_dismissal(child, ref)
        finally:
            with config.SESSION_AGENT_LOCKS_LOCK:
                config.SESSION_AGENT_LOCKS.pop(parent.session_id, None)
                config.SESSION_AGENT_LOCKS.pop(child.session_id, None)
    assert plan.owner_session_id == parent.session_id


def test_compressed_fork_continuation_keeps_fork_parent_owner():
    from api.session_ops import project_provider_error_dismissal_capabilities

    parent = _FakeSession([_message("parent")], session_id="fork-parent")
    parent.pre_compression_snapshot = True
    parent.session_source = parent.raw_source = parent.source_tag = "fork"
    child = _FakeSession(
        [_message("child")],
        session_id="fork-child",
        parent_session_id=parent.session_id,
    )
    child.session_source = child.raw_source = child.source_tag = "fork"
    with patch("api.session_ops.get_session", return_value=parent):
        projected = project_provider_error_dismissal_capabilities(
            child,
            {"messages": copy.deepcopy(parent.messages + child.messages)},
        )
    assert projected["messages"][0]["_provider_error_dismiss_ref"]


def test_settlement_restores_producer_snapshot_before_a_later_unrelated_save():
    from api.session_ops import settle_provider_error_session

    session = _FakeSession([{"role": "user", "content": "prompt"}], pending_user_message="draft")
    producer_snapshot = copy.deepcopy(session.__dict__)
    session.messages.append({"role": "assistant", "content": "partial"})
    session.pending_user_message = None
    session.save_error = OSError("disk")
    assert settle_provider_error_session(session, _message(), snapshot=producer_snapshot) is False
    assert session.__dict__ == producer_snapshot
    session.save_error = None
    session.messages.append({"role": "user", "content": "later"})
    session.save()
    assert session.messages == producer_snapshot["messages"] + [{"role": "user", "content": "later"}]


def test_unreadable_settlement_snapshot_restores_caller_state(tmp_path):
    from api.session_ops import settle_provider_error_session

    session = _FakeSession(
        [{"role": "user", "content": "prompt"}],
        active_stream_id="run",
        pending_user_message="draft",
    )
    session.path = tmp_path / "unreadable.json"
    session.path.write_text("{}", encoding="utf-8")
    before = copy.deepcopy(session.__dict__)
    with patch("api.session_ops.Path.read_bytes", side_effect=OSError("unreadable")):
        assert settle_provider_error_session(session, _message()) is False
    assert session.__dict__ == before


def test_real_sidecar_reload_stays_clean_after_failed_dismissal_then_unrelated_save(tmp_path, monkeypatch):
    from api import models
    from api.session_ops import ProviderErrorDismissalUnavailable, apply_provider_error_dismissal, provider_error_dismissal_plan

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    session = models.Session(
        session_id="issue6610-real",
        workspace=tmp_path,
        messages=[_message()],
        source_tag="webui",
    )
    session.save(touch_updated_at=False, skip_index=True)
    loaded = models.Session.load(session.session_id)
    plan = provider_error_dismissal_plan(loaded, 0)
    with patch.object(models, "_safe_replace", side_effect=OSError("sidecar unavailable")):
        with pytest.raises(ProviderErrorDismissalUnavailable):
            apply_provider_error_dismissal(loaded, plan.dismiss_ref)
    assert models.Session.load(session.session_id).messages[0].get("_dismissed") is None
    assert not list(tmp_path.glob("*.tmp.*"))
    loaded.messages.append({"role": "user", "content": "later"})
    loaded.save(touch_updated_at=False, skip_index=True)
    reloaded = models.Session.load(session.session_id)
    assert reloaded.messages[0].get("_dismissed") is None
    assert reloaded.messages[-1]["content"] == "later"


def test_settlement_helper_rolls_back_failed_producer_and_stamps_successful_rows():
    from api.session_ops import settle_provider_error_session
    session = _FakeSession([])
    session.save_error = OSError("disk")
    assert settle_provider_error_session(session, _message()) is False and session.messages == []
    session.save_error = None
    assert settle_provider_error_session(session, _message()) is True and isinstance(session.messages[0].get("id"), int)


def test_settlement_rolls_back_sidecar_when_index_write_fails(tmp_path, monkeypatch):
    from api import models
    from api.session_ops import settle_provider_error_session

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    session = models.Session(
        session_id="issue6610-producer-index",
        workspace=tmp_path,
        messages=[{"role": "user", "content": "prompt"}],
        source_tag="webui",
    )
    session.save(touch_updated_at=False, skip_index=True)
    loaded = models.Session.load(session.session_id)
    with patch.object(models, "_write_session_index", side_effect=OSError("index unavailable")):
        assert settle_provider_error_session(loaded, _message()) is False
    assert models.Session.load(session.session_id).messages == [{"role": "user", "content": "prompt"}]


def test_settlement_does_not_confuse_preexisting_duplicate_with_new_commit(tmp_path, monkeypatch):
    from api import models
    from api.session_ops import settle_provider_error_session

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    row = _message(id="same-row")
    session = models.Session(
        session_id="issue6610-preexisting-duplicate",
        workspace=tmp_path,
        messages=[row],
        source_tag="webui",
    )
    session.save(touch_updated_at=False, skip_index=True)
    loaded = models.Session.load(session.session_id)
    with patch.object(loaded, "save", side_effect=OSError("save unavailable")), \
         patch("api.session_ops._restore_provider_error_sidecar", return_value=False):
        assert settle_provider_error_session(loaded, _message(id="same-row")) is False
    assert len(loaded.messages) == 1


def test_corrupt_original_sidecar_can_confirm_a_committed_producer_row(tmp_path, monkeypatch):
    from api.session_ops import settle_provider_error_session

    class _PersistingFailure(_FakeSession):
        def save(self, **kwargs):
            self.path.write_text(json.dumps({"messages": self.messages}), encoding="utf-8")
            raise OSError("post-replace failure")

    session = _PersistingFailure([])
    session.path = tmp_path / "corrupt-original.json"
    session.path.write_text("{broken", encoding="utf-8")
    with patch("api.session_ops._restore_provider_error_sidecar", return_value=False):
        assert settle_provider_error_session(session, _message()) is True
    assert len(json.loads(session.path.read_text(encoding="utf-8"))["messages"]) == 1


def test_dismissal_replace_then_raise_returns_success_when_disk_commit_survives(tmp_path, monkeypatch):
    from api import models
    from api.session_ops import apply_provider_error_dismissal, provider_error_dismissal_plan

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    session = models.Session(
        session_id="issue6610-dismiss-partial",
        workspace=tmp_path,
        messages=[_message()],
        source_tag="webui",
    )
    session.save(touch_updated_at=False, skip_index=True)
    loaded = models.Session.load(session.session_id)
    plan = provider_error_dismissal_plan(loaded, 0)
    original_replace = models._safe_replace

    def replace_then_raise(src, dst):
        original_replace(src, dst)
        raise OSError("post-replace failure")

    with patch.object(models, "_safe_replace", side_effect=replace_then_raise), \
         patch("api.session_ops._restore_provider_error_sidecar", return_value=False):
        result = apply_provider_error_dismissal(loaded, plan.dismiss_ref)
    assert result.dismiss_ref == plan.dismiss_ref
    assert models.Session.load(session.session_id).messages[0]["_dismissed"] is True


def test_dismissal_corrupt_original_can_confirm_a_committed_marker(tmp_path, monkeypatch):
    from api import models
    from api.session_ops import apply_provider_error_dismissal, provider_error_dismissal_plan

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    session = models.Session(
        session_id="issue6610-dismiss-corrupt",
        workspace=tmp_path,
        messages=[_message()],
        source_tag="webui",
    )
    session.save(touch_updated_at=False, skip_index=True)
    loaded = models.Session.load(session.session_id)
    plan = provider_error_dismissal_plan(loaded, 0)
    session.path.write_text("{broken", encoding="utf-8")
    original_replace = models._safe_replace

    def replace_then_raise(src, dst):
        original_replace(src, dst)
        raise OSError("post-replace failure")

    with patch.object(models, "_safe_replace", side_effect=replace_then_raise), \
         patch("api.session_ops._restore_provider_error_sidecar", return_value=False):
        result = apply_provider_error_dismissal(loaded, plan.dismiss_ref)
    assert result.dismiss_ref == plan.dismiss_ref


def test_settlement_keeps_committed_sidecar_when_rollback_is_unavailable(tmp_path, monkeypatch):
    from api import models
    from api.session_ops import settle_provider_error_session

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    session = models.Session(
        session_id="issue6610-producer-rollback",
        workspace=tmp_path,
        messages=[{"role": "user", "content": "prompt"}],
        source_tag="webui",
    )
    session.save(touch_updated_at=False, skip_index=True)
    loaded = models.Session.load(session.session_id)
    with patch.object(models, "_write_session_index", side_effect=OSError("index unavailable")), \
         patch("api.session_ops._restore_provider_error_sidecar", return_value=False):
        assert settle_provider_error_session(loaded, _message()) is True
    assert models.Session.load(session.session_id).messages[-1]["_error"] is True


def test_failed_terminal_cleanup_materializes_pending_prompt_and_clears_runtime_state():
    from api.streaming import _clear_failed_provider_error_lifecycle

    session = _FakeSession(
        [],
        active_stream_id="run",
        pending_user_message="draft",
        pending_started_at=100.0,
        pending_user_source="webui",
    )
    _clear_failed_provider_error_lifecycle(session)
    assert session.messages[0]["role"] == "user"
    assert session.messages[0]["content"] == "draft"
    assert session.active_stream_id is None
    assert session.pending_user_message is None
    assert session.pending_started_at is None


def test_failed_local_cleanup_persists_recovered_prompt_without_index(tmp_path, monkeypatch):
    from api import models
    from api.session_ops import settle_provider_error_session
    from api.streaming import _clear_failed_provider_error_lifecycle

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    session = models.Session(
        session_id="issue6610-local-cleanup",
        workspace=tmp_path,
        messages=[],
        active_stream_id="run-cleanup",
        pending_user_message="draft",
        pending_started_at=100.0,
        pending_user_source="webui",
        source_tag="webui",
    )
    session.save(touch_updated_at=False, skip_index=True)
    loaded = models.Session.load(session.session_id)
    with patch.object(models, "_write_session_index", side_effect=OSError("index unavailable")):
        assert settle_provider_error_session(loaded, _message()) is False
        _clear_failed_provider_error_lifecycle(loaded)
    reloaded = models.Session.load(session.session_id)
    assert reloaded.active_stream_id is None
    assert reloaded.pending_user_message is None
    assert reloaded.messages[0]["content"] == "draft"


def test_failed_gateway_cleanup_persists_recovered_prompt_without_index(tmp_path, monkeypatch):
    from api import models
    from api.gateway_chat import _clear_gateway_pending_state

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    session = models.Session(
        session_id="issue6610-gateway-cleanup",
        workspace=tmp_path,
        messages=[],
        active_stream_id="run-gateway-cleanup",
        pending_user_message="draft",
        pending_started_at=100.0,
        pending_user_source="webui",
        source_tag="webui",
    )
    session.save(touch_updated_at=False, skip_index=True)
    loaded = models.Session.load(session.session_id)
    with patch.object(models, "_write_session_index", side_effect=OSError("index unavailable")):
        _clear_gateway_pending_state(loaded, "run-gateway-cleanup")
    reloaded = models.Session.load(session.session_id)
    assert reloaded.active_stream_id is None
    assert reloaded.pending_user_message is None
    assert reloaded.messages[0]["content"] == "draft"


def test_projection_cache_returns_detached_rows(tmp_path, monkeypatch):
    from api import models
    from api.session_ops import _provider_error_owner_entries

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    session = models.Session(
        session_id="issue6610-projection-cache",
        workspace=tmp_path,
        messages=[_message()],
        source_tag="webui",
    )
    session.save(touch_updated_at=False, skip_index=True)
    first, _ = _provider_error_owner_entries(session, projection_cache=True)
    first[0]["row"]["content"] = "mutated"
    second, _ = _provider_error_owner_entries(session, projection_cache=True)
    assert second[0]["row"]["content"] == "provider failed"


def test_projection_ignores_unsaved_in_memory_provider_error(tmp_path, monkeypatch):
    from api import models
    from api.session_ops import project_provider_error_dismissal_capabilities

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    session = models.Session(
        session_id="issue6610-unsaved-projection",
        workspace=tmp_path,
        messages=[],
        source_tag="webui",
    )
    session.save(touch_updated_at=False, skip_index=True)
    loaded = models.Session.load(session.session_id)
    loaded.messages.append(_message())
    projected = project_provider_error_dismissal_capabilities(
        loaded,
        {"messages": copy.deepcopy(loaded.messages)},
    )
    assert "_provider_error_dismiss_ref" not in projected["messages"][0]


def test_route_accepts_only_capability_reference():
    import api.routes as routes
    session = _FakeSession([_message()])
    plan = _plan(session)
    captured = {}
    def response(_handler, data, status=200, extra_headers=None):
        captured.update(data=data, status=status)
        return True
    handler = SimpleNamespace(headers={})
    body = {"session_id": session.session_id, "dismiss_ref": plan.dismiss_ref}
    with patch.object(routes, "read_body", return_value=body), patch.object(routes, "_check_csrf", return_value=True), \
         patch.object(routes, "_guard_request_session_visibility", return_value=True), patch.object(routes, "_get_or_materialize_session", return_value=session), \
         patch.object(routes, "_session_visible_to_active_profile", return_value=True), patch.object(routes, "j", side_effect=response), \
         patch.object(routes, "bad", side_effect=response), patch("api.config._evict_session_agent"):
        assert routes.handle_post(handler, urlparse("/api/session/message/dismiss-error")) is True
    assert captured["status"] == 200 and captured["data"] == {"ok": True}


def test_public_projection_contains_only_reference_and_no_private_owner_fields():
    from api.session_ops import project_provider_error_dismissal_capabilities
    session = _FakeSession([_message()])
    plan = _plan(session)
    projected = project_provider_error_dismissal_capabilities(session, {"messages": copy.deepcopy(session.messages)})
    row = projected["messages"][0]
    assert row["_provider_error_dismiss_ref"] == plan.dismiss_ref
    assert not {"owner_session_id", "owner_index", "row_digest"} & set(row)


def test_redacted_http_projection_keeps_the_capability_from_the_raw_owner_row():
    from api.helpers import redact_session_data
    from api.session_ops import project_provider_error_dismissal_capabilities

    session = _FakeSession([_message(provider_details="Bearer secret-token")])
    raw = {"messages": copy.deepcopy(session.messages)}
    projected = project_provider_error_dismissal_capabilities(session, raw)
    public = redact_session_data(projected)
    assert public["messages"][0]["_provider_error_dismiss_ref"]
