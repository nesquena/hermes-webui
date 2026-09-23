"""Coverage for api/session_squash.py — the in-process squash behind the
WebUI squash action (POST /api/session/squash[/preview|/restore] +
GET /api/session/squash/status).

Backend tests run against REAL ``Session`` objects persisted in a temp
session dir, a temp sidebar index and a temp Agent ``state.db``, so the
selection authority, detached authority, compare-and-swap, transactional
rollback, durable state barrier and restore are exercised on the production
code paths rather than on stand-ins.
"""

import collections
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.models
import api.routes
from api import session_squash


SID = "20260801_120000_ab12cd"
SUMMARY = ("synthèse fournie " * 40).strip()


# ── fixtures ─────────────────────────────────────────────────────────────

def _messages(n=4, base=1000.0):
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "content": f"{role} message {i}", "timestamp": base + i})
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated session store, sidebar index, profile home and state.db."""
    sessions_dir = tmp_path / "webui" / "sessions"
    sessions_dir.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(api.models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(api.models, "SESSION_INDEX_FILE", sessions_dir / "_index.json")
    monkeypatch.setattr(api.models, "SESSIONS", collections.OrderedDict())
    monkeypatch.setattr(api.models, "_get_profile_home", lambda _profile: home)
    monkeypatch.setattr(api.routes, "_publish_session_list_changed", lambda *a, **k: None)
    monkeypatch.setattr("api.config._evict_session_agent", lambda _sid: None)
    monkeypatch.setattr(
        "api.compression_continuation.durable_compression_continuation",
        lambda _session: (False, None),
    )
    session_squash._JOBS.clear()
    return SimpleNamespace(sessions_dir=sessions_dir, home=home, tmp=tmp_path)


def _make_session(env, sid=SID, *, archived=True, messages=None, **fields):
    s = api.models.Session(
        session_id=sid,
        title="session de test",
        workspace=str(env.tmp),
        messages=_messages() if messages is None else messages,
        archived=archived,
        profile="default",
        created_at=1000.0,
        updated_at=1003.0,
        **fields,
    )
    s.context_messages = list(s.messages)
    s.save(touch_updated_at=False)
    with api.models.LOCK:
        api.models.SESSIONS[sid] = s
    return s


def _make_state_db(env, sid=SID, rows=4, lease=None):
    db = env.home / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT);
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
            timestamp REAL, active INTEGER NOT NULL DEFAULT 1, compacted INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS session_turn_leases (
            conversation_id TEXT PRIMARY KEY, holder TEXT NOT NULL,
            acquired_at REAL NOT NULL, expires_at REAL NOT NULL);
        """
    )
    conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES (?)", (sid,))
    for i in range(rows):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
            (sid, "user" if i % 2 == 0 else "assistant", f"state row {i}", 1000.0 + i),
        )
    if lease:
        conn.execute("INSERT INTO session_turn_leases VALUES (?,?,?,?)", (sid, lease, time.time(), time.time() + 300))
    conn.commit()
    conn.close()
    return db


def _state_rows(env, sid=SID):
    conn = sqlite3.connect(env.home / "state.db")
    try:
        return conn.execute(
            "SELECT id, active, compacted FROM messages WHERE session_id = ? ORDER BY id", (sid,)
        ).fetchall()
    finally:
        conn.close()


def _index_entry(env, sid=SID):
    entries = json.loads((env.sessions_dir / "_index.json").read_text(encoding="utf-8"))
    return next((e for e in entries if e.get("session_id") == sid), None)


def _wait(job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = session_squash.squash_job_status(job_id)
        if snap["status"] in ("done", "error"):
            return snap
        time.sleep(0.02)
    raise AssertionError("squash job did not finish")


def _squash(sid=SID, *, summary=SUMMARY, confirm=None, request_profile="default"):
    authority = confirm or session_squash.preview_squash(sid, request_profile=request_profile)
    job = session_squash.start_squash_job(sid, confirm=authority, summary=summary, request_profile=request_profile)
    return _wait(job["job_id"]), authority


def _snapshot(env, sid=SID):
    path = env.sessions_dir / f"{sid}.json"
    return {
        "bytes": path.read_bytes(),
        "index": _index_entry(env, sid),
        "cache": api.models.SESSIONS.get(sid),
        "cache_messages": list(getattr(api.models.SESSIONS.get(sid), "messages", []) or []),
        "state": _state_rows(env, sid) if (env.home / "state.db").exists() else None,
        "archives": sorted(p.name for p in (env.tmp / "webui" / "session-squash-archives" / sid).glob("*.gz"))
        if (env.tmp / "webui" / "session-squash-archives" / sid).exists() else [],
    }


def _assert_unchanged(env, before, sid=SID):
    after = _snapshot(env, sid)
    assert after["bytes"] == before["bytes"], "sidecar bytes changed"
    assert after["index"] == before["index"], "sidebar index changed"
    assert after["cache"] is before["cache"], "cache object replaced"
    assert after["cache_messages"] == before["cache_messages"], "cached transcript mutated"
    assert after["state"] == before["state"], "state.db rows changed"
    assert after["archives"] == before["archives"], "archive left behind"
    leftovers = [p.name for p in env.sessions_dir.iterdir() if ".squash-" in p.name or ".restore-" in p.name]
    assert leftovers == [], leftovers


# ── happy path ───────────────────────────────────────────────────────────

def test_squash_job_collapses_archived_session(env):
    _make_session(env)
    _make_state_db(env)
    original = (env.sessions_dir / f"{SID}.json").read_bytes()
    original_sha = hashlib.sha256(original).hexdigest()

    snap, authority = _squash()
    assert snap["status"] == "done", snap.get("error")
    assert authority["source_sha256"] == original_sha
    assert authority["lineage_tip"] == SID
    assert authority["profile"] == "default"
    result = snap["result"]
    assert result["before"]["message_count"] == 4
    assert result["after"]["message_count"] == 1
    assert result["state_barrier"] == "applied"
    assert result["state_archived_rows"] == 4

    persisted = json.loads((env.sessions_dir / f"{SID}.json").read_text(encoding="utf-8"))
    assert [m.get("_squash_summary") for m in persisted["messages"]] == [True]
    assert persisted["context_messages"][0]["content"].startswith("[CONTEXT COMPACTION")
    assert persisted["compression_anchor_mode"] == "manual"
    assert persisted["truncation_watermark"] == persisted["truncation_boundary"] > 1003.0
    assert persisted["intentional_shrink_generation"] == result["squash_generation"]
    assert persisted["archived"] is True
    # Index and cache are published only after commit and agree with disk.
    assert _index_entry(env)["message_count"] == 1
    assert api.models.SESSIONS[SID].messages[0]["_squash_summary"] is True
    # Durable state barrier: every pre-squash Agent row is soft-archived.
    assert [(a, c) for _id, a, c in _state_rows(env)] == [(0, 1)] * 4

    archive = Path(result["archive_path"])
    with gzip.open(archive, "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == original_sha
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["session_id"] == SID
    assert manifest["profile"] == "default"
    assert manifest["source_sha256"] == original_sha
    assert manifest["squash_generation"] == result["squash_generation"]
    assert len(manifest["state_archived_row_ids"]) == 4
    assert not (env.sessions_dir / f"{SID}.json.bak").exists()


def test_squash_keeps_fork_parent_link(env):
    _make_session(env, "parent_fork_01", archived=False)
    _make_session(env, parent_session_id="parent_fork_01", session_source="fork")
    snap, _ = _squash()
    assert snap["status"] == "done", snap.get("error")
    persisted = json.loads((env.sessions_dir / f"{SID}.json").read_text(encoding="utf-8"))
    assert persisted["parent_session_id"] == "parent_fork_01"


def test_squash_detaches_compression_snapshot_parent(env):
    _make_session(env, "parent_snap_01", archived=True, pre_compression_snapshot=True)
    _make_session(env, parent_session_id="parent_snap_01")
    snap, _ = _squash()
    assert snap["status"] == "done", snap.get("error")
    persisted = json.loads((env.sessions_dir / f"{SID}.json").read_text(encoding="utf-8"))
    assert persisted["parent_session_id"] is None


# ── selection authority ──────────────────────────────────────────────────

def test_non_archived_target_is_refused_with_zero_writes(env):
    _make_session(env, archived=False)
    before = _snapshot(env)
    with pytest.raises(session_squash.SquashError) as exc:
        session_squash.preview_squash(SID, request_profile="default")
    assert exc.value.status == 409 and "archived" in str(exc.value)
    _assert_unchanged(env, before)


def test_sealed_compression_parent_is_not_a_tip(env, monkeypatch):
    _make_session(env)
    monkeypatch.setattr(
        "api.compression_continuation.durable_compression_continuation",
        lambda _session: (True, "child_tip_01"),
    )
    with pytest.raises(session_squash.SquashError) as exc:
        session_squash.preview_squash(SID, request_profile="default")
    assert exc.value.status == 409 and "lineage tip" in str(exc.value)


def test_pre_compression_snapshot_is_not_a_tip(env):
    _make_session(env, pre_compression_snapshot=True)
    with pytest.raises(session_squash.SquashError) as exc:
        session_squash.preview_squash(SID, request_profile="default")
    assert exc.value.status == 409


def test_live_descendant_is_refused(env):
    _make_session(env)
    _make_session(env, "child_fork_01", archived=False, parent_session_id=SID)
    before = _snapshot(env)
    with pytest.raises(session_squash.SquashError) as exc:
        session_squash.preview_squash(SID, request_profile="default")
    assert "descendant" in str(exc.value)
    _assert_unchanged(env, before)


def test_descendant_beyond_prefix_and_after_confirmation_is_refused(env):
    _make_session(env)
    authority = session_squash.preview_squash(SID, request_profile="default")
    child = _make_session(env, "child_fork_deep_01", archived=False, parent_session_id=SID)
    # The normal continuation heuristic reads only a short prefix. Simulate a
    # large metadata field before parent_session_id without relying on its order.
    child_path = child.path
    payload = json.loads(child_path.read_text(encoding="utf-8"))
    child_path.write_text(json.dumps({"padding": "x" * 5000, **payload}), encoding="utf-8")
    with api.models.LOCK:
        api.models.SESSIONS.pop(child.session_id, None)
    before = _snapshot(env)
    with pytest.raises(session_squash.SquashError, match="descendant"):
        session_squash.start_squash_job(SID, confirm=authority, summary=SUMMARY, request_profile="default")
    _assert_unchanged(env, before)


def test_lineage_probe_error_fails_closed(env, monkeypatch):
    _make_session(env)
    def _broken(_session):
        raise OSError("state unavailable")
    monkeypatch.setattr("api.compression_continuation.durable_compression_continuation", _broken)
    with pytest.raises(session_squash.SquashError, match="cannot verify"):
        session_squash.preview_squash(SID, request_profile="default")


def test_descendant_added_during_summary_blocks_commit(env, monkeypatch):
    _make_session(env)
    original = session_squash._generate_summary
    def _summarize(session, sid, provided):
        result = original(session, sid, provided)
        _make_session(env, "child_late_01", archived=False, parent_session_id=sid)
        return result
    monkeypatch.setattr(session_squash, "_generate_summary", _summarize)
    before = _snapshot(env)
    job, _ = _squash()
    assert job["status"] == "error" and "descendant" in job["error"]
    assert (env.sessions_dir / f"{SID}.json").read_bytes() == before["bytes"]


def test_confirmation_must_echo_current_authority(env):
    _make_session(env)
    authority = session_squash.preview_squash(SID, request_profile="default")
    with pytest.raises(session_squash.SquashError):
        session_squash.start_squash_job(SID, confirm={"session_id": SID}, summary=None, request_profile="default")
    stale = dict(authority, source_sha256="0" * 64)
    before = _snapshot(env)
    with pytest.raises(session_squash.SquashError) as exc:
        session_squash.start_squash_job(SID, confirm=stale, summary=None, request_profile="default")
    assert exc.value.status == 409 and "source_sha256" in str(exc.value)
    _assert_unchanged(env, before)


def test_cross_profile_request_is_refused(env):
    _make_session(env)
    with pytest.raises(session_squash.SquashError) as exc:
        session_squash.preview_squash(SID, request_profile="other-profile")
    assert "different profile" in str(exc.value)
    authority = session_squash.preview_squash(SID, request_profile="default")
    with pytest.raises(session_squash.SquashError):
        session_squash.start_squash_job(SID, confirm=authority, summary=None, request_profile="other-profile")


def test_active_or_read_only_sessions_are_refused(env):
    _make_session(env, active_stream_id="stream-123")
    with pytest.raises(session_squash.SquashError) as exc:
        session_squash.preview_squash(SID, request_profile="default")
    assert exc.value.status == 409
    _make_session(env, "20260801_120000_ro0001", read_only=True)
    with pytest.raises(session_squash.SquashError):
        session_squash.preview_squash("20260801_120000_ro0001", request_profile="default")


# ── detached authority ───────────────────────────────────────────────────

def test_detached_worker_runs_under_captured_profile_scope(env, monkeypatch):
    _make_session(env)
    seen = []
    import contextlib
    import api.profiles

    @contextlib.contextmanager
    def _scope(name, purpose="", logger_override=None):
        seen.append((name, threading.current_thread().name))
        yield

    monkeypatch.setattr(api.profiles, "profile_scope_for_detached_worker", _scope)
    snap, _ = _squash()
    assert snap["status"] == "done", snap.get("error")
    assert seen and seen[0][0] == "default"
    assert seen[0][1].startswith("session-squash-")
    assert snap["profile"] == "default"


def test_worker_enforces_frozen_authority_not_a_bare_session_id(env):
    """The detached job carries the accepted authority. If the live session no
    longer matches it (profile or digest), the worker refuses with zero writes
    instead of re-resolving whatever ``get_session(sid)`` now returns."""
    _make_session(env)
    authority = session_squash.preview_squash(SID, request_profile="default")
    before = _snapshot(env)
    for mutated in (dict(authority, profile="other-profile"), dict(authority, source_sha256="f" * 64)):
        job = {"job_id": "frozen-authority", "session_id": SID, "status": "running",
               "_authority": session_squash.SquashAuthority(**mutated)}
        session_squash._run_squash_job(job, SUMMARY)
        assert job["status"] == "error", job
        assert job["error"].startswith("session") and ("profile" in job["error"] or "digest" in job["error"]), job["error"]
    _assert_unchanged(env, before)


def test_jobs_are_keyed_by_profile_and_canonical_path(env, monkeypatch):
    _make_session(env)
    release = threading.Event()
    monkeypatch.setattr(session_squash, "_generate_summary", lambda s, sid, p: (release.wait(5), (SUMMARY, "provided"))[1])
    authority = session_squash.preview_squash(SID, request_profile="default")
    first = session_squash.start_squash_job(SID, confirm=authority, summary=None, request_profile="default")
    try:
        with pytest.raises(session_squash.SquashError) as exc:
            session_squash.start_squash_job(SID, confirm=authority, summary=None, request_profile="default")
        assert exc.value.status == 409
    finally:
        release.set()
    assert _wait(first["job_id"])["status"] == "done"


# ── cross-process exclusion + CAS ────────────────────────────────────────

def test_digest_change_after_confirmation_fails_with_zero_writes(env, monkeypatch):
    _make_session(env)
    authority = session_squash.preview_squash(SID, request_profile="default")
    path = env.sessions_dir / f"{SID}.json"

    # Another process rewrites the sidecar while the summary is generated.
    def _other_process_writes(s, sid, provided):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["messages"].append({"role": "user", "content": "written elsewhere", "timestamp": 2000.0})
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return SUMMARY, "provided"

    monkeypatch.setattr(session_squash, "_generate_summary", _other_process_writes)
    job = session_squash.start_squash_job(SID, confirm=authority, summary=None, request_profile="default")
    before_commit_bytes = None
    snap = _wait(job["job_id"])
    assert snap["status"] == "error" and "digest mismatch" in snap["error"]
    before_commit_bytes = path.read_bytes()
    assert b"written elsewhere" in before_commit_bytes
    assert not list((env.tmp / "webui" / "session-squash-archives" / SID).glob("*.gz"))


def test_replace_between_digest_and_swap_is_detected(env, monkeypatch):
    _make_session(env)
    _make_state_db(env)
    path = env.sessions_dir / f"{SID}.json"
    concurrent = {}

    def _hook(stage):
        if stage == "claimed":
            # A second process publishes a new generation at the live path in
            # the claim window (atomic replace, different inode).
            tmp = path.with_name(".concurrent.tmp")
            tmp.write_text('{"session_id": "%s", "messages": [], "concurrent": true}' % SID, encoding="utf-8")
            os.replace(tmp, path)
            concurrent["bytes"] = path.read_bytes()

    monkeypatch.setattr(session_squash, "_cas_hook", _hook)
    snap, _ = _squash()
    assert snap["status"] == "error"
    assert "concurrent writer" in snap["error"]
    # The concurrent writer's generation wins; it is never overwritten.
    assert path.read_bytes() == concurrent["bytes"]
    assert [(a, c) for _id, a, c in _state_rows(env)] == [(1, 0)] * 4


def test_second_process_holding_squash_lock_blocks(env):
    _make_session(env)
    authority = session_squash.preview_squash(SID, request_profile="default")
    before = _snapshot(env)
    lock_dir = env.tmp / "webui" / "session-squash-archives" / SID
    lock_dir.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [
            os.environ.get("PYTHON", __import__("sys").executable), "-c",
            "import fcntl,os,sys,time;fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR);"
            "fcntl.flock(fd,fcntl.LOCK_EX);print('locked',flush=True);time.sleep(30)",
            str(lock_dir / ".squash.lock"),
        ],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        job = session_squash.start_squash_job(SID, confirm=authority, summary=SUMMARY, request_profile="default")
        snap = _wait(job["job_id"])
        assert snap["status"] == "error" and "another process" in snap["error"]
    finally:
        holder.kill()
        holder.wait()
    before["archives"] = []
    _assert_unchanged(env, before)


# ── partial-failure rollback ─────────────────────────────────────────────

@pytest.mark.parametrize("stage", [
    "archive",
    "stage_verify",
    "after_replace_verify",
    "index_write",
    "index_verify",
    "state_barrier",
    "manifest_finalize",
])
def test_injected_failure_rolls_back_exactly(env, monkeypatch, stage):
    _make_session(env)
    _make_state_db(env)
    api.models._write_session_index(updates=[api.models.SESSIONS[SID]])
    before = _snapshot(env)
    boom = session_squash.SquashError(f"injected {stage}", 500)

    if stage == "archive":
        monkeypatch.setattr(session_squash, "_archive_file", lambda *a, **k: (_ for _ in ()).throw(boom))
    elif stage == "stage_verify":
        real = session_squash._verify_squashed_payload
        calls = []

        def _fail_first(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise boom
            return real(*a, **k)
        monkeypatch.setattr(session_squash, "_verify_squashed_payload", _fail_first)
    elif stage == "after_replace_verify":
        real = session_squash._verify_squashed_payload
        calls = []

        def _fail_second(*a, **k):
            calls.append(1)
            if len(calls) == 2:
                raise boom
            return real(*a, **k)
        monkeypatch.setattr(session_squash, "_verify_squashed_payload", _fail_second)
    elif stage == "index_write":
        real = session_squash._write_index_for
        calls = []

        def _fail_first_index(session):
            calls.append(1)
            if len(calls) == 1:
                real(session)  # the write lands, then the step reports failure
                raise boom
            return real(session)
        monkeypatch.setattr(session_squash, "_write_index_for", _fail_first_index)
    elif stage == "index_verify":
        monkeypatch.setattr(session_squash, "_verify_index", lambda *a, **k: (_ for _ in ()).throw(boom))
    elif stage == "state_barrier":
        def _hook(point):
            if point == "before-commit":
                raise boom
        monkeypatch.setattr(session_squash, "_state_barrier_hook", _hook)
    elif stage == "manifest_finalize":
        real = session_squash._write_manifest
        calls = []

        def _fail_second_manifest(path, manifest):
            calls.append(1)
            if len(calls) == 2:
                raise boom
            return real(path, manifest)
        monkeypatch.setattr(session_squash, "_write_manifest", _fail_second_manifest)

    snap, _ = _squash()
    assert snap["status"] == "error", snap
    assert f"injected {stage}" in snap["error"]
    _assert_unchanged(env, before)


def test_missing_state_barrier_schema_rolls_back_without_success(env):
    _make_session(env)
    db = env.home / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, active INTEGER)")
        conn.execute("INSERT INTO messages (session_id, active) VALUES (?, 1)", (SID,))
    before_bytes = (env.sessions_dir / f"{SID}.json").read_bytes()
    before_index = _index_entry(env)
    before_cache = api.models.SESSIONS[SID]
    job, _ = _squash()
    assert job["status"] == "error" and "required squash barrier" in job["error"]
    assert (env.sessions_dir / f"{SID}.json").read_bytes() == before_bytes
    assert _index_entry(env) == before_index
    assert api.models.SESSIONS[SID] is before_cache
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT active FROM messages WHERE session_id = ?", (SID,)).fetchall() == [(1,)]


def test_missing_turn_lease_table_rolls_back_without_success(env):
    _make_session(env)
    _make_state_db(env)
    with sqlite3.connect(env.home / "state.db") as conn:
        conn.execute("DROP TABLE session_turn_leases")
    before = _snapshot(env)
    job, _ = _squash()
    assert job["status"] == "error" and "lacks turn leases" in job["error"]
    _assert_unchanged(env, before)


def test_live_agent_turn_lease_blocks_state_barrier_and_rolls_back(env):
    _make_session(env)
    _make_state_db(env, lease="pid=1:turn")
    api.models._write_session_index(updates=[api.models.SESSIONS[SID]])
    before = _snapshot(env)
    snap, _ = _squash()
    assert snap["status"] == "error" and "Agent turn" in snap["error"]
    _assert_unchanged(env, before)


# ── delayed state rows / durable generation ──────────────────────────────

def test_delayed_pre_squash_state_rows_cannot_resurface(env):
    _make_session(env)
    _make_state_db(env)
    snap, _ = _squash()
    assert snap["status"] == "done", snap.get("error")
    persisted = json.loads((env.sessions_dir / f"{SID}.json").read_text(encoding="utf-8"))
    cutoff = persisted["truncation_watermark"]
    # A delayed pre-squash row lands after commit (stale writer / queue).
    conn = sqlite3.connect(env.home / "state.db")
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
        (SID, "assistant", "delayed pre-squash row", cutoff - 5),
    )
    conn.commit()
    conn.close()
    state_rows = api.models.get_state_db_session_messages(SID, profile="default")
    # Barrier: only the delayed row is still active (the 4 pre-squash rows are archived) ...
    assert [m["content"] for m in state_rows] == ["delayed pre-squash row"]
    # ... and the persisted watermark keeps it out of the merged transcript.
    merged = api.models.merge_session_messages_append_only(
        persisted["messages"], state_rows,
        truncation_watermark=persisted["truncation_watermark"],
        truncation_boundary=persisted["truncation_boundary"],
    )
    assert [m.get("_squash_summary") for m in merged] == [True]


def test_startup_recovery_honours_squash_generation(env):
    from api.session_recovery import inspect_session_recovery_status

    _make_session(env)
    path = env.sessions_dir / f"{SID}.json"
    pre = path.read_text(encoding="utf-8")
    snap, _ = _squash()
    assert snap["status"] == "done", snap.get("error")
    # A stale pre-squash .bak (e.g. written by another process) must not be
    # restored over the intentional shrink.
    path.with_suffix(".json.bak").write_text(pre, encoding="utf-8")
    status = inspect_session_recovery_status(path)
    assert status["recommend"] == "no_action", status


# ── restore ──────────────────────────────────────────────────────────────

def test_restore_drill_round_trips_bytes_index_cache_and_state(env):
    _make_session(env)
    _make_state_db(env)
    path = env.sessions_dir / f"{SID}.json"
    original = path.read_bytes()
    snap, authority = _squash()
    assert snap["status"] == "done", snap.get("error")
    result = snap["result"]
    squashed_sha = hashlib.sha256(path.read_bytes()).hexdigest()

    confirm = {"session_id": SID, "source_sha256": authority["source_sha256"], "current_sha256": squashed_sha}
    # Wrong digests / foreign profile are refused with zero writes.
    before = _snapshot(env)
    for bad_confirm, profile in (
        (dict(confirm, current_sha256="0" * 64), "default"),
        (dict(confirm, source_sha256="0" * 64), "default"),
        (confirm, "other-profile"),
    ):
        with pytest.raises(session_squash.SquashError):
            session_squash.restore_squash(SID, archive_name=result["archive_name"], confirm=bad_confirm,
                                          request_profile=profile)
    _assert_unchanged(env, before)

    restored = session_squash.restore_squash(SID, archive_name=result["archive_name"], confirm=confirm,
                                             request_profile="default")
    assert path.read_bytes() == original
    assert restored["restored_message_count"] == 4
    assert restored["state_rows_reactivated"] == 4
    assert [(a, c) for _id, a, c in _state_rows(env)] == [(1, 0)] * 4
    assert _index_entry(env)["message_count"] == 4
    assert len(api.models.SESSIONS[SID].messages) == 4
    # The squashed state stays restorable too.
    assert Path(restored["squashed_archive_path"]).is_file()


def test_restore_refuses_when_session_moved_on(env):
    _make_session(env)
    snap, authority = _squash()
    path = env.sessions_dir / f"{SID}.json"
    # A new turn after the squash: restoring would lose it.
    s = api.models.SESSIONS[SID]
    s.messages = s.messages + [{"role": "user", "content": "after squash", "timestamp": time.time()}]
    s.save()
    confirm = {"session_id": SID, "source_sha256": authority["source_sha256"],
               "current_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    with pytest.raises(session_squash.SquashError) as exc:
        session_squash.restore_squash(SID, archive_name=snap["result"]["archive_name"], confirm=confirm,
                                      request_profile="default")
    assert "digest mismatch" in str(exc.value)


def test_restore_refuses_rewritten_summary_even_with_fresh_confirmation(env):
    _make_session(env)
    snap, authority = _squash()
    path = env.sessions_dir / f"{SID}.json"
    s = api.models.SESSIONS[SID]
    s.title = "rewritten after squash"
    s.save()
    before = _snapshot(env)
    confirm = {"session_id": SID, "source_sha256": authority["source_sha256"],
               "current_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    with pytest.raises(session_squash.SquashError, match="digest mismatch"):
        session_squash.restore_squash(SID, archive_name=snap["result"]["archive_name"], confirm=confirm,
                                      request_profile="default")
    _assert_unchanged(env, before)


def test_restore_rolls_back_state_if_cache_publish_fails(env, monkeypatch):
    _make_session(env)
    _make_state_db(env)
    snap, authority = _squash()
    assert snap["status"] == "done"
    before = _snapshot(env)
    path = env.sessions_dir / f"{SID}.json"
    confirm = {"session_id": SID, "source_sha256": authority["source_sha256"],
               "current_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    def _fail_publish(_sid, _session):
        raise RuntimeError("injected cache publish failure")
    monkeypatch.setattr(session_squash, "_publish_cache", _fail_publish)
    with pytest.raises(RuntimeError, match="injected cache publish failure"):
        session_squash.restore_squash(SID, archive_name=snap["result"]["archive_name"],
                                      confirm=confirm, request_profile="default")
    _assert_unchanged(env, before)


def test_restore_rejects_path_traversal_archive_name(env):
    _make_session(env)
    with pytest.raises(session_squash.SquashError):
        session_squash.restore_squash(SID, archive_name="../x.json.gz",
                                      confirm={"session_id": SID, "source_sha256": "a", "current_sha256": "b"},
                                      request_profile="default")


# ── cancelled-writeback fence (kept from the previous gate) ──────────────

def test_cancelled_writeback_cannot_survive_squash(env):
    """turn admitted → operator stops it → cancel clears busy indicators while
    the worker still owns writeback → squash must fail closed; once the
    worker releases ownership the squash proceeds and holds the tombstone."""
    from api import config as api_config

    _make_session(env)
    path = env.sessions_dir / f"{SID}.json"
    original = path.read_bytes()
    api_config.register_session_writeback_owner(SID, "stream-old")
    try:
        with pytest.raises(session_squash.SquashError) as excinfo:
            session_squash.preview_squash(SID, request_profile="default")
        assert excinfo.value.status == 409 and "writeback" in str(excinfo.value)
        assert path.read_bytes() == original
    finally:
        api_config.clear_session_writeback_owner_if_owned(SID, "stream-old")

    owners_seen = []
    real = session_squash._commit_squash

    def _spy(session, authority, summary):
        owners_seen.append(api_config.session_writeback_owner(SID))
        return real(session, authority, summary)

    session_squash._commit_squash, saved = _spy, session_squash._commit_squash
    try:
        snap, _ = _squash()
    finally:
        session_squash._commit_squash = saved
    assert snap["status"] == "done", snap.get("error")
    assert owners_seen and all(o and o.startswith("squash-") for o in owners_seen)
    assert api_config.session_writeback_owner(SID) is None


def test_worker_recheck_refuses_ownership_registered_after_admission(env):
    from api import config as api_config

    _make_session(env)
    authority = session_squash.preview_squash(SID, request_profile="default")
    before = _snapshot(env)
    job = {"job_id": "recheck-test-job", "session_id": SID, "status": "running",
           "_authority": session_squash.SquashAuthority(**authority)}
    api_config.register_session_writeback_owner(SID, "stream-old")
    try:
        session_squash._run_squash_job(job, SUMMARY)
        assert job["status"] == "error" and "writeback" in job["error"]
        assert api_config.session_writeback_owner(SID) == "stream-old"
    finally:
        api_config.clear_session_writeback_owner_if_owned(SID, "stream-old")
    _assert_unchanged(env, before)


def test_already_squashed_is_idempotent(env):
    _make_session(env, messages=[{"role": "assistant", "content": "synthèse", "timestamp": 1.0,
                                  "_squash_summary": True}])
    snap, _ = _squash()
    assert snap["status"] == "done"
    assert snap["result"]["already_squashed"] is True


def test_empty_session_is_refused(env):
    _make_session(env, messages=[])
    snap, _ = _squash()
    assert snap["status"] == "error" and "nothing to squash" in snap["error"]


def test_distill_transcript_respects_budget():
    sess = SimpleNamespace(messages=[
        {"role": "user", "content": "demande " * 500, "timestamp": float(i)}
        for i in range(40)
    ])
    distilled = session_squash._distill_transcript(sess, budget=5000)
    assert len(distilled) <= 5000
    assert "demande" in distilled


def test_fallback_summary_has_all_sections():
    sess = SimpleNamespace(
        title="titre test",
        workspace="/tmp/ws",
        created_at=1000.0,
        updated_at=2000.0,
        messages=[
            {"role": "user", "content": "question initiale"},
            {"role": "assistant", "content": "réponse finale"},
        ],
    )
    text = session_squash._fallback_summary(sess, SID, "modèle auxiliaire indisponible")
    assert len(text) >= session_squash.MIN_SUMMARY_CHARS
    for section in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6.", "## 7.", "## 8.", "## 9."):
        assert section in text
    assert SID in text
    assert "modèle auxiliaire indisponible" in text


def test_mobile_context_panel_contains_squash_action():
    """Mobile must expose squash below the Context card, not only in the
    desktop composer footer where narrow-layout CSS hides it."""
    repo = Path(__file__).resolve().parent.parent
    html = (repo / "static" / "index.html").read_text(encoding="utf-8")
    css = (repo / "static" / "style.css").read_text(encoding="utf-8")
    js = (repo / "static" / "panels.js").read_text(encoding="utf-8")

    context_pos = html.index('id="composerMobileContextAction"')
    squash_pos = html.index('id="composerMobileSquashBtn"')
    panel_end = html.index("</div>", squash_pos)
    assert context_pos < squash_pos < panel_end
    assert 'onclick="closeMobileComposerConfig();squashConversation()"' in html
    assert "composer-mobile-config-panel .composer-mobile-squash-action{flex:1 0 100%;width:100%" in css
    assert "$('composerMobileSquashBtn')" in js


# ── #6704 frontend regressions ──────────────────────────────────────────

def test_squash_completion_reload_reconciles_same_session_navigation():
    """Completion must reconcile a superseding same-session load without
    bypassing requested-navigation authority for any other destination."""
    repo = Path(__file__).resolve().parent.parent
    js = (repo / "static" / "panels.js").read_text(encoding="utf-8")
    fn_start = js.index("async function squashConversation")
    fn_end = js.index("function _pollSquashJob")
    body = js[fn_start:fn_end]
    assert "const navigationAuthority = _captureSessionNavigationAuthority(sid);" in body
    assert "await _refreshSessionAfterConcurrentSameSessionNavigation(" in body
    assert "navigationAuthority" in body
    assert "_squashTranscriptMatchesResult(sid, r)" in body


def test_squash_running_indicator_is_owner_scoped_wiring():
    """Focused regression (#6704 P1 follow-up): 'squash-running' renders on the
    SHARED desktop/mobile controls, so it must be keyed by the owning session
    (upload-bar pattern) and re-synced on every session switch — not toggled
    unconditionally on whatever conversation happens to be displayed."""
    repo = Path(__file__).resolve().parent.parent
    panels = (repo / "static" / "panels.js").read_text(encoding="utf-8")
    sessions = (repo / "static" / "sessions.js").read_text(encoding="utf-8")

    # The per-owner state + sync/set helpers exist.
    assert "const _squashRunningSessions = new Set()" in panels
    assert "function _squashSyncRunningIndicatorForSession(" in panels
    assert "function _squashSetRunning(" in panels

    # squashConversation must go through the owner-scoped setter, never flip
    # the shared class directly on the buttons.
    fn_start = panels.index("async function squashConversation")
    fn_end = panels.index("function _pollSquashJob")
    body = panels[fn_start:fn_end]
    assert "_squashSetRunning(sid, true)" in body
    assert "_squashSetRunning(sid, false)" in body
    assert "classList.add('squash-running')" not in body
    assert "classList.remove('squash-running')" not in body

    # loadSession re-syncs the shared controls only after the current
    # destination's metadata is accepted as the displayed session. A pending,
    # failed, or stale destination must not overwrite the still-visible owner.
    ls_start = sessions.index("async function loadSession")
    ls_body = sessions[ls_start : sessions.index("\nasync function", ls_start + 10)]
    assign_pos = ls_body.index("S.session=data.session;")
    sync_pos = ls_body.index("_squashSyncRunningIndicatorForSession(S.session.session_id)")
    assert sync_pos > assign_pos
    assert ls_body.count("_squashSyncRunningIndicatorForSession(") == 1


def test_squash_running_indicator_does_not_leak_across_sessions_runtime():
    """Behavioral regression (#6704 P1 follow-up), running the REAL helper
    block from panels.js in node's ``vm``:

    start a squash on session A → switch to session B mid-job → the shared
    desktop+mobile controls must drop 'squash-running'; switch back to A →
    the indicator re-asserts; the job settles while B is displayed → B's
    controls stay clean (no flash, no stale removal on the wrong session).
    """
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    repo = Path(__file__).resolve().parent.parent
    panels = (repo / "static" / "panels.js").read_text(encoding="utf-8")
    start = panels.index("const _squashRunningSessions = new Set()")
    end = panels.index("async function squashConversation")
    helpers = panels[start:end]
    harness = textwrap.dedent(
        """
        'use strict';
        const vm = require('vm');
        function makeBtn(){
          const classes = new Set();
          return {classes, classList: {
            toggle(name, force){ if(force) classes.add(name); else classes.delete(name); },
            add(name){ classes.add(name); },
            remove(name){ classes.delete(name); },
            contains(name){ return classes.has(name); },
          }};
        }
        const desktop = makeBtn();
        const mobile = makeBtn();
        const ctx = {
          $: (id) => (id === 'btnSquash' ? desktop : (id === 'composerMobileSquashBtn' ? mobile : null)),
          S: {session: {session_id: 'sess-A'}},
        };
        vm.createContext(ctx);
        vm.runInContext(HELPERS_SRC, ctx);
        const running = () => [desktop, mobile].map(b => b.classList.contains('squash-running'));
        const out = {};
        // Viewing A, squash starts on A -> both shared controls pulse.
        vm.runInContext("_squashSetRunning('sess-A', true)", ctx);
        out.owner_shows = running();
        // User switches to B mid-job (loadSession resyncs for the new sid).
        ctx.S.session = {session_id: 'sess-B'};
        vm.runInContext("_squashSyncRunningIndicatorForSession('sess-B')", ctx);
        out.other_session_clean = running();
        // Back to the owner while the job is still running -> re-asserts.
        ctx.S.session = {session_id: 'sess-A'};
        vm.runInContext("_squashSyncRunningIndicatorForSession('sess-A')", ctx);
        out.owner_reasserts = running();
        // Switch to B again; the job settles while B is displayed -> B stays clean.
        ctx.S.session = {session_id: 'sess-B'};
        vm.runInContext("_squashSyncRunningIndicatorForSession('sess-B')", ctx);
        vm.runInContext("_squashSetRunning('sess-A', false)", ctx);
        out.settle_on_other_session_clean = running();
        // Back on A after settle -> nothing lingers.
        ctx.S.session = {session_id: 'sess-A'};
        vm.runInContext("_squashSyncRunningIndicatorForSession('sess-A')", ctx);
        out.owner_clean_after_settle = running();
        console.log(JSON.stringify(out));
        """
    ).replace("HELPERS_SRC", json.dumps(helpers))
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["owner_shows"] == [True, True]
    assert out["other_session_clean"] == [False, False], (
        "P1 leak: 'squash-running' must clear on the shared controls when a "
        "different conversation is displayed while the job runs"
    )
    assert out["owner_reasserts"] == [True, True]
    assert out["settle_on_other_session_clean"] == [False, False]
    assert out["owner_clean_after_settle"] == [False, False]


def test_squash_completion_obeys_requested_navigation_runtime():
    """Run the real squash flow against the real navigation-authority helpers.

    Metadata requests are controlled promises so the production race is exact:
    session B has been requested, but ``S.session`` still points at A when A's
    squash status settles.  The newer requested-navigation generation must own
    the view even when B is pending or fails.  A superseding A reload is instead
    reconciled after it settles: stale pre-squash data gets exactly one durable
    refresh, while data that already contains the squash result is not doubled.
    """
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")

    repo = Path(__file__).resolve().parent.parent
    sessions = (repo / "static" / "sessions.js").read_text(encoding="utf-8")
    panels = (repo / "static" / "panels.js").read_text(encoding="utf-8")
    nav_start = sessions.index("let _loadingSessionId = null")
    nav_end = sessions.index("// #3306:", nav_start)
    navigation_authority = sessions[nav_start:nav_end]
    load_start = sessions.index("async function loadSession(")
    load_end = sessions.index("\n// ── Handoff hint logic", load_start)
    load_body = sessions[load_start:load_end]
    assert "const _loadGeneration = _beginSessionNavigationRequest(sid);" in load_body
    assert "_sessionNavigationRequestIsCurrent(sid,_loadGeneration)" in load_body
    assert "_settleSessionNavigationRequest(sid,_loadGeneration)" in load_body
    assign_pos = load_body.index("S.session=data.session;")
    sync_pos = load_body.index("_squashSyncRunningIndicatorForSession(S.session.session_id)")
    assert sync_pos > assign_pos
    assert "if(currentSid===sid && !forceReload && (!_loadingSessionId || _loadingSessionId===sid))" in load_body
    squash_start = panels.index("const _squashRunningSessions")
    squash_end = panels.index("// ── Skills panel", squash_start)
    squash_flow = panels[squash_start:squash_end]

    harness = textwrap.dedent(
        r"""
        'use strict';
        const vm = require('vm');

        function deferred(){
          let resolve, reject;
          const promise = new Promise((res, rej)=>{ resolve=res; reject=rej; });
          return {promise, resolve, reject};
        }
        const settle = async()=>{
          await new Promise(resolve=>setImmediate(resolve));
          await new Promise(resolve=>setImmediate(resolve));
        };
        function makeButton(){
          const classes = new Set();
          return {classList:{
            toggle(name, force){ if(force) classes.add(name); else classes.delete(name); },
            contains(name){ return classes.has(name); },
          }};
        }
        function makeRuntime(){
          const status = deferred();
          const metadata = [];
          const loads = [];
          const loadErrors = [];
          const toasts = [];
          const desktop = makeButton();
          const mobile = makeButton();
          const ctx = {
            S:{
              session:{session_id:'sess-A'},
              messages:[{role:'assistant',content:'A'}],
              toolCalls:[], pendingFiles:[],
            },
            $:(id)=>id==='btnSquash'?desktop:(id==='composerMobileSquashBtn'?mobile:null),
            t:(key)=>key,
            showConfirmDialog:async()=>true,
            showToast:(...args)=>toasts.push(args),
            api:async(url, opts)=>{
              if(url==='/api/session/squash/preview') return {authority:{session_id:JSON.parse(opts.body).session_id}};
              if(url==='/api/session/squash') return {job:{job_id:'job-A'}};
              if(url.startsWith('/api/session/squash/status')) return status.promise;
              throw new Error('unexpected api '+url+' '+JSON.stringify(opts||{}));
            },
            _requestMetadata:(sid)=>{
              const request=deferred();
              metadata.push({sid, request});
              return request.promise;
            },
            _loads:loads,
            _loadErrors:loadErrors,
            setTimeout,
            clearTimeout,
            console,
          };
          vm.createContext(ctx);
          vm.runInContext(NAVIGATION_AUTHORITY_SRC, ctx);
          vm.runInContext(SQUASH_FLOW_SRC, ctx);
          // This small runtime adapter deliberately uses the SAME request/current
          // helpers as production loadSession.  Controlled metadata promises keep
          // S.session on A until the requested B response is explicitly released.
          vm.runInContext(`
            async function loadSession(sid, opts={}){
              const currentSid=S.session&&S.session.session_id;
              const forceReload=!!opts.force;
              if(currentSid===sid && !forceReload && (!_loadingSessionId || _loadingSessionId===sid)) return;
              const generation=_beginSessionNavigationRequest(sid);
              _loads.push({sid, force:!!opts.force, generation});
              try{
                const data=await _requestMetadata(sid);
                if(!_sessionNavigationRequestIsCurrent(sid,generation)) return;
                S.session=data.session;
                S.messages=Array.isArray(data.session.messages)?data.session.messages:[];
                if(typeof _squashSyncRunningIndicatorForSession==='function'){
                  _squashSyncRunningIndicatorForSession(S.session.session_id);
                }
                _loadingSessionId=null;
              }catch(error){
                _loadErrors.push({sid,message:String(error&&error.message||error)});
                if(_sessionNavigationRequestIsCurrent(sid,generation)) _loadingSessionId=null;
              }finally{
                await _settleSessionNavigationRequest(sid,generation);
              }
            }
          `, ctx);
          return {
            ctx, status, metadata, loads, loadErrors, toasts, desktop, mobile,
            buttons:()=>[desktop,mobile].map(btn=>btn.classList.contains('squash-running')),
          };
        }
        const run=(rt, source)=>vm.runInContext(source, rt.ctx);
        const done=(sessionId='sess-A')=>({job:{
          job_id:'job-A', session_id:sessionId, status:'done',
          result:{already_squashed:false,before:{message_count:4},after:{message_count:1}},
        }});
        const preSquash={session:{
          session_id:'sess-A', message_count:4,
          messages:[{role:'assistant',content:'pre-squash transcript'}],
        }};
        const postSquash={session:{
          session_id:'sess-A', message_count:1,
          messages:[{role:'assistant',content:'durable squash summary',_squash_summary:true}],
        }};
        const requestFor=(rt, sid, index=0)=>rt.metadata.filter(item=>item.sid===sid)[index];
        function startSquash(rt){
          return {promise:run(rt,'squashConversation()'), ready:settle()};
        }

        (async()=>{
          const out={};

          // A forced reload of the SAME session captured the old transcript
          // before squash committed, then finishes after the job. Completion
          // must wait for it and issue exactly one post-squash refresh.
          {
            const rt=makeRuntime();
            const started=startSquash(rt);
            await started.ready;
            const squash=started.promise;
            const concurrent=run(rt,"loadSession('sess-A',{force:true})");
            await settle();
            rt.status.resolve(done());
            await settle();
            out.stale_same_buttons_while_pending=rt.buttons();
            requestFor(rt,'sess-A',0).request.resolve(preSquash);
            await settle();
            const refresh=requestFor(rt,'sess-A',1);
            if(refresh) refresh.request.resolve(postSquash);
            await Promise.all([squash,concurrent]);
            out.stale_same={
              active:rt.ctx.S.session.session_id,
              messages:rt.ctx.S.messages.map(m=>m.content),
              loads:rt.loads.map(x=>`${x.sid}:${x.force}`),
              buttons:rt.buttons(),
            };
          }

          // The concurrent A load can itself observe the already-committed
          // squash. Its marker satisfies reconciliation, so no second reload.
          {
            const rt=makeRuntime();
            const started=startSquash(rt);
            await started.ready;
            const squash=started.promise;
            const concurrent=run(rt,"loadSession('sess-A',{force:true})");
            await settle();
            rt.status.resolve(done());
            await settle();
            requestFor(rt,'sess-A',0).request.resolve(postSquash);
            await Promise.all([squash,concurrent]);
            out.current_same={
              messages:rt.ctx.S.messages.map(m=>m.content),
              loads:rt.loads.map(x=>`${x.sid}:${x.force}`),
              buttons:rt.buttons(),
            };
          }

          // A failed concurrent load still gets one bounded durable retry. If
          // that retry also fails, the flow settles without a loop or a false
          // squash-failure toast; controls stay running between both attempts.
          {
            const rt=makeRuntime();
            const started=startSquash(rt);
            await started.ready;
            const squash=started.promise;
            const concurrent=run(rt,"loadSession('sess-A',{force:true})");
            await settle();
            rt.status.resolve(done());
            await settle();
            requestFor(rt,'sess-A',0).request.reject(new Error('stale A load failed'));
            await settle();
            const retry=requestFor(rt,'sess-A',1);
            out.failed_same_buttons_between_attempts=rt.buttons();
            if(retry) retry.request.reject(new Error('post-squash refresh failed'));
            await Promise.all([squash,concurrent]);
            out.failed_same={
              loads:rt.loads.map(x=>`${x.sid}:${x.force}`),
              errors:rt.loadErrors.map(x=>x.sid),
              buttons:rt.buttons(),
              squashFailed:rt.toasts.some(args=>String(args[0]).includes('squash_failed')),
            };
          }

          // If another destination is requested after the A marker is queued,
          // that navigation cancels the marker. It must never linger and fire
          // on a later A visit.
          {
            const rt=makeRuntime();
            const started=startSquash(rt);
            await started.ready;
            const squash=started.promise;
            const concurrentA=run(rt,"loadSession('sess-A',{force:true})");
            await settle();
            rt.status.resolve(done());
            await settle();
            const navB=run(rt,"loadSession('sess-B')");
            await settle();
            requestFor(rt,'sess-A',0).request.resolve(preSquash);
            requestFor(rt,'sess-B',0).request.resolve({session:{session_id:'sess-B',messages:[]}});
            await Promise.all([squash,concurrentA,navB]);
            out.marker_cancelled_by_b={
              active:rt.ctx.S.session.session_id,
              loads:rt.loads.map(x=>`${x.sid}:${x.force}`),
              buttons:rt.buttons(),
            };
          }

          // B metadata is pending when A completes: B remains authoritative.
          {
            const rt=makeRuntime();
            const started=startSquash(rt);
            await started.ready;
            const squash=started.promise;
            const navB=run(rt,"loadSession('sess-B')");
            await settle();
            out.pending_b_buttons=rt.buttons();
            rt.status.resolve(done());
            await settle();
            const unexpectedA=requestFor(rt,'sess-A');
            if(unexpectedA) unexpectedA.request.resolve({session:{session_id:'sess-A'}});
            requestFor(rt,'sess-B').request.resolve({session:{session_id:'sess-B'}});
            await Promise.all([squash,navB]);
            out.pending_b={
              active:rt.ctx.S.session.session_id,
              loads:rt.loads.map(x=>`${x.sid}:${x.force}`),
              buttons:rt.buttons(),
            };
          }

          // With no newer navigation, completion owns exactly one forced A refresh.
          {
            const rt=makeRuntime();
            const started=startSquash(rt);
            await started.ready;
            const squash=started.promise;
            out.owner_buttons=rt.buttons();
            rt.status.resolve(done());
            await settle();
            requestFor(rt,'sess-A').request.resolve({session:{session_id:'sess-A'}});
            await squash;
            out.no_navigation={
              active:rt.ctx.S.session.session_id,
              loads:rt.loads.map(x=>`${x.sid}:${x.force}`),
              buttons:rt.buttons(),
            };
          }

          // B -> explicit A: the explicit A request already observes the
          // post-squash transcript, so completion must not duplicate it.
          {
            const rt=makeRuntime();
            const started=startSquash(rt);
            await started.ready;
            const squash=started.promise;
            const navB=run(rt,"loadSession('sess-B')");
            await settle();
            const navA=run(rt,"loadSession('sess-A')");
            await settle();
            out.back_a_buttons=rt.buttons();
            rt.status.resolve(done());
            await settle();
            requestFor(rt,'sess-A').request.resolve(postSquash);
            requestFor(rt,'sess-B').request.resolve({session:{session_id:'sess-B'}});
            await Promise.all([squash,navA,navB]);
            out.back_a={
              active:rt.ctx.S.session.session_id,
              loads:rt.loads.map(x=>`${x.sid}:${x.force}`),
              buttons:rt.buttons(),
            };
          }

          // A failed B request remains a newer user choice.  Its error must not
          // be erased by a late squash-completion navigation back to A.
          {
            const rt=makeRuntime();
            const started=startSquash(rt);
            await started.ready;
            const squash=started.promise;
            const navB=run(rt,"loadSession('sess-B')");
            await settle();
            requestFor(rt,'sess-B').request.reject(new Error('B metadata failed'));
            await navB;
            const beforeCompletionButtons=rt.buttons();
            rt.status.resolve(done());
            await settle();
            const unexpectedA=requestFor(rt,'sess-A');
            if(unexpectedA) unexpectedA.request.resolve({session:{session_id:'sess-A'}});
            await squash;
            out.failed_b={
              active:rt.ctx.S.session.session_id,
              loads:rt.loads.map(x=>`${x.sid}:${x.force}`),
              errors:rt.loadErrors.map(x=>x.sid),
              beforeCompletionButtons,
            };
          }

          // Poll results are owner-scoped too: a B job can never complete A's
          // progress owner or trigger A's reload.
          {
            const rt=makeRuntime();
            const started=startSquash(rt);
            await started.ready;
            const squash=started.promise;
            rt.status.resolve(done('sess-B'));
            await squash;
            out.wrong_owner={
              loads:rt.loads.map(x=>`${x.sid}:${x.force}`),
              buttons:rt.buttons(),
              failed:rt.toasts.some(args=>String(args[0]).includes('squash_failed')),
            };
          }

          console.log(JSON.stringify(out));
        })().catch(error=>{
          console.error(error&&error.stack||error);
          process.exitCode=1;
        });
        """
    ).replace("NAVIGATION_AUTHORITY_SRC", json.dumps(navigation_authority)).replace(
        "SQUASH_FLOW_SRC", json.dumps(squash_flow)
    )
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["stale_same_buttons_while_pending"] == [True, True]
    assert out["stale_same"] == {
        "active": "sess-A",
        "messages": ["durable squash summary"],
        "loads": ["sess-A:true", "sess-A:true"],
        "buttons": [False, False],
    }
    assert out["current_same"] == {
        "messages": ["durable squash summary"],
        "loads": ["sess-A:true"],
        "buttons": [False, False],
    }
    assert out["failed_same_buttons_between_attempts"] == [True, True]
    assert out["failed_same"] == {
        "loads": ["sess-A:true", "sess-A:true"],
        "errors": ["sess-A", "sess-A"],
        "buttons": [False, False],
        "squashFailed": False,
    }
    assert out["marker_cancelled_by_b"] == {
        "active": "sess-B",
        "loads": ["sess-A:true", "sess-B:false"],
        "buttons": [False, False],
    }

    assert out["pending_b_buttons"] == [True, True]
    assert out["pending_b"] == {
        "active": "sess-B", "loads": ["sess-B:false"], "buttons": [False, False],
    }
    assert out["owner_buttons"] == [True, True]
    assert out["no_navigation"] == {
        "active": "sess-A", "loads": ["sess-A:true"], "buttons": [False, False],
    }
    assert out["back_a_buttons"] == [True, True]
    assert out["back_a"] == {
        "active": "sess-A",
        "loads": ["sess-B:false", "sess-A:false"],
        "buttons": [False, False],
    }
    assert out["failed_b"] == {
        "active": "sess-A",
        "loads": ["sess-B:false"],
        "errors": ["sess-B"],
        "beforeCompletionButtons": [True, True],
    }
    assert out["wrong_owner"] == {
        "loads": [], "buttons": [False, False], "failed": True,
    }
