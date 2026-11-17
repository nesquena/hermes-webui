"""One monotonic admission budget, rather than three independent timeouts."""
import subprocess
import sys
import time
import types

import pytest


@pytest.fixture
def clocked_revision(monkeypatch, tmp_path):
    from api import agent_runtime as runtime

    clock = types.SimpleNamespace(now=100.0, calls=[])
    monkeypatch.setattr(runtime, "time", types.SimpleNamespace(monotonic=lambda: clock.now))
    outputs = [str(tmp_path), "run_agent.py", "a" * 40]

    def install(delays, *, failure=None, failed_call=1, respect_timeout=True):
        def run(command, **kwargs):
            index = len(clock.calls)
            timeout = kwargs["timeout"]
            clock.calls.append(timeout)
            if index + 1 == failed_call and failure:
                if failure == "oserror":
                    raise OSError("unavailable")
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(command, timeout)
                return subprocess.CompletedProcess(command, 1, "", "unavailable")
            delay = delays[index]
            if respect_timeout and delay > timeout:
                clock.now += timeout
                raise subprocess.TimeoutExpired(command, timeout)
            clock.now += delay
            return subprocess.CompletedProcess(command, 0, outputs[index], "")
        monkeypatch.setattr(runtime.subprocess, "run", run)

    def read():
        return runtime._read_agent_revision(tmp_path, module_path=tmp_path / "run_agent.py")

    return clock, install, read


def test_three_four_second_calls_exhaust_total_budget(clocked_revision):
    clock, install, read = clocked_revision
    install([4, 4, 4])
    assert read() is None
    assert clock.calls == pytest.approx([10, 6, 2])
    assert clock.now == pytest.approx(110)


def test_each_subprocess_receives_remaining_budget(clocked_revision):
    clock, install, read = clocked_revision
    install([2.2, 2.2, 2.2])
    assert read() == "a" * 40
    assert clock.calls == pytest.approx([10, 7.8, 5.6])


@pytest.mark.parametrize("delays,expected_calls", [([10, 0, 0], 1), ([4, 6, 0], 2)])
def test_no_subprocess_starts_after_budget_exhaustion(clocked_revision, delays, expected_calls):
    clock, install, read = clocked_revision
    install(delays)
    assert read() is None
    assert len(clock.calls) == expected_calls


@pytest.mark.parametrize("last_delay", [2, 2.1])
def test_late_success_is_rejected(clocked_revision, last_delay):
    # OS scheduling/process creation can overshoot even a supplied timeout.
    _, install, read = clocked_revision
    install([4, 4, last_delay], respect_timeout=False)
    assert read() is None


@pytest.mark.parametrize("delays", [[0, 0, 0], [3, 3, 3], [4, 4, 1.99]])
def test_fast_and_slow_within_total_succeed(clocked_revision, delays):
    _, install, read = clocked_revision
    install(delays)
    assert read() == "a" * 40


@pytest.mark.parametrize("failure", ["oserror", "timeout", "nonzero"])
@pytest.mark.parametrize("failed_call", [1, 2, 3])
def test_subprocess_failures_remain_closed(clocked_revision, failure, failed_call):
    clock, install, read = clocked_revision
    install([0, 0, 0], failure=failure, failed_call=failed_call)
    assert read() is None
    assert len(clock.calls) == failed_call


def test_real_three_four_second_git_processes_fail_closed(monkeypatch, tmp_path):
    from api import agent_runtime as runtime

    module = tmp_path / "run_agent.py"
    module.write_text("class AIAgent: pass\n", encoding="utf-8")
    real_run = subprocess.run
    for args in [("init", "-q"), ("add", "run_agent.py"), ("commit", "-qm", "fixture")]:
        real_run(["git", "-C", str(tmp_path), "-c", "user.name=Test",
                  "-c", "user.email=test@example.invalid", *args],
                 check=True, capture_output=True)
    calls = []

    def delayed_run(command, **kwargs):
        calls.append(kwargs["timeout"])
        # The sleep and exec are inside the actual timeout-bounded process.
        return real_run([sys.executable, "-c",
                         "import os,sys,time; time.sleep(4); "
                         "os.execvp(sys.argv[1], sys.argv[1:])", *command], **kwargs)

    monkeypatch.setattr(runtime.subprocess, "run", delayed_run)
    started = time.monotonic()
    revision = runtime._read_agent_revision(tmp_path, module_path=module)
    elapsed = time.monotonic() - started
    print(f"real Git probe: elapsed={elapsed:.3f}s, timeouts={calls}, revision={revision!r}")
    assert revision is None
    assert len(calls) == 3
    # Generous scheduling margin; exact boundaries use the deterministic clock.
    assert 9.5 <= elapsed < 11.5
