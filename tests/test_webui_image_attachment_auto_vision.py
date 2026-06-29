import json
import queue
from collections import OrderedDict
from pathlib import Path

import pytest

pytestmark = pytest.mark.requires_agent_modules


def _make_png(path: Path) -> Path:
    path.write_bytes(
        b'\x89PNG\r\n\x1a\n'
        b'\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
        b'\x00\x00\x00\x0bIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N'
        b'\x00\x00\x00\x00IEND\xaeB`\x82'
    )
    return path


def test_local_webui_text_mode_auto_analyzes_image_before_agent_turn(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.streaming as streaming
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()

    sid = "webui_auto_vision_001"
    stream_id = "stream-auto-vision"
    session = Session(
        session_id=sid,
        title="Auto Vision",
        workspace=str(tmp_path),
        model="test-model",
        messages=[],
        context_messages=[],
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "これどうにかならん？"
    session.pending_started_at = 1000.0
    session.save(touch_updated_at=False)
    models.SESSIONS[sid] = session

    image_path = _make_png(tmp_path / "screenshot.png")
    calls = []

    async def fake_vision_analyze_tool(*, image_url, user_prompt):
        calls.append((image_url, user_prompt))
        return json.dumps({
            "success": True,
            "analysis": "The screenshot shows an error card: Tool iteration limit reached.",
        })

    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = sid
            self.context_compressor = None
            self.ephemeral_system_prompt = None

        def run_conversation(self, **kwargs):
            captured["user_message"] = kwargs.get("user_message")
            captured["persist_user_message"] = kwargs.get("persist_user_message")
            # Simulate Hermes core: when persist_user_message is supplied it
            # rewrites string user content during early persistence/return.  The
            # image-context fix must therefore pass no override for image turns.
            returned_user_content = (
                kwargs.get("persist_user_message")
                if kwargs.get("persist_user_message") is not None
                else kwargs.get("user_message")
            )
            return {
                "completed": True,
                "final_response": "ok",
                "messages": [
                    {"role": "user", "content": returned_user_content},
                    {"role": "assistant", "content": "ok"},
                ],
            }

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *args, **kwargs: ("test-model", "test-provider", None))
    monkeypatch.setattr(config, "get_config_for_profile_home", lambda _home: {"agent": {"image_input_mode": "text"}})
    monkeypatch.setattr(config, "get_config", lambda: {"agent": {"image_input_mode": "text"}})
    monkeypatch.setattr(streaming, "get_config", lambda: {"agent": {"image_input_mode": "text"}})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *args, **kwargs: [])
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {"status": "not_configured", "source": "none", "label": "", "message_count": 0, "messages": []})
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])
    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", fake_vision_analyze_tool)

    config.STREAMS[stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            session_id=sid,
            msg_text="これどうにかならん？",
            model="test-model",
            workspace=str(tmp_path),
            stream_id=stream_id,
            attachments=[{"path": str(image_path), "mime": "image/png", "is_image": True}],
        )
    finally:
        config.STREAMS.pop(stream_id, None)

    assert calls and calls[0][0] == str(image_path)
    user_message = captured["user_message"]
    assert isinstance(user_message, str)
    assert captured["persist_user_message"] is None
    assert "Tool iteration limit reached" in user_message
    assert user_message.index("Tool iteration limit reached") < user_message.index("これどうにかならん？")
    assert f"vision_analyze with image_url: {image_path}" in user_message
    saved = models.SESSIONS[sid]
    context_json = json.dumps(saved.context_messages, ensure_ascii=False)
    assert "Tool iteration limit reached" in context_json
    assert "[WEBUI_IMAGE_CONTEXT" in context_json
    assert saved.messages[0]["content"] == "これどうにかならん？"


def test_builder_marks_image_attachment_even_when_image_validation_fails(tmp_path):
    import api.streaming as streaming

    image_path = tmp_path / "broken.jpeg"
    image_path.write_bytes(b"not a real jpeg")

    user_message = streaming._build_native_multimodal_message(
        "",
        "なにこれ",
        [{"path": str(image_path), "mime": "image/jpeg", "is_image": True}],
        str(tmp_path),
        cfg={"agent": {"image_input_mode": "text"}},
        provider="test-provider",
        model="test-model",
        auto_analyze_text_mode_images=True,
        auto_analyze_image_attachments=True,
        session_id="sid-broken-image",
    )

    assert isinstance(user_message, str)
    assert "[WEBUI_IMAGE_ATTACHMENT_NOTICE]" in user_message
    assert "画像が添付されています" in user_message
    assert "broken.jpeg" in user_message
    assert user_message.index("[WEBUI_IMAGE_ATTACHMENT_NOTICE]") < user_message.index("なにこれ")


def test_image_attachment_notice_phrase_runs_vision_hook(tmp_path, monkeypatch):
    import api.streaming as streaming

    image_path = _make_png(tmp_path / "hook.png")
    calls = []

    async def fake_vision_analyze_tool(*, image_url, user_prompt):
        calls.append((image_url, user_prompt))
        return json.dumps({
            "success": True,
            "analysis": "The image shows a red warning dialog.",
        })

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", fake_vision_analyze_tool)

    user_message = (
        "[WEBUI_IMAGE_ATTACHMENT_NOTICE]\n"
        "画像が添付されています / The user attached image file(s) to this message.\n\n"
        "なにこれ"
    )
    hooked = streaming._run_image_attachment_notice_hook(
        user_message,
        [{"path": str(image_path), "mime": "image/png", "is_image": True}],
        str(tmp_path),
        session_id="sid-hook",
    )

    assert calls and calls[0][0] == str(image_path)
    assert "[WEBUI_IMAGE_CONTEXT" in hooked
    assert "red warning dialog" in hooked
    assert hooked.index("red warning dialog") < hooked.index("なにこれ")


def test_image_attachment_notice_hook_ignores_filename_only_attachment(tmp_path, monkeypatch):
    import api.streaming as streaming

    image_path = _make_png(tmp_path / "filename-only.png")
    calls = []

    async def fake_vision_analyze_tool(*, image_url, user_prompt):
        calls.append((image_url, user_prompt))
        return json.dumps({"success": True, "analysis": "should not run"})

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", fake_vision_analyze_tool)

    user_message = (
        "[WEBUI_IMAGE_ATTACHMENT_NOTICE]\n"
        "画像が添付されています / The user attached image file(s) to this message.\n\n"
        "なにこれ"
    )
    hooked = streaming._run_image_attachment_notice_hook(
        user_message,
        [str(image_path)],
        str(tmp_path),
        session_id="sid-hook-filename-only",
    )

    assert calls == []
    assert hooked == user_message


def test_builder_does_not_mark_filename_only_attachment_as_image(tmp_path):
    import api.streaming as streaming

    image_path = tmp_path / "filename-only.jpeg"
    image_path.write_bytes(b"not a real jpeg")

    user_message = streaming._build_native_multimodal_message(
        "",
        "なにこれ",
        [str(image_path)],
        str(tmp_path),
        cfg={"agent": {"image_input_mode": "text"}},
        provider="test-provider",
        model="test-model",
        auto_analyze_text_mode_images=True,
        auto_analyze_image_attachments=True,
        session_id="sid-filename-only",
    )

    assert isinstance(user_message, str)
    assert "[WEBUI_IMAGE_ATTACHMENT_NOTICE]" not in user_message
    assert "画像が添付されています" not in user_message
    assert "なにこれ" in user_message


def test_local_webui_recovers_pending_filename_attachment_before_agent_turn(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.streaming as streaming
    import api.upload as upload
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    state_dir = tmp_path / "webui_state"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(config, "STATE_DIR", state_dir, raising=False)
    monkeypatch.setattr(upload, "STATE_DIR", state_dir, raising=False)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()

    sid = "webui_pending_filename_vision"
    stream_id = "stream-pending-filename-vision"
    session = Session(
        session_id=sid,
        title="Auto Vision Pending",
        workspace=str(tmp_path),
        model="test-model",
        messages=[],
        context_messages=[],
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "どう思う"
    session.pending_attachments = ["screenshot.png"]
    session.pending_started_at = 1000.0
    session.save(touch_updated_at=False)
    models.SESSIONS[sid] = session

    attachment_dir = upload._session_attachment_dir(sid)
    attachment_dir.mkdir(parents=True, exist_ok=True)
    image_path = _make_png(attachment_dir / "screenshot.png")
    calls = []

    async def fake_vision_analyze_tool(*, image_url, user_prompt):
        calls.append((image_url, user_prompt))
        return json.dumps({
            "success": True,
            "analysis": "The screenshot shows the workspace menu with New conversation in worktree.",
        })

    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = sid
            self.context_compressor = None
            self.ephemeral_system_prompt = None

        def run_conversation(self, **kwargs):
            captured["user_message"] = kwargs.get("user_message")
            captured["persist_user_message"] = kwargs.get("persist_user_message")
            # Simulate Hermes core: when persist_user_message is supplied it
            # rewrites string user content during early persistence/return.  The
            # image-context fix must therefore pass no override for image turns.
            returned_user_content = (
                kwargs.get("persist_user_message")
                if kwargs.get("persist_user_message") is not None
                else kwargs.get("user_message")
            )
            return {
                "completed": True,
                "final_response": "ok",
                "messages": [
                    {"role": "user", "content": returned_user_content},
                    {"role": "assistant", "content": "ok"},
                ],
            }

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *args, **kwargs: ("test-model", "test-provider", None))
    monkeypatch.setattr(config, "get_config_for_profile_home", lambda _home: {"agent": {"image_input_mode": "text"}})
    monkeypatch.setattr(config, "get_config", lambda: {"agent": {"image_input_mode": "text"}})
    monkeypatch.setattr(streaming, "get_config", lambda: {"agent": {"image_input_mode": "text"}})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *args, **kwargs: [])
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {"status": "not_configured", "source": "none", "label": "", "message_count": 0, "messages": []})
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])
    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", fake_vision_analyze_tool)

    config.STREAMS[stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            session_id=sid,
            msg_text="どう思う",
            model="test-model",
            workspace=str(tmp_path),
            stream_id=stream_id,
            attachments=[],
        )
    finally:
        config.STREAMS.pop(stream_id, None)

    assert calls and calls[0][0] == str(image_path)
    user_message = captured["user_message"]
    assert isinstance(user_message, str)
    assert captured["persist_user_message"] is None
    assert "New conversation in worktree" in user_message
    assert user_message.index("New conversation in worktree") < user_message.index("どう思う")
    assert f"vision_analyze with image_url: {image_path}" in user_message
    saved = models.SESSIONS[sid]
    context_json = json.dumps(saved.context_messages, ensure_ascii=False)
    assert "New conversation in worktree" in context_json
    assert "[WEBUI_IMAGE_CONTEXT" in context_json
    assert saved.messages[0]["content"] == "どう思う"


def test_local_webui_recovers_turn_journal_attachment_before_agent_turn(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.streaming as streaming
    import api.upload as upload
    from api.models import Session
    from api.turn_journal import append_turn_journal_event

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    state_dir = tmp_path / "webui_state"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(config, "STATE_DIR", state_dir, raising=False)
    monkeypatch.setattr(upload, "STATE_DIR", state_dir, raising=False)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()

    sid = "webui_turn_journal_vision"
    stream_id = "stream-turn-journal-vision"
    session = Session(
        session_id=sid,
        title="Auto Vision Journal",
        workspace=str(tmp_path),
        model="test-model",
        messages=[],
        context_messages=[],
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "なにこれ"
    session.pending_attachments = []
    session.pending_started_at = 1000.0
    session.save(touch_updated_at=False)
    models.SESSIONS[sid] = session

    attachment_dir = upload._session_attachment_dir(sid)
    attachment_dir.mkdir(parents=True, exist_ok=True)
    image_path = _make_png(attachment_dir / "IMG_1064.png")
    append_turn_journal_event(
        sid,
        {
            "event": "submitted",
            "stream_id": stream_id,
            "role": "user",
            "content": "なにこれ",
            "attachments": ["IMG_1064.png"],
            "workspace": str(tmp_path),
            "model": "test-model",
        },
        session_dir=session_dir,
    )
    calls = []

    async def fake_vision_analyze_tool(*, image_url, user_prompt):
        calls.append((image_url, user_prompt))
        return json.dumps({
            "success": True,
            "analysis": "The image shows a dog lying on jeans in a cluttered room.",
        })

    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = sid
            self.context_compressor = None
            self.ephemeral_system_prompt = None

        def run_conversation(self, **kwargs):
            captured["user_message"] = kwargs.get("user_message")
            captured["persist_user_message"] = kwargs.get("persist_user_message")
            # Simulate Hermes core: when persist_user_message is supplied it
            # rewrites string user content during early persistence/return.  The
            # image-context fix must therefore pass no override for image turns.
            returned_user_content = (
                kwargs.get("persist_user_message")
                if kwargs.get("persist_user_message") is not None
                else kwargs.get("user_message")
            )
            return {
                "completed": True,
                "final_response": "ok",
                "messages": [
                    {"role": "user", "content": returned_user_content},
                    {"role": "assistant", "content": "ok"},
                ],
            }

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *args, **kwargs: ("test-model", "test-provider", None))
    monkeypatch.setattr(config, "get_config_for_profile_home", lambda _home: {"agent": {"image_input_mode": "text"}})
    monkeypatch.setattr(config, "get_config", lambda: {"agent": {"image_input_mode": "text"}})
    monkeypatch.setattr(streaming, "get_config", lambda: {"agent": {"image_input_mode": "text"}})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *args, **kwargs: [])
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {"status": "not_configured", "source": "none", "label": "", "message_count": 0, "messages": []})
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])
    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", fake_vision_analyze_tool)

    config.STREAMS[stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            session_id=sid,
            msg_text="なにこれ",
            model="test-model",
            workspace=str(tmp_path),
            stream_id=stream_id,
            attachments=[],
        )
    finally:
        config.STREAMS.pop(stream_id, None)

    assert calls and calls[0][0] == str(image_path)
    user_message = captured["user_message"]
    assert isinstance(user_message, str)
    assert captured["persist_user_message"] is None
    assert "[WEBUI_IMAGE_CONTEXT" in user_message
    assert "dog lying on jeans" in user_message
    assert user_message.index("dog lying on jeans") < user_message.index("なにこれ")
    assert f"vision_analyze with image_url: {image_path}" in user_message
    saved = models.SESSIONS[sid]
    context_json = json.dumps(saved.context_messages, ensure_ascii=False)
    assert "dog lying on jeans" in context_json
    assert "[WEBUI_IMAGE_CONTEXT" in context_json
    assert saved.messages[0]["content"] == "なにこれ"
