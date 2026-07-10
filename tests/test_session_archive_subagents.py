"""Regression coverage for parent-initiated subagent session archiving.

Delegated children remain view-only. The only permitted mutation is a parent
conversation's archive/restore operation, scoped to its direct
``source='subagent'`` state.db children.
"""
from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
ROUTES_PY = ROOT / "api" / "routes.py"
README = ROOT / "README.md"


def _state_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            parent_session_id TEXT,
            source TEXT,
            archived INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.executemany(
        "INSERT INTO sessions (id, parent_session_id, source, archived) VALUES (?, ?, ?, ?)",
        [
            ("parent", None, "webui", 0),
            ("child-one", "parent", "subagent", 0),
            ("child-two", "parent", "subagent", 0),
            ("case-variant", "parent", "SubAgent", 0),
            ("non-subagent", "parent", "cli", 0),
            ("grandchild", "child-one", "subagent", 0),
            ("other-child", "other-parent", "subagent", 0),
        ],
    )
    conn.commit()
    return conn


class _FakeSessionDb:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.closed = False

    def _execute_write(self, callback):
        result = callback(self.conn)
        self.conn.commit()
        return result

    def close(self):
        self.closed = True


def _archived_rows(conn: sqlite3.Connection) -> dict[str, int]:
    return dict(conn.execute("SELECT id, archived FROM sessions"))


def test_parent_archive_updates_only_direct_exact_subagent_children(monkeypatch):
    """The parent-only capability must not make children individually writable."""
    from api import state_sync

    conn = _state_db()
    db = _FakeSessionDb(conn)
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)

    synced, child_ids = state_sync.sync_session_archive_group(
        "parent", True, profile="default"
    )

    assert synced is True
    assert child_ids == ["child-one", "child-two"]
    assert _archived_rows(conn) == {
        "parent": 1,
        "child-one": 1,
        "child-two": 1,
        "case-variant": 0,
        "non-subagent": 0,
        "grandchild": 0,
        "other-child": 0,
    }
    assert db.closed is True


def test_parent_restore_reverses_the_same_direct_subagent_group(monkeypatch):
    from api import state_sync

    conn = _state_db()
    db = _FakeSessionDb(conn)
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)

    state_sync.sync_session_archive_group("parent", True, profile="default")
    synced, child_ids = state_sync.sync_session_archive_group(
        "parent", False, profile="default"
    )

    assert synced is True
    assert child_ids == ["child-one", "child-two"]
    assert _archived_rows(conn) == {
        "parent": 0,
        "child-one": 0,
        "child-two": 0,
        "case-variant": 0,
        "non-subagent": 0,
        "grandchild": 0,
        "other-child": 0,
    }


def test_parent_with_no_subagents_reports_successful_zero_child_group(monkeypatch):
    from api import state_sync

    conn = _state_db()
    conn.execute("DELETE FROM sessions WHERE parent_session_id = 'parent'")
    conn.commit()
    db = _FakeSessionDb(conn)
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)

    assert state_sync.sync_session_archive_group("parent", True, profile="default") == (
        True,
        [],
    )
    assert _archived_rows(conn)["parent"] == 1
    assert db.closed is True


def test_missing_parent_fails_before_any_child_mutation(monkeypatch):
    """A failed parent write must not archive children on its own."""
    from api import state_sync

    conn = _state_db()
    conn.execute("DELETE FROM sessions WHERE id = 'parent'")
    conn.commit()
    db = _FakeSessionDb(conn)
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)

    assert state_sync.sync_session_archive_group("parent", True, profile="default") == (
        False,
        [],
    )
    assert _archived_rows(conn)["child-one"] == 0
    assert _archived_rows(conn)["child-two"] == 0
    assert db.closed is True


def test_archive_route_assigns_default_to_legacy_profile_before_state_sync(monkeypatch):
    """A profile-less legacy sidecar canonically belongs to root/default."""
    from api import routes, state_sync
    from api.models import Session

    sid = "archive-profile-fallback"
    session = Session(
        session_id=sid,
        workspace=".",
        messages=[{"role": "user", "content": "preserve me"}],
        profile=None,
    )
    saved_profiles = []
    sync_call = {}
    response = {}

    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda value: False)
    monkeypatch.setattr(routes, "get_session", lambda value, **kwargs: session)
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "maiko")
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda value: nullcontext())
    monkeypatch.setattr(
        session,
        "save",
        lambda touch_updated_at=False: saved_profiles.append(session.profile),
    )
    monkeypatch.setattr(
        state_sync,
        "sync_session_archive_group",
        lambda parent_session_id, archived, profile: (
            sync_call.update(
                parent_session_id=parent_session_id,
                archived=archived,
                profile=profile,
            )
            or (True, [])
        ),
    )
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda handler: {"session_id": sid, "archived": True},
    )
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "_worktree_retained_payload", lambda value: {})
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: (
            response.update(payload=payload, status=status) or True
        ),
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True
    assert session.profile == "default"
    assert saved_profiles == ["default"]
    assert sync_call == {
        "parent_session_id": sid,
        "archived": True,
        "profile": "default",
    }
    assert response["status"] == 200
    assert response["payload"]["subagent_archive_synced"] is True


def test_archive_messaging_parent_uses_metadata_profile_for_group_sync(monkeypatch):
    """Messaging fallback must not redirect a named-profile group to default."""
    from api import routes, state_sync

    parent_id = "messaging-parent"
    child_id = "messaging-child"

    def profile_group_db():
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                parent_session_id TEXT,
                source TEXT,
                archived INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.executemany(
            "INSERT INTO sessions (id, parent_session_id, source, archived) VALUES (?, ?, ?, ?)",
            [
                (parent_id, None, "telegram", 0),
                (child_id, parent_id, "subagent", 0),
            ],
        )
        conn.commit()
        return conn

    named_conn = profile_group_db()
    default_conn = profile_group_db()
    dbs = {
        "maiko": _FakeSessionDb(named_conn),
        "default": _FakeSessionDb(default_conn),
    }
    opened_profiles = []
    created_profiles = []
    cli_meta = {
        "profile": "maiko",
        "title": "Messaging parent",
        "source": "telegram",
    }

    class MaterializedMessagingSession:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.profile = kwargs.get("profile")
            self.archived = False
            created_profiles.append(self.profile)

        def save(self, touch_updated_at=False):
            pass

        def compact(self):
            return {"session_id": self.session_id, "archived": self.archived}

    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda value: False)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda value, **kwargs: (_ for _ in ()).throw(KeyError(value)),
    )
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: cli_meta)
    monkeypatch.setattr(routes, "_is_messaging_session_record", lambda value: True)
    monkeypatch.setattr(routes, "Session", MaterializedMessagingSession)
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "wrong-active-profile")
    monkeypatch.setattr(routes, "get_last_workspace", lambda: ".")
    monkeypatch.setattr(routes, "is_cli_session_row", lambda value: True)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda value: nullcontext())
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda handler: {"session_id": parent_id, "archived": True},
    )
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "_worktree_retained_payload", lambda value: {})
    monkeypatch.setattr(routes, "j", lambda handler, payload, **kwargs: True)
    monkeypatch.setattr(
        state_sync,
        "_get_state_db",
        lambda profile=None: (opened_profiles.append(profile) or dbs[profile]),
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True
    assert created_profiles == ["maiko"]
    assert opened_profiles == ["maiko"]
    assert _archived_rows(named_conn) == {parent_id: 1, child_id: 1}
    assert _archived_rows(default_conn) == {parent_id: 0, child_id: 0}

    cli_meta["profile"] = "default"
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True
    assert created_profiles == ["maiko", "default"]
    assert opened_profiles == ["maiko", "default"]
    assert _archived_rows(named_conn) == {parent_id: 1, child_id: 1}
    assert _archived_rows(default_conn) == {parent_id: 1, child_id: 1}


def test_archive_route_preserves_readonly_child_boundary_and_reports_group_state():
    """The public archive flow must use the parent-only group operation."""
    route = ROUTES_PY.read_text(encoding="utf-8")
    route_start = route.index('if parsed.path == "/api/session/archive":')
    route_end = route.index('if parsed.path == "/api/session/move":', route_start)
    archive_route = route[route_start:route_end]

    assert "_session_is_subagent_view_only(sid)" in archive_route
    assert "sync_session_archive_group(" in archive_route
    assert '"subagent_archive_synced"' in archive_route
    assert '"subagent_child_count"' in archive_route
    assert archive_route.index("_session_is_subagent_view_only(sid)") < archive_route.index(
        "sync_session_archive_group("
    )
    assert "direct delegated subagent sessions" in README.read_text(encoding="utf-8")
