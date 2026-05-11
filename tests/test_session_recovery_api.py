import json

from api.session_recovery import audit_session_recovery, repair_safe_session_recovery


def _write_session(session_dir, sid, messages=1):
    path = session_dir / f"{sid}.json"
    path.write_text(
        json.dumps({"id": sid, "session_id": sid, "title": sid, "messages": [{"role": "user", "content": str(i)} for i in range(messages)]}),
        encoding="utf-8",
    )
    return path


def test_repair_safe_session_recovery_restores_backup_and_rebuilds_index(tmp_path, monkeypatch):
    import api.models as _m

    sid = "abc123"
    live = _write_session(tmp_path, sid, messages=4)
    bak = tmp_path / f"{sid}.json.bak"
    bak.write_text(live.read_text(encoding="utf-8"), encoding="utf-8")
    live.unlink()
    index = tmp_path / "_index.json"
    index.write_text(json.dumps([]), encoding="utf-8")
    monkeypatch.setattr(_m, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(_m, "SESSION_INDEX_FILE", index)

    result = repair_safe_session_recovery(tmp_path)

    assert result["clean"] is True
    assert result["repaired"] == 1
    assert live.exists()
    assert audit_session_recovery(tmp_path)["status"] == "ok"
    idx = json.loads(index.read_text(encoding="utf-8"))
    assert [entry["session_id"] for entry in idx] == [sid]


def test_repair_safe_session_recovery_leaves_unsafe_orphan_for_manual_review(tmp_path):
    import sqlite3

    sid = "abc123"
    live = _write_session(tmp_path, sid, messages=1)
    bak = tmp_path / f"{sid}.json.bak"
    bak.write_text(live.read_text(encoding="utf-8"), encoding="utf-8")
    live.unlink()
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table sessions (id text primary key)")
        conn.execute("insert into sessions (id) values (?)", ("other",))

    result = repair_safe_session_recovery(tmp_path, state_db_path=db)

    assert result["clean"] is False
    assert result["repaired"] == 0
    assert not live.exists()
    assert result["after"]["status"] == "needs_manual_review"


def test_recovery_audit_routes_are_registered():
    from pathlib import Path

    src = Path("api/routes.py").read_text(encoding="utf-8")

    assert 'parsed.path == "/api/session/recovery/audit"' in src
    assert 'parsed.path == "/api/session/recovery/repair-safe"' in src
    assert "audit_session_recovery" in src
    assert "repair_safe_session_recovery" in src
