import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from api.session_recovery import audit_session_recovery

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_session(session_dir, sid, messages=1):
    path = session_dir / f"{sid}.json"
    path.write_text(
        json.dumps({"id": sid, "session_id": sid, "title": sid, "messages": [{"role": "user", "content": str(i)} for i in range(messages)]}),
        encoding="utf-8",
    )
    return path


def _state_db(session_dir, *session_ids):
    db = session_dir / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table sessions (id text primary key)")
        conn.executemany("insert into sessions (id) values (?)", [(sid,) for sid in session_ids])
    return db


def test_audit_reports_repairable_orphan_backup_when_state_db_has_session(tmp_path):
    sid = "abc123"
    live = _write_session(tmp_path, sid, messages=3)
    bak = tmp_path / f"{sid}.json.bak"
    bak.write_text(live.read_text(encoding="utf-8"), encoding="utf-8")
    live.unlink()
    db = _state_db(tmp_path, sid)

    report = audit_session_recovery(tmp_path, state_db_path=db)

    assert report["status"] == "warn"
    assert report["summary"]["repairable"] == 1
    assert report["items"] == [
        {
            "session_id": sid,
            "kind": "orphan_backup",
            "category": "repairable",
            "recommendation": "restore_from_bak",
            "live_messages": -1,
            "bak_messages": 3,
        }
    ]


def test_audit_marks_orphan_backup_without_state_row_unsafe(tmp_path):
    sid = "abc123"
    live = _write_session(tmp_path, sid, messages=2)
    bak = tmp_path / f"{sid}.json.bak"
    bak.write_text(live.read_text(encoding="utf-8"), encoding="utf-8")
    live.unlink()
    db = _state_db(tmp_path, "different")

    report = audit_session_recovery(tmp_path, state_db_path=db)

    assert report["status"] == "needs_manual_review"
    assert report["summary"]["unsafe_to_repair"] == 1
    assert report["items"][0]["kind"] == "orphan_backup_without_state_row"
    assert report["items"][0]["recommendation"] == "manual_review"


def test_audit_reports_index_drift(tmp_path):
    sid = "abc123"
    _write_session(tmp_path, sid, messages=1)
    (tmp_path / "_index.json").write_text(
        json.dumps([{"session_id": "missing", "message_count": 1}]),
        encoding="utf-8",
    )

    report = audit_session_recovery(tmp_path)
    kinds = {item["kind"] for item in report["items"]}

    assert "index_missing_file" in kinds
    assert "index_missing_entry" in kinds
    assert report["summary"]["repairable"] == 2


def test_session_recovery_module_audit_cli_outputs_json(tmp_path):
    sid = "abc123"
    _write_session(tmp_path, sid, messages=1)

    result = subprocess.run(
        [sys.executable, "-m", "api.session_recovery", "--audit", "--session-dir", str(tmp_path)],
        cwd=str(REPO_ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["summary"]["ok"] == 1
