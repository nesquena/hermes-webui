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
    outer_finally = streaming_worker.split(
        "\n    finally:\n"
        "        # Stop the periodic checkpoint thread before the final recovery path.",
        1,
    )[1]
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
