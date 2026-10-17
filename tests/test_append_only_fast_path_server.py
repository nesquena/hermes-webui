"""Server-side append-only fast path unit checks (#7925 re-gate)."""
from __future__ import annotations

from api.helpers import _public_message_projection
from api.routes import _append_anchor_fingerprint, _append_fast_path_anchor, _message_window_for_display


def _make_rows(n: int) -> list:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}", "timestamp": 1700000000 + i}
        for i in range(n)
    ]


class _FakeSession:
    def __init__(self, generation: int = 0):
        self.transcript_generation = generation


def test_msg_after_window_returns_rows_strictly_after_anchor():
    rows = _make_rows(2001)
    window, offset = _message_window_for_display(rows, msg_limit=500, msg_after=1999)
    assert len(window) == 1
    assert offset == 2000
    assert window[0]["content"] == "m2000"


def test_msg_after_window_empty_at_end_of_transcript():
    rows = _make_rows(601)
    window, offset = _message_window_for_display(rows, msg_limit=500, msg_after=600)
    assert window == []
    # offset = len(messages) means "nothing after the anchor"; the handler's
    # append-mode truncation check (offset + len < total) is therefore False.
    assert offset == len(rows)


def test_msg_after_window_caps_at_limit_and_reports_more():
    rows = _make_rows(2003)
    window, offset = _message_window_for_display(rows, msg_limit=500, msg_after=500)
    assert len(window) == 500
    assert offset == 501
    assert offset + len(window) < len(rows)  # more rows exist after the cap


def test_msg_after_window_skips_anchor_trailing_tool_rows():
    rows = _make_rows(40)
    # Anchor is an assistant row whose tool result rows immediately follow it;
    # those belong to the anchor (already rendered) and must be skipped.
    rows[10] = {"role": "assistant", "content": "call", "timestamp": 1, "tool_calls": [{"id": "t1"}]}
    rows[11] = {"role": "tool", "tool_call_id": "t1", "content": "result", "timestamp": 2}
    window, offset = _message_window_for_display(rows, msg_limit=500, msg_after=10)
    assert window[0]["content"] == "m12"
    assert offset == 12


def test_append_fast_path_anchor_validates_generation_fingerprint_and_range():
    rows = _make_rows(2001)
    projected = _public_message_projection(rows[1999], _enabled=True)
    fp = _append_anchor_fingerprint(projected)
    assert fp and len(fp) == 26
    s = _FakeSession(generation=0)
    assert _append_fast_path_anchor(s, rows, "1999", fp, "0") == 1999
    # generation changed → None (fall back to the authoritative full fetch)
    assert _append_fast_path_anchor(s, rows, "1999", fp, "1") is None
    # anchor fingerprint mismatch → None
    assert _append_fast_path_anchor(s, rows, "1999", "x" * 26, "0") is None
    # anchor out of range → None
    assert _append_fast_path_anchor(s, rows, "999999", fp, "0") is None
    # unparsable index → None
    assert _append_fast_path_anchor(s, rows, "abc", fp, "0") is None
    # missing fingerprint → None
    assert _append_fast_path_anchor(s, rows, "1999", None, "0") is None


def test_append_fast_path_anchor_detects_rewritten_seam_row():
    rows = _make_rows(2001)
    rewritten = [dict(r) for r in rows]
    rewritten[1999]["content"] = "m1999-edited"
    projected = _public_message_projection(rewritten[1999], _enabled=True)
    fp = _append_anchor_fingerprint(projected)
    s = _FakeSession(generation=0)
    # The client would fingerprint its OLD row; the server's row now differs.
    client_fp = _append_anchor_fingerprint(
        _public_message_projection(rows[1999], _enabled=True)
    )
    assert client_fp != fp
    assert _append_fast_path_anchor(s, rewritten, "1999", client_fp, "0") is None
