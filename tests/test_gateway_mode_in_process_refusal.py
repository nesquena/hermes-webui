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
