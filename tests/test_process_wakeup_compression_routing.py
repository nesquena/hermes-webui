"""Server-side wakeups addressed to a compression-sealed WebUI origin.

The wakeup router must reuse the same durable, profile-pinned lineage resolver
as ``/api/chat/start`` (``api.compression_continuation``) instead of a private
state.db lookup, so the background completion thread can never fall back to
the TLS/process-global profile.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


def _build_real_compression_chain(tmp_path):
    hermes_state = pytest.importorskip("hermes_state")
    SessionDB = hermes_state.SessionDB

    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="webui")
    db.end_session("parent", "compression")

    # Decoy children share parent_session_id but are not compression successors.
    db.create_session(
        "delegate-child",
        source="webui",
        parent_session_id="parent",
        model_config={"_delegate_from": "parent"},
    )
    db.create_session("tool-child", source="tool", parent_session_id="parent")

    db.create_session("middle", source="webui", parent_session_id="parent")
    db.end_session("middle", "compression")
    db.create_session("live-webui", source="webui", parent_session_id="middle")
    db.close()


def _write_session_sidecar(
    session_dir, filename_session_id, *, payload_session_id, profile, snapshot
):
    payload = {
        "session_id": payload_session_id,
        "title": payload_session_id,
        "workspace": str(session_dir),
        "model": "test-model",
        "messages": [],
        "created_at": 1.0,
        "updated_at": 1.0,
        "profile": profile,
        "pre_compression_snapshot": snapshot,
    }
    (session_dir / f"{filename_session_id}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _snapshot(monkeypatch, *, profile="default", snapshot=True):
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "_get_or_materialize_session",
        lambda session_id, **kwargs: SimpleNamespace(
            session_id=session_id,
            profile=profile,
            pre_compression_snapshot=snapshot,
        ),
    )


def _install_fake_session_db(monkeypatch, homes):
    """Standalone SessionDB contract: one fake state.db per profile home."""

    opened = []

    class FakeSessionDB:
        def __init__(self, path, read_only=False):
            assert read_only is True, "wakeup routing must stay read-only"
            self.home = Path(path).parent
            self.rows = homes[self.home]
            opened.append(self)
            self.closed = False

        def get_session(self, session_id):
            return self.rows.get(session_id)

        def get_compression_tip(self, session_id):
            current = session_id
            while True:
                child = next(
                    (
                        sid
                        for sid, row in self.rows.items()
                        if row.get("parent_session_id") == current
                        and row.get("source") == "webui"
                    ),
                    None,
                )
                if child is None or self.rows[current].get("end_reason") != "compression":
                    return current
                current = child

        def close(self):
            self.closed = True

    fake = types.ModuleType("hermes_state")
    fake.SessionDB = FakeSessionDB
    monkeypatch.setitem(sys.modules, "hermes_state", fake)
    return opened


def _lineage(tip):
    return {
        "parent": {"id": "parent", "end_reason": "compression", "ended_at": 1.0, "source": "webui"},
        tip: {"id": tip, "parent_session_id": "parent", "end_reason": None, "ended_at": None, "source": "webui"},
    }


def test_wakeup_target_follows_real_canonical_compression_chain(monkeypatch, tmp_path):
    """A wakeup addressed to a sealed origin lands on its live WebUI tip."""
    import api.background_process as background_process
    import api.profiles as profiles

    _build_real_compression_chain(tmp_path)
    requested = []

    def resolve_home(name):
        requested.append(name)
        return str(tmp_path)

    monkeypatch.setattr(profiles, "_resolve_profile_home_for_name", resolve_home)
    _snapshot(monkeypatch, profile=None)

    assert background_process._canonical_wakeup_session_id("parent") == "live-webui"
    assert requested == ["default"]


@pytest.mark.parametrize(
    ("session_profile", "expected_owner"),
    [(None, "default"), ("", "default"), ("named-profile", "named-profile")],
)
def test_wakeup_target_pins_explicit_profile_owner(
    monkeypatch, tmp_path, session_profile, expected_owner
):
    """Background routing never resolves an archived origin through TLS state.

    Every profile home has its own live compression tip; the process-global
    active profile is a different named profile.  Only the snapshot owner's
    lineage may supply the tip.
    """
    import api.background_process as background_process
    import api.profiles as profiles

    homes = {}
    for name in ("default", "named-profile", "active-other"):
        home = tmp_path / name
        home.mkdir()
        (home / "state.db").write_bytes(b"")
        homes[home] = _lineage(f"{name}-live-tip")
    opened = _install_fake_session_db(monkeypatch, homes)
    requested = []

    def resolve_home(name):
        requested.append(name)
        return str(tmp_path / name)

    monkeypatch.setattr(profiles, "_resolve_profile_home_for_name", resolve_home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "active-other", raising=False)
    _snapshot(monkeypatch, profile=session_profile)

    assert (
        background_process._canonical_wakeup_session_id("parent")
        == f"{expected_owner}-live-tip"
    )
    assert requested == [expected_owner]
    assert opened and all(db.home.name == expected_owner for db in opened)
    assert all(db.closed for db in opened)


@pytest.mark.parametrize(
    "rows",
    [
        # Sealed without any continuation.
        {"parent": {"id": "parent", "end_reason": "compression", "ended_at": 1.0, "source": "webui"}},
        # Continuation explicitly closed by the user: never a redirect target.
        {
            "parent": {"id": "parent", "end_reason": "compression", "ended_at": 1.0, "source": "webui"},
            "closed": {"id": "closed", "parent_session_id": "parent", "end_reason": "user_exit", "ended_at": 2.0, "source": "webui"},
        },
    ],
)
def test_wakeup_target_fails_closed_without_resumable_tip(monkeypatch, tmp_path, rows):
    import api.background_process as background_process
    import api.profiles as profiles

    (tmp_path / "state.db").write_bytes(b"")
    opened = _install_fake_session_db(monkeypatch, {tmp_path: rows})
    monkeypatch.setattr(profiles, "_resolve_profile_home_for_name", lambda name: str(tmp_path))
    _snapshot(monkeypatch, snapshot=False)

    assert background_process._canonical_wakeup_session_id("parent") == ""
    assert all(db.closed for db in opened)


def test_sidecar_snapshot_without_durable_lineage_fails_closed(monkeypatch):
    """No state.db confirmation must not reopen a sidecar-sealed snapshot."""
    import api.background_process as background_process
    import api.compression_continuation as continuation

    monkeypatch.setattr(continuation, "durable_compression_continuation", lambda s: (False, None))
    _snapshot(monkeypatch, snapshot=True)

    assert background_process._canonical_wakeup_session_id("parent") == ""


def test_wakeup_target_rejects_foreign_snapshot_payload(monkeypatch, tmp_path):
    """A foreign payload under the origin's filename cannot choose a lineage."""
    import api.background_process as background_process
    import api.compression_continuation as continuation
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    seen = []

    def resolver(session):
        seen.append((session.session_id, session.profile))
        return True, "owner-live-tip"

    monkeypatch.setattr(continuation, "durable_compression_continuation", resolver)

    _write_session_sidecar(
        session_dir,
        "parent",
        payload_session_id="foreign-session",
        profile="foreign",
        snapshot=True,
    )
    assert background_process._canonical_wakeup_session_id("parent") == ""
    assert seen == []

    _write_session_sidecar(
        session_dir,
        "parent",
        payload_session_id="parent",
        profile=None,
        snapshot=True,
    )
    models.SESSIONS.clear()
    assert background_process._canonical_wakeup_session_id("parent") == "owner-live-tip"
    assert seen == [("parent", None)]
    models.SESSIONS.clear()


def test_wakeup_target_keeps_live_origin(monkeypatch):
    """Ordinary live sessions retain the exact origin routing contract."""
    import api.background_process as background_process
    import api.compression_continuation as continuation

    monkeypatch.setattr(continuation, "durable_compression_continuation", lambda s: (False, None))
    _snapshot(monkeypatch, snapshot=False)

    assert background_process._canonical_wakeup_session_id("live-origin") == "live-origin"
