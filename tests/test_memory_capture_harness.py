import json
import sys
from pathlib import Path

from tools.memory_capture.capture import (
    capture_worker_receipt,
    list_quarantine_items,
    main as capture_main,
    promote_quarantine_item,
    quarantine_doctor,
)
from tools.memory_capture.harness import run_harness_command


def test_run_harness_command_auto_captures_failed_tool_error(tmp_path):
    root = tmp_path / ".memory-quarantine"

    result = run_harness_command(
        command=[sys.executable, "-c", "import sys; print('ok'); print('token=sk-abc123456789', file=sys.stderr); sys.exit(7)"],
        root=root,
        session_id="auto-s1",
        project="demo",
        agent="codex",
        tool="terminal",
    )

    assert result.returncode == 7
    assert result.capture is not None
    path = Path(result.capture["path"])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["kind"] == "tool_error"
    assert rows[0]["session_id"] == "auto-s1"
    assert "sk-" not in rows[0]["stderr"]
    assert rows[0]["review_required"] is True


def test_run_harness_command_does_not_capture_success_by_default(tmp_path):
    root = tmp_path / ".memory-quarantine"

    result = run_harness_command(
        command=[sys.executable, "-c", "print('all good')"],
        root=root,
        session_id="auto-s2",
        project="demo",
        agent="qwen",
        tool="terminal",
    )

    assert result.returncode == 0
    assert result.capture is None
    assert quarantine_doctor(root)["counts"]["tool_error"] == 0


def test_capture_worker_receipt_and_list_quarantine_items(tmp_path):
    root = tmp_path / ".memory-quarantine"

    item = capture_worker_receipt(
        root=root,
        session_id="team-s1",
        project="demo",
        agent="forensic-reviewer",
        lane="forensic-reviewer",
        task_id="task-1",
        summary="Reviewed artifact; credential=sk-abc123456789 should redact",
        findings=["Needs human review"],
        artifact_paths=["/tmp/report.md"],
        verification_status="partial",
        next_actions=["Yuto review"],
    )

    path = Path(item["path"])
    data = json.loads(path.read_text())
    assert data["kind"] == "worker_receipt"
    assert data["lane"] == "forensic-reviewer"
    assert "sk-" not in data["summary"]
    assert data["promotion_status"] == "quarantined"

    listed = list_quarantine_items(root, kind="worker_receipt")
    assert [entry["item_id"] for entry in listed] == [item["item_id"]]
    assert quarantine_doctor(root)["counts"]["worker_receipt"] == 1


def test_capture_cli_supports_worker_receipt_and_list(tmp_path, capsys):
    root = tmp_path / ".memory-quarantine"

    code = capture_main(
        [
            "worker-receipt",
            "--root",
            str(root),
            "--session-id",
            "cli-s1",
            "--project",
            "demo",
            "--agent",
            "qwen",
            "--lane",
            "qa-critic",
            "--task-id",
            "task-cli",
            "--summary",
            "Checked output",
            "--finding",
            "Looks useful",
            "--verification-status",
            "pass",
        ]
    )
    assert code == 0
    created = json.loads(capsys.readouterr().out)
    assert created["item_id"].startswith("worker-")

    code = capture_main(["list", "--root", str(root), "--kind", "worker_receipt"])
    assert code == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    assert listed[0]["kind"] == "worker_receipt"


def test_promote_quarantine_item_writes_reviewed_markdown_and_audit(tmp_path):
    root = tmp_path / ".memory-quarantine"
    knowledge = tmp_path / "knowledge"
    item = capture_worker_receipt(
        root=root,
        session_id="promote-s1",
        project="demo",
        agent="qa",
        lane="qa-critic",
        task_id="task-promote",
        summary="Useful stable lesson for team harness",
        findings=["Promotion should create reviewed markdown"],
        verification_status="pass",
    )

    promoted = promote_quarantine_item(
        root=root,
        knowledge_root=knowledge,
        item_id=item["item_id"],
        destination="kg-draft",
        reviewer="yuto",
        rationale="Verified pass receipt from QA lane",
    )

    path = Path(promoted["path"])
    assert path.exists()
    text = path.read_text()
    assert item["item_id"] in text
    assert "Useful stable lesson" in text
    assert "Promotion should create reviewed markdown" in text
    assert promoted["promotion_status"] == "promoted_to_kg_draft"
    assert "promote_quarantine_item" in (root / "audit-log.jsonl").read_text()


def test_promote_quarantine_item_blocks_review_required_without_force(tmp_path):
    root = tmp_path / ".memory-quarantine"
    item = capture_worker_receipt(
        root=root,
        session_id="promote-s2",
        project="demo",
        agent="forensic-reviewer",
        lane="forensic-reviewer",
        task_id="needs-review",
        summary="Partial review needs human gate",
        verification_status="partial",
    )

    try:
        promote_quarantine_item(
            root=root,
            knowledge_root=tmp_path / "knowledge",
            item_id=item["item_id"],
            destination="kg-draft",
            reviewer="yuto",
            rationale="should block",
        )
    except ValueError as exc:
        assert "review_required" in str(exc)
    else:
        raise AssertionError("expected review_required promotion block")


def test_capture_cli_supports_promote(tmp_path, capsys):
    root = tmp_path / ".memory-quarantine"
    knowledge = tmp_path / "knowledge"
    created = capture_worker_receipt(
        root=root,
        session_id="cli-promote",
        project="demo",
        agent="qa",
        lane="qa-critic",
        task_id="cli-task",
        summary="CLI promotion works",
        verification_status="pass",
    )

    code = capture_main(
        [
            "promote",
            "--root",
            str(root),
            "--knowledge-root",
            str(knowledge),
            created["item_id"],
            "--destination",
            "kg-draft",
            "--reviewer",
            "yuto",
            "--rationale",
            "CLI smoke test",
        ]
    )

    assert code == 0
    promoted = json.loads(capsys.readouterr().out)
    assert Path(promoted["path"]).exists()
    assert promoted["item_id"] == created["item_id"]
