"""#7293 — managed restart must drain or refuse active WebUI-owned runs.

Owner-approved policy (Option A):

* A WebUI-initiated update/restart REFUSES by default while WebUI-owned runs
  are active, reports the active count, and offers an explicit force override.
  Admission is fenced for the whole update so nothing new can start between the
  safety check and the re-exec; a forced restart settles the remaining runs as
  durably interrupted before the process image is replaced.
* An external SIGTERM/SIGINT gets ONE idempotent, bounded drain: admission
  closes, active runs may finish inside the budget, anything still running at
  the deadline is settled as interrupted durably, then the server stops.
* A repeated signal escalates to an immediate settled stop; it never starts a
  second drain.
* Gateway runs whose resumable run identity is durably saved are not
  WebUI-owned and are never waited on or settled; any other run counts.

These are the in-process contract tests. The real serving-process tests live in
``test_issue7293_restart_drain_real_process.py``.
"""

from __future__ import annotations

import functools
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

from api import config
from api import gateway_chat
from api import run_drain


REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_registries():
    run_drain._reset_for_tests()
    with gateway_chat._DURABLE_RUN_STREAMS_LOCK:
        old_durable = set(gateway_chat._DURABLE_RUN_STREAMS)
        gateway_chat._DURABLE_RUN_STREAMS.clear()
    with config.STREAMS_LOCK:
        old_streams = dict(config.STREAMS)
        old_claims = dict(config.PRE_ADMISSION_CLAIMS)
        config.STREAMS.clear()
        config.PRE_ADMISSION_CLAIMS.clear()
    with config.ACTIVE_RUNS_LOCK:
        old_runs = dict(config.ACTIVE_RUNS)
        config.ACTIVE_RUNS.clear()
    try:
        yield
    finally:
        run_drain._reset_for_tests()
        with config.STREAMS_LOCK:
            config.STREAMS.clear()
            config.STREAMS.update(old_streams)
            config.PRE_ADMISSION_CLAIMS.clear()
            config.PRE_ADMISSION_CLAIMS.update(old_claims)
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
            config.ACTIVE_RUNS.update(old_runs)
        with gateway_chat._DURABLE_RUN_STREAMS_LOCK:
            gateway_chat._DURABLE_RUN_STREAMS.clear()
            gateway_chat._DURABLE_RUN_STREAMS.update(old_durable)


def _add_run(stream_id, *, backend="legacy", session_id="sess", phase="running", durable_gateway=False):
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS[stream_id] = {
            "stream_id": stream_id,
            "session_id": session_id,
            "backend": backend,
            "phase": phase,
            "started_at": time.time(),
        }
    if durable_gateway:
        # As _record_gateway_run does once the run identity is saved.
        gateway_chat._mark_gateway_run_durable(stream_id)


def _drop_run(stream_id):
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.pop(stream_id, None)


# ── Admission fence ──────────────────────────────────────────────────────────


def test_admission_is_open_by_default():
    assert run_drain.run_admission_closed_response() is None
    with config.STREAMS_LOCK:
        run_drain.assert_run_admission_open_locked()  # must not raise
    assert run_drain.admission_status()["open"] is True


def test_update_hold_closes_admission_until_released():
    token = run_drain.hold_run_admission("update:webui")
    refusal = run_drain.run_admission_closed_response()
    assert refusal is not None
    assert refusal["_status"] == 503
    assert refusal["code"] == "server_draining"
    assert refusal["server_draining"] is True
    with config.STREAMS_LOCK:
        with pytest.raises(run_drain.RunAdmissionClosed) as exc_info:
            run_drain.assert_run_admission_open_locked()
    assert exc_info.value.status == 503
    assert exc_info.value.payload["code"] == "server_draining"

    run_drain.release_run_admission_hold(token)
    assert run_drain.run_admission_closed_response() is None


def test_restart_pending_keeps_admission_closed_after_hold_release():
    token = run_drain.hold_run_admission("update:agent")
    run_drain.mark_restart_pending()
    run_drain.release_run_admission_hold(token)
    assert run_drain.run_admission_closed_response() is not None
    assert run_drain.admission_status()["phase"] == "restart_pending"


def test_abandoned_restart_reopens_admission():
    token = run_drain.hold_run_admission("update:webui")
    epoch = run_drain.mark_restart_pending()
    run_drain.release_run_admission_hold(token)
    run_drain.abandon_restart_pending(epoch)
    assert run_drain.run_admission_closed_response() is None


def test_force_flag_follows_the_hold_into_restart_pending():
    token = run_drain.hold_run_admission("update:webui", force=True)
    run_drain.mark_restart_pending()
    run_drain.release_run_admission_hold(token)
    assert run_drain.restart_force_requested() is True


def test_force_flag_does_not_survive_a_failed_update():
    token = run_drain.hold_run_admission("update:webui", force=True)
    run_drain.release_run_admission_hold(token)
    # A later, non-forced restart must not inherit the earlier force decision.
    token2 = run_drain.hold_run_admission("update:webui")
    run_drain.mark_restart_pending()
    run_drain.release_run_admission_hold(token2)
    assert run_drain.restart_force_requested() is False


# ── WebUI-owned liveness (gateway exclusion) ────────────────────────────────


def test_webui_owned_runs_exclude_durably_resumable_gateway_runs():
    _add_run("legacy-1", backend="legacy")
    _add_run("gw-1", backend="gateway", durable_gateway=True)
    _add_run("cancelling-1", backend="legacy", phase="cancelling")
    ids = sorted(r["stream_id"] for r in run_drain.webui_owned_active_runs())
    assert ids == ["cancelling-1", "legacy-1"]


def test_gateway_run_without_a_durable_run_identity_is_webui_owned():
    """Codex r1 #2: ``backend == "gateway"`` alone is not resumability.

    The legacy /v1/chat/completions relay never saves a ``gateway_run`` record
    (reattach rejects it), and a Runs API launch has none until its first save.
    A restart would abandon either, so both block updates and are drained.
    """
    _add_run("gw-relay", backend="gateway")
    _add_run("gw-durable", backend="gateway", durable_gateway=True)
    runs = run_drain.webui_owned_active_runs()
    assert [r["stream_id"] for r in runs] == ["gw-relay"]
    assert runs[0]["backend"] == "gateway"


def test_gateway_launch_awaiting_durable_handoff_is_webui_owned(monkeypatch):
    """Codex r1 #2: a Gateway launch (stream registered, lifecycle marked, no
    ACTIVE_RUNS row and no saved run identity yet) is counted, whatever order
    its ownership markers were published in."""
    monkeypatch.setitem(gateway_chat._STREAM_RUN_LIFECYCLE, "gw-launch", {"phase": "pending"})
    with config.STREAMS_LOCK:
        config.STREAMS["gw-launch"] = object()
        config.STREAMS["plain-launch"] = object()
    ids = sorted(r["stream_id"] for r in run_drain.webui_owned_active_runs())
    assert ids == ["gw-launch", "plain-launch"]


def test_update_blocker_counts_a_legacy_gateway_relay():
    from api import updates

    _add_run("gw-relay", backend="gateway")
    snap = updates._restart_blocker_snapshot()
    assert snap["restart_blocked"] is True and snap["blocking_run_ids"] == ["gw-relay"]
    gateway_chat._mark_gateway_run_durable("gw-relay")
    assert updates._restart_blocker_snapshot()["restart_blocked"] is False


def test_record_gateway_run_marks_durable_only_after_a_successful_save(tmp_path, monkeypatch):
    from api import models
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    s = Session(session_id="s7293_gw", title="gw", messages=[])
    s.active_stream_id = "gw-st"
    s.save()
    models.SESSIONS["s7293_gw"] = s
    try:
        # Not the current stream: nothing is saved, so nothing is resumable, and
        # the initial handoff refuses so the launch never POSTs (release gate f2).
        with pytest.raises(gateway_chat.GatewayHandoffCancelled):
            gateway_chat._record_gateway_run("s7293_gw", "gw-other", None, request={"input": "x"})
        assert gateway_chat.gateway_run_durably_resumable("gw-other") is False
        assert Session.load("s7293_gw").gateway_run is None
        # Durability is published only while the stream is still registered.
        with config.STREAMS_LOCK:
            config.STREAMS["gw-st"] = object()

        # The save fails: the request record is not durable (the launch fails).
        def failing_save(*_a, **_kw):
            raise OSError("disk full")

        s.save = failing_save
        try:
            with pytest.raises(OSError):
                gateway_chat._record_gateway_run("s7293_gw", "gw-st", None, request={"input": "x"})
        finally:
            del s.save  # drop the instance override; Session.save serializes __dict__
        assert gateway_chat.gateway_run_durably_resumable("gw-st") is False

        gateway_chat._record_gateway_run("s7293_gw", "gw-st", None, request={"input": "x"})
        assert gateway_chat.gateway_run_durably_resumable("gw-st") is True
        assert (Session.load("s7293_gw").gateway_run or {}).get("stream_id") == "gw-st"
        # Teardown releases it.
        gateway_chat.release_gateway_stream_state("gw-st")
        assert gateway_chat.gateway_run_durably_resumable("gw-st") is False
    finally:
        models.SESSIONS.pop("s7293_gw", None)


def test_launching_streams_count_as_webui_owned_work():
    with config.STREAMS_LOCK:
        config.STREAMS["launching-1"] = object()
        config.PRE_ADMISSION_CLAIMS["launching-1"] = "tok"
    runs = run_drain.webui_owned_active_runs()
    assert [r["stream_id"] for r in runs] == ["launching-1"]
    assert runs[0]["launching"] is True


# ── Settlement ───────────────────────────────────────────────────────────────


def test_settle_cancels_only_webui_owned_runs_with_reason(monkeypatch):
    from api import streaming

    calls = []

    def fake_cancel(stream_id, reason=None, **kw):
        calls.append((stream_id, reason, kw.get("initiator")))
        _drop_run(stream_id)
        return True

    monkeypatch.setattr(streaming, "cancel_stream", fake_cancel)
    _add_run("legacy-a")
    _add_run("legacy-b")
    _add_run("gw-a", backend="gateway", durable_gateway=True)

    result = run_drain.settle_webui_owned_runs("server stopping", timeout=2.0)

    assert sorted(c[0] for c in calls) == ["legacy-a", "legacy-b"]
    assert all(c[1] == "server stopping" for c in calls)
    # Server-initiated: the durable marker must not claim the user cancelled.
    assert all(c[2] == streaming.CANCEL_INITIATOR_SERVER for c in calls)
    assert sorted(result["settled"]) == ["legacy-a", "legacy-b"]
    assert result["timed_out"] is False
    with config.ACTIVE_RUNS_LOCK:
        assert "gw-a" in config.ACTIVE_RUNS


def test_settle_is_bounded_when_cancel_hangs(monkeypatch):
    from api import streaming

    release = threading.Event()

    def hanging_cancel(stream_id, reason=None, **_kw):
        release.wait(10)
        return True

    monkeypatch.setattr(streaming, "cancel_stream", hanging_cancel)
    _add_run("legacy-stuck")
    started = time.monotonic()
    result = run_drain.settle_webui_owned_runs("server stopping", timeout=0.5)
    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 2.0
    assert result["timed_out"] is True


def test_cancel_stream_records_the_shutdown_reason_in_the_durable_marker():
    from api import streaming

    content = streaming._cancelled_turn_content("The WebUI server stopped before this response finished")
    normalized = content.lower()
    assert "the webui server stopped before this response finished" in normalized
    # The marker must still be recognised as a cancel marker by every
    # dedupe / recovery path.
    assert any(p in normalized for p in streaming._CANCEL_MARKER_PATTERNS)
    import inspect

    assert "reason" in inspect.signature(streaming.cancel_stream).parameters


# ── Bounded shutdown drain (external SIGTERM) ───────────────────────────────


class _StopRecorder:
    def __init__(self):
        self.stopped = threading.Event()
        self.at = None

    def __call__(self):
        self.at = time.monotonic()
        self.stopped.set()


def test_zero_active_runs_stops_immediately():
    stop = _StopRecorder()
    drain = run_drain.ShutdownDrain(stop, budget_seconds=30, settle_seconds=1, poll_seconds=0.02)
    t0 = time.monotonic()
    drain.handle_signal(15, None)
    assert stop.stopped.wait(2.0)
    assert stop.at - t0 < 1.0
    assert drain.outcome["result"] == "drained"
    assert run_drain.run_admission_closed_response() is not None


def test_run_finishing_inside_budget_is_waited_for_not_settled(monkeypatch):
    from api import streaming

    settled = []
    monkeypatch.setattr(streaming, "cancel_stream", lambda sid, reason=None, **_kw: settled.append(sid))
    _add_run("legacy-finishing")
    stop = _StopRecorder()
    drain = run_drain.ShutdownDrain(stop, budget_seconds=5, settle_seconds=1, poll_seconds=0.02)
    drain.handle_signal(15, None)
    time.sleep(0.3)
    assert not stop.stopped.is_set(), "drain must wait for the active run"
    _drop_run("legacy-finishing")
    assert stop.stopped.wait(2.0)
    assert settled == []
    assert drain.outcome["result"] == "drained"


def test_run_exceeding_budget_is_settled_then_server_stops(monkeypatch):
    from api import streaming

    settled = []

    def fake_cancel(stream_id, reason=None, **_kw):
        settled.append((stream_id, reason))
        _drop_run(stream_id)
        return True

    monkeypatch.setattr(streaming, "cancel_stream", fake_cancel)
    _add_run("legacy-long")
    _add_run("gw-long", backend="gateway", durable_gateway=True)
    stop = _StopRecorder()
    drain = run_drain.ShutdownDrain(stop, budget_seconds=0.4, settle_seconds=1, poll_seconds=0.02)
    t0 = time.monotonic()
    drain.handle_signal(15, None)
    assert stop.stopped.wait(3.0)
    assert stop.at - t0 >= 0.35
    assert [s[0] for s in settled] == ["legacy-long"]
    assert "shut" in (settled[0][1] or "").lower() or "stop" in (settled[0][1] or "").lower()
    assert drain.outcome["result"] == "deadline"
    assert drain.outcome["settled"] == ["legacy-long"]


def test_new_admission_is_refused_while_draining():
    _add_run("legacy-long")
    stop = _StopRecorder()
    drain = run_drain.ShutdownDrain(stop, budget_seconds=5, settle_seconds=1, poll_seconds=0.02)
    drain.handle_signal(15, None)
    time.sleep(0.1)
    refusal = run_drain.run_admission_closed_response()
    assert refusal is not None and refusal["_status"] == 503
    assert run_drain.admission_status()["phase"] == "draining"
    drain.escalate()
    stop.stopped.wait(3.0)


def test_second_signal_escalates_without_starting_a_second_drain(monkeypatch):
    from api import streaming

    settled = []

    def fake_cancel(stream_id, reason=None, **_kw):
        settled.append(stream_id)
        _drop_run(stream_id)
        return True

    monkeypatch.setattr(streaming, "cancel_stream", fake_cancel)
    _add_run("legacy-long")
    stop = _StopRecorder()
    drain = run_drain.ShutdownDrain(stop, budget_seconds=60, settle_seconds=1, poll_seconds=0.02)
    drain.handle_signal(15, None)
    time.sleep(0.2)
    first_thread = drain._thread
    t0 = time.monotonic()
    drain.handle_signal(15, None)
    assert stop.stopped.wait(3.0)
    assert stop.at - t0 < 2.0, "a repeated signal must escalate to an immediate settled stop"
    assert drain._thread is first_thread, "a repeated signal must not start a second drain"
    assert settled == ["legacy-long"]
    assert drain.outcome["result"] == "escalated"
    # Further signals are inert.
    drain.handle_signal(15, None)
    drain.handle_signal(2, None)
    assert drain._thread is first_thread


def _join_drain_threads(timeout: float = 5.0) -> None:
    for t in threading.enumerate():
        if t.name == "webui-shutdown-drain":
            t.join(timeout)


def _signal_with_reentry_at_line(drain, at_line: int, reentrant_signum: int) -> bool:
    """Call ``drain.handle_signal`` and re-enter it at its ``at_line``-th line.

    Every traced line (the handler and everything it calls, threading
    internals included) is a point where CPython can run another pending signal
    handler. Returns whether the re-entry happened (False once ``at_line`` is
    past the last line the handler executes).
    """
    fired = []
    count = [0]

    def local(frame, event, arg):
        if event == "line" and not fired:
            count[0] += 1
            if count[0] == at_line:
                fired.append(True)
                drain.handle_signal(reentrant_signum, None)
        return local

    sys.settrace(lambda frame, event, arg: local)
    try:
        drain.handle_signal(15, None)
    finally:
        sys.settrace(None)
    return bool(fired)


def _reentry_worker(drain, at_line: int, fired: list) -> None:
    fired[0] = _signal_with_reentry_at_line(drain, at_line, 2)


def test_signal_reentering_the_first_handler_never_starts_a_second_drain(monkeypatch):
    """Codex r1 #3: the first-signal claim must be atomic.

    A signal that lands between "not yet claimed" and "claimed" used to start
    a second drain (two ``stop`` calls). Re-enter the handler at every line.
    """
    monkeypatch.setattr(run_drain, "webui_owned_active_runs", lambda: [])
    lines = 0
    for at_line in range(1, 400):
        stops = []
        drain = run_drain.ShutdownDrain(functools.partial(stops.append, 1), budget_seconds=30, settle_seconds=1, poll_seconds=0.01)
        fired = [None]
        worker = threading.Thread(target=_reentry_worker, args=(drain, at_line, fired), daemon=True)
        worker.start()
        worker.join(5)
        assert not worker.is_alive(), f"stop handler deadlocked at line {at_line}"
        if not fired[0]:
            drain.finished.wait(5)
            break
        lines += 1
        assert drain.finished.wait(5), at_line
        _join_drain_threads()
        assert len(stops) == 1, f"re-entry at line {at_line} started {len(stops)} drains"
        assert drain.signals == [15, 2] or drain.signals == [2, 15], drain.signals
    assert lines >= 3


def test_signal_reentering_an_escalating_handler_cannot_deadlock(monkeypatch):
    """Codex r1 #3: an escalating handler must take no lock a reentrant
    handler could need. ``Event.set()`` in the handler deadlocked when a third
    signal re-entered it inside the Event's condition lock."""
    active = {"on": True}
    monkeypatch.setattr(
        run_drain, "webui_owned_active_runs", lambda: [{"stream_id": "legacy-long"}] if active["on"] else []
    )

    def fake_settle(_reason, timeout=0.0):
        active["on"] = False
        return {"settled": ["legacy-long"], "pending": [], "failed": [], "timed_out": False}

    monkeypatch.setattr(run_drain, "settle_webui_owned_runs", fake_settle)
    lines = 0
    for at_line in range(1, 400):
        active["on"] = True
        stops = []
        drain = run_drain.ShutdownDrain(functools.partial(stops.append, 1), budget_seconds=30, settle_seconds=1, poll_seconds=0.01)
        drain.handle_signal(15, None)  # the first signal: drain running
        fired = [None]
        worker = threading.Thread(target=_reentry_worker, args=(drain, at_line, fired), daemon=True)
        worker.start()
        worker.join(5)
        assert not worker.is_alive(), f"escalating stop handler deadlocked at line {at_line}"
        assert drain.finished.wait(5), at_line
        _join_drain_threads()
        assert len(stops) == 1, (at_line, len(stops))
        assert drain.outcome["result"] == "escalated", (at_line, drain.outcome)
        if not fired[0]:
            break
        lines += 1
    assert lines >= 1


def test_real_reentrant_stop_signals_never_double_drain_or_deadlock(tmp_path):
    """Same two properties with REAL signal delivery: a trace hook sends the
    process a signal at every line the handler executes, so CPython re-enters
    the handler at the next bytecode boundary (tests/issue7293_signal_probe.py)."""
    env = {"PATH": os.pathsep.join([str(pathlib.Path(sys.executable).parent), "/usr/bin", "/bin"])}
    for key in ("HOME", "HERMES_HOME", "TMPDIR"):
        directory = tmp_path / key.lower()
        directory.mkdir()
        env[key] = str(directory)
    (tmp_path / "state").mkdir()
    env.update(
        HERMES_BASE_HOME=env["HERMES_HOME"],
        HERMES_WEBUI_STATE_DIR=str(tmp_path / "state"),
        HERMES_WEBUI_AGENT_DIR=str(tmp_path / "no-agent"),
        HERMES_DISABLE_LAZY_INSTALLS="1",
        PYTHONUNBUFFERED="1",
    )
    try:
        proc = subprocess.run(
            [sys.executable, str(REPO / "tests" / "issue7293_signal_probe.py"), str(REPO)],
            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired as exc:  # a deadlocked handler hangs the probe
        pytest.fail(f"stop-signal probe hung (handler deadlock):\n{(exc.stdout or '')[-2000:]}")
    rows = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    assert rows and rows[-1] == {"done": True}, (proc.returncode, proc.stdout[-2000:], proc.stderr[-2000:])
    scenarios = rows[:-1]
    assert {r["phase"] for r in scenarios} == {"first", "second"}, scenarios
    for row in scenarios:
        assert row["finished"] and row["stops"] == 1, row
        assert row["signals"] >= 2, row
        if row["phase"] == "second":
            assert row["result"] == "escalated", row


def test_default_drain_budget_is_the_owner_chosen_90_seconds(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS", raising=False)
    assert run_drain.DEFAULT_SHUTDOWN_DRAIN_SECONDS == 90.0
    assert run_drain.shutdown_drain_budget_seconds() == 90.0
    monkeypatch.setenv("HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS", "   ")
    assert run_drain.shutdown_drain_budget_seconds() == 90.0
    drain = run_drain.ShutdownDrain(lambda: None)
    assert drain.budget_seconds == 90.0


def test_drain_budget_comes_from_env(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS", "7.5")
    assert run_drain.shutdown_drain_budget_seconds() == 7.5
    monkeypatch.setenv("HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS", "nonsense")
    assert run_drain.shutdown_drain_budget_seconds() == run_drain.DEFAULT_SHUTDOWN_DRAIN_SECONDS
    monkeypatch.setenv("HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS", "-4")
    assert run_drain.shutdown_drain_budget_seconds() == 0.0
    monkeypatch.setenv("HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS", " .5 ")
    assert run_drain.shutdown_drain_budget_seconds() == 0.5
    monkeypatch.setenv("HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS", "99999")
    assert run_drain.shutdown_drain_budget_seconds() == run_drain.MAX_SHUTDOWN_DRAIN_SECONDS
    # ctl.sh parses the same plain-decimal grammar in bash; anything else uses
    # the default on BOTH sides so the stop wait never undercuts the drain.
    for spelling in ("1e3", "inf", "nan", "1 0", "0x10", "--5"):
        monkeypatch.setenv("HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS", spelling)
        assert run_drain.shutdown_drain_budget_seconds() == 90.0, spelling


# ── WebUI-initiated update/restart: refuse by default, explicit force ───────


def _git_dir(tmp_path, monkeypatch, upd):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(upd, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(upd, "_AGENT_DIR", tmp_path)


def test_apply_update_refusal_reports_count_and_offers_force(tmp_path, monkeypatch):
    import api.updates as upd

    _git_dir(tmp_path, monkeypatch, upd)
    monkeypatch.setattr(upd, "_apply_update_inner", lambda *a, **k: pytest.fail("must not update"))
    _add_run("legacy-1")
    result = upd.apply_update("webui")
    assert result["ok"] is False
    assert result["restart_blocked"] is True
    assert result["active_runs"] == 1
    assert result["force_available"] is True
    assert run_drain.run_admission_closed_response() is None, "a refusal must not leave admission fenced"


def test_forced_apply_update_proceeds_and_fences_admission_during_update(tmp_path, monkeypatch):
    import api.updates as upd

    _git_dir(tmp_path, monkeypatch, upd)
    seen = {}

    def inner(target, channel):
        seen["closed_during_update"] = run_drain.run_admission_closed_response() is not None
        return {"ok": False, "message": "pull failed"}

    monkeypatch.setattr(upd, "_apply_update_inner", inner)
    _add_run("legacy-1")
    result = upd.apply_update("webui", force_active_runs=True)
    assert result == {"ok": False, "message": "pull failed"}
    assert seen["closed_during_update"] is True
    # The update did not schedule a restart, so admission reopens.
    assert run_drain.run_admission_closed_response() is None


def test_apply_update_rechecks_after_fencing_admission(tmp_path, monkeypatch):
    """A run admitted between the first check and the fence must still refuse."""
    import api.updates as upd

    _git_dir(tmp_path, monkeypatch, upd)
    monkeypatch.setattr(upd, "_apply_update_inner", lambda *a, **k: pytest.fail("must not update"))
    real_hold = run_drain.hold_run_admission

    def hold_after_race(owner, *, force=False):
        _add_run("legacy-raced-in")  # admitted just before the fence closed
        return real_hold(owner, force=force)

    monkeypatch.setattr(run_drain, "hold_run_admission", hold_after_race)
    result = upd.apply_update("webui")
    assert result["ok"] is False
    assert result["restart_blocked"] is True
    assert run_drain.run_admission_closed_response() is None


def test_successful_update_keeps_admission_closed_until_restart(tmp_path, monkeypatch):
    import api.updates as upd

    _git_dir(tmp_path, monkeypatch, upd)
    monkeypatch.setattr(upd, "_wait_until_restart_safe", lambda *a, **k: {"restart_blocked": False})

    def inner(target, channel):
        upd._schedule_restart(delay=60)  # never fires during the test
        return {"ok": True, "restart_scheduled": True, "target": target}

    monkeypatch.setattr(upd, "_apply_update_inner", inner)
    result = upd.apply_update("webui")
    assert result["ok"] is True
    refusal = run_drain.run_admission_closed_response()
    assert refusal is not None and refusal["_status"] == 503


def test_force_and_clear_lock_paths_accept_force(tmp_path, monkeypatch):
    import inspect
    import api.updates as upd

    for fn in (upd.apply_update, upd.apply_force_update, upd.apply_clear_lock):
        assert "force_active_runs" in inspect.signature(fn).parameters, fn.__name__


def test_forced_restart_settles_webui_runs_before_execv_and_leaves_gateway_runs(monkeypatch):
    import os
    import sys
    import api.updates as upd
    from api import streaming

    events = []

    def fake_cancel(stream_id, reason=None, **_kw):
        events.append(("cancel", stream_id, reason))
        _drop_run(stream_id)
        return True

    monkeypatch.setattr(streaming, "cancel_stream", fake_cancel)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(upd, "_purge_agent_pycache", lambda *_a: None)
    monkeypatch.setattr(os, "execv", lambda exe, args: events.append(("execv", exe)))
    _add_run("legacy-1")
    _add_run("gw-1", backend="gateway", durable_gateway=True)

    token = run_drain.hold_run_admission("update:webui", force=True)
    upd._schedule_restart(delay=0.01)
    run_drain.release_run_admission_hold(token)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(e[0] == "execv" for e in events):
        time.sleep(0.02)

    kinds = [e[0] for e in events]
    assert "execv" in kinds
    assert kinds.index("cancel") < kinds.index("execv")
    assert [e[1] for e in events if e[0] == "cancel"] == ["legacy-1"]
    with config.ACTIVE_RUNS_LOCK:
        assert "gw-1" in config.ACTIVE_RUNS


def test_unforced_restart_wait_timeout_settles_instead_of_abandoning(monkeypatch):
    import os
    import sys
    import api.updates as upd
    from api import streaming

    events = []

    def fake_cancel(stream_id, reason=None, **_kw):
        events.append(("cancel", stream_id))
        _drop_run(stream_id)
        return True

    monkeypatch.setattr(streaming, "cancel_stream", fake_cancel)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(upd, "_purge_agent_pycache", lambda *_a: None)
    monkeypatch.setattr(
        upd, "_wait_until_restart_safe",
        lambda *a, **k: {"restart_blocked": True, "wait_timed_out": True, "active_runs": 1},
    )
    monkeypatch.setattr(os, "execv", lambda exe, args: events.append(("execv", exe)))
    _add_run("legacy-stuck")

    upd._schedule_restart(delay=0.01)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(e[0] == "execv" for e in events):
        time.sleep(0.02)
    kinds = [e[0] for e in events]
    assert kinds == ["cancel", "execv"]


def test_wait_until_restart_safe_does_not_wait_when_forced(monkeypatch):
    import api.updates as upd

    _add_run("legacy-1")
    token = run_drain.hold_run_admission("update:webui", force=True)
    run_drain.mark_restart_pending()
    run_drain.release_run_admission_hold(token)
    sleeps = []
    monkeypatch.setattr(upd.time, "sleep", lambda s: sleeps.append(s))
    snapshot = upd._wait_until_restart_safe(poll_seconds=0.1, max_wait_seconds=30)
    assert sleeps == []
    assert snapshot.get("forced") is True


def test_restart_that_does_not_replace_the_process_reopens_admission(monkeypatch):
    import os
    import sys
    import api.updates as upd

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(upd, "_purge_agent_pycache", lambda *_a: None)
    monkeypatch.setattr(upd, "_wait_until_restart_safe", lambda *a, **k: {"restart_blocked": False})
    done = threading.Event()
    monkeypatch.setattr(os, "execv", lambda exe, args: done.set())
    upd._schedule_restart(delay=0.01)
    assert done.wait(5)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and run_drain.run_admission_closed_response() is not None:
        time.sleep(0.02)
    assert run_drain.run_admission_closed_response() is None


def _updates_threading_with(thread_cls):
    """``api.updates``' view of ``threading`` with only ``Thread`` replaced
    (patching the real module would hit every thread the process starts)."""
    import types

    view = types.SimpleNamespace(**{k: getattr(threading, k) for k in dir(threading) if not k.startswith("__")})
    view.Thread = thread_cls
    return view


def test_restart_thread_that_fails_to_start_reopens_admission(monkeypatch):
    """Codex r2 #2: admission is closed before the restart thread exists, and
    only that thread reopened it. A thread that never starts must not leave
    every later chat start refused with 503 forever."""
    import api.updates as upd

    class _UnstartableThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(upd, "threading", _updates_threading_with(_UnstartableThread))
    with pytest.raises(RuntimeError, match="can't start new thread"):
        upd._schedule_restart(delay=60)
    assert run_drain.admission_status()["restart_pending"] is False
    assert run_drain.run_admission_closed_response() is None


def test_failed_restart_launch_does_not_reopen_a_newer_pending_restart(monkeypatch):
    """The cleanup is epoch-scoped like the thread's own: a failed launch must
    not reopen admission underneath another restart that is still pending."""
    import api.updates as upd

    newer = {}

    class _UnstartableThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            newer["epoch"] = run_drain.mark_restart_pending()
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(upd, "threading", _updates_threading_with(_UnstartableThread))
    with pytest.raises(RuntimeError):
        upd._schedule_restart(delay=60)
    assert run_drain.admission_status()["restart_pending"] is True
    assert run_drain.abandon_restart_pending(newer["epoch"]) is True


# ── Route edges: early refusal and the registration-edge race ───────────────

_RACE_SESSION_ID = "drain-race-session"
_RACE_STREAM_ID = "drain-race-stream"


class _RaceSession:
    """The attributes the launch path and its compensation read."""

    def __init__(self):
        self.session_id = _RACE_SESSION_ID
        self.active_stream_id = _RACE_STREAM_ID
        self.pending_user_message = "hello"
        # Past the registration grace window, so the launch is not refused as
        # a duplicate of itself before it reaches the registration edge.
        self.pending_started_at = time.time() - 3600
        self.messages = []
        self.title = ""
        self.worktree_path = None
        self.pending_attachments = []
        self.pending_user_source = "webui"
        self.attachments = []
        self.profile = None
        self.model = "test-model"
        self.model_provider = "test-provider"
        self.workspace = "test-workspace"
        self.saves = 0

    def save(self, *args, **kwargs):
        self.saves += 1


class _RecordingHandler:
    def __init__(self):
        import io

        self.wfile = io.BytesIO()
        self.headers = {}
        self.status = None

    def send_response(self, status, *args, **kwargs):
        self.status = status

    def send_header(self, *args, **kwargs):
        pass

    def end_headers(self, *args, **kwargs):
        pass

    def body(self):
        import json

        raw = self.wfile.getvalue().decode("utf-8", "replace")
        return json.loads(raw[raw.index("{"):]) if "{" in raw else {}


class _NeverStartedThread:
    started = []

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        _NeverStartedThread.started.append(self)


def test_chat_start_registration_edge_refusal_compensates_the_launch(monkeypatch):
    """The fence closing between the early check and the registration edge.

    ``RunAdmissionClosed`` is raised inside the launch ``try``: the existing
    launch-failure compensation must restore the session snapshot, close the
    submitted journal entry, and leave no registered stream, launch claim or
    worker behind.
    """
    import types as _types

    import api.routes as routes
    import api.session_ops as session_ops
    import api.turn_journal as turn_journal

    restored = []
    journal = []
    _NeverStartedThread.started = []
    monkeypatch.setattr(routes.threading, "Thread", _NeverStartedThread)
    monkeypatch.setattr(routes.uuid, "uuid4", lambda: _types.SimpleNamespace(hex=_RACE_STREAM_ID))
    # The real preparation publishes the new turn on the session; the
    # compensation only restores a session that still points at this launch.
    monkeypatch.setattr(
        routes,
        "_prepare_chat_start_session_for_stream",
        lambda s, **k: setattr(s, "active_stream_id", k["stream_id"]),
    )
    monkeypatch.setattr(session_ops, "snapshot_session_state", lambda s: {"snap": True})
    monkeypatch.setattr(session_ops, "restore_session_state", lambda s, snap: restored.append(snap))

    def _journal(session_id, event):
        journal.append(dict(event))
        return {"turn_id": "turn-1"}

    monkeypatch.setattr(turn_journal, "append_turn_journal_event", _journal)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *a, **k: None)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda cfg: False)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda sid: threading.Lock())
    session = _RaceSession()
    session.active_stream_id = None
    session.pending_user_message = None
    monkeypatch.setattr(routes, "get_session", lambda sid: session)

    run_drain.hold_run_admission("update:webui")
    with pytest.raises(run_drain.RunAdmissionClosed) as exc_info:
        routes._start_chat_stream_for_session(
            session,
            msg="hello",
            attachments=[],
            workspace="test-workspace",
            model="test-model",
            model_provider="test-provider",
            external_runtime_owned=False,
        )

    assert exc_info.value.status == 503
    assert _RACE_STREAM_ID not in config.STREAMS
    assert _RACE_STREAM_ID not in config.PRE_ADMISSION_CLAIMS
    assert _NeverStartedThread.started == [], "no worker may start behind a closed fence"
    assert restored == [{"snap": True}], "the pre-launch session snapshot was not restored"
    assert [e.get("event") for e in journal] == ["submitted", "interrupted"]
    assert journal[-1].get("reason") == "start_compensated"


def test_btw_registration_edge_refusal_leaves_no_registered_stream(monkeypatch):
    import types as _types

    import api.background as background
    import api.models as models
    import api.routes as routes

    _NeverStartedThread.started = []
    monkeypatch.setattr(
        routes,
        "threading",
        _types.SimpleNamespace(Thread=_NeverStartedThread, Lock=threading.Lock, Event=threading.Event),
    )
    monkeypatch.setattr(routes.uuid, "uuid4", lambda: _types.SimpleNamespace(hex=_RACE_STREAM_ID))
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **k: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda sid: False)
    parent = _RaceSession()
    parent.active_stream_id = None
    monkeypatch.setattr(routes, "get_session", lambda sid: parent)
    monkeypatch.setattr(models, "new_session", lambda **kwargs: _RaceSession())
    tracked = []
    monkeypatch.setattr(background, "track_btw", lambda *a, **k: tracked.append(a))
    # Simulate losing the race: the cheap early check saw an open fence.
    monkeypatch.setattr(routes, "run_admission_closed_response", lambda: None)

    run_drain.hold_run_admission("update:webui")
    with pytest.raises(run_drain.RunAdmissionClosed):
        routes._handle_btw(_RecordingHandler(), {"session_id": "parent", "question": "what time is it"})

    assert _RACE_STREAM_ID not in config.STREAMS
    assert _RACE_STREAM_ID not in config.PRE_ADMISSION_CLAIMS
    assert _NeverStartedThread.started == []
    assert tracked == [], "a refused /btw must not be tracked as running"


def test_chat_start_early_refusal_is_503_before_any_session_work(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(
        routes, "_get_or_materialize_session", lambda *a, **k: pytest.fail("session touched")
    )

    class _Diag:
        finished = 0

        def stage(self, *_a, **_k):
            pass

        def finish(self):
            _Diag.finished += 1

    run_drain.begin_shutdown_drain()
    handler = _RecordingHandler()
    routes._handle_chat_start(handler, {"session_id": "s1", "message": "hi"}, diag=_Diag())
    assert handler.status == 503
    body = handler.body()
    assert body.get("code") == "server_draining"
    assert body.get("retryable") is True
    assert "_status" not in body
    assert _Diag.finished == 1, "the request diagnostics must still be finished"


def test_server_initiated_turn_gets_a_retryable_refusal_not_a_raise(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(routes, "get_session", lambda *a, **k: pytest.fail("session touched"))
    run_drain.hold_run_admission("update:webui")
    resp = routes.start_session_turn("s1", "[IMPORTANT: process finished]")
    assert resp["_status"] == 503
    assert resp["code"] == "server_draining"
    assert resp["retryable"] is True


# ── Wiring guards ────────────────────────────────────────────────────────────


def test_server_routes_signals_through_the_bounded_drain():
    src = (REPO / "server.py").read_text(encoding="utf-8")
    assert "ShutdownDrain(" in src
    assert "signal.SIGTERM" in src and "signal.SIGINT" in src
    assert "httpd.shutdown" in src
    assert "RunAdmissionClosed" in src


def test_every_stream_registration_edge_checks_the_fence():
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    registrations = src.count("STREAMS[stream_id] = stream")
    assert registrations >= 4
    assert src.count("assert_run_admission_open_locked()") >= registrations


def test_ctl_stop_waits_for_the_drain_budget():
    src = (REPO / "ctl.sh").read_text(encoding="utf-8")
    assert "HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS" in src
    assert "for i in {1..50}" not in src
