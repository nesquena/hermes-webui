"""Project assignment is durable metadata of one compression lineage.

The production regression was a successful ``POST /api/session/move`` followed by
an authoritative ``GET /api/sessions`` replacing the optimistic project chip with
``null``.  These tests use real sidecars plus a real state.db compression graph so
they exercise the server mutation and sidebar-authority seams together.
"""

from collections import OrderedDict
import json
import sqlite3
from types import SimpleNamespace

import pytest


ROOT = "20260812_101401_f002bb"
MIDDLE = "20260812_102343_19117b"
TIP = "20260812_113743_e99b89"
FORK = "20260812_114500_fork001"
FORK_TIP = "20260812_114550_forktip"
ORDINARY_CHILD = "20260812_114600_child01"
PROJECT = "proj-lineage"


def _make_state_db(path, *, malformed=False):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT,
            model_config TEXT
        );
        CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        """
    )
    rows = [
        (ROOT, "webui", None, "Production lineage", 100.0, 1, None, 150.0, "compression"),
        (MIDDLE, "webui", None, "Production lineage #2", 151.0, 1, ROOT, 200.0, "compression"),
        (TIP, "webui", None, "Production lineage #3", 201.0, 1, MIDDLE, None, None),
        (FORK, "webui", "fork", "Independent fork", 202.0, 1, MIDDLE, None, None),
        # A physical parent edge is not proof that WebUI's compression rotation
        # carried the parent's sidecar lineage into this ordinary child.
        (ORDINARY_CHILD, "webui", None, "Ordinary child", 203.0, 1, MIDDLE, None, None),
    ]
    if malformed:
        rows[1] = (MIDDLE, "webui", None, "Production lineage #2", 151.0, 1, "missing-parent", 200.0, "compression")
    conn.executemany(
        "INSERT INTO sessions "
        "(id, source, session_source, title, started_at, message_count, "
        "parent_session_id, ended_at, end_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    for idx, sid in enumerate((ROOT, MIDDLE, TIP, FORK, ORDINARY_CHILD), start=1):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
            (sid, f"message {sid}", float(100 + idx * 50)),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def lineage_store(monkeypatch, tmp_path):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(
        json.dumps([
            {
                "project_id": PROJECT,
                "name": "Lineage project",
                "profile": "default",
                "color": "#123456",
                "created_at": 1.0,
            }
        ]),
        encoding="utf-8",
    )
    state_db = tmp_path / "state.db"
    _make_state_db(state_db)

    sessions = OrderedDict()
    # The ordinary child exists only in Agent state.db. It has no WebUI sidecar
    # and must not be materialized merely because a project mutation traverses
    # the compression-ended parent row.
    for idx, sid in enumerate((ROOT, MIDDLE, TIP, FORK), start=1):
        session = models.Session(
            session_id=sid,
            title=(
                "Independent fork" if sid == FORK
                else "Ordinary child" if sid == ORDINARY_CHILD
                else "Production lineage"
            ),
            workspace=str(tmp_path),
            model="test-model",
            messages=[{"role": "user", "content": f"message {sid}", "timestamp": float(100 + idx * 50)}],
            created_at=float(90 + idx),
            updated_at=float(100 + idx),
            profile="default",
            parent_session_id=(
                MIDDLE if sid in {TIP, FORK}
                else ROOT if sid == MIDDLE
                else None
            ),
            pre_compression_snapshot=sid in {ROOT, MIDDLE},
            session_source="fork" if sid == FORK else None,
        )
        sessions[sid] = session

    monkeypatch.setattr(config, "STATE_DIR", tmp_path, raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(config, "PROJECTS_FILE", projects_file, raising=False)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file, raising=False)
    monkeypatch.setattr(models, "SESSIONS", sessions, raising=False)
    monkeypatch.setattr(routes, "SESSIONS", sessions, raising=False)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(routes, "PROJECTS_FILE", projects_file, raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default", raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db, raising=False)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: state_db, raising=False)
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default", raising=False)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)

    for session in sessions.values():
        session.save(touch_updated_at=False)

    def post_move(session_id, project_id):
        captured = {}
        monkeypatch.setattr(
            routes,
            "read_body",
            lambda _handler: {"session_id": session_id, "project_id": project_id},
        )
        monkeypatch.setattr(
            routes,
            "j",
            lambda _handler, payload, status=200, extra_headers=None: captured.update(
                payload=payload, status=status
            ) or True,
        )
        monkeypatch.setattr(
            routes,
            "bad",
            lambda _handler, message, status=400: captured.update(
                payload={"error": message}, status=status
            ) or True,
        )
        assert routes.handle_post(object(), SimpleNamespace(path="/api/session/move")) is True
        return captured

    def sidebar_rows():
        payload = routes._build_session_list_cache_payload(
            active_profile="default",
            all_profiles=False,
            show_cli_sessions=False,
            show_previous_messaging_sessions=False,
            show_cron_sessions=False,
            show_claude_code_sessions=False,
            include_archived=True,
            show_webhook_sessions=False,
        )
        return routes._session_list_payload_to_response(payload)["sessions"]

    return SimpleNamespace(
        routes=routes,
        models=models,
        sessions=sessions,
        state_db=state_db,
        post_move=post_move,
        sidebar_rows=sidebar_rows,
    )


def _projects_by_id(rows):
    return {row["session_id"]: row.get("project_id") for row in rows}


def test_move_stale_segment_survives_authoritative_refresh_and_reload(lineage_store):
    result = lineage_store.post_move(MIDDLE, PROJECT)

    assert result["status"] == 200
    assert result["payload"]["session"]["project_id"] == PROJECT
    projects = _projects_by_id(lineage_store.sidebar_rows())
    assert projects[TIP] == PROJECT
    assert projects[FORK] is None
    assert all(
        lineage_store.models.Session.load(sid).project_id == PROJECT
        for sid in (ROOT, MIDDLE, TIP)
    )

    # Simulate a process reload: discard every in-memory Session and rebuild the
    # authoritative sidebar rows from sidecars + state.db.
    lineage_store.sessions.clear()
    reloaded = _projects_by_id(lineage_store.sidebar_rows())
    assert reloaded[TIP] == PROJECT
    assert reloaded[FORK] is None
    assert all(
        lineage_store.models.Session.load(sid).project_id == PROJECT
        for sid in (ROOT, MIDDLE, TIP)
    )


def test_move_state_db_only_tip_updates_every_existing_lineage_sidecar(lineage_store):
    """A canonical URL may advance before WebUI persists the tip sidecar."""
    lineage_store.models.Session.load(TIP).path.unlink()
    lineage_store.sessions.pop(TIP, None)

    result = lineage_store.post_move(TIP, PROJECT)

    assert result["status"] == 200, result
    assert result["payload"]["session"]["project_id"] == PROJECT
    assert lineage_store.models.Session.load(TIP) is None
    assert all(
        lineage_store.models.Session.load(sid).project_id == PROJECT
        for sid in (ROOT, MIDDLE)
    )
    assert lineage_store.models.Session.load(FORK).project_id is None


def test_existing_corrupt_state_db_fails_closed_without_partial_move(lineage_store):
    lineage_store.state_db.write_bytes(b"not a sqlite database")

    result = lineage_store.post_move(ROOT, PROJECT)

    assert result["status"] == 409
    assert all(
        lineage_store.models.Session.load(sid).project_id is None
        for sid in (ROOT, MIDDLE, TIP, FORK)
    )


def test_state_db_only_tip_rejects_cross_profile_materialized_lineage(lineage_store):
    lineage_store.models.Session.load(TIP).path.unlink()
    lineage_store.sessions.pop(TIP, None)
    root = lineage_store.models.Session.load(ROOT)
    root.profile = "other-profile"
    root.save(touch_updated_at=False)

    result = lineage_store.post_move(TIP, PROJECT)

    assert result["status"] == 409
    assert all(
        lineage_store.models.Session.load(sid).project_id is None
        for sid in (ROOT, MIDDLE, FORK)
    )


def test_move_stale_visible_segment_crosses_mixed_historical_source_tags(lineage_store):
    """Durable WebUI rotation markers outrank historical state.db source drift."""
    conn = sqlite3.connect(lineage_store.state_db)
    conn.execute("UPDATE sessions SET source = 'cli' WHERE id = ?", (ROOT,))
    conn.execute("UPDATE sessions SET source = 'webui' WHERE id = ?", (MIDDLE,))
    conn.execute("UPDATE sessions SET source = 'cli' WHERE id = ?", (TIP,))
    conn.commit()
    conn.close()

    result = lineage_store.post_move(MIDDLE, PROJECT)

    assert result["status"] == 200
    assert result["payload"]["session"]["project_id"] == PROJECT
    assert all(
        lineage_store.models.Session.load(sid).project_id == PROJECT
        for sid in (ROOT, MIDDLE, TIP)
    )
    lineage_store.sessions.clear()
    assert _projects_by_id(lineage_store.sidebar_rows())[TIP] == PROJECT


def test_move_tip_then_unassign_root_updates_whole_lineage(lineage_store):
    assigned = lineage_store.post_move(TIP, PROJECT)
    assert assigned["status"] == 200
    assert all(
        lineage_store.models.Session.load(sid).project_id == PROJECT
        for sid in (ROOT, MIDDLE, TIP)
    )

    unassigned = lineage_store.post_move(ROOT, None)
    assert unassigned["status"] == 200
    assert all(
        lineage_store.models.Session.load(sid).project_id is None
        for sid in (ROOT, MIDDLE, TIP)
    )
    assert lineage_store.models.Session.load(FORK).project_id is None


def test_move_fork_does_not_mutate_parent_compression_lineage(lineage_store):
    result = lineage_store.post_move(FORK, PROJECT)

    assert result["status"] == 200
    assert lineage_store.models.Session.load(FORK).project_id == PROJECT
    assert all(
        lineage_store.models.Session.load(sid).project_id is None
        for sid in (ROOT, MIDDLE, TIP)
    )


def test_move_compressed_fork_updates_only_that_fork_lineage(lineage_store):
    """Compression descendants inherit a fork's lineage, not its original parent."""
    conn = sqlite3.connect(lineage_store.state_db)
    conn.execute(
        "UPDATE sessions SET ended_at = 210, end_reason = 'compression' WHERE id = ?",
        (FORK,),
    )
    inherited_branch_marker = json.dumps({"_branched_from": MIDDLE})
    conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = ?",
        (inherited_branch_marker, FORK),
    )
    conn.execute(
        "INSERT INTO sessions "
        "(id, source, session_source, title, started_at, message_count, parent_session_id, model_config) "
        "VALUES (?, 'webui', 'fork', 'Compressed fork', 211, 1, ?, ?)",
        (FORK_TIP, FORK, inherited_branch_marker),
    )
    conn.commit()
    conn.close()
    fork = lineage_store.models.Session.load(FORK)
    fork.pre_compression_snapshot = True
    fork.save(touch_updated_at=False)
    fork_tip = lineage_store.models.Session(
        session_id=FORK_TIP,
        title="Compressed fork",
        workspace=str(lineage_store.state_db.parent),
        model="test-model",
        messages=[{"role": "user", "content": "fork tip", "timestamp": 211.0}],
        created_at=211.0,
        updated_at=211.0,
        profile="default",
        parent_session_id=FORK,
        session_source="fork",
    )
    fork_tip.save(touch_updated_at=False)
    lineage_store.sessions[FORK] = fork
    lineage_store.sessions[FORK_TIP] = fork_tip

    result = lineage_store.post_move(FORK_TIP, PROJECT)

    assert result["status"] == 200
    assert all(
        lineage_store.models.Session.load(sid).project_id == PROJECT
        for sid in (FORK, FORK_TIP)
    )
    assert all(
        lineage_store.models.Session.load(sid).project_id is None
        for sid in (ROOT, MIDDLE, TIP)
    )


def test_move_lineage_does_not_mutate_ordinary_parent_linked_child(lineage_store):
    result = lineage_store.post_move(ROOT, PROJECT)

    assert result["status"] == 200
    assert all(
        lineage_store.models.Session.load(sid).project_id == PROJECT
        for sid in (ROOT, MIDDLE, TIP)
    )
    assert lineage_store.models.Session.load(ORDINARY_CHILD) is None
    assert ORDINARY_CHILD not in lineage_store.sessions


def test_ambiguous_sidecar_backed_ordinary_child_fails_closed(lineage_store):
    ordinary = lineage_store.models.Session(
        session_id=ORDINARY_CHILD,
        title="Production lineage",
        workspace=str(lineage_store.state_db.parent),
        model="test-model",
        messages=[{"role": "user", "content": "ordinary child", "timestamp": 203.0}],
        created_at=90.0,
        updated_at=203.0,
        profile="default",
        parent_session_id=MIDDLE,
        pre_compression_snapshot=False,
    )
    ordinary.save(touch_updated_at=False)
    lineage_store.sessions[ORDINARY_CHILD] = ordinary

    result = lineage_store.post_move(ROOT, PROJECT)

    assert result["status"] == 409
    assert all(
        lineage_store.models.Session.load(sid).project_id is None
        for sid in (ROOT, MIDDLE, TIP, FORK, ORDINARY_CHILD)
    )


def test_model_config_branch_marker_stays_independent(lineage_store):
    conn = sqlite3.connect(lineage_store.state_db)
    conn.execute(
        "UPDATE sessions SET session_source = NULL, model_config = ? WHERE id = ?",
        (json.dumps({"_branched_from": MIDDLE}), FORK),
    )
    conn.commit()
    conn.close()

    result = lineage_store.post_move(ROOT, PROJECT)

    assert result["status"] == 200
    assert lineage_store.models.Session.load(FORK).project_id is None


def test_model_config_delegate_marker_stays_independent(lineage_store):
    conn = sqlite3.connect(lineage_store.state_db)
    conn.execute(
        "UPDATE sessions SET session_source = NULL, model_config = ? WHERE id = ?",
        (json.dumps({"_delegate_from": MIDDLE}), FORK),
    )
    conn.commit()
    conn.close()

    result = lineage_store.post_move(ROOT, PROJECT)

    assert result["status"] == 200
    assert lineage_store.models.Session.load(FORK).project_id is None


def test_dropped_inherited_branch_marker_starts_new_independent_lineage(lineage_store):
    marker = json.dumps({"_branched_from": ROOT})
    conn = sqlite3.connect(lineage_store.state_db)
    conn.execute("UPDATE sessions SET model_config = ? WHERE id = ?", (marker, MIDDLE))
    conn.execute("UPDATE sessions SET model_config = NULL WHERE id = ?", (TIP,))
    conn.commit()
    conn.close()

    result = lineage_store.post_move(TIP, PROJECT)

    assert result["status"] == 200
    assert lineage_store.models.Session.load(TIP).project_id == PROJECT
    assert lineage_store.models.Session.load(MIDDLE).project_id is None


@pytest.mark.parametrize(
    "malformed",
    [
        {"_branched_from": ROOT, "_delegate_from": ROOT},
        {"_branched_from": [ROOT]},
    ],
)
def test_malformed_branch_markers_fail_closed(lineage_store, malformed):
    conn = sqlite3.connect(lineage_store.state_db)
    conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = ?",
        (json.dumps(malformed), TIP),
    )
    conn.commit()
    conn.close()

    result = lineage_store.post_move(TIP, PROJECT)

    assert result["status"] == 409
    assert all(
        lineage_store.models.Session.load(sid).project_id is None
        for sid in (ROOT, MIDDLE, TIP)
    )


def test_malformed_lineage_fails_closed_without_partial_writes(lineage_store):
    conn = sqlite3.connect(lineage_store.state_db)
    conn.execute(
        "UPDATE sessions SET parent_session_id = 'missing-parent' WHERE id = ?",
        (MIDDLE,),
    )
    conn.commit()
    conn.close()

    result = lineage_store.post_move(MIDDLE, PROJECT)

    assert result["status"] == 409
    assert all(
        lineage_store.models.Session.load(sid).project_id is None
        for sid in (ROOT, MIDDLE, TIP, FORK)
    )


def test_contended_lineage_lock_returns_busy_without_partial_writes(lineage_store):
    lock = lineage_store.routes._get_session_agent_lock(TIP)
    assert lock.acquire(timeout=0.1)
    try:
        result = lineage_store.post_move(ROOT, PROJECT)
    finally:
        lock.release()

    assert result["status"] == 503
    assert all(
        lineage_store.models.Session.load(sid).project_id is None
        for sid in (ROOT, MIDDLE, TIP, FORK)
    )


def test_lineage_authority_disappearing_under_lock_fails_closed(lineage_store, monkeypatch):
    original_resolve = lineage_store.routes.resolve_writable_compression_lineage
    calls = 0

    def disappear_after_lock(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return {
                "found": False,
                "complete": False,
                "error": False,
                "root_id": None,
                "segment_ids": [],
            }
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(
        lineage_store.routes,
        "resolve_writable_compression_lineage",
        disappear_after_lock,
    )

    result = lineage_store.post_move(ROOT, PROJECT)

    assert result["status"] == 409
    assert lineage_store.models.Session.load(ROOT).project_id is None


def test_save_failure_rolls_back_already_written_segments(lineage_store, monkeypatch):
    original_save = lineage_store.models.Session.save
    original_updated_at = {
        sid: lineage_store.models.Session.load(sid).updated_at
        for sid in (ROOT, MIDDLE, TIP, FORK)
    }

    def fail_middle_assignment(session, *args, **kwargs):
        if session.session_id == MIDDLE and session.project_id == PROJECT:
            raise OSError("injected sidecar write failure")
        return original_save(session, *args, **kwargs)

    monkeypatch.setattr(lineage_store.models.Session, "save", fail_middle_assignment)

    result = lineage_store.post_move(ROOT, PROJECT)

    assert result["status"] == 500
    assert all(
        lineage_store.models.Session.load(sid).project_id is None
        for sid in (ROOT, MIDDLE, TIP, FORK)
    )
    assert all(
        lineage_store.models.Session.load(sid).updated_at == original_updated_at[sid]
        for sid in (ROOT, MIDDLE, TIP, FORK)
    )


def test_index_failure_rolls_back_sidecar_already_replaced(lineage_store, monkeypatch):
    original_save = lineage_store.models.Session.save
    original_updated_at = lineage_store.models.Session.load(ROOT).updated_at

    def fail_after_root_sidecar_replace(session, *args, **kwargs):
        if session.session_id == ROOT and session.project_id == PROJECT:
            original_write_index = lineage_store.models._write_session_index

            def fail_index(*_args, **_kwargs):
                raise OSError("injected index failure")

            monkeypatch.setattr(lineage_store.models, "_write_session_index", fail_index)
            try:
                return original_save(session, *args, **kwargs)
            finally:
                monkeypatch.setattr(
                    lineage_store.models,
                    "_write_session_index",
                    original_write_index,
                )
        return original_save(session, *args, **kwargs)

    monkeypatch.setattr(lineage_store.models.Session, "save", fail_after_root_sidecar_replace)

    result = lineage_store.post_move(ROOT, PROJECT)

    assert result["status"] == 500
    assert lineage_store.models.Session.load(ROOT).project_id is None
    assert lineage_store.models.Session.load(ROOT).updated_at == original_updated_at


def test_prepared_project_move_journal_rolls_back_partial_crash(lineage_store):
    originals = {
        sid: lineage_store.models.Session.load(sid).updated_at
        for sid in (ROOT, MIDDLE, TIP)
    }
    journal = {
        "txid": "prepared-crash",
        "state": "prepared",
        "target_project_id": PROJECT,
        "target_updated_at": 999.0,
        "entries": [
            {"session_id": sid, "project_id": None, "updated_at": originals[sid]}
            for sid in (ROOT, MIDDLE, TIP)
        ],
    }
    lineage_store.models._write_project_move_journal(journal)
    for sid in (ROOT, MIDDLE):
        session = lineage_store.models.Session.load(sid)
        session.project_id = PROJECT
        session.updated_at = 999.0
        session.save(touch_updated_at=False)

    assert lineage_store.models.recover_project_move_journals() is True
    assert all(
        lineage_store.models.Session.load(sid).project_id is None
        for sid in (ROOT, MIDDLE, TIP)
    )
    assert all(
        lineage_store.models.Session.load(sid).updated_at == originals[sid]
        for sid in (ROOT, MIDDLE, TIP)
    )


def test_committed_project_move_journal_finishes_partial_crash(lineage_store):
    journal = {
        "txid": "committed-crash",
        "state": "committed",
        "target_project_id": PROJECT,
        "target_updated_at": 999.0,
        "entries": [
            {
                "session_id": sid,
                "project_id": None,
                "updated_at": lineage_store.models.Session.load(sid).updated_at,
            }
            for sid in (ROOT, MIDDLE, TIP)
        ],
    }
    lineage_store.models._write_project_move_journal(journal)
    root = lineage_store.models.Session.load(ROOT)
    root.project_id = PROJECT
    root.updated_at = 999.0
    root.save(touch_updated_at=False)

    assert lineage_store.models.recover_project_move_journals() is True
    assert all(
        lineage_store.models.Session.load(sid).project_id == PROJECT
        for sid in (ROOT, MIDDLE, TIP)
    )
    assert all(
        lineage_store.models.Session.load(sid).updated_at == 999.0
        for sid in (ROOT, MIDDLE, TIP)
    )
