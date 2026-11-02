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


def test_recovered_history_keeps_its_own_routing_across_reload(tmp_path, monkeypatch):
    """History recovery inserts an old final answer before the new user.

    The pre-run display only contained the tool-call assistant, so a count
    would treat the recovered final as this turn's first interim and stamp
    the current model on it. The checkpoint user row is the boundary. Old
    routing survives save/reload; this turn's interim is labeled and the
    final keeps the routing it already carried.
    """
    import api.models as models
    from api.models import Session
    from api.streaming import (
        _active_turn_authority,
        _backfill_turn_gateway_routing,
        _settle_result_messages,
        _settle_turn_reasoning,
    )

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    durable = {"_row_id": 501, "message_uid": "a" * 32}
    tool_call = {"role": "assistant", "content": "", "tool_calls": [{"id": "call_x"}]}
    display_tool = {
        "role": "tool", "tool_call_id": "call_x", "content": "full tool output", **durable,
    }
    context_tool = {
        "role": "tool", "tool_call_id": "call_x",
        "content": "[terminal] ran `ls` -> exit 0, 1 lines output",
        **durable,
    }
    old_routing = {"used_model": "old-model", "used_provider": "old-provider"}
    final_routing = {"used_model": "final-model", "used_provider": "final-provider"}
    previous_display = [
        {"role": "user", "content": "run it"},
        tool_call,
        display_tool,
    ]
    previous_context = [
        {"role": "user", "content": "run it"},
        dict(tool_call),
        context_tool,
        {"role": "assistant", "content": "context-only final answer", "_gatewayRouting": dict(old_routing)},
    ]
    result_messages = previous_context + [
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "interim"},
        {"role": "tool", "tool_call_id": "call_new", "content": "tool out"},
        {"role": "assistant", "content": "final answer", "_gatewayRouting": dict(final_routing)},
    ]

    session = Session(session_id="pr8287routing", title="routing boundary")
    session.messages = [dict(m) for m in previous_display]
    session.pending_user_message = "next"
    session.pending_started_at = 20.0
    session.active_stream_id = "stream-8287"
    authority = _active_turn_authority(session, session.active_stream_id, "next")
    assert authority["token"]

    _settle_result_messages(
        session,
        [dict(m) for m in previous_display],
        [dict(m) for m in previous_context],
        result_messages,
        "next",
        "webui",
        authority,
    )
    by_content = {m.get("content"): m for m in session.messages if isinstance(m, dict)}
    assert by_content["context-only final answer"]["_gatewayRouting"] == old_routing
    checkpoint = next(
        m for m in session.messages
        if m.get("role") == "user" and m.get("_active_turn_token") == authority["token"]
    )
    assert checkpoint["content"] == "next"
    assert session.messages.index(by_content["context-only final answer"]) < session.messages.index(checkpoint)

    _settle_turn_reasoning(
        session,
        previous_display,
        {0: "think current"},
        active_turn_identity=authority,
    )
    assert by_content["context-only final answer"].get("reasoning") in (None, "")
    assert by_content["interim"].get("reasoning") == "think current"

    current_routing = {"used_model": "current-model", "used_provider": "current-provider"}
    _backfill_turn_gateway_routing(
        session.messages, routing=current_routing, active_turn_identity=authority,
    )
    assert by_content["context-only final answer"]["_gatewayRouting"] == old_routing
    assert by_content["interim"]["_gatewayRouting"] == current_routing
    assert by_content["final answer"]["_gatewayRouting"] == final_routing

    session.save(skip_index=True)
    models.SESSIONS.pop(session.session_id, None)
    reloaded = Session.load(session.session_id)
    reloaded_by_content = {m.get("content"): m for m in reloaded.messages}
    assert reloaded_by_content["context-only final answer"]["_gatewayRouting"] == old_routing
    assert reloaded_by_content["interim"]["_gatewayRouting"] == current_routing
    assert reloaded_by_content["final answer"]["_gatewayRouting"] == final_routing


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


def test_backfill_labels_every_assistant_message_of_the_turn():
    # Mid-turn answers (interim texts before tool calls) must carry a model
    # label too; the final answer keeps its authoritative result metadata.
    from api.streaming import _backfill_turn_gateway_routing

    routing = {"used_model": "model-a"}
    token = "stream-732:1"
    msgs = [
        {"role": "user", "content": "hi", "_active_turn_token": token},
        {"role": "assistant", "content": "interim one"},
        {"role": "tool", "content": "x"},
        {"role": "assistant", "content": "interim two"},
        {"role": "tool", "content": "y"},
        {"role": "assistant", "content": "final"},
    ]
    msgs[5]["_gatewayRouting"] = {"used_model": "final-model"}

    _backfill_turn_gateway_routing(
        msgs, routing=routing, active_turn_identity={"token": token},
    )

    assert msgs[1]["_gatewayRouting"] == routing
    assert msgs[3]["_gatewayRouting"] == routing
    assert msgs[5]["_gatewayRouting"] == {"used_model": "final-model"}


def test_backfill_skips_prior_turns_and_no_routing_turns():
    from api.streaming import _backfill_turn_gateway_routing

    token = "stream-732:2"
    msgs = [
        {"role": "assistant", "content": "previous turn"},
        {"role": "user", "content": "now", "_active_turn_token": token},
        {"role": "assistant", "content": "current turn"},
    ]
    _backfill_turn_gateway_routing(
        msgs, routing={"used_model": "m"}, active_turn_identity={"token": token},
    )
    assert "_gatewayRouting" not in msgs[0]
    assert msgs[2]["_gatewayRouting"]["used_model"] == "m"

    empty = [{"role": "assistant", "content": "x"}]
    _backfill_turn_gateway_routing(
        empty, routing=None, active_turn_identity={"token": token},
    )
    assert "_gatewayRouting" not in empty[0]


def test_backfill_does_not_guess_when_the_checkpoint_is_missing():
    """A pre-run assistant count is not a substitute for the checkpoint."""
    from api.streaming import _backfill_turn_gateway_routing

    msgs = [
        {"role": "assistant", "content": "recovered history"},
        {"role": "assistant", "content": "current interim"},
    ]
    _backfill_turn_gateway_routing(
        msgs, routing={"used_model": "current"}, active_turn_identity={"token": "missing"},
    )
    assert "_gatewayRouting" not in msgs[0]
    assert "_gatewayRouting" not in msgs[1]
