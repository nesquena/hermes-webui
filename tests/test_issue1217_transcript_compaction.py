from api.models import Session
import contextlib

from api.streaming import (
    _context_messages_for_new_turn,
    _merge_display_messages_after_agent_result,
    _sanitize_messages_for_api,
    _session_context_messages,
)


def test_session_persists_model_context_separately_from_display_transcript(tmp_path, monkeypatch):
    """Compacted model context must not replace the visible WebUI transcript."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)

    import api.models as models

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")

    original_display = [
        {"role": "user", "content": "original long prompt"},
        {"role": "assistant", "content": "original detailed answer"},
    ]
    compacted_context = [
        {
            "role": "user",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted.",
        },
        {"role": "user", "content": "continue from here"},
        {"role": "assistant", "content": "continued response"},
    ]

    session = Session(
        session_id="issue1217",
        workspace=str(tmp_path),
        messages=original_display,
        context_messages=compacted_context,
    )
    session.save(touch_updated_at=False)

    reloaded = Session.load("issue1217")
    assert reloaded.messages == original_display
    assert reloaded.context_messages == compacted_context
    assert _session_context_messages(reloaded) == compacted_context
    assert _sanitize_messages_for_api(_session_context_messages(reloaded)) == compacted_context


def test_workspace_prefixed_current_user_after_compaction_is_not_duplicated():
    previous_display = [
        {"role": "user", "content": "older prompt"},
        {"role": "assistant", "content": "older answer"},
    ]
    previous_context = list(previous_display)
    compacted_result = [
        {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted.",
        },
        {"role": "user", "content": "[Workspace: /home/manfred/.hermes/workspace]\nOk, mache weiter"},
        {"role": "assistant", "content": "continuing"},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        compacted_result,
        "Ok, mache weiter",
    )

    assert [m["role"] for m in merged] == ["user", "assistant", "assistant", "user", "assistant"]
    assert [m["content"] for m in merged[-2:]] == [
        "Ok, mache weiter",
        "continuing",
    ]
    assert sum(1 for m in merged if m.get("role") == "user" and "Ok, mache weiter" in m.get("content", "")) == 1


def test_compacted_agent_result_keeps_old_prompts_and_appends_current_turn():
    previous_display = [
        {"role": "user", "content": "first prompt that must remain visible"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second prompt that must remain visible"},
        {"role": "assistant", "content": "second answer"},
    ]
    previous_context = list(previous_display)
    compacted_result = [
        {
            "role": "user",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted.",
        },
        {"role": "user", "content": "new question after compaction"},
        {"role": "assistant", "content": "new answer after compaction"},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        compacted_result,
        "new question after compaction",
    )

    assert [m["content"] for m in merged] == [
        "first prompt that must remain visible",
        "first answer",
        "second prompt that must remain visible",
        "second answer",
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted.",
        "new question after compaction",
        "new answer after compaction",
    ]


def test_append_only_agent_result_preserves_normal_delta_behavior():
    previous_display = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    previous_context = list(previous_display)
    result_messages = previous_context + [
        {"role": "user", "content": "what next?"},
        {"role": "assistant", "content": "next answer"},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "what next?",
    )

    assert merged == result_messages


def test_repeated_user_text_after_compaction_is_not_dropped():
    previous_display = [
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "old answer"},
    ]
    previous_context = list(previous_display)
    compacted_result = [
        {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary"},
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "new answer"},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        compacted_result,
        "continue",
    )

    assert [m["content"] for m in merged] == [
        "continue",
        "old answer",
        "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
        "continue",
        "new answer",
    ]


def test_session_context_falls_back_to_display_messages_for_legacy_sessions(tmp_path):
    messages = [
        {"role": "user", "content": "legacy prompt"},
        {"role": "assistant", "content": "legacy answer"},
    ]
    session = Session(session_id="legacy1217", workspace=str(tmp_path), messages=messages)

    assert session.context_messages == []
    assert _session_context_messages(session) == messages


def test_casual_greeting_does_not_resume_stale_compaction_active_task(tmp_path):
    compacted_task_context = [
        {
            "role": "user",
            "content": (
                "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted. "
                "Your current task is identified in the Active Task section — resume exactly from there. "
                "[Your active task list was preserved across context compression] "
                "- [>] 5. 更新测试：mock bridge 输出 (in_progress)"
            ),
        },
        {"role": "assistant", "content": "I will inspect api/config.py next."},
    ]
    session = Session(
        session_id="issue2308",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "old provider/model task"},
            {"role": "assistant", "content": "old task answer"},
        ],
        context_messages=compacted_task_context,
    )

    assert _context_messages_for_new_turn(session, "你好") == []


def test_explicit_continue_keeps_compacted_active_task_context(tmp_path):
    compacted_task_context = [
        {
            "role": "user",
            "content": (
                "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted. "
                "Your current task is identified in the Active Task section — resume exactly from there."
            ),
        },
        {"role": "assistant", "content": "I will inspect api/config.py next."},
    ]
    session = Session(
        session_id="issue2308-continue",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "old provider/model task"},
            {"role": "assistant", "content": "old task answer"},
        ],
        context_messages=compacted_task_context,
    )

    assert _context_messages_for_new_turn(session, "继续") == compacted_task_context


def test_retry_truncates_model_context_when_it_is_separate(monkeypatch, tmp_path):
    import api.session_ops as session_ops

    session = Session(
        session_id="retry1217",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "visible one"},
            {"role": "assistant", "content": "visible two"},
            {"role": "user", "content": "visible three"},
            {"role": "assistant", "content": "visible four"},
        ],
        context_messages=[
            {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary"},
            {"role": "user", "content": "visible three"},
            {"role": "assistant", "content": "visible four"},
        ],
    )
    saved = []
    session.save = lambda *args, **kwargs: saved.append(True)
    monkeypatch.setattr(session_ops, "get_session", lambda sid: session)
    monkeypatch.setattr(session_ops, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(session_ops, "_get_session_agent_lock", lambda sid: contextlib.nullcontext())

    result = session_ops.retry_last(session.session_id)

    assert result["last_user_text"] == "visible three"
    assert [m["content"] for m in session.messages] == ["visible one", "visible two"]
    assert [m["content"] for m in session.context_messages] == [
        "[CONTEXT COMPACTION — REFERENCE ONLY] summary"
    ]
    assert saved


def test_undo_truncates_model_context_when_it_is_separate(monkeypatch, tmp_path):
    import api.session_ops as session_ops

    session = Session(
        session_id="undo1217",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "visible one"},
            {"role": "assistant", "content": "visible two"},
            {"role": "user", "content": "visible three"},
            {"role": "assistant", "content": "visible four"},
        ],
        context_messages=[
            {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary"},
            {"role": "user", "content": "visible three"},
            {"role": "assistant", "content": "visible four"},
        ],
    )
    saved = []
    session.save = lambda *args, **kwargs: saved.append(True)
    monkeypatch.setattr(session_ops, "get_session", lambda sid: session)
    monkeypatch.setattr(session_ops, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(session_ops, "_get_session_agent_lock", lambda sid: contextlib.nullcontext())

    result = session_ops.undo_last(session.session_id)

    assert result["removed_count"] == 2
    assert [m["content"] for m in session.messages] == ["visible one", "visible two"]
    assert [m["content"] for m in session.context_messages] == [
        "[CONTEXT COMPACTION — REFERENCE ONLY] summary"
    ]
    assert saved
