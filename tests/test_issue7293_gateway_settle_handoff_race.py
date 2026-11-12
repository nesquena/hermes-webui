"""#7293 release gate (Codex f1 + f2, advisor reattach window): a server settle and a
Gateway run's durable handoff must be serialized.

A Runs-API launch is WebUI-owned drain work until its exact request is durably saved
(``_record_gateway_run``); from then on a restarted WebUI reattaches it, so the
shutdown/restart settle must leave it alone. For every interleaving of the settle with
that handoff exactly one of these holds:

(a) the durable handoff was published first: the settle skips the stream entirely (no
    server reason recorded, no STREAMS pop, no session change, no Gateway stop) and the
    run is still reattachable after a restart;
(b) the settle's cancellation was published first: the handoff fails, the run is never
    POSTed to the Gateway, no replayable request is left on disk, and the turn is
    settled with the server marker as before.

A user Stop that lands before the handoff must not POST the run either, and its
cancelled turn stays exactly what it was.

Every test drives the REAL Gateway worker (``_run_gateway_chat_streaming``), the REAL
``run_drain.settle_webui_owned_runs`` / ``streaming.cancel_stream`` and the REAL
handoff. Only the Gateway HTTP calls are synthetic. Interleavings are forced with
``threading.Event`` seams; timeouts on those waits are failure guards only.
"""

from __future__ import annotations

import io
import json
import queue
import threading
import time
import urllib.error
from email.message import Message

import pytest

import api.clarify as clarify
import api.config as config
import api.gateway_chat as gateway_chat
import api.models as models
import api.run_drain as run_drain
import api.streaming as streaming
from api.models import new_session
from tests.test_gateway_run_reattach_after_restart import (  # noqa: F401 (fixture)
    _orphaned_gateway_turn,
    _saved,
    _wait_for_reattach_threads,
    isolated_sessions,
)

PROMPT = "Summarize the quarterly report."
ANSWER = "The quarterly report, summarized."
RUN_ID = "run-7293-handoff"
REASON = run_drain.RESTART_SETTLE_REASON
WAIT = 10.0  # failure guard for every barrier wait, never a synchronization delay


@pytest.fixture(autouse=True)
def _clean_registries():
    run_drain._reset_for_tests()
    with config.STREAMS_LOCK:
        old_streams = dict(config.STREAMS)
        old_claims = dict(config.PRE_ADMISSION_CLAIMS)
        config.STREAMS.clear()
        config.PRE_ADMISSION_CLAIMS.clear()
    with config.ACTIVE_RUNS_LOCK:
        old_runs = dict(config.ACTIVE_RUNS)
        config.ACTIVE_RUNS.clear()
    with gateway_chat._DURABLE_RUN_STREAMS_LOCK:
        old_durable = set(gateway_chat._DURABLE_RUN_STREAMS)
        gateway_chat._DURABLE_RUN_STREAMS.clear()
    old_reasons = dict(streaming._SERVER_SETTLE_REASONS)
    streaming._SERVER_SETTLE_REASONS.clear()
    try:
        yield
    finally:
        run_drain._reset_for_tests()
        streaming._SERVER_SETTLE_REASONS.clear()
        streaming._SERVER_SETTLE_REASONS.update(old_reasons)
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


class _Launch:
    """A real Runs-API launch: route-side registration, then the real Gateway worker.

    ``pause_before_handoff`` parks the worker right before its initial durable handoff
    (``_record_gateway_run`` with the exact request); ``hold_admission`` parks the
    synthetic ``POST /v1/runs`` until released. Every POST is recorded.
    """

    def __init__(self, monkeypatch, stream_id, *, pause_before_handoff=True, hold_admission=False):
        s = new_session()
        s.active_stream_id = stream_id
        s.pending_user_message = PROMPT
        s.pending_attachments = []
        s.pending_started_at = time.time()
        s.pending_user_source = "webui"
        s.save()
        self.session_id = s.session_id
        self.stream_id = stream_id
        self.events: queue.Queue = queue.Queue()
        # What /api/chat/start publishes before it starts the worker.
        with config.STREAMS_LOCK:
            config.STREAMS[stream_id] = self.events
        config.register_stream_owner(stream_id, self.session_id)
        config.register_session_writeback_owner(self.session_id, stream_id)
        gateway_chat._mark_gateway_run_starting(stream_id)

        self.at_handoff = threading.Event()
        self.handoff_go = threading.Event()
        if not pause_before_handoff:
            self.handoff_go.set()
        self.handoff_done = threading.Event()
        self.handoff_result: list[str] = []
        self.admits: list[tuple[str, str]] = []
        self.admission_open = threading.Event()
        if not hold_admission:
            self.admission_open.set()
        self.stops: list[str] = []
        self.done = threading.Event()
        self.error: BaseException | None = None

        real_record = gateway_chat._record_gateway_run

        def record(session_id, stream_id_, run_id, request=None, **extra):
            if run_id or stream_id_ != stream_id:
                return real_record(session_id, stream_id_, run_id, request=request, **extra)
            self.at_handoff.set()
            assert self.handoff_go.wait(WAIT), "the handoff was never released"
            try:
                real_record(session_id, stream_id_, run_id, request=request, **extra)
            except BaseException as exc:
                self.handoff_result.append(type(exc).__name__)
                raise
            else:
                self.handoff_result.append("saved")
            finally:
                self.handoff_done.set()

        def admit(url_runs, headers, run_body, stream_id_):
            self.admits.append((stream_id_, json.dumps(run_body, sort_keys=True)))
            assert self.admission_open.wait(WAIT), "the admission was never released"
            return RUN_ID

        def no_event_replay(base_url, headers, run_id, last_seq=-1, **_kw):
            # The run's outcome then comes from its durable status (below).
            raise urllib.error.HTTPError(f"{base_url}/v1/runs/{run_id}/events", 404, "no", Message(), io.BytesIO(b""))

        monkeypatch.setattr(gateway_chat, "_record_gateway_run", record)
        monkeypatch.setattr(gateway_chat, "_admit_gateway_run", admit)
        monkeypatch.setattr(gateway_chat, "_open_gateway_run_events", no_event_replay)
        monkeypatch.setattr(
            gateway_chat, "_get_gateway_run_status",
            lambda base_url, api_key, run_id: {"run_id": run_id, "status": "completed", "output": ANSWER},
        )
        monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *a, **k: True)
        monkeypatch.setattr(gateway_chat, "stop_gateway_run", lambda run_id: self.stops.append(run_id) or True)
        self.thread = threading.Thread(target=self._work, name=f"gw-launch-{stream_id}", daemon=True)

    def _work(self):
        try:
            gateway_chat._run_gateway_chat_streaming(
                self.session_id, PROMPT, "test-model", "/tmp", self.stream_id, [],
            )
        except BaseException as exc:  # surfaced by join()
            self.error = exc
        finally:
            self.done.set()

    def start(self, *, wait_for_handoff=True):
        self.thread.start()
        if wait_for_handoff:
            assert self.at_handoff.wait(WAIT), "the worker never reached its durable handoff"

    def join(self):
        self.handoff_go.set()
        self.admission_open.set()
        self.thread.join(WAIT)
        assert not self.thread.is_alive(), "the Gateway worker did not finish"
        assert self.error is None, repr(self.error)

    def cancel_flag(self) -> threading.Event:
        return config.CANCEL_FLAGS[self.stream_id]

    def disk(self) -> dict:
        return _saved(self.session_id)

    def drain_events(self) -> list:
        out = []
        while not self.events.empty():
            out.append(self.events.get_nowait())
        return out


def _cancel_payloads(events) -> list[dict]:
    return [item[1] for item in events if item[0] == "cancel"]


def _cancel_markers(messages) -> list[dict]:
    return [
        m for m in messages
        if m.get("role") == "assistant" and str(m.get("content") or "").startswith("**Task cancelled:**")
    ]


def _start_settle(outcome: dict) -> threading.Thread:
    thread = threading.Thread(
        target=lambda: outcome.update(run_drain.settle_webui_owned_runs(REASON, timeout=30.0)),
        name="drain-settle", daemon=True,
    )
    thread.start()
    return thread


def _join(thread: threading.Thread) -> None:
    thread.join(WAIT * 2)
    assert not thread.is_alive(), f"{thread.name} did not finish"


def _assert_left_reattachable(launch: _Launch, disk: dict) -> None:
    """Property (a): the settle did not touch the durably handed-off run."""
    sid = launch.stream_id
    assert disk["active_stream_id"] == sid, "the settle cleared a durably resumable run's turn"
    record = disk.get("gateway_run") or {}
    assert record.get("stream_id") == sid and record.get("request"), (
        "the run's recovery record is gone: a restart cannot reattach it", record,
    )
    assert disk["pending_user_message"] == PROMPT
    assert not _cancel_markers(disk["messages"]), disk["messages"]
    assert gateway_chat.gateway_run_durably_resumable(sid)
    assert sid not in streaming._SERVER_SETTLE_REASONS, "a skipped run left a server settle reason behind"
    assert sid in config.STREAMS, "the settle popped a resumable run's stream"
    assert not launch.cancel_flag().is_set(), "the settle cancelled a resumable run"
    assert (config.ACTIVE_RUNS.get(sid) or {}).get("phase") != "cancelling"
    assert launch.stops == []
    assert sid not in {r["stream_id"] for r in run_drain.webui_owned_active_runs()}


def _assert_never_admitted_and_server_settled(launch: _Launch, disk: dict) -> None:
    """Property (b): cancelled before the handoff, so never POSTed; turn server-settled."""
    assert launch.admits == [], "a run cancelled before its durable handoff was still submitted to the Gateway"
    assert launch.handoff_result and launch.handoff_result != ["saved"], launch.handoff_result
    assert disk.get("gateway_run") is None, ("a replayable admission was left on disk", disk.get("gateway_run"))
    assert disk["active_stream_id"] is None and disk["pending_user_message"] is None
    msgs = disk["messages"]
    assert [m["content"] for m in msgs if m.get("role") == "user"] == [PROMPT], msgs
    markers = _cancel_markers(msgs)
    assert len(markers) == 1, msgs
    assert markers[0]["content"].startswith(f"**Task cancelled:** {REASON}."), markers
    assert markers[0].get("cancel_initiator") == "server", markers
    assert not any("cancelled by the user" in str(m.get("content") or "").lower() for m in msgs), msgs
    assert not gateway_chat.gateway_run_durably_resumable(launch.stream_id)
    assert launch.stops == []


# ── (a) the handoff wins: the settle skips the run ──────────────────────────


def test_handoff_between_settle_selection_and_cancel_leaves_the_run_reattachable(isolated_sessions, monkeypatch):  # noqa: F811 -- pytest fixture parameter; the module-level import registers the fixture
    """Codex f1: selection before the handoff, cancellation after it."""
    launch = _Launch(monkeypatch, "gw-a-held", hold_admission=True)
    launch.start()

    selected, settle_selected = [], threading.Event()
    real_cancel = streaming.cancel_stream

    def cancel_after_the_handoff(stream_id, reason=None, **kw):
        selected.append(stream_id)
        settle_selected.set()
        assert launch.handoff_done.wait(WAIT), "the handoff never completed"
        return real_cancel(stream_id, reason, **kw)

    monkeypatch.setattr(streaming, "cancel_stream", cancel_after_the_handoff)
    outcome: dict = {}
    settle = _start_settle(outcome)
    assert settle_selected.wait(WAIT), "the settle never selected the launch"
    assert selected == [launch.stream_id], "a launch awaiting its handoff must be settle work"
    launch.handoff_go.set()
    _join(settle)
    # The drain would stop the process right here.
    path = models.SESSION_DIR / f"{launch.session_id}.json"
    at_stop = path.read_bytes()
    assert launch.handoff_result == ["saved"]
    _assert_left_reattachable(launch, json.loads(at_stop))

    # The process lived on instead: the untouched run completes in place.
    launch.join()
    assert launch.disk()["messages"][-1]["content"] == ANSWER
    assert len(launch.admits) == 1

    # A restart from the state the drain left behind reattaches the same run.
    path.write_bytes(at_stop)
    models.SESSIONS.clear()
    assert gateway_chat.resume_gateway_runs_after_restart() == [launch.session_id]
    _wait_for_reattach_threads()
    saved = launch.disk()
    assert saved["messages"][-1]["content"] == ANSWER
    assert not any(m.get("_error") for m in saved["messages"]), saved["messages"]
    assert saved["active_stream_id"] is None and saved["gateway_run"] is None
    assert len(launch.admits) == 2 and launch.admits[0] == launch.admits[1], (
        "the restart must replay the same admission (same stream key, byte-identical body)"
    )


# ── (b) the settle wins: the handoff aborts the admission ───────────────────


def test_settle_published_before_the_handoff_never_admits_the_run(isolated_sessions, monkeypatch):  # noqa: F811 -- pytest fixture parameter; the module-level import registers the fixture
    """Codex f2: the stale-owner guard returned silently and the launch still POSTed."""
    launch = _Launch(monkeypatch, "gw-b-held")
    launch.start()

    outcome = run_drain.settle_webui_owned_runs(REASON, timeout=WAIT)
    assert outcome["settled"] == [launch.stream_id] and outcome["pending"] == [], outcome
    settled_messages = launch.disk()["messages"]
    launch.join()

    disk = launch.disk()
    _assert_never_admitted_and_server_settled(launch, disk)
    assert disk["messages"] == settled_messages, "the worker rewrote the settled turn"
    cancels = _cancel_payloads(launch.drain_events())
    assert cancels and all(c.get("cancel_initiator") == "server" for c in cancels), cancels


def test_user_stop_before_the_handoff_never_admits_and_keeps_the_cancelled_turn(isolated_sessions, monkeypatch):  # noqa: F811 -- pytest fixture parameter; the module-level import registers the fixture
    """The pre-existing orphan: a Stop just before the handoff still let the launch POST."""
    launch = _Launch(monkeypatch, "gw-user-stop")
    launch.start()

    assert streaming.cancel_stream(launch.stream_id) is True
    stopped = launch.disk()
    launch.join()

    assert launch.admits == [], "a run the user stopped before its handoff was still submitted"
    assert launch.handoff_result and launch.handoff_result != ["saved"], launch.handoff_result
    disk = launch.disk()
    # The user-cancel turn is exactly what Stop wrote; the worker adds nothing.
    assert disk["messages"] == stopped["messages"]
    assert disk.get("gateway_run") is None and disk["active_stream_id"] is None
    markers = _cancel_markers(disk["messages"])
    assert len(markers) == 1 and "cancel_initiator" not in markers[0], disk["messages"]
    assert markers[0]["provider_details"] == "Task cancelled."
    assert "cancelled by the user" in markers[0]["content"]
    assert [m["content"] for m in disk["messages"] if m.get("role") == "user"] == [PROMPT]
    assert launch.stream_id not in streaming._SERVER_SETTLE_REASONS
    # Same terminal events as before: Stop's own cancel, then the worker's.
    cancels = _cancel_payloads(launch.drain_events())
    assert len(cancels) == 2 and cancels[-1] == {"message": "Cancelled by user"}, cancels
    assert not any("cancel_initiator" in c for c in cancels), cancels


# ── the reattach window (advisor) ───────────────────────────────────────────


def test_settle_racing_a_reattach_never_settles_the_resumable_run(isolated_sessions, monkeypatch):  # noqa: F811 -- pytest fixture parameter; the module-level import registers the fixture
    """The reattach published its stream before marking it durable; a drain settle in
    that gap counted the run as WebUI-owned and wrote a server marker over it."""
    sid, stream_id = _orphaned_gateway_turn(run_id="run_survivor", stream_id="gw-reattach")
    release_run = threading.Event()

    def status(base_url, api_key, run_id):
        assert release_run.wait(WAIT)
        return {"run_id": run_id, "status": "completed", "output": ANSWER}

    stops: list = []
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", status)
    monkeypatch.setattr(gateway_chat, "stop_gateway_run", lambda run_id: stops.append(run_id) or True)

    durable_at_publication: list[bool] = []
    real_register = gateway_chat.register_active_run

    def probe_register(stream_id_, **metadata):
        if stream_id_ == stream_id and not durable_at_publication:
            durable_at_publication.append(gateway_chat.gateway_run_durably_resumable(stream_id_))
        return real_register(stream_id_, **metadata)

    gap_settles: list[dict] = []
    real_mark = gateway_chat._mark_gateway_run_durable

    def mark(stream_id_):
        # If the drain can already see the reattached stream while it is not yet
        # durable, a drain settle lands in that gap right now.
        if stream_id_ == stream_id and stream_id_ in config.STREAMS:
            gap_settles.append(run_drain.settle_webui_owned_runs(REASON, timeout=WAIT))
        return real_mark(stream_id_)

    monkeypatch.setattr(gateway_chat, "register_active_run", probe_register)
    monkeypatch.setattr(gateway_chat, "_mark_gateway_run_durable", mark)
    try:
        assert gateway_chat.resume_gateway_runs_after_restart() == [sid]
        # The drain deadline after the reattach: neither the drain nor a settle that
        # had already selected the stream may touch it.
        after = run_drain.settle_webui_owned_runs(REASON, timeout=WAIT)
        direct = streaming.cancel_stream(stream_id, reason=REASON, initiator=streaming.CANCEL_INITIATOR_SERVER)
        disk = _saved(sid)
    finally:
        release_run.set()

    assert not _cancel_markers(disk["messages"]), ("a server marker was written over a resumable run", disk["messages"])
    assert disk["active_stream_id"] == stream_id, "the settle cleared a resumable run's turn"
    assert (disk.get("gateway_run") or {}).get("run_id") == "run_survivor"
    assert durable_at_publication == [True], "the reattach was visible to the drain before it was durable"
    assert gap_settles == []
    assert after["settled"] == [] and direct is False, (after, direct)
    assert stream_id not in streaming._SERVER_SETTLE_REASONS
    assert stops == []

    _wait_for_reattach_threads()
    saved = _saved(sid)
    assert saved["messages"][-1]["content"] == ANSWER, saved["messages"]
    assert saved["active_stream_id"] is None and saved["gateway_run"] is None


# ── the settle's lock-busy fallback path ─────────────────────────────────────


@pytest.mark.parametrize("order", ["handoff_first", "settle_first_worker_finalizes", "settle_first_settle_finalizes"])
def test_lock_busy_settle_fallback_obeys_the_same_either_or(isolated_sessions, monkeypatch, order):  # noqa: F811 -- pytest fixture parameter; the module-level import registers the fixture
    """The handoff holds the session lock while it saves, so the settle cannot get it
    and falls back to publishing its cancellation first. The STREAMS_LOCK edge still
    decides: either the durable mark is first (the settle skips) or the cancellation is
    (the handoff withdraws its record and the run is never POSTed)."""
    stream_id = f"gw-fallback-{order}"
    launch = _Launch(
        monkeypatch, stream_id, pause_before_handoff=False, hold_admission=(order == "handoff_first"),
    )
    monkeypatch.setattr(streaming, "_SERVER_SETTLE_SESSION_LOCK_TIMEOUT", 0.05)

    in_save, save_go = threading.Event(), threading.Event()
    real_save = models.Session.save

    def save(self, *args, **kwargs):
        record = getattr(self, "gateway_run", None) or {}
        if record.get("stream_id") == stream_id and "request" in record and not in_save.is_set():
            in_save.set()  # the handoff now holds the session lock mid-save
            assert save_go.wait(WAIT), "the handoff save was never released"
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(models.Session, "save", save)

    fell_back: list[bool] = []
    settle_fell_back, settle_go = threading.Event(), threading.Event()
    real_lock_for = streaming._settle_session_lock_for_stream

    def lock_for(stream_id_):
        held = real_lock_for(stream_id_)
        if stream_id_ == stream_id:
            fell_back.append(held is None)
            settle_fell_back.set()
            if order == "handoff_first":
                assert settle_go.wait(WAIT)
        return held

    monkeypatch.setattr(streaming, "_settle_session_lock_for_stream", lock_for)

    settle_cancel_done = threading.Event()
    real_cancel = streaming.cancel_stream

    def cancel(stream_id_, reason=None, **kw):
        try:
            return real_cancel(stream_id_, reason, **kw)
        finally:
            settle_cancel_done.set()

    monkeypatch.setattr(streaming, "cancel_stream", cancel)

    if order == "settle_first_worker_finalizes":
        real_clear = clarify.clear_pending

        def clear_pending(session_key):
            # The settle published its cancellation; hold it before its session
            # writeback until the worker has finalized the turn on its own.
            if threading.current_thread().name.startswith("webui-settle-"):
                assert launch.done.wait(WAIT), "the worker never finished"
            return real_clear(session_key)

        monkeypatch.setattr(clarify, "clear_pending", clear_pending)
    elif order == "settle_first_settle_finalizes":
        real_settle_cancelled = gateway_chat._settle_gateway_cancelled_turn

        def settle_cancelled_turn(session_id, stream_id_):
            assert settle_cancel_done.wait(WAIT), "the settle never finished"
            return real_settle_cancelled(session_id, stream_id_)

        monkeypatch.setattr(gateway_chat, "_settle_gateway_cancelled_turn", settle_cancelled_turn)

    launch.start(wait_for_handoff=False)
    assert in_save.wait(WAIT), "the handoff never started saving"
    flag = launch.cancel_flag()
    outcome: dict = {}
    settle = _start_settle(outcome)
    if order == "handoff_first":
        assert settle_fell_back.wait(WAIT)
        save_go.set()
        assert launch.handoff_done.wait(WAIT)
        settle_go.set()
        _join(settle)
        path = models.SESSION_DIR / f"{launch.session_id}.json"
        assert fell_back == [True], "the settle did not take its lock-busy fallback"
        assert launch.handoff_result == ["saved"]
        _assert_left_reattachable(launch, json.loads(path.read_bytes()))
        launch.join()
        assert launch.disk()["messages"][-1]["content"] == ANSWER
        assert len(launch.admits) == 1
        return

    assert flag.wait(WAIT), "the settle never published its cancellation"
    save_go.set()
    _join(settle)
    launch.join()
    assert fell_back == [True], "the settle did not take its lock-busy fallback"
    assert outcome["settled"] == [stream_id] and outcome["pending"] == [], outcome
    _assert_never_admitted_and_server_settled(launch, launch.disk())
    cancels = _cancel_payloads(launch.drain_events())
    assert cancels and all(c.get("cancel_initiator") == "server" for c in cancels), cancels
