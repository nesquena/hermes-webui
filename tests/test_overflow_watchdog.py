"""Coverage for the in-process worker-pool overflow watchdog in server.py.

When ``_request_worker_slots`` stays continuously exhausted for the configured
window (``HERMES_WEBUI_OVERFLOW_SUICIDE_S``, legacy ``WEBUI_OVERFLOW_SUICIDE_S``)
the server calls ``os._exit(1)`` so its supervisor (the docker_init.bash
respawn loop, systemd, launchd) replaces the wedged process with a working
one. Any sample with a free slot resets the window — the watchdog only fires
on a pool that never recovers, not on load spikes.
"""

import threading
import time
import types

from server import (
    _overflow_watchdog_window_s,
    _run_overflow_watchdog,
    _start_overflow_watchdog,
)


def _fake_httpd(max_workers: int = 2):
    httpd = types.SimpleNamespace()
    httpd.max_request_workers = max_workers
    httpd._request_worker_slots = threading.BoundedSemaphore(max_workers)
    httpd.overflow_rejects_total = 0
    return httpd


def _run_in_thread(httpd, window_s, poll_interval_s):
    exited = threading.Event()
    codes: list[int] = []

    def _fake_exit(code):
        codes.append(code)
        exited.set()

    thread = threading.Thread(
        target=_run_overflow_watchdog,
        args=(httpd, window_s, poll_interval_s, _fake_exit),
        daemon=True,
    )
    thread.start()
    return thread, exited, codes


def test_exits_after_continuous_exhaustion():
    httpd = _fake_httpd(max_workers=2)
    assert httpd._request_worker_slots.acquire(blocking=False)
    assert httpd._request_worker_slots.acquire(blocking=False)

    thread, exited, codes = _run_in_thread(httpd, window_s=0.1, poll_interval_s=0.01)
    assert exited.wait(timeout=5), "watchdog did not fire on continuous exhaustion"
    thread.join(timeout=5)
    assert codes == [1]


def test_momentary_recovery_resets_the_window():
    httpd = _fake_httpd(max_workers=2)
    assert httpd._request_worker_slots.acquire(blocking=False)
    assert httpd._request_worker_slots.acquire(blocking=False)

    thread, exited, codes = _run_in_thread(httpd, window_s=0.4, poll_interval_s=0.01)
    # Free a slot mid-window: the exhaustion streak must reset.
    time.sleep(0.2)
    httpd._request_worker_slots.release()
    time.sleep(0.5)
    assert not exited.is_set(), "watchdog fired despite the pool recovering"

    # Removing the semaphore makes the loop return, so the thread can be joined.
    httpd._request_worker_slots = None
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert codes == []


def test_healthy_pool_never_fires():
    httpd = _fake_httpd(max_workers=2)
    thread, exited, codes = _run_in_thread(httpd, window_s=0.05, poll_interval_s=0.01)
    time.sleep(0.3)
    assert not exited.is_set()
    httpd._request_worker_slots = None
    thread.join(timeout=5)
    assert codes == []


def test_window_env_parsing(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_OVERFLOW_SUICIDE_S", raising=False)
    monkeypatch.delenv("WEBUI_OVERFLOW_SUICIDE_S", raising=False)
    assert _overflow_watchdog_window_s() == 45  # default sits in the 30-60s band

    monkeypatch.setenv("WEBUI_OVERFLOW_SUICIDE_S", "60")
    assert _overflow_watchdog_window_s() == 60  # legacy spelling honored

    monkeypatch.setenv("HERMES_WEBUI_OVERFLOW_SUICIDE_S", "30")
    assert _overflow_watchdog_window_s() == 30  # primary spelling wins

    monkeypatch.setenv("HERMES_WEBUI_OVERFLOW_SUICIDE_S", "not-a-number")
    assert _overflow_watchdog_window_s() == 60  # bad primary falls through to legacy

    monkeypatch.delenv("WEBUI_OVERFLOW_SUICIDE_S")
    assert _overflow_watchdog_window_s() == 45  # bad value alone -> default

    monkeypatch.setenv("HERMES_WEBUI_OVERFLOW_SUICIDE_S", "0")
    assert _overflow_watchdog_window_s() == 0  # 0 disables

    monkeypatch.setenv("HERMES_WEBUI_OVERFLOW_SUICIDE_S", "-5")
    assert _overflow_watchdog_window_s() == 0  # negative clamps to disabled


def test_start_disabled_via_env(monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WEBUI_OVERFLOW_SUICIDE_S", "0")
    before = {t.name for t in threading.enumerate()}
    _start_overflow_watchdog(_fake_httpd())
    after = {t.name for t in threading.enumerate()}
    assert "webui-overflow-watchdog" not in (after - before)
    assert "watchdog disabled" in capsys.readouterr().out
