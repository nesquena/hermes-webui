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
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress.jsonl"))
    _m.SESSIONS.clear()

    result = repair_safe_session_recovery(tmp_path, state_db_path=tmp_path / "state.db")

    assert result["clean"] is True, result
    assert result["ok"] is True
    assert result["repaired"] == 1
    assert result["prompt_preflight"] == {
        "available": True,
        "action": "session.recovery.repair_safe",
        "boundary": "recovery_action",
        "status": "required",
        "severity": "none",
        "categories": [],
        "checks": ["shared_confirmation_required", "prompt_injection_preflight_required"],
        "metadata_only": True,
        "raw_prompt_stored": False,
        "local_only": True,
    }
    policy = result["autonomy_policy"]
    assert policy["action"] == "session.recovery.repair_safe"
    assert policy["approval_required"] is True
    assert policy["approval_gates"] == ["destructive_external_action"]
    assert policy["prompt_preflight_status"] == "required"
    assert policy["model_route_hint"] == "hint:reasoning"
    assert policy["metadata_only"] is True
    progress = result["progress_event"]
    assert progress["event_type"] == "tool.completed"
    assert progress["family"] == "tool"
    assert progress["run_id"] == "session.recovery.repair_safe"
    assert progress["redaction_status"] == "metadata_only"
    assert live.exists()
    assert audit_session_recovery(tmp_path)["status"] == "ok"
    idx = json.loads(index.read_text(encoding="utf-8"))
    assert [entry["session_id"] for entry in idx] == [sid]


def test_repair_safe_session_recovery_records_failed_progress_when_manual_review_remains(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress.jsonl"))
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
    serialized_receipts = json.dumps(
        {
            "prompt_preflight": result["prompt_preflight"],
            "autonomy_policy": result["autonomy_policy"],
            "progress_event": result["progress_event"],
        },
        sort_keys=True,
    ).lower()

    assert result["clean"] is False
    assert result["progress_event"]["event_type"] == "tool.failed"
    assert result["progress_event"]["run_id"] == "session.recovery.repair_safe"
    assert result["autonomy_policy"]["approval_gates"] == ["destructive_external_action"]
    assert "secret" not in serialized_receipts
    assert "api_key" not in serialized_receipts
    assert "<script" not in serialized_receipts


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
    assert result["ok"] is False
    assert result["repaired"] == 0
    assert not live.exists()
    assert result["after"]["status"] == "needs_manual_review"


def test_repair_safe_route_uses_clean_flag_for_status_code():
    from pathlib import Path

    src = Path("api/routes.py").read_text(encoding="utf-8")

    assert 'status=200 if result.get("clean") else 409' in src


def test_recovery_audit_routes_are_registered():
    from pathlib import Path

    src = Path("api/routes.py").read_text(encoding="utf-8")

    assert 'parsed.path == "/api/session/recovery/audit"' in src
    assert 'parsed.path == "/api/session/recovery/repair-safe"' in src
    assert "audit_session_recovery" in src
    assert "repair_safe_session_recovery" in src
