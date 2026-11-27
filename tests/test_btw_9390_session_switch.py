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
grep.
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

let root;
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

root = mkEl('root');
const msgInner = mkEl('div');
root.appendChild(msgInner);

let es;
const ctx = {
  S: { session: { session_id: 'A' } },
  $: (id) => (id === 'msgInner' ? msgInner : null),
  t: (k) => k,
  renderMd: (s) => s,
  showToast: () => {},
  document: { createElement: mkEl, baseURI: 'http://x/' },
  location: { href: 'http://x/' },
  URL,
  EventSource: function () {
    this.h = {};
    this.addEventListener = (n, f) => { this.h[n] = f; };
    this.close = () => {};
    es = this;
  },
};

vm.createContext(ctx);
vm.runInContext(src + '\nattachBtwStream("A","s1","what is 2+2?");', ctx);

const btwRows = () => msgInner.children.filter((c) => c.dataset.btw === '1');
const tok = (t) => es.h.token({ data: JSON.stringify({ text: t }) });
const out = {};

// The asking chat renders the bubble while it is on screen.
tok('Hello ');
out.askingChatRows = btwRows().length;

// Switching away clears #msgInner (that is what renderMessages() does). A token
// that lands afterwards must not rebuild the bubble in the other chat.
ctx.S.session = { session_id: 'B' };
msgInner.innerHTML = '';
tok('world');
out.otherChatRowsAfterToken = btwRows().length;

// Nor may `done` paint there.
es.h.done({ data: JSON.stringify({ answer: 'Hello world' }) });
out.otherChatRowsAfterDone = btwRows().length;

// Coming back to the asking chat rebuilds the bubble with the full answer,
// because `answer` is accumulated in the closure.
ctx.S.session = { session_id: 'A' };
tok('!');
const back = btwRows();
out.askingChatRowsAfterReturn = back.length;
const ans = back.length ? back[0].querySelector('.msg-btw-answer') : null;
out.askingChatAnswerAfterReturn = ans ? ans.innerHTML : null;

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


def test_done_does_not_paint_into_another_chat(observed):
    assert observed["otherChatRowsAfterDone"] == 0, (
        "the /btw `done` handler painted the side-answer bubble into a chat "
        "that did not ask the question (#9390)"
    )


def test_returning_to_the_asking_chat_restores_the_answer(observed):
    assert observed["askingChatRowsAfterReturn"] == 1, (
        "switching back to the asking chat must rebuild the bubble from the "
        "answer accumulated in the stream closure"
    )
    assert observed["askingChatAnswerAfterReturn"] == "Hello world!", (
        "the rebuilt bubble must carry the whole accumulated answer, "
        f"observed {observed['askingChatAnswerAfterReturn']!r}"
    )
