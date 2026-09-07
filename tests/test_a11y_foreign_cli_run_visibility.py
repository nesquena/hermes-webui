"""Work performed from the CLI/TUI must be visible in the browser.

Report (18.08.2026): the same session open in a terminal and in the WebUI at once.
The terminal shows a turn in progress; the browser looks as if Hermes had finished.
For a screen reader user this is the worst possible state: silence
indistinguishable from a crash (WCAG 4.1.3).

MEASURED CAUSE (on a live session where the agent was actively writing):
``/api/session`` returned ``is_streaming=false`` and ``active_stream_id=null`` —
the server tracks only the streams it serves itself, so a turn running in the CLI
is invisible to it. Comparing a working session against an idle one showed NO
difference at all in the *stream*/*active*/*pending* fields.

The signal existed, it just was not passed on: the agent writes its current stage
to ``sessions.last_activity_at`` / ``last_activity_description``
("receiving stream response", "executing tool: terminal",
"terminal command running (60s elapsed)"). The WebUI read those columns nowhere.

WHY THE THRESHOLD IS 90s AND NOT 10 OR 30: sampling 22 times every 4s during real
work showed the marker can sit still for 58s (it updates on a STAGE CHANGE, not
every second), while the message counter did not move for the full 88s of the
measurement. A threshold shorter than the measured maximum makes the message FLICKER
in the middle of one long model call — and a flickering state is worse for a screen
reader user than no state at all.

The tests EXECUTE the real functions in node, because the defect is about a DECISION
made on data, not about the presence of text in the source.
"""

from pathlib import Path
import json
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parent.parent
A11Y_JS = (REPO / "static" / "a11y-helpers.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
MODELS_PY = (REPO / "api" / "models.py").read_text(encoding="utf-8")
AGENT_SESSIONS_PY = (REPO / "api" / "agent_sessions.py").read_text(encoding="utf-8")
NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

function mkEl(tag) {
  return {
    tagName: (tag || 'div').toUpperCase(),
    children: [], attrs: {}, classes: new Set(), dataset: {}, id: '',
    style: {}, textContent: '', innerHTML: '', parentNode: null, parentElement: null,
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { c.parentNode = this; c.parentElement = this; this.children.push(c); return c; },
    insertBefore(c) { c.parentNode = this; c.parentElement = this; this.children.unshift(c); return c; },
    addEventListener() {}, getClientRects() { return [{}]; },
    get classList() {
      const s = this.classes;
      return { add: (c) => s.add(c), remove: (c) => s.delete(c), contains: (c) => s.has(c),
               toggle: (c, on) => { if (on) s.add(c); else s.delete(c); } };
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
  };
}

const rejestr = {};
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
  setTimeout: () => 0, clearTimeout: () => {},
  setInterval: () => 0, clearInterval: () => {},
  MutationObserver: function () { return { observe() {}, disconnect() {} }; },
  location: { href: 'http://127.0.0.1/', search: '' },
  fetch: async () => ({ ok: false }),
};
ctx.window.document = ctx.document;
vm.createContext(ctx);
vm.runInContext("function t(k){return null;}", ctx);
vm.runInContext(fs.readFileSync(A11Y_PATH, 'utf8'), ctx);

const nowSec = () => Date.now() / 1000;
const out = {};

out.pracaSwieza = ctx.a11yForeignRunActivity({
  last_activity_at: nowSec() - 5,
  last_activity_description: 'executing tool: terminal',
  ended_at: null,
});
out.workWithGap58 = ctx.a11yForeignRunActivity({
  last_activity_at: nowSec() - 58,
  last_activity_description: 'receiving stream response',
  ended_at: null,
});
out.znacznikStary = ctx.a11yForeignRunActivity({
  last_activity_at: nowSec() - 200,
  last_activity_description: 'receiving stream response',
  ended_at: null,
});
out.sesjaZakonczona = ctx.a11yForeignRunActivity({
  last_activity_at: nowSec() - 3,
  last_activity_description: 'receiving stream response',
  ended_at: 1787000000,
});
out.brakOpisu = ctx.a11yForeignRunActivity({
  last_activity_at: nowSec() - 3, last_activity_description: '',
});
out.brakZnacznika = ctx.a11yForeignRunActivity({});
out.pusteDane = ctx.a11yForeignRunActivity(null);
out.znacznikZPrzyszlosci = ctx.a11yForeignRunActivity({
  last_activity_at: nowSec() + 600,
  last_activity_description: 'receiving stream response',
});

const host = mkEl('div'); host.id = 'a11yRunStatus'; rejestr['a11yRunStatus'] = host;
out.zapalony = ctx.a11ySyncForeignRunState({
  last_activity_at: nowSec() - 4,
  last_activity_description: 'executing tool: terminal',
});
out.tekstStanu = host.textContent;

out.zgaszony = ctx.a11ySyncForeignRunState({
  last_activity_at: nowSec() - 300,
  last_activity_description: 'receiving stream response',
});
out.tekstPoZgaszeniu = host.textContent;

let announcements = 0;
ctx.a11yAnnounce = () => { announcements += 1; };
ctx.window.a11yAnnounce = ctx.a11yAnnounce;
const data = { last_activity_at: nowSec() - 2, last_activity_description: 'executing tool: read_file' };
ctx.a11ySyncForeignRunState(data);
ctx.a11ySyncForeignRunState(data);
ctx.a11ySyncForeignRunState(data);
out.announcements = announcements;

ctx.a11yRunStarted();
out.wlasnyMaPierwszenstwo = ctx.a11ySyncForeignRunState({
  last_activity_at: nowSec() - 2,
  last_activity_description: 'executing tool: terminal',
});

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def behaviour(tmp_path_factory):
    if not NODE:
        pytest.skip("node unavailable - cannot measure behavior")
    script = tmp_path_factory.mktemp("foreign") / "harness.js"
    script.write_text(
        f"const A11Y_PATH = {json.dumps(str(REPO / 'static' / 'a11y-helpers.js'))};\n"
        + HARNESS,
        encoding="utf-8",
    )
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, f"harness padl: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestRecognisingWorkFromTheCli:
    def test_recent_work_is_recognised(self, behaviour):
        assert behaviour["pracaSwieza"] == "executing tool: terminal", (
            "a CLI turn must be visible in the browser"
        )

    def test_a_long_pause_does_not_clear_the_state(self, behaviour):
        """The measured maximum gap in the signal is 58 s."""
        assert behaviour["workWithGap58"] == "receiving stream response", (
            "the threshold must survive the longest MEASURED gap, otherwise the message "
            "flickers in the middle of one long model call"
        )

    def test_the_activity_is_specific(self, behaviour):
        """The user must know WHAT is happening, not only that something is happening."""
        assert "tool" in behaviour["pracaSwieza"] or "stream" in behaviour["pracaSwieza"]


class TestWhenTheStateMustStayOff:
    def test_a_stale_marker_is_not_active_work(self, behaviour):
        assert behaviour["znacznikStary"] == ""

    def test_a_finished_session_is_not_working(self, behaviour):
        assert behaviour["sesjaZakonczona"] == "", (
            "ended_at wins over marker freshness"
        )

    def test_no_description_means_no_guessing(self, behaviour):
        assert behaviour["brakOpisu"] == "", (
            "'something is happening' without content is noise, not information"
        )

    def test_missing_data_does_not_light_up_the_state(self, behaviour):
        assert behaviour["brakZnacznika"] == ""
        assert behaviour["pusteDane"] == ""

    def test_a_future_timestamp_is_rejected(self, behaviour):
        """A skewed clock must not produce permanent 'working'."""
        assert behaviour["znacznikZPrzyszlosci"] == ""


class TestQuietState:
    def test_state_turns_on_and_carries_the_activity(self, behaviour):
        assert behaviour["zapalony"] is True
        assert "executing tool: terminal" in behaviour["tekstStanu"]

    def test_state_says_the_work_is_elsewhere(self, behaviour):
        text = behaviour["tekstStanu"].lower()
        assert "elsewhere" in text or "another" in text, (
            "the user must know this is not a run owned by this tab"
        )

    def test_state_clears_when_the_work_stops(self, behaviour):
        assert behaviour["zgaszony"] is False
        assert behaviour["tekstPoZgaszeniu"] == ""

    def test_repeats_do_not_re_announce(self, behaviour):
        assert behaviour["announcements"] <= 1, (
            "the screen reader must hear the message ONCE; refreshes stay silent"
        )

    def test_local_run_takes_precedence(self, behaviour):
        assert behaviour["wlasnyMaPierwszenstwo"] is False, (
            "this tab's state is more precise - it must not be overwritten"
        )


class TestServerSideDataPath:
    """The signal must actually reach the browser from the database."""

    def test_query_selects_the_run_state_columns(self):
        assert "last_activity_at_expr" in AGENT_SESSIONS_PY
        assert "last_activity_description_expr" in AGENT_SESSIONS_PY
        assert "{last_activity_at_expr}" in AGENT_SESSIONS_PY, (
            "the columns must be in SELECT, not only defined"
        )

    def test_projection_passes_state_through_every_path(self):
        """The projection is created in four sibling paths."""
        assert MODELS_PY.count("_agent_row_live_work_state(row)") >= 4, (
            "every path must pass the state through, otherwise some sessions will be mute"
        )

    def test_api_session_reports_state_for_every_source(self):
        """The CLI session was missing from both existing metadata-merge branches."""
        idx = ROUTES_PY.find("_merge_cli_sidebar_metadata(raw, cli_meta)")
        assert idx > 0
        okno = ROUTES_PY[idx:idx + 1800]
        assert "last_activity_at" in okno and "last_activity_description" in okno, (
            "the pass-through must be OUTSIDE the webui/messaging branches"
        )

    def test_watchdog_accounts_for_the_agent_signal(self):
        idx = A11Y_JS.find("_a11yForeignPoll")
        assert idx > 0
        okno = A11Y_JS[idx:idx + 4000]
        assert "last_activity_at" in okno, (
            "last_message_at alone creates dead windows: measured 88 s of work without "
            "a single new message"
        )


class TestTranslations:
    def test_message_exists_in_every_locale(self):
        assert I18N_JS.count("a11y_run_working_elsewhere") >= 15, (
            f"key in {I18N_JS.count('a11y_run_working_elsewhere')} locales instead of 15"
        )
