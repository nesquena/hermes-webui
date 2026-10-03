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

Everything else (SSE parsing, Last-Event-ID replay skip, terminal handling,
STREAM_PARTIAL_TEXT writeback) runs for real.

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

  4. socket reset after a partial delta -> events reconnect 404 -> status 404
      -> the turn fails closed within <= 2 status probes and ZERO
      poll-interval waits (a lost run must not spin the turn for the full
      ~298s reattach budget); the same rule holds for a bare status 404.
  5. the 404 grace is small and non-eager: a single 404 followed by
      ``running`` still lets the run complete, and a 404 interrupted by a
      503 restarts the streak (only CONSECUTIVE 404s are terminal) while
      the 503 alone keeps spending the long budget.

Runs under pytest on supported interpreters and standalone on Python 3.14
(``python3 tests/test_gateway_events_watchdog_7978.py``), where the repo
suite's conftest gate refuses to run.
"""

from __future__ import annotations

import json
import logging
import os
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
    """SSE connection fake: iterates byte lines, optionally resetting mid-stream."""

    def __init__(self, lines, end="eof"):
        self.lines = list(lines)
        self.end = end

    def __iter__(self):
        return self._gen()

    def _gen(self):
        for line in self.lines:
            yield line
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
    lines.append(b"data: " + json.dumps(payload).encode())
    lines.append(b"")
    return lines


def keepalive():
    return [b": keepalive", b""]


class ScriptedUrlopen:
    def __init__(self):
        self.requests = []
        self.script = []

    def queue(self, outcome):
        self.script.append(outcome)

    def __call__(self, req, timeout=None):
        self.requests.append(req)
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


def run_turn(*, urlopen_script=(), status_script=(), cancel_event=None):
    """Run the real streaming function against the fakes; restore all patches."""
    clock = FakeClock()
    urlopen = ScriptedUrlopen()
    status = ScriptedStatus()
    events = []
    for outcome in urlopen_script:
        urlopen.queue(outcome)
    for outcome in status_script:
        status.queue(outcome)

    saved = []
    settles = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    patch(gc, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    patch(gc, "_admit_gateway_run", lambda *a, **k: RUN_ID)
    patch(gc, "_get_gateway_run_status", status)
    patch(urllib.request, "urlopen", urlopen)

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
    """Round-2 (maintainer-timed scenario): socket reset after a partial
    delta -> events reconnect 404 -> durable status 404 -> the turn must fail
    closed within <= 2 status probes and ZERO poll-interval waits, not spin
    the full ~298s reattach budget. Asserted on fake-clock steps, not just
    the outcome."""
    harness = run_turn(
        urlopen_script=[
            partial_delta("Hello", seq=0),   # partial text, then socket reset
            _http_error(404),                # reconnect: gateway has no run
            _http_error(404),                # grace re-probe's reconnect: still no run
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
    # The grace re-probe still reconnected events with the Last-Event-ID cursor.
    assert harness["urlopen"].header(1, "Last-Event-ID") == "0"


def test_bare_status_404_grace_second_consecutive_404_fails_closed():
    """Round-2: a status 404 is terminal after the small grace even without an
    events failure in the picture: first 404 -> one immediate re-probe (no
    sleep), second consecutive 404 -> fail closed. Pre-fix this scenario
    spent the full 150-probe budget."""
    harness = run_turn(
        urlopen_script=[
            FakeSseResponse(
                sse_frame(0, {"event": "message.delta", "delta": "Hello"}), end="eof"),
            _http_error(404),                # grace re-probe's reconnect
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


def test_status_404_grace_does_not_false_positive_on_live_run():
    """Round-2 grace safety: one status 404 followed by a 200 ``running``
    resets the streak — the run keeps going and completes normally, proving
    the grace is not overeager."""
    harness = run_turn(
        urlopen_script=[
            partial_delta("Hello", seq=0),   # partial text, then socket reset
            _http_error(404),                # grace re-probe's reconnect
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


def test_status_404_streak_requires_consecutive_404s():
    """Round-2: only CONSECUTIVE 404s are terminal — a 503 between two 404s
    restarts the streak (and still spends the long budget); 404s themselves
    never sleep the poll interval."""
    harness = run_turn(
        urlopen_script=[
            FakeSseResponse(
                sse_frame(0, {"event": "message.delta", "delta": "Hello"}), end="eof"),
            _http_error(404),                # reconnect after 404 #1
            _http_error(404),                # reconnect after the 503
            _http_error(404),                # reconnect after 404 #2 (grace)
        ],
        status_script=[
            _http_error(404),                # probe 1: streak = 1, immediate re-probe
            _http_error(503),                # probe 2: transient -> streak reset, budget spent
            _http_error(404),                # probe 3: streak = 1 again
            _http_error(404),                # probe 4: streak = 2 -> terminal
        ],
    )

    raised = harness["result"]
    assert isinstance(raised, RuntimeError), f"expected fail-closed raise, got {raised!r}"
    assert "no longer has the run" in str(raised)
    assert harness["status"].calls == 4
    # Only the 503 consumed a poll interval; neither 404 slept.
    assert harness["clock"].now == GATEWAY_REATTACH_POLL_INTERVAL


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


# ------------------------------------------------------------------ main ---


def main():
    tests = [
        test_events_404_after_partial_stream_interrupted_status_does_not_settle_partial_text,
        test_events_404_then_status_404_fails_closed_within_two_probes,
        test_bare_status_404_grace_second_consecutive_404_fails_closed,
        test_status_404_grace_does_not_false_positive_on_live_run,
        test_status_404_streak_requires_consecutive_404s,
        test_status_503_during_stream_does_not_kill_live_run,
        test_stop_is_honoured_between_probe_retries,
        test_probe_budget_exhaustion_transient_errors_only,
        test_completed_status_output_preferred_over_streamed_text,
        test_completed_status_stream_writeback_overwrites_partial_text,
        test_completed_status_empty_output_keeps_streamed_text,
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
