"""Regression for #4720: done settlement keeps absolute transcript indexes valid.

The old implementation reset _oldestIdx from d.session._messages_offset. This
now exercises the actual listener statements for full transcripts, explicit
valid offsets, bounded merged tails, and empty retained snapshots instead of
searching for the old assignment spelling.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_impl = importlib.util.spec_from_file_location(
    "resident_history_recovery_tail",
    REPO / "tests" / "test_resident_history_recovery_tail.py",
)
assert _impl and _impl.loader
_tail = importlib.util.module_from_spec(_impl)
_impl.loader.exec_module(_tail)
_listener = _tail._listener
_sample_messages = _tail._sample_messages


def test_full_done_payload_resets_offset_to_zero():
    prev = _sample_messages()
    got = _listener(prev, {"session_id": "A", "messages": prev}, pre_oldest_idx=70)
    assert got["texts"] == [m["content"] for m in prev]
    assert got["oldestIdx"] == 0


def test_valid_explicit_done_offset_is_honored():
    prev = _sample_messages()
    got = _listener(prev, {"session_id": "A", "messages": prev, "_messages_offset": 12})
    assert got["texts"] == [m["content"] for m in prev]
    assert got["oldestIdx"] == 12


def test_bounded_merge_retains_resident_origin_and_rows():
    prev = _sample_messages()[:4]
    got = _listener(
        prev,
        {
            "session_id": "A",
            "messages": prev[2:],
            "transcript_generation": 0,
            "_messages_offset": 72,
            "message_count": 74,
        },
        pre_generation=0,
        pre_oldest_idx=70,
    )
    # 70 + 4 rows == 74, and no duplicate prefix or reset-to-tail offset occurs.
    assert got["texts"] == [m["content"] for m in prev]
    assert got["oldestIdx"] == 70


def test_empty_done_snapshot_keeps_resident_origin():
    prev = _sample_messages()
    got = _listener(
        prev,
        {"session_id": "A", "messages": [], "message_count": 6},
        pre_generation=0,
        pre_oldest_idx=70,
    )
    assert got["texts"] == [m["content"] for m in prev]
    assert got["oldestIdx"] == 70
