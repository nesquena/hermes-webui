"""Regression coverage for durable compression-transition ownership on abnormal exits."""

import json
import queue
import sys
import types
from pathlib import Path

import pytest

from api import background_process, config, models, routes, streaming
from api import session_lineage
from api.models import Session
from api.session_lineage import resolve_session_lineage


def _run_rotated_turn(tmp_path, monkeypatch, mode: str):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    for module in (config, models, routes, streaming):
        monkeypatch.setattr(module, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    streaming.SESSIONS.clear()
    streaming.STREAMS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.SESSION_AGENT_LOCKS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    with config.SESSION_WRITEBACK_OWNERS_LOCK:
        config.SESSION_WRITEBACK_OWNERS.clear()
    with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
        config.DEFERRED_PROCESS_WAKEUPS.clear()

    old_sid = f"old_{mode}"
    new_sid = f"new_{mode}"
    stream_id = f"stream-{mode}"
    session = Session(
        session_id=old_sid,
        title="Compression transition lifecycle",
        workspace=str(tmp_path),
        model="test-model",
        profile="default",
        messages=[],
        context_messages=[],
        session_source="fork" if mode == "fork_boundary" else None,
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "Continue after compression."
    session.pending_started_at = 1.0
    session.save()
    models.SESSIONS[old_sid] = session
    streaming.SESSIONS[old_sid] = session
    streaming.STREAMS[stream_id] = queue.Queue()
    config.register_session_writeback_owner(old_sid, stream_id)

    class FakeAgent:
        def __init__(self, session_id=None, stream_delta_callback=None, **_kwargs):
            self.session_id = session_id
            self.stream_delta_callback = stream_delta_callback
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            self.session_id = new_sid
            if mode == "cancel":
                self._last_error = "cancelled by user"
                return {
                    "failed": True,
                    "status": "cancelled",
                    "error": "cancelled by user",
                    "messages": [{"role": "user", "content": kwargs["persist_user_message"]}],
                }
            return {
                "messages": [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "Prepared continuation."},
                ]
            }

        def interrupt(self, _message):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()
    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *_args, **_kwargs: ("test-model", "test-provider", None),
    )
    monkeypatch.setattr(config, "get_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)

    if mode == "cancel":
        def cancel_after_rotation(*_args, **_kwargs):
            streaming.CANCEL_FLAGS[stream_id].set()
            return False

        monkeypatch.setattr(
            streaming,
            "_assistant_reply_added_after_current_turn",
            cancel_after_rotation,
        )
    elif mode == "provider_exception":
        monkeypatch.setattr(
            streaming,
            "_assistant_reply_added_after_current_turn",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider post-rotation failure")),
        )
    elif mode == "outer_finally":
        original_alias = streaming._alias_session_agent_lock

        def fail_after_parent_is_bound(*args, **kwargs):
            original_alias(*args, **kwargs)
            raise RuntimeError("abnormal post-rotation teardown")

        monkeypatch.setattr(streaming, "_alias_session_agent_lock", fail_after_parent_is_bound)
    elif mode == "transition_commit_failure":
        original_record_transition = session_lineage.record_lineage_transition

        def fail_committed_transition(*args, **kwargs):
            if kwargs.get("state") == "committed":
                raise OSError("forced transition commit failure")
            return original_record_transition(*args, **kwargs)

        monkeypatch.setattr(
            session_lineage,
            "record_lineage_transition",
            fail_committed_transition,
        )
    elif mode == "continuation_save_failure":
        original_save = Session.save

        def fail_continuation_save(self, *args, **kwargs):
            if self.session_id == new_sid:
                raise OSError("forced continuation save failure")
            return original_save(self, *args, **kwargs)

        monkeypatch.setattr(Session, "save", fail_continuation_save)
    elif mode == "continuation_readback_failure":
        original_read_text = Path.read_text

        def fail_continuation_readback(self, *args, **kwargs):
            if self == session_dir / f"{new_sid}.json":
                raise OSError("forced continuation read-back failure")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_continuation_readback)

    streaming._run_agent_streaming_core(
        session_id=old_sid,
        msg_text=session.pending_user_message,
        model="test-model",
        workspace=str(tmp_path),
        stream_id=stream_id,
    )
    return session_dir, old_sid, new_sid, session


def _assert_committed_tip(session_dir, old_sid, new_sid):
    from api import session_lineage

    transition_files = list((session_dir / "_session_lineage_transitions").glob("*.json"))
    assert len(transition_files) == 1
    transition = json.loads(transition_files[0].read_text(encoding="utf-8"))
    assert transition["state"] == "committed"
    assert resolve_session_lineage(old_sid, session_dir=session_dir).delivery_session_id == new_sid
    assert resolve_session_lineage(new_sid, session_dir=session_dir).root_session_id == old_sid
    assert config.session_writeback_owner(old_sid) is None
    assert config.session_writeback_owner(new_sid) is None
    assert session_lineage.acquire_lineage_turn_permit(
        old_sid, lock_dir=session_dir / "_session_lineage_permits"
    ).release() is None


def test_compressed_cancel_releases_pending_transition_for_next_admission(tmp_path, monkeypatch):
    session_dir, old_sid, new_sid, _session = _run_rotated_turn(tmp_path, monkeypatch, "cancel")

    _assert_committed_tip(session_dir, old_sid, new_sid)


def test_compressed_provider_exception_keeps_deferred_completion_deliverable(tmp_path, monkeypatch):
    session_dir, old_sid, new_sid, _session = _run_rotated_turn(
        tmp_path, monkeypatch, "provider_exception"
    )

    _assert_committed_tip(session_dir, old_sid, new_sid)
    assert background_process.record_deferred_wakeup(new_sid, "proc-1", "Process finished") is True
    assert background_process.claim_deferred_wakeups(old_sid) == [
        {
            "process_id": "proc-1",
            "wakeup_prompt": "Process finished",
            "origin_session_id": new_sid,
        }
    ]


def test_compressed_outer_finally_keeps_continuation_recovery_on_tip(tmp_path, monkeypatch):
    session_dir, old_sid, new_sid, _session = _run_rotated_turn(
        tmp_path, monkeypatch, "outer_finally"
    )

    _assert_committed_tip(session_dir, old_sid, new_sid)

    class Snapshot:
        session_id = old_sid
        profile = "default"
        pre_compression_snapshot = True

    assert routes._pre_compression_continuation_session_id(Snapshot()) == new_sid


def test_first_fork_compression_transition_overrides_inherited_source_marker(
    tmp_path,
    monkeypatch,
):
    """C8: the durable edge wins over source metadata inherited by both segments."""
    session_dir, old_sid, new_sid, _session = _run_rotated_turn(
        tmp_path,
        monkeypatch,
        "fork_boundary",
    )

    for sid in (old_sid, new_sid):
        sidecar = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert sidecar["session_source"] == "fork"
        resolved = resolve_session_lineage(sid, session_dir=session_dir)
        assert resolved.root_session_id == old_sid
        assert resolved.delivery_session_id == new_sid


def test_commit_failure_becomes_recoverable_and_reconciles_deterministically(
    tmp_path,
    monkeypatch,
):
    """C4: a failed commit cannot strand an ownerless permanent pending fence."""
    original_record_transition = session_lineage.record_lineage_transition
    session_dir, old_sid, new_sid, _session = _run_rotated_turn(
        tmp_path,
        monkeypatch,
        "transition_commit_failure",
    )

    [transition_path] = list(
        (session_dir / "_session_lineage_transitions").glob("*.json")
    )
    transition = json.loads(transition_path.read_text(encoding="utf-8"))
    assert transition["state"] == "recoverable"
    assert transition["previous_tip_session_id"] == old_sid
    assert transition["delivery_session_id"] == new_sid

    with pytest.raises(session_lineage.LineageResolutionError, match="recoverable"):
        resolve_session_lineage(old_sid, session_dir=session_dir)

    monkeypatch.setattr(
        session_lineage,
        "record_lineage_transition",
        original_record_transition,
    )
    resolved = resolve_session_lineage(old_sid, session_dir=session_dir)
    assert resolved.root_session_id == old_sid
    assert resolved.delivery_session_id == new_sid
    committed = json.loads(transition_path.read_text(encoding="utf-8"))
    assert committed["state"] == "committed"


def test_continuation_save_failure_never_commits_an_absent_tip(tmp_path, monkeypatch):
    """C4: failed continuation durability has an owner but cannot become committed."""
    session_dir, old_sid, new_sid, _session = _run_rotated_turn(
        tmp_path,
        monkeypatch,
        "continuation_save_failure",
    )

    [transition_path] = list(
        (session_dir / "_session_lineage_transitions").glob("*.json")
    )
    transition = json.loads(transition_path.read_text(encoding="utf-8"))
    assert transition["state"] == "recoverable"
    assert not (session_dir / f"{new_sid}.json").exists()
    with pytest.raises(session_lineage.LineageResolutionError, match="missing"):
        resolve_session_lineage(old_sid, session_dir=session_dir)


def test_continuation_readback_failure_reconciles_after_storage_recovers(
    tmp_path,
    monkeypatch,
):
    """C4: read-back uncertainty fails closed and retries from durable evidence."""
    original_read_text = Path.read_text
    session_dir, old_sid, new_sid, _session = _run_rotated_turn(
        tmp_path,
        monkeypatch,
        "continuation_readback_failure",
    )
    monkeypatch.setattr(Path, "read_text", original_read_text)

    [transition_path] = list(
        (session_dir / "_session_lineage_transitions").glob("*.json")
    )
    transition = json.loads(transition_path.read_text(encoding="utf-8"))
    assert transition["state"] == "recoverable"
    assert (session_dir / f"{new_sid}.json").exists()
    resolved = resolve_session_lineage(old_sid, session_dir=session_dir)
    assert resolved.delivery_session_id == new_sid
    assert json.loads(transition_path.read_text(encoding="utf-8"))["state"] == "committed"


def test_normal_transition_commits_once_after_durable_continuation(tmp_path, monkeypatch):
    """C4: the normal owner publishes pending then exactly one committed state."""
    original_record_transition = session_lineage.record_lineage_transition
    states = []

    def record_transition(*args, **kwargs):
        state = kwargs.get("state")
        if state == "committed":
            delivery_id = kwargs["delivery_session_id"]
            sidecar = tmp_path / "sessions" / f"{delivery_id}.json"
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            assert payload["session_id"] == delivery_id
            assert payload["parent_session_id"] == kwargs["previous_tip_session_id"]
        states.append(state)
        return original_record_transition(*args, **kwargs)

    monkeypatch.setattr(session_lineage, "record_lineage_transition", record_transition)
    _run_rotated_turn(tmp_path, monkeypatch, "normal")

    assert states == ["pending", "committed"]
