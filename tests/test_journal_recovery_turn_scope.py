"""Regression tests for the #7388 review: dedupe must be turn-scoped.

A current turn that legitimately repeats an earlier turn's answer or tool
call must keep its rows — only same-turn (same-stream or pending-checkpoint-
window) rows may collapse. Composed through the real repair path
(_apply_core_sync_or_error_marker), not just the recovery helper.
"""
import json

import pytest

import api.config as config
import api.models as models
from api.models import Session


STREAM_ID = "turnscopestream00000000000000000"

BASE_ENV = {
    "session_id": "turnscope",
    "title": "turn-scope",
    "active_stream_id": None,
    "pending_user_message": None,
    "pending_started_at": None,
}


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    (session_dir / "_run_journal" / "turnscope").mkdir(parents=True)
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


def _write_journal(answer_text, tool_name="terminal", tool_cmd="ls"):
    lines = []
    seq = 0

    def ev(name, payload):
        nonlocal seq
        seq += 1
        lines.append(json.dumps({
            "event": name, "seq": seq, "session_id": "turnscope",
            "run_id": STREAM_ID, "created_at": 1788146009.0 + seq,
            "payload": payload,
        }))

    ev("token", {"text": answer_text})
    ev("tool", {"name": tool_name, "args": {"cmd": tool_cmd}})
    ev("tool_complete", {"name": tool_name, "result": "done"})
    path = models.SESSION_DIR / "_run_journal" / "turnscope" / f"{STREAM_ID}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _session_with_earlier_turn(answer_text, tool_cmd="ls"):
    """A completed earlier turn: user -> assistant (same answer) with an
    untagged tool card (same name+preview) attached to it, then a pending
    user turn whose stream died and left the recovery marker."""
    s = Session(session_id="turnscope", title="turn-scope")
    s.messages = [
        {"role": "user", "content": "do the thing", "timestamp": 1788145900},
        {"role": "assistant", "content": answer_text, "timestamp": 1788145910},
    ]
    s.tool_calls = [{
        "name": "terminal",
        "preview": "ls",
        "snippet": "ls",
        "tid": "live-1",
        "assistant_msg_idx": 1,
        "args": {"cmd": tool_cmd},
        "done": True,
    }]
    s.pending_user_message = "do the thing again"
    s.pending_started_at = 1788146000
    s.active_stream_id = STREAM_ID
    s.messages.append({
        "role": "user", "content": "do the thing again", "timestamp": 1788146000,
    })
    s.messages.append({
        "role": "assistant", "type": "interrupted", "content": "interrupted",
        "timestamp": 1788146010, "_pending_journal_recovery": True,
        "_journal_retry_stream_id": STREAM_ID,
        "_journal_retry_first_seen_ts": 1788146010,
        "_journal_retry_attempts": 0,
    })
    return s



def _run_repair(session):
    core_path = models.SESSION_DIR / "turnscope.json"
    # No core transcript exists in this scenario: the repair takes the
    # pending-recovery branch (messages non-empty) directly.
    models._apply_core_sync_or_error_marker(
        session, core_path, STREAM_ID,
        require_stream_dead=False, touch_updated_at=False,
    )

def test_repeated_answer_across_turns_is_kept(tmp_path):
    """The current turn's answer equals an earlier turn's answer word for
    word. The current row must survive — the maintenance reviewer's first
    required case."""
    answer = "The listing shows three files: README, main.py, setup.cfg."
    _write_journal(answer)
    s = _session_with_earlier_turn(answer)
    s.save(touch_updated_at=False)

    _run_repair(s)

    answers = [
        m for m in s.messages
        if m.get("role") == "assistant" and m.get("content") == answer
    ]
    assert len(answers) == 2, (
        f"current turn's repeated answer was suppressed "
        f"({len(answers)} rows, expected 2)"
    )


def test_repeated_tool_across_turns_is_kept(tmp_path):
    """Same name+preview tool as an earlier untagged turn — the current
    tool card must survive."""
    answer = "Second turn output, distinct from the first turn's prose."
    _write_journal(answer, tool_name="terminal", tool_cmd="ls")
    s = _session_with_earlier_turn(
        "First turn output, entirely different prose entirely.",
    )
    s.save(touch_updated_at=False)

    _run_repair(s)

    current_tools = [
        t for t in (s.tool_calls or [])
        if t.get("tid") != "live-1"
    ]
    assert current_tools, "current turn's repeated tool card was suppressed"


def test_same_stream_repeat_still_dedupes(tmp_path):
    """The original 1.65GB bug must stay fixed: recovering the SAME stream
    twice must not grow the transcript."""
    answer = "Deterministic crash-window answer used twice via re-recovery."
    _write_journal(answer)

    s = Session(session_id="turnscope", title="turn-scope")
    s.messages = [
        {"role": "user", "content": "ask", "timestamp": 1788146000},
    ]
    s.pending_user_message = "ask"
    s.pending_started_at = 1788146000
    s.active_stream_id = STREAM_ID
    s.messages.append({
        "role": "assistant", "type": "interrupted", "content": "interrupted",
        "timestamp": 1788146010, "_pending_journal_recovery": True,
        "_journal_retry_stream_id": STREAM_ID,
        "_journal_retry_first_seen_ts": 1788146010,
        "_journal_retry_attempts": 0,
    })

    _run_repair(s)
    after_first = len(s.messages)

    for _ in range(4):
        models._recover_journaled_output_and_terminal_error(
            s, STREAM_ID, dedupe_existing=True,
        )
    assert len(s.messages) == after_first, (
        f"same-stream re-recovery grew rows: {after_first} -> {len(s.messages)}"
    )
