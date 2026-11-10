"""The Agent's memory recall indicator is shown at the top of the WebUI turn.

Right after an external memory provider injects context for a turn, hermes-agent
(``agent/turn_context.py``) emits ``MemoryManager.describe_recall()`` through
``_emit_status`` with kind ``lifecycle``, e.g. "🧠 Honcho — recalled 3 memories".
The CLI prints it above the reply and the messaging gateway sends it, but
``_agent_status_callback`` dropped it, so in the WebUI there was no sign that
memory was used at all.

The line carries no marker of its own, so the relay does not match its wording: it
compares the line with what the agent's memory manager reports for the same turn.
It is relayed as a ``memory_recall`` event, shown on its own line under the
assistant name (where the CLI prints it above the reply), and
persisted on the settled turn as ``_memoryRecall`` (next to ``_turnTps``). The
composer is not involved.
"""
import json
import pathlib
import re
import shutil
import subprocess
import textwrap
from types import SimpleNamespace

import pytest

from api import models, streaming
from tests.test_composer_status_timeout import _function_source
from tests.test_session_lease_wait_warnings import _build_status_callback

REPO = pathlib.Path(__file__).resolve().parents[1]
RECALL = "🧠 Honcho — recalled 3 memories"
NODE = shutil.which("node")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")


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


def test_status_callback_relays_memory_recall_and_keeps_it_for_the_turn():
    callback, events = _build_status_callback(agent=_agent(lambda: RECALL))
    callback("lifecycle", RECALL)
    assert events == [("memory_recall", {"session_id": "sid-1", "message": RECALL})]
    assert callback.__globals__["_turn_memory_recall"] == [RECALL]


def test_status_callback_drops_recall_shaped_text_without_a_recall():
    callback, events = _build_status_callback(agent=_agent(lambda: ""))
    callback("lifecycle", RECALL)
    callback("lifecycle", "Loaded 12 tools")
    assert events == []
    assert callback.__globals__["_turn_memory_recall"] == [None]


def test_status_callback_other_relays_unchanged_with_memory_active():
    callback, events = _build_status_callback(agent=_agent(lambda: RECALL))
    callback("lifecycle", "Rate limited — switching to fallback model")
    assert events == [
        ("warning", {"type": "fallback", "message": "Rate limited — switching to fallback model"})
    ]


def test_settled_turn_is_stamped_next_to_turn_tps():
    # The settle loop stamps the same assistant row that receives _turnTps.
    tps = "                                _dm['_turnTps'] = _turn_tps\n"
    stamp = (
        "                            if _turn_memory_recall[0]:\n"
        "                                _dm['_memoryRecall'] = _turn_memory_recall[0]\n"
    )
    assert STREAMING_PY.count(stamp) == 1
    assert STREAMING_PY.index(tps) < STREAMING_PY.index(stamp)
    assert STREAMING_PY.index(stamp) - STREAMING_PY.index(tps) < 200


def test_failed_turn_keeps_the_recall_on_its_error_row():
    # Both error settle paths build an _error assistant row; the recall still
    # happened, so the turn keeps showing it.
    stamps = re.findall(
        r"if _turn_memory_recall\[0\]:\n\s+_error_message\['_memoryRecall'\] = _turn_memory_recall\[0\]",
        STREAMING_PY,
    )
    assert len(stamps) == 2


def test_memory_recall_survives_transcript_row_merges():
    # Display-only turn metadata must survive the sidecar/state.db merge.
    target = {"role": "assistant", "content": "answer"}
    models._merge_session_display_metadata(target, {"_memoryRecall": RECALL, "_turnTps": 12.5})
    assert target["_memoryRecall"] == RECALL
    kept = {"_memoryRecall": "🧠 kept"}
    models._merge_session_display_metadata(kept, {"_memoryRecall": RECALL})
    assert kept["_memoryRecall"] == "🧠 kept"


def test_memory_recall_is_carried_forward_and_tracked_by_the_run_journal():
    fields = re.search(r"const _EPHEMERAL_TURN_FIELDS=\[([^\]]*)\]", MESSAGES_JS)
    assert fields and "'_memoryRecall'" in fields.group(1)
    # Journaled like every other relayed event; an untracked one would leave the
    # replay cursor one row behind after a reconnect.
    cursor = re.search(r"for\(const _runJournalEventName of \[([^\]]*)\]", MESSAGES_JS)
    assert cursor and "'memory_recall'" in cursor.group(1)


def test_memory_recall_does_not_touch_the_composer():
    listener = _memory_recall_listener_source()
    assert "setComposerStatus" not in listener
    assert "composerStatus" not in listener


def _memory_recall_listener_source():
    start = MESSAGES_JS.find("source.addEventListener('memory_recall'")
    assert start != -1, "memory_recall listener not found"
    end = MESSAGES_JS.find("source.addEventListener('warning'", start)
    assert end != -1
    return MESSAGES_JS[start:end]


def _recall_line_helpers_source():
    start = UI_JS.index("const _liveMemoryRecall=new Map();")
    tail = _function_source(UI_JS, "_syncLiveAssistantMemoryRecall")
    end = UI_JS.index(tail) + len(tail)
    assert start < end
    return UI_JS[start:end]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_memory_recall_lands_in_the_live_turn_in_node():
    # Runs the real listener from messages.js and the real recall-line helpers from
    # ui.js against a stub DOM. setComposerStatus throws: the composer stays out.
    script = textwrap.dedent(
        """
        class El {
          constructor(className) {
            this.className = className || '';
            this.children = [];
            this.parent = null;
            this.textContent = '';
            this.title = '';
            this.dataset = {};
          }
          appendChild(child) { child.parent = this; this.children.push(child); return child; }
          after(node) {
            const siblings = this.parent.children;
            node.parent = this.parent;
            siblings.splice(siblings.indexOf(this) + 1, 0, node);
          }
          remove() {
            if (!this.parent) return;
            this.parent.children = this.parent.children.filter((c) => c !== this);
            this.parent = null;
          }
          querySelector(selector) {
            const wanted = selector.split('.').filter(Boolean);
            const walk = (node) => {
              for (const child of node.children) {
                const classes = child.className.split(/\\s+/);
                if (wanted.every((c) => classes.includes(c))) return child;
                const found = walk(child);
                if (found) return found;
              }
              return null;
            };
            return walk(this);
          }
        }
        globalThis.document = {createElement: () => new El()};
        function newTurn(sessionId) {
          const turn = new El('msg-row assistant-turn');
          const role = turn.appendChild(new El('msg-role assistant'));
          role.appendChild(new El('msg-role-name'));
          turn.appendChild(new El('assistant-turn-blocks'));
          turn.dataset.sessionId = sessionId;
          return turn;
        }
        const lineOf = (turn) => turn.children.find((c) => c.className === 'msg-memory-recall') || null;
        let liveTurn = null;
        globalThis.$ = (id) => id === 'liveAssistantTurn' ? liveTurn : null;
        globalThis.setComposerStatus = () => { throw new Error('composer touched'); };
        const S = {session: {session_id: 'sid-1'}, activeStreamId: 'stream-1'};
        function expect(condition, message) { if (!condition) throw new Error(message); }

        __HELPERS__

        const handlers = {};
        const source = {addEventListener(name, fn) { handlers[name] = fn; }};
        const activeSid = 'sid-1';
        const streamId = 'stream-1';
        const RECALL = __RECALL__;
        const deliver = (payload) => handlers.memory_recall({data: JSON.stringify(payload)});

        __LISTENER__

        expect(typeof handlers.memory_recall === 'function', 'listener not registered');

        // Recall arrives before the live turn exists: kept, then applied once the
        // live turn is built (placeLiveRunStatusHost / live renders call the sync).
        deliver({session_id: 'sid-1', message: RECALL});
        liveTurn = newTurn('sid-1');
        _syncLiveAssistantMemoryRecall();
        const line = lineOf(liveTurn);
        expect(line && line.textContent === RECALL, 'recall shown in the live turn');
        expect(line.title === RECALL, 'full text kept as the tooltip');
        expect(liveTurn.children.map((c) => c.className).join('|') ===
               'msg-role assistant|msg-memory-recall|assistant-turn-blocks',
               'own line between the assistant name and the turn body');

        // Re-syncing and replayed events after a reconnect keep one chip.
        _syncLiveAssistantMemoryRecall();
        deliver({session_id: 'sid-1', message: RECALL});
        expect(liveTurn.children.filter((c) => c.className === 'msg-memory-recall').length === 1,
               'one line per turn');

        // A foreign-session event changes nothing.
        deliver({session_id: 'sid-2', message: '🧠 Other — recalled 9 memories'});
        expect(lineOf(liveTurn).textContent === RECALL, 'a foreign-session event is ignored');

        // A turn of the next stream does not inherit the previous recall.
        S.activeStreamId = 'stream-2';
        liveTurn = newTurn('sid-1');
        _syncLiveAssistantMemoryRecall();
        expect(!lineOf(liveTurn), 'a stale recall does not leak into the next turn');
        S.activeStreamId = 'stream-1';

        // Off screen: the event is kept for the stream and applied on return.
        S.session = {session_id: 'sid-9'};
        liveTurn = newTurn('sid-1');
        deliver({session_id: 'sid-1', message: RECALL});
        expect(!lineOf(liveTurn), 'nothing is drawn while another session is on screen');
        S.session = {session_id: 'sid-1'};
        _syncLiveAssistantMemoryRecall();
        expect(lineOf(liveTurn).textContent === RECALL, 'applied after switching back');

        // A live turn stamped for another session is never written.
        liveTurn = newTurn('sid-2');
        _syncLiveAssistantMemoryRecall();
        expect(!lineOf(liveTurn), 'a turn of another session is left alone');

        // Empty messages and malformed payloads are ignored.
        liveTurn = newTurn('sid-1');
        _liveMemoryRecall.clear();
        deliver({session_id: 'sid-1', message: ''});
        handlers.memory_recall({data: 'not json'});
        _syncLiveAssistantMemoryRecall();
        expect(!lineOf(liveTurn), 'nothing shown for an empty or malformed event');

        // Settled turns: renderMessages applies the persisted value; '' clears.
        const settled = newTurn('sid-1');
        _setAssistantTurnMemoryRecall(settled, RECALL);
        expect(lineOf(settled).textContent === RECALL, 'settled value applied');
        _setAssistantTurnMemoryRecall(settled, '');
        expect(!lineOf(settled), 'empty value removes the line');

        process.stdout.write(JSON.stringify({ok: true}));
        """
    )
    script = (
        script.replace("__HELPERS__", _recall_line_helpers_source())
        .replace("__LISTENER__", _memory_recall_listener_source())
        .replace("__RECALL__", json.dumps(RECALL))
    )
    completed = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, encoding="utf-8"
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"ok": True}


def test_settled_rows_and_live_paths_apply_the_recall_line():
    # renderMessages applies the persisted value per row (the server stamps the
    # turn's last assistant row), clears it on a recycled turn, and the live-turn
    # lifecycle re-syncs it.
    assert "_setAssistantTurnMemoryRecall(recycled, '');" in UI_JS
    assert (
        "if(m._memoryRecall&&typeof _setAssistantTurnMemoryRecall==='function') "
        "_setAssistantTurnMemoryRecall(currentAssistantTurn, m._memoryRecall);"
    ) in UI_JS
    for name in ("_moveLiveRunStatusToTurnEnd", "placeLiveRunStatusHost"):
        assert "_syncLiveAssistantMemoryRecall();" in _function_source(UI_JS, name), name
    done = MESSAGES_JS[MESSAGES_JS.index("lastAsst._ts=Date.now()/1000;"):]
    assert done.index("lastAsst._memoryRecall=_recall;") < 400
