"""Run admission fence, bounded shutdown drain and run settlement (#7293).

Policy (owner-approved, "Option A"):

* A WebUI-initiated update/restart refuses while WebUI-owned agent runs are
  active, reports how many, and offers an explicit force. While an update is
  being applied the run-admission fence is closed, so nothing new can start
  between the safety check and the re-exec. A forced restart settles the
  remaining runs as durably interrupted before the process image is replaced.
* An external SIGTERM/SIGINT gets ONE bounded drain: admission closes, active
  runs may finish inside ``HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS`` (default 90s),
  anything still running at the deadline is settled durably as interrupted,
  then the HTTP server stops.
* A repeated signal escalates to an immediate settled stop. It never starts a
  second drain.
* A Gateway-backed run whose resumable run identity is durably saved keeps
  running in the Hermes Gateway and is reattached after a restart, so it is not
  counted, waited on or settled. Any other run (a legacy Gateway chat relay, or
  a Gateway launch still awaiting that durable handoff) counts as WebUI-owned.

Lock order: ``STREAMS_LOCK -> _STATE_LOCK``. ``_STATE_LOCK`` is a leaf lock and
nothing here takes ``STREAMS_LOCK`` or ``ACTIVE_RUNS_LOCK`` while holding it.
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import sys
import threading
import time
import uuid

from api import config as _config

logger = logging.getLogger(__name__)

SHUTDOWN_DRAIN_ENV = "HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS"
# Owner-chosen default (#7293): long enough for a typical agent turn to finish.
# A supervisor's stop timeout must exceed it (systemd ``TimeoutStopSec`` >=
# budget + 30s, i.e. 120s for the default); see docs/supervisor.md.
DEFAULT_SHUTDOWN_DRAIN_SECONDS = 90.0
# Upper bound for an operator-supplied budget, so a typo cannot keep a server
# half-stopped (admission closed) for days.
MAX_SHUTDOWN_DRAIN_SECONDS = 3600.0
# Time allowed for settling (writing the durable interrupted marker for) the
# runs still active when the drain deadline passes or a stop escalates.
SHUTDOWN_SETTLE_SECONDS = 3.0

SHUTDOWN_SETTLE_REASON = "The WebUI server shut down before this response finished"
RESTART_SETTLE_REASON = "The WebUI server restarted to apply an update before this response finished"

_ADMISSION_CLOSED_MESSAGES = {
    "draining": (
        "The WebUI server is shutting down. Your message was not started; "
        "send it again once the server is back."
    ),
    "restart_pending": (
        "The WebUI server is restarting to apply an update. Your message was "
        "not started; send it again in a few seconds."
    ),
    "update_hold": (
        "The WebUI server is applying an update. Your message was not started; "
        "send it again in a few seconds."
    ),
}


class RunAdmissionClosed(Exception):
    """Raised on a run-registration edge while the admission fence is closed."""

    status = 503

    def __init__(self, payload: dict):
        super().__init__(payload.get("error") or "server draining")
        self.payload = dict(payload)


def send_admission_closed(handler, exc: RunAdmissionClosed) -> None:
    """Answer a refused run registration with its 503; a client disconnect is fine."""
    from api.helpers import _CLIENT_DISCONNECT_ERRORS, j

    try:
        j(handler, exc.payload, status=exc.status)
    except _CLIENT_DISCONNECT_ERRORS:
        pass


# ── Admission fence state ───────────────────────────────────────────────────

_STATE_LOCK = threading.Lock()
_HOLDS: dict[str, dict] = {}
_RESTART_PENDING_EPOCH: int | None = None
_RESTART_FORCE = False
_EPOCH_COUNTER = 0
_DRAINING = False


def _reset_for_tests() -> None:
    global _RESTART_PENDING_EPOCH, _RESTART_FORCE, _EPOCH_COUNTER, _DRAINING
    with _STATE_LOCK:
        _HOLDS.clear()
        _RESTART_PENDING_EPOCH = None
        _RESTART_FORCE = False
        _EPOCH_COUNTER = 0
        _DRAINING = False


def _phase_locked() -> str:
    if _DRAINING:
        return "draining"
    if _RESTART_PENDING_EPOCH is not None:
        return "restart_pending"
    if _HOLDS:
        return "update_hold"
    return "open"


def hold_run_admission(owner: str, *, force: bool = False) -> str:
    """Close run admission for an in-progress WebUI update. Returns a hold token.

    Takes ``STREAMS_LOCK`` so the fence is a linearization point against the
    run-registration edges: any registration that passed the fence check has
    already published its ``STREAMS`` entry, so the caller's re-check after
    this returns sees it.
    """
    token = uuid.uuid4().hex
    with _config.STREAMS_LOCK:
        with _STATE_LOCK:
            _HOLDS[token] = {"owner": str(owner or "update"), "force": bool(force), "at": time.time()}
    return token


def release_run_admission_hold(token: str | None) -> None:
    if not token:
        return
    with _STATE_LOCK:
        _HOLDS.pop(token, None)


def mark_restart_pending() -> int:
    """Keep admission closed until the scheduled restart replaces the process.

    The force decision of the update(s) currently holding the fence travels
    with the pending restart; an earlier, already-released forced hold does not.
    Returns the restart epoch for ``abandon_restart_pending``.
    """
    global _RESTART_PENDING_EPOCH, _RESTART_FORCE, _EPOCH_COUNTER
    with _config.STREAMS_LOCK:
        with _STATE_LOCK:
            forced_now = any(h.get("force") for h in _HOLDS.values())
            if _RESTART_PENDING_EPOCH is None:
                _RESTART_FORCE = forced_now
            else:
                _RESTART_FORCE = _RESTART_FORCE or forced_now
            _EPOCH_COUNTER += 1
            _RESTART_PENDING_EPOCH = _EPOCH_COUNTER
            return _EPOCH_COUNTER


def abandon_restart_pending(epoch: int | None) -> bool:
    """Reopen admission when the scheduled restart did not replace the process.

    Only the newest pending restart may clear the state, so a stale restart
    thread cannot reopen admission underneath a later one.
    """
    global _RESTART_PENDING_EPOCH, _RESTART_FORCE
    with _STATE_LOCK:
        if epoch is None or _RESTART_PENDING_EPOCH != epoch:
            return False
        _RESTART_PENDING_EPOCH = None
        _RESTART_FORCE = False
        return True


def restart_force_requested() -> bool:
    with _STATE_LOCK:
        return bool(_RESTART_PENDING_EPOCH is not None and _RESTART_FORCE)


def begin_shutdown_drain() -> None:
    """Close admission for good: this process is stopping."""
    global _DRAINING
    with _config.STREAMS_LOCK:
        with _STATE_LOCK:
            _DRAINING = True


def is_draining() -> bool:
    with _STATE_LOCK:
        return _DRAINING


def admission_status() -> dict:
    with _STATE_LOCK:
        phase = _phase_locked()
        return {
            "open": phase == "open",
            "phase": phase,
            "holds": sorted(h.get("owner", "") for h in _HOLDS.values()),
            "restart_pending": _RESTART_PENDING_EPOCH is not None,
            "restart_forced": bool(_RESTART_PENDING_EPOCH is not None and _RESTART_FORCE),
            "draining": _DRAINING,
        }


def _closed_payload(phase: str) -> dict:
    return {
        "error": _ADMISSION_CLOSED_MESSAGES.get(phase, _ADMISSION_CLOSED_MESSAGES["draining"]),
        "code": "server_draining",
        "server_draining": True,
        "admission_phase": phase,
        # Transient: the same request succeeds once the server is back (or the
        # update hold is released). Server-side wakeup callers key their
        # retry-without-consuming-budget handling off this flag.
        "retryable": True,
    }


def run_admission_closed_response() -> dict | None:
    """Return a 503 route-result dict while admission is closed, else ``None``.

    Cheap early check for request handlers, before any session mutation. The
    authoritative check is ``assert_run_admission_open_locked()`` on the
    registration edge.
    """
    with _STATE_LOCK:
        phase = _phase_locked()
    if phase == "open":
        return None
    payload = _closed_payload(phase)
    payload["_status"] = RunAdmissionClosed.status
    return payload


def assert_run_admission_open_locked() -> None:
    """Raise ``RunAdmissionClosed`` when admission is closed.

    Call INSIDE the ``STREAMS_LOCK`` critical section that publishes the new
    ``STREAMS`` entry, before publishing it.
    """
    with _STATE_LOCK:
        phase = _phase_locked()
    if phase != "open":
        raise RunAdmissionClosed(_closed_payload(phase))


# ── WebUI-owned liveness ────────────────────────────────────────────────────


def _gateway_launching_ids() -> set:
    """Stream ids the Gateway backend is launching (labels only, never exclusion)."""
    gw = sys.modules.get("api.gateway_chat")
    if gw is None:
        return set()
    try:
        lifecycle = getattr(gw, "_STREAM_RUN_LIFECYCLE", None) or {}
        return {str(k) for k in list(lifecycle.keys())}
    except Exception:
        return set()


def _gateway_run_durably_resumable(stream_id: str) -> bool:
    """True once a restart can reattach this Gateway run (its identity is saved)."""
    gw = sys.modules.get("api.gateway_chat")
    if gw is None:
        return False
    try:
        return bool(gw.gateway_run_durably_resumable(stream_id))
    except Exception:
        return False


def webui_owned_active_runs() -> list[dict]:
    """Return the WebUI-owned agent work that is live right now.

    Union of ``ACTIVE_RUNS`` (worker lifecycle, including runs still unwinding
    after a cancel) and ``STREAMS`` (registered streams, including ones whose
    worker is still launching).

    The only exclusion is a Gateway run whose resumable run identity is already
    durably saved: it keeps running in the Gateway and a restarted WebUI
    reattaches it. Everything else is counted conservatively: a legacy
    ``/v1/chat/completions`` relay (never resumable) and a Gateway launch still
    awaiting that durable handoff would be abandoned by a restart, so they
    block updates and are drained/settled like any WebUI run. The decision does
    not depend on the order in which a launch publishes its ownership markers.
    """
    with _config.STREAMS_LOCK:
        stream_ids = [str(k) for k in _config.STREAMS.keys()]
        claimed = {str(k) for k in _config.PRE_ADMISSION_CLAIMS.keys()}
    with _config.ACTIVE_RUNS_LOCK:
        run_rows = {
            str(k): (dict(v) if isinstance(v, dict) else {})
            for k, v in _config.ACTIVE_RUNS.items()
        }
    gateway_launching = _gateway_launching_ids()

    runs: list[dict] = []
    for sid, row in run_rows.items():
        backend = str(row.get("backend") or "") or "legacy"
        if backend == "gateway" and _gateway_run_durably_resumable(sid):
            continue
        runs.append({
            "stream_id": sid,
            "session_id": row.get("session_id"),
            "backend": backend,
            "phase": row.get("phase") or "running",
            "launching": False,
            "registered_stream": sid in stream_ids,
        })
    for sid in stream_ids:
        if sid in run_rows:
            continue  # counted above, or a resumable Gateway row skipped above
        runs.append({
            "stream_id": sid,
            "session_id": None,
            "backend": "gateway" if sid in gateway_launching else "legacy",
            "phase": "launching" if sid in claimed else "registered",
            "launching": sid in claimed,
            "registered_stream": True,
        })
    return runs


# ── Settlement ──────────────────────────────────────────────────────────────


def settle_webui_owned_runs(reason: str, *, timeout: float = SHUTDOWN_SETTLE_SECONDS) -> dict:
    """Cancel every WebUI-owned run with ``reason`` and wait (bounded) for it.

    ``cancel_stream`` writes the durable interrupted marker (plus any partial
    output) and saves the session before it returns, so a settled run is
    durable even though its worker thread may still be unwinding. Each cancel
    runs in its own daemon thread so one wedged session lock cannot stall the
    others or the stop.
    """
    runs = webui_owned_active_runs()
    if not runs:
        return {"settled": [], "pending": [], "failed": [], "timed_out": False}
    from api import streaming as _streaming

    cancel = _streaming.cancel_stream
    done: dict[str, threading.Event] = {}
    errors: dict[str, str] = {}

    def _settle_one(stream_id: str, ev: threading.Event) -> None:
        try:
            # initiator="server": the durable marker says the WebUI server
            # stopped the run, not the user. Same partial-preserving path as a
            # user Stop.
            cancel(stream_id, reason=reason, initiator=_streaming.CANCEL_INITIATOR_SERVER)
        except Exception as exc:  # pragma: no cover - defensive
            errors[stream_id] = repr(exc)
            logger.warning("[shutdown-drain] settling run %s failed", stream_id, exc_info=True)
        finally:
            ev.set()

    for run in runs:
        sid = run["stream_id"]
        ev = threading.Event()
        done[sid] = ev
        threading.Thread(
            target=_settle_one, args=(sid, ev), name=f"webui-settle-{sid[:8]}", daemon=True
        ).start()

    deadline = time.monotonic() + max(0.0, float(timeout))
    for ev in done.values():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ev.wait(remaining)
    settled = sorted(sid for sid, ev in done.items() if ev.is_set() and sid not in errors)
    pending = sorted(sid for sid, ev in done.items() if not ev.is_set())
    return {
        "settled": settled,
        "pending": pending,
        "failed": sorted(errors),
        "timed_out": bool(pending),
    }


# ── Bounded shutdown drain ──────────────────────────────────────────────────


# Plain decimal seconds only ("90", "7.5", ".5", "-1"). ctl.sh parses the same
# grammar in pure bash to size its SIGTERM -> SIGKILL wait, so exponent forms,
# "inf" and "nan" are rejected here too (default) rather than drifting apart.
_BUDGET_RE = re.compile(r"^[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)$")


def shutdown_drain_budget_seconds() -> float:
    """``HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS``, clamped to [0, 3600]; default 90."""
    raw = os.environ.get(SHUTDOWN_DRAIN_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_SHUTDOWN_DRAIN_SECONDS
    text = str(raw).strip()
    if not _BUDGET_RE.match(text):
        logger.warning(
            "Ignoring invalid %s=%r (expected plain decimal seconds); using %ss",
            SHUTDOWN_DRAIN_ENV, raw, DEFAULT_SHUTDOWN_DRAIN_SECONDS,
        )
        return DEFAULT_SHUTDOWN_DRAIN_SECONDS
    value = float(text)
    return min(max(0.0, value), MAX_SHUTDOWN_DRAIN_SECONDS)


class ShutdownDrain:
    """One idempotent, bounded drain per process, driven by stop signals.

    ``handle_signal`` is the SIGTERM/SIGINT handler. It does no blocking work
    and takes no lock a reentrant handler could need: CPython runs a handler
    between bytecodes of whatever the main thread is doing, including another
    run of this same handler. The first call takes the one drain ticket
    (``next()`` on an ``itertools.count`` is a single atomic step) and starts
    the drain thread; every later call only sets the plain escalation flag,
    which the drain thread's bounded polling picks up. ``stop`` is called
    exactly once, from the drain thread, after the outcome is recorded
    (normally ``httpd.shutdown``).
    """

    def __init__(
        self,
        stop,
        *,
        budget_seconds: float | None = None,
        settle_seconds: float = SHUTDOWN_SETTLE_SECONDS,
        poll_seconds: float = 0.25,
        reason: str = SHUTDOWN_SETTLE_REASON,
    ):
        self._stop = stop
        self.budget_seconds = (
            shutdown_drain_budget_seconds()
            if budget_seconds is None
            else max(0.0, float(budget_seconds))
        )
        self.settle_seconds = max(0.0, float(settle_seconds))
        self.poll_seconds = max(0.01, float(poll_seconds))
        self.reason = reason
        self._tickets = itertools.count()
        self._escalated = False
        # Woken only by escalate() (a normal-thread API). Signal handlers never
        # touch it: Event.set() takes a non-reentrant lock.
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.signals: list[int] = []
        self.outcome: dict | None = None
        self.finished = threading.Event()

    def handle_signal(self, signum, _frame=None) -> None:
        try:
            self.signals.append(int(signum))
        except Exception:
            pass
        if next(self._tickets) == 0:
            thread = threading.Thread(target=self._run, name="webui-shutdown-drain", daemon=True)
            self._thread = thread
            thread.start()
            return
        # A repeated signal: escalate. A plain attribute store only.
        self._escalated = True

    def escalate(self) -> None:
        """Skip the rest of the drain: settle what is active and stop now.

        For normal threads only; signal handlers set the flag directly.
        """
        self._escalated = True
        self._wake.set()

    @property
    def escalated(self) -> bool:
        return self._escalated

    def _run(self) -> None:
        started = time.monotonic()
        result = "drained"
        settle = {"settled": [], "pending": [], "timed_out": False}
        try:
            begin_shutdown_drain()
            initial = webui_owned_active_runs()
            logger.info(
                "[shutdown-drain] stop requested; admission closed; %d WebUI-owned run(s) active; budget=%.1fs",
                len(initial), self.budget_seconds,
            )
            deadline = started + self.budget_seconds
            while True:
                if self._escalated:
                    result = "escalated"
                    break
                if not webui_owned_active_runs():
                    result = "drained"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result = "deadline"
                    break
                self._wake.wait(min(self.poll_seconds, remaining))
            if result != "drained":
                settle = settle_webui_owned_runs(self.reason, timeout=self.settle_seconds)
        except Exception:
            logger.exception("[shutdown-drain] drain failed; stopping anyway")
            result = "error"
        self.outcome = {
            "result": result,
            "elapsed": round(time.monotonic() - started, 3),
            "budget_seconds": self.budget_seconds,
            "settled": list(settle.get("settled") or []),
            "unsettled": list(settle.get("pending") or []),
        }
        logger.info(
            "[shutdown-drain] result=%s elapsed=%.2fs settled=%s unsettled=%s",
            result, self.outcome["elapsed"], self.outcome["settled"], self.outcome["unsettled"],
        )
        try:
            self._stop()
        finally:
            self.finished.set()
