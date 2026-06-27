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
            return {
                "completed": True,
                "final_response": "ok",
                "messages": [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
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
    assert "Tool iteration limit reached" in user_message
    assert user_message.index("Tool iteration limit reached") < user_message.index("これどうにかならん？")
    assert f"vision_analyze with image_url: {image_path}" in user_message
