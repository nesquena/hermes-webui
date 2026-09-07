"""A pending poll response must not restore run state for a conversation the user left.

Raised in review of PR #7258 (P1, three inline findings, two of them this same
class): `_a11yForeignPoll` checks `sid !== _a11yForeignSid` BEFORE `await fetch`,
then uses the resolved response afterwards without re-checking ownership. If the
user switches conversations while the request is in flight, the late response
carries the PREVIOUS session's data and:

 * `_a11yForeignLastStamp` / `_a11yForeignLastGrowthAt` get set from that session,
   so the baseline of the newly opened conversation is polluted;
 * `a11yRunStarted()` plus `a11ySyncForeignRunState()` can announce and render
   "Hermes is working" in a conversation that is idle.

For a screen reader user this is the same failure mode the original fix was
written against, only inverted: instead of silence during work, a spoken claim of
work that is not happening. Freshness is not proof of continuation, and neither is
a response whose session is no longer on screen.

These tests execute the real code in node and drive the race explicitly: the
harness lets a poll start under session A, switches the location to session B
while the fetch promise is unresolved, and only then resolves it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(SRC, 'utf8');

function mkEl(){
  const el = {
    _attrs: {}, _cls: new Set(), children: [], listeners: {},
    textContent: '', tagName: 'DIV', dataset: {},
    setAttribute(k, v){ this._attrs[k] = String(v); },
    getAttribute(k){ return k in this._attrs ? this._attrs[k] : null; },
    removeAttribute(k){ delete this._attrs[k]; },
    hasAttribute(k){ return k in this._attrs; },
    classList: {
      add(){}, remove(){}, contains(){ return false; }, toggle(){},
    },
    appendChild(c){ this.children.push(c); return c; },
    querySelector(){ return null; },
    querySelectorAll(){ return []; },
    addEventListener(t, fn){ (this.listeners[t] = this.listeners[t] || []).push(fn); },
    removeEventListener(){},
    closest(){ return null; },
    focus(){},
    remove(){},
  };
  return el;
}

// One controllable fetch: the test decides WHEN the response resolves.
let pending = null;
const context = {
  console,
  document: {
    getElementById(){ return null; },
    querySelector(){ return null; },
    querySelectorAll(){ return []; },
    createElement(){ return mkEl(); },
    body: mkEl(),
    activeElement: null,
    addEventListener(){},
  },
  location: { pathname: '/session/AAA' },
  window: {},
  navigator: { userAgent: 'node' },
  setTimeout, clearTimeout, setInterval, clearInterval, Date, Math, JSON,
  Promise, encodeURIComponent, decodeURIComponent, Number, String, Boolean,
  Array, Object, Error, isNaN, parseInt, parseFloat,
  S: {busy: false, activeStreamId: null, session: null},
  t(k){ return {a11y_run_working_elsewhere: 'Hermes is working in another window',
                a11y_run_started: 'Hermes is working'}[k] || k; },
  // The response is held open until the scenario resolves it, so the switch
  // happens WHILE the request is in flight - that is the whole point.
  fetch(){
    return new Promise((resolve) => { pending = resolve; });
  },
  __resolvePending(payload){
    if (!pending) throw new Error('no pending fetch');
    const r = pending; pending = null;
    r({ok: true, json: () => Promise.resolve(payload)});
  },
  __hasPending(){ return !!pending; },
};
context.globalThis = context;
context.window = context;
vm.createContext(context);
vm.runInContext(src, context);
const out = vm.runInContext(SCENARIO, context);
// Explicit exit: a11y-helpers.js arms setInterval timers at load, which keep the
// node event loop alive forever. The other harnesses in this repo are synchronous
// so they never noticed; this one awaits a promise, so without process.exit the
// run hangs until the pytest timeout (measured: exit 124).
Promise.resolve(out).then((v) => {
  console.log(JSON.stringify(v));
  process.exit(0);
}).catch((e) => {
  console.error(e && e.stack || String(e));
  process.exit(1);
});
"""


def _run(scenario):
    if not NODE:
        pytest.skip("node unavailable - cannot measure behaviour")
    script = (
        HARNESS
        .replace("SRC", json.dumps(str(REPO / "static" / "a11y-helpers.js")))
        .replace("SCENARIO", json.dumps(scenario))
    )
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, f"node failed: {r.stderr[-2000:]}"
    return json.loads(r.stdout)


SEC = "Math.floor(Date.now()/1000)"


class TestLatePollResponseIsDiscarded:
    """A response that arrives after the user left its session must be dropped."""

    def test_late_response_does_not_pollute_the_new_baseline(self):
        out = _run(f"""
        (async () => {{
          // Establish a baseline for session AAA, so the next growth would count.
          _a11yForeignSid = 'AAA';
          _a11yForeignLastStamp = {SEC} - 100;
          _a11yForeignLastGrowthAt = Date.now() - 1000;
          const before = _a11yForeignLastStamp;

          const p = _a11yForeignPoll();          // starts under AAA
          location.pathname = '/session/BBB';    // user switches mid-flight
          _a11yForeignSid = 'BBB';
          _a11yForeignLastStamp = null;          // switch resets the baseline
          _a11yForeignLastGrowthAt = null;
          __resolvePending({{session: {{last_message_at: {SEC}, last_activity_at: {SEC}}}}});
          await p;
          return {{stampAfter: _a11yForeignLastStamp, growthAfter: _a11yForeignLastGrowthAt,
                   before: before}};
        }})()
        """)
        assert out["stampAfter"] is None, (
            "a response belonging to the previous session must not set the new "
            f"conversation's baseline (got {out['stampAfter']!r})"
        )
        assert out["growthAfter"] is None, (
            "the growth timestamp must stay unset after switching conversations"
        )

    def test_late_response_does_not_announce_work_in_an_idle_conversation(self):
        out = _run(f"""
        (async () => {{
          let announced = 0;
          a11yAnnounce = () => {{ announced += 1; }};
          window.a11yAnnounce = a11yAnnounce;

          // AAA looks like it is progressing: baseline set, one growth just seen.
          _a11yForeignSid = 'AAA';
          _a11yForeignLastStamp = {SEC} - 5;
          _a11yForeignLastGrowthAt = Date.now() - 500;

          const p = _a11yForeignPoll();
          location.pathname = '/session/BBB';
          _a11yForeignSid = 'BBB';
          _a11yForeignLastStamp = null;
          _a11yForeignLastGrowthAt = null;
          _a11yForeignOwnsRun = false;
          __resolvePending({{session: {{last_message_at: {SEC}, last_activity_at: {SEC},
                                       last_activity_description: 'executing tool: terminal'}}}});
          await p;
          return {{ownsRun: _a11yForeignOwnsRun,
                   active: typeof a11yRunIsActive === 'function' ? a11yRunIsActive() : null,
                   announced: announced}};
        }})()
        """)
        assert out["ownsRun"] is False, (
            "a stale response must not make the watchdog claim the run state of a "
            "conversation the user just opened"
        )
        assert out["active"] is not True, (
            "an idle conversation must not be announced as working because the "
            "PREVIOUS conversation's poll resolved late"
        )

    def test_a_response_for_the_current_session_is_still_used(self):
        """Negative control: the guard must not discard legitimate responses.

        Without this case a fix that drops EVERY response would pass the two tests
        above while disabling the feature entirely.
        """
        out = _run(f"""
        (async () => {{
          _a11yForeignSid = 'AAA';
          _a11yForeignLastStamp = null;
          _a11yForeignLastGrowthAt = null;
          const p = _a11yForeignPoll();          // no switch this time
          __resolvePending({{session: {{last_message_at: {SEC}, last_activity_at: {SEC}}}}});
          await p;
          return {{stampAfter: _a11yForeignLastStamp}};
        }})()
        """)
        assert out["stampAfter"] is not None, (
            "a response for the CURRENT session must still establish the baseline - "
            "otherwise the ownership guard has disabled the watchdog"
        )
