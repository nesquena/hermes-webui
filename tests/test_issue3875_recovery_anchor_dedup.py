"""Regression tests for #3875 — blank transcript from accumulated empty recovery anchors.

A session that was interrupted-and-recovered many times accumulated thousands of
empty-content assistant rows tagged ``_recovered_from_run_journal``. Each was an
"anchor" created by ``_append_journaled_partial_output`` / ``ensure_assistant_anchor``
to host recovered tool cards for a tool-first stream (one that emitted tools before
any visible text). Because a tool-first stream has no text to dedup on, the read-side
lazy-retry path re-created a fresh empty anchor on every retry, and every distinct
interrupted stream added its own — so the session filled with empty rows. Combined
with the render path (which dropped empty-content reasoning-only rows), the transcript
painted blank (only date separators).

This file covers the DATA side: the anchor-dedup guard so recovery reuses a single
empty anchor per stream instead of appending an unbounded run of them. The render-side
fix (surface the message's `reasoning` field so an empty-content turn never paints
blank) is covered by tests/test_issue3875_blank_transcript_failsafe.py.
"""
from __future__ import annotations

import pytest

import api.profiles as profiles
from api.models import Session, _append_journaled_partial_output
from api.run_journal import append_run_event


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    # Mirror tests/test_session_lost_response_regression.py — isolate HERMES_HOME
    # so Session.save() + run-journal writes land in a throwaway sandbox.
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    return home


def _tool_first_journal(session_id: str, stream_id: str) -> None:
    """Write a run journal for a stream that emitted a tool BEFORE any text —
    the shape that forces an empty assistant anchor on recovery."""
    append_run_event(session_id, stream_id, "tool", {"name": "search_files", "preview": "q=x"})
    append_run_event(session_id, stream_id, "tool_complete", {"name": "search_files", "preview": "done"})


def test_tool_first_recovery_creates_single_empty_anchor(hermes_home):
    sid = "issue3875_anchor"
    stream_id = "stream-A"
    _tool_first_journal(sid, stream_id)
    s = Session(session_id=sid, title="repro", messages=[{"role": "user", "content": "go"}])

    # First recovery: one empty anchor is created to host the recovered tool card.
    assert _append_journaled_partial_output(s, stream_id, dedupe_existing=True) is True
    anchors = [
        m for m in s.messages
        if isinstance(m, dict)
        and m.get("_recovered_from_run_journal")
        and m.get("role") == "assistant"
        and not str(m.get("content") or "").strip()
    ]
    assert len(anchors) == 1, "first recovery should create exactly one empty anchor"


def test_repeated_recovery_does_not_accumulate_empty_anchors(hermes_home):
    """The #3875 root cause: re-running recovery for the SAME stream must reuse the
    existing empty anchor, not pile up a fresh one each time."""
    sid = "issue3875_repeat"
    stream_id = "stream-B"
    _tool_first_journal(sid, stream_id)
    s = Session(session_id=sid, title="repro", messages=[{"role": "user", "content": "go"}])

    for _ in range(20):
        _append_journaled_partial_output(s, stream_id, dedupe_existing=True)

    anchors = [
        m for m in s.messages
        if isinstance(m, dict)
        and m.get("_recovered_from_run_journal")
        and m.get("role") == "assistant"
        and not str(m.get("content") or "").strip()
    ]
    assert len(anchors) == 1, (
        f"repeated recovery for one stream must reuse a single empty anchor, "
        f"got {len(anchors)} (the unbounded-accumulation bug)"
    )


def test_distinct_streams_get_distinct_anchors(hermes_home):
    """Dedup is scoped per stream — two genuinely different interrupted streams
    each keep their own anchor (we are not collapsing unrelated turns)."""
    sid = "issue3875_multi"
    s = Session(session_id=sid, title="repro", messages=[{"role": "user", "content": "go"}])
    for stream_id in ("stream-X", "stream-Y", "stream-Z"):
        _tool_first_journal(sid, stream_id)
        # run each twice to prove per-stream reuse on top of per-stream distinctness
        _append_journaled_partial_output(s, stream_id, dedupe_existing=True)
        _append_journaled_partial_output(s, stream_id, dedupe_existing=True)

    anchors = [
        m for m in s.messages
        if isinstance(m, dict)
        and m.get("_recovered_from_run_journal")
        and m.get("role") == "assistant"
        and not str(m.get("content") or "").strip()
    ]
    stream_ids = {m.get("_recovered_stream_id") for m in anchors}
    assert len(anchors) == 3, f"expected one anchor per distinct stream, got {len(anchors)}"
    assert stream_ids == {"stream-X", "stream-Y", "stream-Z"}


def test_text_bearing_recovery_still_appends_real_content(hermes_home):
    """The dedup guard must not suppress recovery of genuine visible text — a
    stream that emitted tokens still produces a content-bearing recovered row."""
    sid = "issue3875_text"
    stream_id = "stream-T"
    append_run_event(sid, stream_id, "token", {"text": "Hello "})
    append_run_event(sid, stream_id, "token", {"text": "world"})
    append_run_event(sid, stream_id, "done", {})
    s = Session(session_id=sid, title="repro", messages=[{"role": "user", "content": "go"}])

    assert _append_journaled_partial_output(s, stream_id, dedupe_existing=True) is True
    recovered_text = [
        m for m in s.messages
        if isinstance(m, dict)
        and m.get("_recovered_from_run_journal")
        and str(m.get("content") or "").strip()
    ]
    assert any("Hello world" in m["content"] for m in recovered_text), (
        "token-bearing recovery must still append the visible assistant text"
    )
