"""Behavioral admission coverage for hidden and parked WebUI turns."""

import json
import threading
from unittest.mock import MagicMock

import pytest

from api import config, models, routes, session_lineage, streaming
from api.models import Session
from api.turn_journal import read_turn_journal


def _session(tmp_path, monkeypatch, session_id):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    for module in (config, models, routes, streaming):
        monkeypatch.setattr(module, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    session = Session(
        session_id=session_id,
        workspace=str(tmp_path),
        model="test-model",
        profile="default",
        messages=[],
        context_messages=[],
    )
    session.save()
    return session, session_dir


def test_prepared_chat_turn_is_the_single_submitted_writer_with_readback(tmp_path, monkeypatch):
    session, session_dir = _session(tmp_path, monkeypatch, "prepared-hidden")
    reserve = getattr(routes, "_reserve_turn_admission", None)
    prepared_type = getattr(routes, "PreparedChatTurn", None)
    assert callable(reserve) and prepared_type is not None
    admission = reserve(session, "prepared-stream")

    prepared = routes._prepare_chat_start_session_for_stream(
        session,
        msg="hidden prompt",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        stream_id="prepared-stream",
        started_at=10.0,
        source="background",
        admission=admission,
    )

    assert isinstance(prepared, prepared_type)
    assert prepared.admission is admission
    assert prepared.turn_id
    sidecar = json.loads((session_dir / "prepared-hidden.json").read_text(encoding="utf-8"))
    assert sidecar["active_stream_id"] == "prepared-stream"
    assert sidecar["pending_user_message"] == "hidden prompt"
    events = read_turn_journal("prepared-hidden", session_dir=session_dir)["events"]
    submitted = [event for event in events if event.get("event") == "submitted"]
    assert len(submitted) == 1
    assert submitted[0]["turn_id"] == prepared.turn_id
    session_lineage.release_turn_admission(admission)


def test_hidden_root_overlaps_parent_but_same_hidden_root_is_excluded(tmp_path, monkeypatch):
    parent, _session_dir = _session(tmp_path, monkeypatch, "parent-root")
    hidden, _session_dir = _session(tmp_path, monkeypatch, "hidden-root")
    reserve = getattr(routes, "_reserve_turn_admission", None)
    assert callable(reserve)

    parent_admission = reserve(parent, "parent-stream")
    hidden_admission = reserve(hidden, "hidden-stream")
    assert parent_admission.root_session_id != hidden_admission.root_session_id
    with pytest.raises(session_lineage.LineageTurnBusyError):
        reserve(hidden, "hidden-stream-2")

    session_lineage.release_turn_admission(hidden_admission)
    session_lineage.release_turn_admission(parent_admission)


def test_btw_verifies_parent_before_creating_hidden_state(monkeypatch):
    parent = MagicMock()
    parent.session_id = "unsafe-parent"
    parent.profile = "default"
    created = []
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "get_session", lambda _sid: parent)
    monkeypatch.setattr(
        session_lineage,
        "resolve_session_lineage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unsafe lineage")),
    )
    monkeypatch.setattr(models, "new_session", lambda **_kwargs: created.append(True))
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: {"error": message, "_status": status},
    )

    response = routes._handle_btw(
        object(),
        {"session_id": parent.session_id, "question": "side question"},
    )
    assert isinstance(response, dict)
    assert response["_status"] == 409
    assert created == []


@pytest.mark.parametrize(
    "field",
    [
        "owner_token",
        "reservation_id",
        "root_session_id",
        "delivery_session_id",
        "admission_signal",
        "admission",
    ],
)
def test_browser_chat_start_rejects_internal_admission_fields(monkeypatch, field):
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: {"error": message, "_status": status},
    )
    response = routes._handle_chat_start(
        object(),
        {"session_id": "browser-session", field: "forged"},
    )
    assert isinstance(response, dict)
    assert response["_status"] == 400
    assert field in response["error"]


def test_hidden_tracker_failure_releases_every_artifact(tmp_path, monkeypatch):
    hidden, session_dir = _session(tmp_path, monkeypatch, "tracker-hidden")
    stream_id = "tracker-hidden-stream"

    def fail_tracker():
        raise RuntimeError("tracker failed")

    with pytest.raises(RuntimeError, match="tracker failed"):
        routes._start_hidden_admitted_turn(
            hidden,
            message="background prompt",
            stream_id=stream_id,
            model="test-model",
            workspace=str(tmp_path),
            model_provider=None,
            source="background",
            ephemeral=False,
            before_thread_start=fail_tracker,
        )

    assert not (session_dir / "tracker-hidden.json").exists()
    with config.ACTIVE_RUNS_LOCK:
        assert stream_id not in config.ACTIVE_RUNS
    with config.STREAM_SESSION_OWNERS_LOCK:
        assert stream_id not in config.STREAM_SESSION_OWNERS
    with config.STREAMS_LOCK:
        assert stream_id not in config.STREAMS


def test_local_admitted_wrapper_parks_aborts_and_releases_once(monkeypatch):
    wrapper = getattr(streaming, "_run_admitted_agent_streaming", None)
    assert callable(wrapper), "local production turns require the admitted wrapper"
    permit = MagicMock()
    permit.acquired = True
    admission = session_lineage.TurnAdmission.create_for_test(
        stream_id="local-admitted",
        root_session_id="local-root",
        delivery_session_id="local-root",
        permit=permit,
    )
    config.register_active_run(
        admission.stream_id,
        admission=admission,
        lineage_id=admission.root_session_id,
        delivery_session_id=admission.delivery_session_id,
        phase="reserved",
        reservation_create=True,
    )
    calls = []
    monkeypatch.setattr(
        streaming,
        "_run_agent_streaming_core",
        lambda *args, **kwargs: calls.append("core"),
    )
    monkeypatch.setattr(
        session_lineage,
        "release_turn_admission",
        lambda owned: calls.append(("release", owned.owner_token)),
    )

    worker = threading.Thread(
        target=wrapper,
        args=("local-root", "hello", "test", "/tmp", "local-admitted"),
        kwargs={"admission": admission},
    )
    worker.start()
    assert admission.admitted.wait(timeout=1)
    assert calls == []
    admission.abort.set()
    admission.gate.set()
    worker.join(timeout=2)
    assert calls == [("release", admission.owner_token)]
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.pop(admission.stream_id, None)


@pytest.mark.parametrize("core_error", [False, True], ids=["success", "error"])
def test_local_admitted_wrapper_runs_observer_and_cleanup_on_every_exit(
    monkeypatch,
    core_error,
):
    permit = MagicMock()
    permit.acquired = True
    admission = session_lineage.TurnAdmission.create_for_test(
        stream_id=f"local-wrapper-{core_error}",
        root_session_id=f"local-root-{core_error}",
        delivery_session_id=f"local-root-{core_error}",
        permit=permit,
    )
    admission.gate.set()
    calls = []

    def run_core(*_args, **_kwargs):
        calls.append("core")
        if core_error:
            raise RuntimeError("core failed")
        return "done"

    monkeypatch.setattr(streaming, "register_active_run", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(streaming, "_run_agent_streaming_core", run_core)
    monkeypatch.setattr(
        session_lineage,
        "release_turn_admission",
        lambda owned: calls.append(("release", owned.owner_token)),
    )

    invoke = lambda: streaming._run_admitted_agent_streaming(
        admission.delivery_session_id,
        "hello",
        "test",
        "/tmp",
        admission.stream_id,
        admission=admission,
        completion_observer=lambda: calls.append("observer"),
    )
    if core_error:
        with pytest.raises(RuntimeError, match="core failed"):
            invoke()
        assert admission.abort.is_set()
    else:
        assert invoke() == "done"
    assert calls == ["core", "observer", ("release", admission.owner_token)]
