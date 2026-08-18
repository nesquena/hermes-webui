"""Frontend lifecycle coverage for the periodic update-banner checker.

PR #6480 re-calls `static/boot.js::_checkUpdates()` on boot, on tab
`visibilitychange`, and on a 30-minute interval. Without a browser-side
in-flight owner those triggers can overlap and issue duplicate POSTs to
`api/updates/check`, each with its own response-side effects.

This module runs the REAL `_checkUpdates` arrow function (extracted from
boot.js) inside a node harness with a controllable `api()` mock and an
in-memory `sessionStorage`, proving the deferred-promise lifecycle:

- two (or three) triggers while one request is pending issue exactly one POST
  and share the same in-flight promise;
- dismiss-before-resolve does not render the banner (the post-response
  `hermes-update-dismissed` re-check is preserved);
- a trigger after settlement starts a new request (the in-flight token is
  cleared in `finally`);
- disabled and already-dismissed states issue no request;
- `?test_updates=1` keeps its separate GET simulation path (one GET, no POST,
  no listener/timer registration), while the normal boot path issues one POST
  and registers the visibilitychange listener plus the 30-minute timer.
"""

import json
import pathlib
import shutil
import subprocess
import textwrap

import pytest

REPO = pathlib.Path(__file__).parent.parent
BOOT_JS = REPO / "static" / "boot.js"
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# The driver extracts the real `_checkUpdates` arrow, the `_testUpdates`
# expression and the `if(_testUpdates){...}else{...}` trigger block from
# boot.js, then exercises them against a controllable api() mock. Each action
# returns a JSON summary that the Python side asserts on.
_DRIVER = textwrap.dedent(
    r"""
    const fs = require('fs');
    const bootSrc = fs.readFileSync(process.argv[1], 'utf8');
    const action = process.argv[2] || 'coalesce';

    function extractBalanced(src, braceIdx) {
      let depth = 0, inString = null, escaped = false, inLineComment = false, inBlockComment = false;
      for (let i = braceIdx; i < src.length; i++) {
        const ch = src[i], nxt = src[i + 1] || '';
        if (inLineComment) { if (ch === '\n') inLineComment = false; continue; }
        if (inBlockComment) { if (ch === '*' && nxt === '/') inBlockComment = false; continue; }
        if (inString) {
          if (escaped) escaped = false;
          else if (ch === '\\') escaped = true;
          else if (ch === inString) inString = null;
          continue;
        }
        if (ch === '/' && nxt === '/') { inLineComment = true; continue; }
        if (ch === '/' && nxt === '*') { inBlockComment = true; continue; }
        if (ch === "'" || ch === '"' || ch === '`') { inString = ch; continue; }
        if (ch === '{') depth += 1;
        if (ch === '}') { depth -= 1; if (depth === 0) return i + 1; }
      }
      throw new Error('could not extract balanced block');
    }

    function extractArrowFn(src, name) {
      const marker = `const ${name}=`;
      const start = src.indexOf(marker);
      if (start < 0) throw new Error(`${name}() not found`);
      const arrowStart = start + marker.length; // '()=>{...'
      const brace = src.indexOf('{', arrowStart);
      if (brace < 0) throw new Error(`${name}() body not found`);
      const end = extractBalanced(src, brace);
      return src.slice(arrowStart, end); // '()=>{...}'
    }

    function extractBlock(src, marker) {
      const start = src.indexOf(marker);
      if (start < 0) throw new Error(`block '${marker}' not found`);
      const brace = src.indexOf('{', start);
      if (brace < 0) throw new Error(`block '${marker}' has no brace`);
      let end = extractBalanced(src, brace);
      // Include a trailing else branch if present.
      const rest = src.slice(end);
      const elseMatch = rest.match(/^\s*else\s*\{/);
      if (elseMatch) {
        const elseBrace = end + elseMatch[0].lastIndexOf('{');
        end = extractBalanced(src, elseBrace);
      }
      return src.slice(start, end);
    }

    function extractConstLine(src, name) {
      const marker = `const ${name}=`;
      const start = src.indexOf(marker);
      if (start < 0) throw new Error(`${name} not found`);
      const semi = src.indexOf(';', start);
      if (semi < 0) throw new Error(`${name} line has no semicolon`);
      return src.slice(start + marker.length, semi);
    }

    const result = { calls: [], postCalls: 0, getCalls: 0, bannerCalls: 0, listeners: [], intervals: [], returnedNull: false, firstReturned: false, secondReturned: false, sharedPromise: false };
    const store = {};
    global.sessionStorage = {
      getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
    };
    global.location = { search: action === 'test-mode' ? '?test_updates=1' : '' };

    const pending = [];
    global.api = (url, opts = {}) => {
      const method = (opts.method || 'GET').toUpperCase();
      result.calls.push({ url: String(url), method, body: opts.body || '' });
      if (method === 'POST') {
        result.postCalls += 1;
        return new Promise((resolve) => { pending.push({ resolve, url: String(url) }); });
      }
      result.getCalls += 1;
      return Promise.resolve({ webui: { behind: 1 }, agent: { behind: 0 } });
    };
    global._showUpdateBanner = () => { result.bannerCalls += 1; };
    global.document = { addEventListener: (ev) => { result.listeners.push(ev); } };
    global.setInterval = (fn, ms) => { result.intervals.push(ms); return 42; };

    // Extract the REAL pieces from boot.js and evaluate them in this scope.
    const checkUpdatesSrc = extractArrowFn(bootSrc, '_checkUpdates');
    const testUpdatesRhs = extractConstLine(bootSrc, '_testUpdates');
    const bootBlock = extractBlock(bootSrc, 'if(_testUpdates){');

    let _bootSettings = { check_for_updates: true };
    let _updateCheckInFlight = null;
    const _checkUpdates = eval('(' + checkUpdatesSrc + ')');
    const _testUpdates = eval(testUpdatesRhs);

    const flush = () => new Promise((r) => setTimeout(r, 0));

    (async () => {
      if (action === 'coalesce') {
        const p1 = _checkUpdates();
        const p2 = _checkUpdates();
        const p3 = _checkUpdates();
        result.sharedPromise = p1 === p2 && p2 === p3;
        result.firstReturned = p1 !== null && p1 !== undefined;
        if (pending.length) pending[0].resolve({ webui: { behind: 1 }, agent: { behind: 0 } });
        await flush();
      } else if (action === 'dismiss-before-resolve') {
        const p1 = _checkUpdates();
        store['hermes-update-dismissed'] = '1'; // user dismisses while request is in flight
        if (pending.length) pending[0].resolve({ webui: { behind: 1 }, agent: { behind: 0 } });
        if (p1 && typeof p1.then === 'function') await p1;
        await flush();
      } else if (action === 'after-settlement') {
        const p1 = _checkUpdates();
        if (pending.length) pending[0].resolve({ webui: { behind: 0 }, agent: { behind: 0 } });
        if (p1 && typeof p1.then === 'function') await p1; // finally clears the token
        const p2 = _checkUpdates(); // new trigger after settlement
        result.secondReturned = p2 !== null && p2 !== undefined;
        if (pending.length) pending[pending.length - 1].resolve({ webui: { behind: 1 }, agent: { behind: 0 } });
        if (p2 && typeof p2.then === 'function') await p2;
        await flush();
      } else if (action === 'disabled') {
        _bootSettings.check_for_updates = false;
        const r1 = _checkUpdates();
        result.returnedNull = r1 === null || r1 === undefined;
      } else if (action === 'dismissed') {
        store['hermes-update-dismissed'] = '1';
        const r1 = _checkUpdates();
        result.returnedNull = r1 === null || r1 === undefined;
      } else if (action === 'test-mode' || action === 'boot-mode') {
        eval(bootBlock);
        if (action === 'boot-mode' && pending.length) {
          pending[0].resolve({ webui: { behind: 1 }, agent: { behind: 0 } });
          await flush();
        }
      }
      console.log(JSON.stringify(result));
    })().catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
    """
)


def _run(action):
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(BOOT_JS), action],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"node driver failed for action {action!r}:\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


@requires_node
class TestUpdateCheckCoalescing:
    """Deferred-promise lifecycle of the update banner scheduler (PR #6480)."""

    def test_two_triggers_while_one_request_pending_issue_one_post(self):
        r = _run("coalesce")
        assert r["postCalls"] == 1, "overlapping triggers must coalesce into one POST"
        assert r["getCalls"] == 0
        assert r["sharedPromise"] is True, "overlapping callers must share the in-flight promise"
        assert r["firstReturned"] is True
        assert len(r["calls"]) == 1
        assert r["calls"][0]["method"] == "POST"
        assert r["calls"][0]["url"] == "api/updates/check"

    def test_dismiss_before_resolve_does_not_render(self):
        r = _run("dismiss-before-resolve")
        assert r["postCalls"] == 1
        assert r["bannerCalls"] == 0, (
            "dismissing while a request is in flight must keep the banner hidden"
        )

    def test_trigger_after_settlement_starts_new_request(self):
        r = _run("after-settlement")
        assert r["postCalls"] == 2, (
            "after the in-flight promise settles, a later trigger must start a new request"
        )
        assert r["secondReturned"] is True

    def test_disabled_state_issues_no_request(self):
        r = _run("disabled")
        assert r["postCalls"] == 0
        assert r["calls"] == []
        assert r["returnedNull"] is True

    def test_already_dismissed_state_issues_no_request(self):
        r = _run("dismissed")
        assert r["postCalls"] == 0
        assert r["calls"] == []
        assert r["returnedNull"] is True

    def test_test_updates_mode_issues_one_simulation_get_and_no_post(self):
        r = _run("test-mode")
        assert r["getCalls"] == 1, "?test_updates=1 must issue exactly one simulation GET"
        assert r["postCalls"] == 0, "the simulation branch must never POST"
        assert r["calls"][0]["url"] == "api/updates/check?simulate=1"
        assert r["calls"][0]["method"] == "GET"
        assert r["listeners"] == [], "simulation mode must not register lifecycle triggers"
        assert r["intervals"] == []

    def test_normal_boot_registers_one_post_visibility_listener_and_timer(self):
        r = _run("boot-mode")
        assert r["postCalls"] == 1, "boot must run exactly one update check"
        assert r["getCalls"] == 0
        assert r["listeners"] == ["visibilitychange"]
        assert r["intervals"] == [1800000], "periodic re-check must stay at 30 minutes"
