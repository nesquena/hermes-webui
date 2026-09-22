from types import SimpleNamespace

import server


class _FakeThread:
    instances = []

    def __init__(self, *, target, name, daemon):
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True


def test_governor_interval_zero_does_not_start_thread(monkeypatch):
    """Zero disables governance instead of creating a time.sleep(0) loop."""
    _FakeThread.instances.clear()
    monkeypatch.setattr(server.threading, "Thread", _FakeThread)
    calls = {"passes": 0}

    def _run_pass():
        calls["passes"] += 1

    governor = SimpleNamespace(run_pass=_run_pass)

    thread = server._start_agent_cache_governor_thread(governor, 0)

    assert thread is None
    assert _FakeThread.instances == []
    assert calls["passes"] == 0


def test_positive_governor_interval_starts_named_daemon(monkeypatch):
    _FakeThread.instances.clear()
    monkeypatch.setattr(server.threading, "Thread", _FakeThread)
    governor = SimpleNamespace(run_pass=lambda: None)

    thread = server._start_agent_cache_governor_thread(governor, 60)

    assert thread is _FakeThread.instances[0]
    assert thread.started is True
    assert thread.name == "agent-cache-governor"
    assert thread.daemon is True
