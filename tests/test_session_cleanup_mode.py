"""Safety tests for WebUI session cleanup mode.

Cleanup mode must start with a read-only report, then use reversible quarantine
with a manifest. It must not touch state.db or hard-delete session JSON files.
"""
import json
import os
import calendar
import time
from pathlib import Path

import pytest


def _write_session(root: Path, sid: str, *, days_old=40, source="webui", messages=None, **extra):
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": sid,
        "title": sid,
        "source": source,
        "updated_at": time.time() - days_old * 86400,
        "created_at": time.time() - days_old * 86400,
        "messages": [] if messages is None else messages,
    }
    payload.update(extra)
    path = root / f"{sid}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_cleanup_report_is_read_only_and_classifies_safe_candidates(tmp_path):
    from api.session_cleanup import build_session_cleanup_report

    sessions = tmp_path / "sessions"
    state_db = tmp_path / "state.db"
    state_db.write_text("do-not-touch", encoding="utf-8")

    cron_ok = _write_session(sessions, "cron_ok_old", days_old=20, source="cron", messages=[{"role":"assistant","content":"ok"}], last_status="ok")
    zero = _write_session(sessions, "empty_old", days_old=1, source="webui", messages=[])
    pinned = _write_session(sessions, "pinned_old", days_old=120, source="webui", messages=[{"role":"user","content":"keep"}], pinned=True)
    active = _write_session(sessions, "active_old", days_old=120, source="webui", messages=[{"role":"user","content":"keep"}], active_stream_id="stream-1")
    telegram = _write_session(sessions, "tg_old", days_old=120, source="telegram", messages=[{"role":"user","content":"keep"}])
    unknown = _write_session(sessions, "unknown_old", days_old=120, source="mystery", messages=[{"role":"user","content":"review"}])

    mtimes_before = {p.name: p.stat().st_mtime_ns for p in sessions.glob("*.json")}
    report = build_session_cleanup_report(session_dir=sessions, state_db_path=state_db, now=time.time())
    mtimes_after = {p.name: p.stat().st_mtime_ns for p in sessions.glob("*.json")}

    assert mtimes_after == mtimes_before
    assert state_db.read_text(encoding="utf-8") == "do-not-touch"
    assert report["mode"] == "read_only"
    assert report["summary"]["total_sessions"] == 6
    assert report["summary"]["state_db_touched"] is False
    assert {c["session_id"] for c in report["cleanup_candidates"]} == {"cron_ok_old", "empty_old"}
    assert {p["session_id"] for p in report["protected"]} == {"pinned_old", "active_old", "tg_old"}
    assert {r["session_id"] for r in report["needs_review"]} == {"unknown_old"}
    assert report["summary"]["estimated_reclaim_bytes"] == cron_ok.stat().st_size + zero.stat().st_size


def test_report_rejects_session_id_filename_mismatch_to_protect_real_session(tmp_path):
    from api.session_cleanup import build_session_cleanup_report, quarantine_sessions

    sessions = tmp_path / "sessions"
    trash = tmp_path / "session-trash"
    _write_session(sessions, "pinned_old", days_old=120, source="webui", messages=[{"role":"user","content":"keep"}], pinned=True)
    decoy = {
        "session_id": "pinned_old",
        "title": "decoy",
        "source": "webui",
        "updated_at": time.time() - 120 * 86400,
        "messages": [],
    }
    (sessions / "decoy.json").write_text(json.dumps(decoy), encoding="utf-8")

    report = build_session_cleanup_report(session_dir=sessions, now=time.time())
    assert "pinned_old" not in {c["session_id"] for c in report["cleanup_candidates"]}
    assert {i.get("path") for i in report["invalid"]} == {"decoy.json"}

    result = quarantine_sessions(["pinned_old"], report=report, session_dir=sessions, trash_root=trash, actor="pytest")
    assert result["moved"] == []
    assert (sessions / "pinned_old.json").exists()


def test_old_non_archived_webui_session_is_protected_not_cleanup_candidate(tmp_path):
    from api.session_cleanup import build_session_cleanup_report

    sessions = tmp_path / "sessions"
    _write_session(sessions, "normal_old", days_old=90, source="webui", messages=[{"role":"user","content":"keep"}])

    report = build_session_cleanup_report(session_dir=sessions, now=time.time())
    assert "normal_old" not in {c["session_id"] for c in report["cleanup_candidates"]}
    protected = {p["session_id"]: p for p in report["protected"]}
    assert "normal_old" in protected
    assert protected["normal_old"]["reasons"] == ["webui_non_archived_retained"]


def test_archived_webui_chat_with_durable_content_requires_structured_review(tmp_path):
    from api.session_cleanup import build_session_cleanup_report, build_session_retention_plan

    sessions = tmp_path / "sessions"
    _write_session(
        sessions,
        "archived_preference",
        days_old=60,
        source="webui",
        archived=True,
        messages=[
            {"role": "user", "content": "기억해둬. 나는 WebUI 채팅 삭제 전에 중요한 선호는 구조화해서 남기는 방식을 선호해."},
            {"role": "assistant", "content": "확인했습니다."},
        ],
    )
    _write_session(
        sessions,
        "archived_noise",
        days_old=60,
        source="webui",
        archived=True,
        messages=[{"role": "user", "content": "임시 테스트 대화"}],
    )

    report = build_session_cleanup_report(session_dir=sessions, now=time.time())

    assert {c["session_id"] for c in report["cleanup_candidates"]} == {"archived_noise"}
    review = {r["session_id"]: r for r in report["needs_review"]}
    assert "archived_preference" in review
    assert review["archived_preference"]["retention_decision"] == "structure_before_delete"
    assert "memory_candidate" in review["archived_preference"]["structured_targets"]
    assert "raw_chat_not_long_term_memory" in review["archived_preference"]["reasons"]

    plan = build_session_retention_plan(report)
    assert plan["summary"]["structure_before_delete_count"] == 1
    assert plan["summary"]["quarantine_ready_count"] == 1
    assert plan["structured_candidates"][0]["session_id"] == "archived_preference"
    assert plan["quarantine_ready"][0]["session_id"] == "archived_noise"


def test_structured_review_identifies_skill_obsidian_handoff_and_secret_risk(tmp_path):
    from api.session_cleanup import build_session_cleanup_report

    sessions = tmp_path / "sessions"
    _write_session(
        sessions,
        "archived_workflow",
        days_old=60,
        source="webui",
        archived=True,
        messages=[{"role": "user", "content": "반복 절차: 실패하면 백업 후 검증한다. 다음 safe_next_action도 남겨라."}],
    )
    _write_session(
        sessions,
        "archived_rule",
        days_old=60,
        source="webui",
        archived=True,
        messages=[{"role": "user", "content": "이 규칙은 Obsidian MR 승인 경계와 Rule Map에 관련된다."}],
    )
    _write_session(
        sessions,
        "archived_secret",
        days_old=60,
        source="webui",
        archived=True,
        title="api_key=SECRET should be redacted",
        messages=[{"role": "user", "content": "api_key=SECRET token 값은 저장하지 말 것"}],
    )

    report = build_session_cleanup_report(session_dir=sessions, now=time.time())
    review = {r["session_id"]: r for r in report["needs_review"]}

    assert set(review) == {"archived_workflow", "archived_rule", "archived_secret"}
    assert {"skill_candidate", "handoff_candidate"}.issubset(set(review["archived_workflow"]["structured_targets"]))
    assert "obsidian_candidate" in review["archived_rule"]["structured_targets"]
    assert review["archived_secret"]["retention_decision"] == "do_not_preserve_secret_review"
    assert "secret_risk" in review["archived_secret"]["structured_targets"]
    assert "SECRET" not in review["archived_secret"]["title"]
    assert "[REDACTED]" in review["archived_secret"]["title"]

    from api.session_cleanup import build_session_retention_plan
    plan = build_session_retention_plan(report)
    plan_secret = {r["session_id"]: r for r in plan["structured_candidates"]}["archived_secret"]
    assert "SECRET" not in plan_secret["title"]
    assert "[REDACTED]" in plan_secret["title"]


def test_report_rejects_symlink_sessions_before_quarantine(tmp_path):
    from api.session_cleanup import build_session_cleanup_report, quarantine_sessions

    sessions = tmp_path / "sessions"
    trash = tmp_path / "session-trash"
    active_payload = {
        "title": "active",
        "source": "webui",
        "updated_at": time.time() - 120 * 86400,
        "messages": [{"role":"user","content":"keep"}],
    }
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "active.json").write_text(json.dumps(active_payload), encoding="utf-8")
    (sessions / "decoy.json").symlink_to(sessions / "active.json")

    report = build_session_cleanup_report(session_dir=sessions, now=time.time(), current_session_id="active")
    assert {p["session_id"] for p in report["protected"]} == {"active"}
    assert "decoy" not in {c["session_id"] for c in report["cleanup_candidates"]}
    assert {i.get("path") for i in report["invalid"]} == {"decoy.json"}

    result = quarantine_sessions(["decoy"], report=report, session_dir=sessions, trash_root=trash, actor="pytest")
    assert result["moved"] == []
    assert (sessions / "active.json").exists()


def test_quarantine_moves_only_report_candidates_and_writes_restore_manifest(tmp_path):
    from api.session_cleanup import build_session_cleanup_report, quarantine_sessions, restore_quarantine

    sessions = tmp_path / "sessions"
    trash = tmp_path / "session-trash"
    _write_session(sessions, "cron_ok_old", days_old=20, source="cron", messages=[{"role":"assistant","content":"ok"}], last_status="ok")
    _write_session(sessions, "pinned_old", days_old=120, source="webui", messages=[{"role":"user","content":"keep"}], pinned=True)
    (sessions / "_index.json").write_text("[]", encoding="utf-8")

    report = build_session_cleanup_report(session_dir=sessions, now=time.time())
    result = quarantine_sessions(
        ["cron_ok_old", "pinned_old"],
        report=report,
        session_dir=sessions,
        trash_root=trash,
        actor="pytest",
    )

    assert result["ok"] is True
    assert result["moved"] == ["cron_ok_old"]
    assert result["skipped"][0]["session_id"] == "pinned_old"
    assert not (sessions / "cron_ok_old.json").exists()
    assert (sessions / "pinned_old.json").exists()
    assert not (sessions / "_index.json").exists(), "index must be invalidated after quarantine"

    manifest = Path(result["manifest_path"])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["operation"] == "quarantine"
    assert payload["state_db_touched"] is False
    assert payload["items"][0]["session_id"] == "cron_ok_old"
    assert (manifest.parent / "sessions" / "cron_ok_old.json").exists()

    restored = restore_quarantine(manifest, session_dir=sessions)
    assert restored["ok"] is True
    assert restored["restored"] == ["cron_ok_old"]
    assert (sessions / "cron_ok_old.json").exists()
    assert not (sessions / "_index.json").exists(), "index must be invalidated after restore"


def test_restore_rejects_manifest_trash_path_outside_quarantine_batch(tmp_path):
    from api.session_cleanup import restore_quarantine

    sessions = tmp_path / "sessions"
    batch = tmp_path / "session-trash" / "20260508-000000-test"
    batch.mkdir(parents=True)
    external = tmp_path / "external.json"
    external.write_text('{"session_id":"evil"}', encoding="utf-8")
    manifest = batch / "manifest.json"
    manifest.write_text(json.dumps({
        "operation": "quarantine",
        "items": [{"session_id": "evil", "trash_path": str(external)}],
    }), encoding="utf-8")

    result = restore_quarantine(manifest, session_dir=sessions)
    assert result["restored"] == []
    assert result["skipped"] == [{"session_id": "evil", "reason": "trash_path_escape"}]
    assert external.exists()
    assert not (sessions / "evil.json").exists()


def test_core_agent_session_prefixed_filename_can_be_scanned_quarantined_and_restored(tmp_path):
    from api.session_cleanup import build_session_cleanup_report, quarantine_sessions, restore_quarantine

    sessions = tmp_path / "sessions"
    trash = tmp_path / "session-trash"
    sessions.mkdir(parents=True)
    sid = "cron_abc123_20260420_010203"
    payload = {
        "session_id": sid,
        "title": "cron core session",
        "platform": "cron",
        "last_updated": "2026-04-20T01:02:03",
        "session_start": "2026-04-20T01:01:01",
        "messages": [{"role": "assistant", "content": "ok"}],
    }
    path = sessions / f"session_{sid}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    fresh_mtime = time.time()
    os.utime(path, (fresh_mtime, fresh_mtime))
    now = calendar.timegm(time.strptime("2026-05-10T01:02:03", "%Y-%m-%dT%H:%M:%S"))

    report = build_session_cleanup_report(session_dir=sessions, now=now)
    assert {c["session_id"] for c in report["cleanup_candidates"]} == {sid}
    assert report["cleanup_candidates"][0]["source"] == "cron"
    assert report["cleanup_candidates"][0]["age_days"] == 20.0

    quarantined = quarantine_sessions([sid], report=report, session_dir=sessions, trash_root=trash, actor="pytest")
    manifest = Path(quarantined["manifest_path"])
    assert not (sessions / f"session_{sid}.json").exists()
    assert (manifest.parent / "sessions" / f"session_{sid}.json").exists()

    restored = restore_quarantine(manifest, session_dir=sessions)
    assert restored["restored"] == [sid]
    assert (sessions / f"session_{sid}.json").exists()


def test_quarantine_uses_exact_reported_candidate_path_when_prefixed_and_unprefixed_ids_collide(tmp_path):
    from api.session_cleanup import build_session_cleanup_report, quarantine_sessions

    sessions = tmp_path / "sessions"
    trash = tmp_path / "session-trash"
    sessions.mkdir(parents=True)
    sid = "abc123"
    protected = {
        "session_id": sid,
        "title": "protected unprefixed",
        "source": "webui",
        "updated_at": time.time() - 120 * 86400,
        "messages": [{"role": "user", "content": "keep"}],
        "pinned": True,
    }
    candidate = {
        "session_id": sid,
        "title": "candidate prefixed",
        "platform": "cron",
        "updated_at": time.time() - 20 * 86400,
        "messages": [{"role": "assistant", "content": "ok"}],
    }
    (sessions / f"{sid}.json").write_text(json.dumps(protected), encoding="utf-8")
    (sessions / f"session_{sid}.json").write_text(json.dumps(candidate), encoding="utf-8")

    report = build_session_cleanup_report(session_dir=sessions, now=time.time())
    assert [(c["session_id"], c["path"]) for c in report["cleanup_candidates"]] == [(sid, f"session_{sid}.json")]
    assert [(p["session_id"], p["path"]) for p in report["protected"]] == [(sid, f"{sid}.json")]

    result = quarantine_sessions([sid], report=report, session_dir=sessions, trash_root=trash, actor="pytest")

    assert result["moved"] == [sid]
    manifest = Path(result["manifest_path"])
    assert (sessions / f"{sid}.json").exists(), "protected unprefixed file must not move"
    assert not (sessions / f"session_{sid}.json").exists()
    assert (manifest.parent / "sessions" / f"session_{sid}.json").exists()
    assert not (manifest.parent / "sessions" / f"{sid}.json").exists()


def test_delete_quarantine_only_removes_manifest_listed_trash_files(tmp_path):
    from api.session_cleanup import build_session_cleanup_report, quarantine_sessions, delete_quarantine

    sessions = tmp_path / "sessions"
    trash = tmp_path / "session-trash"
    live = _write_session(sessions, "cron_ok_old", days_old=20, source="cron", messages=[{"role":"assistant","content":"ok"}], last_status="ok")
    state_db = tmp_path / "state.db"
    state_db.write_text("do-not-touch", encoding="utf-8")

    report = build_session_cleanup_report(session_dir=sessions, state_db_path=state_db, now=time.time())
    quarantined = quarantine_sessions(["cron_ok_old"], report=report, session_dir=sessions, trash_root=trash, actor="pytest")
    manifest = Path(quarantined["manifest_path"])
    stray = manifest.parent / "sessions" / "stray.json"
    stray.write_text('{"session_id":"stray"}', encoding="utf-8")

    result = delete_quarantine(manifest)

    assert result["ok"] is True
    assert result["deleted"] == ["cron_ok_old"]
    assert result["state_db_touched"] is False
    assert state_db.read_text(encoding="utf-8") == "do-not-touch"
    assert not live.exists()
    assert not (manifest.parent / "sessions" / "cron_ok_old.json").exists()
    assert stray.exists(), "hard delete must be manifest-only, not batch-directory glob delete"
    deletion_manifest = manifest.parent / "deleted-manifest.json"
    assert deletion_manifest.exists()
    assert json.loads(deletion_manifest.read_text(encoding="utf-8"))["operation"] == "delete_quarantine"


def test_delete_quarantine_rejects_live_session_paths_and_path_escape(tmp_path):
    from api.session_cleanup import delete_quarantine

    sessions = tmp_path / "sessions"
    live = _write_session(sessions, "keep_live", days_old=120, source="webui", messages=[])
    batch = tmp_path / "session-trash" / "20260508-000000-test"
    batch.mkdir(parents=True)
    manifest = batch / "manifest.json"
    manifest.write_text(json.dumps({
        "operation": "quarantine",
        "items": [
            {"session_id": "keep_live", "trash_path": str(live)},
            {"session_id": "escape", "trash_path": str(tmp_path / "escape.json")},
        ],
    }), encoding="utf-8")

    result = delete_quarantine(manifest)

    assert result["deleted"] == []
    assert result["skipped"] == [
        {"session_id": "keep_live", "reason": "trash_path_escape"},
        {"session_id": "escape", "reason": "trash_path_escape"},
    ]
    assert live.exists()


def test_session_cleanup_routes_are_registered_and_ui_is_explicitly_reversible():
    root = Path(__file__).resolve().parent.parent
    routes = (root / "api" / "routes.py").read_text(encoding="utf-8")
    index = (root / "static" / "index.html").read_text(encoding="utf-8")
    panels = (root / "static" / "panels.js").read_text(encoding="utf-8")

    assert '"/api/sessions/cleanup"' in routes
    assert 'legacy cleanup endpoint disabled; use cleanup_report + quarantine' in routes
    assert '"/api/sessions/cleanup_zero_message"' in routes
    assert '"/api/sessions/cleanup_report"' in routes
    assert '"/api/sessions/quarantine"' in routes
    assert '"/api/sessions/restore_quarantine"' in routes
    assert '"/api/sessions/delete_quarantine"' in routes
    assert 'session_dir=STATE_DIR.parent / "sessions"' in routes
    assert 'trash_root=STATE_DIR.parent / "session-trash"' in routes
    assert 'id="sessionCleanupReport"' in index
    assert "Read-only scan" in index
    assert "Quarantine selected" in index
    assert "Restore latest quarantine" in index
    assert "Delete quarantine only" in index
    assert "state.db is not modified" in index
    assert "loadSessionCleanupReport" in panels
    assert "quarantineSessionCleanupCandidates" in panels
    assert "restoreLatestSessionQuarantine" in panels
    assert "deleteLatestSessionQuarantine" in panels
