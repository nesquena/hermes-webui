"""#8047 review follow-up: the persisted cwd must survive both materialize paths.

Maintainer review of 2026-10-27 (item 1 of review 5663056013) asked for two
things on the aged-out session lookup (#8061):

(a) a non-empty persisted ``cwd`` from state.db wins over the browser's
    last-workspace fallback that the CLI projection puts on every row;
(b) local sources are materialized through ``_claim_or_synthesize_cli_session``
    (enforcing its verdict) and persisted with that resolved workspace, instead
    of importing through ``get_last_workspace()``.

Both paths are driven here through the real helpers on a real isolated
state.db, with ``get_last_workspace`` pointing at a different directory than the
session's own ``cwd`` so a fallback leak is visible.
"""

import json
import sqlite3
import time

import pytest

from api import models, routes

from tests.test_pr7555_cli_session_availability import (
    _configure_real_single_profile,
    _make_state_db,
)


def _add_cwd_column(db):
    """state.db fixtures predate the ``cwd`` column; the claim path reads it."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN cwd TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def _insert_session_with_cwd(db, sid, source, cwd, *, started_at):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, model, started_at, "
        "message_count, cwd) VALUES (?, ?, ?, ?, 'openai/gpt-5', ?, 1, ?)",
        (sid, source, source, f"Title {source}", started_at, cwd),
    )
    conn.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp) "
        "VALUES (?, ?, 'user', 'hi', ?)",
        (f"msg_{sid}", sid, started_at),
    )
    conn.commit()
    conn.close()


def _aged_out_setup(tmp_path, monkeypatch, *, cwd):
    """25 desktop rows + one extra aged row carrying ``cwd``; browser ws = tmp_path."""
    db = tmp_path / "state.db"
    _make_state_db(
        db,
        sessions=25,
        messages_per_session=1,
        source="desktop",
        session_source="desktop",
    )
    _add_cwd_column(db)
    _configure_real_single_profile(monkeypatch, tmp_path, db)
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 20)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "_get_profile_home", lambda _profile: tmp_path)
    sid = "aged_cwd_desktop"
    _insert_session_with_cwd(db, sid, "desktop", cwd, started_at=time.time() - 5000)
    return db, sid


def test_claim_uses_persisted_cwd_over_browser_workspace(tmp_path, monkeypatch):
    agent_dir = tmp_path / "agent-project"
    agent_dir.mkdir()
    _db, sid = _aged_out_setup(tmp_path, monkeypatch, cwd=str(agent_dir))

    meta = routes._lookup_cli_session_metadata(sid)
    assert meta.get("session_id") == sid, "the aged-out row must still be discovered (#8061)"

    session, reason = routes._claim_or_synthesize_cli_session(sid, cli_meta=meta)

    assert reason == "materialized"
    assert session.workspace == str(agent_dir), (
        "the session's own persisted cwd must win over the browser last-workspace fallback"
    )


def test_materialize_saves_the_resolved_cwd_workspace(tmp_path, monkeypatch):
    agent_dir = tmp_path / "agent-project"
    agent_dir.mkdir()
    _db, sid = _aged_out_setup(tmp_path, monkeypatch, cwd=str(agent_dir))

    session = routes._get_or_materialize_session(sid)

    assert session.workspace == str(agent_dir)
    sidecar = tmp_path / "sessions" / f"{sid}.json"
    assert sidecar.exists(), "materializing must persist a WebUI-owned sidecar"
    saved = json.loads(sidecar.read_text(encoding="utf-8"))
    assert saved.get("workspace") == str(agent_dir), (
        "the persisted sidecar must carry the session's own workspace, not get_last_workspace()"
    )


def test_absent_cwd_keeps_the_workspace_fallback(tmp_path, monkeypatch):
    """Control: no persisted cwd -> the browser workspace is still a sane fallback."""
    _db, sid = _aged_out_setup(tmp_path, monkeypatch, cwd=None)

    meta = routes._lookup_cli_session_metadata(sid)
    session, reason = routes._claim_or_synthesize_cli_session(sid, cli_meta=meta)

    assert reason == "materialized"
    assert session.workspace == str(tmp_path)


def test_materialize_enforces_the_claim_verdict(tmp_path, monkeypatch):
    """A discovered-but-unclaimable row must be refused, not imported writable."""
    db = tmp_path / "state.db"
    _make_state_db(
        db,
        sessions=2,
        messages_per_session=1,
        source="external_agent",
        session_source="external_agent",
    )
    _add_cwd_column(db)
    _configure_real_single_profile(monkeypatch, tmp_path, db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "_get_profile_home", lambda _profile: tmp_path)

    sid = "cli_perf_0001"
    foreign_meta = {
        "session_id": sid,
        "title": "Foreign",
        "model": "openai/gpt-5",
        "source_tag": "external_agent",
        "raw_source": "external_agent",
        "session_source": "external_agent",
        "profile": "default",
    }
    monkeypatch.setattr(
        routes,
        "_resolve_cli_import_metadata",
        lambda session_id, **_kwargs: dict(foreign_meta),
    )

    with pytest.raises(PermissionError):
        routes._get_or_materialize_session(sid)

    assert not (tmp_path / "sessions" / f"{sid}.json").exists(), (
        "an unclaimable foreign row must not be materialized as a writable sidecar"
    )
