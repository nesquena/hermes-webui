"""Regression test for #9390: a /btw side answer must stay in the chat that
asked the question.

`attachBtwStream()` renders the ephemeral answer into `#msgInner`, the container
owned by whichever session is currently on screen. The `done` handler already
refused to paint when the user had switched away, but the `token` handler called
`_ensureBtwRow()` unconditionally, so a streaming side answer appeared inside a
chat that never asked for it (and vanished on reload).

The guard belongs at the single choke point, `_ensureBtwRow()`, so every caller
inherits it.

The test drives the real function out of `static/messages.js` in a Node VM with
a minimal DOM, so it fails on the actual control flow rather than on a source
grep. Two lifecycle arms keep the harness faithful to a real `EventSource`:

* mid-stream: switch away and back while tokens are still arriving; the next
  token rebuilds the bubble from the accumulated answer.
* completion: `done` closes the `EventSource`, so no later token can be
  delivered and the ephemeral answer is not restored by switching back later.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
MESSAGES_JS = REPO / "static" / "messages.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None, reason="node is required to drive the /btw stream harness"
)


HARNESS = r"""
// Driven by tests/test_btw_9390_session_switch.py
const fs = require('fs');
const vm = require('vm');

const raw = fs.readFileSync(process.argv[2], 'utf8');

// messages.js runs top-level side effects; extract only the function under
// test by brace matching from its declaration.
function extractFn(text, name) {
  const start = text.indexOf('function ' + name + '(');
  if (start < 0) throw new Error(name + ' not found');
  const open = text.indexOf('{', start);
  if (open < 0) throw new Error('no body for ' + name);
  let depth = 0;
  for (let j = open; j < text.length; j++) {
    const ch = text[j];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return text.slice(start, j + 1);
    }
  }
  throw new Error('unbalanced body for ' + name);
}

const src = extractFn(raw, 'attachBtwStream');

// One browser-ish world per arm: its own #msgInner, its own EventSource.
function boot(sessionId) {
  function mkEl(tag) {
    const e = {
      tag, children: [], parent: null, className: '', dataset: {},
      textContent: '', _html: '',
      appendChild(c) { c.parent = this; this.children.push(c); return c; },
      remove() {
        if (this.parent) {
          this.parent.children = this.parent.children.filter((x) => x !== this);
          this.parent = null;
        }
      },
      get isConnected() {
        let p = this;
        while (p.parent) p = p.parent;
        return p === root;
      },
      set innerHTML(v) {
        this.children.forEach((c) => { c.parent = null; });
        this.children = [];
        this._html = v;
      },
      get innerHTML() { return this._html; },
      querySelector(sel) {
        const cls = sel.slice(1);
        const st = [...this.children];
        while (st.length) {
          const c = st.shift();
          if (c.className.split(' ').includes(cls)) return c;
          st.push(...c.children);
        }
        return null;
      },
      scrollIntoView() {},
    };
    return e;
  }

  const root = mkEl('root');
  const msgInner = mkEl('div');
  root.appendChild(msgInner);

  let es;
  const ctx = {
    S: { session: { session_id: sessionId } },
    $: (id) => (id === 'msgInner' ? msgInner : null),
    t: (k) => k,
    renderMd: (s) => s,
    showToast: () => {},
    document: { createElement: mkEl, baseURI: 'http://x/' },
    location: { href: 'http://x/' },
    URL,
    EventSource: function () {
      this.h = {};
      this.closed = false;
      this.addEventListener = (n, f) => { this.h[n] = f; };
      // A real EventSource stops delivering events once closed.
      this.close = () => { this.closed = true; };
      es = this;
    },
  };

  vm.createContext(ctx);
  vm.runInContext(src + '\nattachBtwStream("A","s1","what is 2+2?");', ctx);

  const btwRows = () => msgInner.children.filter((c) => c.dataset.btw === '1');
  const emit = (name, payload) => {
    if (es.closed) return false;  // the browser would not call the handler
    es.h[name]({ data: JSON.stringify(payload) });
    return true;
  };
  // renderMessages() empties #msgInner when the visible session changes.
  const switchTo = (sid) => {
    ctx.S.session = { session_id: sid };
    msgInner.innerHTML = '';
  };
  return { btwRows, emit, switchTo, get closed() { return es.closed; } };
}

const out = {};

// Arm 1: switch away and back before the stream completes. The answer is
// ephemeral, so the only rebuild trigger is the next live token.
const mid = boot('A');
mid.emit('token', { text: 'Hello ' });
out.askingChatRows = mid.btwRows().length;

mid.switchTo('B');
mid.emit('token', { text: 'world' });
out.otherChatRowsAfterToken = mid.btwRows().length;

mid.switchTo('A');
mid.emit('token', { text: '!' });
const back = mid.btwRows();
out.askingChatRowsAfterReturn = back.length;
const ans = back.length ? back[0].querySelector('.msg-btw-answer') : null;
out.askingChatAnswerAfterReturn = ans ? ans.innerHTML : null;
out.returnHappenedWhileStreamOpen = !mid.closed;

// Arm 2: the stream completes while another chat is on screen. `done` closes
// the EventSource, so a late token is not deliverable at all.
const away = boot('A');
away.emit('token', { text: 'Hello ' });
away.switchTo('B');
away.emit('token', { text: 'world' });
out.doneAwayRowsAfterToken = away.btwRows().length;

away.emit('done', { answer: 'Hello world' });
out.doneAwayRowsAfterDone = away.btwRows().length;
out.doneAwayStreamClosed = away.closed;
out.tokenAfterCloseDelivered = away.emit('token', { text: '!' });

console.log(JSON.stringify(out));
"""


def _run_harness(tmp_path: pathlib.Path) -> dict:
    script = tmp_path / "btw_9390_harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(script), str(MESSAGES_JS)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "the /btw harness failed to run:\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def observed(tmp_path_factory):
    return _run_harness(tmp_path_factory.mktemp("btw9390"))


def test_asking_chat_still_renders_its_own_bubble(observed):
    assert observed["askingChatRows"] == 1, (
        "the /btw bubble must render in the chat that asked the question; "
        f"observed {observed['askingChatRows']} row(s)"
    )


def test_token_does_not_paint_into_another_chat(observed):
    assert observed["otherChatRowsAfterToken"] == 0, (
        "a streaming /btw token rebuilt the side-answer bubble inside a chat "
        "that did not ask the question (#9390). The guard has to live in "
        "`_ensureBtwRow()` so the `token` handler inherits it."
    )
    assert observed["doneAwayRowsAfterToken"] == 0, (
        "the same cross-chat token guard must hold when the asking chat is "
        "away and the stream later completes there"
    )


def test_done_does_not_paint_into_another_chat(observed):
    assert observed["doneAwayRowsAfterDone"] == 0, (
        "the /btw `done` handler painted the side-answer bubble into a chat "
        "that did not ask the question (#9390)"
    )


def test_switching_back_mid_stream_restores_the_answer(observed):
    assert observed["returnHappenedWhileStreamOpen"] is True, (
        "this recovery path only exists while the stream is live: `done` "
        "closes the EventSource, so there is no later event to rebuild the "
        "bubble from"
    )
    assert observed["askingChatRowsAfterReturn"] == 1, (
        "switching back to the asking chat mid-stream must rebuild the bubble "
        "from the answer accumulated in the stream closure"
    )
    assert observed["askingChatAnswerAfterReturn"] == "Hello world!", (
        "the rebuilt bubble must carry the whole accumulated answer, "
        f"observed {observed['askingChatAnswerAfterReturn']!r}"
    )


def test_harness_models_event_source_close(observed):
    assert observed["doneAwayStreamClosed"] is True, (
        "`done` must close the EventSource; a no-op mock would let the test "
        "drive an event sequence a browser cannot deliver"
    )
    assert observed["tokenAfterCloseDelivered"] is False, (
        "the harness delivered a token after `EventSource.close()`; real "
        "streams cannot emit one, so that sequence must not be testable"
    )
