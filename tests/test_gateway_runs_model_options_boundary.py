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
import re
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
    """Feed request vectors through the installed API server's parsing chain.

    The receiver is cross-repository authority, so this must never degrade into
    a silent ``skip``: a missing or unimportable receiver would make the whole
    contract claim disappear exactly when it stops holding. Two topologies:

    * ``HERMES_WEBUI_REQUIRE_AGENT_RECEIVER=1`` (the supported topology, and how
      a gate re-runs these): a missing tree, missing venv, or nonzero
      import/subprocess result is a hard FAILURE.
    * Otherwise (contributor checkout / upstream CI without an Agent install):
      fall back to the vendored parser contract in ``_RECEIVER_CONTRACT`` so the
      assertions still execute. ``test_receiver_contract_matches_installed_agent``
      independently proves that fixture matches the real receiver whenever one
      is present, so the fallback cannot drift unnoticed.
    """
    required = os.environ.get("HERMES_WEBUI_REQUIRE_AGENT_RECEIVER", "").strip() in ("1", "true", "yes")
    agent_dir = _installed_agent_dir()
    if agent_dir is None:
        if required:
            pytest.fail(
                "HERMES_WEBUI_REQUIRE_AGENT_RECEIVER is set but no hermes-agent install "
                "was found; the receiver contract cannot be verified."
            )
        return _compose_with_contract(vectors)
    python = _installed_agent_python(agent_dir)
    if python is None:
        if required:
            pytest.fail(f"hermes-agent venv python not found under {agent_dir}")
        return _compose_with_contract(vectors)
    proc = subprocess.run(
        [python, "-c", _COMPOSE_SCRIPT, str(agent_dir)],
        input=json.dumps(vectors),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        if required:
            pytest.fail(f"installed api_server import failed: {proc.stderr.strip()[:400]}")
        return _compose_with_contract(vectors)
    return json.loads(proc.stdout.strip().splitlines()[-1])


# The WIDEST receiver ladder currently in existence — the upstream
# gateway/platforms/api_server.py ``_REASONING_EFFORTS``. Two generations are
# deployed in the wild: an older six-level parser and the current eight-level
# one that also carries 'max'/'ultra'. Neither advertises itself, which is why
# WebUI fails closed onto the six safe levels until a receiver declares its
# ladder (hermes-agent #92839).
#
# This tuple is only the FALLBACK used when no Agent install is present; when
# one is, the real parser is authority.
_RECEIVER_CONTRACT = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def _installed_receiver_efforts():
    """Return the installed receiver's accepted ladder, or None if absent."""
    agent_dir = _installed_agent_dir()
    if agent_dir is None:
        return None
    source = (agent_dir / "gateway" / "platforms" / "api_server.py").read_text(encoding="utf-8")
    match = re.search(r"_REASONING_EFFORTS\s*=\s*frozenset\(\s*\{(.*?)\}\s*\)", source, re.S)
    assert match, "could not locate _REASONING_EFFORTS in the installed receiver"
    return {
        level.strip().strip("\"'")
        for level in match.group(1).split(",")
        if level.strip().strip("\"'")
    }


def _effective_receiver_efforts():
    """The ladder the vectors in this module are actually parsed against."""
    installed = _installed_receiver_efforts()
    return installed if installed is not None else set(_RECEIVER_CONTRACT)


def _contract_reasoning_config(model_options):
    """Local mirror of the receiver's ``_request_reasoning_config`` contract."""
    if not isinstance(model_options, dict):
        return None
    reasoning = model_options.get("reasoning")
    enabled = None
    effort = model_options.get("reasoning_effort")
    if isinstance(reasoning, dict):
        enabled = reasoning.get("enabled")
        effort = reasoning.get("effort", effort)
    effort_norm = str(effort).strip().lower() if effort is not None else ""
    if enabled is False or effort_norm == "none":
        return {"enabled": False}
    if effort_norm in _RECEIVER_CONTRACT and effort_norm != "none":
        return {"enabled": True, "effort": effort_norm}
    if enabled is True:
        return {"enabled": True}
    return None


def _compose_with_contract(vectors):
    out = []
    for vector in vectors:
        if vector.get("kind") == "run_body":
            out.append(_contract_reasoning_config(vector["body"].get("model_options")))
        else:
            out.append(_contract_reasoning_config(vector.get("model_options")))
    return out


def test_receiver_contract_matches_installed_agent():
    """Pin the relationship between WebUI's policy and the real receiver.

    Two receiver generations exist and neither advertises its ladder, so this
    asserts the invariants that must hold for ANY of them rather than a single
    equality that would break on the other generation:

    * every level WebUI degrades onto must be accepted by the installed parser
      (otherwise the "safe" fallback is itself unsafe);
    * the installed ladder must not exceed the widest known contract (a new
      level upstream means this policy needs revisiting).
    """
    agent_dir = _installed_agent_dir()
    required = os.environ.get("HERMES_WEBUI_REQUIRE_AGENT_RECEIVER", "").strip() in ("1", "true", "yes")
    if agent_dir is None:
        if required:
            pytest.fail("HERMES_WEBUI_REQUIRE_AGENT_RECEIVER is set but no hermes-agent install was found")
        pytest.skip("no hermes-agent install to compare against (fallback contract still exercised elsewhere)")
    installed = _installed_receiver_efforts()

    unsafe = sorted(set(_GATEWAY_LEGACY_MODEL_OPTIONS_EFFORTS) - installed)
    assert not unsafe, (
        "WebUI degrades onto levels the installed receiver does not accept: "
        f"{unsafe}. The fail-closed ladder must be a subset of every receiver."
    )
    unknown = sorted(installed - set(_RECEIVER_CONTRACT))
    assert not unknown, (
        "The installed receiver accepts levels this contract does not know "
        f"about ({unknown}); update _RECEIVER_CONTRACT and the WebUI "
        "degradation policy together."
    )


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


def test_gateway_run_model_options_degrades_supra_levels_without_an_advertised_ladder(monkeypatch):
    """No advertisement means no proof, so max/ultra ride as xhigh.

    Both an unreachable probe and a reachable-but-silent one are ambiguous:
    neither identifies the receiver generation. Failing closed costs one rung
    of effort; failing open costs the effort entirely on an older receiver.
    """
    import api.config as config

    ambiguous_probes = (
        {"reasoning_efforts": None, "capabilities_reachable": False, "probe_error": "URLError: refused"},
        {"reasoning_efforts": None, "capabilities_reachable": True, "probe_error": None},
        {},
    )
    for caps in ambiguous_probes:
        monkeypatch.setattr(config, "get_gateway_caps", lambda *a, _c=caps, **k: _c)
        for effort in ("max", "ultra"):
            assert _gateway_run_model_options(effort, base_url="http://gw:8642") == {
                "reasoning": {"enabled": True, "effort": "xhigh"},
                "reasoning_effort": "xhigh",
            }, f"caps={caps} effort={effort} should fail closed to xhigh"


def test_gateway_run_model_options_preserves_supra_levels_when_receiver_advertises(monkeypatch):
    """An explicit advertisement is authority: max/ultra ride verbatim.

    This is the path hermes-agent #92839 opens. Once the receiver declares
    ``features.reasoning_efforts``, WebUI stops degrading and sends the
    session's exact choice.
    """
    import api.config as config

    monkeypatch.setattr(
        config,
        "get_gateway_caps",
        lambda *a, **k: {
            "reasoning_efforts": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
            "capabilities_reachable": True,
            "probe_error": None,
        },
    )
    for effort in ("max", "ultra"):
        assert _gateway_run_model_options(effort, base_url="http://gw:8642") == {
            "reasoning": {"enabled": True, "effort": effort},
            "reasoning_effort": effort,
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


def test_installed_handler_behaviour_on_supra_legacy_levels_justifies_failing_closed():
    """Prove WHY WebUI fails closed, against whichever receiver is installed.

    Exactly one of two things must be true of a raw 'max'/'ultra' request:

    * the receiver is an older generation and DROPS the effort, yielding a bare
      ``{"enabled": True}`` — the silent downgrade the degradation prevents; or
    * the receiver is current and preserves it verbatim.

    Both are acceptable receiver behaviour; what is NOT acceptable is WebUI
    guessing which one is on the other end when nothing advertises it.
    """
    accepted = _effective_receiver_efforts()
    results = _compose_with_installed_handler([
        {"kind": "model_options", "model_options": {"reasoning": {"enabled": True, "effort": "max"}}},
        {"kind": "model_options", "model_options": {"reasoning": {"enabled": True, "effort": "ultra"}}},
    ])

    for level, result in zip(("max", "ultra"), results, strict=True):
        if level in accepted:
            assert result == {"enabled": True, "effort": level}
        else:
            # The effort is silently dropped — the exact failure mode that
            # makes sending an unadvertised supra-legacy level unsafe.
            assert result == {"enabled": True}


def test_installed_handler_receives_explicit_effort_after_legacy_degradation(monkeypatch):
    """When the receiver IS legacy, the degraded value must still be explicit.

    An unreachable probe forces the legacy ladder. The degraded wire value has
    to reach the parser as a level it can carry, otherwise the request resolves
    to the profile default instead of the session's choice.
    """
    import api.config as config

    monkeypatch.setattr(
        config,
        "get_gateway_caps",
        lambda *a, **k: {
            "reasoning_efforts": None,
            "capabilities_reachable": False,
            "probe_error": "URLError: connection refused",
        },
    )
    degraded = _gateway_run_model_options("ultra", base_url="http://gw:8642")
    assert degraded["reasoning_effort"] == "xhigh"

    results = _compose_with_installed_handler([
        {"kind": "model_options", "model_options": degraded},
    ])

    assert results[0] == {"enabled": True, "effort": "xhigh"}


def test_installed_handler_boundary_covers_the_full_request_ladder():
    """Exercise /v1/runs construction across the whole ladder, incl. none/low/max/ultra.

    The WebUI serializer fails closed, so a session pinned to 'max'/'ultra'
    reaches an unadvertised receiver as 'xhigh' — an explicit effort it is
    guaranteed to honour, never a bare enabled flag.
    """
    run_bodies = [
        {"kind": "run_body", "body": _capture_runs_wire_body(level, global_effort="high")}
        for level in ("none", "low", "max", "ultra")
    ]
    results = _compose_with_installed_handler(run_bodies)

    assert results[0] == {"enabled": False}
    assert results[1] == {"enabled": True, "effort": "low"}
    for result in results[2:]:
        assert result == {"enabled": True, "effort": "xhigh"}
