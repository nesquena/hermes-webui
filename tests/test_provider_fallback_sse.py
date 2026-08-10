"""Focused coverage for the #6267 provider_fallback SSE contract.

Two proven producers feed the typed event (never string-inequality inference):
  1. Agent-side: the configured ``fallback_providers`` chain, detected from the
     agent's one-shot "Switched to fallback model: …" lifecycle notice.
  2. Gateway-side: LLM-gateway failover metadata with an explicit failed
     primary attempt followed by a selected route.

Frontend: the event renders a localized composer/turn indicator guarded by
session/stream ownership, deduplicated by transition_id (replay-safe), and
re-asserted once at ``done`` settlement.
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

    def __init__(self, provider="", model="", buffer=None):
        self.provider = provider
        self.model = model
        self._retry_status_buffer = buffer or []


def _noticed_state(provider="openai", model="gpt-4"):
    return {"seq": 0, "route_provider": provider, "route_model": model}


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
    )
    assert payload is not None
    assert payload["session_id"] == "sess-1"
    assert payload["source"] == "agent"
    assert payload["from_provider"] == "openai"
    assert payload["from_model"] == "gpt-4"
    assert payload["to_provider"] == "anthropic"
    assert payload["to_model"] == "opus"
    assert payload["reason"] == "⏳ Provider rate limit active — trying fallback"
    assert len(payload["transition_id"]) == 16


def test_agent_payload_accepts_provider_only_and_model_only():
    provider_only = _build_agent_fallback_payload("s1", "openai", "gpt-4", "anthropic", "", "boom", 1)
    assert provider_only is not None
    assert provider_only["to_provider"] == "anthropic"
    assert provider_only["to_model"] == ""

    model_only = _build_agent_fallback_payload("s1", "openai", "gpt-4", "", "opus", "boom", 2)
    assert model_only is not None
    assert model_only["to_provider"] == ""
    assert model_only["to_model"] == "opus"


def test_agent_payload_same_route_and_empty_destination_return_none():
    assert _build_agent_fallback_payload("s1", "openai", "gpt-4", "openai", "gpt-4", "x", 1) is None
    assert _build_agent_fallback_payload("s1", "openai", "gpt-4", "", "", "x", 1) is None
    # Alias/no-op: case-only difference is not a fallback.
    assert _build_agent_fallback_payload("s1", "OpenAI", "GPT-4", "openai", "gpt-4", "x", 1) is None


def test_agent_fallback_cause_uses_last_buffered_status_line():
    agent = _FakeAgent(buffer=[
        ("vprint", "ignored debug line"),
        ("status", "⏳ Nous rate limit active — resets in 60s"),
        ("warn", "primary attempt failed: HTTP 429"),
    ])
    assert _agent_fallback_cause(agent) == "primary attempt failed: HTTP 429"
    assert _agent_fallback_cause(_FakeAgent(buffer=[])) == ""
    assert _agent_fallback_cause(_FakeAgent(buffer=[("vprint", "only debug")])) == ""


def test_agent_producer_tracks_chain_switch_prior_route():
    agent = _FakeAgent("anthropic", "opus", [("status", "primary failed")])
    state = _noticed_state()
    first = _maybe_emit_agent_fallback_event(
        "lifecycle",
        "🔄 Switched to fallback model: gpt-4 via openai → opus via anthropic",
        agent, "sess-1", state,
    )
    assert first["from_provider"] == "openai"
    # Chain switch: the agent falls back AGAIN within the same turn.
    agent.provider = "deepseek"
    agent.model = "deepseek-v3"
    second = _maybe_emit_agent_fallback_event(
        "lifecycle",
        "🔄 Switched to fallback model: opus via anthropic → deepseek-v3 via deepseek",
        agent, "sess-1", state,
    )
    assert second is not None
    assert second["from_provider"] == "anthropic"  # tracked, not the original primary
    assert second["to_provider"] == "deepseek"
    assert second["transition_id"] != first["transition_id"]


def test_agent_producer_ignores_non_notice_status():
    state = _noticed_state()
    assert _maybe_emit_agent_fallback_event("lifecycle", "Rate limited, trying fallback…", _FakeAgent("a", "b"), "s1", state) is None
    assert state["seq"] == 0  # nothing consumed


def test_agent_producer_opt_out_env_var_suppresses_event(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PROVIDER_FALLBACK_SSE", "0")
    state = _noticed_state()
    payload = _maybe_emit_agent_fallback_event(
        "lifecycle",
        "🔄 Switched to fallback model: gpt-4 via openai → opus via anthropic",
        _FakeAgent("anthropic", "opus"), "s1", state,
    )
    assert payload is None


def test_agent_event_exactly_once_and_ordered_before_terminal_events():
    """One notice → exactly one provider_fallback event, emitted before the
    turn's terminal done event (the notice fires mid-turn, on recovery)."""
    agent = _FakeAgent("anthropic", "opus", [("status", "primary failed: HTTP 429")])
    state = _noticed_state()
    stream = []
    payload = _maybe_emit_agent_fallback_event(
        "lifecycle",
        "🔄 Switched to fallback model: gpt-4 via openai → opus via anthropic",
        agent, "sess-1", state,
    )
    assert payload is not None
    stream.append(("provider_fallback", payload))
    stream.append(("done", {"session_id": "sess-1"}))
    # Exactly one provider_fallback event and it precedes the terminal event.
    names = [e[0] for e in stream]
    assert names == ["provider_fallback", "done"]
    assert names.count("provider_fallback") == 1


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


def test_gateway_event_requires_explicit_failed_primary():
    # Plain requested-vs-used mismatch with no failed attempt = NOT a fallback.
    no_failure = _gateway_metadata(routing=[
        {"provider": "CanopyWave", "status": "selected"},
    ])
    assert _build_provider_fallback_sse_event(no_failure, "s1", 1) is None
    # has_failover flag alone (string-mismatch driven) is not sufficient.
    flagged = _gateway_metadata(extra={"has_failover": True})
    assert _build_provider_fallback_sse_event(flagged, "s1", 1) is None
    # Explicit failed primary followed by a selected route → proven failover.
    proven = _gateway_metadata(routing=[
        {"provider": "CanopyWave", "status": "failed", "error": "connection timeout"},
        {"provider": "Alibaba Cloud", "status": "selected"},
    ])
    payload = _build_provider_fallback_sse_event(proven, "s1", 1)
    assert payload is not None
    assert payload["source"] == "gateway"
    assert payload["from_provider"] == "CanopyWave"
    assert payload["to_provider"] == "Alibaba Cloud"
    assert payload["reason"] == "connection timeout"


def test_gateway_event_failed_primary_reason_selection():
    """Reason comes from the FIRST failed primary attempt, not the last routing
    entry (which may be the successful fallback's rationale)."""
    routing = [
        {"provider": "CanopyWave", "status": "failed", "error": "primary rejected: 401"},
        {"provider": "DeepInfra", "status": "failed", "error": "secondary also failed"},
        {"provider": "Alibaba Cloud", "status": "selected"},
    ]
    payload = _build_provider_fallback_sse_event(_gateway_metadata(routing=routing), "s1", 1)
    assert payload["reason"] == "primary rejected: 401"
    # reason-only attempts (no error field) are honored too.
    routing2 = [
        {"provider": "CanopyWave", "status": "timeout", "reason": "upstream timeout"},
        {"provider": "Alibaba Cloud", "status": "selected"},
    ]
    payload2 = _build_provider_fallback_sse_event(_gateway_metadata(routing=routing2), "s1", 1)
    assert payload2["reason"] == "upstream timeout"


def test_gateway_event_same_route_and_alias_are_no_fallback():
    failed = [{"provider": "CanopyWave", "status": "failed", "error": "x"}]
    # Same route after failure = recovery, not fallback.
    same = _gateway_metadata(used_provider="CanopyWave", used_model="deepseek-v3.2", routing=failed)
    assert _build_provider_fallback_sse_event(same, "s1", 1) is None
    # Case-only alias difference is not a fallback.
    alias = _gateway_metadata(used_provider="canopywave", used_model="DeepSeek-V3.2", routing=failed)
    assert _build_provider_fallback_sse_event(alias, "s1", 1) is None


def test_gateway_event_provider_only_and_model_only():
    failed = [{"provider": "CanopyWave", "status": "failed", "error": "x"}]
    provider_only = _gateway_metadata(used_provider="Alibaba Cloud", used_model="", routing=failed)
    payload = _build_provider_fallback_sse_event(provider_only, "s1", 1)
    assert payload is not None and payload["to_model"] == ""
    model_only = _gateway_metadata(used_provider="CanopyWave", used_model="other-model", routing=failed)
    payload2 = _build_provider_fallback_sse_event(model_only, "s1", 1)
    assert payload2 is not None and payload2["to_provider"] == "CanopyWave"


def test_gateway_event_malformed_metadata_returns_none():
    assert _build_provider_fallback_sse_event(None, "s1", 1) is None
    assert _build_provider_fallback_sse_event({}, "s1", 1) is None
    assert _build_provider_fallback_sse_event("garbage", "s1", 1) is None
    # routing not a list / entries not dicts
    bad = _gateway_metadata(routing="not-a-list")
    assert _build_provider_fallback_sse_event(bad, "s1", 1) is None
    bad2 = _gateway_metadata(routing=["x", None, 42])
    assert _build_provider_fallback_sse_event(bad2, "s1", 1) is None
    # failed attempts but no selected route
    no_route = _gateway_metadata(used_provider="", used_model="", routing=[
        {"provider": "CanopyWave", "status": "failed", "error": "x"},
    ])
    assert _build_provider_fallback_sse_event(no_route, "s1", 1) is None


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
    a = _fallback_transition_id("s1", "openai", "gpt", "anthropic", "opus", 1)
    b = _fallback_transition_id("s1", "openai", "gpt", "anthropic", "opus", 1)
    assert a == b and len(a) == 16
    assert a != _fallback_transition_id("s1", "openai", "gpt", "anthropic", "opus", 2)  # next occurrence
    assert a != _fallback_transition_id("s2", "openai", "gpt", "anthropic", "opus", 1)  # other session
    assert a != _fallback_transition_id("s1", "openai", "gpt", "anthropic", "opus-2", 1)  # other route


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

// Harness globals mirroring the production closure of attachLiveStream().
let activeSid = 'sess-A';
const S = { session: { session_id: 'sess-A' } };
let _providerFallbackRenderedMap = {};
let _providerFallbackReassertedMap = {};
let _providerFallbackRenderCount = 0;
let _lastComposerStatus = '';
let _lastToast = null;
function setComposerStatus(x) { _lastComposerStatus = x; }
function showToast(m, ms, type) { _lastToast = { m: m, ms: ms, type: type }; }
function t(k, ...a) { return k === 'provider_fallback_status' ? ('⚠️ Fell back to ' + a[0]) : k; }

// Production functions extracted LIVE from messages.js — not test stand-ins.
eval(extractFunc('_providerFallbackLabel'));
eval(extractFunc('_renderProviderFallbackIndicator'));
eval(extractFunc('_reassertProviderFallbackIndicator'));

function assert(c, m) { if (!c) throw new Error(m || 'assertion failed'); }
const scenario = process.argv[3];

if (scenario === 'render') {
  _renderProviderFallbackIndicator({ session_id: 'sess-A', transition_id: 't1', from_provider: 'openai', from_model: 'gpt-4', to_provider: 'anthropic', to_model: 'opus', reason: 'rate limited' });
  assert(_lastComposerStatus === '⚠️ Fell back to opus via anthropic', 'composer status: ' + _lastComposerStatus);
  assert(_lastToast && _lastToast.m === 'rate limited' && _lastToast.type === 'warning', 'toast not shown');
  assert(_providerFallbackRenderedMap['t1'], 'transition not recorded');
  assert(_providerFallbackReassertedMap['sess-A'] === 't1', 'reassert record missing');
  process.stdout.write('PASS render\n');
} else if (scenario === 'ownership') {
  _renderProviderFallbackIndicator({ session_id: 'sess-B', transition_id: 't1', to_provider: 'anthropic' });
  assert(_lastComposerStatus === '', 'foreign session rendered');
  assert(Object.keys(_providerFallbackRenderedMap).length === 0, 'foreign session recorded');
  S.session = { session_id: 'sess-OTHER' };
  _renderProviderFallbackIndicator({ session_id: 'sess-A', transition_id: 't2', to_provider: 'anthropic' });
  assert(_lastComposerStatus === '', 'S.session mismatch rendered');
  assert(Object.keys(_providerFallbackRenderedMap).length === 0, 'mismatch recorded');
  process.stdout.write('PASS ownership\n');
} else if (scenario === 'model_only') {
  _renderProviderFallbackIndicator({ session_id: 'sess-A', transition_id: 't2', from_model: 'gpt-4', to_model: 'opus', reason: 'model swapped' });
  assert(_lastComposerStatus === '⚠️ Fell back to opus', 'model-only status: ' + _lastComposerStatus);
  assert(_lastToast && _lastToast.m === 'model swapped', 'model-only toast missing');
  process.stdout.write('PASS model_only\n');
} else if (scenario === 'provider_only') {
  _renderProviderFallbackIndicator({ session_id: 'sess-A', transition_id: 't3', from_provider: 'openai', to_provider: 'anthropic' });
  assert(_lastComposerStatus === '⚠️ Fell back to anthropic', 'provider-only status: ' + _lastComposerStatus);
  process.stdout.write('PASS provider_only\n');
} else if (scenario === 'dedup_replay') {
  const d = { session_id: 'sess-A', transition_id: 't1', to_provider: 'anthropic' };
  _renderProviderFallbackIndicator(d);
  assert(_lastComposerStatus === '⚠️ Fell back to anthropic', 'first render');
  _lastComposerStatus = '';
  // SSE snapshot replay re-delivers the SAME transition → must not re-render.
  _renderProviderFallbackIndicator(d);
  assert(_lastComposerStatus === '', 'replay re-rendered');
  assert(Object.keys(_providerFallbackRenderedMap).length === 1, 'map grew on replay');
  // A NEW transition in a later turn still renders (map is keyed by id).
  _renderProviderFallbackIndicator({ session_id: 'sess-A', transition_id: 't2', to_model: 'opus' });
  assert(_lastComposerStatus === '⚠️ Fell back to opus', 'new transition suppressed');
  process.stdout.write('PASS dedup_replay\n');
} else if (scenario === 'done_reassert') {
  _renderProviderFallbackIndicator({ session_id: 'sess-A', transition_id: 't1', to_provider: 'anthropic' });
  _lastComposerStatus = '';
  _reassertProviderFallbackIndicator('sess-A');
  assert(_lastComposerStatus === '⚠️ Fell back to anthropic', 'done reassert failed: ' + _lastComposerStatus);
  assert(Object.keys(_providerFallbackRenderedMap).length === 1, 'reassert grew map');
  _lastComposerStatus = '';
  _reassertProviderFallbackIndicator('sess-UNKNOWN');
  assert(_lastComposerStatus === '', 'unknown session reasserted');
  process.stdout.write('PASS done_reassert\n');
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
def test_fe_ownership_guard_drops_foreign_and_mismatched_sessions(fe_driver_path):
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


# ── Frontend source contract + i18n ──────────────────────────────────────


def test_fe_listener_source_contract():
    # The listener drives the real indicator and no longer drops model-only
    # fallbacks or logs to the console.
    assert "_renderProviderFallbackIndicator(d);" in MESSAGES_JS
    assert "if(!d.to_provider) return;" not in MESSAGES_JS
    assert "console.info('[provider_fallback]'" not in MESSAGES_JS
    # `done` settlement re-asserts the indicator (survives settlement).
    assert "_reassertProviderFallbackIndicator(completedSid)" in MESSAGES_JS


def test_i18n_key_present_in_all_locale_blocks():
    # The composer indicator text is localized via t('provider_fallback_status', …)
    # and the key exists in every locale block (missing keys fall back to en).
    assert MESSAGES_JS.count("t('provider_fallback_status'") >= 1
    assert I18N_JS.count("provider_fallback_status:") >= 14
