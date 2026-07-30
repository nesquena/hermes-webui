"""Regression coverage for #2965 streaming SessionDB profile isolation."""

from __future__ import annotations

import os
import queue
import sys
import types
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def test_streaming_sessiondb_uses_session_profile_state_db(tmp_path, monkeypatch):
    """WebUI streaming must not rely on hermes_state.DEFAULT_DB_PATH.

    ``hermes_state.DEFAULT_DB_PATH`` is frozen at import time. A non-default
    WebUI profile therefore needs the streaming attach path to pass the already
    resolved profile home to SessionDB explicitly.
    """
    from api import config as cfg
    from api import oauth
    from api import profiles
    from api import streaming

    profile_home = tmp_path / "hermes-home" / "profiles" / "zhangtingban"
    profile_home.mkdir(parents=True)

    class FakeSession:
        session_id = "issue2965-session"
        title = "Issue 2965"
        workspace = str(tmp_path)
        model = "test-model"
        model_provider = None
        profile = "zhangtingban"
        personality = None
        messages = []
        context_messages = []
        tool_calls = []
        input_tokens = 0
        output_tokens = 0
        estimated_cost = None
        context_length = 0
        threshold_tokens = 0
        last_prompt_tokens = 0
        active_stream_id = "issue2965-stream"
        pending_user_message = None
        pending_attachments = []
        pending_started_at = None
        llm_title_generated = True

        def save(self, *args, **kwargs):
            return None

        def compact(self):
            return {
                "session_id": self.session_id,
                "title": self.title,
                "workspace": self.workspace,
                "model": self.model,
                "created_at": 0,
                "updated_at": 0,
                "pinned": False,
                "archived": False,
                "project_id": None,
                "profile": self.profile,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "estimated_cost": self.estimated_cost,
                "personality": self.personality,
            }

    class FakeSessionDB:
        def __init__(self, db_path=None):
            self.db_path = db_path

        def close(self):
            return None

    class CapturingAgent:
        def __init__(self, **kwargs):
            self.session_db = kwargs.get("session_db")
            self._session_db = self.session_db
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.context_compressor = None
            self._last_error = None
            self.ephemeral_system_prompt = None

        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history
                + [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "ok"},
                ]
            }

        def interrupt(self, _message):
            return None

    session_db_instances = []

    def fake_session_db(db_path=None):
        db = FakeSessionDB(db_path=db_path)
        session_db_instances.append(db)
        return db

    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = lambda requested=None: {
        "provider": requested or "test-provider",
        "api_key": "synthetic-key",
        "base_url": None,
    }
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = fake_session_db

    fake_session = FakeSession()

    monkeypatch.setattr(streaming, "get_session", lambda _sid: fake_session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: CapturingAgent)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda _model, **_kw: ("test-model", "test-provider", None),
    )
    monkeypatch.setattr(streaming, "_maybe_schedule_title_refresh", lambda *args, **kwargs: None)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _profile: profile_home)
    monkeypatch.setattr(profiles, "get_profile_runtime_env", lambda _home: {})
    monkeypatch.setattr(
        oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda _resolver, requested=None: {
            "provider": requested or "test-provider",
            "api_key": "synthetic-key",
            "base_url": None,
        },
    )
    monkeypatch.setattr("api.config.get_config", lambda: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda _cfg: [])
    monkeypatch.setattr("api.config.load_settings", lambda: {})
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)

    with cfg.SESSION_AGENT_CACHE_LOCK:
        cfg.SESSION_AGENT_CACHE.clear()
    streaming.STREAMS.clear()
    streaming.CANCEL_FLAGS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.STREAM_PARTIAL_TEXT.clear()
    streaming.STREAM_REASONING_TEXT.clear()
    streaming.STREAM_LIVE_TOOL_CALLS.clear()

    streaming.STREAMS[fake_session.active_stream_id] = queue.Queue()
    old_home = os.environ.get("HERMES_HOME")
    try:
        streaming._run_agent_streaming(
            session_id=fake_session.session_id,
            msg_text="hello from zhangtingban",
            model="test-model",
            model_provider="test-provider",
            workspace=str(tmp_path),
            stream_id=fake_session.active_stream_id,
        )
    finally:
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home

    assert session_db_instances, "streaming should construct a SessionDB for session_search"
    assert session_db_instances[0].db_path == profile_home / "state.db"
    with cfg.SESSION_AGENT_CACHE_LOCK:
        agent = cfg.SESSION_AGENT_CACHE[fake_session.session_id][0]
    assert agent._session_db.db_path == profile_home / "state.db"
