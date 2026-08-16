"""The agent's merge-into-tail compaction wraps its summary in a
``[PRIOR CONTEXT — for reference only…]`` envelope whose compaction marker
(``[CONTEXT COMPACTION — REFERENCE ONLY]``) only appears *after* the
``[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]`` delimiter.  Every WebUI
detector was keyed on the text *starting with* ``[context compaction``, so a
merged marker was invisible: no anchor summary was persisted, the automatic
tail reduction refused to fire (``no_trusted_boundary``), and after a reload
the full transcript came back.

These tests pin the merged-marker recognition end to end."""

import pytest

from api.compression_anchor import (
    compaction_summary_segment,
    is_context_compression_marker,
)
from api.models import _context_messages_include_compression_marker
from api.streaming import (
    _auto_snapshot_summary_from_compression,
    _compression_tail_after_latest_compaction,
    _is_trusted_auto_compression_marker,
)


MERGED_MARKER_TEXT = (
    "[PRIOR CONTEXT — for reference only; not a new message]\n"
    "\n"
    "user: earlier question\n"
    "assistant: earlier answer\n"
    "\n"
    "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n"
    "\n"
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into "
    "the summary below. This is a handoff from a previous context window.\n"
    "## Historical Task Snapshot\n"
    "The user asked for a thing; it is partially done.\n"
    "## Goal\n"
    "Finish the thing.\n"
    "\n"
    "--- END OF CONTEXT SUMMARY — respond to the message below, not the "
    "summary above ---"
)


def _merged_marker(role="assistant", flagged=True):
    msg = {"role": role, "content": MERGED_MARKER_TEXT}
    if flagged:
        msg["_compressed_summary"] = True
    return msg


def test_segment_extracts_summary_after_delimiter():
    segment = compaction_summary_segment(MERGED_MARKER_TEXT)
    assert segment is not None
    assert segment.lstrip().lower().startswith("[context compaction")
    assert "Historical Task Snapshot" in segment


def test_segment_passes_plain_marker_through():
    plain = "[CONTEXT COMPACTION — REFERENCE ONLY] hello\n## Recap\nx"
    assert compaction_summary_segment(plain) == plain


def test_segment_rejects_ordinary_text():
    assert compaction_summary_segment("just a normal message") is None


def test_flagged_merged_marker_is_recognized():
    assert is_context_compression_marker(_merged_marker()) is True


def test_unflagged_merged_text_stays_untrusted():
    # Pasted user text must never become a marker: without the synthetic
    # ``_compressed_summary`` stamp the merged envelope is not a marker.
    assert is_context_compression_marker(_merged_marker(flagged=False)) is False


def test_trusted_auto_marker_accepts_merged_form():
    assert _is_trusted_auto_compression_marker(_merged_marker()) is True
    assert _is_trusted_auto_compression_marker(_merged_marker(flagged=False)) is False
    tool_shaped = dict(_merged_marker(), role="tool")
    assert _is_trusted_auto_compression_marker(tool_shaped) is False


def test_summary_extraction_from_merged_marker():
    context_messages = [
        {"role": "user", "content": "hello"},
        _merged_marker(),
        {"role": "assistant", "content": "continuing"},
    ]
    summary = _auto_snapshot_summary_from_compression([], context_messages)
    assert summary is not None
    assert summary.startswith("## Historical Task Snapshot")
    assert "Finish the thing." in summary
    # The prior-context replay and the END footer must both be stripped.
    assert "earlier answer" not in summary
    assert "END OF CONTEXT SUMMARY" not in summary


def test_tail_boundary_uses_merged_marker():
    rows = [
        {"role": "user", "content": "old turn"},
        {"role": "assistant", "content": "old answer"},
        _merged_marker(),
        {"role": "user", "content": "fresh question"},
        {"role": "assistant", "content": "fresh answer"},
    ]
    tail = _compression_tail_after_latest_compaction(rows)
    assert len(tail) == 3
    assert tail[0].get("_compressed_summary") is True


def test_context_messages_include_merged_marker():
    assert _context_messages_include_compression_marker([_merged_marker()]) is True
    assert (
        _context_messages_include_compression_marker([_merged_marker(flagged=False)])
        is False
    )
