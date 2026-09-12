"""Regression tests for gateway workspace containment and Runs API relay (#7440).

Covers ``api.gateway_chat._gateway_workspace_for_relay`` (realpath containment
under the relay root) and proves the validated per-session workspace actually
reaches the real ``POST /v1/runs`` request body built by
``_run_gateway_runs_api_streaming``.

The relay root is monkeypatched to ``tmp_path / "workspace"`` so macOS runs
never create or depend on a real ``/workspace``. Real ``os.path.realpath`` is
kept for the whole containment matrix — including the symlink rows, which
must run on macOS/Linux (only an explicit Windows privilege skip is allowed).
All HTTP is faked at ``urllib.request.urlopen``; no real gateway calls,
sockets, or session persistence are used.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from types import SimpleNamespace

import pytest


_SKIP_WINDOWS_SYMLINK = pytest.mark.skipif(
    os.name == "nt",
    reason="directory symlink creation requires elevated privileges on Windows",
)


# ---------------------------------------------------------------------------
# Fixtures and HTTP doubles
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _gateway_run_state_isolation():
    """Snapshot/restore gateway run-id publication state around each test."""
    from api import gateway_chat

    with gateway_chat._STREAM_RUN_STARTING_CONDITION:
        prior_ids = dict(gateway_chat._STREAM_RUN_IDS)
        prior_lifecycle = {
            key: dict(value) for key, value in gateway_chat._STREAM_RUN_LIFECYCLE.items()
        }
    try:
        yield
    finally:
        with gateway_chat._STREAM_RUN_STARTING_CONDITION:
            gateway_chat._STREAM_RUN_IDS.clear()
            gateway_chat._STREAM_RUN_IDS.update(prior_ids)
            gateway_chat._STREAM_RUN_LIFECYCLE.clear()
            gateway_chat._STREAM_RUN_LIFECYCLE.update(prior_lifecycle)
            gateway_chat._STREAM_RUN_STARTING_CONDITION.notify_all()


@pytest.fixture()
def relay_root(tmp_path, monkeypatch):
    """Point the relay root at a temporary directory (never real /workspace)."""
    from api import gateway_chat

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(gateway_chat, "_WORKSPACE_RELAY_ROOT", str(root))
    return root


class _JsonResponse:
    """Context-managed JSON response for POST /v1/runs."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self, _limit=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class _SseResponse:
    """Context-managed empty SSE stream for GET /v1/runs/{id}/events."""

    def __init__(self, lines=()):
        self._lines = list(lines)

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _capture_runs_requests(monkeypatch, run_id: str):
    """Fake urlopen: POST /v1/runs returns ``run_id``; events stream is empty.

    Returns the list of every ``urllib.request.Request`` issued.
    """
    requests: list = []

    def fake_urlopen(req, *, timeout=None):
        requests.append(req)
        if req.get_method() == "POST" and req.full_url.endswith("/v1/runs"):
            return _JsonResponse({"run_id": run_id})
        if req.get_method() == "GET" and req.full_url.endswith("/events"):
            return _SseResponse()
        raise AssertionError(f"unexpected gateway request: {req.get_method()} {req.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return requests


def _reset_run_state(stream_id: str) -> None:
    """Drop any published run-id/lifecycle state for a test stream."""
    from api import gateway_chat

    gateway_chat._STREAM_RUN_IDS.pop(stream_id, None)
    gateway_chat._STREAM_RUN_LIFECYCLE.pop(stream_id, None)


def _run_streaming(session_id: str, stream_id: str, workspace):
    """Invoke the real runs-API bridge with a canned session."""
    from api import gateway_chat

    return gateway_chat._run_gateway_runs_api_streaming(
        session_id=session_id,
        msg_text=f"hello from {session_id}",
        model="test-model",
        workspace=workspace,
        stream_id=stream_id,
        base_url="http://gateway.test",
        api_key="test-key",
        prefill_messages=[],
        body_extras={},
        put_gateway_event=lambda *args, **kwargs: None,
        cancel_event=threading.Event(),
        session=SimpleNamespace(context_messages=[]),
    )


def _post_bodies(requests):
    posts = [
        req for req in requests
        if req.get_method() == "POST" and req.full_url.endswith("/v1/runs")
    ]
    return [json.loads(req.data.decode("utf-8")) for req in posts]


# ---------------------------------------------------------------------------
# Production default and pure-string default-root rejections
# ---------------------------------------------------------------------------


def test_gateway_workspace_default_root():
    from api import gateway_chat

    assert gateway_chat._WORKSPACE_RELAY_ROOT == "/workspace"
    # Pure-string default-root rejections: these paths are never created.
    assert gateway_chat._gateway_workspace_for_relay("/workspace-other/project") is None
    assert gateway_chat._gateway_workspace_for_relay("/workspace/../etc") is None


# ---------------------------------------------------------------------------
# Containment matrix (real realpath, temporary root)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [
        "project",
        "exact_root",
        "exact_root_trailing_slash",
        "nested_dotdot_inside",
        "sibling_prefix",
        "dotdot_escape",
        "none_value",
        "empty_string",
        "whitespace",
        "outside_path",
    ],
)
def test_gateway_workspace_containment(relay_root, tmp_path, case):
    from api import gateway_chat

    root = relay_root
    project = root / "project"
    project.mkdir()

    if case == "project":
        workspace = str(project)
        expected = os.path.realpath(project)
    elif case == "exact_root":
        workspace = str(root)
        expected = os.path.realpath(root)
    elif case == "exact_root_trailing_slash":
        workspace = str(root) + os.sep
        expected = os.path.realpath(root)
    elif case == "nested_dotdot_inside":
        (root / "nested").mkdir()
        workspace = str(root / "nested" / ".." / "project")
        expected = os.path.realpath(project)
    elif case == "sibling_prefix":
        sibling = tmp_path / (root.name + "-other") / "project"
        sibling.mkdir(parents=True)
        workspace = str(sibling)
        expected = None
    elif case == "dotdot_escape":
        (tmp_path / "outside").mkdir()
        workspace = str(root) + os.sep + ".." + os.sep + "outside"
        expected = None
    elif case == "none_value":
        workspace = None
        expected = None
    elif case == "empty_string":
        workspace = ""
        expected = None
    elif case == "whitespace":
        workspace = "   "
        expected = None
    elif case == "outside_path":
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        workspace = str(outside)
        expected = None
    else:  # pragma: no cover - parametrize guard
        raise AssertionError(f"unknown case {case}")

    assert gateway_chat._gateway_workspace_for_relay(workspace) == expected


@_SKIP_WINDOWS_SYMLINK
def test_gateway_workspace_symlink_escape_rejected(relay_root, tmp_path):
    """A symlink inside the root pointing outside must resolve AWAY and be
    rejected — real realpath only, never mocked, for the security proof."""
    from api import gateway_chat

    outside = tmp_path / "symlink-target"
    (outside / "child").mkdir(parents=True)
    link = relay_root / "link"
    link.symlink_to(outside, target_is_directory=True)

    assert gateway_chat._gateway_workspace_for_relay(str(link)) is None
    assert gateway_chat._gateway_workspace_for_relay(str(link / "child")) is None


@_SKIP_WINDOWS_SYMLINK
def test_gateway_workspace_symlink_inside_root_accepted(relay_root):
    """A symlink that resolves back INSIDE the root yields the canonical
    target path."""
    from api import gateway_chat

    project = relay_root / "project"
    project.mkdir()
    link = relay_root / "link-inside"
    link.symlink_to(project, target_is_directory=True)

    assert gateway_chat._gateway_workspace_for_relay(str(link)) == os.path.realpath(project)


def test_gateway_workspace_resolution_error_fails_closed(relay_root, monkeypatch):
    """If path resolution itself fails, the helper returns None (fail closed)
    rather than relaying an unvalidated path."""
    from api import gateway_chat

    def boom(_path):
        raise OSError("resolution failed")

    monkeypatch.setattr(os.path, "realpath", boom)
    assert gateway_chat._gateway_workspace_for_relay(str(relay_root / "project")) is None


# ---------------------------------------------------------------------------
# Workspace propagation into the actual POST /v1/runs body
# ---------------------------------------------------------------------------


def test_runs_api_body_uses_validated_session_workspace(relay_root, monkeypatch):
    """Two sessions with different contained workspaces: each captured POST
    /v1/runs body carries THAT session's canonical workspace — not the other
    session's, not a global default, not the unnormalized input string."""
    requests = _capture_runs_requests(monkeypatch, run_id="run-ws-body")
    ws_one = relay_root / "alpha"
    (ws_one / "nested").mkdir(parents=True)
    ws_two = relay_root / "beta"
    ws_two.mkdir()

    cases = [
        # (session_id, stream_id, raw workspace input, canonical expectation)
        ("sess-ws-one", "stream-ws-body-one", str(ws_one / "nested" / ".."), os.path.realpath(ws_one)),
        ("sess-ws-two", "stream-ws-body-two", str(ws_two), os.path.realpath(ws_two)),
    ]
    try:
        for session_id, stream_id, raw_workspace, _canonical in cases:
            final_text, _usage = _run_streaming(session_id, stream_id, raw_workspace)
            assert final_text == ""
    finally:
        for _session_id, stream_id, _raw, _canonical in cases:
            _reset_run_state(stream_id)

    bodies = _post_bodies(requests)
    assert len(bodies) == 2
    assert bodies[0]["session_id"] == "sess-ws-one"
    assert bodies[0]["workspace"] == cases[0][3]
    assert bodies[1]["session_id"] == "sess-ws-two"
    assert bodies[1]["workspace"] == cases[1][3]
    # Canonical, distinct per-session values — not the raw "nested/.." input.
    assert bodies[0]["workspace"] != bodies[1]["workspace"]
    assert ".." not in bodies[0]["workspace"]
    for body in bodies:
        assert body["model"] == "test-model"
        assert body["input"] == f"hello from {body['session_id']}"


@pytest.mark.parametrize(
    "case",
    [
        "none",
        "sibling_prefix",
        "dotdot_escape",
        pytest.param("symlink_escape", marks=_SKIP_WINDOWS_SYMLINK),
    ],
)
def test_runs_api_body_omits_rejected_workspace(relay_root, tmp_path, monkeypatch, case):
    """Rejected workspaces (None, sibling prefix, traversal, symlink escape)
    must leave the ``workspace`` field OUT of the actual POST body while the
    rest of the request stays intact."""
    if case == "none":
        workspace = None
    elif case == "sibling_prefix":
        workspace = str(relay_root) + "-other/project"
    elif case == "dotdot_escape":
        workspace = str(relay_root) + os.sep + ".." + os.sep + "outside"
    elif case == "symlink_escape":
        outside = tmp_path / "escape-target"
        outside.mkdir()
        link = relay_root / "escape-link"
        link.symlink_to(outside, target_is_directory=True)
        workspace = str(link)
    else:  # pragma: no cover - parametrize guard
        raise AssertionError(f"unknown case {case}")

    session_id = f"sess-ws-omit-{case}"
    stream_id = f"stream-ws-omit-{case}"
    requests = _capture_runs_requests(monkeypatch, run_id=f"run-ws-omit-{case}")
    try:
        _run_streaming(session_id, stream_id, workspace)
    finally:
        _reset_run_state(stream_id)

    bodies = _post_bodies(requests)
    assert len(bodies) == 1
    body = bodies[0]
    assert "workspace" not in body
    assert body["session_id"] == session_id
    assert body["model"] == "test-model"
    assert body["input"] == f"hello from {session_id}"
