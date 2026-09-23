"""Agent session turn-lease waits surface as WebUI ``warning`` events.

When another Hermes process (gateway, CLI, cron) holds a session's turn lease,
the Agent's ``agent/turn_facade_lease.py`` emits:

* ``_emit_status`` (kind ``lifecycle``):
  "⏳ Another Hermes process is using this session; waiting for it to finish
  before starting your turn..." then "⏳ Still waiting for the other Hermes
  process on this session (Ns)..." and, on admission, "Session is free; loading
  the latest transcript...";
* ``_emit_warning`` (kind ``warn``) on timeout: "⏳ Another Hermes process kept
  this session busy too long. Your message was not processed - ...".

``_agent_status_callback`` used to drop every one of these, so a WebUI turn
looked stuck with no explanation. They are now relayed as
``{'type': 'session_lease_wait'}`` warnings shown by the existing
messages.js ``warning`` listener.
"""
import ast
import pathlib

import pytest

from api import streaming

REPO = pathlib.Path(__file__).resolve().parents[1]

AGENT_WAIT = (
    "⏳ Another Hermes process is using this session; "
    "waiting for it to finish before starting your turn..."
)
AGENT_STILL = "⏳ Still waiting for the other Hermes process on this session (30s)..."
AGENT_FREE = "Session is free; loading the latest transcript..."
AGENT_TIMEOUT = (
    "⏳ Another Hermes process kept this session busy too long. Your message was not "
    "processed - wait for the other process to finish, then send it again."
)


@pytest.mark.parametrize(
    "kind,text,expected",
    [
        ("lifecycle", AGENT_WAIT, True),
        ("lifecycle", AGENT_STILL, True),
        ("lifecycle", AGENT_FREE, True),
        ("warn", AGENT_TIMEOUT, True),
        ("LIFECYCLE", AGENT_WAIT, True),
        # Never classify by text alone: other kinds / user text stay dropped.
        ("user", AGENT_WAIT, False),
        ("", AGENT_WAIT, False),
        ("lifecycle", "Compressing conversation history", False),
        ("lifecycle", "Rate limited — switching to fallback", False),
        ("lifecycle", "", False),
    ],
)
def test_session_lease_wait_classification(kind, text, expected):
    assert streaming._is_session_lease_wait_message(kind, text) is expected


def _build_status_callback():
    """Extract ``_agent_status_callback`` from ``_run_agent_streaming`` and bind
    it to a recording ``put`` so the real relay logic is exercised."""
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_agent_status_callback"
    )
    module = ast.Module(body=[fn], type_ignores=[])
    events = []
    ns = {
        "put": lambda event, data: events.append((event, data)),
        "session_id": "sid-1",
        "_captured_terminal_error": [None],
        "_is_agent_compression_start_status": streaming._is_agent_compression_start_status,
        "_is_fallback_lifecycle_message": streaming._is_fallback_lifecycle_message,
        "_is_session_lease_wait_message": streaming._is_session_lease_wait_message,
    }
    exec(compile(module, "streaming_status_callback", "exec"), ns)
    return ns["_agent_status_callback"], events


@pytest.mark.parametrize(
    "kind,text",
    [
        ("lifecycle", AGENT_WAIT),
        ("lifecycle", AGENT_STILL),
        ("lifecycle", AGENT_FREE),
        ("warn", AGENT_TIMEOUT),
    ],
)
def test_status_callback_relays_lease_wait_as_warning(kind, text):
    callback, events = _build_status_callback()
    callback(kind, text)
    assert events == [("warning", {"type": "session_lease_wait", "message": text})]


def test_status_callback_still_drops_unrelated_lifecycle_chatter():
    callback, events = _build_status_callback()
    callback("lifecycle", "Loaded 12 tools")
    callback("user", AGENT_WAIT)
    assert events == []


def test_status_callback_fallback_warning_unchanged():
    callback, events = _build_status_callback()
    callback("lifecycle", "Rate limited — switching to fallback model")
    assert events == [
        ("warning", {"type": "fallback", "message": "Rate limited — switching to fallback model"})
    ]
