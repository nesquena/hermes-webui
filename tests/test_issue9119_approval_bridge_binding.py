"""#9119: WebUI binds each Agent approval name separately and fails closed.

``api/route_approvals.py`` used to import 13 names from ``tools.approval`` in one
statement and swap ALL of them for private stubs when ANY one was missing. Hermes
Agent removed ``_pending`` and ``submit_pending`` (NousResearch/hermes-agent#156131).
Against such an Agent WebUI still showed the approval card and answered "ok", but
it settled a private queue under a private lock, so the Agent thread stayed parked
and denied the command at ``approvals.timeout``; YOLO and session grants never
reached the Agent either, and nothing was logged.

The unit tests execute a fresh copy of ``api/route_approvals.py`` against a
stand-in ``tools.approval``. The copy is a separate module object, so the shared
``api.route_approvals`` that routes.py and streaming.py bound at import is never
reloaded or replaced, and ``sys.modules`` is restored right after the copy loads.
The end-to-end tests drive the real installed Agent's approval gate in a
subprocess, with the #156131 names deleted in memory (the Agent tree on disk is
never touched).
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import logging
import os
import subprocess
import sys
import threading
import types
import urllib.parse
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from api import route_approvals
from api import routes

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTE_APPROVALS_PATH = REPO_ROOT / "api" / "route_approvals.py"

# The 13 tools.approval names WebUI imported in one statement before #9119.
AGENT_NAMES = (
    "submit_pending",
    "approve_session",
    "approve_permanent",
    "save_permanent_allowlist",
    "is_approved",
    "_pending",
    "_lock",
    "_permanent_approved",
    "_gateway_queues",
    "resolve_gateway_approval",
    "enable_session_yolo",
    "disable_session_yolo",
    "is_session_yolo_enabled",
)
# Names WebUI binds to the Agent's own object (same attribute name on both sides).
# submit_pending and is_approved were never called by WebUI and are no longer bound.
BOUND_NAMES = tuple(n for n in AGENT_NAMES if n not in ("submit_pending", "is_approved"))
CRITICAL_NAMES = ("_lock", "_gateway_queues", "resolve_gateway_approval")
SCOPE_NAMES = ("approve_session", "approve_permanent", "save_permanent_allowlist", "_permanent_approved")
YOLO_NAMES = ("enable_session_yolo", "disable_session_yolo", "is_session_yolo_enabled")
OPTIONAL_NAMES = ("_pending", "submit_pending", "is_approved")
FULL_BRIDGE = {
    "agent": True,
    "available": True,
    "scopes_available": True,
    "yolo_available": True,
    "missing": [],
}

_probe_seq = itertools.count()


def _fake_agent_module(missing=()):
    """A stand-in ``tools.approval`` exposing every name except ``missing``."""
    module = types.ModuleType("tools.approval")
    module.__file__ = "<stand-in tools/approval.py>"
    for name in AGENT_NAMES:
        if name in missing:
            continue
        if name == "_lock":
            value = threading.RLock()
        elif name in ("_pending", "_gateway_queues"):
            value = {}
        elif name == "_permanent_approved":
            value = set()
        else:
            def value(*_args, **_kwargs):
                return None
            value.__name__ = value.__qualname__ = f"agent_{name}"
        setattr(module, name, value)
    return module


def _load_route_approvals(monkeypatch, agent_module):
    """Execute api/route_approvals.py as a NEW module with ``agent_module`` as tools.approval.

    ``agent_module=None`` makes ``import tools.approval`` raise ImportError (no Agent).
    """
    with monkeypatch.context() as mp:
        package = types.ModuleType("tools")
        package.__path__ = []
        if agent_module is not None:
            package.approval = agent_module
        mp.setitem(sys.modules, "tools", package)
        mp.setitem(sys.modules, "tools.approval", agent_module)
        spec = importlib.util.spec_from_file_location(
            f"_route_approvals_issue9119_probe_{next(_probe_seq)}", ROUTE_APPROVALS_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def _bridge_records(caplog, module, level=logging.WARNING):
    return [r for r in caplog.records if r.name == module.__name__ and r.levelno >= level]


@pytest.mark.parametrize("missing", AGENT_NAMES)
def test_each_agent_name_binds_separately(monkeypatch, caplog, missing):
    caplog.set_level(logging.INFO)
    agent = _fake_agent_module(missing=(missing,))
    probe = _load_route_approvals(monkeypatch, agent)

    # Every name the Agent still has is the Agent's own object, not a stand-in.
    for name in BOUND_NAMES:
        if name != missing:
            assert getattr(probe, name) is getattr(agent, name), f"{name} not bound to the Agent"

    bridge = probe.APPROVAL_BRIDGE
    records = _bridge_records(caplog, probe)
    if missing in OPTIONAL_NAMES:
        # Only _pending falls back locally, silently: the Agent never reads it.
        assert bridge == FULL_BRIDGE
        assert records == []
        assert probe.approval_bridge_failure(choice="always", yolo=True) is None
        probe.require_approval_bridge()
        if missing == "_pending":
            assert probe._pending == {} and isinstance(probe._pending, dict)
        return

    assert bridge["agent"] is True
    assert bridge["missing"] == [missing]
    assert len(records) == 1, [r.getMessage() for r in records]
    assert missing in records[0].getMessage()
    if missing in CRITICAL_NAMES:
        assert records[0].levelno == logging.ERROR
        assert bridge["available"] is False
        for choice in ("once", "session", "always", "deny"):
            payload, status = probe.approval_bridge_failure(choice=choice)
            assert status == 503
            assert payload["ok"] is False
            assert payload["code"] == "approval_bridge_unavailable"
            assert payload["missing"] == [missing]
            assert missing in payload["error"]
        with pytest.raises(probe.ApprovalBridgeUnavailable, match=missing):
            probe.require_approval_bridge()
    elif missing in SCOPE_NAMES:
        assert (bridge["available"], bridge["scopes_available"], bridge["yolo_available"]) == (True, False, True)
        assert probe.approval_bridge_failure(choice="once") is None
        assert probe.approval_bridge_failure(choice="deny") is None
        for choice in ("session", "always"):
            payload, status = probe.approval_bridge_failure(choice=choice)
            assert (status, payload["ok"], payload["code"]) == (409, False, "approval_scope_unavailable")
        probe.require_approval_bridge()
    else:
        assert missing in YOLO_NAMES
        assert (bridge["available"], bridge["scopes_available"], bridge["yolo_available"]) == (True, True, False)
        assert probe.approval_bridge_failure(choice="always") is None
        payload, status = probe.approval_bridge_failure(yolo=True)
        assert (status, payload["ok"], payload["code"]) == (503, False, "yolo_unavailable")
        assert "yolo_enabled" not in payload
        probe.require_approval_bridge()


def test_agent_without_pending_and_submit_pending_keeps_the_whole_bridge(monkeypatch, caplog):
    """The NousResearch/hermes-agent#156131 shape: both names gone at once."""
    caplog.set_level(logging.INFO)
    agent = _fake_agent_module(missing=("_pending", "submit_pending"))
    probe = _load_route_approvals(monkeypatch, agent)

    for name in BOUND_NAMES:
        if name != "_pending":
            assert getattr(probe, name) is getattr(agent, name), name
    assert probe._pending == {}
    assert probe.APPROVAL_BRIDGE == FULL_BRIDGE
    assert _bridge_records(caplog, probe) == []


def test_full_agent_shares_its_pending_dict(monkeypatch):
    agent = _fake_agent_module()
    probe = _load_route_approvals(monkeypatch, agent)
    for name in BOUND_NAMES:
        assert getattr(probe, name) is getattr(agent, name), name
    assert probe.APPROVAL_BRIDGE == FULL_BRIDGE


def test_agentless_mode_keeps_labelled_local_stand_ins(monkeypatch):
    """No Agent (CI): local WebUI-only state that never fails open."""
    probe = _load_route_approvals(monkeypatch, None)

    assert probe.APPROVAL_BRIDGE == {**FULL_BRIDGE, "agent": False}
    # A plain lock, as in older Agents: WebUI never re-enters it, and a
    # re-entry must show up here rather than only against an older Agent.
    with probe._lock:
        assert probe._lock.acquire(blocking=False) is False
    assert probe.resolve_gateway_approval("sid", "once") == 0
    assert probe.is_session_yolo_enabled("sid") is False
    # No fail-open "everything is approved" stand-in.
    assert getattr(probe, "is_approved", lambda *_a: False)("sid", "key") is False
    # The local respond path keeps working without an Agent.
    assert probe.approval_bridge_failure(choice="always", yolo=True) is None
    probe.require_approval_bridge()


def test_streaming_notify_callback_checks_the_bridge_first():
    """The Agent denies at once (notify_failed) when the notifier raises."""
    src = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
    start = src.index("def _approval_notify_cb(approval_data):")
    body = src[start:src.index("_reg_notify(session_id, _approval_notify_cb)", start)]
    assert body.index("_require_approval_bridge()") < body.index("_settle_pending_for_polling(")
    assert "require_approval_bridge as _require_approval_bridge" in src


# ── Respond / YOLO routes against a drifted Agent ────────────────────────────


class _FakeHandler:
    def __init__(self):
        self.status = None
        self._body = b""
        self.client_address = ("127.0.0.1", 0)
        self.headers = {}
        outer = self

        class _Writer:
            def write(self, data):
                outer._body += data

        self.wfile = _Writer()

    def send_response(self, code):
        self.status = code

    def send_header(self, *_args):
        pass

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self._body.decode("utf-8"))


def _use_bridge_without(monkeypatch, name):
    """Point the live module at the APPROVAL_BRIDGE WebUI computes for an Agent lacking ``name``."""
    probe = _load_route_approvals(monkeypatch, _fake_agent_module(missing=(name,)))
    monkeypatch.setattr(route_approvals, "APPROVAL_BRIDGE", probe.APPROVAL_BRIDGE)


def _seed_local_card(sid):
    entry = {
        "approval_id": f"aid-{sid}",
        "command": "rm -rf /tmp/issue9119",
        "pattern_key": "recursive delete",
        "pattern_keys": ["recursive delete"],
        "description": "recursive delete",
    }
    with routes._lock:
        routes._pending[sid] = [dict(entry)]
    return entry["approval_id"]


def _respond(sid, approval_id, choice, **extra):
    handler = _FakeHandler()
    with patch("api.gateway_chat.webui_gateway_chat_enabled", return_value=False):
        routes._handle_approval_respond(
            handler, {"session_id": sid, "choice": choice, "approval_id": approval_id, **extra}
        )
    return handler.status, handler.json()


def _pending_ids(sid):
    with routes._lock:
        return [e.get("approval_id") for e in routes._pending.get(sid) or []]


@pytest.mark.parametrize("missing", CRITICAL_NAMES)
def test_respond_fails_closed_when_agent_lacks_a_bridge_name(monkeypatch, missing):
    _use_bridge_without(monkeypatch, missing)
    sid = f"issue9119-critical-{uuid.uuid4().hex[:8]}"
    approval_id = _seed_local_card(sid)
    try:
        status, body = _respond(sid, approval_id, "once")
        assert status == 503
        assert body["ok"] is False
        assert body["code"] == "approval_bridge_unavailable"
        assert missing in body["missing"] and missing in body["error"]
        # Nothing was consumed: the response did not pretend to settle anything.
        assert _pending_ids(sid) == [approval_id]
    finally:
        with routes._lock:
            routes._pending.pop(sid, None)


@pytest.mark.parametrize("missing", SCOPE_NAMES)
def test_scoped_choice_refused_when_agent_lacks_a_grant_name(monkeypatch, missing):
    _use_bridge_without(monkeypatch, missing)
    sid = f"issue9119-scope-{uuid.uuid4().hex[:8]}"
    approval_id = _seed_local_card(sid)
    try:
        for choice in ("session", "always"):
            status, body = _respond(sid, approval_id, choice)
            assert (status, body["ok"], body["code"]) == (409, False, "approval_scope_unavailable")
            assert _pending_ids(sid) == [approval_id]
        # A one-off approval still goes through.
        status, body = _respond(sid, approval_id, "once")
        assert (status, body["ok"]) == (200, True)
        assert _pending_ids(sid) == []
    finally:
        with routes._lock:
            routes._pending.pop(sid, None)


@pytest.mark.parametrize("missing", YOLO_NAMES)
def test_yolo_routes_report_unavailable_when_agent_lacks_a_yolo_name(monkeypatch, missing):
    _use_bridge_without(monkeypatch, missing)
    sid = f"issue9119-yolo-{uuid.uuid4().hex[:8]}"
    responses = []
    post_body = {}

    def fake_j(_handler, data, status=200, extra_headers=None):
        responses.append((status, data))
        return data

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "read_body", lambda _handler: dict(post_body))
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)

    routes.handle_get(object(), urllib.parse.urlparse(f"/api/session/yolo?session_id={sid}"))
    for enabled in (True, False):
        post_body.update(session_id=sid, enabled=enabled)
        routes.handle_post(object(), urllib.parse.urlparse("/api/session/yolo"))
    routes._handle_approval_respond(
        object(), {"session_id": sid, "choice": "once", "approval_id": "a", "yolo": True}
    )

    assert len(responses) == 4, responses
    for status, payload in responses:
        assert (status, payload["ok"], payload["code"]) == (503, False, "yolo_unavailable")
        assert "yolo_enabled" not in payload


def test_health_reports_the_approval_bridge(monkeypatch):
    captured = {}
    monkeypatch.setattr(routes, "j", lambda _h, data, status=200, extra_headers=None: captured.update(data=data))
    monkeypatch.setattr(routes, "build_agent_health_payload", lambda: {})
    monkeypatch.setattr(routes, "gateway_chat_config_status", lambda: {})
    monkeypatch.setattr(routes, "gateway_lifecycle_capability", lambda: {})
    routes.handle_get(object(), urllib.parse.urlparse("/api/health/agent"))
    assert captured["data"]["approval_bridge"] == route_approvals.APPROVAL_BRIDGE


# ── Real installed Agent (skipped when tools.approval is not importable) ──────

_AGENT_GATE_DRIVER = r'''
import json, os, sys, threading, time

repo, agent_dir, mode = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, repo)
sys.path.append(agent_dir)
import tools.approval as A

if mode in ("pr156131", "bridge_unavailable"):
    # NousResearch/hermes-agent#156131 removed these (in memory only).
    for name in ("_pending", "submit_pending"):
        if hasattr(A, name):
            delattr(A, name)

import api.route_approvals as R
import api.routes as ROUTES

bridge = getattr(R, "APPROVAL_BRIDGE", None)  # absent before #9119
if mode == "bridge_unavailable" and bridge is not None:
    # What WebUI computes for an Agent without _gateway_queues. Deleting it from
    # the real module would break the Agent's own gate, so set the result.
    bridge.update(available=False, missing=["_gateway_queues"])

sid = "issue9119-" + mode
os.environ["HERMES_EXEC_ASK"] = "1"
os.environ["HERMES_SESSION_PLATFORM"] = "webui"
os.environ["HERMES_SESSION_KEY"] = sid
from tools.approval_context import set_current_session_key

require_bridge = getattr(R, "require_approval_bridge", lambda: None)  # absent before #9119
cards = []

def notify(approval_data):  # the api/streaming.py _approval_notify_cb shape
    require_bridge()
    auto_resolved, head, total = R.settle_gateway_pending_local_notification(sid, approval_data)
    if auto_resolved and head is None:
        return
    cards.append({**(head or approval_data), "pending_count": total})

A.register_gateway_notify(sid, notify)
gate = {}

def agent_turn():
    set_current_session_key(sid)
    started = time.monotonic()
    gate["result"] = A.check_all_command_guards("rm -rf /tmp/issue9119-nonexistent-target", "local")
    gate["seconds"] = round(time.monotonic() - started, 2)

thread = threading.Thread(target=agent_turn, daemon=True)
thread.start()
deadline = time.monotonic() + 15
while time.monotonic() < deadline and thread.is_alive() and not (cards and A.has_blocking_approval(sid)):
    time.sleep(0.02)

response = {}

def fake_j(_handler, data, status=200, extra_headers=None):
    response.update(status=status, payload=data)
    return data

ROUTES.j = fake_j
approval_id = cards[0].get("approval_id") if cards else "no-card"
ROUTES._handle_approval_respond(object(), {"session_id": sid, "choice": "once", "approval_id": approval_id})
thread.join(timeout=4)
out = {
    "bridge": bridge,
    "lock_is_agent": R._lock is A._lock,
    "queues_is_agent": R._gateway_queues is A._gateway_queues,
    "card": bool(cards),
    "respond": response,
    "unblocked_after_click": not thread.is_alive(),
}
R.set_session_yolo_enabled(sid, True)
out["agent_yolo_after_webui_enable"] = A.is_session_yolo_enabled(sid)
R.set_session_yolo_enabled(sid, False)
thread.join(timeout=15)
out["gate"] = {
    "approved": (gate.get("result") or {}).get("approved"),
    "outcome": (gate.get("result") or {}).get("outcome"),
    "seconds": gate.get("seconds"),
}
A.unregister_gateway_notify(sid)
print("RESULT " + json.dumps(out, default=str))
'''

_AGENT_APPROVAL_TIMEOUT_S = 6


def _run_agent_gate(tmp_path, mode):
    agent_approval = pytest.importorskip("tools.approval")
    pytest.importorskip("tools.approval_context")
    agent_dir = Path(agent_approval.__file__).resolve().parents[1]
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / "config.yaml").write_text(
        f"approvals:\n  mode: manual\n  timeout: {_AGENT_APPROVAL_TIMEOUT_S}\n", encoding="utf-8"
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "HERMES_HOME": str(home / ".hermes"),
        "HERMES_WEBUI_STATE_DIR": str(tmp_path / "state"),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(tmp_path),
    }
    proc = subprocess.run(
        [sys.executable, "-B", "-c", _AGENT_GATE_DRIVER, str(REPO_ROOT), str(agent_dir), mode],
        env=env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=180,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, f"driver failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}"
    return json.loads(lines[-1][len("RESULT "):])


@pytest.mark.parametrize("mode", ["installed", "pr156131"])
def test_real_agent_gate_unblocks_on_webui_approve_once(tmp_path, mode):
    """WebUI's approve-once reaches the real Agent, with or without #156131's names."""
    out = _run_agent_gate(tmp_path, mode)
    detail = json.dumps(out, default=str)
    assert out["card"], detail
    assert out["respond"]["status"] == 200 and out["respond"]["payload"]["ok"] is True, detail
    assert out["unblocked_after_click"], detail
    assert out["gate"]["approved"] is True, detail
    assert out["lock_is_agent"] and out["queues_is_agent"], detail
    assert out["agent_yolo_after_webui_enable"] is True, detail


def test_real_agent_denies_at_once_when_bridge_unavailable(tmp_path):
    """No card WebUI cannot settle: the Agent denies now instead of at its timeout."""
    out = _run_agent_gate(tmp_path, "bridge_unavailable")
    detail = json.dumps(out, default=str)
    assert out["card"] is False, detail
    assert out["gate"]["approved"] is False, detail
    assert out["gate"]["outcome"] == "notify_failed", detail
    assert out["gate"]["seconds"] < _AGENT_APPROVAL_TIMEOUT_S / 2, detail
    assert out["respond"]["status"] == 503, detail
    assert out["respond"]["payload"]["code"] == "approval_bridge_unavailable", detail
    assert out["bridge"]["available"] is False, detail
