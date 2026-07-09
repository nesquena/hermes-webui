import json

from api.session_recovery import audit_session_recovery, repair_safe_session_recovery


EXPECTED_MEMORY_ADVISORY = {
    "metadata_only": True,
    "advisory_context": True,
    "context_authority": "untrusted_advisory",
    "can_bypass_safety_gates": False,
    "required_gates": ["prompt_preflight", "approval", "sandbox_preview", "visual_qa", "rollback_recovery"],
}


def _write_session(session_dir, sid, messages=1):
    path = session_dir / f"{sid}.json"
    path.write_text(
        json.dumps({"id": sid, "session_id": sid, "title": sid, "messages": [{"role": "user", "content": str(i)} for i in range(messages)]}),
        encoding="utf-8",
    )
    return path


def test_audit_session_recovery_returns_metadata_only_progress_event_without_hostile_strings(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress.jsonl"))
    session_dir = tmp_path / "sessions_BEARER_TOKEN_SECRET_path"
    session_dir.mkdir()
    sid = "sid_API_AUTH_BEARER_TOKEN_SECRET_prompt_renderer_source"
    live = session_dir / f"{sid}.json"
    backup = session_dir / f"{sid}.json.bak"
    live.write_text(
        json.dumps(
            {
                "id": sid,
                "session_id": sid,
                "title": "raw prompt bearer token secret title",
                "messages": [
                    {"role": "user", "content": "RAW_PROMPT:<script>alert('token')</script> API_AUTH=secret"}
                ],
                "renderer": "unsafe-source-renderer",
                "source": "unsafe-source-body",
            }
        ),
        encoding="utf-8",
    )
    backup.write_text(
        json.dumps(
            {
                "id": sid,
                "session_id": sid,
                "title": "backup raw prompt bearer token secret title",
                "messages": [
                    {"role": "user", "content": "RAW_PROMPT backup bearer secret"},
                    {"role": "assistant", "content": "backup message with token"},
                ],
                "api_auth": "Bearer unsafe-secret-token",
                "backup_path": str(backup),
            }
        ),
        encoding="utf-8",
    )

    result = audit_session_recovery(session_dir)

    assert result["status"] == "warn"
    progress = result["progress_event"]
    assert progress["event_type"] == "tool.completed"
    assert progress["family"] == "tool"
    assert progress["run_id"] == "session.recovery.audit"
    assert progress["redaction_status"] == "metadata_only"
    assert result["progress_events"] == [progress]
    serialized_progress = json.dumps(
        {"progress_event": progress, "progress_events": result["progress_events"]},
        sort_keys=True,
    ).lower()
    for unsafe in (
        str(session_dir).lower(),
        str(live).lower(),
        str(backup).lower(),
        sid.lower(),
        "raw_prompt",
        "<script",
        "api_auth",
        "bearer",
        "unsafe-secret-token",
        "renderer",
        "unsafe-source",
        "secret_path",
    ):
        assert unsafe not in serialized_progress

    missing_result = audit_session_recovery(tmp_path / "missing_SECRET_session_dir")
    assert missing_result["status"] == "ok"
    assert missing_result["progress_event"]["run_id"] == "session.recovery.audit"
    assert "missing_secret_session_dir" not in json.dumps(missing_result["progress_event"], sort_keys=True).lower()


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
    assert result["progress_event"] == result["progress_events"][-1]
    assert [event["event_type"] for event in result["progress_events"]] == ["tool.started", "tool.completed"]
    for progress in result["progress_events"]:
        assert progress["family"] == "tool"
        assert progress["run_id"] == "session.recovery.repair_safe"
        assert progress["redaction_status"] == "metadata_only"
    progress = result["progress_event"]
    assert progress["event_type"] == "tool.completed"
    assert result["memory_advisory"] == EXPECTED_MEMORY_ADVISORY
    compaction = result["output_compaction"]
    assert compaction["tool"] == "capy-session-recovery"
    assert compaction["command"] == "session.recovery.repair_safe"
    assert compaction["exit_status"] == 0
    assert compaction["redaction_status"] == "none"
    assert "repair_status: clean" in compaction["text"]
    assert "repaired_sessions: 1" in compaction["text"]
    assert "approval_required: yes" in compaction["text"]
    assert "progress_status: tool.completed" in compaction["text"]
    assert "progress_event_types: tool.started, tool.completed" in compaction["text"]
    assert "advisory_context: true" in compaction["text"]
    assert "context_authority: untrusted_advisory" in compaction["text"]
    assert "can_bypass_safety_gates: false" in compaction["text"]
    assert "required_gates: prompt_preflight, approval, sandbox_preview, visual_qa, rollback_recovery" in compaction["text"]
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
            "progress_events": result["progress_events"],
            "memory_advisory": result["memory_advisory"],
            "output_compaction": result["output_compaction"],
        },
        sort_keys=True,
    ).lower()

    assert result["clean"] is False
    assert result["progress_event"] == result["progress_events"][-1]
    assert [event["event_type"] for event in result["progress_events"]] == ["tool.started", "tool.failed"]
    for progress in result["progress_events"]:
        assert progress["family"] == "tool"
        assert progress["run_id"] == "session.recovery.repair_safe"
        assert progress["redaction_status"] == "metadata_only"
    assert result["progress_event"]["event_type"] == "tool.failed"
    assert result["progress_event"]["run_id"] == "session.recovery.repair_safe"
    assert result["autonomy_policy"]["approval_gates"] == ["destructive_external_action"]
    assert result["memory_advisory"] == EXPECTED_MEMORY_ADVISORY
    compaction = result["output_compaction"]
    assert compaction["exit_status"] == 1
    assert "repair_status: manual_review_required" in compaction["text"]
    assert "unsafe_remaining: 1" in compaction["text"]
    assert "progress_status: tool.failed" in compaction["text"]
    assert "progress_event_types: tool.started, tool.failed" in compaction["text"]
    assert "advisory_context: true" in compaction["text"]
    assert "context_authority: untrusted_advisory" in compaction["text"]
    assert "can_bypass_safety_gates: false" in compaction["text"]
    assert "required_gates: prompt_preflight, approval, sandbox_preview, visual_qa, rollback_recovery" in compaction["text"]
    assert str(tmp_path).lower() not in serialized_receipts
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
