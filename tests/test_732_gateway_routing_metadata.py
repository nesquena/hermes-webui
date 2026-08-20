"""Regression coverage for #732 LLM Gateway routing metadata display."""

import re
import subprocess
from pathlib import Path

from api.models import Session
from api.streaming import _normalize_gateway_routing_metadata


REPO = Path(__file__).resolve().parents[1]
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _function_body(source, name):
    start = source.index(f"function {name}(")
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1:index]
    raise AssertionError(f"Unterminated function: {name}")


def _function_source(source, name):
    start = source.index(f"function {name}(")
    body = _function_body(source, name)
    opening = source.index("{", start)
    return source[start:opening + len(body) + 2]


def _media_blocks(source):
    for match in re.finditer(r"@media\s*([^\{]+)\{", source):
        opening = match.end() - 1
        depth = 0
        for index in range(opening, len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    yield match.group(1).strip(), source[opening + 1:index]
                    break


def test_gateway_routing_metadata_is_safely_normalized_from_response_metadata():
    metadata = {
        "used_provider": "Alibaba Cloud",
        "used_model": "deepseek-v3.2",
        "requested_provider": "CanopyWave",
        "requested_model": "deepseek-v3.2",
        "api_key": "fake_credential",
        "routing": [
            {
                "provider": "CanopyWave",
                "status": "failed",
                "reason": "timeout",
                "score": 0.12,
                "api_key": "fake_credential",
            },
            {"provider": "Alibaba Cloud", "status": "selected", "score": 0.91},
        ],
    }

    normalized = _normalize_gateway_routing_metadata(metadata, requested_model="deepseek-v3.2", requested_provider="CanopyWave")

    assert normalized == {
        "used_provider": "Alibaba Cloud",
        "used_model": "deepseek-v3.2",
        "requested_provider": "CanopyWave",
        "requested_model": "deepseek-v3.2",
        "provider_changed": True,
        "model_changed": False,
        "has_failover": True,
        "routing": [
            {"provider": "CanopyWave", "status": "failed", "reason": "timeout", "score": 0.12},
            {"provider": "Alibaba Cloud", "status": "selected", "score": 0.91},
        ],
    }
    assert "fake_credential" not in repr(normalized)


def test_gateway_routing_metadata_absent_returns_none_without_placeholder_noise():
    assert _normalize_gateway_routing_metadata({}, requested_model="gpt-5.5", requested_provider="openai-codex") is None
    assert _normalize_gateway_routing_metadata(None, requested_model="gpt-5.5", requested_provider="openai-codex") is None


def test_sse_routing_source_is_safely_normalized_and_persisted():
    normalized = _normalize_gateway_routing_metadata(
        {
            "requested_model": "auto",
            "requested_provider": "TokenTable",
            "used_model": "gpt-5.6-sol",
            "used_provider": "TokenTable",
            "source": "openai-compatible-sse",
            "api_key": "must-not-survive",
        }
    )

    assert normalized == {
        "requested_model": "auto",
        "requested_provider": "TokenTable",
        "used_model": "gpt-5.6-sol",
        "used_provider": "TokenTable",
        "source": "openai-compatible-sse",
        "provider_changed": False,
        "model_changed": True,
        "has_failover": False,
    }
    assert "must-not-survive" not in repr(normalized)


def test_session_persists_latest_gateway_routing_and_history_across_reload():
    routing = _normalize_gateway_routing_metadata(
        {
            "used_provider": "provider-b",
            "used_model": "model-b",
            "requested_provider": "provider-a",
            "requested_model": "model-a",
            "source": "openai-compatible-sse",
            "routing": [
                {"provider": "provider-a", "status": "failed"},
                {"provider": "provider-b", "status": "selected"},
            ],
        },
        requested_model="model-a",
        requested_provider="provider-a",
    )
    session = Session(session_id="732gateway", title="Gateway", gateway_routing=routing, gateway_routing_history=[routing])
    session.messages = [{"role": "assistant", "content": "done", "_gatewayRouting": routing}]
    session.save()

    reloaded = Session.load("732gateway")

    assert reloaded.gateway_routing == routing
    assert reloaded.gateway_routing["source"] == "openai-compatible-sse"
    assert reloaded.gateway_routing_history == [routing]
    assert reloaded.gateway_routing_history[0]["source"] == "openai-compatible-sse"
    assert reloaded.messages[-1]["_gatewayRouting"] == routing
    assert reloaded.messages[-1]["_gatewayRouting"]["source"] == "openai-compatible-sse"
    compact = reloaded.compact()
    assert compact["gateway_routing"] == routing
    assert compact["gateway_routing_history"] == [routing]


def test_streaming_captures_gateway_metadata_into_usage_payload_and_assistant_turn():
    assert "_extract_gateway_routing_metadata" in STREAMING_PY
    assert "usage['gateway_routing']" in STREAMING_PY
    assert "_dm['_gatewayRouting']" in STREAMING_PY
    assert "s.gateway_routing_history" in STREAMING_PY


def test_streaming_explicit_gateway_metadata_uses_display_requested_provider():
    streaming_worker = STREAMING_PY.split("def _run_agent_streaming(", 1)[1]
    persistence = streaming_worker.split(
        "_gateway_routing = _extract_gateway_routing_metadata(", 1
    )[1].split("if _gateway_routing:", 1)[0]

    assert "requested_provider=_requested_provider_display" in persistence
    assert persistence.count("requested_provider=_requested_provider_display") == 2

    normalized = _normalize_gateway_routing_metadata(
        {
            "used_provider": "TokenTable",
            "used_model": "gpt-5.6-sol",
        },
        requested_model="auto",
        requested_provider="TokenTable",
    )
    assert normalized["requested_provider"] == "TokenTable"
    assert normalized["provider_changed"] is False


def test_routed_model_capture_cleanup_is_in_streaming_outer_finally():
    streaming_worker = STREAMING_PY.split("def _run_agent_streaming(", 1)[1].split(
        "# ============================================================\n"
        "# SECTION: HTTP Request Handler",
        1,
    )[0]
    outer_finally = streaming_worker.rsplit("\n    finally:\n", 1)[1]
    reset_capture = outer_finally.find(
        "        if _routed_model_capture_token is not None:\n"
        "            try:\n"
        "                reset_routed_model_capture(_routed_model_capture_token)"
    )
    clear_env = outer_finally.find(
        "        _clear_thread_env()  # TD1: always clear thread-local context"
    )

    assert reset_capture >= 0
    assert clear_env > reset_capture


def test_frontend_copies_and_formats_gateway_metadata_without_absent_noise():
    assert "d.usage.gateway_routing" in MESSAGES_JS
    assert "lastAsst._gatewayRouting" in MESSAGES_JS
    assert "_formatGatewayModelLabel" in UI_JS
    assert "_gatewayRoutingLabel" in UI_JS
    assert "msg-gateway-inline" in UI_JS
    assert "msg-model-warning-inline" in UI_JS
    assert "gateway-failover-inline" in UI_JS
    assert "if(!routing)return''" in UI_JS.replace(" ", "")
    assert "_formatSessionModelWithGateway" in SESSIONS_JS
    assert ".msg-model-warning-inline" in STYLE_CSS


def test_sse_routed_model_footer_uses_three_safe_labelled_fields():
    assert "function _routedModelObservationFields" in UI_JS
    assert "msg-routed-model-inline" in UI_JS
    assert "msg-routed-model-field" in UI_JS
    assert ".msg-routed-model-inline" in STYLE_CSS
    assert ".msg-routed-model-field" in STYLE_CSS

    formatter = "".join(_function_body(UI_JS, "_routedModelObservationFields").split())
    assert "{label:'Requested',value:requested}" in formatter
    assert "{label:'Routed',value:routed}" in formatter
    assert "{label:'Provider',value:provider}" in formatter

    renderer = "".join(_function_body(UI_JS, "_appendRoutedModelObservation").split())
    assert ".textContent=" in renderer
    assert ".innerHTML" not in renderer


def test_routed_model_footer_executes_real_formatter_and_safe_dom_renderer():
    production_functions = "\n".join(
        _function_source(UI_JS, name)
        for name in (
            "_gatewayProviderName",
            "_routedModelObservationFields",
            "_appendRoutedModelObservation",
        )
    )
    harness = f"""
'use strict';
function assert(condition, message) {{
  if (!condition) throw new Error(message);
}}
function elementStub() {{
  return {{
    className: '',
    textContent: '',
    children: [],
    appendChild(child) {{ this.children.push(child); return child; }},
    get firstChild() {{ return this.children[0] || null; }},
    get innerHTML() {{ return ''; }},
    set innerHTML(_value) {{ throw new Error('innerHTML must not be used'); }},
  }};
}}
const document = {{ createElement: elementStub }};
{production_functions}

const complete = {{
  source: 'openai-compatible-sse',
  requested_model: 'auto',
  used_model: 'gpt-5.6-sol',
  requested_provider: 'Fallback Provider',
  used_provider: 'TokenTable',
}};
const completeTarget = elementStub();
assert(_appendRoutedModelObservation(completeTarget, complete) === true, 'complete append return');
assert(completeTarget.children.length === 1, 'complete group count');
const completeGroup = completeTarget.firstChild;
assert(completeGroup.className === 'msg-routed-model-inline', 'complete group class');
assert(completeGroup.children.length === 3, 'complete field count');
assert(completeGroup.children.every(item => item.className === 'msg-routed-model-field'), 'field classes');
assert(JSON.stringify(completeGroup.children.map(item => item.textContent)) === JSON.stringify([
  'Requested: auto',
  'Routed: gpt-5.6-sol',
  'Provider: TokenTable',
]), 'complete field text');

const malicious = {{
  source: 'openai-compatible-sse',
  requested_model: '<img src=x onerror=alert(1)>',
  used_model: '<script>alert(2)</script>',
  used_provider: 'TokenTable',
}};
const maliciousTarget = elementStub();
assert(_appendRoutedModelObservation(maliciousTarget, malicious) === true, 'malicious append return');
assert(JSON.stringify(maliciousTarget.firstChild.children.map(item => item.textContent)) === JSON.stringify([
  'Requested: <img src=x onerror=alert(1)>',
  'Routed: <script>alert(2)</script>',
  'Provider: TokenTable',
]), 'malicious values remain literal text');

const fallback = {{
  source: 'openai-compatible-sse',
  requested_model: 'auto',
  used_model: 'gpt-5.6-sol',
  requested_provider: 'TokenTable',
}};
assert(_routedModelObservationFields(fallback)[2].value === 'TokenTable', 'requested provider fallback');

const missingRouted = {{source: 'openai-compatible-sse', requested_model: 'auto', used_model: ''}};
const missingTarget = elementStub();
assert(_routedModelObservationFields(missingRouted).length === 0, 'missing routed fields');
assert(_appendRoutedModelObservation(missingTarget, missingRouted) === false, 'missing routed append return');
assert(missingTarget.children.length === 0, 'missing routed does not append');

const nonSse = {{source: 'gateway-response', requested_model: 'auto', used_model: 'gpt-5.6-sol'}};
const nonSseTarget = elementStub();
assert(_routedModelObservationFields(nonSse).length === 0, 'non-SSE fields');
assert(_appendRoutedModelObservation(nonSseTarget, nonSse) === false, 'non-SSE append return');
assert(nonSseTarget.children.length === 0, 'non-SSE does not append');
"""

    result = subprocess.run(
        ["node", "-e", harness],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_non_sse_gateway_metadata_keeps_existing_footer_path():
    compact = "".join(UI_JS.split())
    assert "routing.source==='openai-compatible-sse'" in compact
    assert "_formatGatewayModelLabel" in UI_JS
    assert "_gatewayRoutingFailoverText" in UI_JS
    assert "_gatewayModelWarningText" in UI_JS


def test_sse_footer_suppresses_legacy_gateway_and_warning_but_preserves_failover():
    compact = "".join(UI_JS.split())
    assert "constgatewayText=isSseObservation?'':_formatGatewayModelLabel" in compact
    assert "constmodelWarningText=isSseObservation?'':_gatewayModelWarningText" in compact
    assert "constfailoverText=_gatewayRoutingFailoverText(routing)" in compact
    assert "constroutedModelFields=_routedModelObservationFields(routing)" in compact


def test_routed_model_footer_duplicate_guard_includes_new_selector():
    footer_render = UI_JS.split("// Render per-turn duration and optional token usage", 1)[1]
    duplicate_guard = footer_render.split("querySelector(", 1)[1].split(")", 1)[0]
    assert ".msg-routed-model-inline" in duplicate_guard


def test_routed_model_footer_has_exact_600px_mobile_stack_boundary():
    routed_blocks = [
        (header, body)
        for header, body in _media_blocks(STYLE_CSS)
        if ".msg-routed-model-inline" in body and ".msg-routed-model-field" in body
    ]
    assert len(routed_blocks) == 1
    header, body = routed_blocks[0]
    assert "".join(header.split()) == "(max-width:600px)"
    mobile = "".join(body.split())
    assert ".msg-routed-model-inline{flex:11100%;width:100%" in mobile
    assert ".msg-routed-model-field{flex:11100%" in mobile
