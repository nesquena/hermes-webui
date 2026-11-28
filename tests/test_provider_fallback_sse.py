"""Focused coverage for the #6267 provider_fallback SSE contract.

Two proven producers feed the typed event (never string-inequality inference):
  1. Agent-side: the configured ``fallback_providers`` chain, detected from the
     agent's one-shot "Switched to fallback model: …" lifecycle notice.
  2. Gateway-side: LLM-gateway failover metadata with an ORDERED proof — a
     failed attempt matching the REQUESTED primary route, followed (strictly
     later in the list) by an explicit selected/success row matching the USED
     route.  Unrelated / out-of-order / no-selected rows fail closed.

Transition identity embeds an immutable per-stream/per-turn owner
(``stream_id``), so the same from→to route in two different turns never
collides (the per-request ``seq`` resets every turn).

Frontend: the event renders a localized composer/turn indicator guarded by
exact (session, stream) ownership — stale callbacks from a replaced
same-session stream are dropped — deduplicated by transition_id (replay-safe),
and re-asserted at ``done`` AFTER the generic idle cleanup so settlement cannot
erase it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from api.streaming import (
    _agent_fallback_cause,
    _bounded_fallback_reason,
    _build_agent_fallback_payload,
    _build_provider_fallback_sse_event,
    _fallback_transition_id,
    _is_fallback_switch_chatter,
    _is_fallback_switch_notice,
    _maybe_emit_agent_fallback_event,
)

REPO_ROOT = Path(__file__).parent.parent.resolve()
NODE = shutil.which("node")

MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _enable_fallback_sse(monkeypatch):
    """Keep the opt-out env var deterministic for every test in this file."""
    monkeypatch.setenv("HERMES_WEBUI_PROVIDER_FALLBACK_SSE", "1")


# ── Helpers ──────────────────────────────────────────────────────────────


class _FakeAgent:
    """Minimal agent double carrying the fields the producer reads."""

    def __init__(self, provider="", model="", buffer=None, fallback_cause=None):
        self.provider = provider
        self.model = model
        self._retry_status_buffer = buffer or []
        if fallback_cause is not None:
            self._fallback_cause = fallback_cause


def _noticed_state(provider="openai", model="gpt-4"):
    return {
        "seq": 0,
        "route_provider": provider,
        "route_model": model,
        "stream_id": "stream-1",
        "agent_fired": False,
    }


# ── Agent-side producer (configured fallback_providers chain) ────────────


def test_switch_notice_matcher_matches_only_the_one_shot_notice():
    assert _is_fallback_switch_notice(
        "lifecycle",
        "🔄 Switched to fallback model: gpt-4 via openai → opus via anthropic",
    )
    # Pre-switch chatter is NOT the authoritative transition signal.
    assert not _is_fallback_switch_notice("lifecycle", "🔄 Primary model failed — switching to fallback: opus via anthropic")
    assert not _is_fallback_switch_notice("lifecycle", "Rate limited, trying fallback…")
    assert not _is_fallback_switch_notice("warn", "🔄 Switched to fallback model: x via y → a via b")
    assert not _is_fallback_switch_notice("lifecycle", "")


def test_switch_notice_matcher_matches_current_agent_notice():
    current_notice = (
        "⚠️ Model fallback: gpt-4 via openai unavailable (rate limit exceeded); using opus via anthropic."
    )
    assert _is_fallback_switch_notice("lifecycle", current_notice)
    # Case-insensitive
    assert _is_fallback_switch_notice("lifecycle", current_notice.lower())
    assert _is_fallback_switch_notice("lifecycle", current_notice.upper())
    # Tolerant of leading emoji and whitespace variations
    assert _is_fallback_switch_notice("lifecycle", "   " + current_notice)
    assert _is_fallback_switch_notice(
        "lifecycle",
        "Model fallback: gpt-4 via openai unavailable (rate limit exceeded); using opus via anthropic.",
    )
    # Real cooldown suffix from chat_completion_helpers.py
    assert _is_fallback_switch_notice(
        "lifecycle",
        current_notice + " Primary retry eligible in ~60 s; recovery is not guaranteed.",
    )
    # Anchored structure: unrelated status rows must NOT match
    assert not _is_fallback_switch_notice("lifecycle", "Model fallback failed: gpt-4 via openai")
    assert not _is_fallback_switch_notice(
        "lifecycle",
        "Prefix row Model fallback: gpt-4 via openai unavailable (x); using y",
    )
    assert not _is_fallback_switch_notice("lifecycle", "⚠️ Model fallback: gpt-4 via openai unavailable")
    assert not _is_fallback_switch_notice("warn", current_notice)


def test_agent_producer_emits_current_agent_notice_with_route_and_reason():
    agent = _FakeAgent("anthropic", "opus")
    agent._provider_fallback_route = ("opus", "anthropic")
    state = _noticed_state()
    current_notice = (
        "⚠️ Model fallback: gpt-4 via openai unavailable (rate limit exceeded); using opus via anthropic."
    )
    payload = _maybe_emit_agent_fallback_event(
        "lifecycle",
        current_notice,
        agent,
        "sess-1",
        state,
        "stream-1",
    )
    assert payload is not None
    assert payload["session_id"] == "sess-1"
    assert payload["stream_id"] == "stream-1"
    assert payload["source"] == "agent"
    assert payload["from_provider"] == "openai"
    assert payload["from_model"] == "gpt-4"
    assert payload["to_provider"] == "anthropic"
    assert payload["to_model"] == "opus"
    assert payload["reason"] == "rate limit exceeded"
    assert len(payload["transition_id"]) == 16


def test_agent_producer_prefers_live_agent_attrs_over_stale_fallback_route():
    # Live agent attrs win over a stale route (rebound before callback, route set after)
    agent = _FakeAgent(provider="anthropic", model="opus")
    agent._provider_fallback_route = ("stale-model", "stale-provider")
    state = _noticed_state()
    current_notice = (
        "⚠️ Model fallback: gpt-4 via openai unavailable (provider failure); using opus via anthropic."
    )
    payload = _maybe_emit_agent_fallback_event(
        "lifecycle",
        current_notice,
        agent,
        "sess-1",
        state,
        "stream-1",
    )
    assert payload is not None
    assert payload["to_provider"] == "anthropic"
    assert payload["to_model"] == "opus"

    # Route attribute is still used when live attributes are empty
    empty_agent = _FakeAgent(provider="", model="")
    empty_agent._provider_fallback_route = ("opus", "anthropic")
    empty_state = _noticed_state()
    empty_payload = _maybe_emit_agent_fallback_event(
        "lifecycle",
        current_notice,
        empty_agent,
        "sess-1",
        empty_state,
        "stream-1",
    )
    assert empty_payload is not None
    assert empty_payload["to_provider"] == "anthropic"
    assert empty_payload["to_model"] == "opus"


def test_agent_producer_cause_prefers_structured_cause_over_notice_reason():
    agent = _FakeAgent(provider="anthropic", model="opus", fallback_cause="upstream 503")
    agent._provider_fallback_route = ("opus", "anthropic")
    state = _noticed_state()
    current_notice = (
        "⚠️ Model fallback: gpt-4 via openai unavailable (rate limit exceeded); using opus via anthropic."
    )
    payload = _maybe_emit_agent_fallback_event(
        "lifecycle",
        current_notice,
        agent,
        "sess-1",
        state,
        "stream-1",
    )
    assert payload is not None
    assert payload["reason"] == "upstream 503"


def test_agent_producer_emits_without_any_gateway_metadata():
    """The core review finding: a configured fallback with NO gateway metadata
    must still emit — the agent notice is the authoritative signal."""
    agent = _FakeAgent("anthropic", "opus", [("status", "⏳ Provider rate limit active — trying fallback")])
    state = _noticed_state()
    payload = _maybe_emit_agent_fallback_event(
        "lifecycle",
        "🔄 Switched to fallback model: gpt-4 via openai → opus via anthropic",
        agent,
        "sess-1",
        state,
        "stream-1",
    )
    assert payload is not None
    assert payload["session_id"] == "sess-1"
    assert payload["stream_id"] == "stream-1"
    assert payload["source"] == "agent"
    assert payload["from_provider"] == "openai"
    assert payload["from_model"] == "gpt-4"
    assert payload["to_provider"] == "anthropic"
    assert payload["to_model"] == "opus"
    assert payload["reason"] == "⏳ Provider rate limit active — trying fallback"
    assert len(payload["transition_id"]) == 16


def test_agent_payload_accepts_provider_only_and_model_only():
    provider_only = _build_agent_fallback_payload("s1", "stream-1", "openai", "gpt-4", "anthropic", "", "boom", 1)
    assert provider_only is not None
    assert provider_only["to_provider"] == "anthropic"
    assert provider_only["to_model"] == ""

    model_only = _build_agent_fallback_payload("s1", "stream-1", "openai", "gpt-4", "", "opus", "boom", 2)
    assert model_only is not None
    assert model_only["to_provider"] == ""
    assert model_only["to_model"] == "opus"


def test_agent_payload_same_route_and_empty_destination_return_none():
    assert _build_agent_fallback_payload("s1", "stream-1", "openai", "gpt-4", "openai", "gpt-4", "x", 1) is None
    assert _build_agent_fallback_payload("s1", "stream-1", "openai", "gpt-4", "", "", "x", 1) is None
    # Alias/no-op: case-only difference is not a fallback.
    assert _build_agent_fallback_payload("s1", "stream-1", "OpenAI", "GPT-4", "openai", "gpt-4", "x", 1) is None


def test_agent_fallback_cause_uses_last_buffered_status_line():
    agent = _FakeAgent(buffer=[
        ("vprint", "ignored debug line"),
        ("status", "⏳ Nous rate limit active — resets in 60s"),
        ("warn", "primary attempt failed: HTTP 429"),
    ])
    assert _agent_fallback_cause(agent) == "primary attempt failed: HTTP 429"
    assert _agent_fallback_cause(_FakeAgent(buffer=[])) == ""
    assert _agent_fallback_cause(_FakeAgent(buffer=[("vprint", "only debug")])) == ""


def test_agent_fallback_cause_skips_switch_chatter_final_row():
    """The real agent appends the generic ``Primary model failed — switching
    to fallback …`` status LAST; that switch row must NOT win over the actual
    preceding failure."""
    agent = _FakeAgent(buffer=[
        ("status", "⏳ Nous rate limit active — resets in 60s"),
        ("warn", "primary attempt failed: HTTP 429"),
        ("status", "🔄 Primary model failed — switching to fallback: opus via anthropic"),
    ])
    assert _agent_fallback_cause(agent) == "primary attempt failed: HTTP 429"
    # A buffer of ONLY switch chatter yields no cause at all.
    assert _agent_fallback_cause(_FakeAgent(buffer=[
        ("status", "🔄 Switched to fallback model: x via y → a via b"),
        ("status", "🔄 Primary model failed — switching to fallback: opus via anthropic"),
    ])) == ""
    assert _is_fallback_switch_chatter("🔄 Primary model failed — switching to fallback: opus via anthropic")
    assert _is_fallback_switch_chatter("🔄 Switched to fallback model: x via y → a via b")
    assert not _is_fallback_switch_chatter("⏳ Nous rate limit active — resets in 60s")
    assert not _is_fallback_switch_chatter("primary attempt failed: HTTP 429")
    # CAUSE-BEARING rows also contain the switch phrase but are the actual
    # failed-primary cause — they must be preserved, not suppressed.
    assert not _is_fallback_switch_chatter("⚠️ Empty/malformed response — switching to fallback...")
    assert not _is_fallback_switch_chatter("⚠️ Upstream openai rate-limited — switching to fallback model...")
    assert not _is_fallback_switch_chatter("⚠️ Billing or credits exhausted — switching to fallback provider...")
    assert not _is_fallback_switch_chatter("⚠️ Provider unreachable — switching to fallback provider...")
    assert not _is_fallback_switch_chatter("⚠️ Primary auth failed — switching to fallback: deepseek / deepseek-v3")
    assert not _is_fallback_switch_chatter("⚠️ Model returning empty responses — switching to fallback provider...")
    assert not _is_fallback_switch_chatter("Content filter terminated stream; switching to fallback...")


def test_agent_fallback_cause_real_buffer_cause_rows_win():
    """Exact current-Agent buffer shapes: the cause-bearing row precedes the
    generic final switch row and MUST win — only the generic control row is
    suppressed, never the cause that explains the failed primary."""
    cases = [
        "⚠️ Empty/malformed response — switching to fallback...",
        "⚠️ Upstream openai rate-limited — switching to fallback model...",
        "⚠️ Billing or credits exhausted — switching to fallback provider...",
        "⚠️ Provider unreachable — switching to fallback provider...",
        "⚠️ Primary auth failed — switching to fallback: deepseek / deepseek-v3",
        "⚠️ Model returning empty responses — switching to fallback provider...",
        "Content filter terminated stream; switching to fallback...",
    ]
    for cause in cases:
        agent = _FakeAgent(buffer=[
            ("status", cause),
            ("status", "🔄 Primary model failed — switching to fallback: opus via anthropic"),
        ])
        assert _agent_fallback_cause(agent) == cause, cause


def test_agent_fallback_cause_prefers_structured_cause():
    """A structured failed-primary cause on the agent is authoritative when
    present — the buffered trace is only the fallback path."""
    agent = _FakeAgent(
        buffer=[("status", "🔄 Primary model failed — switching to fallback: opus via anthropic")],
        fallback_cause="HTTP 503 upstream unavailable",
    )
    assert _agent_fallback_cause(agent) == "HTTP 503 upstream unavailable"


def test_agent_producer_tracks_chain_switch_prior_route():
    agent = _FakeAgent("anthropic", "opus", [("status", "primary failed")])
    state = _noticed_state()
    first = _maybe_emit_agent_fallback_event(
        "lifecycle",
        "🔄 Switched to fallback model: gpt-4 via openai → opus via anthropic",
        agent, "sess-1", state, "stream-1",
    )
    assert first["from_provider"] == "openai"
    assert state["agent_fired"] is True
    # Chain switch: the agent falls back AGAIN within the same turn.
    agent.provider = "deepseek"
    agent.model = "deepseek-v3"
    second = _maybe_emit_agent_fallback_event(
        "lifecycle",
        "🔄 Switched to fallback model: opus via anthropic → deepseek-v3 via deepseek",
        agent, "sess-1", state, "stream-1",
    )
    assert second is not None
    assert second["from_provider"] == "anthropic"  # tracked, not the original primary
    assert second["to_provider"] == "deepseek"
    assert second["transition_id"] != first["transition_id"]


def test_agent_producer_ignores_non_notice_status():
    state = _noticed_state()
    assert _maybe_emit_agent_fallback_event("lifecycle", "Rate limited, trying fallback…", _FakeAgent("a", "b"), "s1", state, "stream-1") is None
    assert state["seq"] == 0  # nothing consumed
    assert state["agent_fired"] is False


def test_agent_producer_opt_out_env_var_suppresses_event(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PROVIDER_FALLBACK_SSE", "0")
    state = _noticed_state()
    payload = _maybe_emit_agent_fallback_event(
        "lifecycle",
        "🔄 Switched to fallback model: gpt-4 via openai → opus via anthropic",
        _FakeAgent("anthropic", "opus"), "s1", state, "stream-1",
    )
    assert payload is None


def _run_worker_turn(notice_message, *, route_tuple=None, cause=None, notice_kind="lifecycle"):
    import queue
    import sys
    import types
    from unittest import mock
    from api import streaming
    from api.config import SESSION_AGENT_CACHE

    SESSION_AGENT_CACHE.clear()

    class Session:
        session_id = "sess-1"
        title = "Session"
        workspace = "/tmp"
        model = "gpt-4"
        model_provider = "openai"
        profile = None
        personality = None
        messages = [{"role": "user", "content": "hello"}]
        context_messages = messages
        input_tokens = output_tokens = cache_read_tokens = cache_write_tokens = 0
        estimated_cost = 0.0
        tool_calls = []
        gateway_routing = None
        gateway_routing_history = []
        active_stream_id = "stream-1"
        pending_user_message = None
        pending_attachments = []
        pending_started_at = None
        context_length = threshold_tokens = last_prompt_tokens = 0
        llm_title_generated = True

        def save(self, *a, **k):
            pass

        def compact(self):
            return {"session_id": self.session_id}

    class TestAgent:
        def __init__(self, model=None, provider=None, session_id=None,
                     stream_delta_callback=None, reasoning_callback=None,
                     status_callback=None, **kwargs):
            self.model = model or "gpt-4"
            self.provider = provider or "openai"
            self.session_id = session_id
            self.status_callback = status_callback
            self._provider_fallback_active = False
            self.context_compressor = None
            self.session_prompt_tokens = self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = self.session_cache_write_tokens = 0
            self.reasoning_config = self.ephemeral_system_prompt = self._last_error = None
            if route_tuple is not None:
                self._provider_fallback_route = route_tuple
            if cause is not None:
                self._fallback_cause = cause

        def run_conversation(self, **kwargs):
            if route_tuple is not None:
                self.model = route_tuple[0]
                self.provider = route_tuple[1]
                self._provider_fallback_route = route_tuple
            else:
                self.model = "opus"
                self.provider = "anthropic"
            self._provider_fallback_active = True
            if self.status_callback:
                self.status_callback(notice_kind, notice_message)
            return {
                "messages": kwargs.get("conversation_history", []) + [
                    {"role": "user", "content": kwargs.get("persist_user_message", "hi")},
                    {"role": "assistant", "content": "done"},
                ]
            }

        def interrupt(self, message):
            pass

    runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    runtime_module.resolve_runtime_provider = mock.Mock(return_value={
        "provider": "anthropic", "base_url": None, "api_key": "sk-test",
        "api_mode": "chat_completions", "command": None, "args": [], "credential_pool": None,
    })
    cli_module = types.ModuleType("hermes_cli")
    cli_module.runtime_provider = runtime_module
    state_module = types.ModuleType("hermes_state")
    state_module.SessionDB = mock.Mock(return_value=None)
    injected = {
        "hermes_cli": cli_module,
        "hermes_cli.runtime_provider": runtime_module,
        "hermes_state": state_module,
    }
    sentinel = object()
    saved = {name: sys.modules.get(name, sentinel) for name in injected}
    sys.modules.update(injected)

    q = queue.Queue()
    s = Session()
    try:
        with mock.patch.object(streaming, "get_session", return_value=s), \
             mock.patch.object(streaming, "_get_ai_agent", return_value=TestAgent), \
             mock.patch.object(streaming, "resolve_model_provider", return_value=("gpt-4", "openai", None)), \
             mock.patch("api.config.get_config", return_value={}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]), \
             mock.patch("api.process_event_utils._restore_ledger", return_value=None):
            streaming.STREAMS["stream-1"] = q
            streaming._run_agent_streaming("sess-1", "hi", "gpt-4", "/tmp", "stream-1", ephemeral=True)
    finally:
        for name, orig in saved.items():
            if orig is sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = orig
        streaming.STREAMS.pop("stream-1", None)
        SESSION_AGENT_CACHE.clear()

    return list(q.queue)


def test_agent_event_exactly_once_and_ordered_before_terminal_events():
    """Drive the real producer for both notices so ordering and exactly-once
    are proven on emitted events before the turn's terminal done event."""
    notices = [
        (
            "🔄 Switched to fallback model: gpt-4 via openai → opus via anthropic",
            None,
            "anthropic",
            "opus",
            "",
        ),
        (
            "⚠️ Model fallback: gpt-4 via openai unavailable (rate limit exceeded); using opus via anthropic.",
            ("opus", "anthropic"),
            "anthropic",
            "opus",
            "rate limit exceeded",
        ),
    ]
    for notice, route, expected_provider, expected_model, expected_reason in notices:
        events = _run_worker_turn(notice, route_tuple=route)
        names = [e[0] for e in events]
        assert "provider_fallback" in names, f"provider_fallback not emitted for notice: {notice}"
        assert "done" in names
        assert names.count("provider_fallback") == 1
        assert names.index("provider_fallback") < names.index("done")
        payload = next(data for name, data in events if name == "provider_fallback")
        assert payload["source"] == "agent"
        assert payload["to_provider"] == expected_provider
        assert payload["to_model"] == expected_model
        if expected_reason:
            assert payload["reason"] == expected_reason


def test_agent_producer_marks_agent_fired_for_dual_producer_guard():
    """After an agent-side emit the gateway producer must skip (one physical
    fallback visible in BOTH the agent chain and gateway metadata must not
    double-notify the UI with two transition ids)."""
    agent = _FakeAgent("anthropic", "opus", [("status", "primary failed: HTTP 429")])
    state = _noticed_state()
    payload = _maybe_emit_agent_fallback_event(
        "lifecycle",
        "🔄 Switched to fallback model: gpt-4 via openai → opus via anthropic",
        agent, "sess-1", state, "stream-1",
    )
    assert payload is not None
    assert state["agent_fired"] is True
    # A second notice within the same turn (chain switch) still emits.
    agent.provider = "deepseek"
    agent.model = "deepseek-v3"
    second = _maybe_emit_agent_fallback_event(
        "lifecycle",
        "🔄 Switched to fallback model: opus via anthropic → deepseek-v3 via deepseek",
        agent, "sess-1", state, "stream-1",
    )
    assert second is not None
    assert second["transition_id"] != payload["transition_id"]


# ── Gateway-side producer (proven LLM-gateway failover) ──────────────────


def _gateway_metadata(used_provider="Alibaba Cloud", used_model="deepseek-v3.2",
                      req_provider="CanopyWave", req_model="deepseek-v3.2",
                      routing=None, extra=None):
    meta = {
        "used_provider": used_provider,
        "used_model": used_model,
        "requested_provider": req_provider,
        "requested_model": req_model,
    }
    if routing is not None:
        meta["routing"] = routing
    if extra:
        meta.update(extra)
    return meta


def _build(meta, seq=1, session_id="s1", stream_id="stream-1"):
    return _build_provider_fallback_sse_event(
        meta,
        session_id=session_id,
        stream_id=stream_id,
        seq=seq,
    )


def test_gateway_event_requires_explicit_failed_primary():
    # Plain requested-vs-used mismatch with no failed attempt = NOT a fallback.
    no_failure = _gateway_metadata(routing=[
        {"provider": "CanopyWave", "status": "selected"},
    ])
    assert _build(no_failure) is None
    # has_failover flag alone (string-mismatch driven) is not sufficient.
    flagged = _gateway_metadata(extra={"has_failover": True})
    assert _build(flagged) is None
    # Ordered proof: failed requested primary → selected row matching the used
    # route → proven failover.
    proven = _gateway_metadata(routing=[
        {"provider": "CanopyWave", "status": "failed", "error": "connection timeout"},
        {"provider": "Alibaba Cloud", "status": "selected"},
    ])
    payload = _build(proven)
    assert payload is not None
    assert payload["source"] == "gateway"
    assert payload["stream_id"] == "stream-1"
    assert payload["from_provider"] == "CanopyWave"
    assert payload["to_provider"] == "Alibaba Cloud"
    assert payload["reason"] == "connection timeout"
    assert payload["transition_id"] == _fallback_transition_id(
        "s1", "stream-1", "CanopyWave", "deepseek-v3.2",
        "Alibaba Cloud", "deepseek-v3.2", 1,
    )


def test_gateway_event_failed_primary_reason_selection():
    """Reason comes from the PROVEN failed primary row, not the last routing
    entry (which may be the successful fallback's rationale)."""
    routing = [
        {"provider": "CanopyWave", "status": "failed", "error": "primary rejected: 401"},
        {"provider": "DeepInfra", "status": "failed", "error": "secondary also failed"},
        {"provider": "Alibaba Cloud", "status": "selected"},
    ]
    payload = _build(_gateway_metadata(routing=routing))
    assert payload["reason"] == "primary rejected: 401"
    # reason-only attempts (no error field) are honored too.
    routing2 = [
        {"provider": "CanopyWave", "status": "timeout", "reason": "upstream timeout"},
        {"provider": "Alibaba Cloud", "status": "selected"},
    ]
    payload2 = _build(_gateway_metadata(routing=routing2))
    assert payload2["reason"] == "upstream timeout"


def test_gateway_event_unrelated_failure_fails_closed():
    """The failed row must be the REQUESTED primary — an unrelated failure
    (different provider/model) is not proof of fallback even with a selected
    row matching the used route."""
    unrelated = _gateway_metadata(routing=[
        {"provider": "SomeOtherProvider", "status": "failed", "error": "unrelated outage"},
        {"provider": "Alibaba Cloud", "status": "selected"},
    ])
    assert _build(unrelated) is None
    # A failed row with a DIFFERENT model than requested is not the primary.
    wrong_model = _gateway_metadata(routing=[
        {"provider": "CanopyWave", "model": "claude-3-5-sonnet", "status": "failed", "error": "x"},
        {"provider": "Alibaba Cloud", "status": "selected"},
    ])
    assert _build(wrong_model) is None


def test_gateway_event_out_of_order_selection_fails_closed():
    """Selection must happen STRICTLY AFTER the proven primary failure — a
    selected row listed before the failed row is not a fallback."""
    out_of_order = _gateway_metadata(routing=[
        {"provider": "Alibaba Cloud", "status": "selected"},
        {"provider": "CanopyWave", "status": "failed", "error": "connection timeout"},
    ])
    assert _build(out_of_order) is None


def test_gateway_event_no_selected_row_fails_closed():
    """Failed primary with NO selected/success row matching the used route —
    and a selected row that does NOT match the used route — are not proof."""
    no_selected = _gateway_metadata(used_provider="Alibaba Cloud", routing=[
        {"provider": "CanopyWave", "status": "failed", "error": "connection timeout"},
        {"provider": "DeepInfra", "status": "failed", "error": "secondary failed"},
    ])
    assert _build(no_selected) is None
    mismatched_selection = _gateway_metadata(used_provider="Alibaba Cloud", routing=[
        {"provider": "CanopyWave", "status": "failed", "error": "x"},
        {"provider": "SomeOtherProvider", "status": "selected"},
    ])
    assert _build(mismatched_selection) is None


def test_gateway_event_selected_flag_counts_as_selection():
    """A row marked with a truthy ``selected`` flag (no status) is a valid
    selection proof when it matches the used route."""
    meta = _gateway_metadata(routing=[
        {"provider": "CanopyWave", "status": "failed", "error": "boom"},
        {"provider": "Alibaba Cloud", "selected": True},
    ])
    payload = _build(meta)
    assert payload is not None
    assert payload["to_provider"] == "Alibaba Cloud"
    assert payload["reason"] == "boom"


def test_gateway_event_same_route_and_alias_are_no_fallback():
    failed = [{"provider": "CanopyWave", "status": "failed", "error": "x"}]
    # Same route after failure = recovery, not fallback.
    same = _gateway_metadata(used_provider="CanopyWave", used_model="deepseek-v3.2", routing=failed)
    assert _build(same) is None
    # Case-only alias difference is not a fallback.
    alias = _gateway_metadata(used_provider="canopywave", used_model="DeepSeek-V3.2", routing=failed)
    assert _build(alias) is None


def test_gateway_event_provider_only_and_model_only():
    failed_primary = {"provider": "CanopyWave", "status": "failed", "error": "x"}
    provider_only = _gateway_metadata(used_provider="Alibaba Cloud", used_model="", routing=[
        failed_primary,
        {"provider": "Alibaba Cloud", "status": "selected"},
    ])
    payload = _build(provider_only)
    assert payload is not None and payload["to_model"] == ""
    model_only = _gateway_metadata(used_provider="CanopyWave", used_model="other-model", routing=[
        failed_primary,
        {"provider": "CanopyWave", "model": "other-model", "status": "selected"},
    ])
    payload2 = _build(model_only)
    assert payload2 is not None
    assert payload2["to_provider"] == "CanopyWave"
    assert payload2["to_model"] == "other-model"


def test_gateway_event_malformed_metadata_returns_none():
    assert _build(None) is None
    assert _build({}) is None
    assert _build("garbage") is None
    # routing not a list / entries not dicts
    bad = _gateway_metadata(routing="not-a-list")
    assert _build(bad) is None
    bad2 = _gateway_metadata(routing=["x", None, 42])
    assert _build(bad2) is None
    # failed attempts but no used route at all
    no_route = _gateway_metadata(used_provider="", used_model="", routing=[
        {"provider": "CanopyWave", "status": "failed", "error": "x"},
    ])
    assert _build(no_route) is None


# ── Shared payload hygiene ────────────────────────────────────────────────


def test_reason_is_bounded_cleaned_and_redacted():
    long_reason = "x" * 500
    assert len(_bounded_fallback_reason(long_reason)) == 240
    messy = "line1\nline2\t\ttabbed   spaced\rmixed"
    assert _bounded_fallback_reason(messy) == "line1 line2 tabbed spaced mixed"
    assert _bounded_fallback_reason("") == ""
    assert _bounded_fallback_reason(None) == ""
    # Control characters are collapsed, never emitted raw.
    assert _bounded_fallback_reason("a\x00b\x1fc") == "a b c"


def test_transition_id_deterministic_and_per_occurrence():
    a = _fallback_transition_id("s1", "stream-1", "openai", "gpt", "anthropic", "opus", 1)
    b = _fallback_transition_id("s1", "stream-1", "openai", "gpt", "anthropic", "opus", 1)
    assert a == b and len(a) == 16
    assert a != _fallback_transition_id("s1", "stream-1", "openai", "gpt", "anthropic", "opus", 2)  # next occurrence
    assert a != _fallback_transition_id("s2", "stream-1", "openai", "gpt", "anthropic", "opus", 1)  # other session
    assert a != _fallback_transition_id("s1", "stream-1", "openai", "gpt", "anthropic", "opus-2", 1)  # other route


def test_transition_id_embeds_per_stream_owner():
    """Two turns with the SAME first from→to fallback must NOT collide: the
    per-stream/per-turn owner is immutable while ``seq`` resets every turn."""
    turn1 = _fallback_transition_id("s1", "stream-1", "openai", "gpt-4", "anthropic", "opus", 1)
    turn2 = _fallback_transition_id("s1", "stream-2", "openai", "gpt-4", "anthropic", "opus", 1)
    assert turn1 != turn2


# ── Frontend: visible UI behavior (Node driver on the REAL functions) ─────

_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

// Harness globals mirroring the production MODULE scope of messages.js:
// LIVE_STREAMS and the dedup maps are SHARED across all live-stream
// closures; only the activeSid/streamId bindings are per-closure.
const LIVE_STREAMS = {};
let _providerFallbackRenderedMap = {};
let _providerFallbackReassertedMap = {};
let _providerFallbackRenderCount = 0;
const S = { session: { session_id: 'sess-A' } };
let _lastComposerStatus = '';
let _lastComposerDuration = undefined;
let _lastToast = null;
function setComposerStatus(x, duration) {
  _lastComposerStatus = x;
  _lastComposerDuration = duration;
}
function showToast(m, ms, type) { _lastToast = { m: m, ms: ms, type: type }; }
function t(k, ...a) { return k === 'provider_fallback_status' ? ('⚠️ Fell back to ' + a[0]) : k; }

// Production functions extracted LIVE from messages.js — not test stand-ins.
const _labelSrc = extractFunc('_providerFallbackLabel');
const _renderSrc = extractFunc('_renderProviderFallbackIndicator');
const _reassertSrc = extractFunc('_reassertProviderFallbackIndicator');

// Production-shaped closure: attachLiveStream(activeSid, streamId) captures
// per-stream bindings while LIVE_STREAMS + dedup maps stay shared module
// state.  Two makeEnv() calls model two live EventSource closures (an old
// stream and its same-session replacement).
function makeEnv(activeSidVal, streamIdVal) {
  const activeSid = activeSidVal;
  const streamId = streamIdVal;
  eval(_labelSrc);
  eval(_renderSrc);
  eval(_reassertSrc);
  function idleCleanup() {
    if (S.session && S.session.session_id === activeSid) {
      setComposerStatus('');
    }
  }
  return {
    render: _renderProviderFallbackIndicator,
    reassert: _reassertProviderFallbackIndicator,
    idleCleanup: idleCleanup,
  };
}

function assert(c, m) { if (!c) throw new Error(m || 'assertion failed'); }
const scenario = process.argv[3];

function resetState() {
  _providerFallbackRenderedMap = {};
  _providerFallbackReassertedMap = {};
  _providerFallbackRenderCount = 0;
  _lastComposerStatus = '';
  _lastComposerDuration = undefined;
  _lastToast = null;
  S.session = { session_id: 'sess-A' };
  for (const k of Object.keys(LIVE_STREAMS)) delete LIVE_STREAMS[k];
}
function setLive(sid, streamId) { LIVE_STREAMS[sid] = { streamId: String(streamId) }; }

if (scenario === 'render') {
  resetState();
  setLive('sess-A', 'stream-1');
  const env = makeEnv('sess-A', 'stream-1');
  env.render({ session_id: 'sess-A', stream_id: 'stream-1', transition_id: 't1', source: 'agent', from_provider: 'openai', from_model: 'gpt-4', to_provider: 'anthropic', to_model: 'opus', reason: 'rate limited' });
  assert(_lastComposerStatus === '⚠️ Fell back to opus via anthropic', 'composer status: ' + _lastComposerStatus);
  assert(_lastToast && _lastToast.m === 'rate limited' && _lastToast.type === 'warning', 'toast not shown');
  assert(_providerFallbackRenderedMap['t1'], 'transition not recorded');
  assert(_providerFallbackRenderedMap['t1'].streamId === 'stream-1', 'stream owner not recorded');
  assert(_providerFallbackRenderedMap['t1'].source === 'agent', 'source not recorded');
  assert(_providerFallbackReassertedMap['sess-A'].tid === 't1', 'reassert record missing');
  assert(_providerFallbackReassertedMap['sess-A'].streamId === 'stream-1', 'reassert stream missing');
  process.stdout.write('PASS render\n');
} else if (scenario === 'ownership') {
  resetState();
  setLive('sess-A', 'stream-1');
  const env = makeEnv('sess-A', 'stream-1');
  env.render({ session_id: 'sess-B', stream_id: 'stream-1', transition_id: 't1', to_provider: 'anthropic' });
  assert(_lastComposerStatus === '', 'foreign session rendered');
  assert(Object.keys(_providerFallbackRenderedMap).length === 0, 'foreign session recorded');
  // Same session, STALE stream (callback from a replaced same-session stream).
  env.render({ session_id: 'sess-A', stream_id: 'stream-OLD', transition_id: 't2', to_provider: 'anthropic' });
  assert(_lastComposerStatus === '', 'stale stream rendered');
  assert(Object.keys(_providerFallbackRenderedMap).length === 0, 'stale stream recorded');
  S.session = { session_id: 'sess-OTHER' };
  env.render({ session_id: 'sess-A', stream_id: 'stream-1', transition_id: 't3', to_provider: 'anthropic' });
  assert(_lastComposerStatus === '', 'S.session mismatch rendered');
  assert(Object.keys(_providerFallbackRenderedMap).length === 0, 'mismatch recorded');
  S.session = { session_id: 'sess-A' };
  process.stdout.write('PASS ownership\n');
} else if (scenario === 'model_only') {
  resetState();
  setLive('sess-A', 'stream-1');
  const env = makeEnv('sess-A', 'stream-1');
  env.render({ session_id: 'sess-A', stream_id: 'stream-1', transition_id: 't2', from_model: 'gpt-4', to_model: 'opus', reason: 'model swapped' });
  assert(_lastComposerStatus === '⚠️ Fell back to opus', 'model-only status: ' + _lastComposerStatus);
  assert(_lastToast && _lastToast.m === 'model swapped', 'model-only toast missing');
  process.stdout.write('PASS model_only\n');
} else if (scenario === 'provider_only') {
  resetState();
  setLive('sess-A', 'stream-1');
  const env = makeEnv('sess-A', 'stream-1');
  env.render({ session_id: 'sess-A', stream_id: 'stream-1', transition_id: 't3', from_provider: 'openai', to_provider: 'anthropic' });
  assert(_lastComposerStatus === '⚠️ Fell back to anthropic', 'provider-only status: ' + _lastComposerStatus);
  process.stdout.write('PASS provider_only\n');
} else if (scenario === 'dedup_replay') {
  resetState();
  setLive('sess-A', 'stream-1');
  const env = makeEnv('sess-A', 'stream-1');
  const d = { session_id: 'sess-A', stream_id: 'stream-1', transition_id: 't1', to_provider: 'anthropic' };
  env.render(d);
  assert(_lastComposerStatus === '⚠️ Fell back to anthropic', 'first render');
  _lastComposerStatus = '';
  // SSE snapshot replay re-delivers the SAME transition → must not re-render.
  env.render(d);
  assert(_lastComposerStatus === '', 'replay re-rendered');
  assert(Object.keys(_providerFallbackRenderedMap).length === 1, 'map grew on replay');
  // A NEW transition in a later turn still renders (map is keyed by id).
  env.render({ session_id: 'sess-A', stream_id: 'stream-1', transition_id: 't2', to_model: 'opus' });
  assert(_lastComposerStatus === '⚠️ Fell back to opus', 'new transition suppressed');
  process.stdout.write('PASS dedup_replay\n');
} else if (scenario === 'done_reassert') {
  resetState();
  setLive('sess-A', 'stream-1');
  const env = makeEnv('sess-A', 'stream-1');
  env.render({ session_id: 'sess-A', stream_id: 'stream-1', transition_id: 't1', to_provider: 'anthropic' });
  _lastComposerStatus = '';
  _lastComposerDuration = undefined;
  env.reassert('sess-A', 'stream-1');
  assert(_lastComposerStatus === '⚠️ Fell back to anthropic', 'done reassert failed: ' + _lastComposerStatus);
  assert(_lastComposerDuration === 4000, 'done reassert duration: ' + _lastComposerDuration);
  assert(Object.keys(_providerFallbackRenderedMap).length === 1, 'reassert grew map');
  _lastComposerStatus = '';
  env.reassert('sess-UNKNOWN', 'stream-1');
  assert(_lastComposerStatus === '', 'unknown session reasserted');
  env.reassert('sess-A', 'stream-OLD');
  assert(_lastComposerStatus === '', 'stale stream reasserted');
  // Pane ownership fence: if another stream owns the pane, do not write
  setLive('sess-A', 'stream-OTHER');
  _lastComposerStatus = '';
  env.reassert('sess-A', 'stream-1');
  assert(_lastComposerStatus === '', 'reassert wrote when another stream owns pane');
  setLive('sess-A', 'stream-1');
  // Compression rotation: done delivers completedSid for the rotated session,
  // while the record was saved under activeSid ('sess-A'). When viewed, the
  // indicator must survive settlement; in background, it must write nothing.
  _lastComposerStatus = '';
  _lastComposerDuration = undefined;
  S.session = { session_id: 'sess-A-rotated' };
  env.reassert('sess-A-rotated', 'stream-1');
  assert(_lastComposerStatus === '⚠️ Fell back to anthropic', 'compression rotation dropped indicator');
  assert(_lastComposerDuration === 4000, 'compression rotation reassert missing 4000ms bound');
  // Rotated session in background must write nothing:
  _lastComposerStatus = '';
  S.session = { session_id: 'sess-OTHER' };
  env.reassert('sess-A-rotated', 'stream-1');
  assert(_lastComposerStatus === '', 'background rotated settlement wrote into foreground');
  S.session = { session_id: 'sess-A' };
  process.stdout.write('PASS done_reassert\n');
} else if (scenario === 'stale_stream') {
  // Production-shaped regression: replacement stream for the SAME session.
  // TWO live closures (old EventSource → stream-1, replacement → stream-2)
  // share the module-level LIVE_STREAMS owner map.  A delayed stream-1 event
  // delivered through the OLD closure (whose own streamId is still 'stream-1')
  // must NOT render into the replacement — an event-vs-closure comparison
  // alone would false-green — and stream-2 must still render and reassert.
  resetState();
  setLive('sess-A', 'stream-1');
  const oldEnv = makeEnv('sess-A', 'stream-1');   // old EventSource closure
  const newEnv = makeEnv('sess-A', 'stream-2');   // replacement closure
  setLive('sess-A', 'stream-2');                  // attachLiveStream replaced the owner
  oldEnv.render({ session_id: 'sess-A', stream_id: 'stream-1', transition_id: 't-old', to_provider: 'anthropic' });
  assert(_lastComposerStatus === '', 'stale closure rendered into replacement: ' + _lastComposerStatus);
  assert(Object.keys(_providerFallbackRenderedMap).length === 0, 'stale callback recorded');
  assert(!_providerFallbackReassertedMap['sess-A'], 'stale callback poisoned reassert');
  newEnv.render({ session_id: 'sess-A', stream_id: 'stream-2', transition_id: 't-new', to_provider: 'anthropic' });
  assert(_lastComposerStatus === '⚠️ Fell back to anthropic', 'replacement stream event dropped');
  _lastComposerStatus = '';
  newEnv.reassert('sess-A', 'stream-2');
  assert(_lastComposerStatus === '⚠️ Fell back to anthropic', 'replacement reassert failed');
  process.stdout.write('PASS stale_stream\n');
} else if (scenario === 'done_cleanup_order') {
  // Production `done` sequence: render → generic idle cleanup (what
  // `_setActivePaneIdleIfOwner()` runs: setBusy(false)+setComposerStatus(''))
  // → reassert AFTER the cleanup. The reassert must restore the indicator.
  resetState();
  setLive('sess-A', 'stream-1');
  setLive('sess-B', 'stream-2');
  const envA = makeEnv('sess-A', 'stream-1');
  const envB = makeEnv('sess-B', 'stream-2');
  // Second pane (sess-B) recorded a fallback when viewed
  S.session = { session_id: 'sess-B' };
  envB.render({ session_id: 'sess-B', stream_id: 'stream-2', transition_id: 'tb', to_provider: 'anthropic' });
  // User switches back to sess-A
  S.session = { session_id: 'sess-A' };
  envA.render({ session_id: 'sess-A', stream_id: 'stream-1', transition_id: 't1', to_provider: 'anthropic' });
  assert(_lastComposerStatus === '⚠️ Fell back to anthropic', 'foreground render');
  // Idle cleanup for sess-A clears the foreground composer
  envA.idleCleanup();
  assert(_lastComposerStatus === '', 'precondition: idle cleanup cleared status');
  // Background session sess-B settles: its idle cleanup + reassert must NOT touch foreground composer
  envB.idleCleanup();
  envB.reassert('sess-B', 'stream-2');
  assert(_lastComposerStatus === '', 'background settlement wrote into foreground composer');
  // Foreground session sess-A settles: reassert restores indicator with 4000ms bound
  envA.reassert('sess-A', 'stream-1');
  assert(_lastComposerStatus === '⚠️ Fell back to anthropic', 'indicator wiped by idle cleanup: ' + _lastComposerStatus);
  assert(_lastComposerDuration === 4000, 'reassert missing 4000ms bound: ' + _lastComposerDuration);
  envA.reassert('sess-A', 'stream-1');
  assert(_lastComposerStatus === '⚠️ Fell back to anthropic', 'reassert changed text');
  assert(_lastComposerDuration === 4000, 'reassert missing 4000ms bound');
  process.stdout.write('PASS done_cleanup_order\n');
} else if (scenario === 'two_turns_same_route') {
  // Two turns, SAME session and SAME from→to route, DIFFERENT streams: the
  // per-stream owner in the transition id keeps the second turn visible
  // (the per-request seq resets each turn and alone would collide).
  resetState();
  setLive('sess-A', 'stream-1');
  const envTurn1 = makeEnv('sess-A', 'stream-1');
  envTurn1.render({ session_id: 'sess-A', stream_id: 'stream-1', transition_id: 'tid-turn-1', to_provider: 'anthropic' });
  assert(_lastComposerStatus === '⚠️ Fell back to anthropic', 'turn 1 dropped');
  _lastComposerStatus = '';
  setLive('sess-A', 'stream-2');
  const envTurn2 = makeEnv('sess-A', 'stream-2');
  envTurn2.render({ session_id: 'sess-A', stream_id: 'stream-2', transition_id: 'tid-turn-2', to_provider: 'anthropic' });
  assert(_lastComposerStatus === '⚠️ Fell back to anthropic', 'turn 2 (same route) suppressed by dedup');
  assert(Object.keys(_providerFallbackRenderedMap).length === 2, 'two transitions recorded');
  process.stdout.write('PASS two_turns_same_route\n');
} else {
  throw new Error('unknown scenario ' + scenario);
}
"""


@pytest.fixture(scope="module")
def fe_driver_path(tmp_path_factory):
    if NODE is None:
        return None
    path = tmp_path_factory.mktemp("provider_fallback_fe") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _run_fe_scenario(driver_path: str, scenario: str) -> str:
    result = subprocess.run(
        [NODE, driver_path, str(REPO_ROOT / "static" / "messages.js"), scenario],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout


pytestmark_fe = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytestmark_fe
def test_fe_renders_localized_composer_indicator(fe_driver_path):
    assert "PASS render" in _run_fe_scenario(fe_driver_path, "render")


@pytestmark_fe
def test_fe_ownership_guard_drops_foreign_stale_and_mismatched(fe_driver_path):
    assert "PASS ownership" in _run_fe_scenario(fe_driver_path, "ownership")


@pytestmark_fe
def test_fe_model_only_fallback_renders(fe_driver_path):
    assert "PASS model_only" in _run_fe_scenario(fe_driver_path, "model_only")


@pytestmark_fe
def test_fe_provider_only_fallback_renders(fe_driver_path):
    assert "PASS provider_only" in _run_fe_scenario(fe_driver_path, "provider_only")


@pytestmark_fe
def test_fe_dedup_by_transition_id_on_replay(fe_driver_path):
    assert "PASS dedup_replay" in _run_fe_scenario(fe_driver_path, "dedup_replay")


@pytestmark_fe
def test_fe_done_settlement_reasserts_indicator_once(fe_driver_path):
    assert "PASS done_reassert" in _run_fe_scenario(fe_driver_path, "done_reassert")


@pytestmark_fe
def test_fe_stale_replacement_stream_callback_dropped(fe_driver_path):
    """Production-shaped two-closure regression: stream 1 is replaced by
    stream 2 for the same session; a queued stream-1 event delivered through
    the OLD EventSource closure (whose own streamId is still 'stream-1') must
    not render into the replacement, and stream 2 must still render and
    reassert — the render fence is the CURRENT owner in LIVE_STREAMS, not an
    event-vs-closure comparison."""
    assert "PASS stale_stream" in _run_fe_scenario(fe_driver_path, "stale_stream")


@pytestmark_fe
def test_fe_indicator_survives_done_idle_cleanup(fe_driver_path):
    """The settled indicator is re-asserted AFTER the generic idle cleanup
    (setBusy(false)/setComposerStatus('')) — the pre-fix order wiped it."""
    assert "PASS done_cleanup_order" in _run_fe_scenario(fe_driver_path, "done_cleanup_order")


@pytestmark_fe
def test_fe_same_route_two_turns_both_render(fe_driver_path):
    """Two turns with the same route (different streams) both render — the
    per-stream owner in the transition id prevents the second from being
    deduplicated away."""
    assert "PASS two_turns_same_route" in _run_fe_scenario(fe_driver_path, "two_turns_same_route")


# ── Frontend source contract + i18n ──────────────────────────────────────


def test_fe_listener_source_contract():
    # The listener drives the real indicator and no longer drops model-only
    # fallbacks or logs to the console.
    assert "_renderProviderFallbackIndicator(d);" in MESSAGES_JS
    assert "if(!d.to_provider) return;" not in MESSAGES_JS
    assert "console.info('[provider_fallback]'" not in MESSAGES_JS
    # `done` settlement re-asserts the indicator with the exact stream owner,
    # and the reassert call sits AFTER the generic idle cleanup call inside
    # the done handler (the cleanup would otherwise erase the notice).
    assert "_reassertProviderFallbackIndicator(completedSid, streamId)" in MESSAGES_JS
    reassert_pos = MESSAGES_JS.index("_reassertProviderFallbackIndicator(completedSid, streamId)")
    idle_pos = MESSAGES_JS.rindex("_setActivePaneIdleIfOwner();", 0, reassert_pos)
    assert idle_pos < reassert_pos


def test_i18n_key_present_in_all_locale_blocks():
    # The composer indicator text is localized via t('provider_fallback_status', …)
    # and the key exists in every locale block (missing keys fall back to en).
    assert MESSAGES_JS.count("t('provider_fallback_status'") >= 1
    assert I18N_JS.count("provider_fallback_status:") >= 14
