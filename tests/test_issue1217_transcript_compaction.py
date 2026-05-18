from api.models import Session
import contextlib

from api.streaming import (
    _assistant_reply_added_after_current_turn,
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


def test_embedded_workspace_prefixed_current_user_delta_is_deduped():
    """A failed provider path can echo draft text before the workspace tag."""
    current = "正常来说，chrome for testing 不是有独立的profile嘛，为什么会有 user-data-dir 冲突的问题？"
    previous_display = [
        {"role": "user", "content": "older prompt"},
        {"role": "assistant", "content": "older answer"},
        {"role": "user", "content": "正常来说，chrome"},
        {"role": "user", "content": current},
    ]
    previous_context = [
        {"role": "user", "content": "older prompt"},
        {"role": "assistant", "content": "older answer"},
    ]
    result_messages = previous_context + [
        {
            "role": "user",
            "content": (
                "正常来说，chrome\n\n"
                "[Workspace::v1: /mnt/e/vscode_workspace/hermes_workspace]\n"
                f"{current}"
            ),
        },
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        current,
    )

    assert merged == previous_display
    assert all("Workspace::v1" not in str(m.get("content") or "") for m in merged)


def test_embedded_workspace_prefixed_current_user_delta_displays_clean_prompt():
    current = "正常来说，chrome for testing 不是有独立的profile嘛，为什么会有 user-data-dir 冲突的问题？"
    previous_display = [
        {"role": "user", "content": "older prompt"},
        {"role": "assistant", "content": "older answer"},
    ]
    previous_context = list(previous_display)
    result_messages = previous_context + [
        {
            "role": "user",
            "content": (
                "正常来说，chrome\n\n"
                "[Workspace::v1: /mnt/e/vscode_workspace/hermes_workspace]\n"
                f"{current}"
            ),
        },
        {"role": "assistant", "content": "Chrome for Testing 本身没有固定独立 profile。"},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        current,
    )

    assert [m["content"] for m in merged[-2:]] == [
        current,
        "Chrome for Testing 本身没有固定独立 profile。",
    ]
    assert all("Workspace::v1" not in str(m.get("content") or "") for m in merged)


def test_assistant_added_detection_ignores_prior_history():
    previous_context = [
        {"role": "user", "content": "older prompt"},
        {"role": "assistant", "content": "older answer"},
    ]
    current = "new prompt"
    result_messages = previous_context + [
        {"role": "user", "content": f"[Workspace::v1: /tmp/project]\n{current}"},
    ]

    assert not _assistant_reply_added_after_current_turn(
        result_messages,
        previous_context,
        current,
    )
    assert _assistant_reply_added_after_current_turn(
        result_messages + [{"role": "assistant", "content": "new answer"}],
        previous_context,
        current,
    )


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


def test_all_cjk_greetings_drop_stale_compaction_context(tmp_path):
    """Pin every CJK greeting in the casual-fresh-chat set against a stale
    compaction context. Catches typos like \\u5616 (嘖, "click of tongue")
    or \\u5582 (喂, "hey on phone") slipping into the greeting set where
    \\u55c9 (嗨, "hai") and \\u55bd (喽, "luo") were intended."""
    compacted_task_context = [
        {
            "role": "user",
            "content": (
                "[CONTEXT COMPACTION — REFERENCE ONLY] active task: X — resume exactly. in_progress."
            ),
        },
        {"role": "assistant", "content": "I will continue task X"},
    ]
    session = Session(
        session_id="issue2308-cjk-all",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "old task"},
            {"role": "assistant", "content": "old answer"},
        ],
        context_messages=compacted_task_context,
    )

    # Every CJK greeting in _is_casual_fresh_chat_message must drop the stale context.
    # If a typo lands a wrong codepoint here, the user's greeting won't be recognized
    # and the stale "resume active task" prompt will silently leak back through.
    for greeting in ("你好", "您好", "嗨", "哈喽", "在吗", "在么"):
        assert _context_messages_for_new_turn(session, greeting) == [], (
            f"CJK greeting {greeting!r} (U+{ord(greeting[0]):04X}"
            f"{'+'+'U+%04X' % ord(greeting[1]) if len(greeting) > 1 else ''}) "
            f"was not recognized as a casual fresh chat — stale compaction context leaked"
        )


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
