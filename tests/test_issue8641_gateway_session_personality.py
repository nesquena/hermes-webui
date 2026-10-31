"""Regression coverage for #8641: session personality on Gateway-routed turns.

The embedded (in-process) WebUI worker has always resolved the session's
explicitly selected ``agent.personalities`` entry into the ephemeral system
prompt. The Gateway-routed worker built the same prompt but passed a hard-coded
``None`` for the personality argument, so a personality the user picked in the
browser was silently dropped on every Gateway-routed WebUI turn.

Both paths now go through one shared resolver,
``api.streaming._webui_session_personality_prompt``.

The Gateway tests below drive the real ``_run_gateway_chat_streaming`` worker
against a stubbed Gateway endpoint and assert on the system message the worker
actually puts on the wire -- not on the helper in isolation.
"""
from __future__ import annotations

from collections import OrderedDict
import json
import queue
import sys
import types
from types import SimpleNamespace
from unittest import mock

import api.gateway_chat as gateway_chat
import api.models as models
import api.streaming as streaming
from api import config as api_config
from api.config import STREAMS
from api.models import new_session

PERSONALITIES = {
    "terse": "Answer in one short sentence.",
    "analyst": {
        "system_prompt": "Lead with the evidence.",
        "prompt": "UNUSED prompt-key fallback.",
        "tone": "measured",
        "style": "bulleted",
    },
    "prompt_only": {
        "prompt": "Use the prompt key when system_prompt is absent.",
    },
}


def _cfg(personalities):
    return {"agent": {"personalities": personalities}}


def _pin_config(monkeypatch, cfg):
    """Make every get_config() in the worker resolve to this dict."""
    monkeypatch.setattr(api_config, "get_config", lambda: cfg)


# ── shared resolver ─────────────────────────────────────────────────────────

def test_resolver_reads_string_definition():
    session = SimpleNamespace(personality="terse")

    assert streaming._webui_session_personality_prompt(session, _cfg(dict(PERSONALITIES))) == (
        "Answer in one short sentence."
    )


def test_resolver_prefers_system_prompt_over_prompt_key():
    prompt = streaming._webui_session_personality_prompt(
        SimpleNamespace(personality="analyst"), _cfg(dict(PERSONALITIES))
    )

    assert prompt is not None
    assert prompt.splitlines()[0] == "Lead with the evidence."
    # system_prompt wins: the prompt-key fallback must not leak in.
    assert "UNUSED prompt-key fallback." not in prompt


def test_resolver_falls_back_to_prompt_key():
    assert streaming._webui_session_personality_prompt(
        SimpleNamespace(personality="prompt_only"), _cfg(dict(PERSONALITIES))
    ) == "Use the prompt key when system_prompt is absent."


def test_resolver_appends_tone_and_style():
    assert streaming._webui_session_personality_prompt(
        SimpleNamespace(personality="analyst"), _cfg(dict(PERSONALITIES))
    ) == "Lead with the evidence.\nTone: measured\nStyle: bulleted"


def test_resolver_returns_none_without_a_selection():
    cfg = _cfg(dict(PERSONALITIES))

    assert streaming._webui_session_personality_prompt(SimpleNamespace(personality=None), cfg) is None
    assert streaming._webui_session_personality_prompt(SimpleNamespace(personality=""), cfg) is None
    assert streaming._webui_session_personality_prompt(SimpleNamespace(), cfg) is None
    assert streaming._webui_session_personality_prompt(None, cfg) is None


def test_resolver_returns_none_for_unknown_or_malformed_definitions():
    assert streaming._webui_session_personality_prompt(
        SimpleNamespace(personality="nope"), _cfg(dict(PERSONALITIES))
    ) is None

    # A mapping with no system_prompt/prompt key but with hints still renders.
    assert streaming._webui_session_personality_prompt(
        SimpleNamespace(personality="terse"), _cfg({"terse": {"tone": "measured"}})
    ) == "Tone: measured"

    # A non-mapping personalities section must not explode.
    assert streaming._webui_session_personality_prompt(
        SimpleNamespace(personality="terse"), _cfg(["terse"])
    ) is None


# ── gateway-routed worker ───────────────────────────────────────────────────

def _gateway_turn(tmp_path, monkeypatch, cfg, personality, tag="a", session=None, spy=None):
    """Run one Gateway-routed WebUI turn; return (system prompt, session).

    The Gateway endpoint is stubbed at the urllib layer, so the assertion target
    is the system message this worker really assembled for the request body.
    ``spy`` (when given) records every call the worker makes into
    ``_webui_ephemeral_system_prompt`` -- the personality argument it actually
    passes, plus the prompt that came back.
    """
    stream_id = f"stream-8641-{tag}"
    if session is None:
        session_dir = tmp_path / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
        monkeypatch.setattr(models, "SESSIONS", OrderedDict())
        session = new_session()
        session.active_stream_id = stream_id
        session.pending_user_message = "hello"
        session.pending_attachments = []
        session.pending_started_at = 1
        session.save()

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(
        streaming,
        "_load_webui_prefill_context",
        lambda _cfg: {
            "status": "not_configured",
            "source": "none",
            "label": "",
            "message_count": 0,
            "messages": [],
        },
    )
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda _ctx, _cfg: [])

    if spy is not None:
        real_builder = streaming._webui_ephemeral_system_prompt

        def recording_builder(personality_prompt, surface_context=None, config_data=None):
            prompt = real_builder(personality_prompt, surface_context, config_data)
            spy.append({
                "personality": personality_prompt,
                "surface_context": dict(surface_context or {}),
                "config_data": config_data,
                "prompt": prompt,
            })
            return prompt

        monkeypatch.setattr(streaming, "_webui_ephemeral_system_prompt", recording_builder)

    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            yield b"data: [DONE]\n\n"

    def fake_urlopen(req, timeout=0):
        requests.append(req)
        return FakeResponse()

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)

    session.personality = personality
    channel = mock.MagicMock()
    channel.put_nowait = lambda item: None
    STREAMS[stream_id] = channel

    gateway_chat._run_gateway_chat_streaming(
        session.session_id,
        "hello",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
    )

    assert requests, "the gateway worker issued no request"
    chat_requests = [
        r for r in requests
        if r.get_method() == "POST" and r.full_url.endswith("/v1/chat/completions")
    ]
    assert chat_requests, "the gateway worker issued no chat/completions request"
    payload = json.loads(chat_requests[-1].data.decode("utf-8"))
    system_messages = [m for m in payload["messages"] if m.get("role") == "system"]
    assert system_messages, "the gateway request carried no system message"
    return system_messages[0]["content"], session


def test_gateway_turn_carries_string_personality(tmp_path, monkeypatch):
    cfg = _cfg(dict(PERSONALITIES))
    _pin_config(monkeypatch, cfg)
    spy = []

    system_prompt, _ = _gateway_turn(tmp_path, monkeypatch, cfg, "terse", spy=spy)

    # The worker really resolved the session's personality for this turn ...
    assert spy, "the gateway worker never built an ephemeral system prompt"
    assert spy[-1]["personality"] == "Answer in one short sentence."
    # ... and put it on the wire.
    assert "Answer in one short sentence." in system_prompt


def test_gateway_turn_carries_dict_personality_with_tone_and_style(tmp_path, monkeypatch):
    cfg = _cfg(dict(PERSONALITIES))
    _pin_config(monkeypatch, cfg)
    spy = []

    system_prompt, _ = _gateway_turn(tmp_path, monkeypatch, cfg, "analyst", spy=spy)

    assert spy[-1]["personality"] == (
        "Lead with the evidence.\nTone: measured\nStyle: bulleted"
    )
    assert "Lead with the evidence." in system_prompt
    assert "Tone: measured" in system_prompt
    assert "Style: bulleted" in system_prompt
    assert "UNUSED prompt-key fallback." not in system_prompt


def test_gateway_turn_carries_prompt_key_fallback(tmp_path, monkeypatch):
    cfg = _cfg(dict(PERSONALITIES))
    _pin_config(monkeypatch, cfg)
    spy = []

    system_prompt, _ = _gateway_turn(tmp_path, monkeypatch, cfg, "prompt_only", spy=spy)

    assert spy[-1]["personality"] == "Use the prompt key when system_prompt is absent."
    assert "Use the prompt key when system_prompt is absent." in system_prompt


def test_gateway_turn_picks_up_personality_config_changes_next_turn(tmp_path, monkeypatch):
    personalities = {"terse": "First definition."}
    cfg = _cfg(personalities)
    _pin_config(monkeypatch, cfg)
    spy = []

    system_prompt, session = _gateway_turn(
        tmp_path, monkeypatch, cfg, "terse", tag="live", spy=spy
    )
    assert spy[-1]["personality"] == "First definition."
    assert "First definition." in system_prompt

    # Same session, personalities edited in place: the NEXT turn must use the
    # new value (no cached prompt, no stale personality).
    personalities["terse"] = "Second definition."
    system_prompt, _ = _gateway_turn(
        tmp_path, monkeypatch, cfg, "terse", tag="live", session=session, spy=spy
    )

    assert spy[-1]["personality"] == "Second definition."
    assert "Second definition." in system_prompt
    assert "First definition." not in system_prompt


def test_gateway_turn_drops_personality_but_keeps_webui_guidance_when_cleared(tmp_path, monkeypatch):
    cfg = _cfg(dict(PERSONALITIES))
    _pin_config(monkeypatch, cfg)
    spy = []

    _, session = _gateway_turn(tmp_path, monkeypatch, cfg, "analyst", tag="clear", spy=spy)
    system_prompt, _ = _gateway_turn(
        tmp_path, monkeypatch, cfg, None, tag="clear", session=session, spy=spy
    )

    # The personality text is gone ...
    assert spy[-1]["personality"] is None
    assert "Lead with the evidence." not in system_prompt
    assert "Tone: measured" not in system_prompt
    assert "Style: bulleted" not in system_prompt
    # ... but the WebUI surface / progress / delivery guidance survives.
    assert "Final visible assistant replies" in system_prompt
    assert "Connected Platforms:" in system_prompt
    assert "Delivery options for scheduled tasks:" in system_prompt
    assert "WebUI session context" in system_prompt
    assert "Source: webui" in system_prompt


def test_gateway_turn_without_personality_adds_nothing(tmp_path, monkeypatch):
    cfg = _cfg(dict(PERSONALITIES))
    _pin_config(monkeypatch, cfg)
    spy = []

    system_prompt, _ = _gateway_turn(tmp_path, monkeypatch, cfg, None, tag="plain", spy=spy)

    # No personality selected -> the worker passes None, exactly as before.
    assert spy[-1]["personality"] is None
    # And the prompt that went on the wire is precisely what the builder
    # returned for that None personality: nothing invented, nothing dropped.
    assert system_prompt == spy[-1]["prompt"]
    assert "Answer in one short sentence." not in system_prompt
    assert "Lead with the evidence." not in system_prompt


# ── embedded (in-process) worker ────────────────────────────────────────────

class _CapturingAgent:
    def __init__(self, model=None, provider=None, base_url=None, api_key=None,
                 api_mode=None, acp_command=None, acp_args=None,
                 credential_pool=None, platform=None, quiet_mode=False,
                 enabled_toolsets=None, fallback_model=None, session_id=None,
                 session_db=None, stream_delta_callback=None,
                 reasoning_callback=None, tool_progress_callback=None,
                 interim_assistant_callback=None, clarify_callback=None,
                 **kwargs):
        self.session_id = session_id
        self.context_compressor = None
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_estimated_cost_usd = None
        self.reasoning_config = None
        self.ephemeral_system_prompt = None
        self._last_error = None

    def run_conversation(self, **kwargs):
        return {
            "messages": [
                {"role": "user", "content": kwargs.get("persist_user_message", "")},
                {"role": "assistant", "content": "ok"},
            ]
        }

    def interrupt(self, _message):
        pass


class _FakeSession:
    session_id = "sess-8641-embedded"
    title = "Test"
    workspace = "/tmp"
    created_workspace = "/tmp"
    model = "gpt-4o"
    messages = []
    profile = None
    input_tokens = 0
    output_tokens = 0
    estimated_cost = None
    cache_read_tokens = 0
    cache_write_tokens = 0
    tool_calls = []
    active_stream_id = "stream-8641-embedded"
    pending_user_message = None
    pending_attachments = []
    pending_started_at = None
    enabled_toolsets = None

    def save(self, touch_updated_at=True, skip_index=True):
        pass

    def compact(self):
        return {"session_id": self.session_id, "personality": self.personality}

    @property
    def path(self):
        return "/tmp/fake-8641.json"


def _embedded_ephemeral_prompt(personality, cfg, tag="a"):
    """Run one embedded WebUI turn; return the agent's ephemeral system prompt."""
    captured = {}
    # A fresh session id per call: the SESSION_AGENT_CACHE would otherwise hand
    # back the previous test's agent (and its already-built prompt).
    from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK

    with SESSION_AGENT_CACHE_LOCK:
        SESSION_AGENT_CACHE.clear()

    class Agent(_CapturingAgent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["agent"] = self

    fake_session = _FakeSession()
    fake_session.session_id = f"sess-8641-embedded-{tag}"
    fake_session.active_stream_id = f"stream-8641-embedded-{tag}"
    fake_session.personality = personality

    fake_rt_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_rt_module.resolve_runtime_provider = mock.Mock(return_value={
        "provider": "openai-codex",
        "base_url": "https://api.openai.com/v1",
        "api_key": "rt-key",
        "api_mode": "codex_responses",
        "command": "codex",
        "args": ["exec", "--json"],
        "credential_pool": object(),
    })
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_rt_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = mock.Mock(return_value=object())

    with mock.patch.object(streaming, "get_session", return_value=fake_session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=Agent), \
         mock.patch.object(streaming, "resolve_model_provider",
                           return_value=("gpt-4o", "openai-codex", None)), \
         mock.patch("api.config.get_config", return_value=cfg), \
         mock.patch("api.config.get_config_for_profile_home", return_value=cfg), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]), \
         mock.patch.dict(sys.modules, {
             "hermes_cli": fake_hermes_cli,
             "hermes_cli.runtime_provider": fake_rt_module,
             "hermes_state": fake_hermes_state,
         }):
        streaming.STREAMS[fake_session.active_stream_id] = queue.Queue()
        streaming._run_agent_streaming(
            session_id=fake_session.session_id,
            msg_text="hello",
            model="gpt-4o",
            workspace="/tmp",
            stream_id=fake_session.active_stream_id,
        )

    return captured["agent"].ephemeral_system_prompt


def test_embedded_turn_carries_session_personality():
    cfg = _cfg(dict(PERSONALITIES))

    prompt = _embedded_ephemeral_prompt("analyst", cfg)

    assert "Lead with the evidence." in prompt
    assert "Tone: measured" in prompt
    assert "Style: bulleted" in prompt
    assert "UNUSED prompt-key fallback." not in prompt


def test_embedded_turn_without_personality_is_unchanged():
    cfg = _cfg(dict(PERSONALITIES))

    prompt = _embedded_ephemeral_prompt(None, cfg)

    assert "Lead with the evidence." not in prompt
    assert "WebUI progress guidance" in prompt
    assert "Final visible assistant replies" in prompt


def test_gateway_personality_is_not_duplicated_into_saved_history(tmp_path, monkeypatch):
    """The personality rides in the ephemeral system prompt, once, and only there.

    #8641 asks for the prompt to be removed when the selection is cleared
    "without duplicating it in saved history" -- so the persisted transcript
    must never carry the personality text.
    """
    cfg = _cfg(dict(PERSONALITIES))
    _pin_config(monkeypatch, cfg)
    spy = []

    system_prompt, session = _gateway_turn(tmp_path, monkeypatch, cfg, "terse", spy=spy)

    # Exactly once in the ephemeral system prompt the worker assembled.
    assert system_prompt.count("Answer in one short sentence.") == 1

    # ... and nowhere in the persisted transcript.
    persisted = " ".join(str(m) for m in (getattr(session, "messages", None) or []))
    assert "Answer in one short sentence." not in persisted

    saved = " ".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted((tmp_path / "sessions").glob("*.json"))
    )
    assert "Answer in one short sentence." not in saved
