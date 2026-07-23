from __future__ import annotations

import io
import json
import sys
import types
from urllib.parse import urlparse

import pytest

from api import config, routes, streaming


class _Handler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.sent_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass


def _payload(handler):
    return json.loads(handler.wfile.getvalue())


def test_codex_plugins_route_is_covered_by_api_auth(monkeypatch):
    from api import auth

    handler = _Handler()
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda _handler: None)
    monkeypatch.setattr(auth, "ensure_trusted_auth_session", lambda _handler: None)

    assert auth.check_auth(handler, urlparse("/api/codex/plugins")) is False
    assert handler.status == 401


def test_codex_plugins_route_returns_sanitized_inventory(monkeypatch):
    agent_module = types.ModuleType("agent")
    runtime_module = types.ModuleType("agent.codex_runtime")
    runtime_module.list_codex_plugins = lambda: [
        {
            "name": "research",
            "path": "plugin://research",
            "description": "Search helpers",
            "keywords": ["papers"],
            "raw_protocol": {"secret": "not exposed"},
        }
    ]
    monkeypatch.setitem(sys.modules, "agent", agent_module)
    monkeypatch.setitem(sys.modules, "agent.codex_runtime", runtime_module)
    handler = _Handler()

    routes.handle_get(handler, urlparse("/api/codex/plugins"))
    assert handler.status == 200
    assert _payload(handler) == {
        "plugins": [{
            "name": "research",
            "path": "plugin://research",
            "description": "Search helpers",
            "keywords": ["papers"],
        }],
        "available": True,
    }


def test_codex_plugins_route_rejects_blank_identity_and_limits_count(monkeypatch):
    agent_module = types.ModuleType("agent")
    runtime_module = types.ModuleType("agent.codex_runtime")
    runtime_module.list_codex_plugins = lambda: [
        {"name": " ", "path": "plugin://blank", "description": "", "keywords": []},
        {"name": "bad", "path": "https://example.com", "description": "", "keywords": []},
        *[
            {"name": f"p{i}", "path": f"plugin://p{i}", "description": "", "keywords": []}
            for i in range(routes._CODEX_PLUGIN_MENTION_LIMIT + 5)
        ],
    ]
    monkeypatch.setitem(sys.modules, "agent", agent_module)
    monkeypatch.setitem(sys.modules, "agent.codex_runtime", runtime_module)
    handler = _Handler()

    routes.handle_get(handler, urlparse("/api/codex/plugins"))

    payload = _payload(handler)
    assert payload["available"] is True
    assert len(payload["plugins"]) == routes._CODEX_PLUGIN_MENTION_LIMIT - 2
    assert all(item["name"].strip() and item["path"].strip() for item in payload["plugins"])


def test_codex_plugins_route_degrades_when_companion_helper_is_missing(monkeypatch):
    agent_module = types.ModuleType("agent")
    monkeypatch.setitem(sys.modules, "agent", agent_module)
    monkeypatch.delitem(sys.modules, "agent.codex_runtime", raising=False)
    handler = _Handler()

    routes.handle_get(handler, urlparse("/api/codex/plugins"))

    assert handler.status == 200
    assert _payload(handler) == {"plugins": [], "available": False}


@pytest.mark.parametrize(
    "value",
    ["plugin://x", [{}], [{"name": "x", "path": "https://example.com"}],
     [{"name": "x", "path": "plugin://x", "extra": True}]],
)
def test_plugin_mentions_reject_malformed_input(value):
    with pytest.raises(ValueError):
        routes._normalize_codex_plugin_mentions(value)


def test_chat_start_rejects_malformed_mentions_before_session_lookup(monkeypatch):
    monkeypatch.setattr(
        routes,
        "_get_or_materialize_session",
        lambda *_args, **_kwargs: pytest.fail("malformed request reached session lookup"),
    )
    handler = _Handler()

    routes._handle_chat_start(
        handler,
        {"session_id": "s1", "message": "hello", "plugin_mentions": "plugin://one"},
    )

    assert handler.status == 400
    assert "plugin_mentions must be a list" in _payload(handler)["error"]


def test_plugin_mentions_dedupe_preserving_order():
    mentions = [
        {"name": "one", "path": "plugin://one"},
        {"name": "two", "path": "plugin://two/path"},
        {"name": "one", "path": "plugin://one"},
    ]
    assert routes._normalize_codex_plugin_mentions(mentions) == mentions[:2]


def test_structured_mentions_forward_without_changing_visible_message():
    captured = {}

    class NewHermesAgent:
        def run_conversation(self, *, user_message, original_user_message=None):
            pass

    kwargs = {"user_message": "visible text", "persist_user_message": "visible text"}
    mentions = [{"name": "one", "path": "plugin://one"}]
    streaming._add_plugin_mentions_to_run_kwargs(
        NewHermesAgent(), kwargs, msg_text="visible text", plugin_mentions=mentions
    )
    captured.update(kwargs)

    assert captured["user_message"] == "visible text"
    assert captured["persist_user_message"] == "visible text"
    assert captured["original_user_message"] == {
        "content": "visible text",
        "plugin_mentions": mentions,
    }


def test_plain_request_and_older_hermes_remain_compatible():
    class OldHermesAgent:
        def run_conversation(self, *, user_message):
            pass

    kwargs = {"user_message": "hello"}
    streaming._add_plugin_mentions_to_run_kwargs(
        OldHermesAgent(), kwargs, msg_text="hello", plugin_mentions=[]
    )
    assert kwargs == {"user_message": "hello"}


def test_runner_signature_accepts_original_message_through_kwargs():
    class FlexibleHermesAgent:
        def run_conversation(self, *, user_message, **kwargs):
            pass

    kwargs = {"user_message": "hello"}
    mentions = [{"name": "one", "path": "plugin://one"}]
    assert streaming._add_plugin_mentions_to_run_kwargs(
        FlexibleHermesAgent(), kwargs, msg_text="hello", plugin_mentions=mentions
    ) is True
    assert kwargs["original_user_message"]["plugin_mentions"] == mentions


def test_runner_transport_receives_structured_original_message(monkeypatch):
    captured = []

    class RunnerClient:
        def start_run(self, request):
            captured.append(request)
            return {
                "run_id": "run-1",
                "stream_id": "run-1",
                "session_id": request.session_id,
            }

    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "runner-local")
    monkeypatch.setattr(routes, "_runtime_runner_client_factory", lambda: RunnerClient())
    session = types.SimpleNamespace(session_id="s1", profile="default")
    mentions = [{"name": "one", "path": "plugin://one"}]

    routes._start_run(
        session,
        msg="visible text",
        attachments=[],
        workspace="/tmp/workspace",
        model="gpt-test",
        model_provider="openai-codex",
        normalized_model=False,
        source="webui",
        route="/api/chat/start",
        plugin_mentions=mentions,
    )

    assert captured[0].message == "visible text"
    assert captured[0].metadata == {
        "route": "/api/chat/start",
        "plugin_mentions": mentions,
    }


def test_gateway_mentions_require_explicit_capability(monkeypatch):
    monkeypatch.setattr(config, "get_gateway_caps", lambda *_args, **_kwargs: {})
    assert config.gateway_supports_plugin_mentions("http://gateway") is False
    monkeypatch.setattr(
        config,
        "get_gateway_caps",
        lambda *_args, **_kwargs: {"plugin_mentions": True},
    )
    assert config.gateway_supports_plugin_mentions("http://gateway") is True
