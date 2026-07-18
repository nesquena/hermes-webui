"""Regression coverage for notify_on_complete across a WebUI restart."""

from types import SimpleNamespace

from api import background_process as bp


class _FakeThread:
    def __init__(self, *args, **kwargs):
        self.started = False

    def is_alive(self):
        return self.started

    def start(self):
        self.started = True


class _FakeProcessSession:
    def __init__(self, session_key):
        self.session_key = session_key


def test_start_drain_thread_invokes_recovery(monkeypatch):
    calls = []
    monkeypatch.setattr(bp, "_DRAIN_THREAD", None)
    monkeypatch.setattr(bp, "recover_processes_for_webui", lambda: calls.append("recover") or 0)
    monkeypatch.setattr(bp.threading, "Thread", _FakeThread)

    assert bp.start_drain_thread() is True
    assert calls == ["recover"]


def test_start_drain_thread_survives_recovery_failure(monkeypatch):
    def fail_recovery():
        raise OSError("corrupt checkpoint")

    monkeypatch.setattr(bp, "_DRAIN_THREAD", None)
    monkeypatch.setattr(bp, "recover_processes_for_webui", fail_recovery)
    monkeypatch.setattr(bp.threading, "Thread", _FakeThread)

    assert bp.start_drain_thread() is True
    assert bp._DRAIN_THREAD is not None
    assert bp._DRAIN_THREAD.is_alive()


def test_recovery_runs_once_and_rebuilds_session_mapping(monkeypatch):
    calls = {"recover": 0, "registered": []}

    class FakeRegistry:
        def recover_from_checkpoint(self):
            calls["recover"] += 1
            return 1

        def list_sessions(self):
            return [{
                "session_id": "proc_recovered",
                "detached": True,
            }]

        def get(self, process_id):
            assert process_id == "proc_recovered"
            return _FakeProcessSession("webui-session")

    fake_registry = FakeRegistry()
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    monkeypatch.setattr(
        bp,
        "register_process_session",
        lambda key, sid: calls["registered"].append((key, sid)),
    )

    def get_session(sid, metadata_only=False):
        return SimpleNamespace(id=sid)

    assert bp.recover_processes_for_webui(fake_registry, get_session) == 1
    assert bp.recover_processes_for_webui(fake_registry, get_session) == 0
    assert calls == {
        "recover": 1,
        "registered": [("webui-session", "webui-session")],
    }


def test_recovery_is_fail_soft_without_agent(monkeypatch):
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "tools.process_registry":
            raise ImportError("Hermes Agent not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    monkeypatch.setattr("builtins.__import__", fake_import)

    assert bp.recover_processes_for_webui() == 0
    assert bp._PROCESS_RECOVERY_DONE is False