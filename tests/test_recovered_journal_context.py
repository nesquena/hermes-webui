"""Regression: run-journal recovery must feed the next model turn.

A WebUI restart can interrupt an in-flight turn after visible assistant progress
has already streamed to the browser and run journal. Recovery restores that text
to the visible transcript (`session.messages`). It must also restore it to the
model-facing history (`session.context_messages`), otherwise the next user turn
sees a stale pre-restart context and the agent "forgets" the recovered work.
"""
from __future__ import annotations

import pytest

import api.profiles as profiles
from api.models import (
    Session,
    _append_journaled_partial_output,
    _append_recovered_pending_turn,
)
from api.run_journal import append_run_event
from api.streaming import _context_messages_for_new_turn


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    return home


def test_recovered_journal_text_is_in_next_turn_context(hermes_home):
    sid = "recovered_context_repro"
    stream_id = "stream-upgrade"
    append_run_event(
        sid,
        stream_id,
        "interim_assistant",
        {"text": "升级代码层面已经完成并通过关键校验：现在本地是 v0.51.554-1-gd9bd39c0。"},
    )
    append_run_event(
        sid,
        stream_id,
        "interim_assistant",
        {"text": "重启条件满足：现在触发延迟重启脚本。"},
    )

    session = Session(
        session_id=sid,
        title="repro",
        messages=[
            {"role": "user", "content": "查一下上游有没有修复"},
            {"role": "assistant", "content": "上游已合并 v0.51.554，但本地还未升级。"},
        ],
        context_messages=[
            {"role": "user", "content": "查一下上游有没有修复"},
            {"role": "assistant", "content": "上游已合并 v0.51.554，但本地还未升级。"},
        ],
        pending_user_message="帮我升级",
    )

    _append_recovered_pending_turn(session, timestamp=123)
    assert _append_journaled_partial_output(session, stream_id, dedupe_existing=True) is True

    visible_text = "\n".join(m.get("content", "") for m in session.messages)
    assert "v0.51.554-1-gd9bd39c0" in visible_text

    next_context = _context_messages_for_new_turn(session, "升级完成了吗？")
    context_text = "\n".join(m.get("content", "") for m in next_context)

    assert "帮我升级" in context_text
    assert "v0.51.554-1-gd9bd39c0" in context_text
    assert "重启条件满足" in context_text


def test_deduped_existing_recovered_assistant_repairs_missing_context(hermes_home):
    """If visible recovery already happened but context was missing, rerunning
    recovery with dedupe_existing=True should backfill context instead of
    deciding the existing visible row is enough.
    """
    sid = "recovered_context_dedupe"
    stream_id = "stream-upgrade"
    append_run_event(
        sid,
        stream_id,
        "token",
        {"text": "升级代码层面已经完成并通过关键校验。"},
    )

    recovered_assistant = {
        "role": "assistant",
        "content": "升级代码层面已经完成并通过关键校验。",
        "_recovered_from_run_journal": True,
        "_recovered_stream_id": stream_id,
    }
    session = Session(
        session_id=sid,
        title="repro",
        messages=[
            {"role": "user", "content": "帮我升级", "_recovered": True},
            recovered_assistant,
        ],
        context_messages=[
            {"role": "user", "content": "帮我升级", "_recovered": True},
        ],
    )

    assert _append_journaled_partial_output(session, stream_id, dedupe_existing=True) is False

    next_context = _context_messages_for_new_turn(session, "升级完成了吗？")
    context_text = "\n".join(m.get("content", "") for m in next_context)
    assert "升级代码层面已经完成" in context_text
