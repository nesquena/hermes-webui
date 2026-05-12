import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import memory_scout  # noqa: E402


def test_redact_masks_common_secret_patterns():
    text = "api_key=sk-sec...6789 and token: ghp_ab...mnop"

    redacted = memory_scout.redact(text)

    assert "sk-secret" not in redacted
    assert "ghp_" not in redacted
    assert "[REDACTED_SECRET]" in redacted


def test_recent_session_signals_finds_terms_and_redacts(tmp_path, monkeypatch):
    session = tmp_path / "session_1.json"
    session.write_text(
        json.dumps(
            {
                "session_id": "s1",
                "last_updated": "2026-05-12T04:00:00",
                "message_count": 2,
                "messages": [
                    {"role": "user", "content": "AI-Books ล่าสุด token: ghp_ab...mnop"},
                    {"role": "assistant", "content": "Book Expert Factory"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(memory_scout, "SESSIONS_DIR", tmp_path)

    signals = memory_scout.recent_session_signals(limit=3)

    assert signals[0]["session_id"] == "s1"
    assert "AI-Books" in signals[0]["matched_terms"]
    assert "ล่าสุด" in signals[0]["matched_terms"]
    joined = json.dumps(signals, ensure_ascii=False)
    assert "ghp_" not in joined
    assert "[REDACTED_SECRET]" in joined


def test_memory_pressure_reports_long_entries(tmp_path, monkeypatch):
    memory = tmp_path / "MEMORY.md"
    user = tmp_path / "USER.md"
    memory.write_text("short\n§\n" + "long detail " * 20, encoding="utf-8")
    user.write_text("user prefs", encoding="utf-8")
    config = tmp_path / ".hermes" / "config.yaml"
    config.parent.mkdir()
    config.write_text("memory:\n  memory_char_limit: 3000\n  user_char_limit: 1500\n", encoding="utf-8")
    monkeypatch.setattr(memory_scout, "MEMORY_FILE", memory)
    monkeypatch.setattr(memory_scout, "USER_FILE", user)
    monkeypatch.setattr(memory_scout.Path, "home", lambda: tmp_path)

    pressure = memory_scout.memory_pressure()

    assert pressure["memory"]["chars"] > 0
    assert pressure["memory"]["limit"] == 3000
    assert pressure["user"]["limit"] == 1500
    assert pressure["long_memory_entries"][0]["index"] == 2


def test_detect_repo_root_prefers_cwd_with_second_brain(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "second_brain.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("YUTO_REPO", "")
    monkeypatch.chdir(repo)

    assert memory_scout.detect_repo_root() == repo.resolve()


def test_hr_status_reports_role_validator_and_receipts(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    roles = repo / "knowledge" / "company-hr-roles"
    roles.mkdir(parents=True)
    (roles / "hr-role-designer.yaml").write_text("role_id: hr-role-designer\n", encoding="utf-8")
    receipts = repo / "knowledge" / "company-hr-receipts.jsonl"
    receipts.write_text('{"task_id":"t1","role_id":"hr-role-designer"}\n', encoding="utf-8")
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "company_hr_roles.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(memory_scout, "ROOT", repo)
    monkeypatch.setattr(memory_scout, "HR_ROLES_DIR", roles)
    monkeypatch.setattr(memory_scout, "HR_RECEIPTS", receipts)
    monkeypatch.setattr(memory_scout.subprocess, "run", lambda *args, **kwargs: type("P", (), {"returncode": 0, "stdout": '{"ok": true, "role_ids": ["hr-role-designer"], "files_checked": 1, "errors": [], "warnings": [], "receipt_summary": {"count": 1, "role_ids": ["hr-role-designer"]}}', "stderr": ""})())

    status = memory_scout.hr_people_ops_status()

    assert status["roles_dir_exists"] is True
    assert status["role_manifest_count"] == 1
    assert status["validator"]["ok"] is True
    assert status["validator"]["receipt_summary"]["count"] == 1


def test_workforce_kit_status_reports_validator(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    kit = repo / "knowledge" / "company-workforce"
    kit.mkdir(parents=True)
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "company_workforce.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(memory_scout, "ROOT", repo)
    monkeypatch.setattr(memory_scout, "WORKFORCE_KIT", kit)
    monkeypatch.setattr(memory_scout.subprocess, "run", lambda *args, **kwargs: type("P", (), {"returncode": 0, "stdout": '{"ok": true, "summary": {"departments": 11, "skill_categories": 10, "rule_categories": 9}, "errors": [], "warnings": []}', "stderr": ""})())

    status = memory_scout.workforce_kit_status()

    assert status["kit_exists"] is True
    assert status["validator"]["ok"] is True
    assert status["validator"]["summary"]["departments"] == 11


def test_digital_forensic_lab_status_reports_validator(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    lab = repo / "knowledge" / "digital-forensic-lab"
    lab.mkdir(parents=True)
    (lab / "lab-charter.yaml").write_text("phase: phase_0_internal_only\n", encoding="utf-8")
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "digital_forensic_lab.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(memory_scout, "ROOT", repo)
    monkeypatch.setattr(memory_scout, "DIGITAL_FORENSIC_LAB", lab)
    monkeypatch.setattr(memory_scout.subprocess, "run", lambda *args, **kwargs: type("P", (), {"returncode": 0, "stdout": '{"ok": true, "lab": {"summary": {"lab_artifacts": 9, "synthetic_evidence_items": 1}}, "workforce_links": {"summary": {"digital_forensic_personnel": 3}}}', "stderr": ""})())

    status = memory_scout.digital_forensic_lab_status()

    assert status["lab_exists"] is True
    assert status["validator"]["ok"] is True
    assert status["validator"]["lab"]["summary"]["lab_artifacts"] == 9


def test_snapshot_reports_repo_root(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "second_brain.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(memory_scout, "ROOT", repo)
    monkeypatch.setattr(memory_scout, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(memory_scout, "USER_FILE", tmp_path / "USER.md")
    monkeypatch.setattr(memory_scout, "MEMORY_FILE", tmp_path / "MEMORY.md")
    monkeypatch.setattr(memory_scout, "second_brain_status", lambda: {"ok": True})
    monkeypatch.setattr(memory_scout, "book_factory_status", lambda: {"sources": 0, "blueprints": [], "receipts": 0})
    monkeypatch.setattr(memory_scout, "team_receipt_status", lambda: {"exists": False})
    monkeypatch.setattr(memory_scout, "hr_people_ops_status", lambda: {"validator": {"ok": True}})
    monkeypatch.setattr(memory_scout, "workforce_kit_status", lambda: {"validator": {"ok": True}})
    monkeypatch.setattr(memory_scout, "digital_forensic_lab_status", lambda: {"validator": {"ok": True}})

    snapshot = memory_scout.build_snapshot(session_limit=1)

    assert snapshot["repo_root"] == str(repo)
    assert snapshot["hr_people_ops"]["validator"]["ok"] is True
    assert snapshot["workforce_kit"]["validator"]["ok"] is True
    assert snapshot["digital_forensic_lab"]["validator"]["ok"] is True
