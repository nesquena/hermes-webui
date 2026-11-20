"""#9157: the settle projection's installed-repair gate probes the codec the repair actually reads.

Newer Agents define ``repair_message_sequence`` in ``session_rules.turns`` (re-exported by
``agent.agent_runtime_helpers``) with that module's own content codec, and their ``hermes_state.SessionDB`` is a
stand-in without ``_CONTENT_JSON_PREFIX`` / ``_decode_content``. Probing only ``SessionDB`` closed the gate there,
``_agent_replay_settle_projection`` fell back to the pass-0/3 mirrors, the strict-prefix proof refused, and the
settle dropped the current prompt and answer from the saved model context.

The stand-in tests pin the gate decision for both Agent shapes without an Agent; the real-Agent test asserts the
gate opens and the projection proves the current exchange on whichever Agent is installed.
"""

from __future__ import annotations

import copy
import sys
import types

import pytest

from tests.conftest import requires_agent

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_PREFIX = "\x00json:"


def _identity_decode(content):
    return content


def _install_agent_shape(monkeypatch, *, repair_module, turns_codec, session_db):
    """Replace the Agent modules the gate imports with small stand-ins for one test."""
    repair = lambda agent, messages: 0  # noqa: E731
    repair.__module__ = repair_module
    helpers = types.ModuleType("agent.agent_runtime_helpers")
    helpers.repair_message_sequence = repair
    agent_pkg = types.ModuleType("agent")
    agent_pkg.agent_runtime_helpers = helpers
    turns = types.ModuleType("session_rules.turns")
    if turns_codec is not None:
        turns._CONTENT_JSON_PREFIX, turns.decode_content = turns_codec
    rules_pkg = types.ModuleType("session_rules")
    rules_pkg.turns = turns
    state = types.ModuleType("hermes_state")
    state.SessionDB = session_db
    for name, module in (
        ("agent", agent_pkg),
        ("agent.agent_runtime_helpers", helpers),
        ("session_rules", rules_pkg),
        ("session_rules.turns", turns),
        ("hermes_state", state),
    ):
        monkeypatch.setitem(sys.modules, name, module)


class _LegacySessionDB:
    _CONTENT_JSON_PREFIX = _PREFIX
    _decode_content = staticmethod(_identity_decode)


class _StandInSessionDB:
    """Like the newer ``hermes_state`` stand-in: no codec class attributes."""


@pytest.mark.parametrize(
    "repair_module, turns_codec, session_db, expected",
    [
        # Newer Agent: the repair reads session_rules.turns; SessionDB has no codec.
        ("session_rules.turns", (_PREFIX, _identity_decode), _StandInSessionDB, True),
        # Newer Agent with a faked turns codec: closed even though SessionDB carries a real-looking codec.
        ("session_rules.turns", (None, _identity_decode), _LegacySessionDB, False),
        ("session_rules.turns", (_PREFIX, lambda content: object()), _LegacySessionDB, False),
        ("session_rules.turns", None, _LegacySessionDB, False),
        # Older Agent: the repair reads hermes_state.SessionDB.
        ("agent.agent_runtime_helpers", None, _LegacySessionDB, True),
        # Older Agent whose SessionDB is faked or lacks the codec: closed even though a turns codec exists.
        ("agent.agent_runtime_helpers", (_PREFIX, _identity_decode), _StandInSessionDB, False),
        ("agent.agent_runtime_helpers", (_PREFIX, _identity_decode), lambda *a, **k: None, False),
    ],
    ids=[
        "turns-codec-ok",
        "turns-prefix-faked",
        "turns-decode-faked",
        "turns-codec-missing",
        "sessiondb-codec-ok",
        "sessiondb-standin",
        "sessiondb-lambda",
    ],
)
def test_gate_probes_the_codec_the_repair_reads(monkeypatch, repair_module, turns_codec, session_db, expected):
    from api.streaming import _installed_repair_runtime_usable

    _install_agent_shape(
        monkeypatch, repair_module=repair_module, turns_codec=turns_codec, session_db=session_db
    )
    assert _installed_repair_runtime_usable() is expected


def test_gate_closes_on_a_mock_sessiondb_for_the_older_shape(monkeypatch):
    from unittest.mock import Mock

    from api.streaming import _installed_repair_runtime_usable

    _install_agent_shape(
        monkeypatch, repair_module="agent.agent_runtime_helpers", turns_codec=None, session_db=Mock()
    )
    assert _installed_repair_runtime_usable() is False


def test_gate_probes_the_alias_the_turns_repair_calls(monkeypatch):
    """The turns repair calls ``_decode_content``; a faked alias closes the gate even beside a real public name."""
    from api.streaming import _installed_repair_runtime_usable

    _install_agent_shape(
        monkeypatch, repair_module="session_rules.turns", turns_codec=(_PREFIX, _identity_decode),
        session_db=_StandInSessionDB,
    )
    sys.modules["session_rules.turns"]._decode_content = lambda content: object()
    assert _installed_repair_runtime_usable() is False
    sys.modules["session_rules.turns"]._decode_content = _identity_decode
    assert _installed_repair_runtime_usable() is True


@requires_agent
def test_installed_agent_gate_probes_the_codec_its_repair_reads():
    """Real Agent, whichever is installed: the probed codec is the one its repair reads."""
    from agent import agent_runtime_helpers

    from api.streaming import _installed_repair_codec

    if agent_runtime_helpers.repair_message_sequence.__module__ == "session_rules.turns":
        import session_rules.turns as turns

        expected = (turns._CONTENT_JSON_PREFIX, turns.decode_content)
    else:
        from hermes_state import SessionDB

        expected = (SessionDB._CONTENT_JSON_PREFIX, SessionDB._decode_content)
    assert _installed_repair_codec() == expected


@requires_agent
def test_installed_agent_gate_opens_and_settle_proves_the_current_exchange():
    """Real Agent, whichever is installed: the gate opens, and the settle projection of a duplicate-tool history
    proves the current prompt and answer as the suffix to save in the model context."""
    from agent import agent_runtime_helpers

    from api.streaming import (
        _agent_replay_settle_projection,
        _installed_repair_runtime_usable,
        _proven_current_turn_suffix,
        _sanitize_messages_for_agent,
    )

    repair = agent_runtime_helpers.repair_message_sequence
    assert _installed_repair_runtime_usable() is True

    call = {"id": "call-x", "type": "function", "function": {"name": "tool_f", "arguments": "{}"}}
    history = [
        {"role": "user", "content": "OLD_Q", "timestamp": 1.0},
        {"role": "assistant", "content": "", "tool_calls": [copy.deepcopy(call)], "timestamp": 2.0},
        {"role": "tool", "content": "RESULT_ONE", "tool_call_id": "call-x", "timestamp": 3.0},
        {"role": "tool", "content": "RESULT_TWO", "tool_call_id": "call-x", "timestamp": 4.0},
        {"role": "assistant", "content": "OLD_ANSWER", "timestamp": 5.0},
    ]
    sent = _sanitize_messages_for_agent(copy.deepcopy(history), normalize_bare_assistant_adjacency=True)
    returned = copy.deepcopy(sent)
    returned.append({"role": "user", "content": "CURRENT"})
    repair(None, returned)
    returned.append({"role": "assistant", "content": "CURRENT_ANSWER"})

    projected = _agent_replay_settle_projection(copy.deepcopy(sent))
    proven = _proven_current_turn_suffix(projected, returned)
    assert proven is not None, f"projection={projected} returned={returned}"
    assert [row.get("content") for row in proven] == ["CURRENT", "CURRENT_ANSWER"]
