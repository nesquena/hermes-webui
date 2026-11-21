"""Turn-start watchdog: a worker blocked before its first agent call.

A worker can park on a process-wide lock (model-catalog rebuild, another
turn's session lock) before it ever publishes an SSE frame. The browser then
shows its processing placeholder forever while the turn journal holds only
``worker_started``. These tests pin the watchdog's three behaviours:

* it emits exactly one ``warning`` frame once the deadline passes;
* it stays silent when the worker reaches the running phase in time;
* a non-positive timeout disables it entirely.
"""
import time

import pytest

from api import streaming


class _FakeChannel:
    """Minimal stand-in for the stream queue used by the watchdog."""

    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


def _start(monkeypatch, timeout, stream_id="a" * 32, session_id="b" * 12):
    monkeypatch.setenv("HERMES_WEBUI_TURN_START_TIMEOUT", str(timeout))
    channel = _FakeChannel()
    done = streaming._start_turn_start_watchdog(
        stream_id=stream_id, session_id=session_id, channel=channel)
    return done, channel


def test_watchdog_emits_one_warning_when_start_never_completes(monkeypatch):
    done, channel = _start(monkeypatch, 0.2)
    assert done is not None

    time.sleep(0.7)

    assert len(channel.items) == 1, channel.items
    event, payload = channel.items[0]
    assert event == "warning"
    assert payload["type"] == "turn_start_timeout"
    assert payload["timeout_seconds"] == pytest.approx(0.2)
    assert payload["stream_id"] == "a" * 32
    assert "restart" in payload["message"].lower()


def test_watchdog_stays_silent_when_start_completes(monkeypatch):
    done, channel = _start(monkeypatch, 0.2)
    assert done is not None

    done.set()
    time.sleep(0.5)

    assert channel.items == []


def test_watchdog_disabled_by_non_positive_timeout(monkeypatch):
    done, channel = _start(monkeypatch, 0)
    assert done is None

    time.sleep(0.3)

    assert channel.items == []


def test_watchdog_accepts_snapshot_channel_shape(monkeypatch):
    """Channels with subscribe_with_snapshot get the three-tuple frame."""

    class _SnapshotChannel(_FakeChannel):
        def subscribe_with_snapshot(self):  # pragma: no cover - shape probe only
            return None

    monkeypatch.setenv("HERMES_WEBUI_TURN_START_TIMEOUT", "0.2")
    channel = _SnapshotChannel()
    streaming._start_turn_start_watchdog(
        stream_id="c" * 32, session_id="d" * 12, channel=channel)

    time.sleep(0.7)

    assert len(channel.items) == 1
    event, payload, event_id = channel.items[0]
    assert event == "warning"
    assert payload["type"] == "turn_start_timeout"
    assert event_id is None
