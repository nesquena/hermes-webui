import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def _write_sidecar(session_dir: Path, sid: str, **overrides):
    payload = {
        "session_id": sid,
        "title": overrides.get("title", "Audit Test"),
        "workspace": str(session_dir.parent),
        "model": "test-model",
        "created_at": 1000.0,
        "updated_at": 1000.0,
        "pinned": False,
        "archived": False,
        "profile": "default",
        "messages": [{"role": "user", "content": "hi"}],
        "tool_calls": [],
    }
    payload.update(overrides)
    if "message_count" not in payload and isinstance(payload.get("messages"), list):
        payload["message_count"] = len(payload["messages"])
    p = session_dir / f"{sid}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _make_state_db(path: Path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, pinned INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT, message_count INTEGER)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (id, source, session_source, title, pinned, archived, started_at, ended_at, parent_session_id, end_reason, message_count) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["id"],
                r.get("source", "webui"),
                r.get("session_source"),
                r.get("title", "T"),
                int(bool(r.get("pinned", 0))),
                int(bool(r.get("archived", 0))),
                r.get("started_at", 1000.0),
                r.get("ended_at"),
                r.get("parent_session_id"),
                r.get("end_reason"),
                r.get("message_count", 1),
            ),
        )
        conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", (r["id"], "user", "hi", 1000.0))
    conn.commit()
    conn.close()


def test_audit_cli_emits_json_aggregate_only(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid1", pinned=True, archived=False, title="Secret Title AAA", messages=[{"role": "user", "content": "secret transcript BBB"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid1", "pinned": 0, "archived": 0}])
    result = subprocess.run(
        [sys.executable, "scripts/audit_session_metadata_sync.py", "--session-dir", str(session_dir), "--state-db", str(db_path), "--profile", "default", "--json"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parents[1]),
    )
    assert result.returncode == 0
    diag = json.loads(result.stdout)
    assert diag["profile"] == "default"
    assert diag["pinned_mismatch"]["json_true_core_false"] == 1
    dumped = result.stdout
    assert "Secret Title AAA" not in dumped
    assert "secret transcript BBB" not in dumped
    assert "sid1" not in dumped


def test_audit_cli_requires_explicit_paths(tmp_path):
    result = subprocess.run(
        [sys.executable, "scripts/audit_session_metadata_sync.py", "--help"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parents[1]),
    )
    assert result.returncode == 0
    assert "--session-dir" in result.stdout
    assert "--state-db" in result.stdout
    result2 = subprocess.run(
        [sys.executable, "scripts/audit_session_metadata_sync.py"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parents[1]),
    )
    assert result2.returncode != 0
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid1", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid1", "pinned": 0, "archived": 0}])
    repo_root = Path(__file__).parents[1]
    script_path = str(repo_root / "scripts" / "audit_session_metadata_sync.py")
    for blank in ["", "   ", "\t"]:
        isolated = tmp_path / f"audit_cwd_{blank.encode('unicode_escape').decode()}"
        isolated.mkdir(exist_ok=True)
        r1 = subprocess.run([sys.executable, script_path, "--session-dir", blank, "--state-db", str(db_path), "--profile", "default"], capture_output=True, text=True, cwd=str(isolated))
        assert r1.returncode == 2, f"blank session-dir {blank!r} should parser-fail with code 2"
        assert r1.stdout == ""
        assert "non-empty" in (r1.stderr + r1.stdout).lower() or "whitespace" in (r1.stderr + r1.stdout).lower()
        r2 = subprocess.run([sys.executable, script_path, "--session-dir", str(session_dir), "--state-db", blank, "--profile", "default"], capture_output=True, text=True, cwd=str(isolated))
        assert r2.returncode == 2, f"blank state-db {blank!r} should parser-fail with code 2"
        assert r2.stdout == ""
        assert "non-empty" in (r2.stderr + r2.stdout).lower() or "whitespace" in (r2.stderr + r2.stdout).lower()
        r3 = subprocess.run([sys.executable, script_path, "--session-dir", str(session_dir), "--state-db", str(db_path), "--profile", blank], capture_output=True, text=True, cwd=str(isolated))
        assert r3.returncode == 2
        assert r3.stdout == ""
    good = subprocess.run([sys.executable, script_path, "--session-dir", str(session_dir), "--state-db", str(db_path), "--profile", "default", "--json"], capture_output=True, text=True, cwd=str(repo_root))
    assert good.returncode == 0
    assert json.loads(good.stdout)["profile"] == "default"


def test_audit_cli_no_apply_terminology():
    result = subprocess.run(
        [sys.executable, "scripts/audit_session_metadata_sync.py", "--help"],
        capture_output=True, text=True, cwd=str(Path(__file__).parents[1]),
    )
    help_text = (result.stdout + result.stderr).lower()
    assert "--apply" not in help_text
    assert "--yes" not in help_text
    src2 = Path("api/session_metadata_sync.py").read_text(encoding="utf-8").lower()
    assert "tombstone" not in src2
    lines = [l for l in src2.splitlines() if "migration" in l or "repair" in l]
    forbidden = [l for l in lines if "no " not in l and "never" not in l]
    assert not forbidden, f"unexpected migration/repair code: {forbidden[:3]}"


def test_audit_does_not_mutate_files(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    p = _write_sidecar(session_dir, "sid1", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid1", "pinned": 0, "archived": 0}])
    before_sidecar_bytes = p.read_bytes()
    before_sidecar_mtime = p.stat().st_mtime_ns
    before_db_mtime = db_path.stat().st_mtime_ns
    before_db_size = db_path.stat().st_size
    idx = session_dir / "_index.json"
    idx.write_text(json.dumps([{"session_id": "sid1", "pinned": False, "archived": False, "message_count": 1}]), encoding="utf-8")
    before_idx_mtime = idx.stat().st_mtime_ns
    before_idx_bytes = idx.read_bytes()
    from api.session_metadata_sync import compute_aggregate_diagnostics

    compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert p.read_bytes() == before_sidecar_bytes
    assert p.stat().st_mtime_ns == before_sidecar_mtime
    assert db_path.stat().st_mtime_ns == before_db_mtime
    assert db_path.stat().st_size == before_db_size
    assert idx.read_bytes() == before_idx_bytes
    assert idx.stat().st_mtime_ns == before_idx_mtime
    result = subprocess.run(
        [sys.executable, "scripts/audit_session_metadata_sync.py", "--session-dir", str(session_dir), "--state-db", str(db_path), "--profile", "default", "--json"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parents[1]),
    )
    assert result.returncode == 0
    assert p.read_bytes() == before_sidecar_bytes
    assert p.stat().st_mtime_ns == before_sidecar_mtime
    assert db_path.stat().st_mtime_ns == before_db_mtime


def test_minimal_schema_blocked_not_crash(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid1", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
    conn.execute("INSERT INTO sessions (id, source) VALUES ('sid1','webui')")
    conn.commit()
    conn.close()
    from api.session_metadata_sync import compute_aggregate_diagnostics

    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["blocked"]["ambiguous"] == 1
    assert diag["matched"] == 0


def test_truth_table_via_audit_counts(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics, provisional_truth

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    for a, b in [(False, False), (True, False), (False, True)]:
        for c, d in [(False, False), (True, False), (False, True)]:
            expected = provisional_truth(a, b, c, d)
            assert expected["archived"] == (b or d)
            assert expected["pinned"] == ((a or c) and not (b or d))
    _write_sidecar(session_dir, "sid1", pinned=True, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid1", "pinned": False, "archived": True}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["pinned_mismatch"]["json_true_core_false"] == 1
    assert diag["archived_mismatch"]["json_false_core_true"] == 1
    assert diag["pinned_mismatch"]["conflict"] == 1
    assert diag["archived_mismatch"]["conflict"] == 1


def test_hostile_lifecycle_audit_blocks(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    # sidecar with hostile values: list/object and non-0/1 number
    (session_dir / "sidList.json").write_text(json.dumps({"session_id": "sidList", "profile": "default", "messages": [{"role": "user", "content": "hi"}], "pinned": [], "archived": "maybe", "created_at": 1000.0, "updated_at": 1000.0}), encoding="utf-8")
    (session_dir / "sidObj.json").write_text(json.dumps({"session_id": "sidObj", "profile": "default", "messages": [{"role": "user", "content": "hi"}], "pinned": {}, "archived": 2, "created_at": 1000.0, "updated_at": 1000.0}), encoding="utf-8")
    # core with non-0/1 value (2) must be treated as unknown/blocked, not as True via truthiness
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES ('sidList', 'webui', 2, 0, 1000.0)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES ('sidObj', 'webui', 0, 99, 1001.0)")
    conn.commit()
    conn.close()
    # direct tri-state check: hostile core values are None via read_core_lifecycle_batch
    from api.session_metadata_sync import read_core_lifecycle_batch, compute_aggregate_diagnostics

    batch = read_core_lifecycle_batch(db_path, {"sidList", "sidObj"})
    assert batch["sidList"]["pinned"] is None
    assert batch["sidObj"]["archived"] is None
    # compute_aggregate_diagnostics must block (fail-closed) with no merge on hostile values
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["blocked"]["ambiguous"] >= 2
    assert diag["matched"] == 0
    assert diag["pinned_mismatch"]["json_true_core_false"] == 0
    assert diag["pinned_mismatch"]["json_false_core_true"] == 0
    assert diag["archived_mismatch"]["json_true_core_false"] == 0
    dumped = json.dumps(diag)
    assert "sidList" not in dumped
    assert "sidObj" not in dumped


def test_config_gate_off_does_not_change_rows(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid1", pinned=True, archived=False, messages=[{"role": "user", "content": "hi"}], profile="default")
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid1", "pinned": 0, "archived": 0}])
    import api.config as config
    import api.models as models
    from collections import OrderedDict

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "cfg", {"experimental": {"unified_session_db": False, "unified_session_metadata_mode": "off"}})
    rows = models.all_sessions()
    assert any(r["session_id"] == "sid1" and r["pinned"] is True for r in rows)
