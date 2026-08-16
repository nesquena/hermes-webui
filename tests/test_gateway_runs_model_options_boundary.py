"""Gateway-runs handler-boundary coverage for the session reasoning override.

The installed Hermes Agent API server (``gateway/platforms/api_server.py``)
reads reasoning for ``POST /v1/runs`` exclusively from ``body.model_options``:
``_handle_runs`` → ``_request_agent_overrides`` → ``_create_agent`` →
``_request_reasoning_config`` → ``AIAgent(reasoning_config=...)``. A top-level
``reasoning_effort`` field is ignored on that endpoint, so a session override
serialized only at the top level displays one value while gateway-runs executes
the profile/per-model default (nesquena re-gate 2026-08-13).

These tests exercise the real WebUI serialization path (actual
``_run_gateway_chat_streaming`` worker with only the network faked) and then
compose the captured wire body with the INSTALLED handler's parsing chain in a
subprocess, asserting the Agent reasoning config the receiver constructs.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.gateway_chat import (
    _GATEWAY_LEGACY_MODEL_OPTIONS_EFFORTS,
    _gateway_run_model_options,
)


# ── Locating the installed Hermes Agent (read-only) ─────────────────────────

def _installed_agent_dir():
    """Return the installed hermes-agent tree, mirroring api/startup.py discovery."""
    candidates = []
    env_dir = os.environ.get("HERMES_WEBUI_AGENT_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path.home() / ".hermes" / "hermes-agent")
    candidates.append(Path("/usr/local/lib/hermes-agent"))
    for candidate in candidates:
        if (candidate / "gateway" / "platforms" / "api_server.py").is_file():
            return candidate
    return None


def _installed_agent_python(agent_dir):
    for candidate in (
        agent_dir / "venv" / "bin" / "python",
        agent_dir / "venv" / "Scripts" / "python.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return None


_COMPOSE_SCRIPT = """
import json, sys
agent_dir = sys.argv[1]
sys.path.insert(0, agent_dir)
from gateway.platforms.api_server import _request_agent_overrides, _request_reasoning_config

vectors = json.loads(sys.stdin.read())
out = []
for vector in vectors:
    if vector.get("kind") == "run_body":
        # The exact composition _handle_runs performs before _create_agent.
        overrides = _request_agent_overrides(vector["body"], virtual_model="hermes-agent")
        out.append(_request_reasoning_config(overrides.get("model_options")))
    else:
        out.append(_request_reasoning_config(vector.get("model_options")))
print(json.dumps(out))
"""


def _compose_with_installed_handler(vectors):
    """Feed request vectors through the installed API server's parsing chain."""
    agent_dir = _installed_agent_dir()
    if agent_dir is None:
        pytest.skip("hermes-agent install not found (handler-boundary check unavailable)")
    python = _installed_agent_python(agent_dir)
    if python is None:
        pytest.skip("hermes-agent venv python not found (handler-boundary check unavailable)")
    proc = subprocess.run(
        [python, "-c", _COMPOSE_SCRIPT, str(agent_dir)],
        input=json.dumps(vectors),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        pytest.skip(f"installed api_server import failed: {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── model_options serialization (WebUI side) ────────────────────────────────

def test_gateway_run_model_options_serializes_effective_session_effort():
    assert _gateway_run_model_options(None) is None
    assert _gateway_run_model_options("") is None
    assert _gateway_run_model_options("none") == {
        "reasoning": {"enabled": False},
        "reasoning_effort": "none",
    }
    for effort in ("minimal", "low", "medium", "high", "xhigh"):
        assert _gateway_run_model_options(effort) == {
            "reasoning": {"enabled": True, "effort": effort},
            "reasoning_effort": effort,
        }


def test_gateway_run_model_options_degrades_supra_legacy_levels_by_default(monkeypatch):
    """Without an advertised wider ladder, max/ultra must ride as xhigh.

    The legacy receiver parser drops unknown levels and would otherwise build a
    bare enabled-config, silently losing the explicit effort.
    """
    import api.config as config

    monkeypatch.setattr(config, "get_gateway_caps", lambda *a, **k: {"reasoning_efforts": None})
    for effort in ("max", "ultra"):
        assert _gateway_run_model_options(effort, base_url="http://gw:8642") == {
            "reasoning": {"enabled": True, "effort": "xhigh"},
            "reasoning_effort": "xhigh",
        }


def test_gateway_run_model_options_sends_advertised_levels_verbatim(monkeypatch):
    """A gateway advertising the wider ladder via /v1/capabilities gets it verbatim."""
    import api.config as config

    wide = list(_GATEWAY_LEGACY_MODEL_OPTIONS_EFFORTS) + ["max", "ultra"]
    monkeypatch.setattr(config, "get_gateway_caps", lambda *a, **k: {"reasoning_efforts": wide})
    assert _gateway_run_model_options("ultra", base_url="http://gw:8642") == {
        "reasoning": {"enabled": True, "effort": "ultra"},
        "reasoning_effort": "ultra",
    }
    assert _gateway_run_model_options("max", "http://gw:8642") == {
        "reasoning": {"enabled": True, "effort": "max"},
        "reasoning_effort": "max",
    }


def test_gateway_run_model_options_degrades_to_advertised_intermediate(monkeypatch):
    """Degrade lands on the highest advertised level BELOW the requested one."""
    import api.config as config

    monkeypatch.setattr(
        config,
        "get_gateway_caps",
        lambda *a, **k: {"reasoning_efforts": ["low", "medium", "high", "xhigh", "max"]},
    )
    assert _gateway_run_model_options("ultra", base_url="http://gw:8642") == {
        "reasoning": {"enabled": True, "effort": "max"},
        "reasoning_effort": "max",
    }


# ── Wire capture: real WebUI worker, faked network ──────────────────────────

class _JsonResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self, _limit=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class _SseResponse:
    def __init__(self, lines):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _capture_runs_wire_body(session_effort, *, global_effort="high"):
    """Run the REAL gateway-runs worker for a session override; return the wire body."""
    import api.config as config
    from api.config import STREAMS, STREAMS_LOCK
    from api.gateway_chat import _run_gateway_chat_streaming

    stream_id = "sid-model-options-wire"
    q = MagicMock()
    with STREAMS_LOCK:
        STREAMS[stream_id] = q

    requests = []

    def fake_urlopen(req, *, timeout=None):
        requests.append(req)
        if req.full_url.endswith("/v1/runs"):
            return _JsonResponse({"run_id": "run-model-options"})
        return _SseResponse([
            b'data: {"event":"message.delta","delta":"Hello"}\n',
            b"\n",
            b'data: {"event":"run.completed","output":"Hello","usage":{"input_tokens":3,"output_tokens":1}}\n',
            b"\n",
        ])

    mock_session = MagicMock()
    mock_session.active_stream_id = stream_id
    mock_session.workspace = "/tmp"
    mock_session.model = "test-model"
    mock_session.model_provider = None
    mock_session.profile = None
    mock_session.context_messages = []
    mock_session.messages = []
    mock_session.pending_user_message = None
    mock_session.pending_attachments = None
    mock_session.pending_started_at = None
    mock_session.reasoning_effort = session_effort

    cfg = {"agent": {"reasoning_effort": global_effort}}

    try:
        with patch.dict(
            "os.environ",
            {
                "HERMES_WEBUI_CHAT_BACKEND": "gateway",
                "HERMES_WEBUI_GATEWAY_USE_RUNS_API": "1",
                "HERMES_WEBUI_GATEWAY_BASE_URL": "http://gw:8642",
                "HERMES_WEBUI_GATEWAY_API_KEY": "secret",
            },
        ):
            with patch("api.gateway_chat.gateway_supports_approval", lambda *_a, **_k: True), \
                 patch("api.config.get_config", lambda: cfg), \
                 patch.object(config, "resolve_model_reasoning_efforts", lambda *a, **k: list(config.VALID_REASONING_EFFORTS)), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("api.gateway_chat.get_session", return_value=mock_session), \
                 patch("api.gateway_chat._stream_writeback_is_current", return_value=True), \
                 patch("api.gateway_chat.merge_session_messages_append_only", return_value=[]):
                _run_gateway_chat_streaming(
                    session_id="sess-model-options",
                    msg_text="hi",
                    model="test-model",
                    workspace="/tmp",
                    stream_id=stream_id,
                )
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)

    run_requests = [req for req in requests if req.full_url.endswith("/v1/runs")]
    assert run_requests, "worker never POSTed /v1/runs"
    return json.loads(run_requests[0].data.decode("utf-8"))


def test_runs_wire_body_nests_session_effort_under_model_options():
    """The actual /v1/runs POST must carry the session value in model_options."""
    run_body = _capture_runs_wire_body("low", global_effort="high")

    assert run_body["model_options"]["reasoning"] == {"enabled": True, "effort": "low"}
    assert run_body["model_options"]["reasoning_effort"] == "low"
    # Top-level scalar kept for older/newer gateways that read it there.
    assert run_body["reasoning_effort"] == "low"


def test_runs_wire_body_carries_disabled_session_reasoning():
    run_body = _capture_runs_wire_body("none", global_effort="high")

    assert run_body["model_options"]["reasoning"] == {"enabled": False}
    assert run_body["model_options"]["reasoning_effort"] == "none"


# ── Composition with the INSTALLED handler (the actual receiver) ────────────

def test_installed_handler_builds_agent_reasoning_config_from_webui_run_body():
    """Compose the real WebUI wire body with the installed /v1/runs handler.

    ``_handle_runs`` builds ``AIAgent.reasoning_config`` from
    ``_request_reasoning_config(_request_agent_overrides(body)["model_options"])``
    whenever the request carries one (request wins over the gateway's own
    ``GatewayRunner._load_reasoning_config`` default). The constructed config
    must equal the session's effective effort — this is the exact lane the
    2026-08-13 re-gate found dropping the override.
    """
    run_body = _capture_runs_wire_body("low", global_effort="high")
    disabled_body = _capture_runs_wire_body("none", global_effort="high")

    results = _compose_with_installed_handler([
        {"kind": "run_body", "body": run_body},
        {"kind": "run_body", "body": disabled_body},
    ])

    # Not-None ⇒ the handler takes the request branch; the profile/per-model
    # default (_load_reasoning_config) is never consulted for this run.
    assert results[0] == {"enabled": True, "effort": "low"}
    assert results[1] == {"enabled": False}


def test_installed_handler_receives_explicit_effort_for_supra_legacy_levels(monkeypatch):
    """max/ultra must reach the installed parser as an effort it can carry.

    Sending 'ultra' verbatim to the installed parser yields a bare
    ``{"enabled": True}`` (the effort is dropped) — exactly why the WebUI
    degrades to the receiver's ladder instead of sending it blind.
    """
    import api.config as config

    monkeypatch.setattr(config, "get_gateway_caps", lambda *a, **k: {"reasoning_efforts": None})
    degraded = _gateway_run_model_options("ultra", base_url="http://gw:8642")

    results = _compose_with_installed_handler([
        {"kind": "model_options", "model_options": degraded},
        {"kind": "model_options", "model_options": {"reasoning": {"enabled": True, "effort": "ultra"}}},
    ])

    # Degraded wire value survives with an explicit effort…
    assert results[0] == {"enabled": True, "effort": "xhigh"}
    # …while raw 'ultra' would have lost the effort at the receiver.
    assert results[1] == {"enabled": True}
