"""Current-Agent fallback notice (2026-11 producer drift) — #6267 review 2.

The installed Agent now announces a successful fallback with status kind
``fallback`` (``agent/fallback_notice.py:138`` ``FALLBACK_STATUS_KIND``) and the
text ``⚠️ Model fallback: <old> via <old_provider> could not respond
(<reason>); using <new> via <new_provider>.`` (``agent/chat_completion_helpers.py``).
The October shape used kind ``lifecycle`` and the word ``unavailable``.

The WebUI producers must accept BOTH kinds and BOTH text forms, keeping the
legacy ``🔄 Switched to fallback model: …`` phrase, so the typed
``provider_fallback`` event and the existing ``warning`` row both fire.

The Agent also rebinds ``agent.model`` / ``agent.provider`` in
``bind_route_entry`` BEFORE announcing, while ``_provider_fallback_route`` is
assigned only AFTER the callback returns.  A destination read from that route
attribute is therefore stale on the second hop of a chain, so these fixtures
never preload it for the current notice and one case proves the rebound
attributes win over a stale route.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from api import streaming

REPO = Path(__file__).resolve().parent.parent

# Exactly the row the installed Agent emits (chat_completion_helpers.py).
CURRENT_NOTICE = (
    "⚠️ Model fallback: gpt-4 via openai could not respond (rate limit); "
    "using opus via anthropic."
)
# The October shape quoted by the earlier review; a passing control.
OCTOBER_NOTICE = (
    "⚠️ Model fallback: gpt-4 via openai unavailable (rate limit exceeded); "
    "using opus via anthropic."
)
LEGACY_NOTICE = "🔄 Switched to fallback model: gpt-4 via openai → opus via anthropic"


class _Agent:
    """Agent double: the fields the producers read, nothing preloaded."""

    def __init__(self, provider="", model="", buffer=None):
        self.provider = provider
        self.model = model
        self._retry_status_buffer = list(buffer or [])


def _state(provider="openai", model="gpt-4"):
    return {
        "seq": 0,
        "route_provider": provider,
        "route_model": model,
        "agent_fired": False,
    }


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PROVIDER_FALLBACK_SSE", "1")


# ── 1. both status kinds, both text forms ────────────────────────────────


@pytest.mark.parametrize("kind", ["lifecycle", "fallback", "FALLBACK"])
@pytest.mark.parametrize("notice", [CURRENT_NOTICE, OCTOBER_NOTICE, LEGACY_NOTICE])
def test_switch_notice_accepts_both_kinds_and_both_text_forms(kind, notice):
    assert streaming._is_fallback_switch_notice(kind, notice) is True


@pytest.mark.parametrize(
    "kind,notice",
    [
        ("warn", CURRENT_NOTICE),
        ("user", CURRENT_NOTICE),
        ("", CURRENT_NOTICE),
        ("fallback", "Rate limited, trying fallback…"),
        ("fallback", ""),
        ("lifecycle", "⚠️ Model fallback: gpt-4 via openai could not respond"),
    ],
)
def test_switch_notice_still_rejects_unrelated_rows(kind, notice):
    assert streaming._is_fallback_switch_notice(kind, notice) is False


def test_current_notice_surfaces_the_existing_fallback_warning():
    # The generic warning listener is the pre-existing path; the new kind must
    # reach it too, otherwise the user sees nothing until the turn settles.
    assert streaming._is_fallback_lifecycle_message("fallback", CURRENT_NOTICE) is True
    # October + legacy rows keep working on the lifecycle kind.
    assert streaming._is_fallback_lifecycle_message("lifecycle", OCTOBER_NOTICE) is True
    assert streaming._is_fallback_lifecycle_message("lifecycle", LEGACY_NOTICE) is True
    # Classification still keys on the kind: user text is never promoted.
    assert streaming._is_fallback_lifecycle_message("user", CURRENT_NOTICE) is False


# ── 2. producer destination under the Agent's callback order ─────────────


def test_producer_emits_for_current_notice_without_a_preloaded_route():
    agent = _Agent("anthropic", "opus")  # rebound by bind_route_entry already
    payload = streaming._maybe_emit_agent_fallback_event(
        "fallback", CURRENT_NOTICE, agent, "sess-1", _state(), "stream-1",
    )
    assert payload is not None
    assert payload["source"] == "agent"
    assert (payload["from_provider"], payload["from_model"]) == ("openai", "gpt-4")
    assert (payload["to_provider"], payload["to_model"]) == ("anthropic", "opus")
    assert payload["reason"] == "rate limit"


def test_stale_route_attribute_does_not_mask_the_rebound_destination():
    # Second hop of a chain: the route attribute still carries hop 1 (it is
    # assigned after the callback), while the live attributes are hop 2.
    agent = _Agent("deepseek", "deepseek-v3")
    agent._provider_fallback_route = ("opus", "anthropic")
    payload = streaming._maybe_emit_agent_fallback_event(
        "fallback", CURRENT_NOTICE, agent, "sess-1", _state(), "stream-1",
    )
    assert payload is not None
    assert (payload["to_provider"], payload["to_model"]) == ("deepseek", "deepseek-v3")


def test_route_attribute_still_used_when_live_attributes_are_empty():
    agent = _Agent("", "")
    agent._provider_fallback_route = ("opus", "anthropic")
    payload = streaming._maybe_emit_agent_fallback_event(
        "lifecycle", OCTOBER_NOTICE, agent, "sess-1", _state(), "stream-1",
    )
    assert payload is not None
    assert (payload["to_provider"], payload["to_model"]) == ("anthropic", "opus")


# ── 3. the real relay callback, driven with the Agent's emitted row ──────


def _relay_callback(agent, state, session_id="sid-1", stream_id="stream-1"):
    """Extract ``_agent_status_callback`` and bind every free name explicitly."""
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_agent_status_callback"
    )
    nonlocals = sorted({
        name
        for node in ast.walk(fn)
        if isinstance(node, ast.Nonlocal)
        for name in node.names
    })
    binds = "".join(f"    {name} = None\n" for name in nonlocals)
    outer = ast.parse(f"def _scope():\n{binds}    pass\n").body[0]
    outer.body[-1:] = [fn, ast.Return(value=ast.Name(id=fn.name, ctx=ast.Load()))]
    module = ast.fix_missing_locations(ast.Module(body=[outer], type_ignores=[]))
    events = []
    ns = {
        "put": lambda event, data: events.append((event, data)),
        "session_id": session_id,
        "stream_id": stream_id,
        "agent": agent,
        "_fallback_state": state,
        "_captured_terminal_error": [None],
        "_is_agent_compression_start_status": streaming._is_agent_compression_start_status,
        "_is_fallback_lifecycle_message": streaming._is_fallback_lifecycle_message,
        "_is_fallback_switch_notice": streaming._is_fallback_switch_notice,
        "_is_session_lease_wait_message": streaming._is_session_lease_wait_message,
        "_maybe_emit_agent_fallback_event": streaming._maybe_emit_agent_fallback_event,
    }
    exec(compile(module, "streaming_status_callback", "exec"), ns)
    return ns["_scope"](), events


def test_relay_emits_warning_and_typed_event_for_the_current_notice():
    agent = _Agent("anthropic", "opus")
    state = _state()
    callback, events = _relay_callback(agent, state)
    callback("fallback", CURRENT_NOTICE)

    warnings = [data for event, data in events if event == "warning"]
    assert warnings == [{"type": "fallback", "message": CURRENT_NOTICE}]

    typed = [data for event, data in events if event == "provider_fallback"]
    assert len(typed) == 1
    assert (typed[0]["to_provider"], typed[0]["to_model"]) == ("anthropic", "opus")
    assert state["agent_fired"] is True
