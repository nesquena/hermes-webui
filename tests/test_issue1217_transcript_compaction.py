import contextlib
import io
import json
import sys
from types import SimpleNamespace

from api.models import Session, reconciled_state_db_messages_for_session, state_db_delta_after_context

from api.streaming import (
    _assistant_reply_added_after_current_turn,
    _context_messages_for_new_turn,
    _dedupe_replayed_active_context,
    _dedupe_replayed_context_messages,
    _merge_display_messages_after_agent_result,
    _new_turn_context_from_messages,
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

    assert [m["role"] for m in merged] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in merged[-2:]] == [
        "Ok, mache weiter",
        "continuing",
    ]
    assert all("CONTEXT COMPACTION" not in str(m.get("content") or "") for m in merged)
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
        "new question after compaction",
        "new answer after compaction",
    ]
    assert all("CONTEXT COMPACTION" not in str(m.get("content") or "") for m in merged)


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


def test_replayed_active_tail_after_compression_is_not_duplicated():
    previous_display = [
        {"role": "assistant", "content": "old visible transcript"},
        {"role": "user", "content": "choose agent"},
        {"role": "assistant", "content": "checking agents"},
        {"role": "tool", "content": "agent list"},
        {"role": "assistant", "content": "agent answer"},
    ]
    previous_context = [
        {"role": "assistant", "content": "[Session Arc Summary] compacted history"},
        {"role": "user", "content": "choose agent"},
        {"role": "assistant", "content": "checking agents"},
        {"role": "tool", "content": "agent list"},
        {"role": "assistant", "content": "agent answer"},
    ]
    result_messages = previous_context + [
        # The new compressed state.db segment can replay the already-visible
        # active tail before adding the next turn.
        {"role": "user", "content": "choose agent"},
        {"role": "assistant", "content": "checking agents"},
        {"role": "tool", "content": "agent list"},
        {"role": "assistant", "content": "agent answer"},
        {"role": "user", "content": "choose B"},
        {"role": "assistant", "content": "using B"},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "choose B",
    )
    next_context = _dedupe_replayed_active_context(previous_context, result_messages)

    assert [m["content"] for m in merged] == [
        "old visible transcript",
        "choose agent",
        "checking agents",
        "agent list",
        "agent answer",
        "choose B",
        "using B",
    ]
    assert [m["content"] for m in next_context] == [
        "[Session Arc Summary] compacted history",
        "choose agent",
        "checking agents",
        "agent list",
        "agent answer",
        "choose B",
        "using B",
    ]


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
        "continue",
        "new answer",
    ]
    assert all("CONTEXT COMPACTION" not in str(m.get("content") or "") for m in merged)


def test_near_duplicate_session_arc_summary_is_not_replayed_in_context():
    summary_a = {
        "role": "user",
        "content": "[Session Arc Summary (d1, node 39)]\n"
        + "same recovered LCM context\n" * 260
        + "expand hint: old node",
    }
    summary_b = {
        "role": "user",
        "content": "[Session Arc Summary (d1, node 39)]\n"
        + "same recovered LCM context\n" * 260
        + "expand hint: refreshed node",
    }
    previous_context = [summary_a, {"role": "user", "content": "choose agent"}]
    result_messages = previous_context + [
        {"role": "assistant", "content": "agent answer"},
        summary_b,
        {"role": "user", "content": "next question"},
    ]

    next_context = _dedupe_replayed_context_messages(previous_context, result_messages)

    assert [m["content"] for m in next_context] == [
        summary_a["content"],
        "choose agent",
        "agent answer",
        "next question",
    ]


def test_non_adjacent_replayed_context_block_is_not_appended_again():
    previous_context = [
        {"role": "user", "content": "older setup"},
        {"role": "user", "content": "choose agent"},
        {"role": "assistant", "content": "checking agents"},
        {"role": "tool", "content": "agent list"},
        {"role": "assistant", "content": "agent answer"},
    ]
    result_messages = previous_context + [
        {"role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] compacted"},
        {"role": "user", "content": "choose agent"},
        {"role": "assistant", "content": "checking agents"},
        {"role": "tool", "content": "agent list"},
        {"role": "assistant", "content": "agent answer"},
        {"role": "user", "content": "next question"},
    ]

    next_context = _dedupe_replayed_context_messages(previous_context, result_messages)

    assert [m["content"] for m in next_context] == [
        "older setup",
        "choose agent",
        "checking agents",
        "agent list",
        "agent answer",
        "[CONTEXT COMPACTION — REFERENCE ONLY] compacted",
        "next question",
    ]




def test_prefer_context_reconcile_strips_state_db_mirrored_prefix():
    sidecar_context = [
        {"role": "assistant", "content": "cron banner"},
        {"role": "user", "content": "[Session Arc Summary]"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "latest saved question", "timestamp": 200.0},
        {"role": "assistant", "content": "latest saved answer", "timestamp": 201.0},
    ]
    state_messages = [
        {"role": "assistant", "content": "cron banner", "timestamp": 100.0},
        {"role": "user", "content": "[Session Arc Summary]", "timestamp": 101.0},
        {"role": "assistant", "content": "old answer", "timestamp": 102.0},
        {"role": "user", "content": "latest saved question", "timestamp": 200.0},
        {"role": "assistant", "content": "latest saved answer", "timestamp": 201.0},
        {"role": "user", "content": "continue", "timestamp": 300.0},
    ]
    session = SimpleNamespace(
        session_id="prefix-replay",
        context_messages=sidecar_context,
        messages=[],
    )

    reconciled = reconciled_state_db_messages_for_session(
        session, prefer_context=True, state_messages=state_messages
    )

    assert reconciled == sidecar_context + [
        {"role": "user", "content": "continue", "timestamp": 300.0},
    ]


def test_prefer_context_reconcile_keeps_state_delta_when_no_mirrored_prefix():
    sidecar_context = [
        {"role": "user", "content": "summary"},
        {"role": "assistant", "content": "saved answer"},
    ]
    state_messages = [
        {"role": "user", "content": "fresh question", "timestamp": 300.0},
        {"role": "assistant", "content": "fresh answer", "timestamp": 301.0},
    ]
    session = SimpleNamespace(
        session_id="fresh-delta",
        context_messages=sidecar_context,
        messages=[],
    )

    reconciled = reconciled_state_db_messages_for_session(
        session, prefer_context=True, state_messages=state_messages
    )

    assert reconciled == sidecar_context + state_messages


def test_prefer_context_reconcile_uses_compression_anchor_boundary_for_compacted_context():
    sidecar_context = [
        {"role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] compacted"},
        {"role": "user", "content": "fresh kept question"},
        {"role": "assistant", "content": "fresh kept answer"},
    ]
    state_messages = [
        {"role": "user", "content": "old question", "timestamp": 1.0},
        {"role": "assistant", "content": "old answer", "timestamp": 2.0},
        {"role": "user", "content": "fresh kept question", "timestamp": 3.0},
        {"role": "assistant", "content": "fresh kept answer", "timestamp": 4.0},
        {"role": "user", "content": "new after compress", "timestamp": 5.0},
    ]
    session = SimpleNamespace(
        session_id="anchor-boundary",
        context_messages=sidecar_context,
        messages=[],
        compression_anchor_message_key={
            "role": "assistant",
            "text": "fresh kept answer",
            "ts": 4.0,
            "attachments": 0,
        },
    )

    reconciled = reconciled_state_db_messages_for_session(
        session, prefer_context=True, state_messages=state_messages
    )

    assert reconciled == sidecar_context + [
        {"role": "user", "content": "new after compress", "timestamp": 5.0},
    ]


def test_prefer_context_reconcile_fails_closed_when_compression_anchor_missing():
    sidecar_context = [
        {"role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] compacted"},
        {"role": "user", "content": "fresh kept question"},
        {"role": "assistant", "content": "fresh kept answer"},
    ]
    state_messages = [
        {"role": "user", "content": "old question", "timestamp": 1.0},
        {"role": "assistant", "content": "old answer", "timestamp": 2.0},
        {"role": "user", "content": "fresh kept question", "timestamp": 3.0},
        {"role": "assistant", "content": "fresh kept answer", "timestamp": 4.0},
        {"role": "user", "content": "new after compress", "timestamp": 5.0},
    ]
    session = SimpleNamespace(
        session_id="anchor-missing",
        context_messages=sidecar_context,
        messages=[],
        compression_anchor_message_key={
            "role": "assistant",
            "text": "missing boundary",
            "ts": 999.0,
            "attachments": 0,
        },
    )

    reconciled = reconciled_state_db_messages_for_session(
        session, prefer_context=True, state_messages=state_messages
    )

    assert reconciled == sidecar_context


def test_prefer_context_reconcile_fails_closed_when_compressed_context_has_no_anchor_key():
    sidecar_context = [
        {"role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] compacted"},
        {"role": "user", "content": "new question"},
    ]
    state_messages = [
        {"role": "user", "content": "old question", "timestamp": 1.0},
        {"role": "assistant", "content": "old answer", "timestamp": 2.0},
        {"role": "user", "content": "new question", "timestamp": 3.0},
    ]
    session = SimpleNamespace(
        session_id="compressed-anchor-none",
        context_messages=sidecar_context,
        messages=[],
        compression_anchor_message_key=None,
    )

    reconciled = reconciled_state_db_messages_for_session(
        session, prefer_context=True, state_messages=state_messages
    )

    assert reconciled == sidecar_context


def test_prefer_context_reconcile_accepts_iso_compression_anchor_timestamp():
    sidecar_context = [
        {"role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] compacted"},
        {"role": "user", "content": "fresh kept question"},
        {"role": "assistant", "content": "fresh kept answer"},
    ]
    state_messages = [
        {"role": "user", "content": "old question", "timestamp": "2026-06-22T09:00:00Z"},
        {"role": "assistant", "content": "old answer", "timestamp": "2026-06-22T09:01:00Z"},
        {"role": "user", "content": "fresh kept question", "timestamp": "2026-06-22T09:02:00Z"},
        {"role": "assistant", "content": "fresh kept answer", "timestamp": "2026-06-22T09:03:00Z"},
        {"role": "user", "content": "new after compress", "timestamp": "2026-06-22T09:04:00Z"},
    ]
    session = SimpleNamespace(
        session_id="iso-anchor-boundary",
        context_messages=sidecar_context,
        messages=[],
        compression_anchor_message_key={
            "role": "assistant",
            "text": "fresh kept answer",
            "ts": "2026-06-22T09:03:00Z",
            "attachments": 0,
        },
    )

    reconciled = reconciled_state_db_messages_for_session(
        session, prefer_context=True, state_messages=state_messages
    )

    assert reconciled == sidecar_context + [
        {"role": "user", "content": "new after compress", "timestamp": "2026-06-22T09:04:00Z"},
    ]


def test_prefer_context_reconcile_fails_closed_when_compression_anchor_ts_is_missing():
    sidecar_context = [
        {"role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] compacted"},
        {"role": "user", "content": "latest kept question"},
        {"role": "assistant", "content": "latest kept answer"},
    ]
    state_messages = [
        {"role": "user", "content": "original long prompt"},
        {"role": "assistant", "content": "old answer", "timestamp": 1.0},
        {"role": "user", "content": "old tool-heavy follow-up", "timestamp": 2.0},
        {"role": "assistant", "content": "old tool-heavy answer", "timestamp": 3.0},
        {"role": "user", "content": "latest kept question", "timestamp": 4.0},
        {"role": "assistant", "content": "latest kept answer", "timestamp": 5.0},
        {"role": "user", "content": "new after compression", "timestamp": 6.0},
    ]
    session = SimpleNamespace(
        session_id="anchor-ts-missing",
        context_messages=sidecar_context,
        messages=[],
        compression_anchor_message_key={
            "role": "user",
            "text": "original long prompt",
            "ts": None,
            "attachments": 0,
        },
    )

    reconciled = reconciled_state_db_messages_for_session(
        session, prefer_context=True, state_messages=state_messages
    )

    assert reconciled == sidecar_context


def test_prefer_context_reconcile_strips_small_mirrored_context_prefix():
    sidecar_context = [
        {"role": "user", "content": "[Session Arc Summary] compacted"},
        {"role": "assistant", "content": "last compacted answer"},
    ]
    state_messages = [
        {"role": "user", "content": "[Session Arc Summary] compacted", "timestamp": 100.0},
        {"role": "assistant", "content": "last compacted answer", "timestamp": 101.0},
        {"role": "user", "content": "fresh follow-up", "timestamp": 200.0},
    ]
    session = SimpleNamespace(
        session_id="small-context-prefix",
        context_messages=sidecar_context,
        messages=[],
    )

    reconciled = reconciled_state_db_messages_for_session(
        session, prefer_context=True, state_messages=state_messages
    )

    assert reconciled == sidecar_context + [
        {"role": "user", "content": "fresh follow-up", "timestamp": 200.0},
    ]


def test_prefer_context_reconcile_strips_mirrored_rows_without_sidecar_timestamps():
    sidecar_context = [
        {"role": "assistant", "content": "cron banner"},
        {"role": "user", "content": "summary"},
        {"role": "assistant", "content": "older answer"},
        {"role": "user", "content": "already saved"},
    ]
    state_messages = [
        {"role": "assistant", "content": "cron banner", "timestamp": 100.0},
        {"role": "user", "content": "summary", "timestamp": 101.0},
        {"role": "assistant", "content": "older answer", "timestamp": 102.0},
        {"role": "user", "content": "already saved", "timestamp": 500.0},
        {"role": "user", "content": "new after sidecar", "timestamp": 600.0},
    ]
    session = SimpleNamespace(
        session_id="untimestamped-context",
        context_messages=sidecar_context,
        messages=[],
    )

    reconciled = reconciled_state_db_messages_for_session(
        session, prefer_context=True, state_messages=state_messages
    )

    assert reconciled == sidecar_context + [
        {"role": "user", "content": "new after sidecar", "timestamp": 600.0},
    ]


def test_prefer_context_reconcile_starts_after_last_state_row_seen_in_context():
    sidecar_context = [
        {"role": "assistant", "content": "cron banner"},
        {"role": "user", "content": "summary"},
        {"role": "assistant", "content": "old answer"},
        {"role": "assistant", "content": "later represented row"},
    ]
    state_messages = [
        {"role": "assistant", "content": "cron banner", "timestamp": 100.0},
        {"role": "user", "content": "summary", "timestamp": 101.0},
        {"role": "assistant", "content": "old answer", "timestamp": 102.0},
        {"role": "tool", "content": "state-only stale tool", "timestamp": 103.0},
        {"role": "assistant", "content": "later represented row", "timestamp": 104.0},
        {"role": "user", "content": "new after represented boundary", "timestamp": 200.0},
    ]
    session = SimpleNamespace(
        session_id="represented-boundary",
        context_messages=sidecar_context,
        messages=[],
    )

    reconciled = reconciled_state_db_messages_for_session(
        session, prefer_context=True, state_messages=state_messages
    )

    assert reconciled == sidecar_context + [
        {"role": "user", "content": "new after represented boundary", "timestamp": 200.0},
    ]



def test_state_db_delta_preserves_fresh_rows_before_repeated_context_message():
    sidecar_context = [
        {"role": "user", "content": "ok"},
        {"role": "assistant", "content": "ready"},
    ]
    state_messages = [
        {"role": "user", "content": "ok", "timestamp": 1.0},
        {"role": "assistant", "content": "ready", "timestamp": 2.0},
        {"role": "user", "content": "fresh question", "timestamp": 3.0},
        {"role": "user", "content": "ok", "timestamp": 4.0},
        {"role": "assistant", "content": "after repeat", "timestamp": 5.0},
    ]

    delta = state_db_delta_after_context(sidecar_context, state_messages)

    assert [m["content"] for m in delta] == ["fresh question", "ok", "after repeat"]


def test_non_streaming_chat_writeback_dedupes_full_context_replay():
    previous_context = [
        {"role": "assistant", "content": "cron banner"},
        {"role": "user", "content": "[Session Arc Summary (d1, node 39)]\n" + "old context\n" * 400},
        {"role": "assistant", "content": "previous answer"},
    ]
    result_messages = previous_context + previous_context + [
        {"role": "user", "content": "simple follow-up"},
        {"role": "assistant", "content": "short answer"},
    ]

    next_context = _dedupe_replayed_context_messages(previous_context, result_messages)

    assert next_context == previous_context + [
        {"role": "user", "content": "simple follow-up"},
        {"role": "assistant", "content": "short answer"},
    ]

class _FakePostHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.body = bytearray()
        self.wfile = self

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers[name] = value

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def test_handle_chat_sync_writeback_dedupes_full_context_replay(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.routes as routes

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "get_session", models.get_session)
    monkeypatch.setattr(routes, "title_from", models.title_from)
    monkeypatch.setattr(config, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
    monkeypatch.setattr(routes, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value: tmp_path)
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])

    previous_context = [
        {"role": "assistant", "content": "cron banner"},
        {"role": "user", "content": "[Session Arc Summary (d1, node 39)]\n" + "old context\n" * 400},
        {"role": "assistant", "content": "previous answer"},
    ]
    session = Session(
        session_id="sync_chat_replay",
        workspace=str(tmp_path),
        messages=list(previous_context),
        context_messages=list(previous_context),
        model="test-model",
        model_provider="test-provider",
    )
    session.save(touch_updated_at=False)

    replayed_result = previous_context + previous_context + [
        {"role": "user", "content": "simple follow-up"},
        {"role": "assistant", "content": "short answer"},
    ]

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **_kwargs):
            return {
                "messages": replayed_result,
                "final_response": "short answer",
                "completed": True,
            }

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))

    handler = _FakePostHandler()
    routes._handle_chat_sync(
        handler,
        {"session_id": session.session_id, "message": "simple follow-up", "workspace": str(tmp_path)},
    )

    assert handler.status == 200
    reloaded = Session.load(session.session_id)
    assert reloaded.context_messages == previous_context + [
        {"role": "user", "content": "simple follow-up"},
        {"role": "assistant", "content": "short answer"},
    ]
    assert reloaded.context_messages.count(previous_context[0]) == 1


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


def test_streaming_reconciled_context_keeps_casual_greeting_suppression():
    compacted_task_context = [
        {
            "role": "user",
            "content": (
                "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted. "
                "Your current task is identified in the Active Task section — resume exactly from there."
            ),
            "timestamp": 1.0,
        },
        {"role": "assistant", "content": "I will inspect api/config.py next.", "timestamp": 2.0},
    ]
    session = SimpleNamespace(
        session_id="issue2308-streaming",
        messages=[{"role": "user", "content": "old task", "timestamp": 0.5}],
        context_messages=compacted_task_context,
    )
    external_state_messages = list(compacted_task_context)

    # Mirror the streaming pre-turn assembly for prefer_context=True: reconcile
    # sidecar context with one state.db snapshot, then apply the normal new-turn
    # context filter that suppresses casual greetings from resuming stale tasks.
    previous_context_messages = _new_turn_context_from_messages(
        reconciled_state_db_messages_for_session(
            session,
            prefer_context=True,
            state_messages=external_state_messages,
        ),
        "你好",
    )

    assert previous_context_messages == []


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
