"""Read-only native-project adapter regression tests for issue #5763."""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from api import profiles, projects_db_adapter as adapter
from api.projects_db_adapter import load_native_projects, native_project_ids_for_paths


SCHEMA = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    icon TEXT,
    color TEXT,
    board_slug TEXT,
    primary_path TEXT,
    created_at INTEGER NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE project_folders (
    project_id TEXT NOT NULL,
    path TEXT NOT NULL,
    label TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (project_id, path)
);
"""


@pytest.fixture(autouse=True)
def _use_temporary_profiles_root(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)


@dataclass
class FakeFolder:
    path: str
    label: str | None
    is_primary: bool
    added_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "label": self.label,
            "is_primary": self.is_primary,
            "added_at": self.added_at,
        }


@dataclass
class FakeProject:
    id: str
    slug: str
    name: str
    created_at: int
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    board_slug: str | None = None
    primary_path: str | None = None
    archived: bool = False
    folders: list[FakeFolder] = field(default_factory=list)


def _project_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> FakeProject:
    folders = [
        FakeFolder(
            path=folder["path"],
            label=folder["label"],
            is_primary=bool(folder["is_primary"]),
            added_at=folder["added_at"],
        )
        for folder in conn.execute(
            "SELECT path, label, is_primary, added_at FROM project_folders "
            "WHERE project_id = ? ORDER BY is_primary DESC, added_at ASC",
            (row["id"],),
        )
    ]
    return FakeProject(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        description=row["description"],
        icon=row["icon"],
        color=row["color"],
        board_slug=row["board_slug"],
        primary_path=row["primary_path"],
        created_at=row["created_at"],
        archived=bool(row["archived"]),
        folders=folders,
    )


def _fake_projects_db_module() -> types.ModuleType:
    module = types.ModuleType("hermes_cli.projects_db")

    def list_projects(conn, *, include_archived=False):
        sql = "SELECT * FROM projects"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY created_at ASC"
        rows = conn.execute(sql).fetchall()
        return [_project_from_row(conn, row) for row in rows]

    def project_for_path(conn, path, *, include_archived=False):
        target = os.path.abspath(os.path.expanduser(str(path).strip()))
        sql = (
            "SELECT pf.project_id, pf.path FROM project_folders pf "
            "JOIN projects p ON p.id = pf.project_id"
        )
        if not include_archived:
            sql += " WHERE p.archived = 0"
        matches = []
        for row in conn.execute(sql):
            folder = row["path"].rstrip("/\\")
            if target == folder or target.startswith(folder + os.sep):
                matches.append((len(folder), row["project_id"]))
        if not matches:
            return None
        project_id = max(matches)[1]
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return _project_from_row(conn, row)

    module.list_projects = list_projects
    module.project_for_path = project_for_path
    return module


def _install_projects_db(monkeypatch, module):
    try:
        import hermes_cli
    except ImportError:
        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.__path__ = []
        monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setattr(hermes_cli, "projects_db", module, raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.projects_db", module)


def _create_db(home: Path) -> Path:
    home.mkdir(parents=True)
    db_path = home / "projects.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
    return db_path


def test_load_native_projects_maps_active_profile_project(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    db_path = _create_db(home)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "p_native_1",
                "web-ui",
                "Web UI",
                "Native project",
                "folder",
                "#123456",
                "engineering",
                "/work/web-ui",
                1234,
                0,
            ),
        )
        conn.execute(
            "INSERT INTO project_folders VALUES (?, ?, ?, ?, ?)",
            ("p_native_1", "/work/web-ui", "Primary", 1, 1234),
        )

    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    assert load_native_projects() == [
        {
            "project_id": "p_native_1",
            "native_project_id": "p_native_1",
            "slug": "web-ui",
            "name": "Web UI",
            "description": "Native project",
            "icon": "folder",
            "color": "#123456",
            "board_slug": "engineering",
            "primary_path": "/work/web-ui",
            "folders": [
                {
                    "path": "/work/web-ui",
                    "label": "Primary",
                    "is_primary": True,
                    "added_at": 1234,
                }
            ],
            "profile": "alpha",
            "created_at": 1234,
            "archived": False,
            "project_source": "hermes-agent",
            "read_only": True,
        }
    ]


def test_load_native_projects_accepts_positional_profile_name(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    _create_db(home)

    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    assert load_native_projects("alpha") == []


def test_native_project_ids_for_paths_uses_native_longest_folder_match(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    db_path = _create_db(home)
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("p_outer", "outer", "Outer", None, None, None, None, "/work", 1, 0),
                ("p_inner", "inner", "Inner", None, None, None, None, "/work/app", 2, 0),
            ],
        )
        conn.executemany(
            "INSERT INTO project_folders VALUES (?, ?, ?, ?, ?)",
            [
                ("p_outer", "/work", None, 1, 1),
                ("p_inner", "/work/app", None, 1, 2),
            ],
        )

    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    assert native_project_ids_for_paths(
        ["/work/app/src/main.py"], profile_name="alpha"
    ) == {"/work/app/src/main.py": "p_inner"}


def test_native_project_ids_for_paths_accepts_positional_profile_name(
    tmp_path, monkeypatch
):
    home = tmp_path / "profiles" / "alpha"
    _create_db(home)

    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    assert native_project_ids_for_paths(["/work"], "alpha") == {}


def test_invalid_traversal_profile_never_reaches_root_home_resolver(tmp_path, monkeypatch):
    root_home = tmp_path / "hermes"
    _create_db(root_home)
    resolver_calls = []

    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: resolver_calls.append(name) or root_home,
    )

    assert load_native_projects(profile_name="../../root") is None
    assert resolver_calls == []


def test_missing_named_profile_cannot_read_root_database(tmp_path, monkeypatch):
    root_home = tmp_path / "hermes"
    db_path = _create_db(root_home)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("p_root", "root", "Root", None, None, None, None, "/root", 1, 0),
        )

    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: root_home)

    assert load_native_projects(profile_name="missing") is None
    assert not (root_home / "profiles" / "missing").exists()


def test_symlinked_database_leaf_cannot_escape_profile_home(tmp_path, monkeypatch):
    alpha_home = tmp_path / "profiles" / "alpha"
    beta_home = tmp_path / "profiles" / "beta"
    alpha_home.mkdir(parents=True)
    beta_db = _create_db(beta_home)
    with sqlite3.connect(beta_db) as conn:
        conn.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("p_beta", "beta", "Beta", None, None, None, None, "/beta", 1, 0),
        )
        conn.execute(
            "INSERT INTO project_folders VALUES (?, ?, ?, ?, ?)",
            ("p_beta", "/beta", None, 1, 1),
        )
    (alpha_home / "projects.db").symlink_to(beta_db)

    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: alpha_home)

    assert load_native_projects(profile_name="alpha") is None
    assert native_project_ids_for_paths(["/beta/private"], profile_name="alpha") is None


def test_real_resolver_accepts_named_profile_symlink_within_profiles_root(
    tmp_path, monkeypatch
):
    base = tmp_path / "hermes"
    real_home = base / "profiles" / "store" / "beta"
    db_path = _create_db(real_home)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("p_beta", "beta", "Beta", None, None, None, None, "/beta", 1, 0),
        )
        conn.execute(
            "INSERT INTO project_folders VALUES (?, ?, ?, ?, ?)",
            ("p_beta", "/beta", None, 1, 1),
        )
    (base / "profiles" / "beta").symlink_to(real_home, target_is_directory=True)

    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    assert not profiles._is_isolated_profile_mode()

    rows = load_native_projects(profile_name="beta")
    assert rows is not None
    assert [row["native_project_id"] for row in rows] == ["p_beta"]
    assert native_project_ids_for_paths(
        ["/beta/private"], profile_name="beta"
    ) == {"/beta/private": "p_beta"}


def test_real_resolver_rejects_named_profile_symlink_outside_profiles_root_quietly(
    tmp_path, monkeypatch, caplog
):
    base = tmp_path / "hermes"
    outside_home = tmp_path / "outside" / "beta"
    _create_db(outside_home)
    profiles_root = base / "profiles"
    profiles_root.mkdir(parents=True)
    (profiles_root / "beta").symlink_to(outside_home, target_is_directory=True)

    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    assert not profiles._is_isolated_profile_mode()

    with caplog.at_level(logging.DEBUG, logger=adapter.__name__):
        assert load_native_projects(profile_name="beta") is None
        assert native_project_ids_for_paths(
            ["/outside/private"], profile_name="beta"
        ) is None

    records = [record for record in caplog.records if record.name == adapter.__name__]
    assert not [record for record in records if record.levelno >= logging.WARNING]
    assert str(base) not in caplog.text
    assert str(outside_home) not in caplog.text


def test_isolated_mode_rejects_foreign_profile_before_resolving(tmp_path, monkeypatch):
    pinned_home = tmp_path / "profiles" / "alpha"
    _create_db(pinned_home)
    resolver_calls = []

    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: True)
    monkeypatch.setattr(profiles, "_isolated_profile_name", lambda: "alpha")
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: resolver_calls.append(name) or pinned_home,
    )

    assert load_native_projects(profile_name="beta") is None
    assert resolver_calls == []


def test_isolated_mode_accepts_existing_arbitrary_pinned_home(tmp_path, monkeypatch):
    pinned_home = tmp_path / "pinned-hermes-home"
    db_path = _create_db(pinned_home)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("p_alpha", "alpha", "Alpha", None, None, None, None, "/alpha", 1, 0),
        )

    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: True)
    monkeypatch.setattr(profiles, "_isolated_profile_name", lambda: "alpha")
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: pinned_home)

    rows = load_native_projects(profile_name="alpha")

    assert rows is not None
    assert [row["native_project_id"] for row in rows] == ["p_alpha"]


def test_missing_database_creates_no_files_directories_or_sidecars(
    tmp_path, monkeypatch, caplog
):
    home = tmp_path / "profiles" / "alpha"
    home.mkdir(parents=True)
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    with caplog.at_level(logging.DEBUG, logger=adapter.__name__):
        assert load_native_projects(profile_name="alpha") is None
    assert [record for record in caplog.records if record.name == adapter.__name__] == []
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before
    assert not (home / "projects.db").exists()
    assert not (home / "projects.db-wal").exists()
    assert not (home / "projects.db-shm").exists()
    assert not (home / "projects.db-journal").exists()


def test_missing_projects_db_module_logs_debug_and_fails_closed(
    tmp_path, monkeypatch, caplog
):
    home = tmp_path / "profiles" / "alpha"
    _create_db(home)
    real_import = adapter.importlib.import_module
    sensitive_path = "/private/native/projects.db"

    def import_without_projects_db(name):
        if name == "hermes_cli.projects_db":
            raise ModuleNotFoundError(sensitive_path)
        return real_import(name)

    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)
    monkeypatch.setattr(adapter.importlib, "import_module", import_without_projects_db)

    with caplog.at_level(logging.DEBUG, logger=adapter.__name__):
        assert load_native_projects(profile_name="alpha") is None
    records = [record for record in caplog.records if record.name == adapter.__name__]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert records[0].exc_info is None
    assert records[0].getMessage() == (
        "Native projects backend unavailable (ModuleNotFoundError)"
    )
    assert sensitive_path not in caplog.text


def test_incompatible_projects_schema_fails_closed(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    home.mkdir(parents=True)
    with sqlite3.connect(home / "projects.db") as conn:
        conn.execute("CREATE TABLE unrelated (value TEXT)")

    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    assert load_native_projects(profile_name="alpha") is None


def test_corrupt_projects_database_fails_closed(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    home.mkdir(parents=True)
    (home / "projects.db").write_bytes(b"not a sqlite database")

    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    assert load_native_projects(profile_name="alpha") is None


def test_incompatible_native_project_dto_fails_closed_without_logging_paths(
    tmp_path, monkeypatch, caplog
):
    home = tmp_path / "profiles" / "alpha"
    _create_db(home)
    module = _fake_projects_db_module()
    sensitive_path = "/private/native/project-alpha"

    class BrokenProject:
        @property
        def id(self):
            raise RuntimeError(sensitive_path)

    module.list_projects = lambda conn, include_archived=False: [BrokenProject()]

    _install_projects_db(monkeypatch, module)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    with caplog.at_level(logging.WARNING, logger=adapter.__name__):
        assert load_native_projects(profile_name="alpha") is None

    records = [record for record in caplog.records if record.name == adapter.__name__]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    assert records[0].getMessage() == (
        "Failed to load native projects (RuntimeError)"
    )
    assert sensitive_path not in caplog.text


def test_committed_wal_rows_are_visible_while_writer_is_open(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    home.mkdir(parents=True)
    db_path = home / "projects.db"
    writer = sqlite3.connect(db_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.executescript(SCHEMA)
        writer.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("p_wal", "wal", "WAL", None, None, None, None, "/wal", 1, 0),
        )
        writer.commit()
        assert Path(f"{db_path}-wal").exists()

        _install_projects_db(monkeypatch, _fake_projects_db_module())
        monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
        monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

        rows = load_native_projects(profile_name="alpha")
        assert rows is not None
        assert [row["native_project_id"] for row in rows] == ["p_wal"]
    finally:
        writer.close()


def test_delete_journal_committed_rows_are_visible(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    db_path = _create_db(home)
    with sqlite3.connect(db_path) as writer:
        assert writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() == "delete"
        writer.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("p_delete", "delete", "DELETE", None, None, None, None, "/delete", 1, 0),
        )

    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    rows = load_native_projects(profile_name="alpha")
    assert rows is not None
    assert [row["native_project_id"] for row in rows] == ["p_delete"]


def test_uncommitted_delete_journal_write_is_never_exposed(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    db_path = _create_db(home)
    writer = sqlite3.connect(db_path)
    try:
        assert writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() == "delete"
        writer.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("p_committed", "committed", "Committed", None, None, None, None, "/old", 1, 0),
        )
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("p_uncommitted", "new", "New", None, None, None, None, "/new", 2, 0),
        )

        _install_projects_db(monkeypatch, _fake_projects_db_module())
        monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
        monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

        rows = load_native_projects(profile_name="alpha")
        assert rows is not None
        assert [row["native_project_id"] for row in rows] == ["p_committed"]
    finally:
        writer.rollback()
        writer.close()


def _assert_project_load_uses_one_snapshot(
    tmp_path, monkeypatch, journal_mode: str
):
    home = tmp_path / "profiles" / "alpha"
    db_path = _create_db(home)
    with sqlite3.connect(db_path) as conn:
        actual_mode = conn.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0]
        assert actual_mode.lower() == journal_mode.lower()
        conn.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("p_snapshot", "snapshot", "Snapshot", None, None, None, None, "/old", 1, 0),
        )
        conn.execute(
            "INSERT INTO project_folders VALUES (?, ?, ?, ?, ?)",
            ("p_snapshot", "/old", None, 1, 1),
        )

    writer_started = Event()
    writer_committed = Event()
    writer_errors = []
    writer_threads = []

    def commit_path_change():
        try:
            with sqlite3.connect(db_path, timeout=2) as writer:
                writer.execute(
                    "UPDATE projects SET primary_path = ? WHERE id = ?",
                    ("/new", "p_snapshot"),
                )
                writer.execute(
                    "UPDATE project_folders SET path = ? WHERE project_id = ?",
                    ("/new", "p_snapshot"),
                )
                writer_started.set()
            writer_committed.set()
        except Exception as exc:  # pragma: no cover - asserted below
            writer_errors.append(exc)
            writer_started.set()

    module = _fake_projects_db_module()

    def interleaved_list(conn, *, include_archived=False):
        sql = "SELECT * FROM projects"
        if not include_archived:
            sql += " WHERE archived = 0"
        rows = conn.execute(sql + " ORDER BY created_at ASC").fetchall()
        writer_thread = Thread(target=commit_path_change)
        writer_threads.append(writer_thread)
        writer_thread.start()
        assert writer_started.wait(1)
        # WAL commits here. With DELETE plus a reader snapshot, commit waits for
        # the adapter connection to close; either way the folder query follows
        # the intervening write attempt deterministically.
        writer_committed.wait(0.2)
        return [_project_from_row(conn, row) for row in rows]

    module.list_projects = interleaved_list
    _install_projects_db(monkeypatch, module)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    rows = load_native_projects(profile_name="alpha")
    assert len(writer_threads) == 1
    writer_threads[0].join(3)
    assert not writer_threads[0].is_alive()
    assert writer_committed.is_set()
    assert writer_errors == []
    assert rows is not None
    assert [(row["primary_path"], row["folders"]) for row in rows] == [
        (
            "/old",
            [
                {
                    "path": "/old",
                    "label": None,
                    "is_primary": True,
                    "added_at": 1,
                }
            ],
        )
    ]


def test_project_load_uses_one_wal_snapshot(tmp_path, monkeypatch):
    _assert_project_load_uses_one_snapshot(tmp_path, monkeypatch, "WAL")


def test_project_load_uses_one_delete_snapshot(tmp_path, monkeypatch):
    _assert_project_load_uses_one_snapshot(tmp_path, monkeypatch, "DELETE")


def test_clean_delete_database_read_creates_no_sidecars(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    db_path = _create_db(home)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() == "delete"
        conn.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("p_clean", "clean", "Clean", None, None, None, None, "/clean", 1, 0),
        )
    before = {path.name for path in home.iterdir()}

    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    assert load_native_projects(profile_name="alpha") is not None
    assert {path.name for path in home.iterdir()} == before == {"projects.db"}


def test_path_batch_uses_one_snapshot_across_matches(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    db_path = _create_db(home)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        conn.executemany(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("p_old", "old", "Old", None, None, None, None, "/one", 1, 0),
                ("p_new", "new", "New", None, None, None, None, "/two", 2, 0),
            ],
        )
        conn.executemany(
            "INSERT INTO project_folders VALUES (?, ?, ?, ?, ?)",
            [
                ("p_old", "/one", None, 1, 1),
                ("p_old", "/two", None, 0, 2),
            ],
        )

    module = _fake_projects_db_module()
    real_match = module.project_for_path
    calls = []

    def interleaved_match(conn, path, *, include_archived=False):
        project = real_match(conn, path, include_archived=include_archived)
        calls.append(path)
        if len(calls) == 1:
            with sqlite3.connect(db_path) as writer:
                writer.execute(
                    "UPDATE project_folders SET project_id = ? WHERE path = ?",
                    ("p_new", "/two"),
                )
        return project

    module.project_for_path = interleaved_match
    _install_projects_db(monkeypatch, module)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    assert native_project_ids_for_paths(
        ["/one/file", "/two/file"], profile_name="alpha"
    ) == {"/one/file": "p_old", "/two/file": "p_old"}
    assert calls == ["/one/file", "/two/file"]


def test_path_batch_ignores_blanks_and_deduplicates_nonblank_paths(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    db_path = _create_db(home)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("p_work", "work", "Work", None, None, None, None, "/work", 1, 0),
        )
        conn.execute(
            "INSERT INTO project_folders VALUES (?, ?, ?, ?, ?)",
            ("p_work", "/work", None, 1, 1),
        )

    module = _fake_projects_db_module()
    real_match = module.project_for_path
    calls = []

    def recording_match(conn, path, *, include_archived=False):
        assert include_archived is False
        calls.append(path)
        return real_match(conn, path, include_archived=include_archived)

    module.project_for_path = recording_match
    _install_projects_db(monkeypatch, module)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    assert native_project_ids_for_paths(
        [None, "", "   ", "/work", "/work"], profile_name="alpha"
    ) == {"/work": "p_work"}
    assert calls == ["/work"]


def test_path_batch_opens_exactly_one_connection(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    db_path = _create_db(home)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("p_work", "work", "Work", None, None, None, None, "/work", 1, 0),
        )
        conn.execute(
            "INSERT INTO project_folders VALUES (?, ?, ?, ?, ?)",
            ("p_work", "/work", None, 1, 1),
        )

    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)
    real_connect = sqlite3.connect
    connection_calls = []

    def recording_connect(*args, **kwargs):
        connection_calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(adapter.sqlite3, "connect", recording_connect)

    assert native_project_ids_for_paths(
        ["/work/a", "/work/b"], profile_name="alpha"
    ) == {"/work/a": "p_work", "/work/b": "p_work"}
    assert len(connection_calls) == 1


def test_adapter_uses_normal_read_only_sqlite_and_never_upstream_connect(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    _create_db(home)
    module = _fake_projects_db_module()
    inspected = {}
    real_list = module.list_projects

    def forbidden_connect(*args, **kwargs):
        raise AssertionError("projects_db.connect must never be called")

    def inspecting_list(conn, *, include_archived=False):
        assert include_archived is False
        inspected["query_only"] = conn.execute("PRAGMA query_only").fetchone()[0]
        inspected["row_factory"] = conn.row_factory
        return real_list(conn, include_archived=include_archived)

    module.connect = forbidden_connect
    module.connect_closing = forbidden_connect
    module.list_projects = inspecting_list
    _install_projects_db(monkeypatch, module)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    real_connect = sqlite3.connect
    connection_calls = []

    def recording_connect(*args, **kwargs):
        connection_calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(adapter.sqlite3, "connect", recording_connect)

    assert load_native_projects(profile_name="alpha") == []
    assert len(connection_calls) == 1
    args, kwargs = connection_calls[0]
    # SQLite may create empty -wal/-shm auxiliaries for a clean WAL database
    # even with mode=ro. Normal read-only mode is required for live WAL consistency.
    assert args[0].startswith("file:")
    assert args[0].endswith("?mode=ro")
    assert "immutable=1" not in args[0]
    assert kwargs["uri"] is True
    assert 0 < kwargs["timeout"] <= 5
    assert inspected == {"query_only": 1, "row_factory": sqlite3.Row}


def test_adapter_closes_its_fresh_connection_after_read(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    _create_db(home)
    _install_projects_db(monkeypatch, _fake_projects_db_module())
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    real_connect = sqlite3.connect
    opened = []

    def recording_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(adapter.sqlite3, "connect", recording_connect)

    assert load_native_projects(profile_name="alpha") == []
    assert len(opened) == 1
    try:
        opened[0].execute("SELECT 1")
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError("adapter leaked its SQLite connection")


def test_busy_database_logs_debug_and_fails_closed(tmp_path, monkeypatch, caplog):
    home = tmp_path / "profiles" / "alpha"
    db_path = _create_db(home)
    writer = sqlite3.connect(db_path)
    try:
        assert writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() == "delete"
        writer.execute("BEGIN EXCLUSIVE")

        _install_projects_db(monkeypatch, _fake_projects_db_module())
        monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
        monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

        with caplog.at_level(logging.DEBUG, logger=adapter.__name__):
            assert load_native_projects(profile_name="alpha") is None
        records = [record for record in caplog.records if record.name == adapter.__name__]
        assert len(records) == 1
        assert records[0].levelno == logging.DEBUG
        assert records[0].exc_info is None
        assert records[0].getMessage() == (
            "Native projects backend unavailable (OperationalError)"
        )
        assert str(home) not in caplog.text
    finally:
        writer.rollback()
        writer.close()


def test_path_batch_backend_failure_discards_partial_results(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    _create_db(home)
    module = _fake_projects_db_module()

    def failing_match(conn, path, *, include_archived=False):
        if path == "/work/a":
            return types.SimpleNamespace(id="p_work")
        raise sqlite3.DatabaseError("backend failed")

    module.project_for_path = failing_match
    _install_projects_db(monkeypatch, module)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    assert native_project_ids_for_paths(
        ["/work/a", "/work/b"], profile_name="alpha"
    ) is None


def test_modern_list_projects_internal_type_error_is_logged_once(
    tmp_path, monkeypatch, caplog
):
    home = tmp_path / "profiles" / "alpha"
    _create_db(home)
    module = _fake_projects_db_module()
    calls = []

    def failing_list(conn, *, include_archived=False):
        calls.append(include_archived)
        raise TypeError("internal backend failure")

    module.list_projects = failing_list
    _install_projects_db(monkeypatch, module)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    with caplog.at_level(logging.WARNING, logger=adapter.__name__):
        assert load_native_projects(profile_name="alpha") is None
    assert calls == [False]
    records = [record for record in caplog.records if record.name == adapter.__name__]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    assert records[0].getMessage() == "Failed to load native projects (TypeError)"
    assert "internal backend failure" not in caplog.text


def test_modern_project_for_path_internal_type_error_is_not_retried(
    tmp_path, monkeypatch
):
    home = tmp_path / "profiles" / "alpha"
    _create_db(home)
    module = _fake_projects_db_module()
    calls = []

    def failing_match(conn, path, *, include_archived=False):
        calls.append((path, include_archived))
        raise TypeError("internal backend failure")

    module.project_for_path = failing_match
    _install_projects_db(monkeypatch, module)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    assert native_project_ids_for_paths(["/work"], profile_name="alpha") is None
    assert calls == [("/work", False)]


def test_path_matcher_exception_does_not_log_sensitive_path(
    tmp_path, monkeypatch, caplog
):
    sensitive_path = "/private/do-not-log"
    home = tmp_path / "profiles" / "alpha"
    _create_db(home)
    module = _fake_projects_db_module()

    def failing_match(conn, path, *, include_archived=False):
        raise RuntimeError(f"matcher failed for {sensitive_path}")

    module.project_for_path = failing_match
    _install_projects_db(monkeypatch, module)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    with caplog.at_level(logging.WARNING, logger=adapter.__name__):
        assert native_project_ids_for_paths(
            [sensitive_path], profile_name="alpha"
        ) is None

    records = [record for record in caplog.records if record.name == adapter.__name__]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    assert records[0].exc_text is None
    assert "RuntimeError" in records[0].getMessage()
    assert sensitive_path not in caplog.text


def test_expected_path_matcher_error_logs_only_exception_class(
    tmp_path, monkeypatch, caplog
):
    sensitive_path = "/private/do-not-log"
    home = tmp_path / "profiles" / "alpha"
    _create_db(home)
    module = _fake_projects_db_module()

    def failing_match(conn, path, *, include_archived=False):
        raise sqlite3.DatabaseError(f"database failed for {sensitive_path}")

    module.project_for_path = failing_match
    _install_projects_db(monkeypatch, module)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    with caplog.at_level(logging.DEBUG, logger=adapter.__name__):
        assert native_project_ids_for_paths(
            [sensitive_path], profile_name="alpha"
        ) is None

    records = [record for record in caplog.records if record.name == adapter.__name__]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert records[0].exc_info is None
    assert records[0].exc_text is None
    assert "DatabaseError" in records[0].getMessage()
    assert sensitive_path not in caplog.text


def test_positional_only_include_archived_signatures_are_supported(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    db_path = _create_db(home)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("p_old", "old", "Old", None, None, None, None, "/old", 1, 0),
        )

    module = types.ModuleType("hermes_cli.projects_db")

    def positional_list(conn, include_archived=False, /):
        row = conn.execute("SELECT * FROM projects").fetchone()
        return [_project_from_row(conn, row)]

    def positional_match(conn, path, include_archived=False, /):
        return types.SimpleNamespace(id="p_old") if path == "/old/file" else None

    module.list_projects = positional_list
    module.project_for_path = positional_match
    _install_projects_db(monkeypatch, module)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    rows = load_native_projects(profile_name="alpha")
    assert rows is not None and rows[0]["native_project_id"] == "p_old"
    assert native_project_ids_for_paths(
        ["/old/file"], profile_name="alpha"
    ) == {"/old/file": "p_old"}


def test_older_upstream_signatures_are_supported(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "alpha"
    db_path = _create_db(home)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("p_old", "old", "Old", None, None, None, None, "/old", 1, 0),
        )

    module = types.ModuleType("hermes_cli.projects_db")

    def legacy_list(conn):
        row = conn.execute("SELECT * FROM projects").fetchone()
        return [_project_from_row(conn, row)]

    def legacy_match(conn, path):
        return types.SimpleNamespace(id="p_old") if path == "/old/file" else None

    module.list_projects = legacy_list
    module.project_for_path = legacy_match
    _install_projects_db(monkeypatch, module)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home)

    rows = load_native_projects(profile_name="alpha")
    assert rows is not None and rows[0]["native_project_id"] == "p_old"
    assert native_project_ids_for_paths(
        ["/old/file"], profile_name="alpha"
    ) == {"/old/file": "p_old"}
