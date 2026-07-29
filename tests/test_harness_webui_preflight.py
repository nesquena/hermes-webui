from __future__ import annotations

from types import SimpleNamespace

import api.streaming as streaming


class FakePlugins:
    def __init__(self, returns=(), *, raise_on_invoke=False):
        self.returns = list(returns)
        self.raise_on_invoke = raise_on_invoke
        self.discovered = False
        self.events = []

    def discover_plugins(self):
        self.discovered = True

    def invoke_hook(self, name, **kwargs):
        assert name == "pre_gateway_dispatch"
        event = kwargs["event"]
        assert isinstance(event, SimpleNamespace)
        self.events.append(event)
        if self.raise_on_invoke:
            raise RuntimeError("plugin failure")
        return list(self.returns)


def test_webui_preflight_applies_rewrite_without_mutating_event_shape(monkeypatch):
    fake = FakePlugins([{"action": "rewrite", "text": "[Harness]\n请修复这个 bug"}])
    monkeypatch.setattr(streaming.importlib, "import_module", lambda name: fake)

    rewritten = streaming._apply_webui_pre_gateway_dispatch_preflight(
        "请修复这个 bug",
        session_id="sid-123",
        workspace="/tmp/workspace",
        profile="default",
    )

    assert fake.discovered is True
    assert rewritten == "[Harness]\n请修复这个 bug"
    assert fake.events[0].platform == "webui"
    assert fake.events[0].chat_id == "sid-123"
    assert fake.events[0].raw == {
        "source": "webui",
        "session_id": "sid-123",
        "workspace": "/tmp/workspace",
        "profile": "default",
    }

def test_webui_preflight_returns_model_facing_rewrite_without_mutating_input(monkeypatch):
    fake = FakePlugins([{"action": "rewrite", "text": "[Harness]\n请修复这个 bug"}])
    monkeypatch.setattr(streaming.importlib, "import_module", lambda name: fake)
    original_text = "请修复这个 bug"

    rewritten = streaming._apply_webui_pre_gateway_dispatch_preflight(
        original_text,
        session_id="sid-123",
        workspace="/tmp/workspace",
        profile="default",
    )

    assert original_text == "请修复这个 bug"
    assert rewritten == "[Harness]\n请修复这个 bug"
    assert fake.events[0].text == "[Harness]\n请修复这个 bug"


def test_webui_preflight_ignores_non_rewrite_actions(monkeypatch):
    fake = FakePlugins([
        {"action": "allow", "text": "allowed text"},
        {"action": "skip", "text": "blocked text"},
    ])
    monkeypatch.setattr(streaming.importlib, "import_module", lambda name: fake)

    assert streaming._apply_webui_pre_gateway_dispatch_preflight(
        "解释一下什么是 MCP",
        session_id="sid-123",
        workspace="/tmp/workspace",
    ) == "解释一下什么是 MCP"


def test_webui_preflight_fails_open_when_plugin_errors(monkeypatch):
    fake = FakePlugins(raise_on_invoke=True)
    monkeypatch.setattr(streaming.importlib, "import_module", lambda name: fake)

    assert streaming._apply_webui_pre_gateway_dispatch_preflight(
        "请重构这个模块",
        session_id="sid-123",
        workspace="/tmp/workspace",
    ) == "请重构这个模块"
