"""Regression coverage for notify_on_complete across a WebUI restart."""

import logging
import threading
import time
from types import SimpleNamespace

import pytest

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
    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
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


def test_partial_recovery_retry_does_not_repeat_checkpoint_adoption(monkeypatch):
    calls = {"recover": 0, "list": 0}

    class FlakyRegistry:
        def recover_from_checkpoint(self):
            calls["recover"] += 1
            return 1

        def list_sessions(self):
            calls["list"] += 1
            if calls["list"] == 1:
                raise OSError("transient list failure")
            return []

    registry = FlakyRegistry()
    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)

    with pytest.raises(OSError, match="transient list failure"):
        bp.recover_processes_for_webui(registry, lambda *_args, **_kwargs: None)

    assert bp.recover_processes_for_webui(registry, lambda *_args, **_kwargs: None) == 0
    assert calls == {"recover": 1, "list": 2}


def test_concurrent_direct_recovery_runs_once(monkeypatch):
    calls = {"recover": 0}

    class FakeRegistry:
        def recover_from_checkpoint(self):
            time.sleep(0.02)
            calls["recover"] += 1
            return 1

        def list_sessions(self):
            return []

    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    registry = FakeRegistry()
    barrier = threading.Barrier(8)
    results = []

    def recover():
        barrier.wait()
        results.append(
            bp.recover_processes_for_webui(registry, lambda *_args, **_kwargs: None)
        )

    workers = [threading.Thread(target=recover) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert calls["recover"] == 1
    assert sorted(results) == [0] * 7 + [1]


def test_recovery_is_fail_soft_without_agent(monkeypatch):
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "tools.process_registry":
            raise ImportError("Hermes Agent not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    monkeypatch.setattr("builtins.__import__", fake_import)

    assert bp.recover_processes_for_webui() == 0
    assert bp._PROCESS_RECOVERY_DONE is False


def test_recovery_skips_process_whose_session_is_gone(monkeypatch, caplog):
    """A checkpointed process whose owner has been pruned (the resolver's
    ``KeyError(sid)`` contract) must be silently skipped, while a neighbouring
    live process in the same recovery sweep still rebinds. Regression for
    issue #7753: previously every vanished owner logged a full WARNING
    traceback, indistinguishable from a real fault.
    """
    calls = {"registered": []}

    class FakeRegistry:
        def recover_from_checkpoint(self):
            return 2  # two checkpointed processes survive restart

        def list_sessions(self):
            return [
                {"session_id": "proc_vanished", "detached": True},
                {"session_id": "proc_live", "detached": True},
            ]

        def get(self, process_id):
            if process_id == "proc_vanished":
                return _FakeProcessSession("vanished-session")
            if process_id == "proc_live":
                return _FakeProcessSession("live-session")
            raise AssertionError(process_id)

    fake_registry = FakeRegistry()
    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    monkeypatch.setattr(
        bp,
        "register_process_session",
        lambda key, sid: calls["registered"].append((key, sid)),
    )

    def get_session(sid, metadata_only=False):
        # Mirrors api.models._resolve_session_once's contract: missing
        # sessions raise KeyError, NOT None.
        if sid == "vanished-session":
            raise KeyError(sid)
        return SimpleNamespace(id=sid)

    with caplog.at_level(logging.WARNING, logger="api.background_process"):
        result = bp.recover_processes_for_webui(fake_registry, get_session)

    assert result == 2
    # The live process is rebound; the vanished one is silently skipped.
    assert calls["registered"] == [("live-session", "live-session")]
    # No WARNING emitted for the vanished-owner case.
    warning_records = [
        r for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert warning_records == [], (
        "vanished-session KeyError must not produce a WARNING, got: "
        f"{[r.getMessage() for r in warning_records]}"
    )
    assert "Could not resolve recovered WebUI process" not in caplog.text


def test_recovery_still_warns_on_unexpected_resolve_failure(monkeypatch, caplog):
    """Non-KeyError failures from the resolver (e.g. ``OSError`` on an
    unreadable sidecar) must keep entering the existing WARNING path. This
    pins that the fix for #7753 is narrow and does not over-broaden the
    silent-skip contract.
    """
    class FakeRegistry:
        def recover_from_checkpoint(self):
            return 1

        def list_sessions(self):
            return [{"session_id": "proc_unexpected", "detached": True}]

        def get(self, process_id):
            assert process_id == "proc_unexpected"
            return _FakeProcessSession("webui-session")

    fake_registry = FakeRegistry()
    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)

    def get_session(sid, metadata_only=False):
        raise OSError("sidecar unreadable")

    with caplog.at_level(logging.WARNING, logger="api.background_process"):
        result = bp.recover_processes_for_webui(fake_registry, get_session)

    assert result == 1
    warning_records = [
        r for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert any(
        "Could not resolve recovered WebUI process 'proc_unexpected'" in r.getMessage()
        for r in warning_records
    ), (
        "OSError from the resolver must still emit a WARNING, got: "
        f"{[r.getMessage() for r in warning_records]}"
    )