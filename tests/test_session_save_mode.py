"""Regression tests for config-driven first-turn session persistence (#1406)."""
import json

import pytest

import api.config as config
import api.models as models
import api.routes as routes
import api.streaming as streaming
from api.models import Session, new_session


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()
    monkeypatch.setattr(config, "cfg", {})
    monkeypatch.setattr(config, "_cfg_cache", {})
    yield session_dir
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()


def test_session_save_mode_defaults_to_deferred_for_missing_config():
    assert config.get_webui_session_save_mode({}) == "deferred"
    assert config.get_webui_session_save_mode({"webui": {}}) == "deferred"


@pytest.mark.parametrize("raw", ["bogus", "", None, 42, {"mode": "eager"}])
def test_invalid_session_save_mode_falls_back_to_deferred(raw):
    assert config.get_webui_session_save_mode({"webui": {"session_save_mode": raw}}) == "deferred"


def test_eager_session_save_mode_is_accepted():
    assert config.get_webui_session_save_mode({"webui": {"session_save_mode": "eager"}}) == "eager"


def test_eager_mode_still_does_not_save_empty_new_sessions(_isolate_state, monkeypatch):
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "eager"}})
    s = new_session()
    assert not s.path.exists(), "eager mode must not recreate empty Untitled session files"


def test_deferred_chat_start_persists_pending_only_before_thread(_isolate_state, monkeypatch):
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "deferred"}})
    s = new_session(workspace=str(_isolate_state.parent))
    routes._prepare_chat_start_session_for_stream(
        s,
        msg="hello deferred",
        attachments=[],
        workspace=str(_isolate_state.parent),
        model=s.model,
        model_provider=s.model_provider,
        stream_id="stream_deferred",
        started_at=123.0,
    )
    on_disk = json.loads(s.path.read_text(encoding="utf-8"))
    assert on_disk["messages"] == []
    assert on_disk["pending_user_message"] == "hello deferred"


def test_deferred_chat_start_preserves_longer_persisted_display_transcript(_isolate_state, monkeypatch):
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "deferred"}})
    long_messages = [
        {"role": "user", "content": "before compression"},
        {"role": "assistant", "content": "long answer"},
        {"role": "user", "content": "follow up"},
    ]
    persisted = Session(
        session_id="compressed_child",
        workspace=str(_isolate_state.parent),
        messages=long_messages,
        compression_anchor_visible_idx=1,
        compression_anchor_message_key={"role": "assistant", "ts": None, "text": "long answer", "attachments": 0},
        compression_anchor_summary="Context compacted",
    )
    persisted.save()
    short_context = Session(
        session_id=persisted.session_id,
        workspace=str(_isolate_state.parent),
        messages=[{"role": "assistant", "content": "compressed summary"}],
    )

    routes._prepare_chat_start_session_for_stream(
        short_context,
        msg="next turn",
        attachments=[],
        workspace=str(_isolate_state.parent),
        model=short_context.model,
        model_provider=short_context.model_provider,
        stream_id="stream_after_compression",
        started_at=321.0,
    )

    on_disk = json.loads(short_context.path.read_text(encoding="utf-8"))
    assert on_disk["messages"] == long_messages
    assert on_disk["pending_user_message"] == "next turn"
    assert on_disk["active_stream_id"] == "stream_after_compression"
    assert on_disk["compression_anchor_visible_idx"] == 1
    assert on_disk["compression_anchor_message_key"]["text"] == "long answer"


def test_eager_chat_start_checkpoints_first_user_message_before_thread(_isolate_state, monkeypatch):
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "eager"}})
    s = new_session(workspace=str(_isolate_state.parent))
    routes._prepare_chat_start_session_for_stream(
        s,
        msg="hello eager",
        attachments=[{"name": "note.txt", "path": "", "mime": "text/plain"}],
        workspace=str(_isolate_state.parent),
        model=s.model,
        model_provider=s.model_provider,
        stream_id="stream_eager",
        started_at=456.0,
    )
    on_disk = json.loads(s.path.read_text(encoding="utf-8"))
    assert [m["role"] for m in on_disk["messages"]] == ["user"]
    assert on_disk["messages"][0]["content"] == "hello eager"
    assert on_disk["messages"][0]["attachments"][0]["name"] == "note.txt"
    assert on_disk["pending_user_message"] == "hello eager"


def test_eager_chat_start_appends_checkpoint_to_preserved_display_transcript(_isolate_state, monkeypatch):
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "eager"}})
    long_messages = [
        {"role": "user", "content": "older question"},
        {"role": "assistant", "content": "older answer"},
    ]
    persisted = Session(
        session_id="eager_compressed_child",
        workspace=str(_isolate_state.parent),
        messages=long_messages,
    )
    persisted.save()
    short_context = Session(
        session_id=persisted.session_id,
        workspace=str(_isolate_state.parent),
        messages=[{"role": "assistant", "content": "summary"}],
    )

    routes._prepare_chat_start_session_for_stream(
        short_context,
        msg="new eager turn",
        attachments=[],
        workspace=str(_isolate_state.parent),
        model=short_context.model,
        model_provider=short_context.model_provider,
        stream_id="stream_eager_after_compression",
        started_at=654.0,
    )

    on_disk = json.loads(short_context.path.read_text(encoding="utf-8"))
    assert on_disk["messages"] == long_messages + [
        {"role": "user", "content": "new eager turn", "timestamp": 654}
    ]
    assert on_disk["pending_user_message"] == "new eager turn"


def test_eager_wal_repair_does_not_duplicate_checkpointed_user_message(_isolate_state, monkeypatch):
    s = Session(session_id="eager_repair", messages=[{"role": "user", "content": "survive"}])
    s.pending_user_message = "survive"
    s.active_stream_id = "dead_stream"
    s.pending_started_at = 789.0
    s.save()

    repaired = models._repair_stale_pending(s)

    assert repaired is True
    user_messages = [m for m in s.messages if m.get("role") == "user" and m.get("content") == "survive"]
    assert len(user_messages) == 1
    assert s.pending_user_message is None
    assert any(m.get("_error") for m in s.messages if m.get("role") == "assistant")


def test_eager_checkpointed_user_is_removed_from_model_context():
    context = streaming._drop_checkpointed_current_user_from_context(
        [
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "prior"},
            {"role": "user", "content": "current"},
        ],
        "current",
    )
    assert [m["content"] for m in context] == ["older", "prior"]


def test_eager_checkpointed_user_is_not_duplicated_after_agent_result():
    merged = streaming._merge_display_messages_after_agent_result(
        previous_display=[{"role": "user", "content": "repeat me"}],
        previous_context=[],
        result_messages=[
            {"role": "user", "content": "repeat me"},
            {"role": "assistant", "content": "ok"},
        ],
        msg_text="repeat me",
    )
    assert [m["role"] for m in merged] == ["user", "assistant"]


def test_deferred_turn_is_materialized_when_agent_returns_assistant_only_delta():
    merged = streaming._merge_display_messages_after_agent_result(
        previous_display=[
            {"role": "user", "content": "older prompt"},
            {"role": "assistant", "content": "older answer"},
        ],
        previous_context=[
            {"role": "user", "content": "older prompt"},
            {"role": "assistant", "content": "older answer"},
        ],
        result_messages=[
            {"role": "user", "content": "older prompt"},
            {"role": "assistant", "content": "older answer"},
            {"role": "assistant", "content": "current answer"},
        ],
        msg_text="latest prompt",
    )

    assert [m["role"] for m in merged] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [m["content"] for m in merged[-2:]] == ["latest prompt", "current answer"]


def test_duplicate_assistant_delta_is_not_persisted_twice():
    """Provider/result merge replay must not duplicate the same assistant bubble."""
    previous_display = [
        {"role": "user", "content": "older prompt"},
        {"role": "assistant", "content": "older answer"},
    ]
    previous_context = list(previous_display)
    result_messages = previous_context + [
        {"role": "user", "content": "latest prompt"},
        {"role": "assistant", "content": "current answer"},
        {"role": "assistant", "content": "current answer"},
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display=previous_display,
        previous_context=previous_context,
        result_messages=result_messages,
        msg_text="latest prompt",
    )

    assert [m["role"] for m in merged] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [m["content"] for m in merged[-2:]] == ["latest prompt", "current answer"]
    assert (
        sum(
            1
            for m in merged
            if m.get("role") == "assistant" and m.get("content") == "current answer"
        )
        == 1
    )


def test_same_assistant_text_across_different_turns_is_preserved():
    previous_display = [
        {"role": "user", "content": "first prompt"},
        {"role": "assistant", "content": "same answer"},
    ]
    previous_context = list(previous_display)
    result_messages = previous_context + [
        {"role": "user", "content": "second prompt"},
        {"role": "assistant", "content": "same answer"},
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display=previous_display,
        previous_context=previous_context,
        result_messages=result_messages,
        msg_text="second prompt",
    )

    assert [m["content"] for m in merged] == [
        "first prompt",
        "same answer",
        "second prompt",
        "same answer",
    ]


def test_llm_title_generated_survives_save_and_load(_isolate_state):
    s = Session(
        session_id="generated_title",
        title="Useful generated title",
        messages=[{"role": "user", "content": "first prompt"}],
        llm_title_generated=True,
    )
    s.save()

    loaded = Session.load("generated_title")

    assert loaded.llm_title_generated is True
    on_disk = json.loads(s.path.read_text(encoding="utf-8"))
    assert on_disk["llm_title_generated"] is True


def test_session_constructor_preserves_loaded_llm_title_generated_kwarg():
    s = Session(session_id="loaded_generated_title", llm_title_generated=True)

    assert s.llm_title_generated is True
