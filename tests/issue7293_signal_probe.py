"""Real-signal reentrancy probe for ``ShutdownDrain.handle_signal`` (#7293).

Run as a subprocess by ``test_issue7293_restart_drain_policy.py``. For every
line the stop handler (and anything it calls) executes, it delivers a REAL
signal to this process from a trace hook, so CPython runs the handler again,
reentrantly, at the next bytecode boundary of the interrupted handler. Two
phases: the extra signal lands during the FIRST signal's handler, or during a
LATER (escalating) signal's handler.

Prints one JSON line per scenario. A deadlocked handler hangs this process; the
caller enforces a timeout.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading


def main() -> None:
    sys.path.insert(0, sys.argv[1])
    from api import run_drain

    active = {"on": False}

    def fake_runs():
        return [{"stream_id": "probe-run"}] if active["on"] else []

    def fake_settle(_reason, timeout=0.0):
        active["on"] = False
        return {"settled": ["probe-run"], "pending": [], "failed": [], "timed_out": False}

    run_drain.webui_owned_active_runs = fake_runs
    run_drain.settle_webui_owned_runs = fake_settle
    run_drain.begin_shutdown_drain = lambda: None

    def scenario(phase: str, at_line: int) -> dict | None:
        stops: list[int] = []
        active["on"] = phase == "second"
        drain = run_drain.ShutdownDrain(
            lambda: stops.append(1), budget_seconds=30, settle_seconds=1, poll_seconds=0.01
        )
        signal.signal(signal.SIGUSR1, drain.handle_signal)
        if phase == "second":
            os.kill(os.getpid(), signal.SIGUSR1)  # the first, untraced signal
        fired: list[int] = []
        count = [0]

        def local(frame, event, arg):
            if event == "line" and not fired:
                count[0] += 1
                if count[0] == at_line:
                    fired.append(1)
                    os.kill(os.getpid(), signal.SIGUSR1)
            return local

        sys.settrace(lambda frame, event, arg: local)
        try:
            drain.handle_signal(signal.SIGUSR1, None)
        finally:
            sys.settrace(None)
        if not fired:
            active["on"] = False
            drain.escalate()
            drain.finished.wait(5)
            return None
        finished = drain.finished.wait(10)
        for t in threading.enumerate():
            if t.name == "webui-shutdown-drain":
                t.join(5)
        return {
            "phase": phase,
            "line": at_line,
            "finished": finished,
            "stops": len(stops),
            "signals": len(drain.signals),
            "escalated": bool(drain.escalated),
            "result": (drain.outcome or {}).get("result"),
        }

    for phase in ("first", "second"):
        for at_line in range(1, 400):
            row = scenario(phase, at_line)
            if row is None:
                break
            print(json.dumps(row), flush=True)
    print(json.dumps({"done": True}), flush=True)


if __name__ == "__main__":
    main()
