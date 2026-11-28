"""Round-2 lifecycle controls for the update-banner scheduler (PR #6480).

The maintainer's re-review at ``49eb8e0f56c3`` reports three defects that all
reproduce through the real Settings and boot code:

1. re-enabling **Check for updates** never resumes the background checks -- the
   disabled response latches a private flag for the life of the tab;
2. the automatic POST keeps the timeout toast enabled, so a stalled background
   check shows "Request timed out. Please try again." for work the user never
   started;
3. the ``disabled`` latch is written **before** the generation check, so a stale
   automatic reply disables polling after a newer manual check already succeeded.

This module drives the REAL ``static/boot.js::_checkUpdates`` arrow, the REAL
trigger block and the REAL enable/disable interface (extracted from the file and
evaluated in a node harness with a controllable ``api()`` mock), plus the REAL
``static/panels.js::_autosavePreferencesSettings`` with a stubbed settings POST.

Coverage:

- off -> on: an explicit disable stops the triggers and the visibility callback
  issues no request, and an explicit enable resumes them on the next callback;
- a stale ``disabled`` reply after a newer manual success neither latches the
  flag nor renders the banner, and polling keeps working (control: the same
  reply *without* a newer manual check still latches);
- the automatic POST passes ``timeoutToast: false`` and keeps ``timeoutMs``
  at the 300 s budget;
- both Settings save paths hand the **server-confirmed** value to the scheduler
  interface (the optimistic payload must never win).
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import textwrap

import pytest

REPO = pathlib.Path(__file__).parent.parent
BOOT_JS = REPO / "static" / "boot.js"
PANELS_JS = REPO / "static" / "panels.js"
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DRIVER = textwrap.dedent(
    r"""
    const fs = require('fs');
    const bootSrc = fs.readFileSync(process.argv[1], 'utf8');
    const panelsSrc = fs.readFileSync(process.argv[2], 'utf8');
    const action = process.argv[3] || 'resume';

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
      if (start < 0) throw new Error(`${name} not found in boot.js`);
      const arrowStart = start + marker.length;
      const brace = src.indexOf('{', arrowStart);
      if (brace < 0) throw new Error(`${name} body not found`);
      const end = extractBalanced(src, brace);
      return src.slice(arrowStart, end);
    }

    function extractDecl(src, name) {
      // `const name=<expr>;` or `async function name(...) {...}` / `function name(...) {...}`
      for (const head of [`async function ${name}(`, `function ${name}(`]) {
        const at = src.indexOf(head);
        if (at >= 0) {
          const brace = src.indexOf('{', at);
          const end = extractBalanced(src, brace);
          return src.slice(at, end);
        }
      }
      const marker = `const ${name}=`;
      const start = src.indexOf(marker);
      if (start < 0) throw new Error(`${name} not found`);
      const arrowStart = start + marker.length;
      const brace = src.indexOf('{', arrowStart);
      if (brace < 0) throw new Error(`${name} body not found`);
      const end = extractBalanced(src, brace);
      return src.slice(arrowStart, end);
    }

    function extractBlock(src, marker) {
      const start = src.indexOf(marker);
      if (start < 0) throw new Error(`block '${marker}' not found`);
      const brace = src.indexOf('{', start);
      if (brace < 0) throw new Error(`block '${marker}' has no brace`);
      let end = extractBalanced(src, brace);
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
      return src.slice(start + marker.length, semi);
    }

    const result = {
      calls: [], postCalls: 0, getCalls: 0, bannerCalls: 0,
      listeners: [], intervals: [], clearedIntervals: [], interfacePresent: false,
      latchedOff: null, reported: [], threw: null,
      saveSettingsCall: null, xpositions: null,
    };

    // ---------------------------------------------------------------- boot ---
    global.window = global;
    const store = {};
    global.sessionStorage = {
      getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
    };
    global.location = { search: '' };

    const handlers = {};
    const pending = [];
    global.api = (url, opts = {}) => {
      const method = (opts.method || 'GET').toUpperCase();
      result.calls.push({
        url: String(url), method, body: opts.body || '',
        timeoutMs: opts.timeoutMs, timeoutToast: opts.timeoutToast,
      });
      if (method === 'POST') {
        result.postCalls += 1;
        return new Promise((resolve) => { pending.push({ resolve, url: String(url) }); });
      }
      result.getCalls += 1;
      return Promise.resolve({ webui: { behind: 1 }, agent: { behind: 0 } });
    };
    global._showUpdateBanner = () => { result.bannerCalls += 1; };
    global.document = {
      addEventListener: (ev, fn) => { result.listeners.push(ev); (handlers[ev] = handlers[ev] || []).push(fn); },
      removeEventListener: (ev, fn) => {
        const list = handlers[ev] || [];
        const i = list.indexOf(fn);
        if (i >= 0) list.splice(i, 1);
      },
    };
    createdIntervals = [];
    global.setInterval = (fn, ms) => { result.intervals.push(ms); const id = 40 + result.intervals.length; createdIntervals.push({ id, fn }); return id; };
    global.clearInterval = (id) => { result.clearedIntervals.push(id); };

    let _bootSettings = { check_for_updates: true };
    let _updateCheckInFlight = null;
    let _updateCheckTimer = null;

    const flush = () => new Promise((r) => setTimeout(r, 0));
    const visibilityHandlers = () => (handlers['visibilitychange'] || []).slice();
    const intervalHandlers = () => (createdIntervals || []).map((e) => e.fn);

    let _checkUpdates = null;
    let _testUpdates = null;
    let _setUpdateCheckEnabled = () => {};

    function loadBoot() {
      const checkUpdatesSrc = extractArrowFn(bootSrc, '_checkUpdates');
      const testUpdatesRhs = extractConstLine(bootSrc, '_testUpdates');
      const bootBlock = extractBlock(bootSrc, 'if(_testUpdates){');
      let ifaceOk = false;
      try {
        const ifaceSrc = extractDecl(bootSrc, '_setUpdateCheckEnabled');
        _setUpdateCheckEnabled = eval('(' + ifaceSrc + ')');
        ifaceOk = typeof _setUpdateCheckEnabled === 'function'
          && typeof window._setUpdateCheckEnabled === 'function';
      } catch (e) {
        ifaceOk = false;
      }
      // Module-scope bindings: the eval'd trigger block resolves _checkUpdates
      // and _testUpdates lexically from here, exactly like inside the boot IIFE.
      _checkUpdates = eval('(' + checkUpdatesSrc + ')');
      _testUpdates = eval(testUpdatesRhs);
      result.interfacePresent = ifaceOk;
      return { _checkUpdates, _testUpdates, bootBlock, _setUpdateCheckEnabled };
    }

    (async () => {
      if (!panelsSrc.startsWith('SKIP')) {
        // Reserved for wiring actions below; boot actions ignore it.
      }
      if (action === 'resume') {
        const b = loadBoot();
        eval(b.bootBlock);                       // boot: one POST, listener + timer
        if (pending.length) pending[0].resolve({ webui: { behind: 0 }, agent: { behind: 0 } });
        await flush();
        result.postsAfterBoot = result.postCalls;

        b._setUpdateCheckEnabled(false);         // user turns "Check for updates" off
        result.latchedOff = _bootSettings.check_for_updates === false;
        const staleHandlers = visibilityHandlers();
        staleHandlers.forEach((fn) => { try { fn(); } catch (e) {} });
        intervalHandlers().forEach((fn) => { try { fn(); } catch (e) {} });
        await flush();
        result.postsWhileDisabled = result.postCalls;

        b._setUpdateCheckEnabled(true);          // user turns it back on
        const freshHandlers = visibilityHandlers();
        result.handlersAfterEnable = freshHandlers.length;
        freshHandlers.forEach((fn) => { try { fn(); } catch (e) {} });
        (createdIntervals || []).forEach((e) => { try { e.fn(); } catch (err) {} });
        await flush();
        if (pending.length) pending[pending.length - 1].resolve({ webui: { behind: 1 }, agent: { behind: 0 } });
        await flush();
        result.postsAfterEnable = result.postCalls;
        result.bannerAfterEnable = result.bannerCalls;
      } else if (action === 'stale-disabled') {
        const b = loadBoot();
        eval(b.bootBlock);                       // automatic request in flight (generation 0)
        // A manual check starts while the automatic one is pending: this is the
        // bookkeeping checkUpdatesNow() performs on the shared owner object.
        window._updateCheckOwner.generation = (window._updateCheckOwner.generation || 0) + 1;
        window._updateCheckOwner.manualPending = (window._updateCheckOwner.manualPending || 0) + 1;
        if (pending.length) pending[0].resolve({ disabled: true, webui: { behind: 1 }, agent: { behind: 0 } });
        await flush();
        result.latchedOff = _bootSettings.check_for_updates === false;
        result.banner = result.bannerCalls;
        window._updateCheckOwner.manualPending = 0;   // manual check settled
        b._checkUpdates();
        await flush();
        result.postsAfterStaleReply = result.postCalls;
      } else if (action === 'fresh-disabled') {
        const b = loadBoot();
        eval(b.bootBlock);
        if (pending.length) pending[0].resolve({ disabled: true, webui: { behind: 0 }, agent: { behind: 0 } });
        await flush();
        result.latchedOff = _bootSettings.check_for_updates === false;
        b._checkUpdates();
        await flush();
        result.postsAfterFreshReply = result.postCalls;
      } else if (action === 'automatic-options') {
        const b = loadBoot();
        eval(b.bootBlock);
      } else if (action === 'autosave') {
        const fnSrc = extractDecl(panelsSrc, '_autosavePreferencesSettings');
        const fixture = JSON.parse(process.argv[4] || '{}');
        // Browser globals the real autosave body reads. Bare identifiers have to
        // be declared here; the `typeof x === 'function'` guards skip the rest.
        let _settingsPreferencesAutosaveRetryPayload = null;
        let _settingsDirty = true;
        let _settingsHermesDefaultModelOnOpen = '';
        let _settingsHermesDefaultModelProviderOnOpen = null;
        const _setPreferencesAutosaveStatus = () => {};
        const setScreenReaderMode = () => {};
        const $ = () => null;
        global.document = { documentElement: { dataset: {} } };
        global.console = console;
        const _enqueueSettingsPost = async () => fixture.saved;
        window._setUpdateCheckEnabled = (v) => { result.reported.push(v); };
        const _autosave = eval('(' + fnSrc + ')');
        await _autosave(fixture.payload);
        await flush();
      } else if (action === 'save-settings-wiring') {
        const start = panelsSrc.indexOf('async function saveSettings(');
        const tail = panelsSrc.slice(start + 1);
        const nextFn = tail.search(/\n(?:async )?function /);
        const slice = panelsSrc.slice(start, nextFn > 0 ? start + 1 + nextFn : panelsSrc.length);
        const m = slice.match(/_setUpdateCheckEnabled\s*\(([^;\n]*(?:\n[^;\n]*)?)\)/);
        result.saveSettingsCall = m ? m[1] : null;
        result.saveSettingsHasCall = slice.indexOf('_setUpdateCheckEnabled') >= 0;
      } else {
        throw new Error('unknown action ' + action);
      }
      console.log(JSON.stringify(result));
    })().catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
    """
)


def _run(action, fixture=None):
    env = dict(os.environ)
    if fixture is not None:
        env["HERMES_6480_FIXTURE"] = json.dumps(fixture)
    script = _DRIVER.replace(
        "JSON.parse(process.argv[4] || '{}')",
        "JSON.parse(process.env.HERMES_6480_FIXTURE || '{}')",
    )
    proc = subprocess.run(
        [NODE, "-e", script, str(BOOT_JS), str(PANELS_JS), action],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, (
        f"node driver failed for action {action!r}:\n{proc.stdout}\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


@requires_node
class TestSchedulerEnableDisable:
    """Turning checks off must stop them; turning them on must resume them."""

    def test_disabled_then_enabled_resumes_on_the_next_callback(self):
        r = _run("resume")
        assert r["interfacePresent"] is True, (
            "boot.js must expose a scheduler enable/disable interface on window"
        )
        assert r["postsAfterBoot"] == 1, "boot must still run exactly one check"
        assert r["latchedOff"] is True, "disabling must short-circuit further triggers"
        assert r["postsWhileDisabled"] == 1, (
            "a disabled scheduler must issue no request, including from the "
            "previously registered lifecycle callbacks"
        )
        assert r["handlersAfterEnable"] >= 1, (
            "re-enabling must register the visibility callback again"
        )
        assert r["postsAfterEnable"] == 2, (
            "re-enabling update checks must resume the background checks on the "
            "next lifecycle callback; got %r POSTs" % (r["postsAfterEnable"],)
        )
        assert r["bannerAfterEnable"] == 1, (
            "the resumed check must render the banner for a behind>0 payload"
        )


@requires_node
class TestStaleDisabledReply:
    """A stale ``disabled`` reply must not override newer state."""

    def test_stale_disabled_reply_after_manual_success_does_not_disable(self):
        r = _run("stale-disabled")
        assert r["latchedOff"] is False, (
            "a stale automatic 'disabled' reply must not latch the scheduler off "
            "after a newer manual check succeeded"
        )
        assert r["banner"] == 0, "the stale reply must render nothing"
        assert r["postsAfterStaleReply"] == 2, (
            "polling must keep working after the stale reply is discarded"
        )

    def test_control_fresh_disabled_reply_still_disables(self):
        r = _run("fresh-disabled")
        assert r["latchedOff"] is True, (
            "a 'disabled' reply with no newer manual check must still latch off"
        )
        assert r["postsAfterFreshReply"] == 1, (
            "a latched scheduler must issue no further requests"
        )


@requires_node
class TestAutomaticRequestOptions:
    def test_automatic_post_suppresses_the_timeout_toast(self):
        r = _run("automatic-options")
        assert r["postCalls"] == 1
        opts = r["calls"][0]
        assert opts["url"] == "api/updates/check"
        assert opts["timeoutToast"] is False, (
            "the automatic check must pass timeoutToast:false so a stalled "
            "background request shows no error toast"
        )
        assert opts["timeoutMs"] == 300000, (
            "the automatic check must keep the 300 s budget"
        )


@requires_node
class TestSettingsSaveWiring:
    """Both Settings save paths hand the confirmed value to the scheduler."""

    def test_autosave_applies_the_confirmed_value_not_the_payload(self):
        r = _run(
            "autosave",
            {"payload": {"check_for_updates": True}, "saved": {"check_for_updates": False}},
        )
        assert r["reported"] == [False], (
            "the autosave path must apply the server-confirmed value (False), "
            "not the optimistic payload (True); got %r" % (r["reported"],)
        )

    def test_autosave_resumes_when_the_server_confirms_enabled(self):
        r = _run(
            "autosave",
            {"payload": {"check_for_updates": False}, "saved": {"check_for_updates": True}},
        )
        assert r["reported"] == [True], (
            "the autosave path must resume checks when the server confirms the "
            "flag is on; got %r" % (r["reported"],)
        )

    def test_control_autosave_without_the_field_leaves_the_scheduler_alone(self):
        r = _run(
            "autosave",
            {"payload": {"show_tps": True}, "saved": {"show_tps": True}},
        )
        assert r["reported"] == [], (
            "an autosave that does not carry check_for_updates must not touch "
            "the scheduler; got %r" % (r["reported"],)
        )

    def test_explicit_save_settings_applies_the_confirmed_value(self):
        r = _run("save-settings-wiring")
        assert r["saveSettingsHasCall"] is True, (
            "saveSettings() must apply the saved check_for_updates value to the "
            "scheduler (the explicit Save path must not leave it latched)"
        )
        assert r["saveSettingsCall"] is not None and re.search(r"\bsaved\b", r["saveSettingsCall"]), (
            "saveSettings() must pass the server-confirmed value, not the "
            "request body; call argument was %r" % (r["saveSettingsCall"],)
        )
