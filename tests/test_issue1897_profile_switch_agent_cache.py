"""Regression coverage for #1897 — same-session profile switch identity bleed."""

from __future__ import annotations

import os
import queue
import sys
import types
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")


def _signature_block() -> str:
    sig_start = STREAMING_PY.index("_sig_blob = _json.dumps")
    sig_end = STREAMING_PY.index("], sort_keys=True)", sig_start)
    return STREAMING_PY[sig_start:sig_end]


def test_same_session_profile_switch_rebuilds_agent_under_new_soul_home(tmp_path, monkeypatch):
    """Switching profiles in one WebUI session must not reuse old SOUL.md.

    The fake AIAgent mirrors the real failure mode: it reads SOUL.md from
    HERMES_HOME at construction time and keeps that value in a cached system
    prompt. Two consecutive turns on the same profile should reuse the agent;
    changing only ``session.profile`` should create a fresh agent whose cached
    prompt comes from the new synthetic profile home.
    """
    sys.path.insert(0, str(REPO))
    from api import config as cfg
    from api import oauth
    from api import profiles
    from api import streaming

    default_home = tmp_path / "hermes-home"
    profile_a_home = default_home / "profiles" / "alpha"
    profile_b_home = default_home / "profiles" / "beta"
    profile_a_home.mkdir(parents=True)
    profile_b_home.mkdir(parents=True)
    (profile_a_home / "SOUL.md").write_text(
        "PROFILE_ALPHA_SYNTHETIC_SOUL",
        encoding="utf-8",
    )
    (profile_b_home / "SOUL.md").write_text(
        "PROFILE_BETA_SYNTHETIC_SOUL",
        encoding="utf-8",
    )

    class FakeSession:
        def __init__(self):
            self.session_id = "issue1897-same-session"
            self.title = "Pinned test title"
            self.workspace = str(tmp_path)
            self.model = "test-model"
            self.model_provider = None
            self.profile = "alpha"
            self.personality = None
            self.messages = []
            self.context_messages = []
            self.tool_calls = []
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.llm_title_generated = True

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

    constructed_agents = []
    prompts_used_for_runs = []
    homes_seen_during_runs = []

    class SoulCachingAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id")
            self.model = kwargs.get("model")
            self.provider = kwargs.get("provider")
            self.base_url = kwargs.get("base_url")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.ephemeral_system_prompt = None
            self._last_error = None
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.tool_progress_callback = kwargs.get("tool_progress_callback")
            self.reasoning_callback = kwargs.get("reasoning_callback")
            self.clarify_callback = kwargs.get("clarify_callback")
            home = Path(os.environ["HERMES_HOME"])
            self.constructed_home = str(home)
            self._cached_system_prompt = (home / "SOUL.md").read_text(encoding="utf-8")
            constructed_agents.append(self)

        def run_conversation(self, **kwargs):
            prompts_used_for_runs.append(self._cached_system_prompt)
            homes_seen_during_runs.append(os.environ.get("HERMES_HOME"))
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history
                + [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {
                        "role": "assistant",
                        "content": f"reply from {self._cached_system_prompt}",
                    },
                ]
            }

        def interrupt(self, _message):
            return None

    fake_session = FakeSession()
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = lambda requested=None: {
        "provider": requested or "test-provider",
        "api_key": "synthetic-key",
        "base_url": None,
    }
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda: None

    def home_for_profile(profile_name):
        return {"alpha": profile_a_home, "beta": profile_b_home}[profile_name]

    monkeypatch.setattr(streaming, "get_session", lambda _sid: fake_session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: SoulCachingAgent)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda _model: ("test-model", "test-provider", None),
    )
    monkeypatch.setattr(streaming, "_maybe_schedule_title_refresh", lambda *args, **kwargs: None)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", home_for_profile)
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

    def run_turn(profile_name: str, stream_id: str, text: str):
        fake_session.profile = profile_name
        fake_session.active_stream_id = stream_id
        streaming.STREAMS[stream_id] = queue.Queue()
        streaming._run_agent_streaming(
            session_id=fake_session.session_id,
            msg_text=text,
            model="test-model",
            model_provider="test-provider",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    run_turn("alpha", "issue1897-stream-1", "first turn")
    run_turn("alpha", "issue1897-stream-2", "same profile second turn")
    assert len(constructed_agents) == 1, "same-profile turns should reuse the cached agent"

    run_turn("beta", "issue1897-stream-3", "profile switched turn")

    assert prompts_used_for_runs == [
        "PROFILE_ALPHA_SYNTHETIC_SOUL",
        "PROFILE_ALPHA_SYNTHETIC_SOUL",
        "PROFILE_BETA_SYNTHETIC_SOUL",
    ]
    assert [agent.constructed_home for agent in constructed_agents] == [
        str(profile_a_home),
        str(profile_b_home),
    ]
    assert homes_seen_during_runs == [
        str(profile_a_home),
        str(profile_a_home),
        str(profile_b_home),
    ]
    with cfg.SESSION_AGENT_CACHE_LOCK:
        assert cfg.SESSION_AGENT_CACHE[fake_session.session_id][0] is constructed_agents[-1]


def test_cache_signature_includes_profile_home():
    block = _signature_block()
    assert "_profile_home" in block, (
        "SESSION_AGENT_CACHE signature is missing `_profile_home`. Without this, "
        "same-session profile switches reuse the cached agent built under the "
        "previous profile's HERMES_HOME, leaking the old SOUL.md into new turns."
    )


def test_profile_home_resolved_before_cache_signature():
    profile_home_assignment = STREAMING_PY.index("_profile_home = str(_profile_home_path)")
    sig_start = STREAMING_PY.index("_sig_blob = _json.dumps")
    assert profile_home_assignment < sig_start


def test_signature_uses_profile_home_with_fallback():
    block = _signature_block()
    assert "_profile_home or ''" in block, (
        "Signature should use `_profile_home or ''` so empty-home deployments get "
        "a stable cache key rather than unnecessary cache churn."
    )
