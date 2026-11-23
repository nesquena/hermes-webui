"""Regression: MCP discovery must run on every streaming turn, per profile (#6006).

`_run_agent_streaming` calls `discover_mcp_tools()` after mutating HERMES_HOME for
the turn's profile.  A process-wide "already ran" flag turns that per-turn call
into a once-per-process call: the second turn -- including a turn for a different
profile, and including a turn that should pick up a newly configured or retried
MCP server -- never discovers again.

These cases drive the real `_run_agent_streaming` body twice in the same process,
for two different profile homes, and measure what the MCP module sees.
"""

from __future__ import annotations

import contextlib
import os
import queue
import sys
import types

import pytest


def _install_streaming_seams(monkeypatch, *, homes, calls, turns):
    import api.config as _config_mod
    import api.profiles as profiles_api
    import api.streaming as _streaming

    class _Session:
        def __init__(self, session_id: str, profile: str, workspace):
            self.session_id = session_id
            self.workspace = str(workspace)
            self.profile = profile
            self.model = "gpt-4"
            self.model_provider = "hermes"
            self.messages = []
            self.context_messages = []
            self.path = str(workspace / "session.json")
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_started_at = None
            self.pending_user_source = None
            self.pending_attachments = []

        def save(self, *args, **kwargs):
            return None

    class _FakeMeter:
        def begin_session(self, *args, **kwargs):
            return None

        def end_session(self, *args, **kwargs):
            return None

        def get_interval(self):
            return 11.0

        def set_pending_started_at(self, *args, **kwargs):
            return None

        def get_ttft_ms(self, *args, **kwargs):
            return None

        def get_stats(self):
            return {}

        def record_token(self, *args, **kwargs):
            pass

        def record_reasoning(self, *args, **kwargs):
            pass

    class _SentinelAgent:
        def __init__(self, *args, **kwargs):
            pass

        def run_conversation(self, *args, **kwargs):
            raise RuntimeError("streaming test sentinel")

    def _discover_mcp_tools():
        # What the real helper resolves: HERMES_HOME of the turn being started.
        calls.append(os.environ.get("HERMES_HOME"))

    def _set_override(profile_home: str):
        turns.append(profile_home)
        return ("sentinel-module", None, True)

    monkeypatch.setattr(_streaming, "register_active_run", lambda *a, **k: None)
    monkeypatch.setattr(_streaming, "update_active_run", lambda *a, **k: None)
    monkeypatch.setattr(
        _streaming,
        "get_session",
        lambda sid: _Session(sid, "alpha" if "alpha" in sid else "beta", homes["workspace"]),
    )
    monkeypatch.setattr(_streaming, "_get_session_agent_lock", lambda sid: contextlib.nullcontext())
    monkeypatch.setattr(_streaming, "_set_streaming_hermes_home_override", _set_override)
    monkeypatch.setattr(
        _streaming, "_reset_streaming_hermes_home_override", lambda *a, **k: None
    )
    monkeypatch.setattr(_streaming, "_set_thread_env", lambda *a, **k: None)
    monkeypatch.setattr(_streaming, "_clear_thread_env", lambda *a, **k: None)
    monkeypatch.setattr(_streaming, "_prewarm_skill_tool_modules", lambda *a, **k: None)
    monkeypatch.setattr(
        _streaming, "_install_streaming_cronjob_profile_wrapper", lambda *a, **k: None
    )
    monkeypatch.setattr(_streaming, "_get_ai_agent", lambda: _SentinelAgent)
    monkeypatch.setattr(
        _streaming, "_materialize_pending_user_turn_before_error", lambda *a, **k: None
    )
    monkeypatch.setattr(
        _streaming, "_snapshot_and_append_partial_on_error", lambda *a, **k: None
    )
    monkeypatch.setattr(
        _streaming, "append_turn_journal_event_for_stream", lambda *a, **k: None
    )
    monkeypatch.setattr(_streaming, "meter", lambda: _FakeMeter())
    monkeypatch.setattr(_streaming, "RunJournalWriter", lambda *a, **k: None)
    monkeypatch.setattr(_streaming, "_build_agent_thread_env", lambda *a, **k: {})
    monkeypatch.setattr(
        _streaming,
        "resolve_model_provider",
        lambda model_with_provider_context, explicitly_picked=None, **kw: (
            model_with_provider_context,
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        _streaming,
        "_runtime_preferred_base_url",
        lambda rt, provider, configured_base_url: configured_base_url,
    )
    monkeypatch.setattr(
        _streaming,
        "_apply_profile_home_context_to_streaming_model",
        lambda model, provider_context, profile_home, has_profile: (
            model,
            provider_context,
            False,
        ),
    )

    monkeypatch.setattr(profiles_api, "_skill_modules_support_profile_home", lambda home: True)
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda name: homes[name])
    monkeypatch.setattr(profiles_api, "get_profile_runtime_env", lambda home: {})
    monkeypatch.setattr(profiles_api, "filter_runtime_env_for_gateway_parity", lambda env: {})
    monkeypatch.setattr(profiles_api, "patch_skill_home_modules", lambda *a, **k: None)
    monkeypatch.setattr(_config_mod, "_resolve_cli_toolsets", lambda cfg: [])
    monkeypatch.setattr(
        _config_mod, "get_config_for_profile_home", lambda profile_home=None, **kw: {}
    )

    fake_mcp_module = types.ModuleType("tools.mcp_tool")
    fake_mcp_module.discover_mcp_tools = _discover_mcp_tools
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", fake_mcp_module)

    return _streaming


def _stream_one_turn(streaming, *, session_id: str, stream_id: str, workspace):
    streaming.STREAMS[stream_id] = queue.Queue()
    streaming._run_agent_streaming(
        session_id=session_id,
        msg_text="hi",
        model="gpt-4",
        workspace=str(workspace),
        stream_id=stream_id,
    )
    assert stream_id not in streaming.STREAMS


def test_discovery_runs_on_a_turn_for_a_different_profile(tmp_path, monkeypatch):
    """A prior turn must not swallow discovery for the next profile's turn."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home_alpha = tmp_path / "alpha"
    home_alpha.mkdir()
    home_beta = tmp_path / "beta"
    home_beta.mkdir()
    calls: list[str | None] = []
    turns: list[str] = []

    streaming = _install_streaming_seams(
        monkeypatch,
        homes={"alpha": home_alpha, "beta": home_beta, "workspace": workspace},
        calls=calls,
        turns=turns,
    )

    _stream_one_turn(
        streaming,
        session_id="session-alpha",
        stream_id="stream-alpha",
        workspace=workspace,
    )
    first_turn_calls = len(calls)

    _stream_one_turn(
        streaming,
        session_id="session-beta",
        stream_id="stream-beta",
        workspace=workspace,
    )

    assert first_turn_calls == 1, f"first turn must discover once, saw {first_turn_calls}"
    assert len(calls) == 2, (
        "discovery must run again for the second turn (different profile), "
        f"calls={calls}"
    )
    assert calls[0] == str(home_alpha), calls
    assert calls[1] == str(home_beta), calls


def test_discovery_runs_on_every_turn_of_the_same_profile(tmp_path, monkeypatch):
    """Same-profile turns also rediscover: new servers and retries must be seen."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home_alpha = tmp_path / "alpha"
    home_alpha.mkdir()
    calls: list[str | None] = []
    turns: list[str] = []

    streaming = _install_streaming_seams(
        monkeypatch,
        homes={"alpha": home_alpha, "beta": home_alpha, "workspace": workspace},
        calls=calls,
        turns=turns,
    )

    _stream_one_turn(
        streaming,
        session_id="session-alpha-1",
        stream_id="stream-alpha-1",
        workspace=workspace,
    )
    _stream_one_turn(
        streaming,
        session_id="session-alpha-2",
        stream_id="stream-alpha-2",
        workspace=workspace,
    )

    assert len(calls) == 2, f"each turn must discover, calls={calls}"
    assert calls == [str(home_alpha), str(home_alpha)], calls


def test_discovery_failure_is_still_non_fatal(tmp_path, monkeypatch):
    """Control: a raising discovery must not abort the turn (pre-existing contract)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home_alpha = tmp_path / "alpha"
    home_alpha.mkdir()
    calls: list[str | None] = []
    turns: list[str] = []

    streaming = _install_streaming_seams(
        monkeypatch,
        homes={"alpha": home_alpha, "beta": home_alpha, "workspace": workspace},
        calls=calls,
        turns=turns,
    )

    def _boom():
        raise RuntimeError("mcp unavailable")

    sys.modules["tools.mcp_tool"].discover_mcp_tools = _boom

    _stream_one_turn(
        streaming,
        session_id="session-alpha-boom",
        stream_id="stream-alpha-boom",
        workspace=workspace,
    )

    assert turns == [str(home_alpha)], turns


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
