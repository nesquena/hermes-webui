"""Runtime coverage for profile-scoped response style overlays."""

from __future__ import annotations

import os
import queue
import sys
import types
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

yaml = pytest.importorskip("yaml")


class FakeSession:
    def __init__(self, workspace: Path):
        self.session_id = "profile-response-style-session"
        self.title = "Response style runtime"
        self.workspace = str(workspace)
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


def _write_profile_config(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def test_profile_response_style_uses_session_profile_and_updates_cached_agent(tmp_path, monkeypatch):
    """Profile response style is an ephemeral overlay, not a SOUL.md rewrite.

    The default profile intentionally has a different style. If the streaming
    thread reads the process-active profile instead of ``session.profile``, this
    test sees the wrong prompt.
    """
    from api import config as cfg
    from api import oauth
    from api import profiles
    from api import streaming

    base_home = tmp_path / ".hermes"
    alpha_home = base_home / "profiles" / "alpha"
    beta_home = base_home / "profiles" / "beta"
    base_home.mkdir(parents=True)
    alpha_home.mkdir(parents=True)
    beta_home.mkdir(parents=True)
    (alpha_home / "SOUL.md").write_text("ALPHA_SOUL", encoding="utf-8")
    (beta_home / "SOUL.md").write_text("BETA_SOUL", encoding="utf-8")
    _write_profile_config(base_home / "config.yaml", {"agent": {"personality": "hype"}})
    personalities = {
        "custom": "CUSTOM_SESSION_PERSONALITY_APPLIES",
        "technical": "CUSTOM_TECHNICAL_PERSONALITY_APPLIES",
    }
    _write_profile_config(
        alpha_home / "config.yaml",
        {"agent": {"personality": "technical", "personalities": personalities}},
    )
    _write_profile_config(
        beta_home / "config.yaml",
        {"agent": {"personality": "kawaii", "personalities": personalities}},
    )

    fake_session = FakeSession(tmp_path)
    constructed_agents = []
    prompts_used_for_runs = []
    ephemeral_used_for_runs = []

    class StyleCapturingAgent:
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
            self._session_db = None
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
            ephemeral_used_for_runs.append(self.ephemeral_system_prompt or "")
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

    monkeypatch.setenv("HERMES_BASE_HOME", str(base_home))
    monkeypatch.setenv("HERMES_HOME", str(base_home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base_home)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    profiles.clear_request_profile()
    monkeypatch.setattr(streaming, "get_session", lambda _sid: fake_session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: StyleCapturingAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda _model: ("test-model", "test-provider", None))
    monkeypatch.setattr(streaming, "_maybe_schedule_title_refresh", lambda *args, **kwargs: None)
    monkeypatch.setattr(oauth, "resolve_runtime_provider_with_anthropic_env_lock", lambda _resolver, requested=None: {
        "provider": requested or "test-provider",
        "api_key": "synthetic-key",
        "base_url": None,
    })
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

    fake_session.personality = "kawaii"
    run_turn("alpha", "response-style-stream-1", "first turn")
    fake_session.personality = "technical"
    _write_profile_config(
        alpha_home / "config.yaml",
        {"agent": {"personality": "teacher", "personalities": personalities}},
    )
    run_turn("alpha", "response-style-stream-2", "same profile after style change")
    fake_session.personality = None
    run_turn("beta", "response-style-stream-3", "profile switched turn")

    assert [agent.constructed_home for agent in constructed_agents] == [
        str(alpha_home),
        str(beta_home),
    ]
    assert prompts_used_for_runs == ["ALPHA_SOUL", "ALPHA_SOUL", "BETA_SOUL"]
    assert "precise technical response style" in ephemeral_used_for_runs[0]
    assert "teacherly response style" in ephemeral_used_for_runs[1]
    assert "playful, cute, upbeat response style" in ephemeral_used_for_runs[2]
    assert "STALE_SESSION_KAWAII_SHOULD_NOT_APPLY" not in ephemeral_used_for_runs[0]
    assert "CUSTOM_TECHNICAL_PERSONALITY_APPLIES" in ephemeral_used_for_runs[1]
    assert all("high-energy, encouraging response style" not in prompt for prompt in ephemeral_used_for_runs)
    assert all("WebUI progress contract" in prompt for prompt in ephemeral_used_for_runs)


def test_credential_self_heal_reapplies_webui_ephemeral_prompt_to_rebuilt_agents():
    """Auth self-heal retries must keep response-style and progress overlays."""
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
    assert "def _apply_webui_ephemeral_prompt(_agent):" in src
    assert "_apply_webui_ephemeral_prompt(agent)" in src
    assert "_apply_webui_ephemeral_prompt(_heal_agent)" in src


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("", None),
        ("concise", "concise, direct response style"),
        ("technical", "precise technical response style"),
        ("teacher", "teacherly response style"),
        ("kawaii", "playful, cute, upbeat response style"),
        ("hype", "high-energy, encouraging response style"),
    ],
)
def test_profile_response_style_prompt_supports_all_modes(mode, expected):
    from api.streaming import _profile_response_style_prompt

    prompt = _profile_response_style_prompt({"agent": {"personality": mode}})

    if expected is None:
        assert prompt is None
    else:
        assert expected in prompt
