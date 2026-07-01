"""Regression tests for #3280 — file manager falls back to state.db for
external (Telegram/CLI) sessions instead of returning 404.

Covers:
  (a) WebUI session — existing behavior preserved (get_session path).
  (b) state.db-only session — fallback returns a workspace-bearing view.
  (c) Unknown session — KeyError still propagates so callers 404.
  (d) Static check: every file-manager handler in api/routes.py calls
      get_session_for_file_ops, not the raw get_session.
"""

from __future__ import annotations

import io
import logging
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest


ROOT = Path(__file__).resolve().parents[1]
ROUTES_PY = ROOT / "api" / "routes.py"


FILE_HANDLERS = [
    "_handle_folder_download",
    "_handle_file_raw",
    "_handle_file_read",
    "_handle_file_delete",
    "_handle_file_save",
    "_handle_file_create",
    "_handle_file_rename",
    "_handle_create_dir",
    "_handle_file_reveal",
    "_handle_file_path",
    "_handle_file_open_vscode",
]


def _handler_body(src: str, name: str) -> str:
    start = src.index(f"def {name}(")
    # next top-level def or class
    m = re.search(r"\n(?:def |class )", src[start + 1 :])
    end = (start + 1 + m.start()) if m else len(src)
    return src[start:end]


def test_routes_file_handlers_use_fallback():
    src = ROUTES_PY.read_text(encoding="utf-8")
    assert "get_session_for_file_ops" in src, "fallback helper must be imported"
    missing = []
    for name in FILE_HANDLERS:
        body = _handler_body(src, name)
        # Must not call get_session(...) directly inside the handler.
        # (get_session_for_file_ops also contains "get_session(" as a substring,
        # so check word-boundary occurrences.)
        bare = re.findall(r"(?<!_)\bget_session\(", body)
        # Strip occurrences that are actually get_session_for_file_ops( — the
        # regex above already excludes underscore prefix, so any remaining
        # match is a raw get_session call.
        if bare:
            missing.append(name)
    assert not missing, f"raw get_session() still used in: {missing}"


# ---------------------------------------------------------------------------
# Functional tests against api.models.get_session_for_file_ops
# ---------------------------------------------------------------------------

pytestmark_models = pytest.mark.requires_agent_modules


def _make_state_db(path: Path, sid: str) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            model TEXT,
            message_count INTEGER DEFAULT 0,
            started_at TEXT,
            source TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, title, model, message_count, started_at, source) "
        "VALUES (?, 'telegram session', 'gpt-x', 1, '2026-01-01T00:00:00Z', 'telegram')",
        (sid,),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def models_module():
    return pytest.importorskip("api.models")


def test_get_session_for_file_ops_webui_passthrough(models_module, monkeypatch):
    """(a) WebUI session — delegates to get_session, no state.db consulted."""
    profiles_module = pytest.importorskip("api.profiles")
    sentinel = SimpleNamespace(profile=None)
    called = {"get_session": 0, "profile_match": 0, "state_db": 0}

    def fake_get_session(sid, metadata_only=False):
        called["get_session"] += 1
        return sentinel

    def fake_profiles_match(session_profile, active_profile):
        called["profile_match"] += 1
        assert session_profile is None
        assert active_profile == "default"
        return True

    def fake_has(_sid):
        called["state_db"] += 1
        return True

    monkeypatch.setattr(models_module, "get_session", fake_get_session)
    monkeypatch.setattr(models_module, "state_db_has_session", fake_has)
    monkeypatch.setattr(profiles_module, "_profiles_match", fake_profiles_match)
    monkeypatch.setattr(profiles_module, "get_active_profile_name", lambda: "default")
    result = models_module.get_session_for_file_ops("webui-sid")
    assert result is sentinel
    assert called == {"get_session": 1, "profile_match": 1, "state_db": 0}


def test_get_session_for_file_ops_rejects_foreign_profile(
    models_module, monkeypatch, tmp_path, caplog
):
    """WebUI sessions must belong to the active profile before file access."""
    profiles_module = pytest.importorskip("api.profiles")
    foreign_session = SimpleNamespace(profile="research", workspace=str(tmp_path))
    called = {"get_session": 0, "profile_match": 0, "state_db": 0}

    def fake_get_session(sid, metadata_only=False):
        called["get_session"] += 1
        return foreign_session

    def fake_profiles_match(session_profile, active_profile):
        called["profile_match"] += 1
        assert session_profile == "research"
        assert active_profile == "default"
        return False

    def fake_has(_sid):
        called["state_db"] += 1
        return True

    monkeypatch.setattr(models_module, "get_session", fake_get_session)
    monkeypatch.setattr(models_module, "state_db_has_session", fake_has)
    monkeypatch.setattr(profiles_module, "_profiles_match", fake_profiles_match)
    monkeypatch.setattr(profiles_module, "get_active_profile_name", lambda: "default")

    with caplog.at_level(logging.DEBUG, logger=models_module.logger.name):
        with pytest.raises(KeyError):
            models_module.get_session_for_file_ops("foreign-webui-sid")
    # A found-but-foreign WebUI sidecar is an authorization failure, not a
    # missing-session condition that can fall through to the state.db fallback.
    assert called == {"get_session": 1, "profile_match": 1, "state_db": 0}
    assert "Rejected file-manager session for foreign profile" in caplog.text
    assert "foreign-webui-sid" in caplog.text
    assert "session_profile='research'" in caplog.text
    assert "active_profile='default'" in caplog.text


def test_file_read_rejects_foreign_profile_session(
    models_module, monkeypatch, tmp_path
):
    """A default-profile file route cannot read a named-profile workspace."""
    profiles_module = pytest.importorskip("api.profiles")
    routes_module = pytest.importorskip("api.routes")
    workspace = tmp_path / "named-workspace"
    workspace.mkdir()
    (workspace / "marker.txt").write_text("foreign profile marker")
    session = models_module.Session(
        session_id="foreign-profile-file-read",
        workspace=str(workspace),
        profile="research",
    )
    models_module.SESSIONS[session.session_id] = session

    class Handler:
        command = "GET"
        headers = {}

        def __init__(self):
            self.status = None
            self.headers_sent = []
            self.wfile = io.BytesIO()

        def send_response(self, code):
            self.status = code

        def send_header(self, key, value):
            self.headers_sent.append((key, value))

        def end_headers(self):
            pass

    monkeypatch.setattr(profiles_module, "get_active_profile_name", lambda: "default")
    try:
        handler = Handler()
        routes_module._handle_file_read(
            handler,
            urlparse(
                "/api/file?session_id=foreign-profile-file-read&path=marker.txt"
            ),
        )
        assert handler.status == 404
        assert b"foreign profile marker" not in handler.wfile.getvalue()
    finally:
        models_module.SESSIONS.pop(session.session_id, None)


def test_get_session_for_file_ops_state_db_fallback(
    models_module, monkeypatch, tmp_path
):
    """(b) state.db-only session — returns view with workspace populated."""
    db = tmp_path / "state.db"
    _make_state_db(db, "tg-123")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hi from telegram session")

    def raise_key(sid, metadata_only=False):
        raise KeyError(sid)

    monkeypatch.setattr(models_module, "get_session", raise_key)
    monkeypatch.setattr(models_module, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(
        models_module, "get_last_workspace", lambda: str(workspace)
    )

    view = models_module.get_session_for_file_ops("tg-123")
    assert view.session_id == "tg-123"
    assert Path(view.workspace) == workspace
    # The workspace is real and readable — file-manager handlers will
    # successfully serve files relative to it instead of returning 404.
    assert (Path(view.workspace) / "hello.txt").read_text() == "hi from telegram session"


def test_get_session_for_file_ops_unknown_session_raises(
    models_module, monkeypatch, tmp_path
):
    """(c) Unknown session — KeyError propagates so callers still 404."""
    db = tmp_path / "state.db"
    _make_state_db(db, "tg-123")

    def raise_key(sid, metadata_only=False):
        raise KeyError(sid)

    monkeypatch.setattr(models_module, "get_session", raise_key)
    monkeypatch.setattr(models_module, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models_module, "get_last_workspace", lambda: str(tmp_path))

    with pytest.raises(KeyError):
        models_module.get_session_for_file_ops("does-not-exist")


def test_state_db_has_session_missing_db(models_module, monkeypatch, tmp_path):
    monkeypatch.setattr(
        models_module, "_active_state_db_path", lambda: tmp_path / "missing.db"
    )
    assert models_module.state_db_has_session("any") is False


def test_state_db_has_session_present(models_module, monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db, "cli-9")
    monkeypatch.setattr(models_module, "_active_state_db_path", lambda: db)
    assert models_module.state_db_has_session("cli-9") is True
    assert models_module.state_db_has_session("nope") is False
