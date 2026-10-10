"""Tests for interrupting live async delegations when a WebUI session is deleted.

Covers issue #6949: deleting a session must interrupt all live async delegations
owned by that session before deleting records.

The fake ``tools.async_delegation`` module mirrors the REAL owner-aware Agent
contract (hermes-agent PR #88108): ``interrupt_for_session`` scopes by an exact
truthy ``owner_profile`` match AND at least one session selector, and falls back
to unscoped OR matching only when ``owner_profile`` is falsy. The delete route
must always pass a canonical truthy owner (``None``/``'default'``/renamed-root
aliases normalize to ``'default'``) — never a falsy unscoped value — and must
skip the interrupt entirely when the owner cannot be resolved.
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


def _seed_delegation(
    records,
    delegation_id,
    session_key="",
    origin_ui_session_id="",
    parent_session_id="",
    owner_profile="",
    status="running",
    interrupt_fn=None,
):
    """Seed one in-memory delegation record with the same keys Agent-core uses
    (hermes-agent PR #88108): the session selectors, the durable owner_profile,
    the status, and the interrupt callback."""
    records[delegation_id] = {
        "delegation_id": delegation_id,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "parent_session_id": parent_session_id,
        "owner_profile": owner_profile,
        "status": status,
        "interrupt_fn": interrupt_fn or (lambda: None),
    }


def _install_fake_async_delegation(monkeypatch, records=None):
    """Install a fake tools.async_delegation module implementing the REAL
    owner-aware Agent contract (#88108).

    ``records`` maps delegation_id → record dict (see ``_seed_delegation``).
    A truthy ``owner_profile`` requires an EXACT owner match AND at least one
    matching session selector; a falsy ``owner_profile`` preserves the legacy
    unscoped OR semantics — the branch the WebUI must never trigger.
    """
    calls = {"interrupt_for_session": []}
    records = {} if records is None else records

    fake_mod = types.ModuleType("tools.async_delegation")

    def _interrupt_for_session(
        session_key: str = "",
        origin_ui_session_id: str = "",
        parent_session_id: str = "",
        owner_profile: str = "",
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
        interrupted = 0
        for record in records.values():
            if record.get("status") != "running":
                continue
            selectors_match = (
                (
                    origin_ui_session_id
                    and str(record.get("origin_ui_session_id") or "") == origin_ui_session_id
                )
                or (session_key and str(record.get("session_key") or "") == session_key)
                or (parent_session_id and str(record.get("parent_session_id") or "") == parent_session_id)
            )
            if owner_profile:
                owner_matches = str(record.get("owner_profile") or "") == owner_profile
            else:
                owner_matches = True
            if owner_matches and selectors_match:
                record["interrupt_fn"]()
                interrupted += 1
        return interrupted

    fake_mod.interrupt_for_session = _interrupt_for_session
    fake_pkg = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", fake_pkg)
    monkeypatch.setitem(sys.modules, "tools.async_delegation", fake_mod)
    monkeypatch.setattr(fake_pkg, "async_delegation", fake_mod, raising=False)
    return calls


def test_delete_session_interrupts_live_async_delegations(tmp_path, monkeypatch):
    """Deleting a session calls interrupt_for_session with the session id and the
    canonical root owner, and returns the count."""
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "test-session-delete-interrupt"
    session = Session(session_id=sid, title="Test Session", messages=[{"role": "user", "content": "hi"}])
    session.save()

    captured = _capture_post(monkeypatch, {"session_id": sid})
    # Stub the various lookups
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: False)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)

    # Two live delegations owned by the root profile under this session id.
    records = {}
    _seed_delegation(records, "d1", session_key=sid, owner_profile="default")
    _seed_delegation(records, "d2", session_key=sid, owner_profile="default")

    # Install fake async_delegation
    calls = _install_fake_async_delegation(monkeypatch, records=records)

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
    # The legacy (profile-less) session canonicalizes to the root owner — never None.
    assert call["owner_profile"] == "default"
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

    # Install fake async_delegation whose matching delegation raises on interrupt.
    def _boom():
        raise RuntimeError("interrupt failed")

    records = {}
    _seed_delegation(records, "d1", session_key=sid, owner_profile="default", interrupt_fn=_boom)
    calls = _install_fake_async_delegation(monkeypatch, records=records)

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

    # Install fake async_delegation that tracks when it's called.
    records = {}

    def _track_interrupt():
        call_order.append(("interrupt_for_session", {"session_key": sid, "owner_profile": "default"}))

    _seed_delegation(records, "d1", session_key=sid, owner_profile="default", interrupt_fn=_track_interrupt)
    _install_fake_async_delegation(monkeypatch, records=records)

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
    """Deleting a named-profile session passes the session's canonical profile as
    owner_profile, so a same-session-id delegation owned by another profile is
    NOT interrupted.

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

    # Same session id in TWO profiles: only the 'work' delegation may be hit.
    interrupted = {"work": False, "default": False}
    records = {}
    _seed_delegation(
        records,
        "d1",
        session_key=sid,
        owner_profile="work",
        interrupt_fn=lambda: interrupted.update(work=True),
    )
    _seed_delegation(
        records,
        "d2",
        session_key=sid,
        owner_profile="default",
        interrupt_fn=lambda: interrupted.update(default=True),
    )
    calls = _install_fake_async_delegation(monkeypatch, records=records)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200
    assert len(calls["interrupt_for_session"]) == 1
    call = calls["interrupt_for_session"][0]
    assert call["session_key"] == sid
    assert call["origin_ui_session_id"] == sid
    assert call["parent_session_id"] == sid
    assert call["owner_profile"] == "work"
    assert call["reason"] == "session_deleted"
    # Cross-profile isolation under the real truthy-owner contract.
    assert captured["payload"]["interrupted"] == 1
    assert interrupted == {"work": True, "default": False}
    assert not (session_dir / f"{sid}.json").exists()


def test_delete_legacy_null_owner_interrupts_only_root_delegations(tmp_path, monkeypatch):
    """Regression: a legacy session with no persisted profile canonicalizes to
    the root owner 'default'.

    Under the real truthy-owner contract this must NOT interrupt a same-session-id
    delegation owned by another profile. Pre-fix the route passed owner_profile=None
    (falsy → unscoped OR), which reproduced as None → [default, work]: both the
    root and the foreign profile's delegations were interrupted.
    """
    _isolate_session_store(tmp_path, monkeypatch)
    sid = "test-session-delete-legacy-null"
    # No profile attribute → legacy backfill row, owns root/default delegations.
    session = Session(session_id=sid, title="Test Session", messages=[{"role": "user", "content": "hi"}])
    session.save()

    captured = _capture_post(monkeypatch, {"session_id": sid})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: False)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)

    interrupted = {"default": False, "work": False}
    records = {}
    _seed_delegation(
        records,
        "d1",
        session_key=sid,
        owner_profile="default",
        interrupt_fn=lambda: interrupted.update(default=True),
    )
    _seed_delegation(
        records,
        "d2",
        session_key=sid,
        owner_profile="work",
        interrupt_fn=lambda: interrupted.update(work=True),
    )
    calls = _install_fake_async_delegation(monkeypatch, records=records)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200
    assert len(calls["interrupt_for_session"]) == 1
    # Canonical owner, never a falsy unscoped value.
    assert calls["interrupt_for_session"][0]["owner_profile"] == "default"
    # Only the root-profile delegation is interrupted — NOT [default, work].
    assert captured["payload"]["interrupted"] == 1
    assert interrupted == {"default": True, "work": False}


def test_delete_renamed_root_session_canonicalizes_owner_to_default(tmp_path, monkeypatch):
    """A renamed-root session (sidecar label 'kinni', root profile) must interrupt
    delegations recorded under the canonical root owner 'default'.

    Pre-fix the route passed owner_profile='kinni', which matched no records the
    Agent actually owns (Agent records the root owner as 'default'), so the
    session's own live delegations were left running.
    """
    _isolate_session_store(tmp_path, monkeypatch)
    sid = "test-session-delete-renamed-root"
    session = Session(
        session_id=sid,
        title="Test Session",
        messages=[{"role": "user", "content": "hi"}],
        profile="kinni",
    )
    session.save()

    captured = _capture_post(monkeypatch, {"session_id": sid})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: False)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)
    # 'kinni' is a renamed-root display name → resolves to ~/.hermes. The delete
    # route guards session visibility by active profile, so the active profile
    # must match the session's label for the request to reach the handler.
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "kinni")
    monkeypatch.setattr(routes, "_is_root_profile", lambda name: name == "kinni")

    interrupted = {"default": False, "work": False}
    records = {}
    _seed_delegation(
        records,
        "d1",
        session_key=sid,
        owner_profile="default",
        interrupt_fn=lambda: interrupted.update(default=True),
    )
    _seed_delegation(
        records,
        "d2",
        session_key=sid,
        owner_profile="work",
        interrupt_fn=lambda: interrupted.update(work=True),
    )
    calls = _install_fake_async_delegation(monkeypatch, records=records)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200
    assert len(calls["interrupt_for_session"]) == 1
    assert calls["interrupt_for_session"][0]["owner_profile"] == "default"
    # The root-owned delegation is interrupted; the foreign profile's is not.
    assert captured["payload"]["interrupted"] == 1
    assert interrupted == {"default": True, "work": False}


def test_delete_session_unresolvable_owner_skips_interrupt(tmp_path, monkeypatch):
    """Fail closed: when the session's owner cannot be resolved (session absent
    from the registry, e.g. a state.db-only row), the interrupt is skipped
    entirely — never an unscoped call."""
    _isolate_session_store(tmp_path, monkeypatch)
    # NOT saved → get_session raises KeyError → owner unresolvable.
    sid = "test-session-delete-unresolvable"

    captured = _capture_post(monkeypatch, {"session_id": sid})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: False)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)

    calls = _install_fake_async_delegation(monkeypatch)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["interrupted"] == 0
    # No interrupt call at all — passing a falsy owner would re-enable the
    # unscoped over-match, and passing a guessed owner could hit the wrong profile.
    assert calls["interrupt_for_session"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-p", "no:cacheprovider"])
