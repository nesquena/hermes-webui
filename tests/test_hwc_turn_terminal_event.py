"""hwc: stale error writeback must emit apperror before it returns.

Pending flags are cleared only when active_stream_id is already None.
"""

from pathlib import Path

STREAMING = Path(__file__).resolve().parents[1] / "api" / "streaming.py"
MARKER = "Skipping stale stream error writeback"


def _stale_error_branch(src: str) -> str:
    start = src.find(MARKER)
    assert start >= 0, "stale error writeback marker missing"
    lines = []
    for line in src[start:].splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
        if stripped == "return":
            return "\n".join(lines)
    raise AssertionError("stale error writeback branch has no return")


def test_stale_error_writeback_emits_apperror_before_return():
    branch = _stale_error_branch(STREAMING.read_text(encoding="utf-8"))
    assert "put('apperror'" in branch
    guard = "if getattr(s, 'active_stream_id', None) is None:"
    assert guard in branch
    before, after = branch.split(guard, 1)
    assert "put('apperror'" in before
    assert "pending_user_message = None" not in before
    assert "pending_user_message = None" in after
    assert "pending_attachments = []" in after
    assert "pending_started_at = None" in after
    assert "pending_user_source = None" in after
    assert ".save(" in after
