import json
import sqlite3
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

import api.config as config
import api.models as models


def _write_sidecar(session_dir: Path, sid: str, **overrides):
    payload = {
        "session_id": sid,
        "title": overrides.get("title", "Test"),
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
    path = session_dir / f"{sid}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload, path


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


def test_config_defaults_shadow_disabled():
    from api.config import _apply_config_defaults

    cfg = {}
    _apply_config_defaults(cfg)
    assert cfg["experimental"]["unified_session_db"] is False
    assert cfg["experimental"]["unified_session_metadata_mode"] == "off"
    assert config.is_unified_session_db_enabled(cfg) is False
    assert config.is_unified_session_metadata_shadow_enabled(cfg) is False
    assert config.get_unified_session_metadata_mode(cfg) == "off"
    # also verify explicit isolated mapping via direct dict
    isolated = {"experimental": {"unified_session_db": False, "unified_session_metadata_mode": "off"}}
    assert config.is_unified_session_metadata_shadow_enabled(isolated) is False


def test_config_shadow_requires_master_flag(monkeypatch):
    monkeypatch.setattr(config, "cfg", {"experimental": {"unified_session_db": False, "unified_session_metadata_mode": "shadow"}})
    assert config.is_unified_session_metadata_shadow_enabled() is False
    assert config.get_unified_session_metadata_mode() == "shadow"
    monkeypatch.setattr(config, "cfg", {"experimental": {"unified_session_db": True, "unified_session_metadata_mode": "shadow"}})
    assert config.is_unified_session_metadata_shadow_enabled() is True


def test_config_invalid_mode_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "cfg", {"experimental": {"unified_session_db": True, "unified_session_metadata_mode": "bogus"}})
    assert config.get_unified_session_metadata_mode() == "off"
    assert config.is_unified_session_metadata_shadow_enabled() is False
    monkeypatch.setattr(config, "cfg", {"experimental": {"unified_session_db": True, "unified_session_metadata_mode": ""}})
    assert config.get_unified_session_metadata_mode() == "off"
    monkeypatch.setattr(config, "cfg", {"experimental": {"unified_session_db": True, "unified_session_metadata_mode": None}})
    assert config.get_unified_session_metadata_mode() == "off"


def test_config_sync_does_not_enable_shadow(monkeypatch):
    monkeypatch.setattr(config, "cfg", {"experimental": {"unified_session_db": True, "unified_session_metadata_mode": "sync"}})
    assert config.get_unified_session_metadata_mode() == "sync"
    assert config.is_unified_session_metadata_shadow_enabled() is False
    assert config.is_unified_session_metadata_shadow_enabled({"experimental": {"unified_session_db": True, "unified_session_metadata_mode": "sync"}}) is False


def test_config_case_and_whitespace_normalization(monkeypatch):
    monkeypatch.setattr(config, "cfg", {"experimental": {"unified_session_db": True, "unified_session_metadata_mode": " SHADOW "}})
    assert config.get_unified_session_metadata_mode() == "shadow"
    assert config.is_unified_session_metadata_shadow_enabled() is True
    monkeypatch.setattr(config, "cfg", {"experimental": {"unified_session_db": True, "unified_session_metadata_mode": "Sync"}})
    assert config.get_unified_session_metadata_mode() == "sync"


def test_core_lifecycle_preserves_unknown_vs_false_vs_true(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
    conn.execute("INSERT INTO sessions (id, source) VALUES ('sid1','webui')")
    conn.commit()
    conn.close()
    from api.session_metadata_sync import read_core_lifecycle_batch

    result = read_core_lifecycle_batch(db_path, {"sid1"})
    assert result["sid1"]["pinned"] is None
    assert result["sid1"]["archived"] is None
    assert result["sid1"]["exists"] is True

    db_path2 = tmp_path / "state2.db"
    _make_state_db(db_path2, [{"id": "sid2", "pinned": 0, "archived": 0}])
    result2 = read_core_lifecycle_batch(db_path2, {"sid2"})
    assert result2["sid2"]["pinned"] is False
    assert result2["sid2"]["archived"] is False

    db_path3 = tmp_path / "state3.db"
    _make_state_db(db_path3, [{"id": "sid3", "pinned": 1, "archived": 1}])
    result3 = read_core_lifecycle_batch(db_path3, {"sid3"})
    assert result3["sid3"]["pinned"] is True
    assert result3["sid3"]["archived"] is True

    result4 = read_core_lifecycle_batch(db_path3, {"missing"})
    assert result4["missing"]["pinned"] is None
    assert result4["missing"]["archived"] is None
    assert result4["missing"]["exists"] is False


def test_core_lifecycle_read_is_readonly_no_mutation(tmp_path):
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid1", "pinned": 1, "archived": 0}])
    before_mtime = db_path.stat().st_mtime_ns
    before_size = db_path.stat().st_size
    from api.session_metadata_sync import read_core_lifecycle_batch

    read_core_lifecycle_batch(db_path, {"sid1"})
    assert db_path.stat().st_mtime_ns == before_mtime
    assert db_path.stat().st_size == before_size
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _, p = _write_sidecar(session_dir, "sid1", pinned=True, archived=False)
    before = p.read_text(encoding="utf-8")
    before_stat = p.stat()
    from api.session_metadata_sync import compute_aggregate_diagnostics

    compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert p.read_text(encoding="utf-8") == before
    assert p.stat().st_mtime_ns == before_stat.st_mtime_ns


def test_shadow_mode_does_not_alter_returned_rows(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sidA", pinned=True, archived=False, messages=[{"role": "user", "content": "hi"}], profile="default")
    _write_sidecar(session_dir, "sidB", pinned=False, archived=True, messages=[{"role": "user", "content": "hi"}], profile="default")
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sidA", "pinned": 0, "archived": 0}, {"id": "sidB", "pinned": 1, "archived": 0}])

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)

    monkeypatch.setattr(config, "cfg", {"experimental": {"unified_session_db": False, "unified_session_metadata_mode": "off"}})
    rows_off = models.all_sessions()
    monkeypatch.setattr(config, "cfg", {"experimental": {"unified_session_db": True, "unified_session_metadata_mode": "shadow"}})
    rows_shadow = models.all_sessions()
    assert sorted(rows_off, key=lambda r: r["session_id"]) == sorted(rows_shadow, key=lambda r: r["session_id"])
    for r in rows_shadow:
        if r["session_id"] == "sidA":
            assert r["pinned"] is True
            assert r["archived"] is False
        if r["session_id"] == "sidB":
            assert r["pinned"] is False
            assert r["archived"] is True


def test_shadow_enabled_does_not_call_comparator_or_scan(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid1", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}], profile="default")
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid1", "pinned": 0, "archived": 0}])
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    before_mtime = db_path.stat().st_mtime_ns
    before_size = db_path.stat().st_size
    idx_path = session_dir / "_index.json"
    seed_bytes = json.dumps([{"session_id": "sid1", "pinned": False, "archived": False, "message_count": 1}]).encode()
    idx_path.write_bytes(seed_bytes)
    seed_mtime = idx_path.stat().st_mtime_ns
    import api.session_metadata_sync as sms

    called = {"count": 0}
    orig = sms.compute_aggregate_diagnostics

    def _wrapped(*a, **k):
        called["count"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(sms, "compute_aggregate_diagnostics", _wrapped)
    monkeypatch.setattr(sms, "shadow_compare", _wrapped)
    monkeypatch.setattr(models, "clear_cli_sessions_cache", lambda: called.__setitem__("clear", True), raising=False)
    called["clear"] = False
    monkeypatch.setattr(config, "cfg", {"experimental": {"unified_session_db": True, "unified_session_metadata_mode": "shadow"}})
    rows = models.all_sessions()
    assert called["count"] == 0
    assert called["clear"] is False
    assert db_path.stat().st_mtime_ns == before_mtime
    assert db_path.stat().st_size == before_size
    shadow_bytes = idx_path.read_bytes()
    shadow_mtime = idx_path.stat().st_mtime_ns
    assert any(r["session_id"] == "sid1" for r in rows)
    from api.session_metadata_sync import compute_aggregate_diagnostics as direct

    diag = direct(session_dir, db_path, profile="default")
    assert diag["matched"] == 1
    # off vs shadow equivalence: shadow must not mutate index beyond normal all_sessions behavior
    idx_path.write_bytes(seed_bytes)
    try:
        import time
        time.sleep(0.01)
    except Exception:
        pass
    monkeypatch.setattr(config, "cfg", {"experimental": {"unified_session_db": False, "unified_session_metadata_mode": "off"}})
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    rows_off = models.all_sessions()
    off_bytes = idx_path.read_bytes()
    off_mtime = idx_path.stat().st_mtime_ns
    assert shadow_bytes == off_bytes
    assert shadow_mtime != seed_mtime or off_mtime != seed_mtime or shadow_bytes == seed_bytes
    assert sorted(rows, key=lambda r: r["session_id"]) == sorted(rows_off, key=lambda r: r["session_id"])


def test_profile_isolation(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    dir_default = tmp_path / "default_sessions"
    dir_other = tmp_path / "other_sessions"
    dir_default.mkdir()
    dir_other.mkdir()
    _write_sidecar(dir_default, "sid_shared", pinned=True, archived=False, profile="default", messages=[{"role": "user", "content": "hi"}])
    _write_sidecar(dir_other, "sid_shared", pinned=False, archived=False, profile="other", messages=[{"role": "user", "content": "hi"}])
    db_default = tmp_path / "default.db"
    db_other = tmp_path / "other.db"
    _make_state_db(db_default, [{"id": "sid_shared", "pinned": 0, "archived": 0}])
    _make_state_db(db_other, [{"id": "sid_shared", "pinned": 0, "archived": 0}])
    diag_default = compute_aggregate_diagnostics(dir_default, db_default, profile="default")
    assert diag_default["pinned_mismatch"]["json_true_core_false"] == 1
    diag_other = compute_aggregate_diagnostics(dir_other, db_other, profile="other")
    assert diag_other["matched"] == 1
    assert diag_other["pinned_mismatch"]["json_true_core_false"] == 0


def test_cross_profile_sidecars_do_not_compare(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid_default", pinned=True, archived=False, profile="default", messages=[{"role": "user", "content": "hi"}])
    _write_sidecar(session_dir, "sid_other", pinned=True, archived=False, profile="other", messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid_default", "pinned": 0, "archived": 0}, {"id": "sid_other", "pinned": 0, "archived": 0}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["blocked"]["ambiguous"] >= 1
    assert diag["pinned_mismatch"]["json_true_core_false"] == 1
    assert diag["matched"] == 0


def test_profile_binding_fail_closed(tmp_path):
    import pytest

    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid1", pinned=False, archived=False, profile="default", messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid1", "pinned": 0, "archived": 0}])
    for bad in [None, "", "   "]:
        with pytest.raises(ValueError):
            compute_aggregate_diagnostics(session_dir, db_path, profile=bad)


def test_cli_rejects_missing_empty_profile(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid1", pinned=False, archived=False, profile="default", messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid1", "pinned": 0, "archived": 0}])
    base = [sys.executable, "scripts/audit_session_metadata_sync.py", "--session-dir", str(session_dir), "--state-db", str(db_path)]
    r1 = subprocess.run(base, capture_output=True, text=True, cwd=str(Path(__file__).parents[1]))
    assert r1.returncode != 0
    r2 = subprocess.run(base + ["--profile", ""], capture_output=True, text=True, cwd=str(Path(__file__).parents[1]))
    assert r2.returncode != 0
    r3 = subprocess.run(base + ["--profile", "   "], capture_output=True, text=True, cwd=str(Path(__file__).parents[1]))
    assert r3.returncode != 0
    r4 = subprocess.run(base + ["--profile", "default", "--json"], capture_output=True, text=True, cwd=str(Path(__file__).parents[1]))
    assert r4.returncode == 0
    diag = json.loads(r4.stdout)
    assert diag["profile"] == "default"
    dumped = r4.stdout
    assert "sid1" not in dumped


def test_foreign_source_core_rows_excluded_from_core_only(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "coreForeign1", "pinned": 0, "archived": 0, "source": "cron"}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["core_only"] == 0
    assert diag["blocked"]["ambiguous"] == 0
    assert diag["total_lineages"] == 0
    assert diag["total_core_lineages"] == 0


def test_foreign_core_only_yields_zero_eligible_counts(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "coreForeignOnly", "pinned": 0, "archived": 0, "source": "cron"}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["total_lineages"] == 0
    assert diag["total_core_lineages"] == 0
    assert diag["total_sidecar_lineages"] == 0
    assert diag["core_only"] == 0
    assert diag["blocked"]["ambiguous"] == 0
    assert diag["blocked"]["active"] == 0
    assert diag["blocked"]["unreadable"] == 0
    dumped = json.dumps(diag)
    assert "coreForeignOnly" not in dumped


def test_unverified_matching_core_rows_block(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid1", pinned=False, archived=False, profile="default", messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid1", "pinned": 0, "archived": 0, "source": "cron"}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["matched"] == 0
    assert diag["blocked"]["ambiguous"] == 1
    assert diag["pinned_mismatch"]["json_true_core_false"] == 0


def test_malformed_sidecar_blocks(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "bad.json").write_text("{ not json", encoding="utf-8")
    _write_sidecar(session_dir, "good1", pinned=False, archived=False, profile="default", messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "good1", "pinned": 0, "archived": 0}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["total_lineages"] == 2
    assert diag["matched"] == 1
    assert diag["blocked"]["ambiguous"] == 1
    assert diag["blocked"]["unreadable"] == 0
    dumped = json.dumps(diag)
    assert "bad.json" not in dumped
    assert "good1" not in dumped


def test_unanchorable_malformed_sidecar_seeds_once(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / " .json").write_text("{ not json", encoding="utf-8")
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [])

    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")

    assert diag["total_lineages"] == 0
    assert diag["matched"] == 0
    assert diag["blocked"]["ambiguous"] == 1
    assert diag["blocked"]["unreadable"] == 0
    assert " .json" not in json.dumps(diag)


def test_id_mismatch_blocks(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    payload = {
        "session_id": "payload_id",
        "title": "Test",
        "workspace": str(session_dir.parent),
        "model": "test-model",
        "created_at": 1000.0,
        "updated_at": 1000.0,
        "pinned": False,
        "archived": False,
        "profile": "default",
        "messages": [{"role": "user", "content": "hi"}],
        "tool_calls": [],
        "message_count": 1,
    }
    (session_dir / "file_id.json").write_text(json.dumps(payload), encoding="utf-8")
    _write_sidecar(session_dir, "good1", pinned=False, archived=False, profile="default", messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "good1", "pinned": 0, "archived": 0}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["total_lineages"] == 3
    assert diag["matched"] == 1
    assert diag["blocked"]["ambiguous"] == 2
    assert diag["blocked"]["unreadable"] == 0
    dumped = json.dumps(diag)
    assert "payload_id" not in dumped
    assert "file_id" not in dumped
    assert "good1" not in dumped


def test_messages_invalid_blocks(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "badMsgs", pinned=False, archived=False, profile="default", messages=None)
    (session_dir / "badMsgs.json").write_text(json.dumps({"session_id": "badMsgs", "profile": "default", "messages": "not-a-list", "pinned": False, "archived": False, "created_at": 1000.0, "updated_at": 1000.0}), encoding="utf-8")
    _write_sidecar(session_dir, "good1", pinned=False, archived=False, profile="default", messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "good1", "pinned": 0, "archived": 0}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["matched"] == 1
    assert diag["blocked"]["ambiguous"] >= 1


def test_has_pending_user_message_blocks(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "pend1", pinned=False, archived=False, profile="default", messages=[{"role": "user", "content": "hi"}], has_pending_user_message=True)
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "pend1", "pinned": 0, "archived": 0}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["blocked"]["active"] == 1
    assert diag["matched"] == 0


def test_absent_lifecycle_columns_block(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid1", pinned=False, archived=False, profile="default", messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
    conn.execute("INSERT INTO sessions (id, source) VALUES ('sid1','webui')")
    conn.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["blocked"]["ambiguous"] == 1
    assert diag["matched"] == 0


def test_canonical_root_tip_identity(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    root_id = "root11111111"
    tip_id = "tip22222222"
    _write_sidecar(session_dir, root_id, pinned=False, archived=False, parent_session_id=None, messages=[{"role": "user", "content": "hi"}])
    _write_sidecar(session_dir, tip_id, pinned=False, archived=False, parent_session_id=root_id, messages=[{"role": "user", "content": "hi2"}])

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, message_count INTEGER)")
    conn.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason, message_count) VALUES (?,?,?,?,?,?,?,?,?)", (root_id, "webui", 1, 0, 1000.0, 1000.5, None, "compression", 1))
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason, message_count) VALUES (?,?,?,?,?,?,?,?,?)", (tip_id, "webui", 1, 0, 1001.0, None, root_id, None, 1))
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", (root_id, "user", "hi", 1000.0))
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", (tip_id, "user", "hi2", 1001.0))
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["total_lineages"] == 1
    assert diag["pinned_mismatch"]["json_false_core_true"] == 1


def test_exact_identity_not_title(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid1", title="Same Title", pinned=True, archived=False, messages=[{"role": "user", "content": "hi"}])
    _write_sidecar(session_dir, "sid2", title="Same Title", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid1", "pinned": 0, "archived": 0}, {"id": "sid2", "pinned": 0, "archived": 0}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["pinned_mismatch"]["json_true_core_false"] == 1
    assert diag["matched"] == 1


def test_sidecar_only_empty_vs_messageful(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "empty1", pinned=False, archived=False, messages=[])
    _write_sidecar(session_dir, "msgful1", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER)")
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["sidecar_only"]["empty"] == 1
    assert diag["sidecar_only"]["messageful"] == 1


def test_core_only_and_blocked_active(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "active1", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}], active_stream_id="stream123", pending_user_message="hi")
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "active1", "pinned": 1, "archived": 0}, {"id": "coreOnly1", "pinned": 0, "archived": 0}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["blocked"]["active"] == 1
    assert diag["core_only"] == 1


def test_aggregate_output_free_of_titles_and_ids(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sidSecret123", title="Secret Prompt Title", pinned=True, archived=False, messages=[{"role": "user", "content": "super secret transcript"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sidSecret123", "pinned": 0, "archived": 0}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    dumped = json.dumps(diag)
    assert "Secret Prompt Title" not in dumped
    assert "super secret transcript" not in dumped
    assert "sidSecret123" not in dumped


def test_pure_truth_table():
    from api.session_metadata_sync import provisional_truth

    assert provisional_truth(False, False, False, False) == {"archived": False, "pinned": False}
    assert provisional_truth(False, True, False, False) == {"archived": True, "pinned": False}
    assert provisional_truth(False, False, False, True) == {"archived": True, "pinned": False}
    assert provisional_truth(True, False, False, False) == {"archived": False, "pinned": True}
    assert provisional_truth(False, False, True, False) == {"archived": False, "pinned": True}
    assert provisional_truth(True, False, True, True) == {"archived": True, "pinned": False}
    assert provisional_truth(True, False, True, False) == {"archived": False, "pinned": True}
    assert provisional_truth(True, False, None, None) == {"archived": False, "pinned": True}
    assert provisional_truth(False, False, None, True) == {"archived": True, "pinned": False}
    assert provisional_truth(True, False, False, True) == {"archived": True, "pinned": False}
    assert provisional_truth(False, True, True, False) == {"archived": True, "pinned": False}


def test_no_apply_flags_in_audit_help():
    result = subprocess.run([sys.executable, "scripts/audit_session_metadata_sync.py", "--help"], capture_output=True, text=True, cwd=str(Path(__file__).parents[1]))
    assert result.returncode == 0
    help_text = result.stdout + result.stderr
    assert "--apply" not in help_text
    assert "--yes" not in help_text
    assert "aggregate" in help_text.lower()


def test_blocked_covers_unreadable_ambiguous(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid1", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "nope.db"
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["blocked"]["unreadable"] >= 1
    db_path2 = tmp_path / "bad.db"
    conn = sqlite3.connect(str(db_path2))
    conn.execute("CREATE TABLE something_else (id TEXT)")
    conn.commit()
    conn.close()
    diag2 = compute_aggregate_diagnostics(session_dir, db_path2, profile="default")
    assert diag2["matched"] == 0
    assert diag2["blocked"]["unreadable"] >= 1


def test_no_event_or_cache_invalidation_on_shadow(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid1", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid1", "pinned": 0, "archived": 0}])
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    called = {"clear": False}
    monkeypatch.setattr(models, "clear_cli_sessions_cache", lambda: called.__setitem__("clear", True), raising=False)
    monkeypatch.setattr(config, "cfg", {"experimental": {"unified_session_db": True, "unified_session_metadata_mode": "shadow"}})
    before = db_path.stat().st_mtime_ns
    models.all_sessions()
    assert called["clear"] is False
    assert db_path.stat().st_mtime_ns == before
    monkeypatch.setattr(config, "cfg", {"experimental": {"unified_session_db": True, "unified_session_metadata_mode": "shadow"}})
    models.all_sessions()
    assert db_path.stat().st_mtime_ns == before


def test_shadow_comparator_is_exercised_and_pure(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics, shadow_compare

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid1", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid1", "pinned": 0, "archived": 0}])
    before_db_mtime = db_path.stat().st_mtime_ns
    before_db_size = db_path.stat().st_size
    before_sidecar = (session_dir / "sid1.json").read_text(encoding="utf-8")
    before_sidecar_mtime = (session_dir / "sid1.json").stat().st_mtime_ns
    idx = session_dir / "_index.json"
    idx.write_text(json.dumps([{"session_id": "sid1", "pinned": False, "archived": False, "message_count": 1}]), encoding="utf-8")
    before_idx_bytes = idx.read_bytes()
    before_idx_mtime = idx.stat().st_mtime_ns
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    via_shadow = shadow_compare(session_dir, db_path, profile="default")
    assert diag == via_shadow
    assert diag["matched"] == 1
    assert (session_dir / "sid1.json").read_text(encoding="utf-8") == before_sidecar
    assert (session_dir / "sid1.json").stat().st_mtime_ns == before_sidecar_mtime
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
    assert (session_dir / "sid1.json").read_text(encoding="utf-8") == before_sidecar
    assert db_path.stat().st_mtime_ns == before_db_mtime


def test_absent_vs_explicit_false_tri_state(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    p1 = session_dir / "sid_absent.json"
    p1.write_text(json.dumps({"session_id": "sid_absent", "profile": "default", "messages": [{"role": "user", "content": "hi"}], "created_at": 1000.0, "updated_at": 1000.0}), encoding="utf-8")
    _write_sidecar(session_dir, "sid_false", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid_absent", "pinned": 0, "archived": 0}, {"id": "sid_false", "pinned": 0, "archived": 0}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["blocked"]["ambiguous"] == 1
    assert diag["matched"] == 1


def test_cli_rejects_blank_and_whitespace_paths_no_cwd_fallback(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid1", pinned=False, archived=False, profile="default", messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid1", "pinned": 0, "archived": 0}])
    repo_root = Path(__file__).parents[1]
    script_path = str(repo_root / "scripts" / "audit_session_metadata_sync.py")
    for blank in ["", "   ", "\t", "\n"]:
        isolated = tmp_path / f"cwd_{blank.encode('unicode_escape').decode()}"
        isolated.mkdir(exist_ok=True)
        r = subprocess.run([sys.executable, script_path, "--session-dir", blank, "--state-db", str(db_path), "--profile", "default"], capture_output=True, text=True, cwd=str(isolated))
        assert r.returncode == 2, f"blank session-dir {blank!r} should parser-fail with code 2, got {r.returncode}"
        assert r.stdout == "", f"blank session-dir {blank!r} must have empty stdout"
        assert "non-empty" in (r.stderr + r.stdout).lower() or "whitespace" in (r.stderr + r.stdout).lower()
        r2 = subprocess.run([sys.executable, script_path, "--session-dir", str(session_dir), "--state-db", blank, "--profile", "default"], capture_output=True, text=True, cwd=str(isolated))
        assert r2.returncode == 2, f"blank state-db {blank!r} should parser-fail with code 2, got {r2.returncode}"
        assert r2.stdout == "", f"blank state-db {blank!r} must have empty stdout"
        assert "non-empty" in (r2.stderr + r2.stdout).lower() or "whitespace" in (r2.stderr + r2.stdout).lower()
        r3 = subprocess.run([sys.executable, script_path, "--session-dir", str(session_dir), "--state-db", str(db_path), "--profile", blank], capture_output=True, text=True, cwd=str(isolated))
        assert r3.returncode == 2, f"blank profile {blank!r} should parser-fail with code 2, got {r3.returncode}"
        assert r3.stdout == "", f"blank profile {blank!r} must have empty stdout"
        assert "non-empty" in (r3.stderr + r3.stdout).lower() or "whitespace" in (r3.stderr + r3.stdout).lower() or "profile" in (r3.stderr + r3.stdout).lower()
    isolated_good = tmp_path / "cwd_good"
    isolated_good.mkdir(exist_ok=True)
    good = subprocess.run([sys.executable, script_path, "--session-dir", str(session_dir), "--state-db", str(db_path), "--profile", "default", "--json"], capture_output=True, text=True, cwd=str(isolated_good))
    assert good.returncode == 0
    parsed = json.loads(good.stdout)
    assert parsed["profile"] == "default"
    assert parsed["matched"] == 1


def test_hostile_lifecycle_values_block_not_coerced(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics, read_core_lifecycle_batch, _tri_state_sidecar_flag

    assert _tri_state_sidecar_flag({"pinned": []}, "pinned") is None
    assert _tri_state_sidecar_flag({"archived": {}}, "archived") is None
    assert _tri_state_sidecar_flag({"pinned": "maybe"}, "pinned") is None
    assert _tri_state_sidecar_flag({"pinned": 2}, "pinned") is None
    assert _tri_state_sidecar_flag({"pinned": 2.5}, "pinned") is None
    assert _tri_state_sidecar_flag({"pinned": -1}, "pinned") is None
    assert _tri_state_sidecar_flag({"pinned": True}, "pinned") is True
    assert _tri_state_sidecar_flag({"pinned": False}, "pinned") is False
    assert _tri_state_sidecar_flag({"pinned": 1}, "pinned") is True
    assert _tri_state_sidecar_flag({"pinned": 0}, "pinned") is False
    assert _tri_state_sidecar_flag({"pinned": 1.0}, "pinned") is True
    assert _tri_state_sidecar_flag({"pinned": 0.0}, "pinned") is False
    assert _tri_state_sidecar_flag({"pinned": "true"}, "pinned") is None
    assert _tri_state_sidecar_flag({"pinned": "false"}, "pinned") is None
    assert _tri_state_sidecar_flag({"pinned": ""}, "pinned") is None
    assert _tri_state_sidecar_flag({"pinned": "   "}, "pinned") is None
    assert _tri_state_sidecar_flag({"pinned": "yes"}, "pinned") is None
    assert _tri_state_sidecar_flag({"pinned": "no"}, "pinned") is None
    assert _tri_state_sidecar_flag({"pinned": "on"}, "pinned") is None
    assert _tri_state_sidecar_flag({"pinned": "off"}, "pinned") is None
    assert _tri_state_sidecar_flag({"pinned": "1"}, "pinned") is None
    assert _tri_state_sidecar_flag({"pinned": "0"}, "pinned") is None

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived) VALUES ('sidHostile', 'webui', 2, 2)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived) VALUES ('sidNeg', 'webui', -1, 99)")
    conn.commit()
    conn.close()
    result = read_core_lifecycle_batch(db_path, {"sidHostile", "sidNeg"})
    assert result["sidHostile"]["pinned"] is None
    assert result["sidHostile"]["archived"] is None
    assert result["sidNeg"]["pinned"] is None
    assert result["sidNeg"]["archived"] is None

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "sidHostile.json").write_text(json.dumps({"session_id": "sidHostile", "profile": "default", "messages": [{"role": "user", "content": "hi"}], "pinned": [], "archived": {}, "created_at": 1000.0, "updated_at": 1000.0}), encoding="utf-8")
    (session_dir / "sidNeg.json").write_text(json.dumps({"session_id": "sidNeg", "profile": "default", "messages": [{"role": "user", "content": "hi"}], "pinned": 2, "archived": "maybe", "created_at": 1000.0, "updated_at": 1000.0}), encoding="utf-8")
    db2 = tmp_path / "state2.db"
    conn2 = sqlite3.connect(str(db2))
    conn2.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn2.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES ('sidHostile', 'webui', 0, 0, 1000.0)")
    conn2.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES ('sidNeg', 'webui', 1, 0, 1000.0)")
    conn2.commit()
    conn2.close()
    diag = compute_aggregate_diagnostics(session_dir, db2, profile="default")
    assert diag["blocked"]["ambiguous"] >= 2
    assert diag["matched"] == 0
    assert diag["pinned_mismatch"]["json_true_core_false"] == 0
    assert diag["pinned_mismatch"]["json_false_core_true"] == 0
    dumped = json.dumps(diag)
    assert "sidHostile" not in dumped
    assert "sidNeg" not in dumped


def test_fork_is_not_continuation_groups_separately(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    root = "root_fork_test"
    child = "child_fork_test"
    _write_sidecar(session_dir, root, pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    _write_sidecar(session_dir, child, pinned=False, archived=False, parent_session_id=root, messages=[{"role": "user", "content": "hi2"}])
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason, session_source) VALUES (?,?,?,?,?,?,?,?,?)", (root, "webui", 0, 0, 1000.0, 1000.5, None, "compression", None))
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason, session_source) VALUES (?,?,?,?,?,?,?,?,?)", (child, "webui", 0, 0, 1001.0, None, root, None, "fork"))
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", (root, "user", "hi", 1000.0))
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", (child, "user", "hi2", 1001.0))
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["total_lineages"] == 2


def test_started_at_not_used_as_end_boundary(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    root = "root_boundary"
    child = "child_boundary"
    _write_sidecar(session_dir, root, pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    _write_sidecar(session_dir, child, pinned=False, archived=False, parent_session_id=root, messages=[{"role": "user", "content": "hi2"}])
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason, session_source) VALUES (?,?,?,?,?,?,?,?,?)", (root, "webui", 0, 0, 1000.0, 2000.0, None, "compression", None))
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason, session_source) VALUES (?,?,?,?,?,?,?,?,?)", (child, "webui", 0, 0, 1500.0, None, root, None, None))
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", (root, "user", "hi", 1000.0))
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", (child, "user", "hi2", 1500.0))
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["total_lineages"] == 2


def test_string_and_blank_lifecycle_block_not_merge(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics, provisional_truth, _tri_state_sidecar_flag

    assert provisional_truth("true", False, False, False) == {"archived": False, "pinned": False}
    assert provisional_truth(2, False, False, False) == {"archived": False, "pinned": False}
    assert provisional_truth("", False, False, False) == {"archived": False, "pinned": False}
    assert provisional_truth("   ", False, False, False) == {"archived": False, "pinned": False}
    assert provisional_truth("yes", False, False, False) == {"archived": False, "pinned": False}
    assert _tri_state_sidecar_flag({"pinned": "true"}, "pinned") is None
    assert _tri_state_sidecar_flag({"pinned": ""}, "pinned") is None
    assert _tri_state_sidecar_flag({"pinned": "   "}, "pinned") is None

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "sidStrTrue.json").write_text(json.dumps({"session_id": "sidStrTrue", "profile": "default", "messages": [{"role": "user", "content": "hi"}], "pinned": "true", "archived": False, "created_at": 1000.0, "updated_at": 1000.0}), encoding="utf-8")
    (session_dir / "sidBlank.json").write_text(json.dumps({"session_id": "sidBlank", "profile": "default", "messages": [{"role": "user", "content": "hi"}], "pinned": "", "archived": "   ", "created_at": 1000.0, "updated_at": 1000.0}), encoding="utf-8")
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES ('sidStrTrue', 'webui', 0, 0, 1000.0)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES ('sidBlank', 'webui', 0, 0, 1001.0)")
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["blocked"]["ambiguous"] >= 2
    assert diag["matched"] == 0
    assert diag["pinned_mismatch"]["json_true_core_false"] == 0
    dumped = json.dumps(diag)
    assert "sidStrTrue" not in dumped
    assert "sidBlank" not in dumped


def test_malformed_same_id_sidecar_blocks_not_core_only(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "same-id.json").write_text("{ not json", encoding="utf-8")
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "same-id", "pinned": 0, "archived": 0, "source": "webui"}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["core_only"] == 0
    assert diag["matched"] == 0
    assert diag["blocked"]["ambiguous"] >= 1
    dumped = json.dumps(diag)
    assert "same-id" not in dumped
    # Non-dict JSON payload with same id also blocks, not core_only
    (session_dir / "same-id.json").write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    diag2 = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag2["core_only"] == 0
    assert diag2["blocked"]["ambiguous"] >= 1
    assert "same-id" not in json.dumps(diag2)


def test_deterministic_lineage_representatives_across_hash_seeds(tmp_path):
    """Equal started_at must not produce hash-random aggregate buckets."""
    import os
    import textwrap

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    root = "root_det_seed"
    child = "child_det_seed"
    _write_sidecar(session_dir, root, pinned=False, archived=False, parent_session_id=None, messages=[{"role": "user", "content": "hi"}], started_at=1000.0, created_at=1000.0)
    _write_sidecar(session_dir, child, pinned=True, archived=False, parent_session_id=root, messages=[{"role": "user", "content": "hi2"}], started_at=1000.0, created_at=1000.0)
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", (root, "webui", 1, 0, 1000.0, 1000.0, None, "compression"))
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", (child, "webui", 0, 0, 1000.0, None, root, None))
    conn.commit()
    conn.close()
    runner = tmp_path / "runner.py"
    runner.write_text(textwrap.dedent("""\
        import json, pathlib, os, sys
        sys.path.insert(0, os.environ.get("HERMES_TEST_REPO_ROOT", "."))
        from api.session_metadata_sync import compute_aggregate_diagnostics
        sd = pathlib.Path(os.environ["HERMES_TEST_SESSION_DIR"])
        db = pathlib.Path(os.environ["HERMES_TEST_STATE_DB"])
        d = compute_aggregate_diagnostics(sd, db, profile="default")
        print(json.dumps(d, sort_keys=True))
        """), encoding="utf-8")
    outputs = []
    base_env = dict(os.environ)
    base_env["HERMES_TEST_SESSION_DIR"] = str(session_dir)
    base_env["HERMES_TEST_STATE_DB"] = str(db_path)
    base_env["HERMES_TEST_REPO_ROOT"] = str(Path(__file__).parents[1])
    for seed in ("1", "2"):
        env = dict(base_env)
        env["PYTHONHASHSEED"] = seed
        result = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True, cwd=str(Path(__file__).parents[1]), env=env)
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout.strip())
    assert outputs[0] == outputs[1]
    diag = json.loads(outputs[0])
    dumped = outputs[0]
    assert "root_det_seed" not in dumped
    assert "child_det_seed" not in dumped
    assert diag["total_lineages"] == 1
    assert diag["pinned_mismatch"]["json_true_core_false"] == 1


# ---- Required acceptance coverage: defects 1-6 ----

def test_malformed_core_ids_block_aggregate_only(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    # Use no-affinity id column so numeric/BLOB types are preserved (TEXT affinity would coerce 42 to '42')
    conn.execute("CREATE TABLE sessions (id PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    # invalid ids
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES (NULL, 'webui', 0, 0, 1000.0)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES (42, 'webui', 0, 0, 1001.0)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES (?, 'webui', 0, 0, 1002.0)", (sqlite3.Binary(b"blobid"),))
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES ('', 'webui', 0, 0, 1003.0)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES ('   ', 'webui', 0, 0, 1004.0)")
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["core_only"] == 0
    assert diag["matched"] == 0
    assert diag["total_lineages"] == 0
    assert diag["total_core_lineages"] == 0
    assert diag["blocked"]["ambiguous"] >= 5
    dumped = json.dumps(diag)
    assert "42" not in dumped
    assert "blobid" not in dumped
    # positive control: valid string trusted core-only
    session_dir2 = tmp_path / "sessions2"
    session_dir2.mkdir()
    db2 = tmp_path / "state2.db"
    conn2 = sqlite3.connect(str(db2))
    conn2.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL)")
    conn2.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES ('valid-core-1', 'webui', 0, 0, 1000.0)")
    conn2.commit()
    conn2.close()
    diag2 = compute_aggregate_diagnostics(session_dir2, db2, profile="default")
    assert diag2["core_only"] == 1
    assert diag2["total_lineages"] == 1


def test_mixed_provenance_canonical_lineage_blocks(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    root = "root_mixed_prov"
    child = "child_mixed_prov"
    _write_sidecar(session_dir, root, pinned=False, archived=False, parent_session_id=None, messages=[{"role": "user", "content": "hi"}])
    _write_sidecar(session_dir, child, pinned=False, archived=False, parent_session_id=root, messages=[{"role": "user", "content": "hi2"}])
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    # parent trusted (source webui), child untrusted via NULL source + cron session_source but still continuation (source mismatch skipped when child source empty)
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason, session_source) VALUES (?,?,?,?,?,?,?,?,?)", (root, "webui", 0, 0, 1000.0, 1000.5, None, "compression", "webui"))
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason, session_source) VALUES (?,?,?,?,?,?,?,?,?)", (child, None, 0, 0, 1001.0, None, root, None, "cron"))
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["matched"] == 0
    assert diag["blocked"]["ambiguous"] >= 1
    dumped = json.dumps(diag)
    assert root not in dumped
    assert child not in dumped


def test_core_inventory_select_failure_is_unreadable(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid1", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid1", "pinned": 0, "archived": 0}])
    import api.session_metadata_sync as sms
    orig = sms.open_state_db_readonly
    call_n = {"c": 0}
    denial_seen = {"v": False}

    class _FakeCursor:
        def __init__(self, real_cur):
            self._real = real_cur

        def execute(self, sql, *a, **k):
            if "FROM sessions s" in sql and "s.id" in sql:
                denial_seen["v"] = True
                raise sqlite3.DatabaseError("authorizer denied")
            return self._real.execute(sql, *a, **k)

        def fetchall(self):
            return self._real.fetchall()

        def fetchone(self):
            return self._real.fetchone()

    class _FakeConn:
        def __init__(self, real_conn, deny: bool):
            self._real = real_conn
            self._deny = deny
            self.row_factory = real_conn.row_factory

        def cursor(self, *a, **k):
            real_cur = self._real.cursor(*a, **k)
            if self._deny:
                return _FakeCursor(real_cur)
            return real_cur

        def execute(self, sql, *a, **k):
            if self._deny and "FROM sessions s" in sql and "s.id" in sql:
                denial_seen["v"] = True
                raise sqlite3.DatabaseError("authorizer denied")
            return self._real.execute(sql, *a, **k)

        def close(self):
            return self._real.close()

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return self._real.__exit__(*a, **k) if hasattr(self._real, "__exit__") else False

    def _proxied_open(dbp):
        call_n["c"] += 1
        real = orig(dbp)
        deny = call_n["c"] == 1
        wrapped = _FakeConn(real, deny=deny)
        orig_close = real.close

        def _close_and_unwrap(*a, **k):
            try:
                return orig_close(*a, **k)
            finally:
                pass

        wrapped.close = _close_and_unwrap
        wrapped._real_close = orig_close
        wrapped._real_conn = real
        return wrapped

    import contextlib

    def _closing_proxy(conn):
        if isinstance(conn, _FakeConn):
            real = conn._real_conn
            cm = contextlib.closing(real)
            orig_exit = cm.__exit__

            class _CM:
                def __enter__(self):
                    return conn

                def __exit__(self, *a, **k):
                    try:
                        return orig_exit(*a, **k)
                    finally:
                        pass

            return _CM()
        return contextlib.closing(conn)

    import unittest.mock as mock

    with mock.patch.object(sms, "open_state_db_readonly", side_effect=_proxied_open):
        with mock.patch.object(sms, "closing", side_effect=_closing_proxy):
            diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert denial_seen["v"] is True
    assert diag["blocked"]["unreadable"] >= 1
    assert diag["matched"] == 0
    assert diag["core_only"] == 0
    assert diag["sidecar_only"]["empty"] == 0
    assert diag["sidecar_only"]["messageful"] == 0
    dumped = json.dumps(diag)
    assert "sid1" not in dumped


def test_malformed_blocked_parent_blocks_child_match(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    parent_id = "parent_malformed_blocked"
    child_id = "child_of_blocked_parent"
    # malformed parent (invalid json)
    (session_dir / f"{parent_id}.json").write_text("{ not json", encoding="utf-8")
    # valid child pointing to blocked parent
    _write_sidecar(session_dir, child_id, pinned=False, archived=False, parent_session_id=parent_id, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": child_id, "pinned": 0, "archived": 0}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["matched"] == 0
    assert diag["blocked"]["ambiguous"] >= 1
    # fork separation: unrelated valid fork must still be counted separately when not descendant
    session_dir2 = tmp_path / "sessions2"
    session_dir2.mkdir()
    _write_sidecar(session_dir2, "fork_root", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    _write_sidecar(session_dir2, "fork_child", pinned=False, archived=False, parent_session_id="fork_root", messages=[{"role": "user", "content": "hi2"}])
    db_fork = tmp_path / "state_fork.db"
    conn = sqlite3.connect(str(db_fork))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason, session_source) VALUES (?,?,?,?,?,?,?,?,?)", ("fork_root", "webui", 0, 0, 1000.0, 1000.5, None, "compression", None))
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason, session_source) VALUES (?,?,?,?,?,?,?,?,?)", ("fork_child", "webui", 0, 0, 1001.0, None, "fork_root", None, "fork"))
    conn.commit()
    conn.close()
    diag_fork = compute_aggregate_diagnostics(session_dir2, db_fork, profile="default")
    assert diag_fork["total_lineages"] == 2


def test_unknown_pending_blocks_known_controls(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics, _classify_pending

    assert _classify_pending({"has_pending_user_message": "maybe"}) is None
    assert _classify_pending({"has_pending_user_message": 2}) is None
    assert _classify_pending({"has_pending_user_message": []}) is None
    assert _classify_pending({"active_stream_id": 123}) is None
    assert _classify_pending({"pending_user_message": ["hi"]}) is None
    # known inactive
    assert _classify_pending({"has_pending_user_message": False}) is False
    assert _classify_pending({"has_pending_user_message": 0}) is False
    assert _classify_pending({"has_pending_user_message": "false"}) is False
    assert _classify_pending({}) is False
    # known active
    assert _classify_pending({"has_pending_user_message": True}) is True
    assert _classify_pending({"active_stream_id": "stream123"}) is True
    assert _classify_pending({"pending_user_message": "hi"}) is True

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "sid_unknown", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}], has_pending_user_message="maybe")
    _write_sidecar(session_dir, "sid_bad_type", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}], active_stream_id=123)
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "sid_unknown", "pinned": 0, "archived": 0}, {"id": "sid_bad_type", "pinned": 0, "archived": 0}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["blocked"]["ambiguous"] >= 2
    assert diag["blocked"]["active"] == 0
    assert diag["matched"] == 0
    dumped = json.dumps(diag)
    assert "sid_unknown" not in dumped
    assert "sid_bad_type" not in dumped
    # active control still blocks as active
    session_dir2 = tmp_path / "sessions2"
    session_dir2.mkdir()
    _write_sidecar(session_dir2, "sid_active", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}], has_pending_user_message=True)
    db2 = tmp_path / "state2.db"
    _make_state_db(db2, [{"id": "sid_active", "pinned": 0, "archived": 0}])
    diag2 = compute_aggregate_diagnostics(session_dir2, db2, profile="default")
    assert diag2["blocked"]["active"] == 1
    # inactive control allows match
    session_dir3 = tmp_path / "sessions3"
    session_dir3.mkdir()
    _write_sidecar(session_dir3, "sid_inactive", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}], has_pending_user_message=False)
    db3 = tmp_path / "state3.db"
    _make_state_db(db3, [{"id": "sid_inactive", "pinned": 0, "archived": 0}])
    diag3 = compute_aggregate_diagnostics(session_dir3, db3, profile="default")
    assert diag3["matched"] == 1


def test_missing_payload_session_id_blocks(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    # missing payload session_id
    (session_dir / "fileOnly.json").write_text(json.dumps({"profile": "default", "messages": [{"role": "user", "content": "hi"}], "pinned": False, "archived": False, "created_at": 1000.0}), encoding="utf-8")
    # blank payload
    (session_dir / "blankPayload.json").write_text(json.dumps({"session_id": "   ", "profile": "default", "messages": [{"role": "user", "content": "hi"}], "pinned": False, "archived": False}), encoding="utf-8")
    # non-string payload
    (session_dir / "nonString.json").write_text(json.dumps({"session_id": 12345, "profile": "default", "messages": [{"role": "user", "content": "hi"}], "pinned": False, "archived": False}), encoding="utf-8")
    # mismatched payload (filename vs payload)
    (session_dir / "mismatch.json").write_text(json.dumps({"session_id": "other_id", "profile": "default", "messages": [{"role": "user", "content": "hi"}], "pinned": False, "archived": False}), encoding="utf-8")
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "fileOnly", "pinned": 0, "archived": 0}, {"id": "blankPayload", "pinned": 0, "archived": 0}, {"id": "mismatch", "pinned": 0, "archived": 0}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["matched"] == 0
    assert diag["blocked"]["ambiguous"] >= 3
    assert diag["core_only"] == 0
    assert diag["sidecar_only"]["empty"] == 0
    assert diag["sidecar_only"]["messageful"] == 0
    dumped = json.dumps(diag)
    assert "fileOnly" not in dumped.replace("pinned_mismatch", "").replace("archived_mismatch", "")
    assert "blankPayload" not in dumped
    assert "nonString" not in dumped
    assert "other_id" not in dumped
    assert "mismatch" not in dumped.replace("pinned_mismatch", "").replace("archived_mismatch", "")
    # valid payload+filename still works
    session_dir2 = tmp_path / "sessions2"
    session_dir2.mkdir()
    _write_sidecar(session_dir2, "validId", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db2 = tmp_path / "state2.db"
    _make_state_db(db2, [{"id": "validId", "pinned": 0, "archived": 0}])
    diag2 = compute_aggregate_diagnostics(session_dir2, db2, profile="default")
    assert diag2["matched"] == 1


def test_aggregate_only_no_leak(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sid = "leakTestId999"
    _write_sidecar(session_dir, sid, title="LeakTitleXYZ", pinned=True, archived=False, messages=[{"role": "user", "content": "leak transcript content"}], pending_user_message="should not leak")
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": sid, "pinned": 0, "archived": 0}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    dumped = json.dumps(diag)
    for needle in [sid, "LeakTitleXYZ", "leak transcript", "should not leak"]:
        assert needle not in dumped
    # also check error/path not leaked via unreadable path
    diag2 = compute_aggregate_diagnostics(session_dir, tmp_path / "nonexistent.db", profile="default")
    dumped2 = json.dumps(diag2)
    assert "nonexistent" not in dumped2
    assert sid not in dumped2


def test_blocked_parent_core_only_reference_blocks_child(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    parent_id = "blocked-parent"
    child_id = "child"
    (session_dir / f"{parent_id}.json").write_text("{ not json", encoding="utf-8")
    _write_sidecar(session_dir, child_id, pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    for rid, parent in [(parent_id, None), (child_id, parent_id)]:
        conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, parent_session_id) VALUES (?,?,?,?,?,?)", (rid, "webui", 0, 0, 1000.0, parent))
        conn.execute("INSERT INTO messages VALUES (?,?,?,?)", (rid, "user", "hi", 1000.0))
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["matched"] == 0
    assert diag["blocked"]["ambiguous"] >= 1
    assert diag["sidecar_only"]["messageful"] == 0
    assert diag["core_only"] == 0
    dumped = json.dumps(diag)
    assert parent_id not in dumped
    assert child_id not in dumped


def test_malformed_sidecar_parent_blocks_candidate(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics, _classify_parent_ref

    assert _classify_parent_ref(None) == ("absent", None)
    assert _classify_parent_ref("  ") == ("malformed", None)
    assert _classify_parent_ref("") == ("malformed", None)
    assert _classify_parent_ref([]) == ("malformed", None)
    assert _classify_parent_ref({}) == ("malformed", None)
    assert _classify_parent_ref(123) == ("malformed", None)
    assert _classify_parent_ref("valid-parent") == ("valid", "valid-parent")

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    for sid, parent in [("sid_ws", "   "), ("sid_list", ["x"]), ("sid_dict", {"a": 1})]:
        _write_sidecar(session_dir, sid, pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}], parent_session_id=parent)
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL)")
    for sid in ["sid_ws", "sid_list", "sid_dict"]:
        conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES (?,?,?,?,?)", (sid, "webui", 0, 0, 1000.0))
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["matched"] == 0
    assert diag["blocked"]["ambiguous"] >= 3
    dumped = json.dumps(diag)
    for sid in ["sid_ws", "sid_list", "sid_dict"]:
        assert sid not in dumped


def test_malformed_core_parent_blob_and_numeric_blocks(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "child_blob", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    _write_sidecar(session_dir, "child_num", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    # No-affinity parent column preserves BLOB/numeric; TEXT affinity would coerce int to text
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, parent_session_id, end_reason TEXT, session_source TEXT)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, parent_session_id) VALUES ('child_blob','webui',0,0,1000.0,?)", (sqlite3.Binary(b"blob-parent"),))
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, parent_session_id) VALUES ('child_num','webui',0,0,1001.0,42)")
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["matched"] == 0
    assert diag["blocked"]["ambiguous"] >= 2
    dumped = json.dumps(diag)
    assert "child_blob" not in dumped
    assert "child_num" not in dumped


def test_core_parent_authority_sidecar_cannot_overwrite_none(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics, _rows_for_canonical_continuation

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    root = "root_core_auth"
    child = "child_core_auth"
    _write_sidecar(session_dir, root, pinned=False, archived=False, parent_session_id=None, messages=[{"role": "user", "content": "hi"}])
    _write_sidecar(session_dir, child, pinned=False, archived=False, parent_session_id=root, messages=[{"role": "user", "content": "hi2"}])
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", (root, "webui", 0, 0, 1000.0, 1000.5, None, "compression"))
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", (child, "webui", 0, 0, 1001.0, None, None, None))
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["total_lineages"] == 2
    assert diag["total_core_lineages"] == 2
    dumped = json.dumps(diag)
    assert root not in dumped
    assert child not in dumped
    from api.session_metadata_sync import _inventory_sidecars, _inventory_core_all

    sidecars, _, _, _, _ = _inventory_sidecars(session_dir, "default")
    core_all, _, _, _ = _inventory_core_all(db_path)
    rows = _rows_for_canonical_continuation(sidecars, {k: v for k, v in core_all.items() if v.get("trusted")})
    assert rows[child]["parent_session_id"] is None


def test_exact_identity_padded_values_do_not_alias(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics, _classify_parent_ref, read_core_lifecycle_batch

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES ('real-id','webui',0,0,1000.0)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES (' padded','webui',0,0,1001.0)")
    conn.commit()
    conn.close()
    payload_padded = {"session_id": " padded", "title": "Test", "workspace": str(session_dir.parent), "model": "test-model", "created_at": 1000.0, "updated_at": 1000.0, "pinned": False, "archived": False, "profile": "default", "messages": [{"role": "user", "content": "hi"}], "tool_calls": [], "message_count": 1}
    (session_dir / " padded.json").write_text(json.dumps(payload_padded), encoding="utf-8")
    _write_sidecar(session_dir, "real-id", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["matched"] == 2
    assert diag["total_lineages"] == 2
    dumped = json.dumps(diag)
    assert "real-id" not in dumped
    assert " padded" not in dumped
    session_dir2 = tmp_path / "sessions2"
    session_dir2.mkdir()
    _write_sidecar(session_dir2, "sid1", pinned=False, archived=False, profile="default", messages=[{"role": "user", "content": "hi"}])
    (session_dir2 / "sid1.json").write_text(json.dumps({"session_id": "sid1", "profile": " default", "messages": [{"role": "user", "content": "hi"}], "pinned": False, "archived": False, "created_at": 1000.0, "updated_at": 1000.0}), encoding="utf-8")
    db2 = tmp_path / "state2.db"
    conn2 = sqlite3.connect(str(db2))
    conn2.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER)")
    conn2.execute("INSERT INTO sessions (id, source, pinned, archived) VALUES ('sid1','webui',0,0)")
    conn2.commit()
    conn2.close()
    diag2 = compute_aggregate_diagnostics(session_dir2, db2, profile="default")
    assert diag2["matched"] == 0
    assert diag2["blocked"]["ambiguous"] >= 1
    assert "sid1" not in json.dumps(diag2)
    assert _classify_parent_ref(" root") == ("valid", " root")
    assert _classify_parent_ref("root") == ("valid", "root")
    assert _classify_parent_ref(" root")[1] != _classify_parent_ref("root")[1]
    db3 = tmp_path / "state3.db"
    conn3 = sqlite3.connect(str(db3))
    conn3.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER)")
    conn3.execute("INSERT INTO sessions (id, source, pinned, archived) VALUES ('42','webui',0,0)")
    conn3.execute("INSERT INTO sessions (id, source, pinned, archived) VALUES (' padded2','webui',0,0)")
    conn3.commit()
    conn3.close()
    assert read_core_lifecycle_batch(db3, {42}) == {}
    assert read_core_lifecycle_batch(db3, {"42"})["42"]["exists"] is True
    assert read_core_lifecycle_batch(db3, {" padded2"})[" padded2"]["exists"] is True
    assert read_core_lifecycle_batch(db3, {"padded2"})["padded2"]["exists"] is False
    assert read_core_lifecycle_batch(db3, {"   "}) == {}


def test_exact_identity_filename_payload_core_alias_blocks(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    payload = {
        "session_id": "file-id",
        "title": "Test",
        "workspace": str(session_dir.parent),
        "model": "test-model",
        "created_at": 1000.0,
        "updated_at": 1000.0,
        "pinned": False,
        "archived": False,
        "profile": "default",
        "messages": [{"role": "user", "content": "hi"}],
        "tool_calls": [],
        "message_count": 1,
    }
    (session_dir / " file-id.json").write_text(json.dumps(payload), encoding="utf-8")
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "file-id", "pinned": 0, "archived": 0}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["matched"] == 0
    assert diag["core_only"] == 0
    assert diag["blocked"]["ambiguous"] >= 1
    dumped = json.dumps(diag)
    assert "file-id" not in dumped
    assert " file-id" not in dumped


def test_exact_identity_core_id_alias_separate(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "core-id", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES (' core-id','webui',0,0,1000.0)")
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["matched"] == 0
    assert diag["sidecar_only"]["messageful"] == 1
    assert diag["core_only"] == 1
    assert diag["sidecar_only"]["empty"] == 0
    assert diag["blocked"]["ambiguous"] == 0
    dumped = json.dumps(diag)
    assert "core-id" not in dumped
    assert " core-id" not in dumped


def test_exact_identity_parent_alias_canonical_groups_separate(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    root_id = "root"
    child_id = "child_parent_alias"
    _write_sidecar(session_dir, root_id, pinned=False, archived=False, parent_session_id=None, messages=[{"role": "user", "content": "hi"}])
    _write_sidecar(session_dir, child_id, pinned=False, archived=False, parent_session_id=" root", messages=[{"role": "user", "content": "hi2"}])
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", (root_id, "webui", 0, 0, 1000.0, 1000.5, None, "compression"))
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", (child_id, "webui", 0, 0, 1001.0, None, " root", None))
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", (root_id, "user", "hi", 1000.0))
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", (child_id, "user", "hi2", 1001.0))
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["total_lineages"] == 2
    assert diag["total_core_lineages"] == 2
    assert diag["total_sidecar_lineages"] == 2
    dumped = json.dumps(diag)
    assert root_id not in dumped
    assert child_id not in dumped
    assert " root" not in dumped


def test_valid_roots_and_canonical_continuation_retained(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "root_valid", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    _write_sidecar(session_dir, "child_valid", pinned=False, archived=False, parent_session_id="root_valid", messages=[{"role": "user", "content": "hi2"}])
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", ("root_valid", "webui", 0, 0, 1000.0, 1000.5, None, "compression"))
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, parent_session_id) VALUES (?,?,?,?,?,?)", ("child_valid", "webui", 0, 0, 1001.0, "root_valid"))
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["total_lineages"] == 1
    assert diag["matched"] == 1
    # valid root without parent (absent parent) also valid
    session_dir2 = tmp_path / "sessions2"
    session_dir2.mkdir()
    _write_sidecar(session_dir2, "solo_root", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db2 = tmp_path / "state2.db"
    _make_state_db(db2, [{"id": "solo_root", "pinned": 0, "archived": 0}])
    diag2 = compute_aggregate_diagnostics(session_dir2, db2, profile="default")
    assert diag2["matched"] == 1
    # fork remains separate lineage
    session_dir3 = tmp_path / "sessions3"
    session_dir3.mkdir()
    _write_sidecar(session_dir3, "fork_root", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    _write_sidecar(session_dir3, "fork_child", pinned=False, archived=False, parent_session_id="fork_root", messages=[{"role": "user", "content": "hi2"}])
    db3 = tmp_path / "state3.db"
    conn3 = sqlite3.connect(str(db3))
    conn3.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn3.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", ("fork_root", "webui", 0, 0, 1000.0, 1000.5, None, "compression", None))
    conn3.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", ("fork_child", "webui", 0, 0, 1001.0, None, "fork_root", None, "fork"))
    conn3.commit()
    conn3.close()
    diag3 = compute_aggregate_diagnostics(session_dir3, db3, profile="default")
    assert diag3["total_lineages"] == 2


def test_missing_session_dir_is_unreadable_block(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    missing_dir = tmp_path / "no_such_sessions_xyz"
    assert not missing_dir.exists()
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "trustedCore1", "pinned": 0, "archived": 0, "source": "webui"}])
    diag = compute_aggregate_diagnostics(missing_dir, db_path, profile="default")
    assert diag["blocked"]["unreadable"] >= 1
    assert diag["matched"] == 0
    assert diag["core_only"] == 0
    assert diag["sidecar_only"]["empty"] == 0
    assert diag["sidecar_only"]["messageful"] == 0
    assert diag["total_lineages"] == 0
    dumped = json.dumps(diag)
    assert "trustedCore1" not in dumped
    assert "no_such_sessions_xyz" not in dumped
    assert missing_dir.name not in dumped
    # existing empty session dir is not unreadable
    empty_dir = tmp_path / "empty_sessions"
    empty_dir.mkdir()
    diag2 = compute_aggregate_diagnostics(empty_dir, db_path, profile="default")
    assert diag2["blocked"]["unreadable"] == 0


def test_sidecar_read_oserror_is_unreadable(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sid = "sidOSErrorLeakTest999"
    _write_sidecar(session_dir, sid, pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": sid, "pinned": 0, "archived": 0}])
    target = session_dir / f"{sid}.json"
    orig_read_text = Path.read_text

    def _faulting_read(self, *args, **kwargs):
        if self == target:
            raise OSError("injected read failure SECRET_TOKEN_OSERROR_XYZ")
        return orig_read_text(self, *args, **kwargs)

    import unittest.mock as mock

    with mock.patch.object(Path, "read_text", autospec=True, side_effect=_faulting_read):
        diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["blocked"]["unreadable"] >= 1
    assert diag["matched"] == 0
    assert diag["core_only"] == 0
    assert diag["sidecar_only"]["empty"] == 0
    assert diag["sidecar_only"]["messageful"] == 0
    dumped = json.dumps(diag)
    assert sid not in dumped
    assert "SECRET_TOKEN_OSERROR_XYZ" not in dumped
    assert "injected" not in dumped
    assert target.name not in dumped
    # malformed JSON that was successfully read remains ambiguous (not unreadable)
    session_dir2 = tmp_path / "sessions2"
    session_dir2.mkdir()
    (session_dir2 / "bad.json").write_text("{ not json", encoding="utf-8")
    _write_sidecar(session_dir2, "good1", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db2 = tmp_path / "state2.db"
    _make_state_db(db2, [{"id": "good1", "pinned": 0, "archived": 0}])
    diag2 = compute_aggregate_diagnostics(session_dir2, db2, profile="default")
    assert diag2["matched"] == 1
    assert diag2["blocked"]["ambiguous"] >= 1
    assert diag2["blocked"]["unreadable"] == 0
    # existing empty session dir is not unreadable (positive control)
    empty_dir = tmp_path / "empty_sessions_positive"
    empty_dir.mkdir()
    diag3 = compute_aggregate_diagnostics(empty_dir, db_path, profile="default")
    assert diag3["blocked"]["unreadable"] == 0


def test_read_core_lifecycle_batch_select_denied_is_unreadable(tmp_path):
    from api.session_metadata_sync import read_core_lifecycle_batch
    import api.session_metadata_sync as sms

    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "goodPresent", "pinned": 1, "archived": 0}, {"id": "goodAbsent", "pinned": 0, "archived": 0}])
    conn_check = sqlite3.connect(str(db_path))
    conn_check.execute("INSERT OR REPLACE INTO sessions (id, source, pinned, archived, started_at) VALUES ('goodPresent','webui',1,0,1000.0)")
    conn_check.commit()
    conn_check.close()
    orig_open = sms.open_state_db_readonly
    denial_seen = {"v": False}

    def _proxied_open(dbp):
        real = orig_open(dbp)

        def _auth(action, a1, a2, dbname, trig):
            if action == sqlite3.SQLITE_READ and a1 == "sessions":
                denial_seen["v"] = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        try:
            real.set_authorizer(_auth)
        except Exception:
            pass
        return real

    import unittest.mock as mock

    with mock.patch.object(sms, "open_state_db_readonly", side_effect=_proxied_open):
        result = read_core_lifecycle_batch(db_path, {"goodPresent", "missingRow"})
    assert denial_seen["v"] is True
    assert result["goodPresent"]["unreadable"] is True
    assert result["missingRow"]["unreadable"] is True
    assert result["goodPresent"]["exists"] is False
    assert result["missingRow"]["exists"] is False
    # absent row without denial is not unreadable (positive control)
    result2 = read_core_lifecycle_batch(db_path, {"missingRow2"})
    assert result2["missingRow2"]["exists"] is False
    assert result2["missingRow2"]["unreadable"] is False
    assert result2["missingRow2"]["pinned"] is None
    # present row without denial retains values and not unreadable
    result3 = read_core_lifecycle_batch(db_path, {"goodPresent"})
    assert result3["goodPresent"]["exists"] is True
    assert result3["goodPresent"]["pinned"] is True
    assert result3["goodPresent"]["unreadable"] is False


def test_singleton_unknown_lifecycle_blocked_as_ambiguous(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    sid_mal_side = "sidSidecarMalformedABC123"
    hostile_mal = "maybeHostile123"
    sd1 = tmp_path / "sd_mal_side"
    sd1.mkdir()
    (sd1 / f"{sid_mal_side}.json").write_text(
        json.dumps(
            {
                "session_id": sid_mal_side,
                "profile": "default",
                "messages": [{"role": "user", "content": "hi"}],
                "pinned": hostile_mal,
                "archived": False,
                "created_at": 1000.0,
                "updated_at": 1000.0,
            }
        ),
        encoding="utf-8",
    )
    db1 = tmp_path / "db_mal_side.db"
    conn1 = sqlite3.connect(str(db1))
    conn1.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER)")
    conn1.commit()
    conn1.close()
    d1 = compute_aggregate_diagnostics(sd1, db1, profile="default")
    assert d1["blocked"]["ambiguous"] >= 1
    assert d1["matched"] == 0
    assert d1["core_only"] == 0
    assert d1["sidecar_only"]["empty"] == 0
    assert d1["sidecar_only"]["messageful"] == 0
    dumped1 = json.dumps(d1)
    assert sid_mal_side not in dumped1
    assert hostile_mal not in dumped1

    sid_inc_side = "sidSidecarIncompleteXYZ789"
    sd2 = tmp_path / "sd_inc_side"
    sd2.mkdir()
    (sd2 / f"{sid_inc_side}.json").write_text(
        json.dumps(
            {
                "session_id": sid_inc_side,
                "profile": "default",
                "messages": [{"role": "user", "content": "hi"}],
                "archived": False,
                "created_at": 1000.0,
                "updated_at": 1000.0,
            }
        ),
        encoding="utf-8",
    )
    db2 = tmp_path / "db_inc_side.db"
    conn2 = sqlite3.connect(str(db2))
    conn2.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER)")
    conn2.commit()
    conn2.close()
    d2 = compute_aggregate_diagnostics(sd2, db2, profile="default")
    assert d2["blocked"]["ambiguous"] >= 1
    assert d2["matched"] == 0
    assert d2["core_only"] == 0
    assert d2["sidecar_only"]["empty"] == 0
    assert d2["sidecar_only"]["messageful"] == 0
    dumped2 = json.dumps(d2)
    assert sid_inc_side not in dumped2

    sid_mal_core = "sidCoreMalformedDEF456"
    hostile_core_val = "coreHostileLifecycleTokenQRS456"
    sd3 = tmp_path / "sd_mal_core"
    sd3.mkdir()
    db3 = tmp_path / "db_mal_core.db"
    conn3 = sqlite3.connect(str(db3))
    conn3.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL)")
    conn3.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES (?,?,?,?,?)", (sid_mal_core, "webui", hostile_core_val, 0, 1000.0))
    conn3.commit()
    conn3.close()
    d3 = compute_aggregate_diagnostics(sd3, db3, profile="default")
    assert d3["blocked"]["ambiguous"] >= 1
    assert d3["matched"] == 0
    assert d3["core_only"] == 0
    assert d3["sidecar_only"]["empty"] == 0
    assert d3["sidecar_only"]["messageful"] == 0
    dumped3 = json.dumps(d3)
    assert sid_mal_core not in dumped3
    assert hostile_core_val not in dumped3

    sid_inc_core = "sidCoreIncompleteGHI789"
    sd4 = tmp_path / "sd_inc_core"
    sd4.mkdir()
    db4 = tmp_path / "db_inc_core.db"
    conn4 = sqlite3.connect(str(db4))
    conn4.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
    conn4.execute("INSERT INTO sessions (id, source) VALUES (?,?)", (sid_inc_core, "webui"))
    conn4.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    conn4.commit()
    conn4.close()
    d4 = compute_aggregate_diagnostics(sd4, db4, profile="default")
    assert d4["blocked"]["ambiguous"] >= 1
    assert d4["matched"] == 0
    assert d4["core_only"] == 0
    assert d4["sidecar_only"]["empty"] == 0
    assert d4["sidecar_only"]["messageful"] == 0
    dumped4 = json.dumps(d4)
    assert sid_inc_core not in dumped4

    sid_inc_core_null = "sidCoreNullJKL012"
    sd5 = tmp_path / "sd_inc_core_null"
    sd5.mkdir()
    db5 = tmp_path / "db_inc_core_null.db"
    conn5 = sqlite3.connect(str(db5))
    conn5.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL)")
    conn5.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES (?,?,?,?,?)", (sid_inc_core_null, "webui", None, None, 1000.0))
    conn5.commit()
    conn5.close()
    d5 = compute_aggregate_diagnostics(sd5, db5, profile="default")
    assert d5["blocked"]["ambiguous"] >= 1
    assert d5["matched"] == 0
    assert d5["core_only"] == 0
    assert d5["sidecar_only"]["empty"] == 0
    assert d5["sidecar_only"]["messageful"] == 0
    dumped5 = json.dumps(d5)
    assert sid_inc_core_null not in dumped5

    sd_ok = tmp_path / "sd_ok_valid"
    sd_ok.mkdir()
    _write_sidecar(sd_ok, "sidValidSidecarOnlyOK", pinned=False, archived=False, messages=[{"role": "user", "content": "hi"}])
    db_ok = tmp_path / "db_ok_valid.db"
    conn_ok = sqlite3.connect(str(db_ok))
    conn_ok.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER)")
    conn_ok.commit()
    conn_ok.close()
    dok = compute_aggregate_diagnostics(sd_ok, db_ok, profile="default")
    assert dok["sidecar_only"]["messageful"] == 1
    assert dok["blocked"]["ambiguous"] == 0

    sd_ok2 = tmp_path / "sd_ok_core"
    sd_ok2.mkdir()
    db_ok2 = tmp_path / "db_ok_core.db"
    conn_ok2 = sqlite3.connect(str(db_ok2))
    conn_ok2.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL)")
    conn_ok2.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES ('sidValidCoreOnlyOK','webui',0,0,1000.0)")
    conn_ok2.commit()
    conn_ok2.close()
    dok2 = compute_aggregate_diagnostics(sd_ok2, db_ok2, profile="default")
    assert dok2["core_only"] == 1
    assert dok2["blocked"]["ambiguous"] == 0


def test_audit_blocks_missing_ended_at_compression_continuation(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    db_path = tmp_path / "state.db"
    parent_id = "parentMissingEndedAtCompression999"
    child_id = "childMissingEndedAtCompression999"
    secret_parent = "SECRET_PARENT_TOKEN_XYZ_1"
    secret_child = "SECRET_CHILD_TOKEN_XYZ_1"
    _write_sidecar(session_dir, parent_id, pinned=False, archived=False, parent_session_id=None, messages=[{"role": "user", "content": "hi"}], started_at=1000.0, title=secret_parent)
    _write_sidecar(session_dir, child_id, pinned=False, archived=False, parent_session_id=parent_id, messages=[{"role": "user", "content": "hi2"}], started_at=1001.0, title=secret_child)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", (parent_id, "webui", 0, 0, 1000.0, None, None, "compression"))
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", (child_id, "webui", 0, 0, 1001.0, None, parent_id, None))
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["blocked"]["ambiguous"] >= 1
    assert diag["matched"] == 0
    assert diag["core_only"] == 0
    assert diag["sidecar_only"]["empty"] == 0
    assert diag["sidecar_only"]["messageful"] == 0
    assert diag["pinned_mismatch"]["json_true_core_false"] == 0
    assert diag["pinned_mismatch"]["json_false_core_true"] == 0
    assert diag["archived_mismatch"]["json_true_core_false"] == 0
    assert diag["archived_mismatch"]["json_false_core_true"] == 0
    dumped = json.dumps(diag)
    assert parent_id not in dumped
    assert child_id not in dumped
    assert secret_parent not in dumped
    assert secret_child not in dumped
    # cli_close variant with same missing ended_at semantics
    session_dir2 = tmp_path / "sessions2"
    session_dir2.mkdir()
    db2 = tmp_path / "state2.db"
    _write_sidecar(session_dir2, parent_id, pinned=False, archived=False, parent_session_id=None, messages=[{"role": "user", "content": "hi"}], started_at=1000.0)
    _write_sidecar(session_dir2, child_id, pinned=False, archived=False, parent_session_id=parent_id, messages=[{"role": "user", "content": "hi2"}], started_at=1001.0)
    conn2 = sqlite3.connect(str(db2))
    conn2.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn2.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", (parent_id, "webui", 0, 0, 1000.0, None, None, "cli_close"))
    conn2.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", (child_id, "webui", 0, 0, 1001.0, None, parent_id, None))
    conn2.commit()
    conn2.close()
    diag2 = compute_aggregate_diagnostics(session_dir2, db2, profile="default")
    assert diag2["blocked"]["ambiguous"] >= 1
    assert diag2["matched"] == 0
    assert parent_id not in json.dumps(diag2)
    # positive control: valid continuation with real ended_at must still collapse and match
    session_dir3 = tmp_path / "sessions3"
    session_dir3.mkdir()
    db3 = tmp_path / "state3.db"
    _write_sidecar(session_dir3, parent_id, pinned=False, archived=False, parent_session_id=None, messages=[{"role": "user", "content": "hi"}], started_at=1000.0)
    _write_sidecar(session_dir3, child_id, pinned=False, archived=False, parent_session_id=parent_id, messages=[{"role": "user", "content": "hi2"}], started_at=1001.0)
    conn3 = sqlite3.connect(str(db3))
    conn3.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn3.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", (parent_id, "webui", 0, 0, 1000.0, 1000.5, None, "compression"))
    conn3.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", (child_id, "webui", 0, 0, 1001.0, None, parent_id, None))
    conn3.commit()
    conn3.close()
    diag3 = compute_aggregate_diagnostics(session_dir3, db3, profile="default")
    assert diag3["matched"] == 1
    assert diag3["blocked"]["ambiguous"] == 0
    assert diag3["total_lineages"] == 1


def test_audit_blocks_continuation_cycle_deterministically(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    db_path = tmp_path / "state.db"
    id_a = "cycleA999"
    id_b = "cycleB999"
    secret_a = "SECRET_CYCLE_A_TOKEN_XYZ"
    secret_b = "SECRET_CYCLE_B_TOKEN_XYZ"
    _write_sidecar(session_dir, id_a, pinned=False, archived=False, parent_session_id=id_b, messages=[{"role": "user", "content": "hi"}], started_at=1001.0, title=secret_a)
    _write_sidecar(session_dir, id_b, pinned=False, archived=False, parent_session_id=id_a, messages=[{"role": "user", "content": "hi2"}], started_at=1001.0, title=secret_b)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL, ended_at REAL, parent_session_id TEXT, end_reason TEXT, session_source TEXT)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", (id_a, "webui", 0, 0, 1001.0, 1000.0, id_b, "compression"))
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", (id_b, "webui", 0, 0, 1001.0, 1000.0, id_a, "compression"))
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["blocked"]["ambiguous"] >= 1
    assert diag["matched"] == 0
    assert diag["core_only"] == 0
    assert diag["sidecar_only"]["empty"] == 0
    assert diag["sidecar_only"]["messageful"] == 0
    assert diag["pinned_mismatch"]["json_true_core_false"] == 0
    assert diag["archived_mismatch"]["json_true_core_false"] == 0
    assert diag["total_lineages"] == 1
    assert diag["total_sidecar_lineages"] == 1
    assert diag["total_core_lineages"] == 1
    assert diag["blocked"]["ambiguous"] == 1
    dumped = json.dumps(diag)
    assert id_a not in dumped
    assert id_b not in dumped
    assert secret_a not in dumped
    assert secret_b not in dumped
    diag2 = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag == diag2
    # descendant of cycle must also be blocked and not form clean lineage
    child_id = "cycleChild999"
    secret_child = "SECRET_CYCLE_CHILD_XYZ"
    _write_sidecar(session_dir, child_id, pinned=False, archived=False, parent_session_id=id_a, messages=[{"role": "user", "content": "hi3"}], started_at=1002.0, title=secret_child)
    conn2 = sqlite3.connect(str(db_path))
    conn2.execute("INSERT OR REPLACE INTO sessions (id, source, pinned, archived, started_at, ended_at, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?)", (child_id, "webui", 0, 0, 1002.0, None, id_a, None))
    conn2.commit()
    conn2.close()
    diag3 = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag3["blocked"]["ambiguous"] >= 1
    assert diag3["matched"] == 0
    assert diag3["core_only"] == 0
    assert diag3["sidecar_only"]["messageful"] == 0
    assert diag3["total_lineages"] == 1
    assert diag3["total_sidecar_lineages"] == 1
    assert diag3["total_core_lineages"] == 1
    assert child_id not in json.dumps(diag3)
    assert secret_child not in json.dumps(diag3)


def test_profile_mismatch_counted_once_via_blocked_lineage_exact(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "good1", pinned=False, archived=False, profile="default", messages=[{"role": "user", "content": "hi"}])
    _write_sidecar(session_dir, "other1", pinned=False, archived=False, profile="other", messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    _make_state_db(db_path, [{"id": "good1", "pinned": 0, "archived": 0}])
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["total_lineages"] == 2
    assert diag["matched"] == 1
    assert diag["blocked"]["ambiguous"] == 1
    assert diag["blocked"]["unreadable"] == 0
    dumped = json.dumps(diag)
    assert "other1" not in dumped
    assert "good1" not in dumped
    assert diag["blocked"]["ambiguous"] != 2


def test_invalid_core_id_no_lineage_not_double_counted_exact(tmp_path):
    from api.session_metadata_sync import compute_aggregate_diagnostics

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_sidecar(session_dir, "good1", pinned=False, archived=False, profile="default", messages=[{"role": "user", "content": "hi"}])
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES (42, 'webui', 0, 0, 1000.0)")
    conn.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES ('good1', 'webui', 0, 0, 1000.0)")
    conn.commit()
    conn.close()
    diag = compute_aggregate_diagnostics(session_dir, db_path, profile="default")
    assert diag["total_lineages"] == 1
    assert diag["matched"] == 1
    assert diag["blocked"]["ambiguous"] == 1
    assert diag["blocked"]["unreadable"] == 0
    assert diag["total_core_lineages"] == 1
    dumped = json.dumps(diag)
    assert "good1" not in dumped
    assert "42" not in dumped
    assert "exception" not in dumped.lower()
    assert "traceback" not in dumped.lower()
    session_dir2 = tmp_path / "sessions2"
    session_dir2.mkdir()
    _write_sidecar(session_dir2, "good1b", pinned=False, archived=False, profile="default", messages=[{"role": "user", "content": "hi"}])
    db2 = tmp_path / "state2.db"
    conn2 = sqlite3.connect(str(db2))
    conn2.execute("CREATE TABLE sessions (id PRIMARY KEY, source TEXT, pinned INTEGER, archived INTEGER, started_at REAL)")
    conn2.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES (?, 'webui', 0, 0, 1000.0)", (sqlite3.Binary(b"blobid"),))
    conn2.execute("INSERT INTO sessions (id, source, pinned, archived, started_at) VALUES ('good1b', 'webui', 0, 0, 1000.0)")
    conn2.commit()
    conn2.close()
    diag2 = compute_aggregate_diagnostics(session_dir2, db2, profile="default")
    assert diag2["total_lineages"] == 1
    assert diag2["blocked"]["ambiguous"] == 1
    assert "blobid" not in json.dumps(diag2)
    assert "good1b" not in json.dumps(diag2)
