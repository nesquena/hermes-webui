"""Turn numbers must be GLOBAL, not counted from the first row in the DOM.

Reported issue (18.08.2026): "in a long session the little numbers are 1, 2, 3, even
though the session has dozens of messages; I would prefer them to be calculated
globally - a screen reader user should have a clear sense that the session is
progressing".

WebUI loads only the tail of the conversation (older content is loaded backward
with a button), and heading numbering was counted from the first row IN THE DOM.
That had two effects, both harming a screen reader user's orientation:
 * "1." on turn number 576 said NOTHING about the position in the conversation,
 * the same turn changed number after every backfill of the window.

MEASURED COORDINATE TRAP: raw ``_messages_offset`` must not be used here,
because it counts storage rows while numbering applies to VISIBLE turns. In a
live session: offset 978 for a window that contained 100 turns across 194 rows
(the rest were tool rows collapsed into cards). So the server calculates the
offset with the SAME predicate,
``_message_counts_as_renderable_for_window``, that sliced the window.

Measured properties of the server response (step130, session with 576 turns):
    okno 10  -> before 566, w oknie 10,  total 576
    okno 30  -> before 546, w oknie 30,  total 576
    okno 100 -> before 476, w oknie 100, total 576
    okno 300 -> before 276, w oknie 300, total 576
so the last turn number is 576 REGARDLESS of window size - that is the core of
the report.
"""

from pathlib import Path
import json
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parent.parent
A11Y_JS = (REPO / "static" / "a11y-helpers.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
NODE = shutil.which("node")

# --- server-side part: calculate offset in the space of VISIBLE rows ---

import sys
sys.path.insert(0, str(REPO))


def _rows(*role):
    """Build a row list: 'u'/'a' = turn, 't' = tool row."""
    out = []
    for r in role:
        if r == "u":
            out.append({"role": "user", "content": "pytanie"})
        elif r == "a":
            out.append({"role": "assistant", "content": "odpowiedz"})
        else:
            out.append({"role": "tool", "content": "{}", "tool_call_id": "x"})
    return out


class TestServerSideOffsetCalculation:
    """``_renderable_count_before`` must count TURNS, not rows."""

    def test_tool_rows_are_skipped(self):
        from api.routes import _renderable_count_before
        # 10 rows, 4 of them are turns
        rows = _rows("u", "a", "t", "t", "u", "a", "t", "t", "t", "t")
        assert _renderable_count_before(rows, 10) == 4, (
            "counting raw rows would yield 10 and numbering would drift by "
            "the number of tool rows"
        )

    def test_zero_and_missing_offset(self):
        from api.routes import _renderable_count_before
        rows = _rows("u", "a")
        assert _renderable_count_before(rows, 0) == 0
        assert _renderable_count_before(rows, None) == 0

    def test_offset_larger_than_the_list(self):
        from api.routes import _renderable_count_before
        rows = _rows("u", "a", "t")
        assert _renderable_count_before(rows, 999) == 2

    def test_bad_data_does_not_break_it(self):
        from api.routes import _renderable_count_before
        assert _renderable_count_before(None, 5) == 0
        assert _renderable_count_before(_rows("u"), "abc") == 0
        assert _renderable_count_before(_rows("u"), -3) == 0

    def test_api_session_exposes_the_coordinates(self):
        assert '"_visible_turns_before"' in ROUTES_PY
        assert '"_visible_turns_total"' in ROUTES_PY
        idx = ROUTES_PY.find('raw["_visible_turns_before"]')
        assert idx > 0
        assert "_renderable_count_before" in ROUTES_PY[:idx], (
            "the offset must be calculated with the visibility predicate"
        )


# --- browser-side part: heading numbering ---

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

function mkEl(tag, klasy) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    children: [], attrs: {}, classes: new Set(klasy || []), dataset: {}, id: '',
    style: {}, textContent: '', innerHTML: '', parentNode: null, parentElement: null,
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { c.parentNode = this; c.parentElement = this; this.children.push(c); return c; },
    insertBefore(c) { c.parentNode = this; c.parentElement = this; this.children.unshift(c); return c; },
    // firstElementChild/firstChild MUST be computed: the code inserts the
    // heading at the beginning, and a cached field would keep showing the role label.
    get firstElementChild() { return this.children[0] || null; },
    get firstChild() { return this.children[0] || null; },
    set className(v) { for (const c of String(v || '').split(/\s+/)) if (c) this.classes.add(c); },
    get className() { return Array.from(this.classes).join(' '); },
    addEventListener() {}, getClientRects() { return [{}]; },
    get classList() {
      const s = this.classes;
      return { add: (c) => s.add(c), remove: (c) => s.delete(c), contains: (c) => s.has(c),
               toggle: (c, on) => { if (on) s.add(c); else s.delete(c); } };
    },
    descendants() { let o = []; for (const c of this.children) { o.push(c); o = o.concat(c.descendants()); } return o; },
    matches(sel) {
      if (sel === '.msg-row') return this.classes.has('msg-row');
      if (sel === '.msg-role') return this.classes.has('msg-role');
      return false;
    },
    querySelectorAll(sel) { return this.descendants().filter((d) => d.matches(sel)); },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    closest() { return null; },
    cloneNode() {
      const k = mkEl(this.tagName, Array.from(this.classes));
      k.textContent = this.textContent;
      k.attrs = Object.assign({}, this.attrs);
      k.dataset = Object.assign({}, this.dataset);
      for (const c of this.children) k.appendChild(c.cloneNode(true));
      return k;
    },
    remove() {
      const p = this.parentNode;
      if (p) p.children = p.children.filter((c) => c !== this);
    },
  };
  return el;
}

const messages = mkEl('div');
messages.id = 'messages';
const rejestr = { messages };
const ctx = {
  window: {},
  document: {
    activeElement: null, readyState: 'complete',
    createElement: (t) => mkEl(t),
    getElementById: (id) => rejestr[id] || null,
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener: () => {}, body: mkEl('body'),
  },
  console, Math, JSON, String, Number, Boolean, Array, Object, Date, RegExp, Set, Map,
  requestAnimationFrame: (fn) => fn(),
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  MutationObserver: function () { return { observe() {}, disconnect() {} }; },
  location: { href: 'http://127.0.0.1/', search: '' },
  fetch: async () => ({ ok: false }),
};
ctx.window.document = ctx.document;
vm.createContext(ctx);
vm.runInContext("function t(k){return null;}", ctx);
vm.runInContext(fs.readFileSync(A11Y_PATH, 'utf8'), ctx);

function zbudujOkno(n) {
  messages.children.length = 0;
  for (let i = 0; i < n; i++) {
    const row = mkEl('div', ['msg-row']);
    row.dataset.role = (i % 2 === 0) ? 'user' : 'assistant';
    if (i % 2 === 1) row.classes.add('assistant-turn');
    const rola = mkEl('span', ['msg-role']);
    rola.setAttribute('title', '18.08.2026, 09:15:25');
    rola.textContent = row.dataset.role === 'user' ? 'You' : 'Hermes';
    row.appendChild(rola);
    messages.appendChild(row);
  }
}

function numbers() {
  return messages.children
    .map((r) => (r.firstElementChild && r.firstElementChild.textContent) || '')
    .filter((s) => s);
}

const out = {};

zbudujOkno(3);
ctx.a11ySetTurnNumbering(0, 3);
out.short_key = numbers();

zbudujOkno(3);
ctx.a11ySetTurnNumbering(573, 576);
out.long_key = numbers();

zbudujOkno(6);
ctx.a11ySetTurnNumbering(570, 576);
out.poDoladowaniu = numbers();

zbudujOkno(2);
ctx.a11ySetTurnNumbering(0, 2);
out.beforeChange = numbers();
ctx.a11ySetTurnNumbering(100, 102);
out.afterChange = numbers();
const a = numbers().join('|');
ctx.a11ySetTurnNumbering(100, 102);
out.idempotentne = (a === numbers().join('|'));

zbudujOkno(2);
ctx.a11ySetTurnNumbering(undefined, undefined);
out.brakDanych = numbers();
ctx.a11ySetTurnNumbering(-5, -1);
out.ujemne = numbers();
ctx.a11ySetTurnNumbering('abc', 'xyz');
out.text = numbers();
ctx.a11ySetTurnNumbering(10.7, 20.2);
out.niecalkowite = numbers();

zbudujOkno(1);
ctx.a11ySetTurnNumbering(42, 60);
const h = messages.children[0].firstElementChild;
out.heading = { tag: h && h.tagName, text: h && h.textContent };

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def behaviour(tmp_path_factory):
    if not NODE:
        pytest.skip("node unavailable - cannot measure behavior")
    script = tmp_path_factory.mktemp("num") / "harness.js"
    script.write_text(
        f"const A11Y_PATH = {json.dumps(str(REPO / 'static' / 'a11y-helpers.js'))};\n"
        + HARNESS,
        encoding="utf-8",
    )
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, f"harness padl: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestGlobalNumbering:
    def test_a_long_session_does_not_start_at_one(self, behaviour):
        """Core report: a 3-turn window, 573 hidden -> numbers 574-576."""
        d = behaviour["long_key"]
        assert d[0].startswith("574"), f"pierwszy heading: {d[0]}"
        assert d[-1].startswith("576"), f"ostatni heading: {d[-1]}"

    def test_a_short_session_numbers_from_one(self, behaviour):
        assert behaviour["short_key"][0].startswith("1")

    def test_first_heading_states_the_total(self, behaviour):
        """The user should perceive that the session is progressing."""
        assert "/576" in behaviour["long_key"][0]

    def test_later_headings_do_not_repeat_the_total(self, behaviour):
        """Repeating the total on every heading was verbose during heading navigation."""
        for n in behaviour["long_key"][1:]:
            assert "/576" not in n, f"heading repeats the total: {n}"


class TestLoadingOlderMessages:
    def test_numbers_of_older_turns_do_not_jump(self, behaviour):
        """The same turn must keep the same number after backfill."""
        assert behaviour["poDoladowaniu"][-1].startswith("576"), (
            f"ostatni po doladowaniu: {behaviour['poDoladowaniu'][-1]}"
        )

    def test_newly_revealed_turns_get_lower_numbers(self, behaviour):
        assert behaviour["poDoladowaniu"][0].startswith("571")

    def test_the_total_stays_the_same(self, behaviour):
        assert "/576" in behaviour["poDoladowaniu"][0]

    def test_changing_the_offset_recomputes_existing_headings(self, behaviour):
        """Without recomputation, backfill would leave stale numbers."""
        assert behaviour["beforeChange"][0] != behaviour["afterChange"][0]
        assert behaviour["afterChange"][0].startswith("101")

    def test_setting_it_again_changes_nothing(self, behaviour):
        assert behaviour["idempotentne"] is True


class TestRobustnessAgainstBadData:
    """Missing or bad data must not break numbering - local is better than none."""

    def test_missing_data_falls_back_to_local_numbering(self, behaviour):
        assert behaviour["brakDanych"][0].startswith("1")

    def test_negative_numbers_are_rejected(self, behaviour):
        assert behaviour["ujemne"][0].startswith("1")

    def test_text_instead_of_a_number_is_rejected(self, behaviour):
        assert behaviour["text"][0].startswith("1")

    def test_non_integer_numbers_are_truncated(self, behaviour):
        assert behaviour["niecalkowite"][0].startswith("11")


class TestHeadingIsReadableByScreenReaders:
    def test_heading_is_an_h2(self, behaviour):
        assert behaviour["heading"]["tag"] == "H2"

    def test_heading_contains_the_global_number(self, behaviour):
        assert behaviour["heading"]["text"].startswith("43")


class TestEveryWindowLoadPath:
    """Numbering must be set everywhere the window changes."""

    def test_every_call_site_sets_the_numbering(self):
        trafienia = 0
        for name in ("messages.js", "sessions.js", "ui.js"):
            trafienia += (REPO / "static" / name).read_text(
                encoding="utf-8").count("a11ySetTurnNumbering(")
        assert trafienia >= 4, (
            f"only {trafienia} hookups - the window changes in four places "
            "(full load, backward backfill, refresh, session switch)"
        )

    def test_loading_older_messages_is_wired_up(self):
        sessions = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
        idx = sessions.find("_oldestIdx = responseSession._messages_offset")
        assert idx > 0
        assert "a11ySetTurnNumbering" in sessions[idx:idx + 600], (
            "without this, loading older messages would leave stale numbers"
        )
