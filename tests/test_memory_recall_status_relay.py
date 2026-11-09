"""The Agent's memory recall indicator reaches the WebUI as a ``memory_recall`` event.

Right after an external memory provider injects context for a turn, hermes-agent
(``agent/turn_context.py``) emits ``MemoryManager.describe_recall()`` through
``_emit_status`` with kind ``lifecycle``, e.g. "🧠 Honcho — recalled 3 memories".
The CLI prints it and the messaging gateway sends it, but ``_agent_status_callback``
dropped it, so in the WebUI there was no sign that memory was used at all.

The line carries no marker of its own, so the relay does not match its wording: it
compares the line with what the agent's memory manager reports for the same turn.
"""
import pathlib
import re
from types import SimpleNamespace

import pytest

from api import streaming
from tests.test_session_lease_wait_warnings import _build_status_callback

REPO = pathlib.Path(__file__).resolve().parents[1]
RECALL = "🧠 Honcho — recalled 3 memories"


def _agent(describe):
    return SimpleNamespace(_memory_manager=SimpleNamespace(describe_recall=describe))


def _raises():
    raise RuntimeError("provider failed")


@pytest.mark.parametrize(
    "kind,text,agent,expected",
    [
        ("lifecycle", RECALL, _agent(lambda: RECALL), True),
        ("LIFECYCLE", f"  {RECALL}  ", _agent(lambda: RECALL), True),
        # A provider label worded differently is still the manager's own value.
        ("lifecycle", "🧠 LightRAG (a.md, b.md) · 0.2s — recalled 2 memories",
         _agent(lambda: "🧠 LightRAG (a.md, b.md) · 0.2s — recalled 2 memories"), True),
        # Recall-shaped text the manager did not report this turn is not promoted.
        ("lifecycle", RECALL, _agent(lambda: ""), False),
        ("lifecycle", RECALL, _agent(lambda: "🧠 Honcho — recalled 1 memory"), False),
        ("user", RECALL, _agent(lambda: RECALL), False),
        ("warn", RECALL, _agent(lambda: RECALL), False),
        ("lifecycle", "", _agent(lambda: ""), False),
        # No agent yet, no memory manager, or an older Agent without describe_recall.
        ("lifecycle", RECALL, None, False),
        ("lifecycle", RECALL, SimpleNamespace(_memory_manager=None), False),
        ("lifecycle", RECALL, SimpleNamespace(_memory_manager=SimpleNamespace()), False),
        # A provider failing inside recall_status() must not break the stream.
        ("lifecycle", RECALL, _agent(_raises), False),
    ],
)
def test_memory_recall_classification(kind, text, agent, expected):
    assert streaming._is_memory_recall_status(kind, text, agent) is expected


def test_status_callback_relays_memory_recall():
    callback, events = _build_status_callback(agent=_agent(lambda: RECALL))
    callback("lifecycle", RECALL)
    assert events == [("memory_recall", {"session_id": "sid-1", "message": RECALL})]


def test_status_callback_drops_recall_shaped_text_without_a_recall():
    callback, events = _build_status_callback(agent=_agent(lambda: ""))
    callback("lifecycle", RECALL)
    callback("lifecycle", "Loaded 12 tools")
    assert events == []


def test_status_callback_other_relays_unchanged_with_memory_active():
    callback, events = _build_status_callback(agent=_agent(lambda: RECALL))
    callback("lifecycle", "Rate limited — switching to fallback model")
    assert events == [
        ("warning", {"type": "fallback", "message": "Rate limited — switching to fallback model"})
    ]


def test_messages_js_shows_memory_recall_for_the_active_session_only():
    src = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
    start = src.find("source.addEventListener('memory_recall'")
    assert start != -1, "memory_recall listener not found"
    block = src[start:src.find("source.addEventListener('warning'", start)]
    assert "S.session.session_id!==activeSid" in block
    assert "d.session_id!==activeSid" in block
    assert "setComposerStatus(String(d.message),6000)" in block


def test_memory_recall_is_tracked_by_the_run_journal_cursor():
    # The event is journaled like every other relayed event; an untracked one would
    # leave the replay cursor one row behind after a reconnect.
    src = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
    cursor = re.search(r"for\(const _runJournalEventName of \[([^\]]*)\]", src)
    assert cursor and "'memory_recall'" in cursor.group(1)


def test_composer_status_keeps_the_full_text_on_hover():
    # The composer status is ellipsized (max-width 170px, 96px narrow), which cuts a
    # recall line such as 🧠 LightRAG · 0.2s — recalled 2 memories mid-way.
    src = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    start = src.find("function setComposerStatus(")
    assert start != -1
    block = src[start:src.find("\n}\n", start)]
    assert "el.title=t;" in block
    # Both clearing paths drop the tooltip so a stale one never outlives its text.
    assert block.count("el.removeAttribute('title');") == 2
