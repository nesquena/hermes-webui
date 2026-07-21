import json

from api.session_recovery import (
    audit_session_recovery,
    inspect_session_recovery_status,
    recover_incomplete_compression_transactions,
    recover_session,
    repair_safe_session_recovery,
)


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
    stale = _m.Session(
        session_id="stale_cached",
        title="stale",
        messages=[{"role": "user", "content": "from another test"}],
    )
    _m.SESSIONS[stale.session_id] = stale

    try:
        result = repair_safe_session_recovery(tmp_path)
    finally:
        _m.SESSIONS.pop(stale.session_id, None)

    assert result["clean"] is True
    assert result["ok"] is True
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


def test_foreign_backup_identity_is_rejected_before_recovery_replace(tmp_path):
    live = _write_session(tmp_path, "identity-a", messages=1)
    before = live.read_bytes()
    foreign = {
        "session_id": "identity-b",
        "messages": [{"role": "user", "content": str(i)} for i in range(3)],
    }
    live.with_suffix(".json.bak").write_text(json.dumps(foreign), encoding="utf-8")

    status = inspect_session_recovery_status(live)
    result = recover_session(live)

    assert status["bak_messages"] == -1
    assert status["recommend"] == "no_action"
    assert result["restored"] is False
    assert live.read_bytes() == before


def test_startup_finalizes_durable_published_compression_intent(tmp_path, monkeypatch):
    import api.models as models

    index = tmp_path / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index)
    session = models.Session(
        session_id="compression-recover-published",
        messages=[{"role": "user", "content": "durable"}],
    )
    session.save(skip_index=True)
    token = session._publication_generation.token
    intent_dir = tmp_path / "_compression_transactions"
    intent_dir.mkdir()
    intent = intent_dir / f"{session.session_id}.json"
    intent.write_text(
        json.dumps(
            {
                "version": 1,
                "old_session_id": "compression-recover-old",
                "new_session_id": session.session_id,
                "incarnation_token": token,
                "phase": "sidecar_published",
            }
        ),
        encoding="utf-8",
    )

    result = recover_incomplete_compression_transactions(tmp_path)

    assert result == {"finalized": 1, "rolled_back": 0, "residuals": []}
    assert not intent.exists()
    assert json.loads(index.read_text(encoding="utf-8"))[0]["session_id"] == session.session_id


def test_startup_rolls_back_prepared_compression_claim_without_sidecar(
    tmp_path, monkeypatch
):
    import api.models as models
    import api.session_media as session_media

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(tmp_path / "attachments"))
    sid = "compression-recover-prepared"
    token = "a" * 32
    models._persist_session_incarnation_claim(sid, token)
    intent_dir = tmp_path / "_compression_transactions"
    intent_dir.mkdir()
    intent = intent_dir / f"{sid}.json"
    intent.write_text(
        json.dumps(
            {
                "version": 1,
                "old_session_id": "compression-recover-old",
                "new_session_id": sid,
                "incarnation_token": token,
                "phase": "prepared",
            }
        ),
        encoding="utf-8",
    )

    result = recover_incomplete_compression_transactions(tmp_path)

    assert result == {"finalized": 0, "rolled_back": 1, "residuals": []}
    assert not intent.exists()
    assert not models._session_incarnation_claim_file(sid).exists()
