"""Repro: run-journal recovery must be idempotent across repeated passes.

Production evidence (99ed0f78e02d, Aug 2026): a session that went through
repeated restart/recovery cycles accumulated 458,782 duplicate empty
assistant rows — each of ~28 distinct reasoning texts duplicated exactly
16,385 times (2**14 + 1), a power-of-two pattern consistent with the
recovered array being re-appended on every recovery pass.

Recovery replays the full journal from seq 1 every pass
(_append_journaled_partial_output never passes after_seq), and its dedupe
only matches rows stamped with the SAME stream id inside
range(initial_message_count) — and only when dedupe_existing=True, which
the pending-turn repair caller does not pass.

These tests assert the invariant: recovering the same journal N times must
produce the same rows as recovering it once.
"""
import json

import pytest

import api.config as config
import api.models as models
from api.models import Session


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    (session_dir / "_run_journal" / "reprosid").mkdir(parents=True)
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()
    yield session_dir
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()


STREAM_ID = "reprostream0000000000000000000000"

REASONING_TEXTS = [f"recovery reasoning block {i} " + "x" * 40 for i in range(4)]


def _write_synthetic_journal(session_dir, *, terminal=True):
    """A small but realistic dead-stream journal: interleaved reasoning
    deltas that coalesce into REASONING_TEXTS full blocks, one tool pair,
    and (optionally) no terminal event (the crash case)."""
    journal = session_dir / "_run_journal" / "reprosid" / f"{STREAM_ID}.jsonl"
    seq = 0

    def ev(name, payload):
        nonlocal seq
        seq += 1
        return json.dumps({
            "event": name, "seq": seq, "session_id": "reprosid",
            "run_id": STREAM_ID, "created_at": 1788146009.0 + seq,
            "payload": payload,
        })

    lines = []
    # reasoning arrives in deltas; each full text is built from 3 chunks
    # (boundaries at i*len//3 so the chunks concatenate to the exact text)
    for text in REASONING_TEXTS:
        L = len(text)
        for i in range(3):
            lines.append(ev("reasoning", {"text": text[i * L // 3:(i + 1) * L // 3]}))
    lines.append(ev("tool", {"name": "terminal", "args": {"cmd": "ls"}}))
    lines.append(ev("tool_complete", {"name": "terminal", "result": "ok"}))
    lines.append(ev("token", {"text": "Visible partial answer text."}))
    if terminal:
        lines.append(ev("stream_end", {}))
    journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return journal


def _fresh_session_with_pending_marker(session_dir):
    s = Session(session_id="reprosid", title="repro")
    s.messages = [
        {"role": "user", "content": "run the thing", "timestamp": 1788146000},
    ]
    s.pending_user_message = "run the thing"
    s.pending_started_at = 1788146001
    s.active_stream_id = STREAM_ID
    marker = {
        "role": "assistant", "type": "interrupted",
        "content": "interrupted", "timestamp": 1788146010,
        "_pending_journal_recovery": True,
        "_journal_retry_stream_id": STREAM_ID,
        "_journal_retry_first_seen_ts": 1788146010,
        "_journal_retry_attempts": 0,
    }
    s.messages.append(marker)
    return s


def test_recovery_is_idempotent_across_repeated_passes(tmp_path):
    session_dir = tmp_path / "sessions"
    _write_synthetic_journal(session_dir)

    s = _fresh_session_with_pending_marker(session_dir)
    s.save(touch_updated_at=False)

    # First pass: recovery legitimately appends recovered rows.
    recovered, _ = models._recover_journaled_output_and_terminal_error(
        s, STREAM_ID, dedupe_existing=True,
    )
    assert recovered, "first recovery should recover journaled output"
    after_first = len(s.messages)

    # Simulate the production pattern: the retry path runs the recovery
    # again on a session that already holds the recovered rows (this is
    # what every get_session()/restart cycle did).
    for _ in range(5):
        models._recover_journaled_output_and_terminal_error(
            s, STREAM_ID, dedupe_existing=True,
        )
    assert len(s.messages) == after_first, (
        f"recovery re-appended rows: {after_first} -> {len(s.messages)}"
    )


def test_recovery_without_dedupe_flag_is_still_bounded(tmp_path):
    """The pending-turn repair caller (models.py ~3447) passes no
    dedupe_existing flag. Repeated repair passes must not grow the
    transcript either."""
    session_dir = tmp_path / "sessions"
    _write_synthetic_journal(session_dir)

    s = _fresh_session_with_pending_marker(session_dir)
    s.save(touch_updated_at=False)

    models._recover_journaled_output_and_terminal_error(s, STREAM_ID)
    after_first = len(s.messages)
    for _ in range(5):
        models._recover_journaled_output_and_terminal_error(s, STREAM_ID)
    assert len(s.messages) == after_first, (
        f"no-flag recovery re-appended rows: {after_first} -> {len(s.messages)}"
    )


def test_reasoning_blocks_recovered_exactly_once(tmp_path):
    """Content invariant: each distinct reasoning text occurs exactly once
    across the transcript after repeated recoveries (recovery may coalesce
    consecutive reasoning events into one row — that is fine; duplication
    is not)."""
    session_dir = tmp_path / "sessions"
    _write_synthetic_journal(session_dir)

    s = _fresh_session_with_pending_marker(session_dir)
    for _ in range(4):
        models._recover_journaled_output_and_terminal_error(
            s, STREAM_ID, dedupe_existing=True,
        )

    def norm(t):
        return "".join(t.split())

    all_reasoning = norm(" ".join(
        str(m.get("reasoning") or "")
        for m in s.messages
        if isinstance(m, dict) and m.get("role") == "assistant"
    ))
    for text in REASONING_TEXTS:
        n = norm(text)
        occurrences = all_reasoning.count(n)
        assert occurrences == 1, (
            f"reasoning block appeared {occurrences}x across transcript, expected 1"
        )
