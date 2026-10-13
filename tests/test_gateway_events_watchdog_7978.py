#!/usr/bin/env python3
"""Regression harness for the #7978 gateway events watchdog rework.

Maintainer-style harness: the REAL ``_run_gateway_runs_api_streaming`` is
imported from ``api.gateway_chat`` and only its edges are faked —

  * ``urllib.request.urlopen``        -> scripted events-stream responses
  * ``api.gateway_chat._admit_gateway_run`` -> run admission (returns a fixed id)
  * ``api.gateway_chat._get_gateway_run_status`` -> scripted durable status polls
  * ``api.gateway_chat.time``         -> fake clock (scales the probe budget and
                                        the keepalive-stall budget instantly)
  * ``api.route_approvals.settle_gateway_pending_run`` -> recorded, no I/O

Everything else (SSE parsing, seq cursor bookkeeping, terminal handling,
STREAM_PARTIAL_TEXT writeback) runs for real. Every urlopen/status call is
recorded into a shared ordered trace with the fake-clock timestamp at call
time, so scenarios can assert CALL ORDER and ELAPSED bounds, not just
outcomes (round-3 review bar).

Regressions pinned (maintainer review of PR #7978):

  1. events 404 after a partial stream + durable status ``interrupted``
      -> the turn does NOT succeed with the partial text (Fix 1).
  2. status 503 during the stream while the stream later delivers
      ``run.completed`` -> the turn completes with the full answer, the probe
      budget is not exhausted, and Stop is honoured mid-way (Fix 2).
  3. completed status carrying a longer output than the streamed text
      -> the adopted output is the status output, in both settle paths
      (Fix 3), while an empty status output keeps the streamed text.

Round-2 review regressions pinned (2026-10-02):

  4. socket reset after a partial delta -> status 404 -> the turn fails
      closed within <= 2 status probes and ZERO poll-interval waits (a lost
      run must not spin the turn for the full ~298s reattach budget); the
      same rule holds for a bare status 404.
  5. the 404 grace is small and non-eager: a single 404 followed by
      ``running`` still lets the run complete, and a 404 interrupted by a
      503 restarts the streak (only CONSECUTIVE 404s are terminal) while
      the 503 alone keeps spending the long budget.

Round-3 review regressions pinned (2026-10-03):

  6. the 404 grace re-probe runs INSIDE status arbitration: on the first
      status 404 the second probe happens BEFORE any /events reopen and
      with ~0 fake-clock elapsed between the probes (pre-fix, the first
      404 ``continue``d the outer loop and the next iteration reopened
      /events, consuming a full watchdog interval of keepalives before
      probe 2 — the maintainer's ~122s clock witnesses). Pinned for
      keepalive-only and comment-only connections, and for the
      reset -> status 404 prompt fail-closed path; Stop between the two
      probes cancels without a reconnect; a 404 -> running -> completed
      control proves the grace does not false-positive; the 503 in a
      mixed 404/503/404->404 sequence resets the streak and keeps its
      paced reconnect.

Round-4 review regressions pinned (2026-10-05, Greptile findings + maintainer
round-3 re-review of 56f8d50c):

  7. a mid-stream socket drop discards the relay's local text carrier
      (the exception bypasses its return), so a ``completed`` durable
      status with an EMPTY output must fall back to the accumulated
      partial text — the turn never settles empty when partial text
      exists (Greptile item 1 + maintainer must-fix 1: the transport-
      exception handler re-syncs ``final_text`` from the shared buffer).
  8. ``run.completed`` is terminal: keepalives after the frame must not
      flip the lane to ``stalled`` and force a status probe (item 2).
  9. a byte-SILENT connection (no lines at all) must surface within the
      watchdog budget — the per-read wait is bounded, not the full 600s
      read timeout — and Stop pressed during the silence cancels inside
      that same budget (item 3).
  10. a durable status of ``waiting_for_approval`` carrying the approval
      payload surfaces the approval card exactly once, mirroring the
      reattach path, and a re-probe must not double-card (item 4).
  11. the settle flow has exactly ONE ``action == "continue"`` branch
      (item 6, also a maintainer nit: the duplicated dead branch is gone).
  12. maintainer must-fix 2: the cancelled-turn marker scan stops at the
      current user-turn boundary (a historical marker from a previous
      cancelled turn is never reused for this turn's partial).
  13. maintainer should-fix: the status-lane cancel persists the cancelled
      turn BEFORE emitting the browser-facing cancel event (the browser
      refetches the session the moment the event lands; the event callback
      here plays the browser and asserts the partial is already on disk).

Round-3 maintainer re-review regressions pinned (2026-10-05 08:57 UTC):

  12. the status-lane cancel persists the cancelled turn BEFORE emitting
      the browser-facing cancel event — the refetch-at-event-time proof
      (should-fix 3).
  13. the cancel-marker backward scan stops at the current user-turn
      boundary: a previous cancelled turn's marker is never reused for
      the new partial (must-fix 2).

Greptile round-3 review regression pinned (2026-10-05 09:50 UTC, one P1 on
40db362d — a round-4 latch regression; maintainer re-gate 2026-10-05 10:34
UTC sharpened the requirements):

  14. the round-4 ``terminal_frame_seen`` latch removed the relay's ONLY
      post-terminal exit: a gateway that keeps a completed run's events
      connection open and feeds comment keepalives forever (no [DONE], no
      EOF) pinned the loop — the latch disabled the stall check and the
      keepalives kept the read timeout fed, so the completed answer was
      stranded inside the relay and the caller's ``ended`` return was
      unreachable. The fix is two-layer: the ``run.completed`` handler
      breaks out on the frame itself (trailing frames are post-terminal
      noise), and the stall check, should any path re-enter the loop
      latched, finishes the turn as ``ended`` with the relayed text
      instead of probing or failing. Pinned with the maintainer's sharper
      probes, all on a bounded fake clock with NO status request (the
      frame is the arbiter) and no events reopen:
      a. infinite EOF-less comment tail after the frame → the terminal
         answer, usage and committed sequence are returned and the tail
         is NEVER READ (``keepalives_sent == 0``); JSON-framed and
         ``event:``-header-framed variants.
      b. a socket reset AFTER the frame → the immediate return handles it;
         the reset is never read, so it is not a transport failure.
      c. a delta frame AFTER the frame → ignored; the turn completes with
         the terminal answer, uncorrupted.

Round-4 maintainer re-gate regressions pinned (2026-10-05 10:46 UTC, must
fix + minor on the approval-mirror lane):

  15. the status-payload approval mirror can invert the FIFO queue: the
      durable status carries only the LATEST pending approval, so a stall
      surfaces B and the replay then delivers A -> B — on a non-identity
      gateway the shared dedupe set suppressed the replayed B and the
      browser queue became B -> A (the approved card could resolve a
      different command than the one shown). The status-card insert now
      requires BOTH a non-blank raw ``approval_id``/``id`` AND the
      ``approval_identity_v1`` capability; without them it is skipped
      entirely and the cursor-ordered replay alone orders the queue.
  16. identity-gateway variant: the exact-id status-card recovery is kept
      — B cards from status, A cards from the replay, B deduped.
  17. the status lane registers the approval key only AFTER the relay
      inserts the card, so a payload the translator rejects no longer
      suppresses the later event-feed replay of the same approval.

Runs under pytest on supported interpreters and standalone on Python 3.14
(``python3 tests/test_gateway_events_watchdog_7978.py``), where the repo
suite's conftest gate refuses to run.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import types
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.gateway_chat as gc  # noqa: E402
from api.gateway_chat import GATEWAY_REATTACH_MAX_POLL_FAILURES, GATEWAY_REATTACH_POLL_INTERVAL  # noqa: E402

BASE_URL = "http://gateway.test"
STREAM_ID = "watchdog-harness-stream"
RUN_ID = "run-watchdog-1"


# ---------------------------------------------------------------- harness ---


class FakeClock:
    """Monotonic clock advanced explicitly (and by every fake wait())."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class FakeCancelEvent:
    """threading.Event stand-in; wait() advances the fake clock only."""

    def __init__(self, clock: FakeClock):
        self._clock = clock
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, timeout=None):
        self._clock.advance(timeout or 0.0)
        return self._set


class StopDuringWaitEvent(FakeCancelEvent):
    """Simulates the user pressing Stop while the loop waits out a probe interval."""

    def wait(self, timeout=None):
        self._clock.advance(timeout or 0.0)
        self.set()
        return True


class FakeSseResponse:
    """SSE connection fake: iterates byte lines, optionally resetting mid-stream.

    ``lines_read`` counts every line the consumer actually pulled, so
    post-terminal "tail not consumed" assertions can pin that the relay
    stopped reading at the terminal frame (maintainer re-gate probe).

    ``advance_per_line`` advances a fake clock between lines, modelling a real
    connection that stays open and emits spaced keepalives (so the watchdog
    stall budget can be scaled instantly instead of waited out).
    """

    def __init__(self, lines, end="eof", clock=None, advance_per_line=0.0):
        self.lines = list(lines)
        self.end = end
        self._clock = clock
        self._advance = float(advance_per_line)
        self.lines_read = 0

    def __iter__(self):
        return self._gen()

    def _gen(self):
        for line in self.lines:
            self.lines_read += 1
            yield line
            if self._advance:
                self._clock.advance(self._advance)
        if self.end == "reset":
            raise OSError("[Errno 104] Connection reset by peer")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def sse_frame(seq, payload):
    lines = []
    if seq is not None:
        lines.append(f"id: {seq}".encode())
    if seq is not None:
        payload = {**payload, "seq": seq}
    lines.append(b"data: " + json.dumps(payload).encode())
    lines.append(b"")
    return lines


def keepalive():
    return [b": keepalive", b""]


class InfiniteKeepaliveSseResponse(FakeSseResponse):
    """SSE connection fake that replays its lines, then emits keepalives
    FOREVER — no [DONE], no EOF (the post-terminal stream Greptile's round-3
    review pinned). The per-line fake-clock advance models a live connection
    whose bytes keep the real read timeout fed, so nothing but the relay's
    own logic can end the turn.

    A safety valve at ``max_keepalives`` raises ``socket.timeout`` so the
    UNFIXED (red) run still terminates in bounded fake time; the fixed relay
    breaks out at the terminal frame long before the valve."""
    def __init__(self, lines, clock=None, advance_per_line=0.0, max_keepalives=8):
        super().__init__(lines, end="eof", clock=clock, advance_per_line=advance_per_line)
        self.max_keepalives = int(max_keepalives)
        self.keepalives_sent = 0

    def _gen(self):
        for line in self.lines:
            yield line
            if self._advance:
                self._clock.advance(self._advance)
        while self.keepalives_sent < self.max_keepalives:
            self.keepalives_sent += 1
            yield b": keepalive"
            if self._advance:
                self._clock.advance(self._advance)
            yield b""
            if self._advance:
                self._clock.advance(self._advance)
        raise socket.timeout(
            "harness safety valve: gateway never sent [DONE] nor closed "
            f"after {self.keepalives_sent} keepalives")


class ScriptedUrlopen:
    def __init__(self):
        self.requests = []
        self.timeouts = []
        self.script = []

    def queue(self, outcome):
        self.script.append(outcome)

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        self.timeouts.append(timeout)
        outcome = self.script.pop(0) if self.script else None
        if outcome is None:
            raise AssertionError(
                f"unexpected urlopen call #{len(self.requests)}: {req.full_url}")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def header(self, index, name):
        headers = self.requests[index].headers or {}
        for key, value in headers.items():
            if key.lower() == name.lower():
                return value
        return None


class SilentSseResponse:
    """Byte-SILENT SSE connection fake (round-4 item 3): accepts the request,
    then sends NOTHING — not even keepalives. The first read blocks for the
    connection's full read timeout (whatever the code under test passed to
    ``urlopen``, recorded by ``ScriptedUrlopen.timeouts`` and bound at queue
    time) and then raises ``socket.timeout`` — exactly how a real dead socket
    behind a dropped NAT mapping behaves. The fake clock is advanced by that
    timeout first, so elapsed-bound assertions measure the configured wait."""

    def __init__(self, clock, set_cancel=None):
        self._clock = clock
        self._set_cancel = set_cancel
        self._timeouts = None

    def bind_timeouts(self, timeouts):
        self._timeouts = timeouts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return self._gen()

    def _gen(self):
        if self._set_cancel is not None:
            self._set_cancel()  # the user presses Stop during the silence
        blocked = float(self._timeouts[-1]) if self._timeouts else 0.0
        self._clock.advance(blocked)
        raise socket.timeout(f"the read blocked for {blocked}s (byte-silent socket)")
        yield  # unreachable; makes _gen a generator


class ScriptedStatus:
    def __init__(self):
        self.calls = 0
        self.script = []

    def queue(self, outcome):
        self.script.append(outcome)

    def __call__(self, base_url, api_key, run_id):
        self.calls += 1
        outcome = self.script.pop(0) if self.script else {"status": "running"}
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)


def _http_error(code):
    return urllib.error.HTTPError(
        url=f"{BASE_URL}/v1/runs/{RUN_ID}/events",
        code=code,
        msg={404: "Not Found", 503: "Service Unavailable"}.get(code, "Error"),
        hdrs=None,
        fp=None,
    )


def run_turn(*, urlopen_script=(), status_script=(), cancel_event=None, clock=None,
             cancel_settle="stub", extra_patches=(), on_seq=None):
    """Run the real streaming function against the fakes; restore all patches.

    ``on_seq`` mirrors the production caller's cursor commit (default: none,
    as before) so scenarios can assert the terminal sequence was committed.

    ``cancel_settle="stub"`` (default) replaces ``_settle_gateway_cancelled_turn``
    with a traced no-op so status-lane cancel scenarios stay hermetic (they run
    against session ids with no Session object); the round-3 persistence tests
    call the real settle manually after the turn, exactly as the worker caller
    site does. ``cancel_settle="real"`` traces and CALLS the real settle inside
    the turn (round-4 persist-first proofs use this with real temp sessions).
    Settle calls are recorded into ``trace`` as ("cancel_settle", clock) either
    way, so ordering against the cancel event can be asserted."""
    clock = clock or FakeClock()
    urlopen = ScriptedUrlopen()
    status = ScriptedStatus()
    events = []
    trace = []
    for outcome in urlopen_script:
        if isinstance(outcome, SilentSseResponse):
            outcome.bind_timeouts(urlopen.timeouts)
        urlopen.queue(outcome)
    for outcome in status_script:
        status.queue(outcome)

    saved = []
    settles = []

    real_cancel_settle = gc._settle_gateway_cancelled_turn

    def traced_cancel_settle(session_key, stream_key):
        trace.append(("cancel_settle", clock.now))
        if cancel_settle == "real":
            return real_cancel_settle(session_key, stream_key)
        return None

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def traced(kind, fn):
        def call(*a, **k):
            trace.append((kind, clock.now))
            return fn(*a, **k)
        return call

    patch(gc, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    patch(gc, "_admit_gateway_run", lambda *a, **k: RUN_ID)
    patch(gc, "_get_gateway_run_status", traced("status", status))
    patch(gc, "_settle_gateway_cancelled_turn", traced_cancel_settle)
    patch(urllib.request, "urlopen", traced("events", urlopen))
    for obj, name, value in extra_patches:
        patch(obj, name, value)

    import api.route_approvals as ra

    def fake_settle(session_key, run_id, *, reason):
        settles.append(reason)
        return 0, None, 0

    patch(ra, "settle_gateway_pending_run", fake_settle)

    gc.STREAM_PARTIAL_TEXT[STREAM_ID] = ""
    saved_log_level = gc.logger.level
    gc.logger.setLevel(logging.CRITICAL)
    try:
        try:
            result = gc._run_gateway_runs_api_streaming(
                "sess-watchdog-harness", "hi", "test-model", "/tmp", STREAM_ID,
                BASE_URL, "test-key", [], {},
                put_gateway_event=lambda name, payload: events.append((name, payload)),
                cancel_event=cancel_event or FakeCancelEvent(clock),
                on_seq=on_seq,
            )
        except RuntimeError as exc:
            result = exc  # expected-raise scenarios inspect the give-up error
    finally:
        gc.logger.setLevel(saved_log_level)
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)
        gc.STREAM_PARTIAL_TEXT.pop(STREAM_ID, None)
    return {
        "result": result,
        "events": events,
        "urlopen": urlopen,
        "status": status,
        "clock": clock,
        "trace": trace,
        "settles": settles,
    }


def partial_delta(text, seq=0):
    return FakeSseResponse(
        sse_frame(seq, {"event": "message.delta", "delta": text}), end="reset")


# --------------------------------------------------------------- Fix 1 -----


def test_events_404_after_partial_stream_interrupted_status_does_not_settle_partial_text():
    """Fix 1: stream delivers partial text, socket resets, reconnect gets 404
    (gateway restarted), durable status says ``interrupted`` -> the turn must
    NOT succeed with the partial text; cancellation is surfaced instead."""
    harness = run_turn(
        urlopen_script=[
            partial_delta("Hello", seq=0),   # partial text, then socket reset
            _http_error(404),                # reconnect: gateway restarted, run reaped from events
        ],
        status_script=[
            {"status": "running"},           # probe 1 right after the reset: run still alive
            {"status": "interrupted"},       # probe 2 after the events 404: durable truth
        ],
    )

    result = harness["result"]
    assert result[0] is None, f"partial text must not settle as success, got {result!r}"
    assert result[0] != "Hello"
    cancel_events = [e for e in harness["events"] if e[0] == "cancel"]
    assert cancel_events, f"cancellation must be surfaced, events={harness['events']!r}"
    # The durable status was consulted BEFORE deciding, and the probe budget
    # was nowhere near exhausted.
    assert harness["status"].calls == 2
    # The reconnect carried the Last-Event-ID cursor from the partial stream.
    assert harness["urlopen"].header(1, "Last-Event-ID") == "0"
    # The gateway pending run was settled for the interrupted terminal state.
    assert any("interrupted" in reason for reason in harness["settles"])


# ------------------------------------------------- round-2 review: 404 ----


def test_events_404_then_status_404_fails_closed_within_two_probes():
    """Round-2 (maintainer-timed scenario) + round-3 call-order bar: socket
    reset after a partial delta -> durable status 404 -> the turn must fail
    closed within <= 2 status probes and ZERO poll-interval waits, and the
    second probe must run BEFORE any /events reconnect (round-3 review: the
    pre-fix branch reconnected events between the two probes)."""
    harness = run_turn(
        urlopen_script=[
            partial_delta("Hello", seq=0),   # partial text, then socket reset
            _http_error(404),                # pre-fix would reconnect events here
            _http_error(404),
        ],
        status_script=[
            _http_error(404),                # probe 1: definitive "no record of this run"
            _http_error(404),                # probe 2 (grace): terminal
        ],
    )

    raised = harness["result"]
    assert isinstance(raised, RuntimeError), f"expected fail-closed raise, got {raised!r}"
    assert "no longer has the run" in str(raised)
    # Timing proof: exactly the grace probes ran, and none of them slept a
    # poll interval — the pre-fix branch spent 150 probes / 298.0s here.
    assert harness["status"].calls == 2
    assert harness["clock"].now == 0.0, (
        f"grace probes must not sleep the poll interval, spent {harness['clock'].now}s")
    assert harness["clock"].now < 2 * GATEWAY_REATTACH_POLL_INTERVAL
    # Partial streamed text was never settled as success.
    assert harness["settles"] == []
    # Round-3 CALL ORDER: one events connect, then the two probes back to
    # back — no /events reopen between probe 1 and probe 2.
    assert harness["trace"] == [
        ("events", 0.0), ("status", 0.0), ("status", 0.0)], harness["trace"]
    assert len(harness["urlopen"].requests) == 1, (
        f"no /events reconnect between the grace probes, "
        f"got {len(harness['urlopen'].requests)} connects")


def test_bare_status_404_grace_second_consecutive_404_fails_closed():
    """Round-2 + round-3: a status 404 is terminal after the small grace even
    without an events failure in the picture: first 404 -> one immediate
    re-probe (no sleep, no /events reopen), second consecutive 404 -> fail
    closed. Pre-fix this scenario spent the full 150-probe budget (round-2)
    and reconnected events between the probes (round-3)."""
    harness = run_turn(
        urlopen_script=[
            FakeSseResponse(
                sse_frame(0, {"event": "message.delta", "delta": "Hello"}), end="eof"),
            _http_error(404),                # pre-fix would reconnect events here
        ],
        status_script=[
            _http_error(404),                # probe 1
            _http_error(404),                # probe 2 (grace): terminal
        ],
    )

    raised = harness["result"]
    assert isinstance(raised, RuntimeError), f"expected fail-closed raise, got {raised!r}"
    assert "no longer has the run" in str(raised)
    assert harness["status"].calls == 2
    assert harness["clock"].now == 0.0, (
        f"grace probes must not sleep the poll interval, spent {harness['clock'].now}s")
    assert harness["settles"] == []
    assert harness["trace"] == [
        ("events", 0.0), ("status", 0.0), ("status", 0.0)], harness["trace"]
    assert len(harness["urlopen"].requests) == 1, (
        f"no /events reconnect between the grace probes, "
        f"got {len(harness['urlopen'].requests)} connects")


def test_status_404_grace_does_not_false_positive_on_live_run():
    """Round-2 grace safety + round-3 ordering control: one status 404
    followed by a 200 ``running`` resets the streak — the run keeps going and
    completes normally, proving the grace is not overeager. The ``running``
    arbitration is what REOPENS /events (after the events-unreachable pacing
    wait), never the 404 grace itself."""
    harness = run_turn(
        urlopen_script=[
            partial_delta("Hello", seq=0),   # partial text, then socket reset
            # (no reconnect between the probes — the grace re-probe is in-arbitration)
            FakeSseResponse(
                keepalive()
                + sse_frame(1, {"event": "run.completed", "output": "Hello world"}),
                end="eof",
            ),
        ],
        status_script=[
            _http_error(404),                # probe 1: registration race / blip
            {"status": "running"},           # probe 2 (grace): run is alive -> streak reset
        ],
    )

    result = harness["result"]
    assert result[0] == "Hello world", f"live run must still complete, got {result!r}"
    # Probe 1 = 404 (streak 1), probe 2 = running (streak reset); the run then
    # completes on the events stream with no further probing.
    assert harness["status"].calls == 2
    # Exactly one poll-interval wait: the events-unreachable pacing after the
    # ``running`` probe. The 404 grace itself slept nothing.
    assert harness["clock"].now == GATEWAY_REATTACH_POLL_INTERVAL
    # Round-3 CALL ORDER: both probes precede the reconnect, and the reconnect
    # happens only after the ``running`` arbitration paced the reattach.
    assert harness["trace"] == [
        ("events", 0.0), ("status", 0.0), ("status", 0.0),
        ("events", GATEWAY_REATTACH_POLL_INTERVAL)], harness["trace"]


def test_status_404_streak_requires_consecutive_404s():
    """Round-2 + round-3: only CONSECUTIVE 404s are terminal — a 503 between
    two 404s restarts the streak (and still spends the long budget, with its
    poll-interval pacing wait); 404s themselves never sleep and never
    reopen /events (the 503's paced reconnect is the only second connect)."""
    harness = run_turn(
        urlopen_script=[
            FakeSseResponse(
                sse_frame(0, {"event": "message.delta", "delta": "Hello"}), end="eof"),
            _http_error(404),                # reconnect after the 503 pacing
            _http_error(404),
            _http_error(404),
        ],
        status_script=[
            _http_error(404),                # probe 1: streak = 1, immediate re-probe
            _http_error(503),                # probe 2 (grace): transient -> streak reset, budget spent
            _http_error(404),                # probe 3: streak = 1 again
            _http_error(404),                # probe 4 (grace): streak = 2 -> terminal
        ],
    )

    raised = harness["result"]
    assert isinstance(raised, RuntimeError), f"expected fail-closed raise, got {raised!r}"
    assert "no longer has the run" in str(raised)
    assert harness["status"].calls == 4
    # Only the 503 consumed a poll interval; neither 404 slept.
    assert harness["clock"].now == GATEWAY_REATTACH_POLL_INTERVAL
    # Round-3 CALL ORDER: the 503's pacing is the only reconnect — probes 3
    # and 4 run back to back inside arbitration (a missed streak reset would
    # have failed the turn already at probe 3 with only 3 status calls).
    assert harness["trace"] == [
        ("events", 0.0), ("status", 0.0), ("status", 0.0),
        ("events", GATEWAY_REATTACH_POLL_INTERVAL),
        ("status", GATEWAY_REATTACH_POLL_INTERVAL),
        ("status", GATEWAY_REATTACH_POLL_INTERVAL)], harness["trace"]
    assert len(harness["urlopen"].requests) == 2, (
        f"only the initial connect and the post-503 paced reconnect may happen, "
        f"got {len(harness['urlopen'].requests)} connects")


# ---------------------------------- round-3 review: in-arbitration grace ----


def test_keepalive_only_stream_then_status_404_x2_probes_back_to_back():
    """Round-3 primary finding (maintainer clock witnesses): a keepalive-only
    connection trips the stall watchdog -> status probe 1 gets 404 -> the
    grace re-probe must run IMMEDIATELY, BEFORE any /events reopen and with
    ~0 fake-clock elapsed. Pre-fix the first 404 ``continue``d the outer
    loop, reopening /events and burning another full watchdog interval of
    keepalives (~120s scaled) before probe 2."""
    clock = FakeClock()
    harness = run_turn(
        clock=clock,
        urlopen_script=[
            # Each keepalive block advances the fake clock 60s per line: the
            # stall budget (120s of comment-only traffic) trips inside the
            # first connection, and the second script entry is the reconnect
            # the PRE-FIX branch would burn.
            FakeSseResponse(keepalive() * 4, end="eof", clock=clock, advance_per_line=60.0),
            FakeSseResponse(keepalive() * 4, end="eof", clock=clock, advance_per_line=60.0),
        ],
        status_script=[
            _http_error(404),                # probe 1: definitive "no record of this run"
            _http_error(404),                # probe 2 (grace): terminal
        ],
    )

    raised = harness["result"]
    assert isinstance(raised, RuntimeError), f"expected fail-closed raise, got {raised!r}"
    assert "no longer has the run" in str(raised)
    assert harness["status"].calls == 2
    # CALL ORDER: one events connect, then the two probes back to back.
    assert harness["trace"] == [
        ("events", 0.0), ("status", 180.0), ("status", 180.0)], harness["trace"]
    assert len(harness["urlopen"].requests) == 1, (
        f"no /events reconnect between the grace probes, "
        f"got {len(harness['urlopen'].requests)} connects")
    # ELAPSED: probe 1 -> probe 2 delta is 0 fake seconds (no watchdog-interval
    # wait); the whole turn cost one stall interval, not two.
    probe_times = [t for kind, t in harness["trace"] if kind == "status"]
    assert probe_times[1] - probe_times[0] == 0.0, harness["trace"]
    assert harness["clock"].now == 180.0, harness["clock"].now


def test_comment_only_stream_then_status_404_x2_probes_back_to_back():
    """Round-3 comment-only variant: a connection that emits only comment
    frames and then EOFs lands in the same arbitration — probe 1 (404) and
    the grace re-probe run back to back with no /events reopen and no
    elapsed time between them."""
    clock = FakeClock()
    comments = [b": ping", b""] * 2
    harness = run_turn(
        clock=clock,
        urlopen_script=[
            FakeSseResponse(comments, end="eof", clock=clock, advance_per_line=30.0),
            FakeSseResponse(comments, end="eof", clock=clock, advance_per_line=30.0),
        ],
        status_script=[
            _http_error(404),                # probe 1
            _http_error(404),                # probe 2 (grace): terminal
        ],
    )

    raised = harness["result"]
    assert isinstance(raised, RuntimeError), f"expected fail-closed raise, got {raised!r}"
    assert "no longer has the run" in str(raised)
    assert harness["status"].calls == 2
    assert harness["trace"] == [
        ("events", 0.0), ("status", 120.0), ("status", 120.0)], harness["trace"]
    assert len(harness["urlopen"].requests) == 1, (
        f"no /events reconnect between the grace probes, "
        f"got {len(harness['urlopen'].requests)} connects")
    probe_times = [t for kind, t in harness["trace"] if kind == "status"]
    assert probe_times[1] - probe_times[0] == 0.0, harness["trace"]


def test_reset_events404_then_status404_x2_fails_closed_promptly():
    """Round-3: reset -> durable status 404 -> the grace re-probe (404) must
    fail the turn closed PROMPTLY — before any /events reconnect (the queued
    keepalive connection must never be consumed) and inside an elapsed bound
    well below one poll interval."""
    harness = run_turn(
        urlopen_script=[
            partial_delta("Hello", seq=0),   # partial text, then socket reset
            _http_error(404),                # pre-fix would reconnect events here
            FakeSseResponse(keepalive() * 30, end="eof"),  # pre-fix kept the turn alive here
        ],
        status_script=[
            _http_error(404),                # probe 1
            _http_error(404),                # probe 2 (grace): terminal
        ],
    )

    raised = harness["result"]
    assert isinstance(raised, RuntimeError), f"expected fail-closed raise, got {raised!r}"
    assert "no longer has the run" in str(raised)
    assert harness["status"].calls == 2
    assert harness["trace"] == [
        ("events", 0.0), ("status", 0.0), ("status", 0.0)], harness["trace"]
    assert len(harness["urlopen"].requests) == 1
    # Elapsed bound: fails closed immediately, not after a full interval.
    assert harness["clock"].now < GATEWAY_REATTACH_POLL_INTERVAL, harness["clock"].now


def test_stop_between_grace_probes_surfaces_cancel_without_reconnect():
    """Round-3: Stop pressed between the two 404 probes is honoured — the
    turn cancels immediately, without the grace re-probe and without any
    /events reopen."""
    clock = FakeClock()
    stop = FakeCancelEvent(clock)

    status = ScriptedStatus()

    def status_after_stop(base_url, api_key, run_id):
        stop.set()  # user presses Stop the instant probe 1 answers 404
        return status(base_url, api_key, run_id)

    urlopen = ScriptedUrlopen()
    urlopen.queue(FakeSseResponse(
        sse_frame(0, {"event": "message.delta", "delta": "Hello"}), end="eof"))
    urlopen.queue(_http_error(404))  # must never be consumed
    status.queue(_http_error(404))
    status.queue({"status": "running"})  # must never be reached

    saved = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    events = []
    patch(gc, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    patch(gc, "_admit_gateway_run", lambda *a, **k: RUN_ID)
    patch(gc, "_get_gateway_run_status", status_after_stop)
    patch(urllib.request, "urlopen", urlopen)
    import api.route_approvals as ra

    patch(ra, "settle_gateway_pending_run", lambda *a, **k: (0, None, 0))
    gc.STREAM_PARTIAL_TEXT[STREAM_ID] = ""
    try:
        result = gc._run_gateway_runs_api_streaming(
            "sess-watchdog-harness", "hi", "test-model", "/tmp", STREAM_ID,
            BASE_URL, "test-key", [], {},
            put_gateway_event=lambda name, payload: events.append((name, payload)),
            cancel_event=stop,
        )
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)
        gc.STREAM_PARTIAL_TEXT.pop(STREAM_ID, None)

    assert result[0] is None, f"Stop between the probes must cancel, got {result!r}"
    assert any(name == "cancel" for name, _ in events), f"events={events!r}"
    assert status.calls == 1, "no grace re-probe after Stop"
    assert len(urlopen.requests) == 1, "no /events reopen after Stop"


# ---------------------------- round-4 review (Greptile + maintainer) -------


_R4_WATCHDOG_BUDGET_BOUND = 125.0  # 120s budget + read epsilon + slack


def test_r4_transport_reset_completed_empty_output_returns_partial_text():
    """Round-4 Greptile item 1 + maintainer must-fix 1: a mid-stream socket
    reset discards the relay's LOCAL text carrier (the exception bypasses its
    return), and the durable status then reports ``completed`` with an EMPTY
    output. The settle path is deterministic: status output when non-empty,
    ELSE the accumulated partial text (either carrier) — never empty when
    partial text exists. (Pre-fix this settled "" and cleared pending state;
    maintainer repro.)"""
    harness = run_turn(
        urlopen_script=[
            partial_delta("Hello", seq=0),   # partial text, then socket reset
        ],
        status_script=[
            {"status": "completed", "output": ""},
        ],
    )
    result = harness["result"]
    assert result[0] == "Hello", (
        f"completed-with-empty-output must keep the streamed partial, got {result!r}")


def test_r4_run_completed_frame_latches_terminal_despite_keepalives():
    """Round-4 Greptile item 2: ``run.completed`` is terminal. Keepalives
    after the frame must not flip the outcome to ``stalled`` and force a
    status probe — a probe failure would FAIL a turn the gateway already
    finished. Call-order assertion: NO /v1/runs/{id} status GET after the
    frame. Greptile round-3 strengthening: the turn must also RETURN
    promptly — on the unfixed branch the relay kept consuming the trailing
    keepalives (fake clock 540s) before EOF; fixed, it breaks out on the
    frame itself."""
    clock = FakeClock()
    stream = FakeSseResponse(
        sse_frame(0, {"event": "run.completed", "output": "Done answer"})
        + keepalive() * 4,
        end="eof", clock=clock, advance_per_line=60.0,
    )
    harness = run_turn(
        clock=clock,
        urlopen_script=[stream],
        status_script=[],  # ANY probe here is the bug
    )
    result = harness["result"]
    assert result[0] == "Done answer", f"turn must complete from the frame, got {result!r}"
    assert harness["status"].calls == 0, "no status GET may follow the terminal frame"
    assert [t for t in harness["trace"] if t[0] == "status"] == [], harness["trace"]
    assert clock.now <= _R4_WATCHDOG_BUDGET_BOUND, (
        f"turn must return promptly on the terminal frame, burned {clock.now}s")
    assert stream.lines_read == 2, (
        f"the post-terminal tail must not be read at all, read {stream.lines_read} lines")


_R5_POSTTERMINAL_BOUND = 480.0  # the pre-terminal frames' fake-clock cost + slack


def test_r5_run_completed_breaks_out_of_infinite_postterminal_keepalives():
    """Greptile round-3 P1 (regression from the round-4 latch), maintainer
    re-gate probe: a completed run's events connection stays open and feeds
    comment keepalives FOREVER — no [DONE], no EOF. The round-4 latch
    disabled the stall check, so nothing ended the relay and the completed
    answer was stranded (the caller's ``ended`` return was unreachable). The
    turn must RETURN the frame's answer, usage and committed sequence within
    a bounded fake-clock window, with NO status request (the terminal frame
    is the arbiter), no events reopen, and the tail must NOT BE READ AT ALL
    (``keepalives_sent == 0`` pins the relay stopped at the frame). Red on
    40db362d: the relay consumed the keepalives to the harness safety valve,
    then the valve's timeout forced a status poll that returned different
    text."""
    import api.config as api_config

    clock = FakeClock()
    approval_settles = []
    committed_seqs = []
    stream = InfiniteKeepaliveSseResponse(
        sse_frame(1, {
            "event": "approval.request", "command": "rm -rf /tmp/x",
            "description": "Dangerous command approval",
            "pattern_key": "dangerous_command",
            "pattern_keys": ["dangerous_command"],
            "choices": ["once", "always"], "approval_id": "appr-r5-1",
        })
        + sse_frame(2, {"event": "message.delta", "delta": "streamed "})
        + sse_frame(3, {
            "event": "run.completed", "output": "Hello",
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }),
        clock=clock, advance_per_line=60.0,
    )
    harness = run_turn(
        clock=clock,
        urlopen_script=[stream],
        status_script=[
            # Red-only: reached after the safety-valve timeout. The fixed
            # relay must never get here — the frame is the arbiter.
            {"status": "completed", "output": "status-poll-text"},
        ],
        extra_patches=[
            (gc, "_settle_gateway_run_approval",
             lambda *a, **k: approval_settles.append(k) or (True, {"approval_id": "appr-r5-1"}, 0)),
            (api_config, "gateway_supports_approval_identity_v1", lambda *a, **k: False),
        ],
        on_seq=committed_seqs.append,
    )
    result = harness["result"]
    assert result[0] == "Hello", (
        f"turn must return the terminal frame's output, got {result!r}")
    assert result[1] == {"input_tokens": 3, "output_tokens": 4}, (
        f"terminal usage must be retained through the break, got {result[1]!r}")
    assert committed_seqs == [1, 2, 3], (
        f"the terminal sequence must be committed before the break, got {committed_seqs}")
    assert len(approval_settles) == 1, harness["trace"]
    assert harness["status"].calls == 0, "no status GET may follow the terminal frame"
    assert [t for t in harness["trace"] if t[0] == "status"] == [], harness["trace"]
    assert len(harness["urlopen"].requests) == 1, (
        "the terminal frame must end the turn without reopening /events")
    assert stream.keepalives_sent == 0, (
        f"the post-terminal tail must not be read at all, consumed {stream.keepalives_sent} keepalives")
    assert clock.now <= _R5_POSTTERMINAL_BOUND, (
        f"turn must return promptly on the terminal frame, burned {clock.now}s")


def test_r5_event_header_framed_terminal_frame_breaks_out_of_keepalives():
    """Variant: the terminal frame is framed with the SSE ``event:`` header
    (payload carries no event key — the relay falls back to ``sse_event``),
    deltas stream before it, [DONE] is never sent, and the keepalives run
    forever. Same assertions: the turn returns "Hello" promptly with no
    status probe, no events reopen, and the tail not read at all. Red on
    40db362d for the same mechanism as the JSON-framed scenario."""
    import api.config as api_config

    clock = FakeClock()
    stream = InfiniteKeepaliveSseResponse(
        [b"event: message.delta", b"data: " + json.dumps({"delta": "streamed "}).encode(), b""]
        + [b"event: run.completed", b"data: " + json.dumps({"output": "Hello"}).encode(), b""],
        clock=clock, advance_per_line=60.0,
    )
    harness = run_turn(
        clock=clock,
        urlopen_script=[stream],
        status_script=[
            # Red-only: reached after the safety-valve timeout.
            {"status": "completed", "output": "status-poll-text"},
        ],
        extra_patches=[
            (api_config, "gateway_supports_approval_identity_v1", lambda *a, **k: False),
        ],
    )
    result = harness["result"]
    assert result[0] == "Hello", (
        f"turn must return the terminal frame's output, got {result!r}")
    assert harness["status"].calls == 0, "no status GET may follow the terminal frame"
    assert [t for t in harness["trace"] if t[0] == "status"] == [], harness["trace"]
    assert len(harness["urlopen"].requests) == 1, (
        "the terminal frame must end the turn without reopening /events")
    assert stream.keepalives_sent == 0, (
        f"the post-terminal tail must not be read at all, consumed {stream.keepalives_sent} keepalives")
    assert clock.now <= _R5_POSTTERMINAL_BOUND, (
        f"turn must return promptly on the terminal frame, burned {clock.now}s")


def test_r5_socket_reset_after_terminal_frame_returns_immediately():
    """Maintainer re-gate (b): a socket reset AFTER the terminal frame must be
    handled by the immediate return — the reset is never read, so it must not
    be treated as a transport failure (no status probe, no events reopen).
    Red on 40db362d: the relay read past the frame, the generator's reset
    raised, and the turn fell into the transport-failure status poll."""
    clock = FakeClock()
    stream = FakeSseResponse(
        sse_frame(0, {"event": "run.completed", "output": "Hello"}),
        end="reset",  # the reset fires only if the relay reads past the frame
    )
    harness = run_turn(clock=clock, urlopen_script=[stream])
    result = harness["result"]
    assert result[0] == "Hello", (
        f"turn must return the terminal frame's output, got {result!r}")
    assert harness["status"].calls == 0, (
        "a post-terminal reset must not become a transport failure / status probe")
    assert len(harness["urlopen"].requests) == 1, (
        "no events reopen may follow an accepted completion")
    assert stream.lines_read == 2, (
        f"relay must stop reading at the terminal frame's data line, read {stream.lines_read} lines")


def test_r5_postterminal_delta_frame_ignored():
    """Maintainer re-gate (c): a delta frame arriving AFTER run.completed is
    post-terminal noise — the turn completes with the terminal answer, the
    delta is neither read nor relayed. Red on 40db362d: the relay consumed
    the delta and appended it, corrupting the accepted answer with
    'HelloEXTRA'."""
    clock = FakeClock()
    stream = FakeSseResponse(
        sse_frame(0, {"event": "run.completed", "output": "Hello"})
        + sse_frame(1, {"event": "message.delta", "delta": "EXTRA"}),
        end="eof",
    )
    harness = run_turn(clock=clock, urlopen_script=[stream])
    result = harness["result"]
    assert result[0] == "Hello", (
        f"a post-terminal delta must not corrupt the accepted answer, got {result!r}")
    tokens = [p["text"] for name, p in harness["events"] if name == "token"]
    assert tokens == [], f"the post-terminal delta must not be relayed, got {tokens}"
    assert harness["status"].calls == 0, harness["trace"]
    assert stream.lines_read == 2, (
        f"relay must stop reading at the terminal frame's data line, read {stream.lines_read} lines")


def test_r4_byte_silent_stream_probes_status_within_watchdog_budget():
    """Round-4 Greptile item 3: a connection that accepts the request then
    sends NOTHING (no lines at all) must surface within the watchdog budget —
    the per-read wait is bounded, so byte-silence cannot pin the full
    configured 600s read timeout before the status probe runs. Elapsed bound
    asserted on the fake clock AND on the timeout passed to urlopen."""
    clock = FakeClock()
    harness = run_turn(
        clock=clock,
        urlopen_script=[SilentSseResponse(clock)],
        status_script=[{"status": "completed", "output": "recovered from status"}],
    )
    result = harness["result"]
    assert result[0] == "recovered from status", result
    timeouts = harness["urlopen"].timeouts
    assert timeouts, harness["trace"]
    assert timeouts[0] <= _R4_WATCHDOG_BUDGET_BOUND, (
        f"events read must be bounded by the watchdog budget, got timeout={timeouts[0]}")
    assert timeouts[0] < 600.0, timeouts
    status_times = [t for kind, t in harness["trace"] if kind == "status"]
    assert status_times, harness["trace"]
    assert status_times[0] <= _R4_WATCHDOG_BUDGET_BOUND, (
        f"status probe must run within ~one watchdog interval, probed at {status_times[0]}s")
    assert harness["clock"].now < 600.0, harness["clock"].now


def test_r4_stop_during_byte_silence_cancels_within_budget():
    """Round-4 Greptile item 3 (Stop arm): Stop pressed during byte-silence is
    honoured within the watchdog budget — the cancel path must run without
    waiting the full 600s read timeout, and must not spend a status probe on
    the way out."""
    clock = FakeClock()
    stop = FakeCancelEvent(clock)
    harness = run_turn(
        clock=clock,
        cancel_event=stop,
        urlopen_script=[SilentSseResponse(clock, set_cancel=stop.set)],
        status_script=[],  # ANY probe here is the bug
    )
    assert harness["result"][0] is None, harness["result"]
    assert any(name == "cancel" for name, _ in harness["events"]), harness["events"]
    assert harness["status"].calls == 0, "Stop during silence must cancel without a status probe"
    assert harness["clock"].now <= _R4_WATCHDOG_BUDGET_BOUND, harness["clock"].now
    timeouts = harness["urlopen"].timeouts
    assert timeouts and timeouts[0] <= _R4_WATCHDOG_BUDGET_BOUND, timeouts


def test_r4_status_waiting_for_approval_surfaces_approval_card_once():
    """Round-4 Greptile item 4, identity-gateway contract (round-4 maintainer
    must-fix): on a gateway advertising ``approval_identity_v1``, a durable
    status of ``waiting_for_approval`` carrying the approval payload surfaces
    the card exactly once (mirroring the reattach path's parked-approval
    surface), even when the events feed never delivered it. A re-probe
    carrying the same approval key must NOT double-card, and a later
    ``completed`` status finalizes the turn normally. On non-identity
    gateways the status-card insert is skipped entirely — the FIFO ordering
    scenarios below pin that."""
    import api.config as api_config

    clock = FakeClock()
    approval_payload = {
        "approval_id": "appr-r4-1",
        "tool": "shell",
        "command": "ls -la /tmp",
        "description": "list files",
        "risk_level": "high",
        "choices": ["once", "always"],
    }
    harness = run_turn(
        clock=clock,
        urlopen_script=[
            # Two keepalive-only connections trip the stall watchdog and land
            # in status arbitration; the events feed never carries the frame.
            FakeSseResponse(keepalive() * 4, end="eof", clock=clock, advance_per_line=60.0),
            FakeSseResponse(keepalive() * 4, end="eof", clock=clock, advance_per_line=60.0),
            FakeSseResponse(
                sse_frame(0, {"event": "run.completed", "output": "after approval"}),
                end="eof",
            ),
        ],
        status_script=[
            {"status": "waiting_for_approval", "approval": dict(approval_payload)},
            {"status": "waiting_for_approval", "approval": dict(approval_payload)},
        ],
        extra_patches=[
            # identity gateway: the raw approval_id carries the exact-id
            # status-card recovery; the patch also keeps the capability
            # probe off the scripted urlopen wire
            (api_config, "gateway_supports_approval_identity_v1", lambda *a, **k: True),
        ],
    )
    result = harness["result"]
    assert result[0] == "after approval", (
        f"run must finalize after the approval lane, got {result!r}")
    approvals = [payload for name, payload in harness["events"] if name == "approval"]
    assert len(approvals) == 1, (
        f"exactly one approval card expected (re-probe must not double-card), "
        f"got {len(approvals)}: {approvals!r}")
    assert approvals[0]["approval_id"] == "appr-r4-1", approvals[0]
    assert approvals[0]["command"] == "ls -la /tmp", approvals[0]
    assert harness["status"].calls == 2


def _approval_request_frame(seq, appr_id, command):
    return sse_frame(seq, {
        "event": "approval.request",
        "approval_id": appr_id,
        "tool": "shell",
        "command": command,
        "description": f"run {command}",
        "pattern_key": "dangerous_command",
        "pattern_keys": ["dangerous_command"],
        "choices": ["once", "always"],
        "risk_level": "high",
    })


def _pending_approval_payload(appr_id, command):
    """Durable-status shape of one parked approval."""
    return {
        "approval_id": appr_id,
        "tool": "shell",
        "command": command,
        "description": f"run {command}",
        "risk_level": "high",
        "choices": ["once", "always"],
    }


def test_r6_status_approval_mirror_skipped_without_identity_fifo_replay_orders_a_b():
    """Maintainer round-4 must-fix repro: approvals queued A -> B on the
    gateway, the watchdog stalls, the durable status carries only B, then the
    events replay delivers A -> B. On a non-identity gateway the status-card
    insert must be SKIPPED so the cursor-ordered replay alone surfaces the
    queue A -> B. Pre-fix the status mirror surfaced B first and the replay's
    B was suppressed by the shared dedupe set — the browser queue became
    B -> A and the card the user approved could resolve a different pending
    command than the one shown."""
    import api.config as api_config

    clock = FakeClock()
    settles = []

    def fake_settle(*args, **kwargs):
        settles.append(kwargs.get("approval") or (args[1] if len(args) > 1 else None))
        return False, None, 1  # not auto-approved -> the card is surfaced

    harness = run_turn(
        clock=clock,
        urlopen_script=[
            # keepalives only -> stall -> status probe 1 (carries B)
            FakeSseResponse(keepalive() * 4, end="eof", clock=clock, advance_per_line=60.0),
            # reconnect: the cursor-ordered replay delivers A then B, then
            # keepalives stall again -> probe 2 (B again)
            FakeSseResponse(
                _approval_request_frame(1, "appr-A", "echo first")
                + _approval_request_frame(2, "appr-B", "rm -rf /tmp/x")
                + keepalive() * 4,
                end="eof", clock=clock, advance_per_line=60.0,
            ),
            FakeSseResponse(
                sse_frame(0, {"event": "run.completed", "output": "done after approvals"}),
                end="eof",
            ),
        ],
        status_script=[
            {"status": "waiting_for_approval",
             "approval": _pending_approval_payload("appr-B", "rm -rf /tmp/x")},
            {"status": "waiting_for_approval",
             "approval": _pending_approval_payload("appr-B", "rm -rf /tmp/x")},
        ],
        extra_patches=[
            (api_config, "gateway_supports_approval_identity_v1", lambda *a, **k: False),
            (gc, "_settle_gateway_run_approval", fake_settle),
        ],
    )
    result = harness["result"]
    assert result[0] == "done after approvals", (
        f"run must finalize after the approval lane, got {result!r}")
    approvals = [payload["approval_id"] for name, payload in harness["events"]
                 if name == "approval"]
    assert approvals == ["appr-A", "appr-B"], (
        f"FIFO queue order must be preserved by the replay (A -> B), got {approvals}")
    assert harness["status"].calls == 2, harness["trace"]


def test_r6_identity_gateway_status_card_from_status_deduped_against_replay():
    """Maintainer round-4 must-fix, identity-gateway variant: the exact-id
    status-card recovery is KEPT. With ``approval_identity_v1`` advertised
    and a raw approval id on the payload, the stalled probe surfaces B's
    card from status, the replay then delivers A (surfaced) and B (deduped
    by exact id) — each approval cards exactly once. (Not red by design:
    pins the preserved behavior.)"""
    import api.config as api_config

    clock = FakeClock()

    def fake_settle(*args, **kwargs):
        return False, None, 1

    harness = run_turn(
        clock=clock,
        urlopen_script=[
            FakeSseResponse(keepalive() * 4, end="eof", clock=clock, advance_per_line=60.0),
            FakeSseResponse(
                _approval_request_frame(1, "appr-A", "echo first")
                + _approval_request_frame(2, "appr-B", "rm -rf /tmp/x")
                + keepalive() * 4,
                end="eof", clock=clock, advance_per_line=60.0,
            ),
            FakeSseResponse(
                sse_frame(0, {"event": "run.completed", "output": "done after approvals"}),
                end="eof",
            ),
        ],
        status_script=[
            {"status": "waiting_for_approval",
             "approval": _pending_approval_payload("appr-B", "rm -rf /tmp/x")},
            {"status": "waiting_for_approval",
             "approval": _pending_approval_payload("appr-B", "rm -rf /tmp/x")},
        ],
        extra_patches=[
            (api_config, "gateway_supports_approval_identity_v1", lambda *a, **k: True),
            (gc, "_settle_gateway_run_approval", fake_settle),
        ],
    )
    result = harness["result"]
    assert result[0] == "done after approvals", (
        f"run must finalize after the approval lane, got {result!r}")
    approvals = [payload["approval_id"] for name, payload in harness["events"]
                 if name == "approval"]
    assert sorted(approvals) == ["appr-A", "appr-B"], (
        f"each approval must card exactly once, got {approvals}")
    assert approvals.count("appr-B") == 1, (
        f"the status card must be deduped against the replay, got {approvals}")
    assert harness["status"].calls == 2, harness["trace"]


def test_r6_rejected_status_approval_payload_does_not_mask_replay():
    """Maintainer round-4 minor: the status lane registered the approval key
    BEFORE the relay call, so a payload the translator rejects (no tool,
    command or description) still suppressed the later event-feed replay of
    the same approval — the approval never surfaced at all. The key must be
    registered only after the relay inserts the card, so a rejected status
    payload never masks the replayed event."""
    import api.config as api_config

    clock = FakeClock()

    def fake_settle(*args, **kwargs):
        return False, None, 1

    harness = run_turn(
        clock=clock,
        urlopen_script=[
            # keepalives only -> stall -> probe 1 carries a REJECTED-shape
            # approval (raw id present, no tool/command/description)
            FakeSseResponse(keepalive() * 4, end="eof", clock=clock, advance_per_line=60.0),
            # reconnect: the replay delivers the FULL approval, then stalls
            FakeSseResponse(
                _approval_request_frame(1, "appr-1", "ls -la /tmp")
                + keepalive() * 4,
                end="eof", clock=clock, advance_per_line=60.0,
            ),
            FakeSseResponse(
                sse_frame(0, {"event": "run.completed", "output": "done"}),
                end="eof",
            ),
        ],
        status_script=[
            {"status": "waiting_for_approval",
             "approval": {"approval_id": "appr-1", "tool": "", "command": "",
                          "description": ""}},
            {"status": "waiting_for_approval",
             "approval": _pending_approval_payload("appr-1", "ls -la /tmp")},
        ],
        extra_patches=[
            (api_config, "gateway_supports_approval_identity_v1", lambda *a, **k: True),
            (gc, "_settle_gateway_run_approval", fake_settle),
        ],
    )
    result = harness["result"]
    assert result[0] == "done", f"run must finalize, got {result!r}"
    approvals = [payload for name, payload in harness["events"] if name == "approval"]
    assert len(approvals) == 1, (
        f"the replayed approval must surface exactly once despite the "
        f"rejected status payload, got {approvals!r}")
    assert approvals[0]["approval_id"] == "appr-1", approvals[0]
    assert approvals[0]["command"] == "ls -la /tmp", approvals[0]


def test_r4_settle_flow_has_single_continue_branch():
    """Round-4 Greptile item 6: the duplicated dead ``if action == "continue":``
    block (a porting artifact — the first branch always continued) is gone and
    the settle flow is linear. The duplicate was unreachable, so no behavioral
    trace can show it; this pins the source structure instead."""
    import inspect
    src = inspect.getsource(gc._run_gateway_runs_api_streaming)
    count = src.count('action == "continue"')
    assert count == 1, (
        f"the settle flow must have exactly one continue branch, found {count}")


def test_r4_cancel_marker_scan_stops_at_turn_boundary():
    """Maintainer must-fix 2: with a PREVIOUS cancelled turn on the session,
    the cancel-marker backward scan must not cross the new user-turn boundary.
    The new partial must attach to the NEW cancel marker and the historical
    rows must stay untouched (pre-fix reload order: old user -> NEW partial ->
    old cancel -> new user -> new cancel)."""
    import shutil
    import tempfile
    import pathlib

    import api.models as models

    tmp = tempfile.mkdtemp(prefix="watchdog-harness-marker-")
    saved_dir, saved_index = models.SESSION_DIR, models.SESSION_INDEX_FILE
    saved_sessions = dict(models.SESSIONS)
    models.SESSION_DIR = pathlib.Path(tmp)
    models.SESSION_INDEX_FILE = pathlib.Path(tmp) / "_index.json"
    models.SESSIONS.clear()
    saved_partial = gc.STREAM_PARTIAL_TEXT.pop(STREAM_ID, None)
    saved_reasoning = gc.STREAM_REASONING_TEXT.pop(STREAM_ID, None)
    saved_tools = gc.STREAM_LIVE_TOOL_CALLS.pop(STREAM_ID, None)
    try:
        sid = "sess-watchdog-marker-boundary"
        session = models.Session(session_id=sid, title="Watchdog Harness", messages=[
            {"role": "user", "content": "first prompt", "timestamp": 1},
            {"role": "assistant", "content": "first partial answer", "_partial": True, "timestamp": 2},
            {"role": "assistant", "content": "**Task cancelled:** Cancelled by gateway.",
             "_error": True, "timestamp": 3},
        ])
        session.active_stream_id = STREAM_ID
        session.pending_user_message = "second prompt"
        session.pending_attachments = []
        session.pending_started_at = None
        session.save()
        models.SESSIONS[sid] = session

        gc.STREAM_PARTIAL_TEXT[STREAM_ID] = "second partial answer"
        gc.STREAM_REASONING_TEXT[STREAM_ID] = ""
        gc.STREAM_LIVE_TOOL_CALLS[STREAM_ID] = []
        gc._settle_gateway_cancelled_turn(sid, STREAM_ID)

        models.SESSIONS.pop(sid, None)
        reloaded = models.get_session(sid)
        rows = [m for m in reloaded.messages if isinstance(m, dict)]
        kinds = [
            "user" if m.get("role") == "user"
            else "partial" if m.get("_partial")
            else "marker" if m.get("_error")
            else "other"
            for m in rows
        ]
        assert kinds == ["user", "partial", "marker", "user", "partial", "marker"], (
            f"marker scan crossed the turn boundary, rows={kinds}")
        assert rows[1]["content"] == "first partial answer", rows[1]
        assert rows[3]["content"] == "second prompt", rows[3]
        assert rows[4]["content"] == "second partial answer", rows[4]
    finally:
        gc.STREAM_PARTIAL_TEXT.pop(STREAM_ID, None)
        gc.STREAM_REASONING_TEXT.pop(STREAM_ID, None)
        gc.STREAM_LIVE_TOOL_CALLS.pop(STREAM_ID, None)
        if saved_partial is not None:
            gc.STREAM_PARTIAL_TEXT[STREAM_ID] = saved_partial
        if saved_reasoning is not None:
            gc.STREAM_REASONING_TEXT[STREAM_ID] = saved_reasoning
        if saved_tools is not None:
            gc.STREAM_LIVE_TOOL_CALLS[STREAM_ID] = saved_tools
        models.SESSION_DIR, models.SESSION_INDEX_FILE = saved_dir, saved_index
        models.SESSIONS.clear()
        models.SESSIONS.update(saved_sessions)
        shutil.rmtree(tmp, ignore_errors=True)


def test_r4_status_lane_cancel_persists_before_cancel_event():
    """Maintainer should-fix: the status-lane cancel persists the cancelled
    turn BEFORE emitting the browser-facing cancel event. The event callback
    here plays the browser: at the moment the cancel event lands, a fresh
    reload from disk must already carry the streamed partial row."""
    import shutil
    import tempfile
    import pathlib

    import api.models as models

    tmp = tempfile.mkdtemp(prefix="watchdog-harness-persist-first-")
    saved_dir, saved_index = models.SESSION_DIR, models.SESSION_INDEX_FILE
    saved_sessions = dict(models.SESSIONS)
    models.SESSION_DIR = pathlib.Path(tmp)
    models.SESSION_INDEX_FILE = pathlib.Path(tmp) / "_index.json"
    models.SESSIONS.clear()
    sid = "sess-watchdog-persist-first"
    try:
        session = models.Session(session_id=sid, title="Watchdog Harness", messages=[])
        session.active_stream_id = STREAM_ID
        session.pending_user_message = "hi"
        session.pending_attachments = []
        session.pending_started_at = None
        session.save()
        models.SESSIONS[sid] = session

        clock = FakeClock()
        urlopen = ScriptedUrlopen()
        status = ScriptedStatus()
        # A real partial streams on the events connection, then the durable
        # status resolves cancelled: the persist-first settle must land before
        # the cancel event reaches this callback.
        urlopen.queue(FakeSseResponse(
            sse_frame(0, {"event": "message.delta", "delta": "partial answer so far"}),
            end="eof"))
        status.queue({"status": "cancelled"})

        saved = []

        def patch(obj, name, value):
            saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        refetch_at_event = []

        def on_event(name, payload):
            if name == "cancel":
                # The browser handler fetches the session as soon as the
                # cancel event lands: the sidecar must already carry it.
                models.SESSIONS.pop(sid, None)
                reloaded = models.get_session(sid)
                partials = [m for m in reloaded.messages
                            if isinstance(m, dict) and m.get("_partial")]
                refetch_at_event.append(
                    (len(partials), partials[0].get("content") if partials else None))

        patch(gc, "time", types.SimpleNamespace(monotonic=clock.monotonic))
        patch(gc, "_admit_gateway_run", lambda *a, **k: RUN_ID)
        patch(gc, "_get_gateway_run_status", status)
        patch(urllib.request, "urlopen", urlopen)
        import api.route_approvals as ra

        patch(ra, "settle_gateway_pending_run", lambda *a, **k: (0, None, 0))
        gc.STREAM_PARTIAL_TEXT[STREAM_ID] = ""
        gc.STREAM_REASONING_TEXT.pop(STREAM_ID, None)
        gc.STREAM_LIVE_TOOL_CALLS.pop(STREAM_ID, None)
        try:
            result = gc._run_gateway_runs_api_streaming(
                sid, "hi", "test-model", "/tmp", STREAM_ID,
                BASE_URL, "test-key", [], {},
                put_gateway_event=on_event,
                cancel_event=FakeCancelEvent(clock),
            )
        finally:
            for obj, name, value in reversed(saved):
                setattr(obj, name, value)
            gc.STREAM_PARTIAL_TEXT.pop(STREAM_ID, None)

        assert result[0] is None, result
        assert refetch_at_event and refetch_at_event[0] == (1, "partial answer so far"), (
            f"cancel event raced the persistence, refetch={refetch_at_event!r}")
        # And the sidecar settles exactly once (one partial, one marker).
        models.SESSIONS.pop(sid, None)
        reloaded = models.get_session(sid)
        assistant = [m for m in reloaded.messages
                     if isinstance(m, dict) and m.get("role") == "assistant"]
        partials = [m for m in assistant if m.get("_partial")]
        markers = [m for m in assistant if m.get("_error")]
        assert len(partials) == 1 and partials[0].get("content") == "partial answer so far", partials
        assert len(markers) == 1, markers
    finally:
        gc.STREAM_PARTIAL_TEXT.pop(STREAM_ID, None)
        gc.STREAM_REASONING_TEXT.pop(STREAM_ID, None)
        gc.STREAM_LIVE_TOOL_CALLS.pop(STREAM_ID, None)
        models.SESSION_DIR, models.SESSION_INDEX_FILE = saved_dir, saved_index
        models.SESSIONS.clear()
        models.SESSIONS.update(saved_sessions)
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- Fix 2 -----


def test_status_503_during_stream_does_not_kill_live_run():
    """Fix 2: status 503s while the run is alive and streaming -> the probes
    retry inside the reattach budget, the events stream reconnects with the
    cursor between attempts, and the later run.completed frame completes the
    turn with the full answer. Three consecutive 503s is exactly the case the
    old 3-probe budget killed; the reattach budget (150) survives it."""
    harness = run_turn(
        urlopen_script=[
            FakeSseResponse(
                keepalive() * 3
                + sse_frame(0, {"event": "message.delta", "delta": "Hel"}),
                end="eof",
            ),
            FakeSseResponse(keepalive() * 2, end="eof"),
            FakeSseResponse(keepalive() * 2, end="eof"),
            FakeSseResponse(
                keepalive() * 2
                + sse_frame(1, {"event": "message.delta", "delta": "lo world"})
                + sse_frame(2, {"event": "run.completed", "output": "Hello world"}),
                end="eof",
            ),
        ],
        status_script=[
            _http_error(503),                # probe 1: transient
            _http_error(503),                # probe 2: still transient
            _http_error(503),                # probe 3: exhausted the OLD budget
        ],
    )

    result = harness["result"]
    assert result[0] == "Hello world", f"turn must complete with the full answer, got {result!r}"
    # Exactly the three transient probes happened; the old 3-probe budget
    # would have raised here, the reattach budget (150) is nowhere near done.
    assert harness["status"].calls == 3
    assert harness["status"].calls < GATEWAY_REATTACH_MAX_POLL_FAILURES
    # Each reconnect between probes carried the Last-Event-ID cursor.
    assert harness["urlopen"].header(1, "Last-Event-ID") == "0"
    assert harness["urlopen"].header(2, "Last-Event-ID") == "0"
    assert harness["urlopen"].header(3, "Last-Event-ID") == "0"


def test_stop_is_honoured_between_probe_retries():
    """Fix 2: cancel_event set during a probe-failure wait surfaces the user
    Stop immediately instead of retrying the budget."""
    clock = FakeClock()
    stop = StopDuringWaitEvent(clock)
    urlopen = ScriptedUrlopen()
    status = ScriptedStatus()
    for outcome in [partial_delta("Hel", seq=0)]:
        urlopen.queue(outcome)
    status.queue(_http_error(503))

    saved = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    events = []
    patch(gc, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    patch(gc, "_admit_gateway_run", lambda *a, **k: RUN_ID)
    patch(gc, "_get_gateway_run_status", status)
    patch(urllib.request, "urlopen", urlopen)

    import api.route_approvals as ra

    patch(ra, "settle_gateway_pending_run", lambda *a, **k: (0, None, 0))
    gc.STREAM_PARTIAL_TEXT[STREAM_ID] = ""
    try:
        result = gc._run_gateway_runs_api_streaming(
            "sess-watchdog-harness", "hi", "test-model", "/tmp", STREAM_ID,
            BASE_URL, "test-key", [], {},
            put_gateway_event=lambda name, payload: events.append((name, payload)),
            cancel_event=stop,
        )
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)
        gc.STREAM_PARTIAL_TEXT.pop(STREAM_ID, None)

    assert result[0] is None, "Stop must cancel the turn, not retry the budget"
    assert any(name == "cancel" for name, _ in events), f"events={events!r}"
    assert status.calls == 1, "no further probes after Stop"


def test_probe_budget_exhaustion_transient_errors_only():
    """Fix 2 give-up condition: the reattach budget (150 consecutive probe
    failures) applies to TRANSIENT failures only — a 404 status is exempt and
    terminal after the small grace (pinned by the round-2 tests above), so
    the long budget cannot be spent on a lost run."""
    # Transient: 150 consecutive 503s -> "unreachable" RuntimeError; every
    # events reconnect in between also 404s (gateway is unhealthy).
    harness = run_turn(
        urlopen_script=[partial_delta("Hel", seq=0)]
        + [_http_error(404)] * (GATEWAY_REATTACH_MAX_POLL_FAILURES - 1),
        status_script=[_http_error(503)] * GATEWAY_REATTACH_MAX_POLL_FAILURES,
    )
    raised = harness["result"]
    assert isinstance(raised, RuntimeError), f"expected give-up, got {raised!r}"
    assert "unreachable" in str(raised)
    assert harness["status"].calls == GATEWAY_REATTACH_MAX_POLL_FAILURES
    # The fake clock scaled the budget: one poll interval per failed probe.
    assert harness["clock"].now >= (
        GATEWAY_REATTACH_MAX_POLL_FAILURES - 1) * GATEWAY_REATTACH_POLL_INTERVAL


# --------------------------------------------------------------- Fix 3 -----


def test_completed_status_output_preferred_over_streamed_text():
    """Fix 3: a terminal completed status with a NON-EMPTY output outranks the
    (truncated) streamed final_text, and STREAM_PARTIAL_TEXT is overwritten so
    the UI writeback matches."""
    # Status-poll settle path.
    harness = run_turn(
        urlopen_script=[
            FakeSseResponse(sse_frame(0, {"event": "message.delta", "delta": "Hello"}), end="eof"),
        ],
        status_script=[
            {"status": "completed", "output": "Hello world"},
        ],
    )
    result = harness["result"]
    assert result[0] == "Hello world", f"status output must win, got {result!r}"

    # run.completed SSE frame settle path (the other Fix 3 apply site).
    harness = run_turn(
        urlopen_script=[
            FakeSseResponse(
                sse_frame(0, {"event": "message.delta", "delta": "Hello"})
                + sse_frame(1, {"event": "run.completed", "output": "Hello world"}),
                end="eof",
            ),
        ],
        status_script=[],
    )
    result = harness["result"]
    assert result[0] == "Hello world", f"frame output must win, got {result!r}"


def test_completed_status_stream_writeback_overwrites_partial_text():
    """Fix 3 writeback arm: STREAM_PARTIAL_TEXT ends at the adopted status
    output, not the stale streamed prefix."""
    import api.route_approvals as ra

    clock = FakeClock()
    urlopen = ScriptedUrlopen()
    status = ScriptedStatus()
    urlopen.queue(FakeSseResponse(
        sse_frame(0, {"event": "message.delta", "delta": "Hello"}), end="eof"))
    status.queue({"status": "completed", "output": "Hello world"})

    saved = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    patch(gc, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    patch(gc, "_admit_gateway_run", lambda *a, **k: RUN_ID)
    patch(gc, "_get_gateway_run_status", status)
    patch(urllib.request, "urlopen", urlopen)
    patch(ra, "settle_gateway_pending_run", lambda *a, **k: (0, None, 0))
    gc.STREAM_PARTIAL_TEXT[STREAM_ID] = ""
    try:
        result = gc._run_gateway_runs_api_streaming(
            "sess-watchdog-harness", "hi", "test-model", "/tmp", STREAM_ID,
            BASE_URL, "test-key", [], {},
            put_gateway_event=lambda name, payload: None,
            cancel_event=FakeCancelEvent(clock),
        )
        writeback = gc.STREAM_PARTIAL_TEXT.get(STREAM_ID)
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)
        gc.STREAM_PARTIAL_TEXT.pop(STREAM_ID, None)

    assert result[0] == "Hello world"
    assert writeback == "Hello world", f"UI writeback must match the adopted output, got {writeback!r}"


def test_completed_status_empty_output_keeps_streamed_text():
    """Fix 3 keep-today arm: an EMPTY status output leaves the streamed text
    as the turn result."""
    harness = run_turn(
        urlopen_script=[
            FakeSseResponse(sse_frame(0, {"event": "message.delta", "delta": "Hello"}), end="eof"),
        ],
        status_script=[
            {"status": "completed", "output": ""},
        ],
    )
    result = harness["result"]
    assert result[0] == "Hello", f"streamed text must be kept, got {result!r}"


# -------------------------- round-3 review: status-lane cancel persistence ----


def _assistant_rows(messages):
    return [m for m in (messages or []) if isinstance(m, dict) and m.get("role") == "assistant"]


def test_status_lane_cancel_persists_partial_reasoning_and_tool_buffers():
    """Round-3 item 2: when the watchdog's durable status resolves
    cancelled, the turn reaches the cancelled-turn settle WITHOUT the browser
    Stop path ever running — so the partial answer, reasoning trace, and live
    tool buffers must be persisted into the session BEFORE teardown clears
    them. Asserted on the persisted sidecar (fresh reload from disk)."""
    import shutil
    import tempfile
    import pathlib

    import api.models as models

    tmp = tempfile.mkdtemp(prefix="watchdog-harness-sessions-")
    saved_dir, saved_index = models.SESSION_DIR, models.SESSION_INDEX_FILE
    saved_sessions = dict(models.SESSIONS)
    models.SESSION_DIR = pathlib.Path(tmp)
    models.SESSION_INDEX_FILE = pathlib.Path(tmp) / "_index.json"
    models.SESSIONS.clear()
    sid = "sess-watchdog-cancel-persist"
    saved_reasoning = gc.STREAM_REASONING_TEXT.pop(STREAM_ID, None)
    saved_tools = gc.STREAM_LIVE_TOOL_CALLS.pop(STREAM_ID, None)
    try:
        session = models.Session(session_id=sid, title="Watchdog Harness", messages=[])
        session.active_stream_id = STREAM_ID
        session.pending_user_message = "hi"
        session.pending_attachments = []
        session.pending_started_at = None
        session.save()
        models.SESSIONS[sid] = session

        # Buffers as the worker holds them mid-turn: run_turn() resets the
        # shared partial buffer around the run (as the worker's setup does),
        # so the streamed partial is set the way the worker would have it
        # after the earlier connection — before the settle, after the run.
        gc.STREAM_PARTIAL_TEXT[STREAM_ID] = ""
        gc.STREAM_REASONING_TEXT[STREAM_ID] = "thinking hard"
        gc.STREAM_LIVE_TOOL_CALLS[STREAM_ID] = [
            {"name": "shell", "args": {"cmd": "ls"}, "done": False}]

        harness = run_turn(
            urlopen_script=[FakeSseResponse([b": ping", b""], end="eof")],
            status_script=[{"status": "cancelled"}],
        )
        assert harness["result"][0] is None, harness["result"]
        # A prior connection streamed a partial answer before the socket
        # dropped; the durable status then resolved cancelled.
        gc.STREAM_PARTIAL_TEXT[STREAM_ID] = "partial answer so far"
        # The production settle is invoked at TWO sites: inside the streaming
        # function's status-lane cancel branch (persist-first, round-4) and
        # at the worker caller (`if final_text is None: ...`) for the other
        # cancel paths; the second call no-ops on the cleared ownership.
        # This test drives the canonical settle directly, as the caller site
        # does, with the worker's own buffers still populated.
        gc._settle_gateway_cancelled_turn(sid, STREAM_ID)

        # Fresh reload from disk: the sidecar must carry the partial.
        models.SESSIONS.pop(sid, None)
        reloaded = models.get_session(sid)
        assistant = _assistant_rows(reloaded.messages)
        partials = [m for m in assistant if m.get("_partial")]
        assert len(partials) == 1, f"exactly one partial row expected, got {assistant!r}"
        partial = partials[0]
        assert partial.get("content") == "partial answer so far", partial
        assert partial.get("reasoning") == "thinking hard", partial
        assert partial.get("_partial_tool_calls") == [
            {"name": "shell", "args": {"cmd": "ls"}, "done": False}], partial
        assert not partial.get("_error"), partial
        # The user prompt and a cancel marker are present too, in order:
        # user -> partial -> cancel marker.
        assert any(
            isinstance(m, dict) and m.get("role") == "user" and m.get("content") == "hi"
            for m in reloaded.messages)
        markers = [m for m in assistant if m.get("_error")]
        assert len(markers) == 1, f"exactly one cancel marker expected, got {assistant!r}"
        assert assistant.index(partial) < assistant.index(markers[0])
        # Same persistence for the interrupted terminal state: a fresh turn
        # admitted on the session (settle 1 cleared ownership, as cancel
        # does), a new partial streamed, then the interrupted resolution.
        reloaded.active_stream_id = STREAM_ID
        reloaded.pending_user_message = "hi again"
        reloaded.pending_attachments = []
        reloaded.pending_started_at = None
        reloaded.save()
        models.SESSIONS[sid] = reloaded
        harness = run_turn(
            urlopen_script=[FakeSseResponse([b": ping", b""], end="eof")],
            status_script=[{"status": "interrupted"}],
        )
        assert harness["result"][0] is None, harness["result"]
        gc.STREAM_PARTIAL_TEXT[STREAM_ID] = "interrupted partial"
        gc.STREAM_REASONING_TEXT[STREAM_ID] = ""
        gc.STREAM_LIVE_TOOL_CALLS[STREAM_ID] = []
        gc._settle_gateway_cancelled_turn(sid, STREAM_ID)
        models.SESSIONS.pop(sid, None)
        reloaded = models.get_session(sid)
        partials = [m for m in _assistant_rows(reloaded.messages) if m.get("_partial")]
        assert len(partials) == 2, f"interrupted lane must persist too, got {partials!r}"
        assert partials[1].get("content") == "interrupted partial"
    finally:
        gc.STREAM_PARTIAL_TEXT.pop(STREAM_ID, None)
        gc.STREAM_REASONING_TEXT.pop(STREAM_ID, None)
        gc.STREAM_LIVE_TOOL_CALLS.pop(STREAM_ID, None)
        if saved_reasoning is not None:
            gc.STREAM_REASONING_TEXT[STREAM_ID] = saved_reasoning
        if saved_tools is not None:
            gc.STREAM_LIVE_TOOL_CALLS[STREAM_ID] = saved_tools
        models.SESSION_DIR, models.SESSION_INDEX_FILE = saved_dir, saved_index
        models.SESSIONS.clear()
        models.SESSIONS.update(saved_sessions)
        shutil.rmtree(tmp, ignore_errors=True)


def test_status_lane_cancel_successor_owner_and_empty_output_controls():
    """Round-3 item 2 controls: (a) successor-owner — when the stream/turn
    ownership moved to a successor (active_stream_id no longer ours), the
    settle must not write over the successor's state; (b) empty-output —
    with nothing buffered, no empty partial rows are appended and the settle
    does not crash."""
    import shutil
    import tempfile
    import pathlib

    import api.models as models

    tmp = tempfile.mkdtemp(prefix="watchdog-harness-sessions-")
    saved_dir, saved_index = models.SESSION_DIR, models.SESSION_INDEX_FILE
    saved_sessions = dict(models.SESSIONS)
    models.SESSION_DIR = pathlib.Path(tmp)
    models.SESSION_INDEX_FILE = pathlib.Path(tmp) / "_index.json"
    models.SESSIONS.clear()
    saved_reasoning = gc.STREAM_REASONING_TEXT.pop(STREAM_ID, None)
    saved_tools = gc.STREAM_LIVE_TOOL_CALLS.pop(STREAM_ID, None)
    try:
        # (a) successor owner: the session moved on to another stream.
        sid = "sess-watchdog-cancel-successor"
        session = models.Session(session_id=sid, title="Watchdog Harness", messages=[
            {"role": "user", "content": "hi", "timestamp": 1},
            {"role": "assistant", "content": "successor answer", "timestamp": 2},
        ])
        session.active_stream_id = "successor-stream-id"  # NOT ours
        session.pending_user_message = None
        session.save()
        models.SESSIONS[sid] = session
        gc.STREAM_PARTIAL_TEXT[STREAM_ID] = "stale partial of the old turn"
        gc.STREAM_REASONING_TEXT[STREAM_ID] = "stale reasoning"
        gc.STREAM_LIVE_TOOL_CALLS[STREAM_ID] = [{"name": "shell", "args": {}, "done": False}]
        harness = run_turn(
            urlopen_script=[FakeSseResponse([b": ping", b""], end="eof")],
            status_script=[{"status": "cancelled"}],
        )
        assert harness["result"][0] is None
        gc._settle_gateway_cancelled_turn(sid, STREAM_ID)  # must no-op
        models.SESSIONS.pop(sid, None)
        reloaded = models.get_session(sid)
        assert [m.get("content") for m in reloaded.messages] == ["hi", "successor answer"], (
            f"successor state must be untouched, got {reloaded.messages!r}")

        # (b) empty output: nothing buffered -> no empty rows, no crash.
        sid = "sess-watchdog-cancel-empty"
        session = models.Session(session_id=sid, title="Watchdog Harness", messages=[])
        session.active_stream_id = STREAM_ID
        session.pending_user_message = "hi"
        session.pending_attachments = []
        session.pending_started_at = None
        session.save()
        models.SESSIONS[sid] = session
        gc.STREAM_PARTIAL_TEXT[STREAM_ID] = ""
        gc.STREAM_REASONING_TEXT[STREAM_ID] = ""
        gc.STREAM_LIVE_TOOL_CALLS[STREAM_ID] = []
        harness = run_turn(
            urlopen_script=[FakeSseResponse([b": ping", b""], end="eof")],
            status_script=[{"status": "cancelled"}],
        )
        assert harness["result"][0] is None
        gc._settle_gateway_cancelled_turn(sid, STREAM_ID)
        models.SESSIONS.pop(sid, None)
        reloaded = models.get_session(sid)
        assistant = _assistant_rows(reloaded.messages)
        assert not [m for m in assistant if m.get("_partial")], (
            f"no empty partial rows allowed, got {assistant!r}")
        assert len(assistant) == 1 and assistant[0].get("_error"), assistant
        assert any(
            isinstance(m, dict) and m.get("role") == "user" and m.get("content") == "hi"
            for m in reloaded.messages)
    finally:
        gc.STREAM_PARTIAL_TEXT.pop(STREAM_ID, None)
        gc.STREAM_REASONING_TEXT.pop(STREAM_ID, None)
        gc.STREAM_LIVE_TOOL_CALLS.pop(STREAM_ID, None)
        if saved_reasoning is not None:
            gc.STREAM_REASONING_TEXT[STREAM_ID] = saved_reasoning
        if saved_tools is not None:
            gc.STREAM_LIVE_TOOL_CALLS[STREAM_ID] = saved_tools
        models.SESSION_DIR, models.SESSION_INDEX_FILE = saved_dir, saved_index
        models.SESSIONS.clear()
        models.SESSIONS.update(saved_sessions)
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ main ---


def test_r7_observer_on_seq_still_advances_reconnect_cursor():
    """Round-5 review (maintainer + Greptile, same finding): supplying the
    ``on_seq`` observer used to REPLACE the internal cursor updater, so a
    reconnect after a mid-stream reset omitted ``Last-Event-ID`` and a
    cursor-aware gateway replayed already-delivered tokens into the answer
    (``HelloHello``, observer saw ``[5, 5, 6]``). The observer must be
    NOTIFIED, not substituted: observer-on and observer-off both reconnect
    with the cursor, emit the text once, and the observer still records
    every committed seq. RED pre-fix: the second connect carried no
    ``Last-Event-ID`` and the answer duplicated the first delta."""
    committed = []
    harness = run_turn(
        urlopen_script=[
            partial_delta("Hello", seq=5),   # partial text, then socket reset
            FakeSseResponse(
                sse_frame(5, {"event": "message.delta", "delta": "Hello"})
                + sse_frame(6, {"event": "run.completed", "output": "Hello"}),
                end="eof"),
        ],
        status_script=[
            {"status": "running"},           # probe right after the reset
        ],
        on_seq=committed.append,
    )
    result = harness["result"]
    assert result[0] == "Hello", f"answer must be the terminal output once, got {result!r}"
    # The reconnect MUST carry the cursor even with the observer present.
    assert harness["urlopen"].header(1, "Last-Event-ID") == "5", (
        f"observer-on reconnect lost the cursor, headers={harness['urlopen'].requests[1].headers!r}")
    # The observer saw every committed seq exactly once (no replayed 5).
    assert committed == [5, 5, 6], f"observer seqs must not duplicate, got {committed!r}"
    deltas = [p for name, p in harness["events"]
              if name == "token" and isinstance(p, dict) and "text" in p]
    assert len(deltas) == 2, f"exactly one replayed delta expected, got {deltas!r}"


def test_r7_observer_off_reconnect_cursor_unchanged():
    """Observer-off control: the default path still reconnects with
    Last-Event-ID (regression guard while composing the hook)."""
    harness = run_turn(
        urlopen_script=[
            partial_delta("Hello", seq=5),
            FakeSseResponse(
                sse_frame(6, {"event": "run.completed", "output": "Hello"}),
                end="eof"),
        ],
        status_script=[
            {"status": "running"},
        ],
    )
    assert harness["result"][0] == "Hello"
    assert harness["urlopen"].header(1, "Last-Event-ID") == "5"


def test_r8_clean_eof_reconnect_backoff_bounds_reconnect_rate():
    """Round-6 shipping gate (maintainer, release PR #8030): a gateway or
    proxy that accepts the events connection and closes it cleanly every
    ~20ms used to hot-loop this thread — 3,000 reconnects / 3,001 status
    probes in 60s with zero waits (his virtual-clock witness). Consecutive
    clean-EOF reconnects now back off (0.5s doubling to a 30s cap), the
    backoff RESETS when real progress arrives (a data frame committing a
    new seq), and Stop during a backoff wait still cancels promptly.
    RED pre-fix: the 60s script burned ~all 16 connect slots with ~16
    probes and zero clock time."""
    clock = FakeClock()
    # 16 clean-EOF closes (each ~20ms of virtual connection time), then a
    # terminal frame so the turn finishes inside the script budget.
    script = [
        FakeSseResponse(keepalive() * 2, end="eof", clock=clock, advance_per_line=0.01)
        for _ in range(16)
    ]
    script.append(FakeSseResponse(
        sse_frame(1, {"event": "run.completed", "output": "finally"}), end="eof"))
    harness = run_turn(
        clock=clock,
        urlopen_script=script,
        status_script=[{"status": "running"}] * 17,
    )
    result = harness["result"]
    assert result[0] == "finally", f"turn must complete from the terminal frame, got {result!r}"
    urlopen_calls = len(harness["urlopen"].requests)
    probe_calls = harness["status"].calls
    # Bounded rate: with the backoff, the 16 clean-EOF connects cost
    # 0.5+1+2+4+8+16+30*10 ≈ 331.5 virtual seconds (his unpatched witness:
    # the same script burned 60s of connects with ZERO waits). Assert the
    # waits happened and the doubling reached the cap.
    assert clock.now >= 300.0, (
        f"clean-EOF backoff must pace the reconnects (elapsed {clock.now}s, "
        f"connects={urlopen_calls}, probes={probe_calls})")
    assert urlopen_calls == 17, f"script consumed exactly: connects={urlopen_calls}"
    # The waits scaled: probe gaps climb 0.5, 1, 2, 4, 8, 16 then sit at
    # the 30s cap — doubling engaged, not a flat sleep.
    gaps = [t for (kind, t) in harness["trace"] if kind == "status"]
    deltas = [round(b - a, 2) for a, b in zip(gaps, gaps[1:], strict=False)]
    # The +0.04 per gap is the virtual connection time of each clean-EOF
    # script entry (2 keepalive lines x 0.02s).
    assert deltas[:6] == [0.54, 1.04, 2.04, 4.04, 8.04, 16.04], f"doubling gaps, got {deltas!r}"
    assert all(g == 30.04 for g in deltas[6:]), f"cap engaged at 30s, got {deltas!r}"


def test_r8_real_progress_resets_clean_eof_backoff():
    """Round-7 review fix: the backoff reset must key on per-connection
    progress, not cumulative turn text. Two clean EOFs -> a data frame
    connection -> TWO more clean EOFs: the reset must persist across both
    (gaps [0.5, 1.0, 0.5, 1.0]). RED pre-fix: after the data frame the
    relay's seeded text made every later clean EOF reset too, so gaps ran
    [0.5, 1.0, 0.5, 0.5, ...] flat forever."""
    clock = FakeClock()
    harness = run_turn(
        clock=clock,
        urlopen_script=[
            FakeSseResponse(b"", end="eof"),                      # clean EOF 1
            FakeSseResponse(b"", end="eof"),                      # clean EOF 2
            # clean EOF 3 carrying a DATA frame: real progress
            FakeSseResponse(sse_frame(2, {"event": "message.delta", "delta": "Hello"}), end="eof"),
            FakeSseResponse(b"", end="eof"),                      # clean EOF 4
            FakeSseResponse(b"", end="eof"),                      # clean EOF 5
            FakeSseResponse(
                sse_frame(3, {"event": "run.completed", "output": "Hello"}), end="eof"),
        ],
        status_script=[{"status": "running"}] * 6,
    )
    assert harness["result"][0] == "Hello"
    probes = [t for (kind, t) in harness["trace"] if kind == "status"]
    gaps = [round(b - a, 2) for a, b in zip(probes, probes[1:], strict=False)]
    assert gaps == [0.5, 1.0, 0.5, 1.0], (
        f"reset must apply per-connection (gaps {gaps!r})")


def test_r8_reasoning_only_connection_resets_backoff():
    """A connection that delivers only a reasoning frame (new seq, no
    answer text) is REAL progress and must reset the backoff (round-7
    review converse case: accepted frames can carry no answer text)."""
    clock = FakeClock()
    harness = run_turn(
        clock=clock,
        urlopen_script=[
            FakeSseResponse(b"", end="eof"),                      # clean EOF 1
            FakeSseResponse(b"", end="eof"),                      # clean EOF 2
            FakeSseResponse(b"", end="eof"),                      # clean EOF 3
            # clean EOF 4 carrying ONLY a reasoning frame
            FakeSseResponse(sse_frame(1, {"event": "reasoning.available", "text": "thinking"}), end="eof"),
            FakeSseResponse(b"", end="eof"),                      # clean EOF 5
            FakeSseResponse(
                sse_frame(2, {"event": "run.completed", "output": "done"}), end="eof"),
        ],
        status_script=[{"status": "running"}] * 6,
    )
    assert harness["result"][0] == "done"
    probes = [t for (kind, t) in harness["trace"] if kind == "status"]
    gaps = [round(b - a, 2) for a, b in zip(probes, probes[1:], strict=False)]
    assert gaps == [0.5, 1.0, 2.0, 0.5], (
        f"reasoning-only connection must reset the backoff (gaps {gaps!r})")


def test_r8_stop_during_clean_eof_backoff_cancels_promptly():
    """Stop landing during the FIRST backoff wait must cancel inside that
    wait: no second connect, no status probe after the wait, and the
    cancel event surfaces."""
    clock = FakeClock()
    harness = run_turn(
        clock=clock,
        cancel_event=StopDuringWaitEvent(clock),
        urlopen_script=[
            FakeSseResponse(b"", end="eof"),
        ],
        status_script=[{"status": "running"}],
    )
    assert harness["result"][0] is None
    cancels = [e for e in harness["events"] if e[0] == "cancel"]
    assert cancels, "cancellation must be surfaced"
    assert len(harness["urlopen"].requests) == 1, (
        "Stop during the backoff must prevent the second connect")
    assert len(harness["trace"]) == 2, (
        f"no status probe may follow the cancelled wait, trace={harness['trace']!r}")


def test_r8_keepalive_stall_does_not_feed_clean_eof_backoff():
    """A keepalive-only STALL is a live connection the watchdog closed after
    ~120s of liveness, not a clean EOF the gateway slammed shut: with the run
    still running it must reconnect without accumulating the clean-EOF
    backoff (a long silent tool call would otherwise wait up to 30s per
    reconnect for events the gateway is already holding). The wait after
    each stall stays at the base 0.5s. RED on 4f90c606: [0.5, 1.0, 2.0, 4.0]."""
    clock = FakeClock()

    def stall():
        # 16 comment/blank lines x 10s: the 120s watchdog trips on the 14th.
        return FakeSseResponse(keepalive() * 8, end="eof", clock=clock, advance_per_line=10.0)

    script = [stall() for _ in range(4)]
    script.append(FakeSseResponse(
        sse_frame(0, {"event": "run.completed", "output": "done"}), end="eof"))
    harness = run_turn(
        clock=clock, urlopen_script=script, status_script=[{"status": "running"}] * 5)
    assert harness["result"][0] == "done"
    probes = [t for (kind, t) in harness["trace"] if kind == "status"]
    connects = [t for (kind, t) in harness["trace"] if kind == "events"]
    assert len(probes) == 4 and len(connects) == 5
    waits = [round(connects[i + 1] - probes[i], 2) for i in range(4)]
    assert waits == [0.5, 0.5, 0.5, 0.5], (
        f"a watchdog stall must not accumulate the clean-EOF backoff (waits {waits!r})")


def test_r8_overflow_bounded_exponent_survives_long_clean_eof_runs():
    """Round-7 review CORE: unbounded, `0.5 * (2 ** (n - 1))` overflows
    float conversion at reconnect ~1,025 (~8.5h into a stuck turn) and the
    OverflowError escapes the streaming function, ending the turn. The
    exponent must be bounded BEFORE multiplying; the turn must survive
    past 1,100 consecutive clean-EOF reconnects (virtual clock) and still
    complete when the gateway finally delivers the terminal frame."""
    clock = FakeClock()
    script = [FakeSseResponse(b"", end="eof") for _ in range(1100)]
    script.append(FakeSseResponse(
        sse_frame(1, {"event": "run.completed", "output": "survived"}), end="eof"))
    harness = run_turn(
        clock=clock,
        urlopen_script=script,
        status_script=[{"status": "running"}] * 1101,
    )
    assert harness["result"][0] == "survived", f"turn must survive the long EOF run, got {harness['result']!r}"
    assert len(harness["urlopen"].requests) == 1101
    # With the exponent bounded at 6 (x64), every backoff after the 7th
    # reconnect sits at the 30s cap: 6 * 0.5 + 6 * 1 + ... capped sum.
    elapsed_floor = 0.5 + 1.0 + 2.0 + 4.0 + 8.0 + 16.0 + (1101 - 7) * 30.0
    assert clock.now >= elapsed_floor, (
        f"capped backoffs must accumulate (elapsed {clock.now}s < {elapsed_floor}s)")


def main():
    tests = [
        test_events_404_after_partial_stream_interrupted_status_does_not_settle_partial_text,
        test_events_404_then_status_404_fails_closed_within_two_probes,
        test_bare_status_404_grace_second_consecutive_404_fails_closed,
        test_status_404_grace_does_not_false_positive_on_live_run,
        test_status_404_streak_requires_consecutive_404s,
        test_keepalive_only_stream_then_status_404_x2_probes_back_to_back,
        test_comment_only_stream_then_status_404_x2_probes_back_to_back,
        test_reset_events404_then_status404_x2_fails_closed_promptly,
        test_stop_between_grace_probes_surfaces_cancel_without_reconnect,
        test_status_lane_cancel_persists_partial_reasoning_and_tool_buffers,
        test_status_lane_cancel_successor_owner_and_empty_output_controls,
        test_status_503_during_stream_does_not_kill_live_run,
        test_stop_is_honoured_between_probe_retries,
        test_probe_budget_exhaustion_transient_errors_only,
        test_completed_status_output_preferred_over_streamed_text,
        test_completed_status_stream_writeback_overwrites_partial_text,
        test_completed_status_empty_output_keeps_streamed_text,
        test_r4_transport_reset_completed_empty_output_returns_partial_text,
        test_r4_run_completed_frame_latches_terminal_despite_keepalives,
        test_r4_byte_silent_stream_probes_status_within_watchdog_budget,
        test_r4_stop_during_byte_silence_cancels_within_budget,
        test_r4_status_waiting_for_approval_surfaces_approval_card_once,
        test_r4_settle_flow_has_single_continue_branch,
        test_r4_cancel_marker_scan_stops_at_turn_boundary,
        test_r4_status_lane_cancel_persists_before_cancel_event,
        test_r5_run_completed_breaks_out_of_infinite_postterminal_keepalives,
        test_r5_event_header_framed_terminal_frame_breaks_out_of_keepalives,
        test_r5_socket_reset_after_terminal_frame_returns_immediately,
        test_r5_postterminal_delta_frame_ignored,
        test_r6_status_approval_mirror_skipped_without_identity_fifo_replay_orders_a_b,
        test_r6_identity_gateway_status_card_from_status_deduped_against_replay,
        test_r6_rejected_status_approval_payload_does_not_mask_replay,
        test_r7_observer_on_seq_still_advances_reconnect_cursor,
        test_r7_observer_off_reconnect_cursor_unchanged,
        test_r8_clean_eof_reconnect_backoff_bounds_reconnect_rate,
        test_r8_real_progress_resets_clean_eof_backoff,
        test_r8_reasoning_only_connection_resets_backoff,
        test_r8_stop_during_clean_eof_backoff_cancels_promptly,
        test_r8_overflow_bounded_exponent_survives_long_clean_eof_runs,
        test_r8_keepalive_stall_does_not_feed_clean_eof_backoff,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - harness reporter
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {test.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
