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


def test_start_drain_thread_invokes_recovery(monkeypatch):
    calls = []
    monkeypatch.setattr(bp, "_DRAIN_THREAD", None)
    monkeypatch.setattr(bp, "recover_processes_for_webui", lambda: calls.append("recover") or 0)
    monkeypatch.setattr(bp.threading, "Thread", _FakeThread)

    assert bp.start_drain_thread() is True
    assert calls == ["recover"]


def test_recovery_runs_once_and_rebuilds_session_mapping(monkeypatch):
    calls = {"recover": 0, "registered": []}

    class FakeRegistry:
        def recover_from_checkpoint(self):
            calls["recover"] += 1
            return 1

        def list_sessions(self):
            return [{
                "session_id": "proc_recovered",
                "session_key": "webui-session",
                "detached": True,
            }]

    fake_registry = FakeRegistry()
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    monkeypatch.setattr(bp, "register_process_session", lambda key, sid: calls["registered"].append((key, sid)))

    import tools.process_registry as pr_mod
    monkeypatch.setattr(pr_mod, "process_registry", fake_registry)
    import api.models as models
    monkeypatch.setattr(models, "get_session", lambda sid, metadata_only=False: SimpleNamespace(id=sid))

    assert bp.recover_processes_for_webui() == 1
    assert bp.recover_processes_for_webui() == 0
    assert calls == {
        "recover": 1,
        "registered": [("webui-session", "webui-session")],
    }