"""Regression coverage for #732 LLM Gateway routing metadata display."""

from pathlib import Path

from api.models import Session
from api.streaming import _normalize_gateway_routing_metadata


REPO = Path(__file__).resolve().parents[1]
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


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


def test_session_persists_latest_gateway_routing_and_history_across_reload():
    routing = _normalize_gateway_routing_metadata(
        {
            "used_provider": "provider-b",
            "used_model": "model-b",
            "requested_provider": "provider-a",
            "requested_model": "model-a",
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
    assert reloaded.gateway_routing_history == [routing]
    assert reloaded.messages[-1]["_gatewayRouting"] == routing
    compact = reloaded.compact()
    assert compact["gateway_routing"] == routing
    assert compact["gateway_routing_history"] == [routing]


def test_streaming_captures_gateway_metadata_into_usage_payload_and_assistant_turn():
    assert "_extract_gateway_routing_metadata" in STREAMING_PY
    assert "usage['gateway_routing']" in STREAMING_PY
    assert "_dm['_gatewayRouting']" in STREAMING_PY
    assert "s.gateway_routing_history" in STREAMING_PY


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
