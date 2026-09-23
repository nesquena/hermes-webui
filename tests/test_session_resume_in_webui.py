"""Focused tests for the safe backend ``POST /api/session/resume_in_webui``.

This is the *only* backend path that converts a foreign, read-only-projected
session (CLI / TUI / ACP / Desktop) into a writable WebUI sidecar while
``HERMES_WEBUI_EXTERNAL_STATE_READ_ONLY=1`` is in force. The tests below pin
the whole gate list:

  * disabled (allowlist unset)
  * invalid profile shape
  * wrong active profile
  * missing source row
  * bad (non-allowlisted) source
  * active/unended source (concurrent-writer guard)
  * lineage root/tip mismatch
  * happy path — and the source ``state.db`` bytes are unchanged
  * idempotent re-resume
  * cross-profile same-sid flat sidecar collision
  * existing read-only projection regression (ordinary import stays read-only)

All DBs are synthetic SQLite files under ``tmp_path``; no production state is
touched and every module-global path is monkeypatched per-test.
"""
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _Recorder:
    """Captures the (msg, status) of ``bad()`` or (payload, status) of ``j()``."""

    def __init__(self):
        self.bad: Any = None
        self.j: Any = None

    @property
    def status(self):
        if self.j is not None:
            return self.j[1]
        if self.bad is not None:
            return self.bad[1]
        return None

    def payload(self):
        return self.j[0] if self.j is not None else None

    def error(self):
        return self.bad[0] if self.bad is not None else None


def _install_response_recorder(routes, monkeypatch):
    """Swap ``j``/``bad`` for pure-python recorders so tests need no socket
    handler. Returns a fresh recorder dict passed as the fake handler."""
    rec = _Recorder()

    def fake_bad(_handler, msg, status=400):
        rec.bad = (msg, status)
        return None

    def fake_j(_handler, payload, status=200, extra_headers=None, *, pretty=True):
        rec.j = (payload, status)
        return None

    monkeypatch.setattr(routes, "bad", fake_bad)
    monkeypatch.setattr(routes, "j", fake_j)
    return rec


_STATE_DB_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT,
    session_source TEXT,
    model TEXT,
    parent_session_id TEXT,
    started_at REAL,
    ended_at REAL,
    end_reason TEXT,
    title TEXT,
    cwd TEXT,
    message_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    role TEXT,
    content TEXT,
    timestamp REAL
);
"""


def _make_state_db(
    path: Path,
    *,
    sid: str,
    source: str = "cli",
    session_source: str = "cli",
    model: str = "test-model",
    title: str = "Resumable session",
    parent_session_id=None,
    started_at: float = 1700000000.0,
    ended_at: "float | None" = 1700000100.0,
    end_reason: "str | None" = "cli_close",
    messages: int = 3,
    insert_row: bool = True,
) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_STATE_DB_SQL)
        if insert_row:
            conn.execute(
                "INSERT INTO sessions (id, source, session_source, model, "
                "parent_session_id, started_at, ended_at, end_reason, title, cwd, "
                "message_count) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid, source, session_source, model, parent_session_id,
                    started_at, ended_at, end_reason, title, "/tmp/ws", messages,
                ),
            )
        for i in range(messages):
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?,?,?,?)",
                (sid, "user" if i % 2 == 0 else "assistant", f"msg {i}",
                 started_at + i),
            )
        conn.commit()
    finally:
        conn.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def routes_module():
    return pytest.importorskip("api.routes")


@pytest.fixture
def resume_env(routes_module, tmp_path, monkeypatch):
    """Wire every external path the endpoint touches onto ``tmp_path``.

    * ``get_hermes_home_for_profile`` -> per-profile dirs under tmp_path
    * active profile -> "alpha"
    * ``SESSION_DIR`` (routes + models) -> tmp_path/webui-state/sessions
    * operator allowlist -> "alpha"
    * response helpers -> pure recorders (returned as the fake handler)
    """
    import api.models as models
    import api.profiles as profiles

    profiles_root = tmp_path / "profiles"
    homes = {}
    for name in ("alpha", "beta"):
        home = profiles_root / name
        home.mkdir(parents=True)
        homes[name] = home

    sessions_dir = tmp_path / "webui-state" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "_index.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile",
        lambda name: homes.get(str(name), homes["alpha"]),
    )
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "alpha")
    # Deterministic root-profile handling (no hermes_cli subprocess / cache).
    monkeypatch.setattr(
        profiles, "_is_root_profile",
        lambda name: (name or "default") == "default",
    )

    monkeypatch.setattr(routes_module, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: homes["alpha"] / "state.db")
    monkeypatch.setattr(routes_module, "publish_session_list_changed", lambda *a, **k: None)

    monkeypatch.setenv("HERMES_WEBUI_RESUME_ALLOW_PROFILES", "alpha")
    monkeypatch.delenv("HERMES_WEBUI_EXTERNAL_STATE_READ_ONLY", raising=False)

    rec = _install_response_recorder(routes_module, monkeypatch)
    return {"routes": routes_module, "models": models, "rec": rec,
            "homes": homes, "sessions_dir": sessions_dir}


def _body(sid="sess-alpha-1", profile="alpha", root=None, tip=None,
          confirm=True, **extra):
    payload = {
        "session_id": sid,
        "profile": profile,
        "lineage_root_id": sid if root is None else root,
        "lineage_tip_id": sid if tip is None else tip,
        "confirm": confirm,
    }
    payload.update(extra)
    return payload


def _alpha_db(env) -> Path:
    return env["homes"]["alpha"] / "state.db"


# ---------------------------------------------------------------------------
# Gate: allowlist disabled
# ---------------------------------------------------------------------------


def test_disabled_when_allowlist_unset(resume_env, monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_RESUME_ALLOW_PROFILES", raising=False)
    _make_state_db(_alpha_db(resume_env), sid="sess-alpha-1")
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body())
    assert env["rec"].status == 403
    assert "disabled" in env["rec"].error()


def test_disabled_when_allowlist_blank(resume_env, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RESUME_ALLOW_PROFILES", "  ,  ")
    _make_state_db(_alpha_db(resume_env), sid="sess-alpha-1")
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body())
    assert env["rec"].status == 403


def test_profile_not_in_allowlist(resume_env):
    _make_state_db(_alpha_db(resume_env), sid="sess-alpha-1")
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body(profile="beta"))
    # beta is a valid profile shape but not on the operator allowlist
    assert env["rec"].status == 403
    assert "not allowed" in env["rec"].error()


# ---------------------------------------------------------------------------
# Gate: profile validation / active profile
# ---------------------------------------------------------------------------


def test_invalid_profile_rejected(resume_env):
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body(profile="Bad Name!"))
    assert env["rec"].status == 400
    assert "invalid profile" in env["rec"].error()


def test_missing_profile_rejected(resume_env):
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body(profile=""))
    assert env["rec"].status == 400
    assert "invalid profile" in env["rec"].error()


def test_wrong_active_profile_rejected(resume_env, monkeypatch):
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "beta")
    _make_state_db(_alpha_db(resume_env), sid="sess-alpha-1")
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body(profile="alpha"))
    assert env["rec"].status == 403
    assert "active profile" in env["rec"].error()


def test_confirm_required(resume_env):
    _make_state_db(_alpha_db(resume_env), sid="sess-alpha-1")
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body(confirm=False))
    assert env["rec"].status == 400


def test_lineage_ids_required(resume_env):
    _make_state_db(_alpha_db(resume_env), sid="sess-alpha-1")
    env = resume_env
    body = _body()
    body.pop("lineage_root_id")
    env["routes"]._handle_session_resume_in_webui(env["rec"], body)
    assert env["rec"].status == 400


# ---------------------------------------------------------------------------
# Gate: source row / source allowlist / concurrent writer
# ---------------------------------------------------------------------------


def test_missing_source_row_404(resume_env):
    # DB exists (with schema) but no row for the requested sid.
    db = _alpha_db(resume_env)
    _make_state_db(db, sid="some-other-sid", insert_row=False)
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body())
    assert env["rec"].status == 404


def test_missing_source_db_404_no_fallback(resume_env):
    # No state.db at all for the profile -> hard 404, never the active store.
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body())
    assert env["rec"].status == 404


def test_source_store_errors_are_sanitized(resume_env, monkeypatch):
    _make_state_db(_alpha_db(resume_env), sid="sess-alpha-1")
    env = resume_env

    def fail_read(*_args, **_kwargs):
        raise sqlite3.OperationalError("cannot open /private/secret/profile/state.db")

    monkeypatch.setattr(env["routes"], "_read_source_session_row", fail_read)
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body())

    assert env["rec"].status == 500
    assert "/private/secret" not in env["rec"].error()
    assert "<path>" in env["rec"].error()


def test_sidecar_store_errors_are_sanitized(resume_env, monkeypatch):
    _make_state_db(_alpha_db(resume_env), sid="sess-alpha-1")
    env = resume_env

    def fail_store(*_args, **_kwargs):
        raise OSError("permission denied: /private/secret/webui/sess-alpha-1.json")

    monkeypatch.setattr(env["routes"], "import_cli_session", fail_store)
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body())

    assert env["rec"].status == 500
    assert "/private/secret" not in env["rec"].error()
    assert "<path>" in env["rec"].error()


def test_transcript_read_errors_fail_closed_and_are_sanitized(resume_env, monkeypatch):
    _make_state_db(_alpha_db(resume_env), sid="sess-alpha-1")
    env = resume_env

    def fail_messages(*_args, **_kwargs):
        raise sqlite3.OperationalError("cannot read /private/secret/profile/state.db")

    monkeypatch.setattr(env["routes"], "get_state_db_session_messages", fail_messages)
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body())

    assert env["rec"].status == 500
    assert "/private/secret" not in env["rec"].error()
    assert "<path>" in env["rec"].error()
    assert not (env["sessions_dir"] / "sess-alpha-1.json").exists()


@pytest.mark.parametrize("replacement_sql", [
    "DROP TABLE messages",
    "DROP TABLE messages; CREATE TABLE messages (role TEXT, content TEXT)",
])
def test_missing_transcript_schema_fails_closed(resume_env, replacement_sql):
    db_path = _alpha_db(resume_env)
    _make_state_db(db_path, sid="sess-alpha-1")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(replacement_sql)
        conn.commit()
    finally:
        conn.close()

    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body())

    assert env["rec"].status == 500
    assert not (env["sessions_dir"] / "sess-alpha-1.json").exists()


def test_bad_source_rejected(resume_env):
    _make_state_db(_alpha_db(resume_env), sid="sess-alpha-1", source="telegram",
                   session_source="telegram")
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body())
    assert env["rec"].status == 403
    assert "not resumable" in env["rec"].error()


@pytest.mark.parametrize("source", ["cli", "tui", "acp", "desktop"])
def test_allowed_sources_pass_the_source_gate(resume_env, source):
    _make_state_db(_alpha_db(resume_env), sid="sess-alpha-1", source=source,
                   session_source=source)
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body())
    assert env["rec"].status == 200, env["rec"].error()
    assert env["rec"].payload()["resumed"] is True


def test_active_unended_source_rejected(resume_env):
    _make_state_db(_alpha_db(resume_env), sid="sess-alpha-1",
                   ended_at=None, end_reason=None)
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body())
    assert env["rec"].status == 409
    assert "appears active" in env["rec"].error()


# ---------------------------------------------------------------------------
# Gate: lineage
# ---------------------------------------------------------------------------


def test_lineage_tip_mismatch_rejected(resume_env):
    _make_state_db(_alpha_db(resume_env), sid="sess-alpha-1")
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(
        env["rec"], _body(root="sess-alpha-1", tip="some-other-tip"))
    assert env["rec"].status == 409
    assert "lineage" in env["rec"].error()


def test_lineage_root_mismatch_rejected(resume_env):
    _make_state_db(_alpha_db(resume_env), sid="sess-linear-tip",
                   parent_session_id="sess-linear-root")
    _make_state_db(_alpha_db(resume_env), sid="sess-linear-root",
                   ended_at=1700000000.0, end_reason="cli_close")
    env = resume_env
    # correct root/tip is (root, tip); send a bogus root.
    env["routes"]._handle_session_resume_in_webui(
        env["rec"],
        _body(sid="sess-linear-tip", root="wrong-root", tip="sess-linear-tip"),
    )
    assert env["rec"].status == 409


def test_lineage_continuation_happy_path(resume_env):
    db = _alpha_db(resume_env)
    _make_state_db(db, sid="sess-root", started_at=1700000000.0,
                   ended_at=1700000050.0, end_reason="compression")
    _make_state_db(db, sid="sess-tip", parent_session_id="sess-root",
                   started_at=1700000060.0, ended_at=1700000100.0,
                   end_reason="cli_close")
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(
        env["rec"], _body(sid="sess-tip", root="sess-root", tip="sess-tip"))
    assert env["rec"].status == 200, env["rec"].error()
    assert env["rec"].payload()["resumed"] is True
    saved = env["models"].Session.load("sess-tip")
    assert saved is not None
    assert len(saved.messages) == 6


def test_lineage_without_started_at_imports_every_validated_segment(resume_env):
    db = _alpha_db(resume_env)
    _make_state_db(db, sid="sess-root", ended_at=None,
                   end_reason="compression")
    _make_state_db(db, sid="sess-tip", parent_session_id="sess-root",
                   ended_at=1700000100.0, end_reason="cli_close")
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("ALTER TABLE sessions DROP COLUMN started_at")
        conn.commit()
    finally:
        conn.close()

    env = resume_env
    env["routes"]._handle_session_resume_in_webui(
        env["rec"], _body(sid="sess-tip", root="sess-root", tip="sess-tip"))

    assert env["rec"].status == 200, env["rec"].error()
    saved = env["models"].Session.load("sess-tip")
    assert saved is not None
    assert len(saved.messages) == 6


def test_obsolete_ancestor_with_continuation_is_rejected(resume_env):
    db = _alpha_db(resume_env)
    _make_state_db(db, sid="sess-root", started_at=1700000000.0,
                   ended_at=1700000050.0, end_reason="compression")
    _make_state_db(db, sid="sess-tip", parent_session_id="sess-root",
                   started_at=1700000060.0, ended_at=1700000100.0,
                   end_reason="cli_close")
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(
        env["rec"], _body(sid="sess-root", root="sess-root", tip="sess-root"))

    assert env["rec"].status == 409
    assert "current tip" in env["rec"].error()
    assert not (env["sessions_dir"] / "sess-root.json").exists()


# ---------------------------------------------------------------------------
# Happy path + source-db immutability
# ---------------------------------------------------------------------------


def test_happy_path_materialises_writable_sidecar_and_leaves_source_untouched(resume_env):
    sid = "sess-alpha-1"
    db = _alpha_db(resume_env)
    _make_state_db(db, sid=sid, source="cli", session_source="cli",
                   title="A finished CLI chat", model="m1", messages=4)

    before = _sha256(db)
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 200, env["rec"].error()
    payload = env["rec"].payload()
    assert payload["ok"] is True
    assert payload["resumed"] is True
    assert payload["idempotent"] is False
    assert payload["session"]["session_id"] == sid
    assert payload["session"]["profile"] == "alpha"

    # Same sid, materialised as a writable sidecar bound to the profile with
    # the source metadata + messages preserved.
    sidecar = env["sessions_dir"] / f"{sid}.json"
    assert sidecar.exists()
    saved = env["models"].Session.load(sid)
    assert saved is not None
    assert saved.profile == "alpha"
    assert saved.read_only is False
    assert saved.is_cli_session is True
    assert saved.source_tag == "cli"
    assert saved.raw_source == "cli"
    assert saved.title == "A finished CLI chat"
    assert saved.workspace == str(Path("/tmp/ws").resolve())
    assert saved.created_workspace == str(Path("/tmp/ws").resolve())
    assert len(saved.messages) == 4
    assert saved.resume_source_profile == "alpha"
    assert saved.resume_source_state_db == str(db.resolve())
    assert saved.resume_lineage_root_id == sid
    assert saved.resume_lineage_tip_id == sid

    # The endpoint must not mutate the source state.db.
    assert _sha256(db) == before


def test_resume_source_snapshot_reuses_one_explicit_connection(resume_env, monkeypatch):
    env = resume_env
    sid = "snapshot-one-connection"
    db_path = _alpha_db(env)
    _make_state_db(db_path, sid=sid)

    routes = env["routes"]
    seen_connections = []
    real_row = routes._read_source_session_row
    real_lineage = routes.read_session_lineage_report
    real_messages = routes.get_state_db_session_messages

    def capture_row(*args, **kwargs):
        seen_connections.append(kwargs.get("connection"))
        return real_row(*args, **kwargs)

    def capture_lineage(*args, **kwargs):
        seen_connections.append(kwargs.get("connection"))
        return real_lineage(*args, **kwargs)

    def capture_messages(*args, **kwargs):
        seen_connections.append(kwargs.get("connection"))
        return real_messages(*args, **kwargs)

    monkeypatch.setattr(routes, "_read_source_session_row", capture_row)
    monkeypatch.setattr(routes, "read_session_lineage_report", capture_lineage)
    monkeypatch.setattr(routes, "get_state_db_session_messages", capture_messages)

    source_row, report, messages = routes._read_resume_source_snapshot(db_path, sid, "alpha")

    assert source_row["id"] == sid
    assert report["tip_session_id"] == sid
    assert messages
    assert seen_connections[0] is not None
    assert all(conn is seen_connections[0] for conn in seen_connections)


def test_source_change_before_publication_is_rejected(resume_env, monkeypatch):
    env = resume_env
    sid = "source-changed-before-publish"
    db_path = _alpha_db(env)
    _make_state_db(db_path, sid=sid)
    routes = env["routes"]
    real_snapshot = routes._read_resume_source_snapshot
    calls = 0

    def mutate_before_second_snapshot(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                    (sid, "assistant", "late write", 1700000200.0),
                )
                conn.execute(
                    "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                    (sid,),
                )
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(routes, "_read_resume_source_snapshot", mutate_before_second_snapshot)
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 409
    assert "changed" in env["rec"].error()
    assert not (env["sessions_dir"] / f"{sid}.json").exists()


def test_failed_post_publish_verification_quarantines_new_sidecar(resume_env, monkeypatch):
    env = resume_env
    sid = "post-publish-verification-failure"
    _make_state_db(_alpha_db(env), sid=sid)
    routes = env["routes"]
    models = env["models"]
    real_write_index = models._write_session_index
    full_rebuilds = []

    def record_index_write(updates=None, **kwargs):
        if updates is None:
            full_rebuilds.append(True)
        return real_write_index(updates=updates, **kwargs)

    monkeypatch.setattr(models, "_write_session_index", record_index_write)
    monkeypatch.setattr(routes, "_load_resume_sidecar_nonmutating", lambda _path: object())
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    sidecar = env["sessions_dir"] / f"{sid}.json"
    quarantined = list((env["sessions_dir"] / ".resume-quarantine").glob(f"{sid}-*.json"))
    assert env["rec"].status == 500
    assert not sidecar.exists()
    assert len(quarantined) == 1
    assert full_rebuilds == [], "route quarantine rebuilt the complete session index"


def test_happy_path_rejects_confirm_false_leaves_no_sidecar(resume_env):
    sid = "sess-alpha-1"
    _make_state_db(_alpha_db(resume_env), sid=sid)
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body(sid=sid, confirm=False))
    assert env["rec"].status == 400
    assert not (env["sessions_dir"] / f"{sid}.json").exists()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_second_resume(resume_env):
    sid = "sess-alpha-1"
    db = _alpha_db(resume_env)
    _make_state_db(db, sid=sid, messages=3)
    env = resume_env
    routes = env["routes"]

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 200
    assert env["rec"].payload()["resumed"] is True

    sidecar = env["sessions_dir"] / f"{sid}.json"
    first_bytes = sidecar.read_bytes()
    db_before = _sha256(db)

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 200
    second = env["rec"].payload()
    assert second["resumed"] is False
    assert second["idempotent"] is True
    assert second["session"]["session_id"] == sid

    # A re-resume must not rewrite/duplicate the sidecar.
    assert sidecar.read_bytes() == first_bytes
    assert _sha256(db) == db_before


def test_controlled_post_resume_turn_continues_same_state_db_session_only(resume_env):
    """Exercise Hermes' production message persistence primitive after resume.

    The resume itself must leave the copied/synthetic state.db byte-identical.
    A subsequent controlled turn then appends two messages under the SAME source
    session id, creates no duplicate session row, and leaves a control session
    (its row and messages) byte-for-byte unchanged.
    """
    hermes_state = pytest.importorskip("hermes_state")
    env = resume_env
    db_path = _alpha_db(env)
    sid = "sess-controlled-resume"
    control_sid = "sess-control-untouched"

    db = hermes_state.SessionDB(db_path)
    try:
        db.create_session(sid, "cli", model="test-model", cwd="/tmp/ws")
        db.set_session_title(sid, "Controlled resume")
        db.append_message(sid, "user", "before resume")
        db.end_session(sid, "cli_close")

        db.create_session(control_sid, "cli", model="test-model", cwd="/tmp/ws")
        db.set_session_title(control_sid, "Control")
        db.append_message(control_sid, "user", "must remain unchanged")
        db.end_session(control_sid, "cli_close")
    finally:
        db.close()

    def snapshot_control():
        conn = sqlite3.connect(str(db_path))
        try:
            session_row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (control_sid,)
            ).fetchone()
            message_rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (control_sid,)
            ).fetchall()
            target_count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)
            ).fetchone()[0]
            return session_row, message_rows, target_count
        finally:
            conn.close()

    control_before, control_messages_before, target_before = snapshot_control()
    db_hash_before_resume = _sha256(db_path)

    env["routes"]._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 200, env["rec"].error()
    assert env["rec"].payload()["session"]["session_id"] == sid
    assert _sha256(db_path) == db_hash_before_resume

    # Same primitive used by Hermes Agent for the resumed turn's persisted
    # user/assistant messages; there is deliberately no model/network call.
    db = hermes_state.SessionDB(db_path)
    try:
        db.append_message(sid, "user", "continued in WebUI")
        db.append_message(sid, "assistant", "controlled response")
    finally:
        db.close()

    control_after, control_messages_after, target_after = snapshot_control()
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = ?", (sid,)
        ).fetchone()[0] == 1
        changed_session_ids = {
            row[0]
            for row in conn.execute(
                "SELECT session_id FROM messages GROUP BY session_id "
                "HAVING session_id = ? AND COUNT(*) = ?",
                (sid, target_before + 2),
            ).fetchall()
        }
    finally:
        conn.close()

    assert target_after == target_before + 2
    assert changed_session_ids == {sid}
    assert control_after == control_before
    assert control_messages_after == control_messages_before


# ---------------------------------------------------------------------------
# Flat sidecar collision (the WebUI store is not profile-qualified)
# ---------------------------------------------------------------------------


def test_cross_profile_same_sid_collision_refused(resume_env, monkeypatch):
    sid = "sess-shared-id"
    _make_state_db(_alpha_db(resume_env), sid=sid)
    env = resume_env
    # A sidecar for the SAME sid already exists, owned by another profile.
    other = env["models"].Session(session_id=sid, profile="beta", messages=[])
    other.save(touch_updated_at=False)
    sidecar = env["sessions_dir"] / f"{sid}.json"
    before = _sha256(sidecar)

    def mutating_load_must_not_run(*_args, **_kwargs):
        raise AssertionError("resume collision check called mutating Session.load")

    monkeypatch.setattr(env["models"].Session, "load", mutating_load_must_not_run)
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 409
    assert "already owns" in env["rec"].error()

    # The foreign sidecar is byte-identical; collision inspection is pure read.
    assert _sha256(sidecar) == before


def test_competing_sidecar_writer_cannot_overwrite_resumed_owner(resume_env):
    sid = "sess-competing-owner"
    _make_state_db(_alpha_db(resume_env), sid=sid)
    env = resume_env
    routes = env["routes"]
    sidecar = env["sessions_dir"] / f"{sid}.json"

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 200, env["rec"].error()
    resumed_bytes = sidecar.read_bytes()

    competing = env["models"].Session(
        session_id=sid,
        profile="beta",
        title="Competing owner",
        messages=[],
    )
    with pytest.raises(PermissionError, match="owned by profile"):
        competing.save(touch_updated_at=False)

    assert sidecar.read_bytes() == resumed_bytes
    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    assert persisted["profile"] == "alpha"
    assert persisted["resume_source_profile"] == "alpha"


def test_blank_profile_sidecar_collision_refused(resume_env):
    sid = "sess-blank-owner"
    _make_state_db(_alpha_db(resume_env), sid=sid)
    env = resume_env
    blank = env["models"].Session(session_id=sid, profile=None, messages=[])
    blank.save(touch_updated_at=False)

    env["routes"]._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 409
    assert "already owns" in env["rec"].error()


def test_same_profile_unmarked_sidecar_is_not_treated_as_idempotent(resume_env):
    sid = "sess-existing-unmarked"
    _make_state_db(_alpha_db(resume_env), sid=sid)
    env = resume_env
    existing = env["models"].Session(session_id=sid, profile="alpha", messages=[])
    existing.save(touch_updated_at=False)

    env["routes"]._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 409
    assert "resume identity" in env["rec"].error()


# ---------------------------------------------------------------------------
# Regression: ordinary import / projection stays read-only
# ---------------------------------------------------------------------------


def test_existing_readonly_projection_regression(resume_env, monkeypatch):
    """With the operator read-only flag set, the ordinary claim path must still
    refuse to materialise a writable sidecar (i.e. resume_in_webui did not
    loosen the default projection)."""
    monkeypatch.setenv("HERMES_WEBUI_EXTERNAL_STATE_READ_ONLY", "1")
    sid = "sess-readonly-projection"
    _make_state_db(_alpha_db(resume_env), sid=sid, source="cli",
                   session_source="cli", messages=3)
    routes = resume_env["routes"]

    session, reason = routes._claim_or_synthesize_cli_session(sid)
    assert reason == "not_claimable"
    assert session is not None
    assert session.read_only is True
    # No writable sidecar may have been created by the projection.
    assert not (resume_env["sessions_dir"] / f"{sid}.json").exists()


def test_resume_flag_gate_independent_of_readonly_projection(resume_env, monkeypatch):
    """The resume endpoint works regardless of the read-only projection flag,
    but only through its explicit operator allowlist."""
    monkeypatch.setenv("HERMES_WEBUI_EXTERNAL_STATE_READ_ONLY", "1")
    sid = "sess-explicit-resume"
    _make_state_db(_alpha_db(resume_env), sid=sid, messages=2)
    env = resume_env
    env["routes"]._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 200, env["rec"].error()
    assert env["rec"].payload()["resumed"] is True


# ---------------------------------------------------------------------------
# Route wiring (the endpoint is actually reachable through handle_post)
# ---------------------------------------------------------------------------


class _DispatchHandler:
    def __init__(self, path):
        self.status = None
        self.headers = {"Content-Type": "application/json", "Content-Length": "1"}
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.command = "POST"
        self.path = path
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


def test_endpoint_reachable_through_handle_post(resume_env, monkeypatch):
    sid = "sess-dispatch-1"
    _make_state_db(_alpha_db(resume_env), sid=sid)
    env = resume_env
    monkeypatch.setattr(env["routes"], "_check_csrf", lambda _h: True)
    monkeypatch.setattr(env["routes"], "read_body", lambda _h: _body(sid=sid))

    path = "/api/session/resume_in_webui"
    env["routes"].handle_post(_DispatchHandler(path), urlparse(path))

    assert env["rec"].status == 200, env["rec"].error()
    assert env["rec"].payload()["resumed"] is True
    assert (env["sessions_dir"] / f"{sid}.json").exists()


# ---------------------------------------------------------------------------
# Atomic first publication (#765 follow-up: no writer lock, exclusive claim)
# ---------------------------------------------------------------------------


def _install_thread_recorders(routes, monkeypatch):
    """Route ``j``/``bad`` to the *calling thread's* recorder.

    The shared ``resume_env`` recorder cannot observe two concurrent requests,
    so concurrency tests give each worker its own ``_Recorder`` via a
    thread-local. The worker must assign ``local.rec`` before calling.
    """
    local = threading.local()

    def fake_bad(_handler, msg, status=400):
        local.rec.bad = (msg, status)
        return None

    def fake_j(_handler, payload, status=200, extra_headers=None, *, pretty=True):
        local.rec.j = (payload, status)
        return None

    monkeypatch.setattr(routes, "bad", fake_bad)
    monkeypatch.setattr(routes, "j", fake_j)
    return local


def test_first_publication_claims_the_sidecar_with_an_exclusive_link(resume_env, monkeypatch):
    """First publication must be an exclusive claim, never a clobbering rename."""
    sid = "sess-exclusive-claim"
    _make_state_db(_alpha_db(resume_env), sid=sid, messages=3)
    env = resume_env
    models = env["models"]
    sidecar = env["sessions_dir"] / f"{sid}.json"

    links = []
    replaces = []
    real_link = models.os.link
    real_replace = models.os.replace

    def recording_link(src, dst):
        links.append((str(src), str(dst)))
        return real_link(src, dst)

    def recording_replace(src, dst):
        replaces.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(models.os, "link", recording_link)
    monkeypatch.setattr(models.os, "replace", recording_replace)

    env["routes"]._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 200, env["rec"].error()
    # Exactly one claim, from the private staging namespace onto the canonical id.
    assert [dst for _src, dst in links] == [str(sidecar)]
    staging_src = Path(links[0][0])
    assert staging_src.parent == env["sessions_dir"] / ".resume-staging"
    assert staging_src.name.startswith(f"{sid}.")
    # The canonical path is never *created* or clobbered by a rename: that
    # would be the check-then-replace race the exclusive claim replaces. F2/F3
    # add exactly one later in-place rewrite onto the canonical: the explicit
    # verified-marker commit, which happens only after the claim succeeded and
    # final source re-verification passed (never a foreign writer's rename).
    canonical_replaces = [(src, dst) for src, dst in replaces if dst == str(sidecar)]
    assert len(canonical_replaces) <= 1, canonical_replaces
    for src, _dst in canonical_replaces:
        assert Path(src).parent == env["sessions_dir"], src
    # The staged artifact is consumed by the claim, not left behind.
    assert not list((env["sessions_dir"] / ".resume-staging").glob("*.json"))
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["session_id"] == sid
    assert len(data["messages"]) == 3
    assert data["resume_source_profile"] == "alpha"


def test_readers_never_observe_a_partial_canonical_sidecar(resume_env, monkeypatch):
    """The canonical id stays invisible until the verified payload lands."""
    sid = "sess-atomic-visibility"
    _make_state_db(_alpha_db(resume_env), sid=sid, messages=3)
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sidecar = env["sessions_dir"] / f"{sid}.json"

    entered = threading.Event()
    release = threading.Event()
    observations = []
    real_link = models.os.link

    def gated_link(src, dst):
        observations.append(
            {
                "canonical_visible_before_claim": Path(dst).exists(),
                "staged_payload": json.loads(Path(src).read_text(encoding="utf-8")),
            }
        )
        entered.set()
        assert release.wait(timeout=5)
        return real_link(src, dst)

    monkeypatch.setattr(models.os, "link", gated_link)

    worker = threading.Thread(
        target=routes._handle_session_resume_in_webui,
        args=(env["rec"], _body(sid=sid)),
        daemon=True,
    )
    worker.start()
    assert entered.wait(timeout=5)
    try:
        # Parked at the claim: a concurrent reader still sees no writable sidecar
        # at the canonical path (the staged file lives only in the private
        # namespace), so a partially published artifact can never be observed.
        assert not sidecar.exists()
        assert models.Session.load(sid) is None
        assert observations[0]["canonical_visible_before_claim"] is False
        # The payload that is about to become visible is already complete.
        assert observations[0]["staged_payload"]["session_id"] == sid
        assert len(observations[0]["staged_payload"]["messages"]) == 3
    finally:
        release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert env["rec"].status == 200, env["rec"].error()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["session_id"] == sid
    assert len(data["messages"]) == 3


def test_concurrent_resume_requests_publish_exactly_one_sidecar(resume_env, monkeypatch):
    """Two simultaneous Resumes must publish once and stay free of writer locks."""
    sid = "sess-concurrent-resume"
    _make_state_db(_alpha_db(resume_env), sid=sid, messages=3)
    env = resume_env
    routes = env["routes"]
    sidecar = env["sessions_dir"] / f"{sid}.json"

    barrier = threading.Barrier(2)
    real_stage = routes.stage_session_sidecar

    def gated_stage(session, staging_path):
        staged = real_stage(session, staging_path)
        # Both requests reach the publication boundary together, so the
        # exclusive claim is the only thing that can pick the winner.
        barrier.wait(timeout=5)
        return staged

    monkeypatch.setattr(routes, "stage_session_sidecar", gated_stage)
    local = _install_thread_recorders(routes, monkeypatch)

    results = {}
    errors = []

    def worker(name):
        local.rec = _Recorder()
        try:
            routes._handle_session_resume_in_webui(local.rec, _body(sid=sid))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        results[name] = local.rec

    t1 = threading.Thread(target=worker, args=("a",), daemon=True)
    t2 = threading.Thread(target=worker, args=("b",), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not t1.is_alive() and not t2.is_alive()
    assert not errors, errors
    assert sorted(rec.status for rec in results.values()) == [200, 200], [
        rec.error() for rec in results.values()
    ]
    payloads = [rec.payload() for rec in results.values()]
    # Exactly one request publishes; the other reconciles against the winner's
    # identical identity as an idempotent success instead of failing.
    assert sum(1 for payload in payloads if payload["resumed"] is True) == 1
    assert sum(1 for payload in payloads if payload["idempotent"] is True) == 1

    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["session_id"] == sid
    assert len(data["messages"]) == 3
    assert not list((env["sessions_dir"] / ".resume-staging").glob("*.json"))
    quarantine = env["sessions_dir"] / ".resume-quarantine"
    assert not quarantine.exists() or not list(quarantine.glob("*.json"))


def test_parked_resume_publication_does_not_stall_unrelated_saves(resume_env, monkeypatch):
    """A Resume mid-claim must not block saves of other conversations (#765 F2)."""
    sid = "sess-parked-claim"
    _make_state_db(_alpha_db(resume_env), sid=sid, messages=3)
    env = resume_env
    models = env["models"]
    routes = env["routes"]

    entered = threading.Event()
    release = threading.Event()
    real_link = models.os.link

    def gated_link(src, dst):
        entered.set()
        assert release.wait(timeout=5)
        return real_link(src, dst)

    monkeypatch.setattr(models.os, "link", gated_link)

    worker = threading.Thread(
        target=routes._handle_session_resume_in_webui,
        args=(env["rec"], _body(sid=sid)),
        daemon=True,
    )
    worker.start()
    assert entered.wait(timeout=5)

    unrelated = models.Session(
        session_id="sess-unrelated-save",
        title="Unrelated",
        profile="alpha",
        messages=[{"role": "user", "content": "hello"}],
    )
    replaced = threading.Event()
    real_replace = models.os.replace

    def recording_replace(src, dst):
        if str(dst).endswith("sess-unrelated-save.json"):
            replaced.set()
        return real_replace(src, dst)

    monkeypatch.setattr(models.os, "replace", recording_replace)
    saver = threading.Thread(target=unrelated.save, kwargs={"skip_index": True}, daemon=True)
    saver.start()
    try:
        # The unrelated save must complete while the Resume claim is parked:
        # nothing on the Resume path may serialize unrelated writers.
        assert replaced.wait(timeout=5), (
            "an unrelated session save was blocked by a parked Resume publication"
        )
    finally:
        release.set()
    saver.join(timeout=5)
    worker.join(timeout=5)

    assert not saver.is_alive()
    assert env["rec"].status == 200, env["rec"].error()


def test_publish_refuses_to_clobber_an_occupied_id(resume_env):
    """An occupied canonical id is never overwritten by a lost claim."""
    sid = "sess-occupied-claim"
    _make_state_db(_alpha_db(resume_env), sid=sid)
    env = resume_env
    models = env["models"]
    sidecar = env["sessions_dir"] / f"{sid}.json"
    sidecar.write_text('{"session_id": "occupied"}\n', encoding="utf-8")

    candidate = models.import_cli_session(
        sid,
        "staged candidate",
        [{"role": "user", "content": "hello"}],
        "test-model",
        profile="alpha",
        persist=False,
    )
    staging = env["sessions_dir"] / ".resume-staging" / f"{sid}.stage.json"
    staged = models.stage_session_sidecar(candidate, staging)

    with pytest.raises(FileExistsError):
        models.publish_staged_session_sidecar(staged, staging)

    assert sidecar.read_text(encoding="utf-8") == '{"session_id": "occupied"}\n'
    # A lost claim leaves the staging file for the caller to clean up; nothing
    # was published and nothing was destroyed.
    assert staging.exists()


# ---------------------------------------------------------------------------
# D1 — first-ownership gate: a pre-claim ordinary save can never clobber a
# claimed Resume sidecar (audit probe: ``ordinary-save-race``), while ordinary
# same-id saves still stay lock-free against each other (#765).
# ---------------------------------------------------------------------------


def test_ordinary_save_in_flight_makes_resume_refuse_without_clobbering(resume_env, monkeypatch):
    """A save admitted before the claim must not be silently overwritten.

    Reproduces the audit's ``ordinary-save-race`` probe: an ordinary same-id
    save is parked immediately before ``os.replace`` when Resume arrives. The
    gate must fail closed (409) while a writer is active so that a Resume can
    never return 200 and then have its published sidecar overwritten.
    """
    sid = "sess-pre-claimed-writer"
    _make_state_db(_alpha_db(resume_env), sid=sid, messages=3)
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sidecar = env["sessions_dir"] / f"{sid}.json"

    parked = threading.Event()
    release = threading.Event()
    real_replace = models._safe_replace

    def gated_replace(src, dst):
        if str(dst).endswith(f"{sid}.json"):
            parked.set()
            assert release.wait(timeout=5)
        return real_replace(src, dst)

    monkeypatch.setattr(models, "_safe_replace", gated_replace)

    writer = models.Session(
        session_id=sid,
        title="Ordinary writer",
        profile="alpha",
        messages=[{"role": "user", "content": "writer"}],
    )
    writer_errors = []

    def run_writer():
        try:
            writer.save(skip_index=True)
        except Exception as exc:  # pragma: no cover - asserted below
            writer_errors.append(exc)

    saver = threading.Thread(target=run_writer, daemon=True)
    saver.start()
    assert parked.wait(timeout=5), "the ordinary save never reached os.replace"

    # Resume arrives while the writer is in flight: it must refuse, never
    # publish a sidecar that a stale writer can then overwrite.
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 409, env["rec"].error()
    assert "in flight" in env["rec"].error()
    assert not sidecar.exists()

    release.set()
    saver.join(timeout=5)

    assert not saver.is_alive()
    assert not writer_errors, writer_errors
    published = json.loads(sidecar.read_text(encoding="utf-8"))
    assert published["session_id"] == sid
    assert published["profile"] == "alpha"
    # Nothing claims a resume identity for this sidecar, and no gate state leaks.
    assert not published.get("resume_source_profile")


def test_claim_gate_state_is_released_after_success(resume_env):
    """The gate must not leak per-session state once both sides finish."""
    sid = "sess-gate-release"
    _make_state_db(_alpha_db(resume_env), sid=sid, messages=2)
    env = resume_env
    models = env["models"]

    env["routes"]._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 200, env["rec"].error()

    assert sid not in models._SESSION_CLAIM_STATE
    # A later ordinary save of the claimed sidecar is admitted again.
    saved = models.Session.load(sid)
    assert saved is not None
    saved.messages.append({"role": "user", "content": "after resume"})
    saved.save(skip_index=True)
    assert sid not in models._SESSION_CLAIM_STATE


def test_active_resume_claim_blocks_a_new_same_id_save(resume_env, monkeypatch):
    """While a Resume claim is held, a new same-id save must refuse to write."""
    sid = "sess-claim-blocks-save"
    _make_state_db(_alpha_db(resume_env), sid=sid, messages=3)
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sidecar = env["sessions_dir"] / f"{sid}.json"

    entered = threading.Event()
    release = threading.Event()
    real_link = models.os.link

    def gated_link(src, dst):
        entered.set()
        assert release.wait(timeout=5)
        return real_link(src, dst)

    monkeypatch.setattr(models.os, "link", gated_link)

    worker = threading.Thread(
        target=routes._handle_session_resume_in_webui,
        args=(env["rec"], _body(sid=sid)),
        daemon=True,
    )
    worker.start()
    assert entered.wait(timeout=5)

    competing = models.Session(
        session_id=sid,
        title="late save",
        profile="alpha",
        messages=[{"role": "user", "content": "late"}],
    )
    with pytest.raises(PermissionError):
        competing.save(skip_index=True)

    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert env["rec"].status == 200, env["rec"].error()
    published = json.loads(sidecar.read_text(encoding="utf-8"))
    assert published["resume_source_profile"] == "alpha"
    assert len(published["messages"]) == 3
    assert sid not in models._SESSION_CLAIM_STATE


def test_concurrent_same_id_ordinary_saves_still_reach_replace_in_parallel(resume_env, monkeypatch):
    """#765: the gate is a count, so two same-id saves still race in parallel."""
    sid = "sess-lock-free-pair"
    env = resume_env
    models = env["models"]

    barrier = threading.Barrier(2)
    both_replaced = []
    real_replace = models._safe_replace

    def gated_replace(src, dst):
        if str(dst).endswith(f"{sid}.json"):
            both_replaced.append(threading.get_ident())
            barrier.wait(timeout=5)
        return real_replace(src, dst)

    monkeypatch.setattr(models, "_safe_replace", gated_replace)

    errors = []

    def run_writer(n):
        session = models.Session(
            session_id=sid,
            title=f"writer {n}",
            profile="alpha",
            messages=[{"role": "user", "content": f"m{n}"}],
        )
        try:
            session.save(skip_index=True)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=run_writer, args=(n,), daemon=True) for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    # Both writers reached os.replace and rendezvoused there: the gate never
    # serialized them.
    assert len(both_replaced) == 2
    assert sid not in models._SESSION_CLAIM_STATE


# ---------------------------------------------------------------------------
# D2/D3/D4 — fail-closed post-publish verification, quarantine and cache/index
# eviction (audit probes: ``late-source-change``, ``unreadable-publication``,
# ``cached-quarantine``).
# ---------------------------------------------------------------------------


def test_source_change_after_final_snapshot_is_quarantined_fail_closed(resume_env, monkeypatch):
    """A source that changes during publication must not yield a 200 + stale sidecar."""
    env = resume_env
    sid = "late-source-change"
    db_path = _alpha_db(env)
    _make_state_db(db_path, sid=sid, messages=3)
    routes = env["routes"]
    sidecar = env["sessions_dir"] / f"{sid}.json"
    real_stage = routes.stage_session_sidecar
    mutated = []

    def mutate_during_stage(session, staging_path):
        staged = real_stage(session, staging_path)
        if not mutated:
            mutated.append(True)
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                    (sid, "assistant", "late write", 1700000200.0),
                )
                conn.execute(
                    "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                    (sid,),
                )
        return staged

    monkeypatch.setattr(routes, "stage_session_sidecar", mutate_during_stage)
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert mutated, "the source mutation never ran"
    assert env["rec"].status == 500, env["rec"].error()
    assert not sidecar.exists()
    quarantined = list((env["sessions_dir"] / ".resume-quarantine").glob(f"{sid}-*.json"))
    assert len(quarantined) == 1
    assert sid not in (env["sessions_dir"] / "_index.json").read_text(encoding="utf-8")


def test_unreadable_published_sidecar_is_quarantined_and_deindexed(resume_env, monkeypatch):
    """A just-published sidecar that cannot be parsed must not stay live/indexed."""
    env = resume_env
    sid = "unreadable-publication"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    routes = env["routes"]
    sidecar = env["sessions_dir"] / f"{sid}.json"
    real_publish = routes.publish_staged_session_sidecar

    def publish_then_corrupt(session, staging_path):
        real_publish(session, staging_path)
        sidecar.write_text("{not valid json", encoding="utf-8")

    monkeypatch.setattr(routes, "publish_staged_session_sidecar", publish_then_corrupt)
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 500, env["rec"].error()
    assert not sidecar.exists()
    quarantined = list((env["sessions_dir"] / ".resume-quarantine").glob(f"{sid}-*.json"))
    assert len(quarantined) == 1
    index_text = (env["sessions_dir"] / "_index.json").read_text(encoding="utf-8")
    assert sid not in index_text


def test_quarantine_evicts_cached_writable_sidecar(resume_env, monkeypatch):
    """F3: a provisional publication is never exposed writable, even mid-publish.

    The pre-fix D5 probe asserted a *writable cached object* existed during the
    publication window and that quarantine evicted it. That assertion encoded
    the very F3 defect: a delayed reader could adopt an unverified publication
    and, if the quarantine move failed, keep writing through it. The invariant
    is now stronger and checked at the worst possible moment (inside publish):
    no reader may resolve a provisional publication, nothing is cached, and the
    quarantine denial is durable.
    """
    env = resume_env
    sid = "cached-quarantine"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    routes = env["routes"]
    models = env["models"]
    sidecar = env["sessions_dir"] / f"{sid}.json"
    real_publish = routes.publish_staged_session_sidecar
    probe = {}

    def publish_then_probe(session, staging_path):
        real_publish(session, staging_path)
        # The canonical is now PROVISIONAL: any reader must fail closed.
        try:
            models.get_session(sid)
            probe["writable"] = True
        except KeyError:
            probe["writable"] = False
        probe["cached"] = sid in models.SESSIONS
        probe["state"] = json.loads(sidecar.read_text(encoding="utf-8"))["resume_publication_state"]

    monkeypatch.setattr(routes, "publish_staged_session_sidecar", publish_then_probe)
    monkeypatch.setattr(routes, "_load_resume_sidecar_nonmutating", lambda _path: object())
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert probe.get("state") == "provisional"
    assert probe.get("writable") is False, "provisional publication was served writable"
    assert probe.get("cached") is False, "provisional publication was cached as writable"
    assert env["rec"].status == 500, env["rec"].error()
    assert not sidecar.exists()
    # The stale writable object is gone from the cache and cannot be re-resolved.
    assert sid not in models.SESSIONS
    assert models.Session.load(sid) is None
    # Denial is durable on disk, so even a fresh process resolution fails closed.
    assert models.is_resume_publication_denied(sid)
    assert sid not in (env["sessions_dir"] / "_index.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# D4 (identity) — an idempotent re-resume must match the sidecar's own
# ``session_id``, not just its resume identity (audit probe: ``sid-identity``).
# ---------------------------------------------------------------------------


def test_idempotent_resume_rejects_sidecar_with_foreign_session_id(resume_env):
    """A hand-edited sidecar naming another session is never an idempotent match."""
    env = resume_env
    sid = "sid-identity-mismatch"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    routes = env["routes"]
    sidecar = env["sessions_dir"] / f"{sid}.json"

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 200, env["rec"].error()

    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["session_id"] = "some-other-session"
    sidecar.write_text(json.dumps(data), encoding="utf-8")

    # The fixture's recorder is captured by the patched j()/bad(); reset it so
    # only the second request's outcome is observed.
    env["rec"].bad = None
    env["rec"].j = None
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 409, env["rec"].error()
    assert env["rec"].bad is not None
    assert "identity" in (env["rec"].error() or "")
    # The foreign payload is left untouched, not silently adopted.
    assert json.loads(sidecar.read_text(encoding="utf-8"))["session_id"] == "some-other-session"


# ---------------------------------------------------------------------------
# F1–F4 (Astra re-audit 94bfe8af) — explicit verified ownership and durable
# quarantine fencing.  Each test mirrors one independent probe schedule:
# index-error-with-source-change, quarantine-move-failure,
# unreadable-canonical-with-publish-error, second-Resume-while-provisional and
# delayed-cache-fill.
# ---------------------------------------------------------------------------


def _write_provisional_canonical(env, sid, *, messages=3):
    """Leave the exact on-disk state a first Resume holds before committing.

    Reproduces the PROVISIONAL canonical (and nothing else) without running the
    handler, which would keep the cross-process resume claim for the duration.
    """
    models = env["models"]
    session = models.Session(
        session_id=sid,
        profile="alpha",
        messages=[{"role": "user", "content": f"m{i}"} for i in range(messages)],
        read_only=False,
    )
    session.resume_source_profile = "alpha"
    session.resume_source_state_db = str(_alpha_db(env))
    session.resume_lineage_root_id = sid
    session.resume_lineage_tip_id = sid
    session.resume_publication_state = models.RESUME_PUBLICATION_PROVISIONAL
    session.save()
    return env["sessions_dir"] / f"{sid}.json"


def _mutate_source(db: Path, sid: str):
    """Mutate the source row after publication (the D2 "source moved" race)."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?",
            (sid,),
        )
        conn.commit()
    finally:
        conn.close()


def test_provisional_marker_precedes_verified_commit(resume_env, monkeypatch):
    """F2/F3: the canonical is PROVISIONAL until an explicit verified commit.

    Pins the two-phase ownership boundary: a reader observing the canonical
    before ``mark_resume_publication_verified`` must see ``provisional``, and
    only the explicit commit flips it to ``verified``.
    """
    env = resume_env
    sid = "phase-boundary"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    routes = env["routes"]
    models = env["models"]
    sidecar = env["sessions_dir"] / f"{sid}.json"
    seen = []

    real_mark = models.mark_resume_publication_verified

    def observe_then_mark(session):
        seen.append(json.loads(sidecar.read_text(encoding="utf-8"))["resume_publication_state"])
        return real_mark(session)

    monkeypatch.setattr(models, "mark_resume_publication_verified", observe_then_mark)
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 200, env["rec"].error()
    assert seen == ["provisional"]
    final = json.loads(sidecar.read_text(encoding="utf-8"))
    assert final["resume_publication_state"] == "verified"
    # Only a verified, non-denied publication is resolvable as writable.
    resolved = models.get_session(sid)
    assert resolved is not None
    assert models.session_publication_admissible(resolved)
    assert not models.is_resume_publication_denied(sid)


def test_second_resume_sees_provisional_publication_and_fails_closed(resume_env, monkeypatch):
    """F2: a competing Resume must never return idempotent success on a
    provisional first publication; it waits briefly then fails closed."""
    env = resume_env
    sid = "competing-provisional"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    routes = env["routes"]
    models = env["models"]
    sidecar = _write_provisional_canonical(env, sid)

    # A provisional publication is invisible to every reader.
    with pytest.raises(KeyError):
        models.get_session(sid)

    monkeypatch.setattr(routes, "_await_resume_publication_commit", lambda *a, **k: None)

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 409, env["rec"].error()
    assert env["rec"].bad is not None
    assert "still being verified" in (env["rec"].error() or "")
    # Never adopted as an idempotent re-resume, and the artifact is untouched.
    assert sidecar.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["resume_publication_state"] == "provisional"


def test_second_resume_waits_for_a_committed_first_publication(resume_env, monkeypatch):
    """F2: a committed (verified) first publication is still an idempotent match."""
    env = resume_env
    sid = "competing-committed"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    routes = env["routes"]
    models = env["models"]
    _write_provisional_canonical(env, sid)
    # Commit the verified marker directly (as the winning publisher would).
    session = models.Session.load(sid)
    models.mark_resume_publication_verified(session)

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 200, env["rec"].error()
    assert json.loads((env["sessions_dir"] / f"{sid}.json").read_text(encoding="utf-8"))["resume_publication_state"] == "verified"


def test_parked_first_publication_fails_competing_resume_closed(resume_env, monkeypatch):
    """F2 (probe ``unverified-idempotent``): while a first publication is parked
    mid-window, a competing Resume must not report success and must not get a
    writable object."""
    env = resume_env
    sid = "unverified-idempotent"
    db = _alpha_db(env)
    _make_state_db(db, sid=sid, messages=3)
    routes = env["routes"]
    models = env["models"]

    real_publish = routes.publish_staged_session_sidecar
    released = threading.Event()
    parked = threading.Event()

    def parked_publish(session, staging_path):
        real_publish(session, staging_path)
        parked.set()
        released.wait(5.0)

    monkeypatch.setattr(routes, "publish_staged_session_sidecar", parked_publish)

    # Thread-safe outcome capture: each call's own handler object keys its result.
    outcomes = {}

    def dispatch_bad(handler, msg, status=400):
        outcomes[id(handler)] = ("bad", status, msg)
        return None

    def dispatch_j(handler, payload, status=200, extra_headers=None, *, pretty=True):
        outcomes[id(handler)] = ("ok", status, payload)
        return None

    monkeypatch.setattr(routes, "bad", dispatch_bad)
    monkeypatch.setattr(routes, "j", dispatch_j)

    first_handler = object()

    def first_call():
        routes._handle_session_resume_in_webui(first_handler, _body(sid=sid))

    thread = threading.Thread(target=first_call)
    thread.start()
    assert parked.wait(5.0), "first publication never reached the parked window"

    # The first publication is provisional and invisible; the competing Resume
    # must fail closed rather than adopt it, even after the source moves.
    _mutate_source(db, sid)
    second_handler = object()
    routes._handle_session_resume_in_webui(second_handler, _body(sid=sid))
    second = outcomes.get(id(second_handler))
    assert second is not None
    assert second[0] == "bad", second
    assert second[1] != 200, second
    assert second[1] in (409, 500), second

    released.set()
    thread.join(10.0)
    assert not thread.is_alive()
    # The first publication's source moved, so it must end non-200 with no
    # writable canonical left behind.
    first = outcomes.get(id(first_handler))
    assert first is not None and first[0] == "bad", first
    assert first[1] == 500, first
    assert not (env["sessions_dir"] / f"{sid}.json").exists()
    assert models.is_resume_publication_denied(sid)
    with pytest.raises(KeyError):
        models.get_session(sid)


def test_source_change_during_index_write_after_verified_commit_is_safe_degradation(resume_env, monkeypatch):
    """F1 (probe ``index-error-source-change``): the post-publish source re-read
    runs BEFORE the index write, so a source that moves in the index window is
    already covered by the verified commit. This schedule injects NO index
    exception (the real index writer runs and succeeds) — the name says exactly
    what it proves: a post-verification source change observed during the index
    write degrades safely to the committed verified artifact.

    The real index-exception schedules live in
    ``test_real_index_exception_after_verified_commit_keeps_verified_publication``
    and ``test_marker_loss_during_index_failure_is_not_adopted``.
    """
    env = resume_env
    sid = "index-error-source-change"
    db = _alpha_db(env)
    _make_state_db(db, sid=sid, messages=3)
    routes = env["routes"]
    models = env["models"]
    sidecar = env["sessions_dir"] / f"{sid}.json"
    snapshots = []

    real_load = routes._load_resume_sidecar_nonmutating
    real_index = models._write_session_index

    def counting_load(path):
        loaded = real_load(path)
        if loaded is not None:
            snapshots.append(len(getattr(loaded, "messages", []) or []))
        return loaded

    def index_then_mutate(updates=None):
        # The source moves exactly in the index window, after the canonical was
        # linked and read back. Only the post-commit index call carries updates.
        if updates:
            _mutate_source(db, sid)
        return real_index(updates=updates)

    monkeypatch.setattr(routes, "_load_resume_sidecar_nonmutating", counting_load)
    monkeypatch.setattr(models, "_write_session_index", index_then_mutate)

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    # The live re-read happened (3 messages observed from the published
    # canonical) — the source change was caught after publication.
    assert 3 in snapshots
    assert snapshots.count(3) >= 1
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["resume_publication_state"] == "verified"
    assert env["rec"].status == 200, env["rec"].error()


def test_quarantine_move_failure_still_denies_and_deindexes(resume_env, monkeypatch):
    """F1/F3 (probe ``quarantine-move-failure``): final-verification failure is
    terminal even when the quarantine move itself fails."""
    env = resume_env
    sid = "quarantine-move-failure"
    db = _alpha_db(env)
    _make_state_db(db, sid=sid, messages=3)
    routes = env["routes"]
    models = env["models"]
    sidecar = env["sessions_dir"] / f"{sid}.json"

    # Snapshot #1 proves consistency, #2 is the pre-publish boundary re-read,
    # #3 is the POST-publish final re-read: failing #3 is the F1 terminal case.
    real_snapshot = routes._read_resume_source_snapshot
    calls = {"n": 0}

    def flaky_snapshot(*a, **k):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise RuntimeError("source moved during publication")
        return real_snapshot(*a, **k)

    monkeypatch.setattr(routes, "_read_resume_source_snapshot", flaky_snapshot)

    real_replace = routes.os.replace

    def fail_quarantine_move(src, dst):
        if str(Path(dst).parent).endswith(".resume-quarantine"):
            raise OSError("quarantine move failed")
        return real_replace(src, dst)

    monkeypatch.setattr(routes.os, "replace", fail_quarantine_move)

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert calls["n"] >= 2, "the post-publish source re-read never ran"
    assert env["rec"].status == 500, env["rec"].error()
    # The denial tombstone is written BEFORE the (failing) move, so the
    # publication is still inaccessible even though the file remains on disk.
    assert models.is_resume_publication_denied(sid)
    assert sidecar.exists(), "the move was supposed to fail; file should remain"
    with pytest.raises(KeyError):
        models.get_session(sid)
    assert sid not in models.SESSIONS
    loaded = models.Session.load(sid)
    assert loaded is not None and not models.session_publication_admissible(loaded)
    assert sid not in (env["sessions_dir"] / "_index.json").read_text(encoding="utf-8")


def test_unreadable_canonical_with_publish_error_is_quarantined(resume_env, monkeypatch):
    """F1/F3 (probe ``unreadable-with-publish-error``): a post-link error that
    leaves an unreadable canonical must make it inaccessible, never adopt it."""
    env = resume_env
    sid = "resume-quarantine"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    routes = env["routes"]
    models = env["models"]
    sidecar = env["sessions_dir"] / f"{sid}.json"

    def index_corrupt_then_raise(updates=None):
        # Corrupt the canonical in the index window, then fail: this is the
        # schedule where the post-link error leaves an unreadable canonical.
        if sidecar.exists():
            sidecar.write_text("{ not json", encoding="utf-8")
        raise OSError("index write failed after publication")

    monkeypatch.setattr(models, "_write_session_index", index_corrupt_then_raise)

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 500, env["rec"].error()
    assert models.is_resume_publication_denied(sid)
    # The unreadable canonical is gone from the live namespace.
    assert not sidecar.exists()
    assert not list(env["sessions_dir"].glob(f"{sid}.json"))
    with pytest.raises(KeyError):
        models.get_session(sid)
    assert models.Session.load(sid) is None
    assert sid not in (env["sessions_dir"] / "_index.json").read_text(encoding="utf-8")
    # The unreadable canonical was moved out of the live namespace (best
    # effort) and the denial tombstone keeps it inaccessible regardless.
    assert list((env["sessions_dir"] / ".resume-quarantine").glob(f"{sid}*.json"))


def test_provisional_read_during_first_publication_is_rejected(resume_env, monkeypatch):
    """F3 (probe ``cached``): a reader that looks at the canonical while the
    first publication is still PROVISIONAL is refused, and cannot restore a
    writable object or index entry after the eventual quarantine.

    The reader here is synchronous: it runs inside the publish wrapper, i.e. at
    the exact instant the provisional canonical is live, and it gets no
    writable object. (The delayed/barrier schedules that exercise a reader
    parked across a quarantine are
    ``test_reader_parked_after_affirmative_check_cannot_admit_after_rejection``
    and ``test_post_rejection_save_cannot_recreate_a_quarantined_canonical``.)
    """
    env = resume_env
    sid = "cached"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    routes = env["routes"]
    models = env["models"]

    real_publish = routes.publish_staged_session_sidecar
    reader_out = {}

    def publish_then_stall(session, staging_path):
        real_publish(session, staging_path)
        # A delayed reader loads the provisional canonical and is preempted.
        try:
            models.get_session(sid)
            reader_out["adopted"] = True
        except KeyError:
            reader_out["adopted"] = False

    monkeypatch.setattr(routes, "publish_staged_session_sidecar", publish_then_stall)
    monkeypatch.setattr(routes, "_load_resume_sidecar_nonmutating", lambda _path: object())

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert reader_out.get("adopted") is False
    assert env["rec"].status == 500, env["rec"].error()
    # After quarantine the reader still cannot rebuild a writable object.
    with pytest.raises(KeyError):
        models.get_session(sid)
    assert sid not in models.SESSIONS
    assert models.Session.load(sid) is None
    assert models.is_resume_publication_denied(sid)
    models.SESSIONS.clear()
    assert models.all_sessions() == [] or sid not in [
        s.get("session_id") for s in models.all_sessions()
    ]
    assert sid not in (env["sessions_dir"] / "_index.json").read_text(encoding="utf-8")


def test_ordinary_save_concurrency_is_not_serialized(resume_env, monkeypatch):
    """#765 guard: the ordinary save path must not serialize on Resume fencing.

    Two distinct sessions saving concurrently must both complete while an
    unrelated Resume publication is parked inside its (claim-held) window.
    """
    env = resume_env
    routes = env["routes"]
    models = env["models"]
    parked_sid = "parked-resume"
    _make_state_db(_alpha_db(env), sid=parked_sid, messages=3)

    real_publish = routes.publish_staged_session_sidecar
    released = threading.Event()
    parked = threading.Event()

    def parked_publish(session, staging_path):
        real_publish(session, staging_path)
        parked.set()
        released.wait(5.0)

    monkeypatch.setattr(routes, "publish_staged_session_sidecar", parked_publish)

    def first_call():
        routes._handle_session_resume_in_webui(object(), _body(sid=parked_sid))

    thread = threading.Thread(target=first_call)
    thread.start()
    assert parked.wait(5.0)

    barrier = threading.Barrier(2, timeout=10)
    done = []

    def save_session(sid):
        session = models.Session(session_id=sid, profile="alpha", messages=[{"role": "user", "content": sid}])
        barrier.wait()
        session.save()
        done.append(sid)

    savers = [threading.Thread(target=save_session, args=(f"concurrent-{i}",)) for i in range(2)]
    for t in savers:
        t.start()
    for t in savers:
        t.join(10.0)
    assert all(not t.is_alive() for t in savers)

    released.set()
    thread.join(10.0)
    assert sorted(done) == ["concurrent-0", "concurrent-1"], done
    for sid in done:
        assert (env["sessions_dir"] / f"{sid}.json").exists()


# ---------------------------------------------------------------------------
# A1-A4 (Astra 4f0ad5d8): adversarial regressions
#
# A1  recovery of the CURRENT attempt's publication must require an explicit
#     ``resume_publication_state == 'verified'`` marker; a missing, empty,
#     unknown or provisional marker is never proof.
# A2  admission/cache insertion and denial/eviction are atomic, and a tracked
#     Resume sidecar is revalidated at persistence, so a reader parked after an
#     affirmative admissibility check (or one holding an already-admitted
#     object) can neither cache nor save a revoked publication.
# A3  denial wins for marker-absent legacy Resume sidecars in direct lookup,
#     the cache, the fallback scan, the full index rebuild and incremental
#     index updates.
# A4  real index-exception and after-affirmative-check barriers, including a
#     real post-rejection save attempt.
# ---------------------------------------------------------------------------


def _isolate_resume_index(monkeypatch, models, env) -> Path:
    """Point the sidebar index at the test session dir for this test only."""
    index = env["sessions_dir"] / "_index.json"
    index.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index)
    return index


def _make_tracked_resume_sidecar(env, sid, *, marker, messages=3, profile="alpha"):
    """Write a Resume sidecar directly, with an explicit publication marker.

    ``marker=None`` downgrades it to a pre-fix *legacy* Resume sidecar: it keeps
    every ownership field but carries no publication marker at all.
    """
    models = env["models"]
    session = models.Session(
        session_id=sid,
        profile=profile,
        messages=[{"role": "user", "content": f"m{i}"} for i in range(messages)],
        read_only=False,
    )
    session.resume_source_profile = profile
    session.resume_source_state_db = str(_alpha_db(env))
    session.resume_lineage_root_id = sid
    session.resume_lineage_tip_id = sid
    if marker is not None:
        session.resume_publication_state = marker
    session.save()
    return session, env["sessions_dir"] / f"{sid}.json"


def _make_source_consistent_resume_sidecar(env, sid, *, marker, profile="alpha"):
    """Write the canonical transcript a real Resume publisher would write."""
    models = env["models"]
    routes = env["routes"]
    _row, _report, messages = routes._read_resume_source_snapshot(
        _alpha_db(env), sid, profile
    )
    session = models.import_cli_session(
        sid,
        "Resumable session",
        messages,
        model="test-model",
        profile=profile,
        resume_source_profile=profile,
        resume_source_state_db=str(_alpha_db(env)),
        resume_lineage_root_id=sid,
        resume_lineage_tip_id=sid,
        read_only=False,
        persist=False,
    )
    if marker is not None:
        session.resume_publication_state = marker
    session.save()
    return session, env["sessions_dir"] / f"{sid}.json"


def _strip_marker_from_disk(sidecar: Path) -> None:
    """Remove the publication marker from a canonical without re-saving it."""
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data.pop("resume_publication_state", None)
    sidecar.write_text(json.dumps(data), encoding="utf-8")


# ------------------------------- A1 ----------------------------------------


@pytest.mark.parametrize("marker", [None, "", "provisional", "unexpected"])
def test_post_link_recovery_requires_an_explicit_verified_marker(
    resume_env, monkeypatch, marker
):
    """A1: recovery over a post-link error never adopts an unverified marker.

    The publication attempt owns the canonical (it linked it itself), so the
    recovery path must demand ``resume_publication_state == 'verified'``. A
    missing, empty, provisional or unknown marker is rejected and the artifact
    is made inaccessible - the legacy marker-absent migration policy lives on a
    different, existing-sidecar-only path and must not leak in here.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    tag = marker if marker else "absent"
    sid = f"a1-recovery-{tag}"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    sidecar = env["sessions_dir"] / f"{sid}.json"
    _isolate_resume_index(monkeypatch, models, env)

    real_publish = routes.publish_staged_session_sidecar

    def publish_then_break(session, staging_path):
        real_publish(session, staging_path)
        # The canonical is now linked and owned by THIS attempt. Simulate the
        # marker never reaching disk (or reaching it incorrectly) and a
        # post-link persistence error inside the same publication window.
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        if marker is None:
            data.pop("resume_publication_state", None)
        else:
            data["resume_publication_state"] = marker
        sidecar.write_text(json.dumps(data), encoding="utf-8")
        raise OSError("post-link persist failure")

    monkeypatch.setattr(routes, "publish_staged_session_sidecar", publish_then_break)

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 500, (tag, env["rec"].error())
    assert models.is_resume_publication_denied(sid)
    assert not sidecar.exists(), f"{tag}: rejected publication stayed live"
    with pytest.raises(KeyError):
        models.get_session(sid)
    assert sid not in models.SESSIONS


def test_marker_loss_during_index_failure_is_not_adopted(resume_env, monkeypatch):
    """A1: a verified marker lost during the index window is not proof.

    The verified commit succeeded, then the index write failed AND the on-disk
    marker was gone by the time the post-index integrity re-read ran. The
    attempt must not fall back to the legacy "no marker means committed"
    compatibility rule for a publication it owns: it fails closed, denies the
    id, and quarantines the artifact out of the live namespace.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "a1b-marker-loss"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    sidecar = env["sessions_dir"] / f"{sid}.json"
    _isolate_resume_index(monkeypatch, models, env)

    real_index = models._write_session_index

    def index_strip_marker_and_raise(updates=None, **kwargs):
        if updates:
            _strip_marker_from_disk(sidecar)
            raise OSError("index write failed after verified commit")
        return real_index(updates=updates, **kwargs)

    monkeypatch.setattr(models, "_write_session_index", index_strip_marker_and_raise)

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 500, env["rec"].error()
    assert models.is_resume_publication_denied(sid)
    assert not sidecar.exists(), "the marker-lost artifact stayed in the live namespace"
    with pytest.raises(KeyError):
        models.get_session(sid)
    assert models.Session.load(sid) is None
    assert sid not in models.SESSIONS


def test_real_index_exception_after_verified_commit_keeps_verified_publication(
    resume_env, monkeypatch
):
    """A4/A1 positive control: a REAL index exception over an intact verified
    artifact is the F1 safe-degradation path, and it still succeeds.

    This is the schedule the misleadingly-named older test claimed to cover but
    did not (it injected no index exception at all).
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "a4-real-index-exception"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    sidecar = env["sessions_dir"] / f"{sid}.json"
    _isolate_resume_index(monkeypatch, models, env)

    real_index = models._write_session_index
    raised = {"n": 0}

    def index_raise_once(updates=None, **kwargs):
        if updates:
            raised["n"] += 1
            raise OSError("index write failed after verified commit")
        return real_index(updates=updates, **kwargs)

    monkeypatch.setattr(models, "_write_session_index", index_raise_once)

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert raised["n"] == 1, "the real index exception was never injected"
    assert env["rec"].status == 200, env["rec"].error()
    assert not models.is_resume_publication_denied(sid)
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["resume_publication_state"] == "verified"
    models.SESSIONS.clear()
    assert models.get_session(sid) is not None


# ------------------------------- A2 ----------------------------------------


def test_reader_during_inflight_publication_is_refused_and_rejection_stays_denied(
    resume_env, monkeypatch
):
    """A2/A4: active durable authority refuses readers before rejection.

    Candidate5 deliberately changes this schedule: while the endpoint still owns
    its durable ``publishing`` record, a real reader cannot reach an affirmative
    admissibility result. The reader must be refused immediately, and the later
    endpoint rejection must leave the id durably denied and uncached.

    The original after-affirmative-check invariant remains covered separately by
    ``test_revocation_completes_while_a_reader_is_parked_after_its_check``, which
    uses a legitimately admitted verified sidecar and the real production revoke
    path without forcing the predicate result.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "a2-affirmative-barrier"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    sidecar = env["sessions_dir"] / f"{sid}.json"
    _isolate_resume_index(monkeypatch, models, env)

    entered = threading.Event()
    reader = {}
    real_admissible = models.session_publication_admissible

    def observing_admissible(session, **kwargs):
        result = real_admissible(session, **kwargs)
        if str(getattr(session, "session_id", "") or "") == sid:
            reader["admissible"] = result
            entered.set()
        return result

    monkeypatch.setattr(models, "session_publication_admissible", observing_admissible)

    def reader_body():
        try:
            obj = models.get_session(sid)
            reader["object"] = obj
            reader["writable"] = not bool(getattr(obj, "read_only", False))
        except KeyError:
            reader["refused"] = True
        except Exception as exc:  # pragma: no cover - diagnostic
            reader["error"] = repr(exc)

    real_index = models._write_session_index

    def index_start_reader_strip_and_raise(updates=None, **kwargs):
        if updates:
            thread = threading.Thread(target=reader_body, daemon=True)
            thread.start()
            assert entered.wait(10.0), "reader never reached the admissibility check"
            thread.join(10.0)
            assert not thread.is_alive(), "reader did not fail closed during publication"
            assert reader.get("admissible") is False
            assert reader.get("refused") is True, reader
            reader["thread"] = thread
            _strip_marker_from_disk(sidecar)
            raise OSError("index write failed after verified commit")
        return real_index(updates=updates, **kwargs)

    monkeypatch.setattr(models, "_write_session_index", index_start_reader_strip_and_raise)

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 500, env["rec"].error()
    assert models.is_resume_publication_denied(sid)
    assert "error" not in reader, reader.get("error")
    assert reader.get("refused") is True, reader
    assert "object" not in reader, "a revoked publication was served writable"
    assert sid not in models.SESSIONS, "a revoked publication was re-cached"


def test_post_rejection_save_cannot_recreate_a_quarantined_canonical(
    resume_env, monkeypatch
):
    """A2/A4: a real post-rejection save attempt on an already-admitted object.

    The reader legitimately admitted and cached the verified publication BEFORE
    the rejection. After the denial and quarantine the canonical is gone, but
    the caller still holds the writable object. Its real ``save()`` must be
    refused, and the canonical must NOT reappear. (Before the fix the save
    recreated it: quarantine only evicted the cache and wrote a tombstone, and
    the save path never consulted either.)
    """
    env = resume_env
    models = env["models"]
    sid = "a2-post-rejection-save"
    sidecar = env["sessions_dir"] / f"{sid}.json"
    _isolate_resume_index(monkeypatch, models, env)

    _make_tracked_resume_sidecar(env, sid, marker=models.RESUME_PUBLICATION_VERIFIED)
    assert sidecar.exists()

    models.SESSIONS.clear()
    admitted = models.get_session(sid)
    assert admitted is not None and not bool(getattr(admitted, "read_only", False))
    assert sid in models.SESSIONS

    # Deny + evict (atomic in the fixed implementation), then simulate the
    # best-effort move out of the live namespace succeeding.
    models.mark_resume_publication_denied(sid, reason="post-rejection save probe")
    models.evict_session_from_cache(sid)
    assert models.is_resume_publication_denied(sid)
    sidecar.unlink()
    assert not sidecar.exists()

    with pytest.raises(PermissionError):
        admitted.save(skip_index=True)

    assert not sidecar.exists(), "a post-rejection save recreated the canonical"
    assert sid not in models.SESSIONS
    with pytest.raises(KeyError):
        models.get_session(sid)


# ------------------------------- A3 ----------------------------------------


def test_denied_legacy_resume_sidecar_fails_closed_in_lookup_cache_and_scan(
    resume_env, monkeypatch
):
    """A3: denial wins for a marker-absent legacy Resume sidecar.

    The sidecar keeps every resume ownership field but no publication marker
    (a pre-fix artifact), its canonical stays on disk (the quarantine move
    failed), and it IS denied. Direct lookup, the cache and the fallback scan
    must all refuse it.
    """
    env = resume_env
    models = env["models"]
    sid = "a3-legacy-denied"
    _isolate_resume_index(monkeypatch, models, env)
    _, sidecar = _make_tracked_resume_sidecar(env, sid, marker=None)

    legacy = models.Session.load(sid)
    assert legacy is not None
    # It really is a pre-fix legacy Resume sidecar: ownership fields present,
    # publication marker absent.
    assert models.resume_publication_state(legacy) is None
    assert getattr(legacy, "resume_source_state_db", None)
    assert getattr(legacy, "resume_lineage_root_id", None) == sid
    # Control: a legacy sidecar with no denial is still admissible (the
    # supported pre-fix migration policy must not regress).
    assert models.session_publication_admissible(legacy)

    models.mark_resume_publication_denied(sid, reason="legacy denial probe")
    assert models.is_resume_publication_denied(sid)
    assert sidecar.exists(), "the move was supposed to fail; the file stays"

    models.SESSIONS.clear()
    with pytest.raises(KeyError):
        models.get_session(sid)
    assert sid not in models.SESSIONS

    loaded = models.Session.load(sid)
    assert loaded is not None and not models.session_publication_admissible(loaded)

    models.SESSIONS.clear()
    rows = models.all_sessions()
    assert sid not in [row.get("session_id") for row in rows]
    assert not models.SESSIONS


def test_denied_legacy_resume_sidecar_is_dropped_by_full_index_rebuild(
    resume_env, monkeypatch
):
    """A3: a full ``_write_session_index()`` rebuild drops the denied legacy row
    while keeping an un-denied legacy sibling (so the fix is not a blanket
    "hide every Resume sidecar")."""
    env = resume_env
    models = env["models"]
    denied_sid = "a3-rebuild-denied"
    control_sid = "a3-rebuild-control"
    index = _isolate_resume_index(monkeypatch, models, env)
    _make_tracked_resume_sidecar(env, denied_sid, marker=None)
    _make_tracked_resume_sidecar(env, control_sid, marker=None)

    models.mark_resume_publication_denied(denied_sid, reason="rebuild probe")
    models.SESSIONS.clear()

    models._write_session_index()

    rows = json.loads(index.read_text(encoding="utf-8"))
    ids = [row.get("session_id") for row in rows]
    assert denied_sid not in ids, "the full rebuild re-indexed a denied legacy row"
    assert control_sid in ids, "an un-denied legacy sidecar was wrongly dropped"


def test_denied_legacy_resume_sidecar_is_dropped_by_incremental_index_update(
    resume_env, monkeypatch
):
    """A3: an incremental ``_write_session_index(updates=...)`` drops a denied
    legacy row that is already in the index."""
    env = resume_env
    models = env["models"]
    denied_sid = "a3-incremental-denied"
    index = _isolate_resume_index(monkeypatch, models, env)
    _, sidecar = _make_tracked_resume_sidecar(env, denied_sid, marker=None)
    models.mark_resume_publication_denied(denied_sid, reason="incremental probe")

    # Seed the index exactly as a pre-fix deployment would have left it.
    stale_row = models.Session.load(denied_sid).compact()
    index.write_text(json.dumps([stale_row]), encoding="utf-8")

    control = models.Session(
        session_id="a3-incremental-control",
        profile="alpha",
        messages=[{"role": "user", "content": "control"}],
    )
    models._write_session_index(updates=[control])

    rows = json.loads(index.read_text(encoding="utf-8"))
    ids = [row.get("session_id") for row in rows]
    assert denied_sid not in ids, "the incremental update kept a denied legacy row"
    assert "a3-incremental-control" in ids
    assert sidecar.exists()


def test_all_sessions_index_path_drops_a_denied_legacy_resume_sidecar(
    resume_env, monkeypatch
):
    """A3: the sidebar index fast path must deny the legacy sid, not just a
    marked one, when its canonical is still on disk."""
    env = resume_env
    models = env["models"]
    denied_sid = "a3-indexpath-denied"
    control_sid = "a3-indexpath-control"
    index = _isolate_resume_index(monkeypatch, models, env)
    _make_tracked_resume_sidecar(env, denied_sid, marker=None)
    _make_tracked_resume_sidecar(env, control_sid, marker=None)
    models.mark_resume_publication_denied(denied_sid, reason="index-path probe")

    # A valid index (both rows present) so all_sessions() takes the fast path.
    rows = [
        models.Session.load(denied_sid).compact(),
        models.Session.load(control_sid).compact(),
    ]
    index.write_text(json.dumps(rows), encoding="utf-8")

    models.SESSIONS.clear()
    listed = [row.get("session_id") for row in models.all_sessions()]
    assert denied_sid not in listed
    assert control_sid in listed


def test_denial_snapshot_failure_fails_closed_for_tracked_resume_identity(
    resume_env, monkeypatch
):
    """A3: an unavailable denial *snapshot* is "unknown", never "no denials".

    ``_denied_resume_publication_ids`` returns None when the denial directory
    cannot be listed. The per-id tombstone check must then be used instead of
    affirmative absence, for a marker-absent legacy Resume identity as much as
    for a marked one - direct lookup, enumeration and the index rebuild.
    """
    env = resume_env
    models = env["models"]
    denied_sid = "a3-snapshot-denied"
    control_sid = "a3-snapshot-control"
    index = _isolate_resume_index(monkeypatch, models, env)
    _make_tracked_resume_sidecar(env, denied_sid, marker=None)
    _make_tracked_resume_sidecar(env, control_sid, marker=None)
    models.mark_resume_publication_denied(denied_sid, reason="snapshot probe")

    monkeypatch.setattr(models, "_denied_resume_publication_ids", lambda *a, **k: None)

    models.SESSIONS.clear()
    with pytest.raises(KeyError):
        models.get_session(denied_sid)
    assert not models.session_publication_admissible(models.Session.load(denied_sid))

    listed = [row.get("session_id") for row in models.all_sessions()]
    assert denied_sid not in listed
    assert control_sid in listed, "an un-denied legacy sidecar was wrongly dropped"

    models._write_session_index()
    ids = [row.get("session_id") for row in json.loads(index.read_text(encoding="utf-8"))]
    assert denied_sid not in ids

    # Ordinary (non-Resume) sessions are unaffected by an unavailable snapshot.
    plain = models.Session(session_id="a3-plain", profile="alpha")
    assert models.session_publication_admissible(plain)


# ---------------------------------------------------------------------------
# B1-B4 / F3 (Astra release blockers): durable refusal authority, cross-process
# authority, observable denial enumeration, dead-clear fencing and
# ended-source-before-idempotency.
#
# B1  refusal authority must be durable and architecture-complete: the
#     publication record is written (fsync'd) BEFORE the canonical can exist,
#     so even a refusal whose denial channels BOTH fail leaves a durable
#     on-disk guard that survives a restart.
# B2  publication authority is exclusive per id across processes, a stale
#     publisher cannot clear/outrun a newer denial, and a failed attempt never
#     revokes another attempt's committed artifact.
# B3  the batched denial snapshot uses observable enumeration: a missing
#     directory is genuinely empty, any other error is "unknown" and falls
#     back to the per-id fail-closed checks.
# B4  an unended source is refused before ANY idempotent success, including a
#     settled -> unended revival over an existing sidecar.
# F3  a denial that cannot be verifiably cleared never yields 200.
# ---------------------------------------------------------------------------


_CROSS_PROCESS_OWNER = """
import sys, time
from pathlib import Path

repo, sid, session_dir, ready, done = sys.argv[1:6]
sys.path.insert(0, repo)
import api.models as models

models.SESSION_DIR = Path(session_dir)
acquired = models.claim_session_for_resume(sid)
claim_token = models.resume_claim_ownership_token(sid) if acquired else None
Path(ready).write_text("1" if acquired else "0", encoding="utf-8")
deadline = time.time() + 30.0
while time.time() < deadline and not Path(done).exists():
    time.sleep(0.02)
models.release_session_claim(sid, ownership_token=claim_token)
sys.exit(0 if acquired else 3)
"""


def test_cross_process_resume_owner_blocks_ordinary_save(resume_env, tmp_path):
    """An ordinary save shares the durable fence through its final replace."""
    env = resume_env
    sid = "cross-process-save-fence"
    result_path = tmp_path / "save-result.txt"
    script = r"""
import sys
from pathlib import Path

repo, session_dir, sid, result_path = sys.argv[1:]
sys.path.insert(0, repo)
from api import models

models.SESSION_DIR = Path(session_dir)
session = models.Session(
    session_id=sid,
    title="foreign ordinary writer",
    workspace=session_dir,
    profile="alpha",
)
try:
    session.save()
except PermissionError:
    Path(result_path).write_text("refused", encoding="utf-8")
else:
    Path(result_path).write_text("saved", encoding="utf-8")
"""

    assert env["models"].claim_session_for_resume(sid) is True
    claim_token = env["models"].resume_claim_ownership_token(sid)
    assert claim_token
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(Path(__file__).resolve().parents[1]),
                str(env["sessions_dir"]),
                sid,
                str(result_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, proc.stderr
        assert result_path.read_text(encoding="utf-8") == "refused"
        assert not (env["sessions_dir"] / f"{sid}.json").exists()
    finally:
        assert env["models"].release_session_claim(
            sid, ownership_token=claim_token
        ) is True


@pytest.mark.parametrize("sid", ["../escape", "/absolute/escape"])
def test_ordinary_save_fence_rejects_unsafe_id_before_filesystem_io(
    resume_env, monkeypatch, sid
):
    models = resume_env["models"]

    def forbidden_open(_path):  # pragma: no cover - assertion is the behavior
        pytest.fail("unsafe id reached the lock-file opener")

    monkeypatch.setattr(models, "_open_resume_lock_fd", forbidden_open)
    with pytest.raises(ValueError, match="Unsafe session_id"):
        models.begin_session_save(sid)


def test_windows_save_and_resume_fences_request_shared_and_exclusive_locks(
    resume_env, monkeypatch
):
    models = resume_env["models"]
    calls = []
    unlocks = []

    monkeypatch.setattr(models, "_fcntl", None)
    monkeypatch.setattr(models, "_msvcrt", object())
    monkeypatch.setattr(
        models,
        "_windows_lock_file_fd",
        lambda fd, *, exclusive: calls.append((fd, exclusive)) or True,
    )
    monkeypatch.setattr(
        models, "_windows_unlock_file_fd", lambda fd: unlocks.append(fd)
    )

    assert models._flock_session_save_fd(10) is True
    assert models._flock_resume_lock_fd(11) is True
    models._unlock_resume_lock_fd(11)

    assert calls == [(10, False), (11, True)]
    assert unlocks == [11]


def _wait_for_file(path: Path, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


def _reset_recorder(rec) -> None:
    rec.bad = None
    rec.j = None


def _join_thread(thread, timeout: float = 10.0) -> bool:
    thread.join(timeout)
    return not thread.is_alive()


def test_fail_closed_when_denial_and_quarantine_both_fail_survives_restart(
    resume_env, monkeypatch
):
    """B1: durable fail-closed state when both denial channels AND the move fail.

    The publication record is written before the canonical can exist, so a
    refusal whose tombstone write fails, whose ledger-denial write fails AND
    whose quarantine move fails still leaves the id inadmissible — on disk, not
    in process memory — and therefore after a restart too. The artifact is
    still live on disk, so the refusal provably is not "the file is gone".
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "b1-double-channel-failure"
    db_path = _alpha_db(env)
    _make_state_db(db_path, sid=sid, messages=3)
    _isolate_resume_index(monkeypatch, models, env)
    sidecar = env["sessions_dir"] / f"{sid}.json"

    # Make the best-effort quarantine move fail for real: the quarantine path is
    # occupied by a regular file, so its mkdir can never succeed.
    (env["sessions_dir"] / ".resume-quarantine").write_text(
        "not a directory", encoding="utf-8"
    )

    # Make BOTH durable denial channels fail, AFTER the pre-publication record.
    real_write = models._durable_write_resume_record
    writes = []

    def failing_write(target, payload):
        writes.append(Path(target).name)
        if len(writes) > 1:
            raise OSError("simulated durable denial write failure")
        return real_write(target, payload)

    monkeypatch.setattr(models, "_durable_write_resume_record", failing_write)

    # Force the publication to fail final verification (source changed mid-stage).
    real_stage = routes.stage_session_sidecar

    def mutate_during_stage(session, staging_path):
        staged = real_stage(session, staging_path)
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?,?,?,?)",
                (sid, "assistant", "late write", 1700000200.0),
            )
            conn.execute(
                "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                (sid,),
            )
        return staged

    monkeypatch.setattr(routes, "stage_session_sidecar", mutate_during_stage)

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert len(writes) >= 2, writes  # begin succeeded, denial writes were attempted
    assert env["rec"].status == 500, env["rec"].error()

    # Both denial channels really failed...
    assert not models._resume_denial_tombstone_present(sid)
    assert not models.is_resume_publication_denied(sid)
    # ...and the artifact is still live (the move failed too).
    assert sidecar.exists()

    # B1: the record written BEFORE the artifact existed is what refuses the id.
    assert models._resume_ledger_record_present(sid)
    assert models.resume_publication_authority_blocked(sid)

    # Restart: every in-process admission/revocation structure is dropped.
    models.SESSIONS.clear()
    models._RESUME_REVOCATION_GENERATION.clear()
    with pytest.raises(KeyError):
        models.get_session(sid)
    assert sid not in models.SESSIONS
    assert not models.session_publication_admissible(models.Session.load(sid))

    # A fresh Resume attempt cannot return writable over it either.
    monkeypatch.setattr(models, "_durable_write_resume_record", real_write)
    _reset_recorder(env["rec"])
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 409, env["rec"].error()
    assert env["rec"].payload() is None


def test_publication_authority_failure_refuses_before_any_artifact(resume_env, monkeypatch):
    """B1: no artifact may exist without durable publication authority.

    When the pre-publication record cannot be written the request is refused
    and nothing is published at all — a publication that could not be durably
    denounced must never be created. The id is not bricked by the transient
    store error: a healthy retry succeeds.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "b1-authority-write-failure"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    _isolate_resume_index(monkeypatch, models, env)
    sidecar = env["sessions_dir"] / f"{sid}.json"

    real_write = models._durable_write_resume_record

    def exploding_write(target, payload):
        raise OSError("simulated authority write failure")

    monkeypatch.setattr(models, "_durable_write_resume_record", exploding_write)
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 500, env["rec"].error()
    assert not sidecar.exists(), "an artifact was published without durable authority"
    assert not models.is_resume_publication_denied(sid)
    assert not models._resume_ledger_record_present(sid)

    monkeypatch.setattr(models, "_durable_write_resume_record", real_write)
    _reset_recorder(env["rec"])
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 200, env["rec"].error()
    assert sidecar.exists()
    served = models.Session.load(sid)
    assert models.resume_publication_state(served) == models.RESUME_PUBLICATION_VERIFIED


def test_cross_process_ownership_blocks_resume_without_revoking(resume_env, monkeypatch):
    """B2: a live foreign owner of an id refuses Resume and is not revoked.

    A real second process takes exclusive ownership of the id. This request must
    refuse with the ownership conflict (not an in-process save conflict), must
    create no artifact, and must NOT deny/quarantine anything — losing the
    ownership race is not a rejection of the owner's publication. Once the
    owner releases, a fresh Resume succeeds.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "b2-cross-process-owner"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    _isolate_resume_index(monkeypatch, models, env)
    sidecar = env["sessions_dir"] / f"{sid}.json"

    repo_root = Path(__file__).resolve().parents[1]
    ready = env["sessions_dir"].parent / "b2-child-ready"
    done = env["sessions_dir"].parent / "b2-child-done"
    child = subprocess.Popen(
        [
            sys.executable, "-c", _CROSS_PROCESS_OWNER,
            str(repo_root), sid, str(env["sessions_dir"]), str(ready), str(done),
        ],
        cwd=str(repo_root),
        env={**os.environ, "PYTHONPATH": str(repo_root)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_for_file(ready), "the competing owner never started"
        claimed = ready.read_text(encoding="utf-8")
        assert claimed == "1", "the competing owner could not take ownership"
        assert child.poll() is None, "the competing owner exited early"

        routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

        assert env["rec"].status == 409, env["rec"].error()
        assert "another process" in env["rec"].error()
        assert env["rec"].status != 200
        # The loser must not revoke, deny or publish anything.
        assert not sidecar.exists()
        assert not models._resume_ledger_record_present(sid)
        assert not models.is_resume_publication_denied(sid)
        assert models.resume_store_ownership_conflict(sid)
    finally:
        done.write_text("1", encoding="utf-8")
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - diagnostic
            child.kill()
            child.wait(timeout=10)
    child_stderr = child.stderr.read().decode("utf-8", "replace") if child.stderr else ""
    assert child.returncode == 0, child_stderr
    assert not models.resume_store_ownership_conflict(sid)

    _reset_recorder(env["rec"])
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 200, env["rec"].error()
    assert sidecar.exists()


def test_failed_attempt_cannot_revoke_a_committed_publication(resume_env, monkeypatch):
    """B2: only the attempt that committed the artifact may revoke it.

    A committed (verified, un-denied) publication belongs to an attempt that
    finished its own verified commit. Any other, failing attempt must neither
    deny the id nor move that artifact — and an ordinary lookup must keep
    serving it writable. The owning attempt (``force=True``) may still revoke
    its own artifact, so this is not a blanket refusal to quarantine.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "b2-foreign-committed"
    _isolate_resume_index(monkeypatch, models, env)
    _, sidecar = _make_tracked_resume_sidecar(
        env, sid, marker=models.RESUME_PUBLICATION_VERIFIED
    )

    models.SESSIONS.clear()
    served = models.get_session(sid)
    assert served is not None and not bool(getattr(served, "read_only", False))
    assert sid in models.SESSIONS

    assert routes._quarantine_resume_sidecar(sidecar, sid) is None
    assert sidecar.exists(), "a failed attempt revoked a committed artifact"
    assert not models.is_resume_publication_denied(sid)
    assert sid in models.SESSIONS, "an unrelated admission was evicted"
    assert models.session_publication_admissible(models.Session.load(sid))

    # The attempt that owns the commit may still revoke its own artifact.
    target = routes._quarantine_resume_sidecar(sidecar, sid, force=True)
    assert target is not None and target.exists()
    assert not sidecar.exists()
    assert models.is_resume_publication_denied(sid)
    assert sid not in models.SESSIONS


def test_denial_snapshot_distinguishes_missing_dir_from_enumeration_failure(
    resume_env, monkeypatch
):
    """B3: missing directory = empty; any other enumeration error = unknown.

    A genuinely absent denial directory is empty, but an unreadable/unlistable
    one is UNKNOWN and must fall back to the per-id fail-closed checks — a
    tracked Resume identity is refused, ordinary sessions are untouched.
    """
    env = resume_env
    models = env["models"]
    sessions_dir = env["sessions_dir"]
    denial_dir = sessions_dir / models.RESUME_DENIAL_DIRNAME
    sid = "b3-enumeration-failure"

    # Missing directory: genuinely empty, not unknown.
    assert not denial_dir.exists()
    assert models._scan_resume_state_dir(denial_dir) == frozenset()
    assert models._denied_resume_publication_ids() == frozenset()

    _make_tracked_resume_sidecar(env, sid, marker=models.RESUME_PUBLICATION_VERIFIED)
    models.SESSIONS.clear()
    control = models.Session(session_id="b3-control", profile="alpha")
    assert models.session_publication_admissible(control)
    assert models.session_publication_admissible(models.Session.load(sid))

    # The denial path is a regular file: enumeration raises, so it is unknown.
    denial_dir.write_text("not a directory", encoding="utf-8")
    assert models._scan_resume_state_dir(denial_dir) is None
    assert models._denied_resume_publication_ids() is None
    models.SESSIONS.clear()
    with pytest.raises(KeyError):
        models.get_session(sid)
    assert not models.session_publication_admissible(models.Session.load(sid))
    assert models.session_publication_admissible(control)

    # A real permission failure behaves the same way.
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        denial_dir.unlink()
        denial_dir.mkdir()
        os.chmod(denial_dir, 0o000)
        try:
            assert models._scan_resume_state_dir(denial_dir) is None
            assert models._denied_resume_publication_ids() is None
            models.SESSIONS.clear()
            with pytest.raises(KeyError):
                models.get_session(sid)
            assert models.session_publication_admissible(control)
        finally:
            os.chmod(denial_dir, 0o700)


def test_failed_denial_clear_cannot_return_200(resume_env, monkeypatch):
    """F3: a denial that cannot be verifiably cleared never yields 200.

    The stale tombstone cannot be removed (its path is a directory), so the
    pre-commit clear fails. The request must fail closed and the artifact must
    not stay served writable.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "f3-unclearable-denial"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    _isolate_resume_index(monkeypatch, models, env)
    sidecar = env["sessions_dir"] / f"{sid}.json"

    denial_dir = env["sessions_dir"] / models.RESUME_DENIAL_DIRNAME
    denial_dir.mkdir(parents=True, exist_ok=True)
    (denial_dir / f"{sid}.json").mkdir()  # a tombstone that cannot be unlinked

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].payload() is None
    assert env["rec"].status == 500, env["rec"].error()
    assert models.is_resume_publication_denied(sid)
    assert not sidecar.exists()
    models.SESSIONS.clear()
    with pytest.raises(KeyError):
        models.get_session(sid)


def test_denial_landing_after_the_verified_commit_is_not_reported_as_success(
    resume_env, monkeypatch
):
    """F3: a concurrent revocation during the request wins over the response.

    The denial lands after the verified commit and the clear (while the index
    is written). The final authority verification must catch it: no 200, and
    the artifact must not survive as a writable publication.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "f3-post-commit-revocation"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    index = _isolate_resume_index(monkeypatch, models, env)
    sidecar = env["sessions_dir"] / f"{sid}.json"

    real_index = models._write_session_index
    revoked = []

    def index_then_revoke(*args, **kwargs):
        result = real_index(*args, **kwargs)
        if not revoked:
            revoked.append(True)
            models.revoke_resume_publication(sid, reason="concurrent revocation probe")
        return result

    monkeypatch.setattr(models, "_write_session_index", index_then_revoke)

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert revoked, "the concurrent revocation never ran"
    assert env["rec"].payload() is None
    assert env["rec"].status == 500, env["rec"].error()
    assert models.is_resume_publication_denied(sid)
    assert not sidecar.exists()
    assert sid not in models.SESSIONS
    assert sid not in index.read_text(encoding="utf-8")
    models.SESSIONS.clear()
    with pytest.raises(KeyError):
        models.get_session(sid)


def test_unended_source_is_refused_before_an_idempotent_retry(resume_env, monkeypatch):
    """B4: a settled -> unended revival blocks even the idempotent re-resume.

    The sidecar published while the source was settled must not be handed back
    (200) once the source is live again: the ended-source invariant is checked
    before any idempotent success, and the existing publication is untouched.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "b4-unended-idempotent-retry"
    db_path = _alpha_db(env)
    _make_state_db(db_path, sid=sid, messages=3)
    _isolate_resume_index(monkeypatch, models, env)
    sidecar = env["sessions_dir"] / f"{sid}.json"

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 200, env["rec"].error()
    assert sidecar.exists()
    published = sidecar.read_bytes()

    # Idempotent control: a second resume of the still-settled source is 200.
    _reset_recorder(env["rec"])
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 200, env["rec"].error()

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?",
            (sid,),
        )

    _reset_recorder(env["rec"])
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].payload() is None
    assert env["rec"].status == 409, env["rec"].error()
    assert "appears active" in env["rec"].error()
    assert sidecar.read_bytes() == published, "the existing publication was rewritten"


def test_revocation_completes_while_a_reader_is_parked_after_its_check(
    resume_env, monkeypatch
):
    """A2/A4: the real revocation completes while a reader stays parked.

    The reader is parked INSIDE the admissibility predicate, immediately after
    it returned True. The production revocation
    (``revoke_resume_publication``) must then complete without waiting for that
    reader: it records the denial durably, bumps the generation, and evicts the
    cached writable object. When the reader resumes, the generation recheck
    refuses admission and the durable denial refuses it after a restart.
    """
    env = resume_env
    models = env["models"]
    sid = "a2-revoke-while-parked"
    _isolate_resume_index(monkeypatch, models, env)
    _make_tracked_resume_sidecar(env, sid, marker=models.RESUME_PUBLICATION_VERIFIED)
    # Warm the cache with a REAL, legitimately admitted writable object first, so
    # the revocation below has something concrete to evict and the parked reader
    # needs no cold load of its own.
    models.SESSIONS.clear()
    warm = models.get_session(sid)
    assert warm is not None and not bool(getattr(warm, "read_only", False))
    assert sid in models.SESSIONS

    entered = threading.Event()
    release = threading.Event()
    reader = {}
    real_admissible = models.session_publication_admissible

    def parking_admissible(session, **kwargs):
        result = real_admissible(session, **kwargs)
        if (
            result
            and str(getattr(session, "session_id", "") or "") == sid
            and not entered.is_set()
        ):
            entered.set()
            release.wait(10.0)
        return result

    monkeypatch.setattr(models, "session_publication_admissible", parking_admissible)

    def reader_body():
        try:
            obj = models.get_session(sid)
            reader["object"] = obj
            reader["writable"] = not bool(getattr(obj, "read_only", False))
        except KeyError:
            reader["refused"] = True
        except Exception as exc:  # pragma: no cover - diagnostic
            reader["error"] = repr(exc)

    thread = threading.Thread(target=reader_body, daemon=True)
    thread.start()
    assert entered.wait(10.0), "the reader never reached its affirmative check"

    previous_generation = models.resume_revocation_generation(sid)
    generation = models.revoke_resume_publication(sid, reason="A2 barrier probe")

    # The revocation COMPLETED while the reader is still parked: it never waited
    # for that reader, and the reader's pre-park affirmative check bought it
    # nothing.
    assert not release.is_set()
    assert thread.is_alive()
    assert generation > previous_generation
    assert models.is_resume_publication_denied(sid)
    assert models.resume_revocation_generation(sid) == generation
    assert models._resume_denial_tombstone_present(sid)
    assert sid not in models.SESSIONS, "the revocation did not evict the cached object"

    release.set()
    assert _join_thread(thread), "the parked reader never resumed"
    assert reader.get("refused") is True, reader
    assert "object" not in reader
    assert sid not in models.SESSIONS

    # Durable, not process memory: drop every in-process admission structure and
    # the refusal still holds.
    models.SESSIONS.clear()
    models._RESUME_REVOCATION_GENERATION.clear()
    with pytest.raises(KeyError):
        models.get_session(sid)
    assert not models.session_publication_admissible(models.Session.load(sid))


# ---------------------------------------------------------------------------
# F1 - F5: architecture-complete review remediation
# ---------------------------------------------------------------------------

_F1_DURABILITY_PROBE = """
import sys
from pathlib import Path

repo, sid, session_dir = sys.argv[1:4]
sys.path.insert(0, repo)
import api.models as models

models.SESSION_DIR = Path(session_dir)
blocked = models.resume_publication_authority_blocked(sid)
denied = models.is_resume_publication_denied(sid)
canonical = (Path(session_dir) / (sid + ".json")).exists()
if blocked and denied and not canonical:
    print("blocked")
else:
    print(f"blocked={blocked} denied={denied} canonical={canonical}")
sys.exit(0)
"""


def test_f1_post_verified_commit_rejection_survives_cache_clear_and_restart(
    resume_env, monkeypatch
):
    """F1: the durable attempt must outlive index reconciliation.

    The publication is verified+committed, then (a) the index reconciliation
    fails AND (b) the readback cannot re-read the canonical at all (transient
    read failure), so no readback-based re-validation is possible. The attempt
    must still be denied AND the attempt's OWN just-committed artifact must be
    quarantined - the committed-publication protection must not shield it - and
    no writable canonical may survive a cache clear / restart.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "f1-post-commit-rejection"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    _isolate_resume_index(monkeypatch, models, env)
    sidecar = env["sessions_dir"] / f"{sid}.json"

    state = {"index_failed": False}
    real_index = models._write_session_index
    real_load = routes._load_resume_sidecar_nonmutating

    def failing_index(updates=None, **kwargs):
        if updates:
            state["index_failed"] = True
            raise OSError("index reconciliation failed after a verified commit")
        return real_index(updates=updates, **kwargs)

    def transient_load(path):
        if state["index_failed"]:
            raise OSError("transient canonical read failure")
        return real_load(path)

    monkeypatch.setattr(models, "_write_session_index", failing_index)
    monkeypatch.setattr(routes, "_load_resume_sidecar_nonmutating", transient_load)

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert state["index_failed"] is True
    assert env["rec"].status == 500, env["rec"].error()
    assert env["rec"].payload() is None

    # The durable attempt became a durable denial on every channel.
    assert models.is_resume_publication_denied(sid)
    assert models._resume_denial_tombstone_present(sid)
    assert models.resume_publication_authority_blocked(sid)

    # ...and the attempt's OWN verified commit was quarantined anyway.
    assert not sidecar.exists(), "the failing attempt's own commit was not quarantined"
    quarantined = list((env["sessions_dir"] / ".resume-quarantine").glob(f"{sid}-*.json"))
    assert len(quarantined) == 1, quarantined

    # No writable canonical survives a cache clear + restart.
    models.SESSIONS.clear()
    models._RESUME_REVOCATION_GENERATION.clear()
    with pytest.raises(KeyError):
        models.get_session(sid)
    assert sid not in models.SESSIONS
    assert models.Session.load(sid) is None, "a writable canonical survived the restart"
    assert models.resume_publication_authority_blocked(sid)

    # The refusal is durable state, not this process's memory: a fresh
    # interpreter sharing the store sees the same denial and no canonical.
    probe = subprocess.run(
        [
            sys.executable, "-c", _F1_DURABILITY_PROBE,
            str(Path(__file__).resolve().parents[1]), sid,
            str(env["sessions_dir"]),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={**os.environ,
             "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        capture_output=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.decode().strip() == "blocked", probe.stdout


def test_f1_committed_publication_protection_never_shields_the_own_failing_commit(
    resume_env, monkeypatch
):
    """F1: the attempt's OWN verified commit must still be quarantined.

    The index reconciliation fails and the readback returns a canonical that is
    still an explicitly verified, non-denied publication - so the B2
    committed-publication protection would refuse to move it - but whose
    published content no longer matches what this attempt committed. The
    protection must not shield this attempt's own failing commit: the id must be
    denied and the artifact moved out, with nothing writable surviving a restart.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "f1-protection-does-not-shield-own-commit"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    _isolate_resume_index(monkeypatch, models, env)
    sidecar = env["sessions_dir"] / f"{sid}.json"

    state = {"index_failed": False}
    real_index = models._write_session_index
    real_load = routes._load_resume_sidecar_nonmutating

    def failing_index(updates=None, **kwargs):
        if updates:
            state["index_failed"] = True
            raise OSError("index reconciliation failed after a verified commit")
        return real_index(updates=updates, **kwargs)

    def mismatched_load(path):
        loaded = real_load(path)
        if state["index_failed"] and loaded is not None:
            # Still an explicitly verified, non-denied publication.
            loaded.messages = list(getattr(loaded, "messages", []) or [])[:-1]
        return loaded

    monkeypatch.setattr(models, "_write_session_index", failing_index)
    monkeypatch.setattr(routes, "_load_resume_sidecar_nonmutating", mismatched_load)

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert state["index_failed"] is True
    assert env["rec"].status == 500, env["rec"].error()
    assert models.is_resume_publication_denied(sid)
    assert models._resume_denial_tombstone_present(sid)
    assert not sidecar.exists(), (
        "the committed-publication protection shielded this attempt's own "
        "failing commit"
    )
    assert list((env["sessions_dir"] / ".resume-quarantine").glob(f"{sid}-*.json"))

    models.SESSIONS.clear()
    models._RESUME_REVOCATION_GENERATION.clear()
    with pytest.raises(KeyError):
        models.get_session(sid)
    assert models.Session.load(sid) is None, "a writable canonical survived the restart"


def test_f1_loser_sibling_cannot_retire_winner_before_commit_complete(
    resume_env, monkeypatch
):
    """F1: a same-epoch loser cannot remove the winner's publication guard.

    Both requests cross the staging boundary before either publishes.  The
    winner is then parked in the real index reconciliation step: its canonical
    already carries the verified marker, but rejection-capable commit work is
    still outstanding.  The loser must fail closed and the durable publishing
    record must remain present until the winner completes.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "f1-sibling-cannot-retire-winner"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    _isolate_resume_index(monkeypatch, models, env)

    staged = threading.Barrier(2)
    index_entered = threading.Event()
    release_index = threading.Event()
    real_stage = routes.stage_session_sidecar
    real_index = models._write_session_index

    def gated_stage(session, staging_path):
        result = real_stage(session, staging_path)
        staged.wait(timeout=10)
        return result

    def gated_index(updates=None, **kwargs):
        if updates:
            index_entered.set()
            assert release_index.wait(timeout=10)
        return real_index(updates=updates, **kwargs)

    monkeypatch.setattr(routes, "stage_session_sidecar", gated_stage)
    monkeypatch.setattr(models, "_write_session_index", gated_index)
    local = _install_thread_recorders(routes, monkeypatch)
    results = {}
    errors = []

    def worker(name):
        local.rec = _Recorder()
        try:
            routes._handle_session_resume_in_webui(local.rec, _body(sid=sid))
        except Exception as exc:  # pragma: no cover - diagnostic
            errors.append(exc)
        results[name] = local.rec

    workers = [
        threading.Thread(target=worker, args=(name,), daemon=True)
        for name in ("a", "b")
    ]
    for thread in workers:
        thread.start()
    assert index_entered.wait(timeout=10), "the winner never reached index reconciliation"
    try:
        deadline = time.time() + 10
        while len(results) < 1 and time.time() < deadline:
            time.sleep(0.01)
        assert len(results) == 1, "the losing sibling did not finish while the winner was parked"
        loser = next(iter(results.values()))
        assert loser.status == 409, loser.payload() or loser.error()
        assert models._resume_ledger_record_present(sid), (
            "the losing sibling retired the winner's in-flight publication record"
        )
        assert models.resume_publication_authority_blocked(sid)
    finally:
        release_index.set()

    for thread in workers:
        assert _join_thread(thread), "a Resume worker did not finish"
    assert not errors, errors
    assert sorted(rec.status for rec in results.values()) == [200, 409]


def test_f1_abort_retirement_is_atomic_against_a_new_sibling_join(
    resume_env, monkeypatch
):
    """F1: abort retirement cannot remove a later sibling's guard."""
    env = resume_env
    models, routes = env["models"], env["routes"]
    sid = "f1-abort-join-race"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)

    real_finish = models.finish_resume_publication
    abort_parked = threading.Event()
    sibling_done = threading.Event()
    release_sibling = threading.Event()
    sibling = {}

    def paused_finish(target_sid, *, attempt=None, **kwargs):
        if target_sid == sid and threading.current_thread().name == "f1-aborting":
            abort_parked.set()
            assert sibling_done.wait(timeout=10), "sibling never ran"
        return real_finish(target_sid, attempt=attempt, **kwargs)

    monkeypatch.setattr(models, "finish_resume_publication", paused_finish)

    def explode(staged, staging_path):
        raise RuntimeError("publish failure after authority established")

    monkeypatch.setattr(routes, "publish_staged_session_sidecar", explode)

    def sibling_worker():
        joined = models.claim_session_for_resume(sid)
        sibling["joined"] = joined
        token = models.resume_claim_ownership_token(sid) if joined else None
        if joined:
            sibling["wrote_record"] = bool(models.begin_resume_publication(sid))
        sibling_done.set()
        if joined:
            release_sibling.wait(timeout=10)
            models.release_session_claim(sid, ownership_token=token)

    aborting = threading.Thread(
        name="f1-aborting",
        target=lambda: routes._handle_session_resume_in_webui(
            env["rec"], _body(sid=sid)
        ),
        daemon=True,
    )
    aborting.start()
    assert abort_parked.wait(timeout=10), "abort never reached retirement"
    joiner = threading.Thread(name="f1-sibling", target=sibling_worker, daemon=True)
    joiner.start()
    assert sibling_done.wait(timeout=10)
    aborting.join(timeout=10)
    release_sibling.set()
    joiner.join(timeout=10)

    assert env["rec"].status == 500
    established = bool(sibling.get("joined")) and bool(sibling.get("wrote_record"))
    assert not (established and not models._resume_ledger_record_present(sid))
    if established:
        assert models.resume_publication_authority_blocked(sid) is True


_F2_RACER = """
import sys, time
from pathlib import Path

repo, sid, session_dir, go, out, done = sys.argv[1:7]
sys.path.insert(0, repo)
import api.models as models

models.SESSION_DIR = Path(session_dir)
Path(out + ".armed").write_text("1", encoding="utf-8")
go = Path(go)
deadline = time.time() + 30.0
while time.time() < deadline and not go.exists():
    time.sleep(0.001)
fd = models._acquire_resume_store_ownership(sid)
Path(out).write_text("1" if fd is not None else "0", encoding="utf-8")
deadline = time.time() + 30.0
while time.time() < deadline and not Path(done).exists():
    time.sleep(0.005)
models._release_resume_store_ownership(sid, fd)
sys.exit(0)
"""


def test_f2_concurrent_stale_reclaim_never_double_owns(resume_env):
    """F2: three real processes racing to reclaim a stale lock never both own.

    A crashed publisher left a lock file whose recorded pid is alive but owns
    nothing, with an ancient mtime. Under the old check-then-unlink recreation
    both racers could "reclaim" it; with a held kernel lock exactly one ever can.
    """
    env = resume_env
    models = env["models"]
    sid = "f2-concurrent-stale-reclaim"
    workdir = env["sessions_dir"].parent

    lock_path = models.resume_publish_lock_path(sid)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"session_id": sid, "pid": 1, "token": "deadbeef"}),
        encoding="utf-8",
    )
    ancient = time.time() - 3650 * 24 * 3600.0
    os.utime(lock_path, (ancient, ancient))

    repo_root = Path(__file__).resolve().parents[1]
    go = workdir / "f2-go"
    done = workdir / "f2-done"
    outs = [workdir / f"f2-out-{i}" for i in range(3)]
    children = [
        subprocess.Popen(
            [
                sys.executable, "-c", _F2_RACER, str(repo_root), sid,
                str(env["sessions_dir"]), str(go), str(out), str(done),
            ],
            cwd=str(repo_root),
            env={**os.environ, "PYTHONPATH": str(repo_root)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for out in outs
    ]
    try:
        # Every racer must be armed (past import, before its claim) first.
        for out in outs:
            armed = Path(str(out) + ".armed")
            assert _wait_for_file(armed), f"{out.name} never armed"
        go.write_text("1", encoding="utf-8")
        for out in outs:
            assert _wait_for_file(out), f"{out.name} never reported"

        results = [out.read_text(encoding="utf-8") for out in outs]
        assert results.count("1") == 1, results
        assert results.count("0") == len(outs) - 1, results

        # The single owner really holds a live kernel lock: this process cannot
        # become a second owner while it holds.
        assert models.resume_store_ownership_conflict(sid) is True
        assert models._acquire_resume_store_ownership(sid) is None
        assert models.claim_session_for_resume(sid) is False
    finally:
        done.write_text("1", encoding="utf-8")
        for child in children:
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - diagnostic
                child.kill()
                child.wait(timeout=10)

    assert all(child.returncode == 0 for child in children)
    # Every racer exited: the id is reclaimable again (no permanent stale lock).
    assert models.resume_store_ownership_conflict(sid) is False
    fd = models._acquire_resume_store_ownership(sid)
    assert fd is not None
    models._release_resume_store_ownership(sid, fd)


def test_f3_stale_lock_naming_a_live_pid_is_reclaimable(resume_env):
    """F3: liveness is a held kernel lock, never a pid/mtime heuristic.

    A lock file recording pid 1 (alive, owns nothing) with an ancient mtime is
    NOT a conflict, so it can never become a permanent stale lock.
    """
    env = resume_env
    models = env["models"]
    sid = "f3-pid-reuse-stale-lock"
    lock_path = models.resume_publish_lock_path(sid)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"session_id": sid, "pid": 1, "token": "recycled"}),
        encoding="utf-8",
    )
    ancient = time.time() - 3650 * 24 * 3600.0
    os.utime(lock_path, (ancient, ancient))

    assert models.resume_store_ownership_conflict(sid) is False
    assert models.claim_session_for_resume(sid) is True
    claim_token = models.resume_claim_ownership_token(sid)
    assert claim_token
    try:
        # Holding it makes it a conflict for everyone else, this process included.
        assert models.resume_store_ownership_conflict(sid) is True
    finally:
        models.release_session_claim(sid, ownership_token=claim_token)
    assert models.resume_store_ownership_conflict(sid) is False


_F3_STRAND = """
import sys
from pathlib import Path

repo, sid, session_dir = sys.argv[1:4]
sys.path.insert(0, repo)
import api.models as models

models.SESSION_DIR = Path(session_dir)
models.begin_resume_publication(sid)
# Crash: exit with the `publishing` record still live and un-retired. The
# kernel drops this process's flock on exit.
sys.exit(0)
"""


def test_f3_crashed_publishing_record_is_recovered_by_a_new_owner(
    resume_env, monkeypatch
):
    """F3: a crashed publisher's stranded `publishing` record is recoverable.

    A real second process establishes durable publication authority and then
    dies without retiring it. Recovery is only allowed to a new exclusive owner
    (no live kernel lock) and only when the canonical is absent or already a
    committed publication. A durable DENIAL is never cleared, and a provisional
    artifact is never unguarded - fail-closed admission is not weakened.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "f3-crashed-publishing-record"
    repo_root = Path(__file__).resolve().parents[1]
    _isolate_resume_index(monkeypatch, models, env)

    def strand():
        child = subprocess.run(
            [sys.executable, "-c", _F3_STRAND, str(repo_root), sid,
             str(env["sessions_dir"])],
            cwd=str(repo_root),
            env={**os.environ, "PYTHONPATH": str(repo_root)},
            capture_output=True,
            timeout=60,
        )
        assert child.returncode == 0, child.stderr

    # Case A: no canonical at all -> the stranded record must not brick a fresh
    # Resume, which replaces the record with its own attempt.
    strand()
    assert models._resume_ledger_record_present(sid)
    assert models.resume_publication_authority_blocked(sid)
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 200, env["rec"].error()
    served = models.Session.load(sid)
    assert models.resume_publication_state(served) == models.RESUME_PUBLICATION_VERIFIED
    assert not models._resume_ledger_record_present(sid)

    # Case B: a committed verified canonical + a stranded record -> a fresh
    # Resume recovers the record and serves the committed artifact.
    sid_b = "f3-crashed-with-committed-canonical"
    _make_state_db(_alpha_db(env), sid=sid_b, messages=3)
    _make_source_consistent_resume_sidecar(
        env, sid_b, marker=models.RESUME_PUBLICATION_VERIFIED
    )
    child_sid = sid
    sid = sid_b
    strand()
    sid = child_sid
    assert models._resume_ledger_record_present(sid_b)
    _reset_recorder(env["rec"])
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid_b))
    assert env["rec"].status == 200, env["rec"].error()
    assert not models._resume_ledger_record_present(sid_b)

    # Case C (fail closed): a durable DENIAL is never recovered.
    denied_sid = "f3-denied-is-never-recovered"
    models.deny_resume_publication(denied_sid, reason="durable denial probe")
    assert models.recover_crashed_resume_publication(denied_sid) is False
    assert models.resume_publication_authority_blocked(denied_sid)

    # Case D (fail closed): a marker-less/provisional canonical stays guarded.
    legacy_sid = "f3-provisional-canonical-stays-guarded"
    _make_tracked_resume_sidecar(env, legacy_sid, marker=None)
    sid = legacy_sid
    strand()
    sid = child_sid
    assert models.recover_crashed_resume_publication(legacy_sid) is False
    assert models.resume_publication_authority_blocked(legacy_sid)


def test_f2_verified_marker_does_not_commit_a_crashed_publication(
    resume_env, monkeypatch
):
    """F2: recovery must not resurrect verified-but-uncommitted bytes.

    A foreign process crashes after establishing durable ``publishing``
    authority.  The canonical has the verified marker, but no durable
    commit-complete transition.  Recovery must leave the id guarded; treating
    the marker alone as proof of commit re-opens a terminally rejectable
    publication after restart.
    """
    env = resume_env
    models = env["models"]
    sid = "f2-verified-is-not-commit-complete"
    repo_root = Path(__file__).resolve().parents[1]
    _isolate_resume_index(monkeypatch, models, env)
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    _make_tracked_resume_sidecar(
        env, sid, marker=models.RESUME_PUBLICATION_VERIFIED
    )

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _F3_STRAND,
            str(repo_root),
            sid,
            str(env["sessions_dir"]),
        ],
        cwd=str(repo_root),
        env={**os.environ, "PYTHONPATH": str(repo_root)},
        capture_output=True,
        timeout=60,
    )
    assert child.returncode == 0, child.stderr
    assert models._resume_ledger_record_present(sid)
    assert models.resume_publication_authority_blocked(sid)

    assert models.recover_crashed_resume_publication(sid) is False
    assert models._resume_ledger_record_present(sid)
    assert models.resume_publication_authority_blocked(sid)
    models.SESSIONS.clear()
    loaded = models.Session.load(sid)
    assert loaded is not None
    assert not models.session_publication_admissible(loaded)


_F4_DENIER = """
import sys
from pathlib import Path

repo, sid, session_dir = sys.argv[1:4]
sys.path.insert(0, repo)
import api.models as models

models.SESSION_DIR = Path(session_dir)
models.revoke_resume_publication(sid, reason="F4 cross-process denial")
sys.exit(0)
"""


def test_f4_post_write_cross_process_denial_prevents_resurrection(
    resume_env, monkeypatch
):
    """F4: a denial that lands DURING the write must not be outlived.

    A different process records a durable denial after this writer passed its
    pre-write fence but before its post-write recheck. The save must fail
    closed, the canonical it just wrote must not survive, and no cached
    writable object may remain after a cache clear.
    """
    env = resume_env
    models = env["models"]
    sid = "f4-post-write-denial"
    _isolate_resume_index(monkeypatch, models, env)
    session, sidecar = _make_tracked_resume_sidecar(
        env, sid, marker=models.RESUME_PUBLICATION_VERIFIED
    )
    models.SESSIONS.clear()
    served = models.get_session(sid)
    assert served is not None
    assert sid in models.SESSIONS

    real_index = models._write_session_index
    fired = []
    rebuilds = []

    def index_then_deny(updates=None, **kwargs):
        result = real_index(updates=updates, **kwargs)
        # Q4: a full rebuild (``updates is None``) re-scans every sidecar under
        # ``_INDEX_WRITE_LOCK``. The denial recovery must not trigger one.
        if updates is None:
            rebuilds.append(True)
        if updates and not fired:
            fired.append(True)
            child = subprocess.run(
                [sys.executable, "-c", _F4_DENIER,
                 str(Path(__file__).resolve().parents[1]), sid,
                 str(env["sessions_dir"])],
                cwd=str(Path(__file__).resolve().parents[1]),
                env={**os.environ,
                     "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                capture_output=True,
                timeout=60,
            )
            assert child.returncode == 0, child.stderr
        return result

    monkeypatch.setattr(models, "_write_session_index", index_then_deny)

    served.messages.append({"role": "user", "content": "post-denial write"})
    with pytest.raises(PermissionError):
        served.save()

    assert fired == [True]
    assert models.is_resume_publication_denied(sid)
    assert models.resume_publication_authority_blocked(sid)
    # The resurrected canonical is gone and the cache no longer holds it.
    assert not sidecar.exists(), "the denied canonical survived the post-write recheck"
    assert sid not in models.SESSIONS

    # Q4: the denied bytes are preserved (quarantined), not destroyed, and the
    # quarantine holds the artifact THIS save wrote.
    quarantined = sorted(
        (env["sessions_dir"] / models.RESUME_QUARANTINE_DIRNAME).glob(f"{sid}-*.json")
    )
    assert len(quarantined) == 1, (
        f"the post-write denial deleted the bytes instead of quarantining: {quarantined}"
    )
    preserved = json.loads(quarantined[0].read_text(encoding="utf-8"))
    assert preserved["session_id"] == sid
    assert any(
        m.get("content") == "post-denial write" for m in preserved.get("messages", [])
    ), "the quarantined copy is not the artifact this save wrote"

    # Q4: and it cost no full index rebuild (the row is pruned incrementally).
    assert rebuilds == [], "the post-write denial rebuilt the whole session index"

    models.SESSIONS.clear()
    models._RESUME_REVOCATION_GENERATION.clear()
    with pytest.raises(KeyError):
        models.get_session(sid)
    assert models.Session.load(sid) is None, "a denied canonical was resurrected"


def test_q4_post_write_denial_quarantines_instead_of_deleting(resume_env, monkeypatch):
    """Q4: the post-write denial preserves the bytes by quarantine, not deletion.

    The sibling F4 test drives this transition from a real *cross-process*
    denial through the test server. This one drives the same production
    transition in-process -- ``_write_session_index`` runs after the pre-write
    fence and before the post-write recheck, so revoking there lands the denial
    exactly inside the window -- and so pins byte preservation without the
    server.

    Red before the fix: the save path unlinked the canonical, so the bytes the
    caller had just written were destroyed.
    """
    env = resume_env
    models = env["models"]
    _isolate_resume_index(monkeypatch, models, env)
    sid = "q4-quarantine-on-save"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)
    _make_tracked_resume_sidecar(env, sid, marker=models.RESUME_PUBLICATION_VERIFIED)
    models.SESSIONS.clear()

    served = models.get_session(sid)
    assert served is not None
    sidecar = env["sessions_dir"] / f"{sid}.json"
    assert sidecar.exists()

    real_index = models._write_session_index
    fired = []

    def index_then_deny(updates=None, **kwargs):
        result = real_index(updates=updates, **kwargs)
        if updates and not fired:
            fired.append(True)
            models.revoke_resume_publication(sid, reason="Q4 in-window revocation")
        return result

    monkeypatch.setattr(models, "_write_session_index", index_then_deny)

    served.messages.append({"role": "user", "content": "post-denial write"})
    with pytest.raises(PermissionError):
        served.save()

    assert fired == [True]
    assert models.is_resume_publication_denied(sid) is True
    # The canonical is withdrawn, and the bytes this save wrote survive in the
    # quarantine instead of being deleted.
    assert not sidecar.exists(), "the denied canonical survived the post-write recheck"
    quarantined = sorted(
        (env["sessions_dir"] / models.RESUME_QUARANTINE_DIRNAME).glob(f"{sid}-*.json")
    )
    assert len(quarantined) == 1, (
        f"the post-write denial deleted the bytes instead of quarantining: {quarantined}"
    )
    preserved = json.loads(quarantined[0].read_text(encoding="utf-8"))
    assert preserved["session_id"] == sid
    assert any(
        m.get("content") == "post-denial write" for m in preserved.get("messages", [])
    ), "the quarantined copy is not the artifact this save wrote"

    models.SESSIONS.clear()
    with pytest.raises(KeyError):
        models.get_session(sid)
    assert models.Session.load(sid) is None, "a denied canonical was resurrected"


def test_f5_ledger_dir_eacces_leaves_ordinary_sessions_unaffected(
    resume_env, monkeypatch
):
    """F5: a Resume-state I/O error fails closed ONLY for tracked identities.

    With the publication ledger directory unreadable (EACCES) the batched
    denial snapshot is unavailable. That must still refuse a tracked Resume
    publication, but it must never make an unrelated ordinary session look
    denied - which would drop every ordinary row from the sidebar / index.
    """
    env = resume_env
    models = env["models"]
    ordinary_sid = "f5-ordinary-session"
    tracked_sid = "f5-tracked-identity"

    ledger_dir = models.resume_ledger_dir()
    ledger_dir.mkdir(parents=True, exist_ok=True)

    # An ordinary session: no Resume ownership fields at all.
    ordinary = models.Session(
        session_id=ordinary_sid,
        profile="alpha",
        messages=[{"role": "user", "content": "ordinary"}],
        read_only=False,
    )
    ordinary.save()
    _make_tracked_resume_sidecar(env, tracked_sid, marker=models.RESUME_PUBLICATION_VERIFIED)
    index = _isolate_resume_index(monkeypatch, models, env)
    index.write_text(
        json.dumps([
            {"session_id": ordinary_sid, "updated_at": 2.0},
            {"session_id": tracked_sid, "updated_at": 1.0},
        ]),
        encoding="utf-8",
    )
    models.SESSIONS.clear()

    os.chmod(ledger_dir, 0o000)
    try:
        # We really are on the error path: the snapshot is unavailable.
        assert models._denied_resume_publication_ids() is None

        # The fail-closed fallback applies to the tracked identity only.
        assert models._denied_ids_contains(None, ordinary_sid) is False
        assert models._denied_ids_contains(None, tracked_sid) is True

        # An ordinary session still loads and admits normally.
        loaded = models.get_session(ordinary_sid)
        assert loaded is not None
        assert loaded.session_id == ordinary_sid

        # A tracked Resume publication is still refused, fail closed.
        models.SESSIONS.clear()
        with pytest.raises(KeyError):
            models.get_session(tracked_sid)

        # The sidebar index is not wiped: the ordinary row survives the write.
        models._write_session_index()
        rows = json.loads(index.read_text(encoding="utf-8"))
        ids = {row.get("session_id") for row in rows}
        assert ordinary_sid in ids, rows
        assert tracked_sid not in ids, rows
    finally:
        os.chmod(ledger_dir, 0o700)


# ---------------------------------------------------------------------------
# Q1-Q7 (audit 7d92dbc1, candidate5): each test below is a real regression —
# it fails against the pre-fix behaviour named in its docstring ("Red before
# the fix") and passes after it. None of them weakens an existing assertion.
# ---------------------------------------------------------------------------


def test_q1_revocation_inside_the_idempotent_race_window_refuses_the_retry(
    resume_env, monkeypatch
):
    """Q1: an in-window revocation is never reported as an idempotent success.

    The retry loses the exclusive first-publication claim to a committed winner,
    so the idempotent-after-race branch runs. A revocation that lands inside that
    branch's window must refuse with 409 (not answer 200 ``idempotent: True``),
    must leave the durable denial record intact -- the retirement flag must stay
    truthful, nothing of this attempt is retired -- and must keep the id refused
    afterwards.

    Red before the fix: the branch consulted only ``finish_resume_publication``,
    so the revocation's own ledger record (state ``denied``, no attempt token)
    was retired and the request was answered 200 ``idempotent: True``.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "sess-alpha-1"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)

    # A real first publication commits the winner.
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))
    assert env["rec"].status == 200
    canonical = env["sessions_dir"] / f"{sid}.json"
    committed = canonical.read_bytes()

    real_conflict = routes._resume_existing_sidecar_conflict

    def conflict_then_revoke(*args, **kwargs):
        # The REAL conflict verdict for the pre-revocation state, then a REAL
        # revocation: exactly the audit's window (after the committed-winner
        # checks, before the guard).
        verdict = real_conflict(*args, **kwargs)
        assert not verdict, verdict
        models.revoke_resume_publication(sid, reason="Q1 in-window revocation")
        return verdict

    monkeypatch.setattr(routes, "_resume_existing_sidecar_conflict", conflict_then_revoke)
    # Grade only the retry: ``_Recorder.status`` keeps preferring a recorded 200.
    env["rec"].j = None
    env["rec"].bad = None
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 409, env["rec"].error()
    assert "still being published" in env["rec"].error()
    # Nothing was retired: the denial still owns the id on every channel.
    assert models.is_resume_publication_denied(sid) is True
    assert models._resume_ledger_record_present(sid) is True
    assert models.resume_publication_authority_blocked(sid) is True
    # The committed artifact was neither rewritten nor adopted.
    assert canonical.read_bytes() == committed
    models.SESSIONS.clear()
    with pytest.raises(KeyError):
        models.get_session(sid)


def test_q2_foreign_attempt_token_cannot_retire_a_live_publication_record(resume_env):
    """Q2: retirement is token-authoritative, so a sibling cannot steal an id.

    ``begin_resume_publication`` publishes this attempt's token into the durable
    record. A *different* attempt that finishes its own work must not retire a
    live record: doing so drops the id's admission fence while the owner is
    still publishing. The owning attempt -- and the legacy unscoped form used by
    recovery/cleanup -- may retire it.

    Red before the fix: ``finish_resume_publication`` ignored every token and
    unlinked whatever record it found.
    """
    env = resume_env
    models = env["models"]
    sid = "q2-cross-attempt-owner"
    attempt_a = models.begin_resume_publication(sid)
    assert attempt_a
    assert models._resume_ledger_record_present(sid) is True
    assert models.resume_publication_authority_blocked(sid) is True

    # A sibling attempt may not retire the live record.
    assert models.finish_resume_publication(sid, attempt="not-this-attempt") is False
    assert models._resume_ledger_record_present(sid) is True
    assert models.resume_publication_authority_blocked(sid) is True

    # Its owner may.
    assert models.finish_resume_publication(sid, attempt=attempt_a) is True
    assert models._resume_ledger_record_present(sid) is False
    assert models.resume_publication_authority_blocked(sid) is False

    # A scoped caller must also refuse a malformed/legacy live record whose
    # attempt token is missing. Absence is not proof of ownership. Only the
    # explicitly unscoped recovery form may retire it.
    tokenless_sid = "q2-tokenless-live-record"
    record = models.resume_ledger_path(tokenless_sid)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps({"session_id": tokenless_sid, "state": models.RESUME_LEDGER_PUBLISHING}),
        encoding="utf-8",
    )
    assert models.finish_resume_publication(tokenless_sid, attempt="scoped-attempt") is False
    assert models._resume_ledger_record_present(tokenless_sid) is True
    assert models.finish_resume_publication(tokenless_sid) is True


def test_q1_denial_after_route_guard_before_retirement_refuses_retry(
    resume_env, monkeypatch
):
    """Q1: retirement revalidates denial inside its atomic transaction.

    The revocation lands after the route's explicit denial guard but immediately
    before retirement.  A matching attempt token must not let retirement erase
    the newer ``denied`` record and turn the retry into a 200.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "sess-alpha-1"
    _make_state_db(_alpha_db(env), sid=sid, messages=3)

    real_finish = models.finish_resume_publication

    def revoke_then_finish(target_sid, *, attempt=None, **kwargs):
        models.revoke_resume_publication(
            target_sid,
            reason="Q1 denial after route guard",
            attempt=attempt,
        )
        return real_finish(target_sid, attempt=attempt, **kwargs)

    monkeypatch.setattr(models, "finish_resume_publication", revoke_then_finish)
    env["rec"].j = None
    env["rec"].bad = None
    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 500, env["rec"].error()
    assert "could not complete" in env["rec"].error()
    assert models.is_resume_publication_denied(sid) is True
    assert not (env["sessions_dir"] / f"{sid}.json").exists()
    entry = models._read_resume_ledger_entry(sid)
    assert isinstance(entry, dict) and entry.get("state") == models.RESUME_LEDGER_DENIED


def test_q2_retirement_cannot_unlink_a_denial_replacement(resume_env, monkeypatch):
    """Q2: read/validate/unlink is serialized with a replacing denial write."""
    models = resume_env["models"]
    sid = "q2-ledger-replacement-race"
    attempt = models.begin_resume_publication(sid)
    real_read = models._read_resume_ledger_entry
    read_done = threading.Event()
    allow_finish = threading.Event()
    denial_done = threading.Event()
    finish_result = []

    def parked_read(target_sid, **kwargs):
        result = real_read(target_sid, **kwargs)
        if target_sid == sid and threading.current_thread().name == "q2-finisher":
            read_done.set()
            assert allow_finish.wait(5)
        return result

    monkeypatch.setattr(models, "_read_resume_ledger_entry", parked_read)

    finisher = threading.Thread(
        name="q2-finisher",
        target=lambda: finish_result.append(
            models.finish_resume_publication(sid, attempt=attempt)
        ),
        daemon=True,
    )

    def deny():
        models.deny_resume_publication(
            sid,
            attempt=attempt,
            reason="replacement must survive",
        )
        denial_done.set()

    denier = threading.Thread(name="q2-denier", target=deny, daemon=True)
    finisher.start()
    assert read_done.wait(5)
    denier.start()
    assert not denial_done.wait(0.2), "denial replaced the ledger during retirement"
    allow_finish.set()
    finisher.join(5)
    denier.join(5)

    assert finish_result == [True]
    assert denial_done.is_set()
    entry = real_read(sid)
    assert isinstance(entry, dict) and entry.get("state") == models.RESUME_LEDGER_DENIED
    assert models.is_resume_publication_denied(sid) is True


def test_q2_same_pid_siblings_share_one_attempt_token_for_recovery(resume_env):
    """Q2: in-process siblings share the epoch token (same-pid recovery/cleanup).

    Concurrent Resume requests for one id share a single ownership epoch, so
    they must share one attempt token: the sibling that LOSES the exclusive
    first-publication race still has to be able to retire the record the winner
    wrote. A record stranded by an earlier same-pid attempt (no live epoch) must
    stay reclaimable through the legacy unscoped form.

    Red before the fix: the token was minted per call, so the losing sibling
    carried a token that could not retire the winner's record.
    """
    env = resume_env
    models = env["models"]
    sid = "q2-same-pid-siblings"
    release_tokens = []
    try:
        assert models.claim_session_for_resume(sid) is True
        release_tokens.append(models.resume_claim_ownership_token(sid))
        token_1 = models.begin_resume_publication(sid)
        assert models.claim_session_for_resume(sid) is True
        release_tokens.append(models.resume_claim_ownership_token(sid))
        token_2 = models.begin_resume_publication(sid)
        assert token_2 == token_1, "in-process siblings must share one attempt token"
        assert models.finish_resume_publication(sid, attempt=token_2) is True
        assert models._resume_ledger_record_present(sid) is False
    finally:
        for release_token in release_tokens:
            models.release_session_claim(sid, ownership_token=release_token)

    # Same-pid recovery of a stranded record with no live epoch.
    assert models.begin_resume_publication(sid)
    assert models.finish_resume_publication(sid) is True
    assert models._resume_ledger_record_present(sid) is False


def test_q3_unclearable_denial_cannot_leave_a_claimed_canonical(resume_env, monkeypatch):
    """Q3: the shared publish helper withdraws its own claim on a failed clear.

    ``publish_staged_session_sidecar`` installs the canonical with an exclusive
    link and only then clears a stale denial. If that clear fails, the artifact
    must not stay live: the helper's contract is "the path either appears
    complete and verified, or never appears". This invokes the helper DIRECTLY
    (not only through the route) so no caller can carry the hole.

    Red before the fix: the helper raised ``PermissionError`` but left the
    canonical it had just linked in place.
    """
    env = resume_env
    models = env["models"]
    sid = "q3-unclearable-denial"
    canonical = env["sessions_dir"] / f"{sid}.json"

    candidate = models.import_cli_session(
        sid,
        "staged candidate",
        [{"role": "user", "content": "hello"}],
        "test-model",
        profile="alpha",
        persist=False,
    )
    staging = env["sessions_dir"] / ".resume-staging" / f"{sid}.stage.json"
    staged = models.stage_session_sidecar(candidate, staging)

    monkeypatch.setattr(models, "clear_resume_publication_denial", lambda *a, **k: False)
    with pytest.raises(PermissionError):
        models.publish_staged_session_sidecar(staged, staging)

    assert not canonical.exists(), "an unclearable denial left the claimed canonical live"
    assert models.Session.load(sid) is None
    # The bytes are preserved for the operator and no staging orphan is left:
    # the canonical is a second link to the staged inode, so exactly one
    # quarantine copy survives.
    quarantined = sorted(
        (env["sessions_dir"] / models.RESUME_QUARANTINE_DIRNAME).glob(f"{sid}-*.json")
    )
    assert len(quarantined) == 1, quarantined
    assert json.loads(quarantined[0].read_text(encoding="utf-8"))["session_id"] == sid
    assert not staging.exists()


def test_q5_slow_ownership_io_for_one_id_never_stalls_an_unrelated_id(
    resume_env, monkeypatch
):
    """Q5: the global claim lock is not held across durable ownership I/O.

    ``claim_session_for_resume`` used to run ``mkdir``/``open``/``flock``/
    ``ftruncate``/``write``/``fsync`` for sid A while holding the one global
    ``_SESSION_CLAIM_LOCK``, so every unrelated id queued behind A's disk
    latency. Ownership is acquired OUTSIDE the lock here and the counters are
    rechecked before the descriptor is published.

    Red before the fix: A blocks inside its ownership call while holding the
    global lock, so the unrelated B thread can never reach its own claim and the
    join below times out.
    """
    env = resume_env
    models = env["models"]
    sid_a = "q5-slow-owner-a"
    sid_b = "q5-unrelated-b"
    entered = threading.Event()
    allow_a = threading.Event()
    errors: list = []
    claim_tokens: dict[str, str] = {}

    real_acquire = models._acquire_resume_store_ownership

    def slow_acquire(sid, **kwargs):
        fd = real_acquire(sid, **kwargs)
        if sid == sid_a:
            entered.set()
            # Block inside the acquisition: whatever lock the caller holds
            # across this call is held here too.
            if not allow_a.wait(20.0):
                errors.append("A's ownership I/O outlived the barrier")
        return fd

    monkeypatch.setattr(models, "_acquire_resume_store_ownership", slow_acquire)

    def claim_a():
        if models.claim_session_for_resume(sid_a):
            claim_tokens[sid_a] = models.resume_claim_ownership_token(sid_a)

    def claim_b():
        if models.claim_session_for_resume(sid_b):
            claim_tokens[sid_b] = models.resume_claim_ownership_token(sid_b)
        allow_a.set()

    thread_a = threading.Thread(target=claim_a, daemon=True)
    thread_a.start()
    assert entered.wait(20.0), "A never reached its durable ownership call"

    thread_b = threading.Thread(target=claim_b, daemon=True)
    thread_b.start()
    thread_b.join(20.0)
    assert not thread_b.is_alive(), (
        errors or "an unrelated id's claim was stalled by A's durable ownership I/O"
    )
    assert not errors, errors
    thread_a.join(20.0)
    assert not thread_a.is_alive()

    assert sid_a in models._SESSION_CLAIM_STATE
    assert sid_b in models._SESSION_CLAIM_STATE
    token_a = claim_tokens.get(sid_a)
    token_b = claim_tokens.get(sid_b)
    assert token_a and token_b
    assert models.release_session_claim(sid_a, ownership_token=token_a) is True
    assert models.release_session_claim(sid_b, ownership_token=token_b) is True


def test_q6_stale_release_cannot_close_a_live_ownership_descriptor(resume_env):
    """Q6: a stale/extra release never closes a live owner's descriptor.

    The epoch's descriptor is the only handle on the durable cross-process
    flock. A release that cannot be attributed to the live epoch (an extra
    call, or a stale token) must be refused, leaving the descriptor open; the
    release that owns the epoch closes it exactly once and further calls are
    no-ops.

    Red before the fix: ``release_session_claim`` took no token at all and was
    keyed on the counter only, so a single extra call dropped the flock.
    """
    env = resume_env
    models = env["models"]
    sid = "q6-stale-release"
    assert models.claim_session_for_resume(sid) is True
    state = models._SESSION_CLAIM_STATE[sid]
    fd = state["ownership_fd"]
    token = models.resume_claim_ownership_token(sid)
    assert token

    # A stale/foreign token is refused and the live descriptor survives.
    assert models.release_session_claim(sid, ownership_token="stale-token") is False
    assert models._SESSION_CLAIM_STATE[sid].get("ownership_fd") == fd
    assert os.fstat(fd).st_ino > 0

    # The owning release closes it once; extras are no-ops.
    assert models.release_session_claim(sid, ownership_token=token) is True
    assert models.release_session_claim(sid, ownership_token=token) is False
    assert sid not in models._SESSION_CLAIM_STATE
    with pytest.raises(OSError):
        os.fstat(fd)


def test_q6_concurrent_claims_share_one_epoch_and_never_weaken_exclusion(
    resume_env,
):
    """Q6: the recheck/join in the claim path keeps exclusion exactly as strong.

    Two threads racing for one id both succeed (an epoch is shared), but they
    share a single durable descriptor: exclusion against other processes is
    never weakened by the pre-frame recheck. Red before the fix in the sense
    that any implementation that "waits then overwrites" would open a second
    descriptor and lose the first flock.
    """
    env = resume_env
    models = env["models"]
    sid = "q6-concurrent-claim"
    go = threading.Event()
    results: list = []
    errors: list = []

    def claim():
        go.wait(20.0)
        try:
            acquired = models.claim_session_for_resume(sid)
            results.append(
                (acquired, models.resume_claim_ownership_token(sid))
            )
        except Exception as exc:  # pragma: no cover - diagnostic only
            errors.append(repr(exc))

    threads = [threading.Thread(target=claim, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    go.set()
    for thread in threads:
        thread.join(20.0)
        assert not thread.is_alive()

    assert errors == [], errors
    assert len(results) == 2 and all(acquired for acquired, _ in results), results
    tokens = [token for _, token in results]
    assert all(tokens) and tokens[0] != tokens[1]
    state = models._SESSION_CLAIM_STATE[sid]
    assert int(state["claims"]) == 2
    fd = state["ownership_fd"]
    assert os.fstat(fd).st_ino > 0

    # An unattributed compatibility release must not consume either sibling's
    # one-shot token or weaken the shared ownership epoch.
    assert models.release_session_claim(sid) is False
    assert int(models._SESSION_CLAIM_STATE[sid]["claims"]) == 2
    assert os.fstat(fd).st_ino > 0

    assert models.release_session_claim(sid, ownership_token=tokens[0]) is True
    assert os.fstat(fd).st_ino > 0, "one claim released the shared descriptor"
    # A duplicate of the first claim's correct token must not consume claim #2.
    assert models.release_session_claim(sid, ownership_token=tokens[0]) is False
    assert os.fstat(fd).st_ino > 0, "duplicate release closed a sibling's descriptor"
    assert models.release_session_claim(sid, ownership_token=tokens[1]) is True
    assert sid not in models._SESSION_CLAIM_STATE


def test_q6_idle_revocation_generation_is_pruned_only_behind_a_durable_denial(
    resume_env, monkeypatch
):
    """Q6: per-id authority bookkeeping is pruned when, and only when, safe.

    A revoked, otherwise idle id keeps its refusal authority in the durable
    channels, so its in-memory generation entry is redundant and is dropped --
    the refusal survives the prune (no resurrection). An id whose generation is
    the ONLY authority left (both durable channels failed when it was bumped) is
    never pruned: dropping it would re-open writes to a revoked id.

    Red before the fix: nothing was ever pruned; in the second half the
    dangerous entry would have been dropped too.
    """
    env = resume_env
    models = env["models"]
    denied_sid = "q6-pruned-behind-durable-denial"
    memory_only_sid = "q6-in-memory-only-authority"

    generation = models.revoke_resume_publication(denied_sid, reason="q6 durable")
    assert generation > 0
    assert models.is_resume_publication_denied(denied_sid) is True
    assert models.resume_revocation_generation(denied_sid) == generation
    models._prune_resume_authority_state(denied_sid)
    assert models.resume_revocation_generation(denied_sid) == 0
    # The durable denial still refuses the id after the prune.
    assert models.is_resume_publication_denied(denied_sid) is True
    assert models.resume_publication_authority_blocked(denied_sid) is True
    with pytest.raises(KeyError):
        models.get_session(denied_sid)

    # Both durable channels fail: the in-memory generation is the only authority.
    monkeypatch.setattr(models, "_write_resume_denial_tombstone", lambda *a, **k: None)
    monkeypatch.setattr(models, "_durable_write_resume_record", lambda *a, **k: None)
    assert models.revoke_resume_publication(memory_only_sid, reason="q6 memory") > 0
    assert models.is_resume_publication_denied(memory_only_sid) is False
    assert models.resume_revocation_generation(memory_only_sid) > 0
    models._prune_resume_authority_state(memory_only_sid)
    assert models.resume_revocation_generation(memory_only_sid) > 0, (
        "the only refusal authority for a revoked id was pruned"
    )


def test_q7_authority_failure_discards_the_staged_payload(resume_env, monkeypatch):
    """Q7: a refused publication leaves no staging orphan.

    The staged payload is created and verified before durable publication
    authority is established. When that establishment fails the request returns
    500 with nothing published, so the staged duplicate must be discarded like
    the other refusing exits do.

    Red before the fix: the 500 path returned after staging and left a full
    duplicate of the transcript in ``.resume-staging``.
    """
    env = resume_env
    models = env["models"]
    routes = env["routes"]
    sid = "q7-authority-failure-orphan"
    _make_state_db(_alpha_db(env), sid=sid)

    def no_authority(*args, **kwargs):
        raise OSError("no durable publication authority")

    monkeypatch.setattr(models, "begin_resume_publication", no_authority)

    routes._handle_session_resume_in_webui(env["rec"], _body(sid=sid))

    assert env["rec"].status == 500, env["rec"].error()
    assert "authority" in env["rec"].error()
    assert not (env["sessions_dir"] / f"{sid}.json").exists()
    staging_dir = env["sessions_dir"] / ".resume-staging"
    leftovers = sorted(p.name for p in staging_dir.glob(f"{sid}*"))
    assert leftovers == [], f"a refused publication orphaned staging files: {leftovers}"


def test_q7_index_rebuild_and_patch_never_probe_authority_under_global_lock(
    resume_env, monkeypatch
):
    """Q7: durable authority I/O never runs under process-global ``LOCK``."""
    env = resume_env
    models = env["models"]
    sid = "q7-index-lock-scope"
    session = models.Session(
        session_id=sid,
        title="Q7 lock scope",
        profile="alpha",
        messages=[{"role": "user", "content": "hello"}],
    )
    real_admissible = models.session_publication_admissible
    calls = []
    lock_held_calls = []

    def assert_unlocked(*args, **kwargs):
        # ``LOCK`` is an RLock, so a same-thread nonblocking acquire would be a
        # false green. Probe from another thread to prove the caller does not
        # hold the process-global lock across the authority predicate.
        acquired = threading.Event()

        def probe_lock():
            with models.LOCK:
                acquired.set()

        probe = threading.Thread(target=probe_lock, daemon=True)
        probe.start()
        probe.join(2.0)
        if probe.is_alive() or not acquired.is_set():
            # Record instead of raising here: ``all_sessions`` intentionally
            # catches index-path exceptions and would otherwise hide this test
            # failure by falling back to a full scan.
            lock_held_calls.append(args[0].session_id)
        calls.append(args[0].session_id)
        return real_admissible(*args, **kwargs)

    monkeypatch.setattr(models, "session_publication_admissible", assert_unlocked)
    with models.LOCK:
        models.SESSIONS[sid] = session
    try:
        # Full rebuild path.
        models._write_session_index(updates=None)
        assert calls == [sid]
        assert lock_held_calls == []

        # Existing-index fast path.
        calls.clear()
        lock_held_calls.clear()
        models._write_session_index(updates=[session])
        assert calls == [sid]
        assert lock_held_calls == []

        # Cached sidebar overlay path.
        calls.clear()
        lock_held_calls.clear()
        rows = models.all_sessions()
        assert any(row.get("session_id") == sid for row in rows)
        assert sid in calls
        assert lock_held_calls == [], (
            "durable publication authority was probed under global LOCK"
        )
    finally:
        with models.LOCK:
            models.SESSIONS.pop(sid, None)


def test_q8_denial_io_for_one_id_never_holds_global_authority_lock(
    resume_env, monkeypatch
):
    """Q8: slow durable denial for one id cannot stall unrelated authority."""
    models = resume_env["models"]
    sid_a = "q8-slow-denial-a"
    entered = threading.Event()
    allow_a = threading.Event()
    b_acquired = threading.Event()

    real_deny = models.deny_resume_publication

    def slow_deny(sid, **kwargs):
        if sid == sid_a:
            entered.set()
            assert allow_a.wait(20.0), "slow denial outlived the barrier"
            return True
        return real_deny(sid, **kwargs)

    monkeypatch.setattr(models, "deny_resume_publication", slow_deny)

    thread_a = threading.Thread(
        target=models.mark_resume_publication_denied,
        args=(sid_a,),
        kwargs={"reason": "q8 slow denial"},
        daemon=True,
    )
    thread_a.start()
    assert entered.wait(20.0), "A never reached durable denial I/O"

    def acquire_unrelated_authority():
        with models._RESUME_AUTHORITY_LOCK:
            b_acquired.set()
        allow_a.set()

    thread_b = threading.Thread(target=acquire_unrelated_authority, daemon=True)
    thread_b.start()
    try:
        thread_b.join(2.0)
        assert not thread_b.is_alive(), (
            "unrelated authority was stalled by another id's durable denial I/O"
        )
        assert b_acquired.is_set()
    finally:
        allow_a.set()
        thread_a.join(20.0)
        thread_b.join(20.0)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()


def test_q8_prune_never_stats_canonical_under_global_authority_lock(
    resume_env, monkeypatch
):
    """Q8: canonical probing during pruning stays outside the global lock."""
    env = resume_env
    models = env["models"]
    sid = "q8-prune-probe"
    canonical = env["sessions_dir"] / f"{sid}.json"
    entered = threading.Event()
    allow_probe = threading.Event()
    lock_acquired = threading.Event()
    real_exists = models.Path.exists

    monkeypatch.setattr(
        models,
        "_read_resume_ledger_entry",
        lambda *args, **kwargs: {"state": models.RESUME_LEDGER_DENIED},
    )
    monkeypatch.setattr(models, "is_resume_publication_denied", lambda *a, **k: True)

    def slow_exists(path):
        if path == canonical:
            entered.set()
            assert allow_probe.wait(20.0), "canonical probe outlived the barrier"
            return False
        return real_exists(path)

    monkeypatch.setattr(models.Path, "exists", slow_exists)

    with models._RESUME_AUTHORITY_LOCK:
        models._RESUME_REVOCATION_GENERATION[sid] = 1

    thread_a = threading.Thread(
        target=models._prune_resume_authority_state, args=(sid,), daemon=True
    )
    thread_a.start()
    assert entered.wait(20.0), "prune never reached the canonical probe"

    def acquire_authority():
        with models._RESUME_AUTHORITY_LOCK:
            lock_acquired.set()
        allow_probe.set()

    thread_b = threading.Thread(target=acquire_authority, daemon=True)
    thread_b.start()
    try:
        thread_b.join(2.0)
        assert not thread_b.is_alive(), "canonical stat ran under global authority lock"
        assert lock_acquired.is_set()
    finally:
        allow_probe.set()
        thread_a.join(20.0)
        thread_b.join(20.0)
        with models._RESUME_AUTHORITY_LOCK:
            models._RESUME_REVOCATION_GENERATION.pop(sid, None)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()


def test_q9_tracked_admission_never_probes_disk_under_global_authority_lock(
    resume_env, monkeypatch
):
    """Q9: the final tracked admission fence probes authority outside the lock."""
    models = resume_env["models"]
    sid = "q9-admission-probe"
    session = models.Session(
        session_id=sid,
        title="Q9 admission",
        profile="alpha",
        messages=[{"role": "user", "content": "hello"}],
    )
    session.resume_publication_state = models.RESUME_PUBLICATION_VERIFIED

    calls = 0
    entered = threading.Event()
    allow_probe = threading.Event()
    lock_acquired = threading.Event()
    admitted = []

    def parked_authority_probe(*args, **kwargs):
        nonlocal calls
        calls += 1
        # One admissibility pass performs two authority probes. The third call
        # is the old final recheck under _RESUME_AUTHORITY_LOCK; after the fix
        # it is the first probe in a second pass under the per-SID ledger
        # transaction, but outside the process-global lock.
        if calls == 3:
            entered.set()
            assert allow_probe.wait(20.0), "authority probe outlived the barrier"
        return False

    monkeypatch.setattr(
        models, "resume_publication_authority_blocked", parked_authority_probe
    )

    thread_a = threading.Thread(
        target=lambda: admitted.append(
            models.admit_session(sid, session, cache_on_miss=False)
        ),
        daemon=True,
    )
    thread_a.start()
    assert entered.wait(20.0), "admission never reached its final authority probe"

    def acquire_authority():
        with models._RESUME_AUTHORITY_LOCK:
            lock_acquired.set()
        allow_probe.set()

    thread_b = threading.Thread(target=acquire_authority, daemon=True)
    thread_b.start()
    try:
        thread_b.join(2.0)
        assert not thread_b.is_alive(), (
            "tracked admission probed durable authority under the global lock"
        )
        assert lock_acquired.is_set()
    finally:
        allow_probe.set()
        thread_a.join(20.0)
        thread_b.join(20.0)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert admitted == [True]


def test_f3_eviction_persistence_io_runs_outside_global_locks(
    resume_env, monkeypatch
):
    """F3: tracked admission cannot hold global locks across eviction I/O."""
    env = resume_env
    models = env["models"]
    ordinary_sid = "f3-ordinary-eviction-candidate"
    tracked_sid = "f3-tracked-admission"
    _isolate_resume_index(monkeypatch, models, env)

    ordinary = models.Session(
        session_id=ordinary_sid,
        title="Persisted eviction candidate",
        profile="alpha",
        messages=[{"role": "user", "content": "persisted"}],
    )
    ordinary.save()
    tracked, _ = _make_tracked_resume_sidecar(
        env, tracked_sid, marker=models.RESUME_PUBLICATION_VERIFIED
    )
    with models.LOCK:
        models.SESSIONS.clear()
        models.SESSIONS[ordinary_sid] = ordinary
    monkeypatch.setattr(models._cfg, "get_sessions_cache_max", lambda *a, **k: 1)

    entered = threading.Event()
    release_stat = threading.Event()
    authority_available = threading.Event()
    cache_lock_available = threading.Event()
    admitted = []
    errors = []
    real_stat = models.Path.stat
    ordinary_path = ordinary.path

    def parked_stat(path, *args, **kwargs):
        if path == ordinary_path:
            entered.set()
            assert release_stat.wait(20.0), "eviction stat outlived the barrier"
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(models.Path, "stat", parked_stat)

    def admit_tracked():
        try:
            admitted.append(models.admit_session(tracked_sid, tracked))
        except Exception as exc:  # pragma: no cover - diagnostic
            errors.append(exc)

    admission = threading.Thread(target=admit_tracked, daemon=True)
    admission.start()
    assert entered.wait(20.0), "admission never reached eviction persistence I/O"

    def probe_locks():
        if models._RESUME_AUTHORITY_LOCK.acquire(timeout=2.0):
            authority_available.set()
            models._RESUME_AUTHORITY_LOCK.release()
        if models.LOCK.acquire(timeout=2.0):
            cache_lock_available.set()
            models.LOCK.release()

    probe = threading.Thread(target=probe_locks, daemon=True)
    probe.start()
    probe.join(5.0)
    try:
        assert not probe.is_alive(), "global-lock probe did not complete"
        assert authority_available.is_set(), (
            "eviction filesystem I/O ran under the global Resume authority lock"
        )
        assert cache_lock_available.is_set(), (
            "eviction filesystem I/O ran under the global session cache lock"
        )
    finally:
        release_stat.set()
        admission.join(20.0)
        probe.join(20.0)

    assert not admission.is_alive()
    assert not errors, errors
    assert admitted == [True]


def test_f4_unrelated_resume_ids_never_share_a_sid_lock(resume_env):
    """F4: unrelated Resume identities must not serialize on lock shards."""
    models = resume_env["models"]
    first_sid = None
    second_sid = None
    seen = {}
    for index in range(10_000):
        sid = f"f4-lock-collision-{index}"
        bucket = hash(sid) % 256
        previous = seen.get(bucket)
        if previous is not None and previous != sid:
            first_sid, second_sid = previous, sid
            break
        seen[bucket] = sid
    assert first_sid is not None and second_sid is not None, (
        "the production lock implementation exposed no collision in 10k ids"
    )

    acquired = threading.Event()
    release_second = threading.Event()

    def hold_second():
        with models._resume_sid_lock(second_sid):
            acquired.set()
            release_second.wait(10.0)

    with models._resume_sid_lock(first_sid):
        worker = threading.Thread(target=hold_second, daemon=True)
        worker.start()
        try:
            assert acquired.wait(2.0), (
                f"unrelated ids {first_sid!r} and {second_sid!r} shared one lock"
            )
        finally:
            release_second.set()
    assert _join_thread(worker), "the unrelated SID lock worker did not finish"
    assert first_sid not in models._RESUME_SID_LOCK_REGISTRY
    assert second_sid not in models._RESUME_SID_LOCK_REGISTRY
