"""Gateway mode refuses entry points that still run the agent in-process.

``/btw``, ``/background``, ``/compress``, the synchronous ``/api/chat`` and the
one-shot model calls (update summaries, commit messages) build an in-process
agent. On a gateway-backed deployment they either fail with a
provider-credential error (the WebUI usually has no model key) or silently run a
turn outside the gateway. Until each is routed through the gateway they answer
409, matching the existing MoA refusal, and must do so before creating a hidden
session, job or any other state.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import helpers, routes  # noqa: E402

_REFUSAL = "is unavailable on gateway-backed sessions"


@pytest.fixture
def captured(monkeypatch):
    """Capture the JSON response written through ``helpers.bad``."""
    box = {}

    def fake_j(handler, payload, status=200, extra_headers=None, *, pretty=True):
        box["payload"] = payload
        box["status"] = status

    monkeypatch.setattr(helpers, "j", fake_j)
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    return box


@pytest.fixture
def gateway_mode(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")


def _fail_on_state_access(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError(
            "handler touched session state before the gateway refusal"
        )

    monkeypatch.setattr(routes, "get_session", boom)
    monkeypatch.setattr(routes, "new_session", boom, raising=False)


@pytest.mark.parametrize(
    ("handler_name", "body", "route"),
    [
        ("_handle_btw", {"session_id": "s1", "question": "why?"}, "/btw"),
        ("_handle_background", {"session_id": "s1", "prompt": "do it"}, "/background"),
        ("_handle_chat_sync", {"session_id": "s1", "message": "hi"}, "/api/chat"),
        ("_handle_session_compress_start", {"session_id": "s1"}, "/compress"),
        ("_handle_session_compress", {"session_id": "s1"}, "/compress"),
        ("_handle_git_commit_message", {"session_id": "s1"}, "/api/git/commit-message"),
        (
            "_handle_git_commit_message_selected",
            {"session_id": "s1"},
            "/api/git/commit-message-selected",
        ),
    ],
)
def test_in_process_routes_refuse_in_gateway_mode(
    monkeypatch, captured, gateway_mode, handler_name, body, route
):
    _fail_on_state_access(monkeypatch)

    getattr(routes, handler_name)(object(), dict(body))

    assert captured["status"] == 409
    assert captured["payload"] == {
        "error": f"{route} {_REFUSAL}",
        "code": "gateway_mode_unsupported",
        "feature": route,
    }


def test_updates_summary_refuses_in_gateway_mode(
    monkeypatch, captured, gateway_mode
):
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"updates": {}})
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)

    handled = routes.handle_post(object(), SimpleNamespace(path="/api/updates/summary"))

    assert handled is not False, "the route fell through to 404"
    assert captured["status"] == 409
    assert captured["payload"]["error"] == f"/api/updates/summary {_REFUSAL}"
    assert captured["payload"]["code"] == "gateway_mode_unsupported"
    assert captured["payload"]["feature"] == "/api/updates/summary"


def test_local_backend_is_not_refused(monkeypatch, captured):
    """The refusal is gateway-only: the in-process backend still reaches its handler."""
    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    monkeypatch.setattr(
        routes, "get_session", lambda _sid: (_ for _ in ()).throw(KeyError("missing"))
    )

    routes._handle_session_compress_start(object(), {"session_id": "s1"})

    assert captured["status"] == 404
    assert "unavailable on gateway-backed sessions" not in captured["payload"]["error"]


def test_refusal_helper_is_false_off_gateway(monkeypatch):
    from api.gateway_chat import refuse_on_gateway_backend

    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    assert not refuse_on_gateway_backend(object(), "/btw", {})


@pytest.fixture
def captured_route_j(monkeypatch):
    """Capture the payload returned through ``routes.j`` (success responses)."""
    box = {}

    def fake_j(handler, payload, status=200, extra_headers=None, *, pretty=True):
        box["payload"] = payload
        box["status"] = status
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    return box


def _prime_handoff_summary(monkeypatch):
    """Stub the state reads the handoff handler performs before the model call."""
    from api import models

    msgs = [
        {"role": "user", "content": "Please fix the failing build", "timestamp": 1},
        {"role": "assistant", "content": "Build fixed; tests pass", "timestamp": 2},
    ]
    monkeypatch.setattr(models, "get_cli_session_messages", lambda _sid: msgs)
    monkeypatch.setattr(
        models, "count_conversation_rounds", lambda _sid, since=None: 10
    )
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "_persist_handoff_summary", lambda *a, **k: {})


def _stub_model_resolution(monkeypatch):
    """Make execution reach in-process agent construction deterministically.

    The handler imports the agent runtime lazily and wraps the whole model
    path in ``except Exception`` → fallback, so without these stubs a missing
    module would make both backend modes return the fallback and the
    gateway/no-agent assertion below would pass for the wrong reason.
    """
    import types

    import api.config as api_config
    import api.oauth as api_oauth

    fake_runtime_provider = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_provider.resolve_runtime_provider = lambda *a, **k: {}
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_provider
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(
        sys.modules, "hermes_cli.runtime_provider", fake_runtime_provider
    )
    monkeypatch.setattr(routes, "ensure_agent_runtime_current", lambda: None)
    monkeypatch.setattr(routes, "require_ai_agent_class", lambda: object)
    monkeypatch.setattr(
        routes,
        "_resolve_agent_connection_bundle",
        lambda provider, api_key, base_url, _rt: {
            "provider": provider,
            "api_key": api_key,
            "base_url": base_url,
        },
    )
    monkeypatch.setattr(routes, "_agent_bundle_kwargs", lambda _cls, _bundle: {})
    monkeypatch.setattr(
        api_config, "model_with_provider_context", lambda model, provider: model
    )
    monkeypatch.setattr(
        api_config, "resolve_model_provider", lambda _model: ("m", "openai", None)
    )
    monkeypatch.setattr(
        api_oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda _fn, requested=None: {"api_key": "test-key", "provider": "openai"},
    )


def test_handoff_summary_never_builds_an_agent_in_gateway_mode(
    monkeypatch, captured_route_j, gateway_mode
):
    """#8971 item 2: gateway mode returns the deterministic fallback.

    The handler exists for gateway sessions, so the fix is the local fallback,
    not a 409 — but it must never construct the in-process helper agent that
    calls the model outside the gateway and the session's profile.
    """
    _prime_handoff_summary(monkeypatch)
    _stub_model_resolution(monkeypatch)

    built = []

    def record_agent_construction(*args, **kwargs):
        built.append((args, kwargs))
        raise AssertionError("unreachable fake agent")

    monkeypatch.setattr(
        routes, "construct_memoryless_helper_agent", record_agent_construction
    )

    routes._handle_handoff_summary(object(), {"session_id": "s1"})

    # If the agent were built, its construction would be recorded even though
    # the handler's own ``except Exception`` turns the failure into a fallback.
    assert built == []
    assert captured_route_j["status"] == 200
    assert captured_route_j["payload"]["ok"] is True
    assert captured_route_j["payload"]["fallback"] is True
    assert captured_route_j["payload"]["summary"]
    assert captured_route_j["payload"]["rounds"] == 10


def test_handoff_summary_reaches_the_model_path_off_gateway(
    monkeypatch, captured_route_j
):
    """The gateway gate is the only thing that skips agent construction."""
    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    _prime_handoff_summary(monkeypatch)
    _stub_model_resolution(monkeypatch)

    class FakeAgent:
        reasoning_config = None

    built = []

    def record_agent_construction(*args, **kwargs):
        built.append((args, kwargs))
        return FakeAgent()

    monkeypatch.setattr(
        routes, "construct_memoryless_helper_agent", record_agent_construction
    )

    routes._handle_handoff_summary(object(), {"session_id": "s1"})

    # Off gateway, the handler proceeds into the model path; the fake agent
    # then fails inside it and the handler degrades to the fallback, which is
    # fine — the assertion is that construction was attempted at all.
    assert built, "gateway gate skipped agent construction off gateway"

