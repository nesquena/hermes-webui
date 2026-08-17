"""Tests for interrupting live async delegations when a WebUI session is deleted.

Covers issue #6949: deleting a session must interrupt all live async delegations
owned by that session before deleting records.
"""
from __future__ import annotations

import sys
import types
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
        owner_profile: str | None = None,
        reason: str = "session_end",
    ) -> int:
        calls["interrupt_for_session"].append(
            {
                "session_key": session_key,
                "origin_ui_session_id": origin_ui_session_id,
                "parent_session_id": parent_session_id,
                "owner_profile": owner_profile,
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
    assert call["owner_profile"] is None  # event_profile is None in test (no profile set on session)
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

    def _interrupt_for_session(
        session_key: str = "",
        origin_ui_session_id: str = "",
        parent_session_id: str = "",
        owner_profile: str | None = None,
        reason: str = "session_end",
    ):
        calls["interrupt_for_session"].append(
            {
                "session_key": session_key,
                "origin_ui_session_id": origin_ui_session_id,
                "parent_session_id": parent_session_id,
                "owner_profile": owner_profile,
                "reason": reason,
            }
        )
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


def test_delete_session_interrupt_before_runtime_teardown(tmp_path, monkeypatch):
    """interrupt_for_session is called before the session agent is evicted."""
    _isolate_session_store(tmp_path, monkeypatch)
    sid = "test-session-delete-interrupt-order"
    session = Session(session_id=sid, title="Test Session", messages=[{"role": "user", "content": "hi"}])
    session.save()

    captured = _capture_post(monkeypatch, {"session_id": sid})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: False)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)

    # Track call order between interrupt_for_session and the agent eviction.
    call_order = []

    import api.config as config_mod

    def tracking_evict(session_id):
        call_order.append(("evict_session_agent", session_id))

    monkeypatch.setattr(config_mod, "_evict_session_agent", tracking_evict)

    # Install fake async_delegation that tracks when it's called
    fake_mod = types.ModuleType("tools.async_delegation")

    def _interrupt_for_session(
        session_key: str = "",
        origin_ui_session_id: str = "",
        parent_session_id: str = "",
        owner_profile: str | None = None,
        reason: str = "session_end",
    ):
        call_order.append(("interrupt_for_session", {
            "session_key": session_key,
            "origin_ui_session_id": origin_ui_session_id,
            "parent_session_id": parent_session_id,
            "owner_profile": owner_profile,
            "reason": reason,
        }))
        return 1

    fake_mod.interrupt_for_session = _interrupt_for_session
    fake_pkg = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", fake_pkg)
    monkeypatch.setitem(sys.modules, "tools.async_delegation", fake_mod)
    monkeypatch.setattr(fake_pkg, "async_delegation", fake_mod, raising=False)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    # interrupt_for_session must fire while the session runtime still exists,
    # i.e. before _evict_session_agent tears it down. The interrupt must NOT
    # hold the per-session mutation lock (it runs after the lock's finally).
    interrupt_calls = [c for c in call_order if c[0] == "interrupt_for_session"]
    evict_calls = [c for c in call_order if c[0] == "evict_session_agent"]

    assert len(interrupt_calls) == 1
    assert len(evict_calls) == 1
    assert call_order.index(interrupt_calls[0]) < call_order.index(evict_calls[0])
    # Deletion still succeeds and the interrupted count is reported.
    assert captured["status"] == 200
    assert captured["payload"]["interrupted"] == 1


def test_delete_session_scopes_interrupt_by_owner_profile(tmp_path, monkeypatch):
    """Deleting a session passes the session's profile as owner_profile to
    interrupt_for_session, so the interrupt cannot cross profile boundaries.

    Regression for the CHANGES_REQUESTED gate: session IDs are not globally
    unique across profiles, so a profile-scoped owner is required to avoid
    interrupting an unrelated profile's live delegations.
    """
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "test-session-delete-owner-profile"
    session = Session(
        session_id=sid,
        title="Test Session",
        messages=[{"role": "user", "content": "hi"}],
        profile="work",
    )
    session.save()

    captured = _capture_post(monkeypatch, {"session_id": sid})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: False)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)
    # The delete route guards session visibility by active profile; match the
    # session's profile so the request reaches the delete handler.
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "work")

    calls = _install_fake_async_delegation(monkeypatch)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200
    assert len(calls["interrupt_for_session"]) == 1
    call = calls["interrupt_for_session"][0]
    assert call["session_key"] == sid
    assert call["origin_ui_session_id"] == sid
    assert call["parent_session_id"] == sid
    assert call["owner_profile"] == "work"
    assert call["reason"] == "session_deleted"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-p", "no:cacheprovider"])