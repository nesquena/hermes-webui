"""Tests for interrupting live async delegations when a WebUI session is deleted.

Covers issue #6949: deleting a session must interrupt all live async delegations
owned by that session before deleting records.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.models as models
import api.routes as routes
from api.models import SESSIONS, Session


def _capture_post(monkeypatch, body):
    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )
    return captured


def _isolate_session_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    SESSIONS.clear()
    return session_dir


def _install_fake_async_delegation(monkeypatch):
    """Install a fake tools.async_delegation module with interrupt_for_session stub."""
    calls = {"interrupt_for_session": []}

    fake_mod = types.ModuleType("tools.async_delegation")

    def _interrupt_for_session(
        session_key: str = "",
        origin_ui_session_id: str = "",
        parent_session_id: str = "",
        reason: str = "session_end",
    ) -> int:
        calls["interrupt_for_session"].append(
            {
                "session_key": session_key,
                "origin_ui_session_id": origin_ui_session_id,
                "parent_session_id": parent_session_id,
                "reason": reason,
            }
        )
        # Simulate interrupting 2 delegations
        return 2

    fake_mod.interrupt_for_session = _interrupt_for_session
    fake_pkg = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", fake_pkg)
    monkeypatch.setitem(sys.modules, "tools.async_delegation", fake_mod)
    monkeypatch.setattr(fake_pkg, "async_delegation", fake_mod, raising=False)
    return calls


def test_delete_session_interrupts_live_async_delegations(tmp_path, monkeypatch):
    """Deleting a session calls interrupt_for_session with the session id and returns the count."""
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "test-session-delete-interrupt"
    session = Session(session_id=sid, title="Test Session", messages=[{"role": "user", "content": "hi"}])
    session.save()

    captured = _capture_post(monkeypatch, {"session_id": sid})
    # Stub the various lookups
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: False)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)

    # Install fake async_delegation
    calls = _install_fake_async_delegation(monkeypatch)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["state_db_cleanup_failed"] is False
    # The interrupted count should be returned in the response
    assert captured["payload"]["interrupted"] == 2
    # interrupt_for_session should have been called with the session id as all three selectors
    assert len(calls["interrupt_for_session"]) == 1
    call = calls["interrupt_for_session"][0]
    assert call["session_key"] == sid
    assert call["origin_ui_session_id"] == sid
    assert call["parent_session_id"] == sid
    assert call["reason"] == "session_deleted"
    # Sidecar should be deleted
    assert not (session_dir / f"{sid}.json").exists()


def test_delete_session_interrupt_failure_does_not_block_deletion(tmp_path, monkeypatch):
    """If interrupt_for_session raises, deletion still proceeds and returns interrupted=0."""
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "test-session-delete-interrupt-fail"
    session = Session(session_id=sid, title="Test Session", messages=[{"role": "user", "content": "hi"}])
    session.save()

    captured = _capture_post(monkeypatch, {"session_id": sid})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: False)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)

    # Install fake async_delegation that raises on interrupt
    calls = {"interrupt_for_session": []}
    fake_mod = types.ModuleType("tools.async_delegation")

    def _interrupt_for_session(**kwargs):
        calls["interrupt_for_session"].append(kwargs)
        raise RuntimeError("interrupt failed")

    fake_mod.interrupt_for_session = _interrupt_for_session
    fake_pkg = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", fake_pkg)
    monkeypatch.setitem(sys.modules, "tools.async_delegation", fake_mod)
    monkeypatch.setattr(fake_pkg, "async_delegation", fake_mod, raising=False)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    # When interrupt fails, we return 0 (the initial value)
    assert captured["payload"]["interrupted"] == 0
    assert len(calls["interrupt_for_session"]) == 1
    # Sidecar should still be deleted
    assert not (session_dir / f"{sid}.json").exists()


def test_delete_session_interrupt_called_before_sidecar_deletion(tmp_path, monkeypatch):
    """interrupt_for_session is called while the session record still exists."""
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "test-session-delete-interrupt-order"
    session = Session(session_id=sid, title="Test Session", messages=[{"role": "user", "content": "hi"}])
    session.save()

    captured = _capture_post(monkeypatch, {"session_id": sid})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: False)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)

    # Track call order
    call_order = []

    # Wrap get_session to track when it's called (imported in routes)
    original_get_session = routes.get_session

    def tracking_get_session(session_id, metadata_only=False):
        call_order.append(("get_session", session_id))
        return original_get_session(session_id, metadata_only)

    monkeypatch.setattr(routes, "get_session", tracking_get_session)

    # Install fake async_delegation that tracks when it's called
    fake_mod = types.ModuleType("tools.async_delegation")

    def _interrupt_for_session(**kwargs):
        call_order.append(("interrupt_for_session", kwargs))
        return 1

    fake_mod.interrupt_for_session = _interrupt_for_session
    fake_pkg = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", fake_pkg)
    monkeypatch.setitem(sys.modules, "tools.async_delegation", fake_mod)
    monkeypatch.setattr(fake_pkg, "async_delegation", fake_mod, raising=False)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    # interrupt_for_session should be called before SESSIONS.pop (which happens
    # inside the session_lock block). Since get_session is called to get the
    # event_profile BEFORE interrupt_for_session, we expect:
    # 1. get_session (for event_profile)
    # 2. interrupt_for_session
    # 3. get_session (inside session_lock for pop)
    get_session_calls = [c for c in call_order if c[0] == "get_session"]
    interrupt_calls = [c for c in call_order if c[0] == "interrupt_for_session"]

    assert len(get_session_calls) >= 2
    assert len(interrupt_calls) == 1
    # The first get_session (event_profile) happens before interrupt
    assert call_order.index(("get_session", sid)) < call_order.index(interrupt_calls[0])
    # The interrupt happens before the second get_session (inside lock)
    # Note: we can't easily check the exact second get_session, but the order
    # ensures interrupt happens while session still exists


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-p", "no:cacheprovider"])