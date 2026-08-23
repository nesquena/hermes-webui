"""#6018 final gate — clamp reasoning after Agent fallback/model transitions."""

import ast
from contextlib import nullcontext
import io
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from api.agent_runtime import _destination_aware_ai_agent_class


ROOT = Path(__file__).resolve().parents[1]


class _MinimalAgent:
    def __init__(self, *, model, provider, base_url, reasoning_config):
        self.model = model
        self.provider = provider
        self.base_url = base_url
        self.reasoning_config = reasoning_config


def _guarded_agent(model="gpt-5.6-sol", effort="ultra"):
    guarded = _destination_aware_ai_agent_class(_MinimalAgent)
    return guarded(
        model=model,
        provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        reasoning_config={"enabled": True, "effort": effort},
    )


def test_transition_assignments_follow_destination_model_ceiling():
    agent = _guarded_agent()
    assert agent.reasoning_config["effort"] == "ultra"

    # Both installed-Agent transition chokepoints assign reasoning_config only
    # after updating agent.model/provider. The shared assignment guard therefore
    # clamps fallback and /model writes against the destination.
    agent.model = "gpt-5.5"
    agent.reasoning_config = {"enabled": True, "effort": "ultra"}
    assert agent.reasoning_config["effort"] == "xhigh"

    agent.model = "o3"
    agent.reasoning_config = {"enabled": True, "effort": "ultra"}
    assert agent.reasoning_config["effort"] == "high"


def test_gpt56_ultra_to_gpt55_fallback_emits_xhigh_on_real_agent_transport(
    monkeypatch,
):
    """Compose the installed Agent resolver and Responses wire transport."""
    constants = pytest.importorskip("hermes_constants")
    codex = pytest.importorskip("agent.transports.codex")
    # ResponsesApiTransport imports this one identity constant lazily from the
    # heavyweight run_agent module. Supply only that production input so the
    # wire test does not require the Agent's unrelated terminal/browser deps.
    monkeypatch.setitem(
        sys.modules,
        "run_agent",
        SimpleNamespace(DEFAULT_AGENT_IDENTITY="Hermes Agent"),
    )

    cfg = {"agent": {"reasoning_effort": "ultra"}}
    agent = _guarded_agent(
        effort=constants.resolve_reasoning_config(cfg, "gpt-5.6-sol")["effort"]
    )
    assert agent.reasoning_config["effort"] == "ultra"

    # Production fallback activation updates the route, then assigns the result
    # of resolve_reasoning_config(load_config(), agent.model).
    agent.model = "gpt-5.5"
    agent.reasoning_config = constants.resolve_reasoning_config(cfg, agent.model)
    assert agent.reasoning_config["effort"] == "xhigh"

    wire = codex.ResponsesApiTransport().build_kwargs(
        agent.model,
        [{"role": "user", "content": "fallback probe"}],
        reasoning_config=agent.reasoning_config,
        provider=agent.provider,
        base_url=agent.base_url,
        is_codex_backend=True,
    )
    assert wire["reasoning"]["effort"] == "xhigh"
    assert wire["reasoning"]["effort"] != "ultra"


def test_required_agent_class_is_cached_and_destination_aware(monkeypatch):
    """The helper shared by every WebUI constructor must never expose the raw class."""
    from api import agent_runtime

    class RawAgent:
        pass

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=RawAgent))

    first = agent_runtime.require_ai_agent_class()
    second = agent_runtime.require_ai_agent_class()

    assert first is second
    assert first is not RawAgent
    assert issubclass(first, RawAgent)
    assert getattr(first, "_webui_destination_reasoning_guard", False) is True


def test_api_chat_sync_clamps_transition_through_production_constructor(
    monkeypatch, tmp_path
):
    """POST /api/chat must construct the destination-aware class, not raw AIAgent."""
    from api import config, oauth, routes

    captured = {}

    class RawAgent:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]
            self.provider = kwargs["provider"]
            self.base_url = kwargs["base_url"]
            self.reasoning_config = {"enabled": True, "effort": "ultra"}

        def run_conversation(self, **kwargs):
            assert self.reasoning_config["effort"] == "ultra"
            self.model = "gpt-5.5"
            self.reasoning_config = {"enabled": True, "effort": "ultra"}
            captured["fallback_effort"] = self.reasoning_config["effort"]
            return {
                "messages": [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "ok"},
                ],
                "final_response": "ok",
                "completed": True,
            }

    class Session:
        session_id = "sync-reasoning-transition"
        workspace = str(tmp_path)
        model = "gpt-5.6-sol"
        model_provider = "openai-codex"
        messages = []
        context_messages = []
        title = "Reasoning transition"
        pending_user_source = None

        def save(self):
            return None

        def compact(self):
            return {"session_id": self.session_id, "messages": self.messages}

    session = Session()
    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=RawAgent))
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    runtime_provider = ModuleType("hermes_cli.runtime_provider")
    runtime_provider.resolve_runtime_provider = lambda **_kwargs: {}
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", runtime_provider)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda _value: tmp_path)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: nullcontext())
    monkeypatch.setattr(
        routes,
        "_read_profile_model_config",
        lambda *_args: ("openai-codex", "gpt-5.6-sol", {}),
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("gpt-5.6-sol", "openai-codex"),
    )
    monkeypatch.setattr(
        config,
        "resolve_model_provider",
        lambda _model: (
            "gpt-5.6-sol",
            "openai-codex",
            "https://chatgpt.com/backend-api/codex",
        ),
    )
    monkeypatch.setattr(
        oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])
    monkeypatch.setattr(routes, "public_session_projection", lambda payload: payload)

    class Handler:
        def __init__(self):
            self.status = None
            self.wfile = io.BytesIO()

        def send_response(self, status):
            self.status = status

        def send_header(self, _name, _value):
            return None

        def end_headers(self):
            return None

    handler = Handler()
    routes._handle_chat_sync(
        handler,
        {
            "session_id": session.session_id,
            "message": "fallback probe",
            "workspace": str(tmp_path),
        },
    )

    assert handler.status == 200
    assert captured["fallback_effort"] == "xhigh"
    assert captured["fallback_effort"] != "ultra"


def test_routes_cannot_construct_raw_ai_agent():
    """Non-vacuous AST guard for every routes.py constructor, including /api/chat."""
    tree = ast.parse((ROOT / "api" / "routes.py").read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    constructor_owners = [
        node
        for node in functions
        if any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "AIAgent"
            for child in ast.walk(node)
        )
    ]
    raw_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "run_agent"
        and any(alias.name == "AIAgent" for alias in node.names)
    ]
    chat_dispatches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(child, ast.Constant) and child.value == "/api/chat"
            for child in ast.walk(node.test)
        )
        and any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "_handle_chat_sync"
            for statement in node.body
            for child in ast.walk(statement)
        )
    ]

    assert len(constructor_owners) == 5, "guard must inventory every routes.py AIAgent constructor"
    assert "_handle_chat_sync" in {node.name for node in constructor_owners}
    for owner in constructor_owners:
        wrapped_bindings = [
            node
            for node in ast.walk(owner)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "AIAgent"
                for target in node.targets
            )
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "require_ai_agent_class"
        ]
        assert len(wrapped_bindings) == 1, owner.name
    assert len(chat_dispatches) == 1, "POST /api/chat must dispatch to the guarded sync handler"
    assert raw_imports == []