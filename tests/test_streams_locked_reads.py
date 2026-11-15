"""STREAMS registry reads must go through the lock-disciplined peek_stream().

Writers mutate config.STREAMS under STREAMS_LOCK (teardown in api/streaming.py
and the route layer); bare STREAMS.get() reads race those pops. These tests pin
the peek_stream() helper and forbid reintroducing bare STREAMS.get() calls in
the api package outside api/config.py itself.
"""

import queue
import re
import threading
from pathlib import Path

import api.config as config

REPO = Path(__file__).resolve().parents[1]
API_DIR = REPO / "api"


def test_peek_stream_returns_registered_queue_and_none_for_missing():
    stream_id = "peek-stream-registered"
    q = queue.Queue()
    registered = False
    try:
        with config.STREAMS_LOCK:
            config.STREAMS[stream_id] = q
            registered = True
        assert config.peek_stream(stream_id) is q
        assert config.peek_stream("nope") is None
    finally:
        if registered:
            with config.STREAMS_LOCK:
                config.STREAMS.pop(stream_id, None)


def test_peek_stream_acquires_streams_lock(monkeypatch):
    events = []
    real_lock = threading.Lock()

    class _RecordingLock:
        def __enter__(self):
            events.append("enter")
            real_lock.acquire()
            return real_lock

        def __exit__(self, exc_type, exc, tb):
            real_lock.release()
            return False

    monkeypatch.setattr(config, "STREAMS_LOCK", _RecordingLock())
    # Missing id: the locked lookup still happens and returns None.
    assert config.peek_stream("peek-stream-lock-check") is None
    assert events, "peek_stream did not acquire STREAMS_LOCK"


def test_no_bare_streams_get_outside_config():
    pattern = re.compile(r"STREAMS\.get\(")
    offenders = []
    for path in sorted(API_DIR.glob("*.py")):
        if path.name == "config.py":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(
                    f"{path.relative_to(REPO)}:{lineno}: {line.strip()}"
                )
    assert not offenders, (
        "bare STREAMS.get( outside api/config.py; use config.peek_stream() "
        "so reads take STREAMS_LOCK:\n" + "\n".join(offenders)
    )
