import api.agent_cache_governance as govmod
from api import config as api_config


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
    monkeypatch.setattr(govmod.threading, "Thread", _FakeThread)
    monkeypatch.setattr(api_config, "SESSION_AGENT_CACHE_GOVERN_INTERVAL", 0)

    thread = govmod.start_server_governor()

    assert thread is None
    assert _FakeThread.instances == []


def test_positive_governor_interval_starts_named_daemon(monkeypatch):
    _FakeThread.instances.clear()
    monkeypatch.setattr(govmod.threading, "Thread", _FakeThread)
    monkeypatch.setattr(api_config, "SESSION_AGENT_CACHE_GOVERN_INTERVAL", 60)

    thread = govmod.start_server_governor()

    assert thread is _FakeThread.instances[0]
    assert thread.started is True
    assert thread.name == "agent-cache-governor"
    assert thread.daemon is True
