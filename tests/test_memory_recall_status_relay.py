"""The Agent's memory recall indicator reaches the WebUI as a ``memory_recall`` event.

Right after an external memory provider injects context for a turn, hermes-agent
(``agent/turn_context.py``) emits ``MemoryManager.describe_recall()`` through
``_emit_status`` with kind ``lifecycle``, e.g. "🧠 Honcho — recalled 3 memories".
The CLI prints it and the messaging gateway sends it, but ``_agent_status_callback``
dropped it, so in the WebUI there was no sign that memory was used at all.

The line carries no marker of its own, so the relay does not match its wording: it
compares the line with what the agent's memory manager reports for the same turn.
"""
import json
import pathlib
import re
import shutil
import subprocess
import textwrap
from types import SimpleNamespace

import pytest

from api import streaming
from tests.test_composer_status_timeout import _composer_status_source
from tests.test_session_lease_wait_warnings import _build_status_callback

REPO = pathlib.Path(__file__).resolve().parents[1]
RECALL = "🧠 Honcho — recalled 3 memories"
NODE = shutil.which("node")


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


def _memory_recall_listener_source():
    src = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
    start = src.find("source.addEventListener('memory_recall'")
    assert start != -1, "memory_recall listener not found"
    end = src.find("source.addEventListener('warning'", start)
    assert end != -1
    return src[start:end]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_memory_recall_listener_and_composer_status_run_in_node():
    # Runs the real listener from messages.js and the real setComposerStatus from
    # ui.js against a stub composer element and a fake clock. The composer status is
    # ellipsized (max-width 170px, 96px narrow), so the full text rides on `title`.
    listener = _memory_recall_listener_source()
    composer_status = _composer_status_source(
        (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    )
    script = textwrap.dedent(
        """
        const el = {
          style: {display: 'none'},
          textContent: '',
          attrs: {},
          classList: {remove() {}},
          removeAttribute(name) { delete this.attrs[name]; },
          set title(value) { this.attrs.title = String(value); },
          get title() { return this.attrs.title || ''; },
        };
        globalThis.window = {_composerControlVisibility: {}};
        globalThis.$ = (id) => id === 'composerStatus' ? el : null;

        let now = 0;
        let nextTimerId = 1;
        const timers = new Map();
        globalThis.setTimeout = (fn, delay) => {
          const id = nextTimerId++;
          timers.set(id, {fn, due: now + delay});
          return id;
        };
        globalThis.clearTimeout = (id) => timers.delete(id);
        function advance(ms) {
          const target = now + ms;
          for (;;) {
            let next = null;
            for (const [id, timer] of timers) {
              if (timer.due <= target && (!next || timer.due < next.timer.due)) next = {id, timer};
            }
            if (!next) break;
            timers.delete(next.id);
            now = next.timer.due;
            next.timer.fn();
          }
          now = target;
        }
        function expect(condition, message) { if (!condition) throw new Error(message); }
        function shown() { return el.style.display !== 'none' ? el.textContent : ''; }
        const hasTitle = () => 'title' in el.attrs;

        __COMPOSER_STATUS__

        const handlers = {};
        const source = {addEventListener(name, fn) { handlers[name] = fn; }};
        const activeSid = 'sid-1';
        const S = {session: {session_id: 'sid-1'}};
        const RECALL = __RECALL__;
        const deliver = (payload) => handlers.memory_recall({data: JSON.stringify(payload)});

        __LISTENER__

        expect(typeof handlers.memory_recall === 'function', 'listener not registered');

        deliver({session_id: 'sid-1', message: RECALL});
        expect(shown() === RECALL, 'recall for the session on screen is shown');
        expect(el.attrs.title === RECALL, 'full text is kept as the tooltip');
        advance(5999);
        expect(shown() === RECALL, 'notice stays until its timeout');
        advance(1);
        expect(!shown(), 'notice hides after 6 s');
        expect(!hasTitle(), 'tooltip is cleared with the notice');

        deliver({session_id: 'sid-2', message: RECALL});
        expect(!shown() && !hasTitle(), 'a foreign-session event is ignored');

        S.session = {session_id: 'sid-2'};
        deliver({session_id: 'sid-1', message: RECALL});
        expect(!shown(), 'nothing is shown after switching away from the stream');
        S.session = null;
        deliver({session_id: 'sid-1', message: RECALL});
        expect(!shown(), 'nothing is shown without a session');
        S.session = {session_id: 'sid-1'};

        deliver({session_id: 'sid-1', message: ''});
        expect(!shown(), 'an empty message shows nothing');
        handlers.memory_recall({data: 'not json'});
        expect(!shown(), 'a malformed payload is ignored');

        deliver({message: RECALL});
        expect(shown() === RECALL, 'a payload without session_id belongs to this stream');
        setComposerStatus('');
        expect(!shown() && !hasTitle(), 'an explicit clear drops the tooltip');

        deliver({session_id: 'sid-1', message: RECALL});
        window._composerControlVisibility.hide_composer_status = true;
        setComposerStatus('Other notice');
        expect(!shown() && !hasTitle(), 'a hidden composer status drops the tooltip');
        advance(6000);
        expect(!shown(), 'a hidden composer status stays hidden');

        process.stdout.write(JSON.stringify({ok: true}));
        """
    )
    script = (
        script.replace("__COMPOSER_STATUS__", composer_status)
        .replace("__LISTENER__", listener)
        .replace("__RECALL__", json.dumps(RECALL))
    )
    completed = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, encoding="utf-8"
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"ok": True}


def test_memory_recall_is_tracked_by_the_run_journal_cursor():
    # The event is journaled like every other relayed event; an untracked one would
    # leave the replay cursor one row behind after a reconnect.
    src = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
    cursor = re.search(r"for\(const _runJournalEventName of \[([^\]]*)\]", src)
    assert cursor and "'memory_recall'" in cursor.group(1)
