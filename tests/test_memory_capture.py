import json
from pathlib import Path

from tools.memory_capture.capture import (
    capture_session_summary,
    capture_tool_error,
    quarantine_doctor,
)
from tools.memory_capture.privacy_filter import sanitize_text


def test_sanitize_text_redacts_common_secrets_and_private_blocks():
    text = """
    api_key = sk-proj-abcdefghijklmnopqrstuvwxyz123456
    Authorization: Bearer abcdefghijklmnopqrstuvwxyz1234567890
    github_pat_1234567890abcdefghijklmnopqrstuv_1234567890abcdefghijklmnopqrstuv
    <private>do not store this private note</private>
    normal text survives
    """

    result = sanitize_text(text)

    assert "normal text survives" in result.text
    assert "sk-proj-" not in result.text
    assert "Bearer abc" not in result.text
    assert "github_pat_" not in result.text
    assert "do not store" not in result.text
    assert result.safe_to_store is False
    assert {r.type for r in result.redactions} >= {"openai_api_key", "bearer_token", "github_token", "private_block"}


def test_capture_tool_error_writes_sanitized_jsonl_and_audit(tmp_path):
    root = tmp_path / ".memory-quarantine"

    item = capture_tool_error(
        root=root,
        session_id="s1",
        project="demo",
        agent="codex",
        tool="terminal",
        command="pytest tests -q",
        exit_code=1,
        stderr="failed with token=sk-abcdefghijklmnopqrstuvwxyz123456",
        stdout="some stdout",
    )

    path = Path(item["path"])
    assert path.exists()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["kind"] == "tool_error"
    assert rows[0]["status"] == "sanitized"
    assert "sk-" not in rows[0]["stderr"]
    assert rows[0]["redactions"]

    audit = root / "audit-log.jsonl"
    assert audit.exists()
    assert "capture_tool_error" in audit.read_text()


def test_capture_session_summary_and_doctor(tmp_path):
    root = tmp_path / ".memory-quarantine"

    item = capture_session_summary(
        root=root,
        session_id="s2",
        project="demo",
        agent="yuto",
        decisions=["Use quarantine before KG"],
        verified_outputs=["privacy filter test passed"],
        open_risks=["No hook integration yet"],
        changed_files=["tools/memory_capture/privacy_filter.py"],
    )

    path = Path(item["path"])
    data = json.loads(path.read_text())
    assert data["kind"] == "session_summary"
    assert data["status"] == "sanitized"
    assert data["decisions"] == ["Use quarantine before KG"]

    health = quarantine_doctor(root)
    assert health["ok"] is True
    assert health["counts"]["session_summary"] == 1
    assert health["counts"]["tool_error"] == 0
