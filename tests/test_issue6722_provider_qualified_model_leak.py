"""Regression tests for issue #6722 — @provider:model leaking to the gateway.

The WebUI model picker deliberately carries a provider-qualified model string
(``@provider:model``) through internal routing (#1253). The leak involved
three parsing/request-boundary failures:

1. ``_clean_session_model_provider`` did not parse qualified values with the
   shared grammar, so provider selection could retain or truncate the wrong
   segments.
2. ``_split_provider_qualified_model`` used a positional split that could not
   distinguish multi-segment custom provider IDs from colon-tagged model IDs.
3. The gateway request builders sent the internally qualified model string
   verbatim as ``body["model"]``, which the upstream provider API 404'd on.

Internal session-state resolution deliberately keeps the qualified string for
routing and provider-switch repair. The gateway boundary alone strips it to the
bare model. All parsing now uses the single shared grammar in
``config._parse_provider_qualified_model_id()``. That parser is the important
part of the fix: a positional colon split cannot tell
``@ollama-cloud:deepseek-v4-flash:0731`` (single-segment provider, tagged
model) from ``@custom:backup:model-a`` (two-segment custom provider ID), and
guessing either way breaks the other. These tests pin both shapes so a future
"simplification" back to a split heuristic fails loudly.
"""

from collections import OrderedDict
import json

import api.gateway_chat as gateway_chat
import api.models as models
import api.streaming as streaming
from api.config import STREAMS, create_stream_channel
from api.models import new_session


# (qualified value, expected bare model, expected provider)
#
# The custom:* rows are the ones a positional split gets wrong. ``custom:backup``
# and ``custom:<host>:<port>`` are legitimate multi-segment provider IDs, so the
# provider is NOT just the text before the first colon.
QUALIFIED_MODEL_CASES = [
    # Single-segment provider, plain model — the originally reported #6722 case.
    ("@ollama-cloud:deepseek-v4-flash", "deepseek-v4-flash", "ollama-cloud"),
    # Single-segment provider, model carrying its own colon tag.
    ("@ollama-cloud:deepseek-v4-flash:0731", "deepseek-v4-flash:0731", "ollama-cloud"),
    # Named custom provider — provider keeps both segments.
    ("@custom:backup:model-a", "model-a", "custom:backup"),
    # Named custom provider AND a tagged model.
    ("@custom:backup:model-a:free", "model-a:free", "custom:backup"),
    # host:port custom provider IDs.
    ("@custom:192.168.1.5:11434:llama4", "llama4", "custom:192.168.1.5:11434"),
    ("@custom:localhost:1234:qwen3", "qwen3", "custom:localhost:1234"),
    # Slash-style model id plus a :free tag (#6221 shape).
    ("@openrouter:meta/llama-4:free", "meta/llama-4:free", "openrouter"),
    ("@anthropic:claude-opus-4.8", "claude-opus-4.8", "anthropic"),
]


class TestSharedParserIsTheSingleGrammar:
    """routes' splitter must agree with the shared config parser, exactly."""

    def test_split_provider_qualified_model_matches_shared_parser(self):
        from api.config import _parse_provider_qualified_model_id
        from api.routes import _split_provider_qualified_model

        for value, expected_model, expected_provider in QUALIFIED_MODEL_CASES:
            assert _parse_provider_qualified_model_id(value) == (
                expected_model,
                expected_provider,
            ), f"shared parser disagrees for {value!r}"
            assert _split_provider_qualified_model(value) == (
                expected_model,
                expected_provider,
            ), f"routes splitter diverged from the shared parser for {value!r}"

    def test_non_qualified_values_pass_through_unchanged(self):
        from api.routes import _split_provider_qualified_model

        for value in ("gpt-5.4", "anthropic/claude-opus-4.8", "custom:backup", "@nocolon", ""):
            assert _split_provider_qualified_model(value) == (value, None)


class TestCleanSessionModelProvider:
    """The provider field must never keep a model segment attached."""

    def test_at_qualified_values_resolve_to_the_provider_only(self):
        from api.routes import _clean_session_model_provider

        for value, _expected_model, expected_provider in QUALIFIED_MODEL_CASES:
            assert _clean_session_model_provider(value) == expected_provider, (
                f"{value!r} must clean to provider {expected_provider!r}"
            )

    def test_non_at_provider_ids_keep_their_colons(self):
        """A bare ``custom:backup`` is already a provider ID, not a hint."""
        from api.routes import _clean_session_model_provider

        assert _clean_session_model_provider("custom:backup") == "custom:backup"
        assert _clean_session_model_provider("custom:localhost:1234") == "custom:localhost:1234"
        assert _clean_session_model_provider("ollama-cloud") == "ollama-cloud"

    def test_empty_and_default_values_resolve_to_none(self):
        from api.routes import _clean_session_model_provider

        for value in (None, "", "   ", "default", "@"):
            assert _clean_session_model_provider(value) is None


class TestSessionModelStatePreservesQualifierForRepair:
    """The request path must keep the @provider: qualifier through resolution so
    the compatibility/repair step (and PR #6718's custom-provider normalizer) can
    still see the qualified form. The qualifier is stripped to the bare model ONLY
    at the gateway boundary (see TestGatewayModelField)."""

    def test_unavailable_qualified_provider_repairs_to_active_default(self, monkeypatch):
        """A session pinned to a removed/unconfigured provider must revert to the
        active default on a non-explicit resolve — the premature strip in the
        previous revision returned (bare_model, removed) instead."""
        import api.routes as routes

        monkeypatch.setattr(
            routes,
            "get_available_models",
            lambda: {
                "active_provider": "openai-codex",
                "default_model": "gpt-5.5",
                "groups": [
                    {
                        "provider_id": "openai-codex",
                        "models": [{"id": "@openai-codex:gpt-5.5"}],
                    },
                ],
            },
        )
        model_value, provider = routes._session_model_state_from_request(
            "@removed:mistral-large", None, "removed"
        )
        assert model_value == "gpt-5.5", (
            f"removed/unconfigured qualified provider must repair to the active "
            f"default, got model_value={model_value!r}"
        )
        assert provider == "openai-codex"

    def test_active_provider_qualified_model_preserves_qualifier_for_routing(self, monkeypatch):
        """A qualified model naming the active provider is preserved intact so
        downstream routing (resolve_model_provider) can use the provider."""
        import api.routes as routes

        monkeypatch.setattr(
            routes,
            "get_available_models",
            lambda: {
                "active_provider": "openai-codex",
                "default_model": "gpt-5.5",
                "groups": [
                    {
                        "provider_id": "openai-codex",
                        "models": [{"id": "@openai-codex:gpt-5.5"}],
                    },
                ],
            },
        )
        model_value, provider = routes._session_model_state_from_request(
            "@openai-codex:gpt-5.5", "openai-codex"
        )
        assert model_value == "@openai-codex:gpt-5.5", (
            f"qualifier must survive session-state resolution for routing, got {model_value!r}"
        )
        assert provider == "openai-codex"


class TestGatewayModelField:
    """body['model'] must be the bare model for every provider shape."""

    def test_gateway_model_field_strips_qualifier(self):
        from api.gateway_chat import _gateway_model_field

        for value, expected_model, _expected_provider in QUALIFIED_MODEL_CASES:
            assert _gateway_model_field(value) == expected_model, (
                f"gateway body model for {value!r} must be {expected_model!r}"
            )

    def test_unqualified_models_are_untouched(self):
        from api.gateway_chat import _gateway_model_field

        assert _gateway_model_field("gpt-5.4") == "gpt-5.4"
        assert _gateway_model_field("anthropic/claude-opus-4.8") == "anthropic/claude-opus-4.8"
        assert _gateway_model_field(None) == ""
        assert _gateway_model_field("") == ""


def _stub_prefill(monkeypatch):
    monkeypatch.setattr(
        streaming,
        "_load_webui_prefill_context",
        lambda cfg: {
            "status": "not_configured",
            "source": "none",
            "label": "",
            "message_count": 0,
            "messages": [],
        },
    )
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])


class _FakeChatResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __iter__(self):
        yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        yield b"data: [DONE]\n\n"


def _run_legacy_gateway_chat(tmp_path, monkeypatch, model):
    """Drive the legacy chat-completions path and return the request body."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["body"] = req.data.decode("utf-8")
        return _FakeChatResponse()

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    _stub_prefill(monkeypatch)
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)

    s = new_session()
    stream_id = f"stream-6722-legacy-{model}"
    s.active_stream_id = stream_id
    s.pending_user_message = "hi"
    s.pending_attachments = []
    s.pending_started_at = 123
    s.save()
    channel = create_stream_channel()
    channel.subscribe()
    STREAMS[stream_id] = channel

    gateway_chat._run_gateway_chat_streaming(
        s.session_id, "hi", model, str(tmp_path), stream_id, []
    )
    return json.loads(captured["body"])


class TestGatewayRequestBodiesCarryBareModel:
    """End-to-end: the bytes actually sent to the gateway."""

    def test_legacy_chat_completions_body_has_bare_model(self, tmp_path, monkeypatch):
        payload = _run_legacy_gateway_chat(
            tmp_path, monkeypatch, "@ollama-cloud:deepseek-v4-flash"
        )
        assert payload["model"] == "deepseek-v4-flash"
        assert not payload["model"].startswith("@")

    def test_legacy_body_keeps_named_custom_provider_model_intact(self, tmp_path, monkeypatch):
        """The multi-segment case a positional split would truncate."""
        payload = _run_legacy_gateway_chat(
            tmp_path, monkeypatch, "@custom:backup:model-a:free"
        )
        assert payload["model"] == "model-a:free"

    def test_legacy_body_keeps_host_port_custom_provider_model_intact(self, tmp_path, monkeypatch):
        payload = _run_legacy_gateway_chat(
            tmp_path, monkeypatch, "@custom:192.168.1.5:11434:llama4"
        )
        assert payload["model"] == "llama4"

    def test_runs_api_body_has_bare_model(self, monkeypatch):
        """The runs API builder is the second gateway request site."""
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["body"] = req.data.decode("utf-8")
            raise AssertionError("stop after the POST body is captured")

        monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(gateway_chat, "update_active_run", lambda *a, **k: None)

        try:
            gateway_chat._run_gateway_runs_api_streaming(
                "sess-6722",
                "hi",
                "@custom:backup:model-a:free",
                "/tmp",
                "stream-6722-runs",
                "http://gateway.local",
                "",
                [],
                {},
                put_gateway_event=lambda *a, **k: None,
                cancel_event=None,
            )
        except Exception:
            pass

        assert "body" in captured, "runs API POST body was never built"
        payload = json.loads(captured["body"])
        assert payload["model"] == "model-a:free"
        assert not payload["model"].startswith("@")
