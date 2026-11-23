"""Authority of the periodic update check against the manual check (PR #6480).

The reviewer's re-gate of 2026-10-06 named two behaviours that the coalescing
harness of the PR does not cover, because they only exist when the boot-scope
scheduler and the settings-panel manual check run in the same page:

1. A delayed AUTOMATIC check overwrites a newer MANUAL check after a channel
   switch.  ``_showUpdateBanner()`` replaces ``window._updateData``
   (``static/ui.js``), and ``applyUpdates()`` sends that object's channel, so an
   automatic response that lands after the manual one makes the apply request
   use the channel the user already abandoned.
2. Turning "Check for updates" off does not stop the triggers: the boot
   snapshot ``_bootSettings`` never learns that the server answered
   ``{"disabled": true}``, so every focus/interval tick keeps POSTing for the
   life of the tab.

The driver below extracts the REAL ``_checkUpdates`` arrow from
``static/boot.js`` and the REAL ``checkUpdatesNow()`` from ``static/panels.js``
(``tests/js_source_extract.extract_function``) and composes them with deferred
HTTP responses.  Stubs exist only at UI seams (``$``, ``t``, ``document``,
``sessionStorage``, ``setInterval`` and ``_showUpdateBanner``, whose entire
production contract at the top is ``window._updateData=data``).
"""

import json
import pathlib
import subprocess
import shutil
import textwrap

import pytest

from tests.js_source_extract import extract_function

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
    const action = process.argv[3];

    // boot.js is browser code.  Both the model harness and the product read
    // browser globals (window._updateRecoveryGeneration, window._updateData).
    global.window = global;

    const store = {};
    global.sessionStorage = {
      getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
    };
    global.location = { search: '' };

    const calls = [];
    const posts = [];
    global.api = (url, opts = {}) => {
      const method = (opts.method || 'GET').toUpperCase();
      calls.push({ url: String(url), method, body: opts.body || '' });
      if (method !== 'POST') return Promise.resolve({});
      return new Promise((resolve) => {
        const p = { url: String(url), body: opts.body || '', resolve };
        posts.push(p);
        pending.push(p);
      });
    };
    const pending = [];
    const banners = [];
    global._showUpdateBanner = (d) => { window._updateData = d; banners.push(d); };
    global.document = { visibilityState: 'visible', addEventListener: () => {} };
    global.setInterval = () => 42;
    global.t = (k) => String(k);
    global.$ = () => ({ disabled: false, style: {}, textContent: '' });

    function balanced(src, braceIdx) {
      let depth = 0, inStr = null, esc = false, line = false, block = false;
      for (let i = braceIdx; i < src.length; i++) {
        const ch = src[i], nx = src[i + 1] || '';
        if (line) { if (ch === '\n') line = false; continue; }
        if (block) { if (ch === '*' && nx === '/') block = false; continue; }
        if (inStr) {
          if (esc) esc = false; else if (ch === '\\') esc = true; else if (ch === inStr) inStr = null;
          continue;
        }
        if (ch === '/' && nx === '/') { line = true; continue; }
        if (ch === '/' && nx === '*') { block = true; continue; }
        if (ch === "'" || ch === '"' || ch === '`') { inStr = ch; continue; }
        if (ch === '{') depth += 1;
        if (ch === '}') { depth -= 1; if (depth === 0) return i + 1; }
      }
      throw new Error('could not extract balanced block');
    }

    function arrowFn(src, name) {
      const marker = `const ${name}=`;
      const start = src.indexOf(marker);
      if (start < 0) throw new Error(`${name} not found`);
      const afterEq = start + marker.length;
      const brace = src.indexOf('{', afterEq);
      if (brace < 0) throw new Error(`${name} body not found`);
      return src.slice(afterEq, balanced(src, brace));
    }

    let _bootSettings = { check_for_updates: true };
    let _updateCheckInFlight = null;
    let _updateCheckTimer = null;
    const _checkUpdates = eval('(' + arrowFn(bootSrc, '_checkUpdates') + ')');
    eval(panelsSrc); // defines checkUpdatesNow() in this scope

    const flush = () => new Promise((r) => setTimeout(r, 0));
    const done = (extra) => {
      const data = window._updateData || {};
      const webui = data.webui || {};
      console.log(JSON.stringify(Object.assign({
        posts: posts.length,
        postBodies: posts.map((p) => p.body),
        bannerChannels: banners.map((b) => ((b && b.webui) ? b.webui.channel : null)),
        finalChannel: (webui.channel === undefined) ? null : webui.channel,
        finalBehind: (webui.behind === undefined) ? null : webui.behind,
      }, extra)));
    };

    (async () => {
      if (action === 'auto-then-manual' || action === 'auto-then-manual-reverse') {
        const reverse = action === 'auto-then-manual-reverse';
        const autoChannel = reverse ? 'stable' : 'experimental';
        const manualChannel = reverse ? 'experimental' : 'stable';
        _checkUpdates();                              // automatic, old channel
        const manual = checkUpdatesNow(manualChannel); // user switched channel
        pending[1].resolve({ webui: { behind: 2, channel: manualChannel }, agent: { behind: 0 } });
        await manual;
        await flush();
        const channelAfterManual = (window._updateData && window._updateData.webui)
          ? window._updateData.webui.channel : null;
        pending[0].resolve({ webui: { behind: 3, channel: autoChannel }, agent: { behind: 0 } });
        await flush();
        await flush();
        done({ action, manualChannel, channelAfterManual });
      } else if (action === 'focus-during-manual') {
        const manual = checkUpdatesNow('stable');
        const ret = _checkUpdates(); // focus tick while the manual check is in flight
        await flush();
        const postsDuringManual = posts.length;
        pending[0].resolve({ webui: { behind: 2, channel: 'stable' }, agent: { behind: 0 } });
        await manual;
        await flush();
        done({ action, postsDuringManual, autoReturnedNothing: ret === null || ret === undefined });
      } else if (action === 'disabled-stops-triggers') {
        _checkUpdates();
        pending[0].resolve({ disabled: true });
        await flush();
        await flush();
        const afterDisabled = posts.length;
        _checkUpdates();
        _checkUpdates();
        await flush();
        done({ action, postsRightAfterDisabled: afterDisabled });
      } else if (action === 'control-auto-renders') {
        _checkUpdates();
        pending[0].resolve({ webui: { behind: 1, channel: 'experimental' }, agent: { behind: 0 } });
        await flush();
        await flush();
        done({ action });
      } else if (action === 'control-manual-after-auto-settled') {
        _checkUpdates();
        pending[0].resolve({ webui: { behind: 0 }, agent: { behind: 0 } });
        await flush();
        await flush();
        const manual = checkUpdatesNow('stable');
        pending[1].resolve({ webui: { behind: 2, channel: 'stable' }, agent: { behind: 0 } });
        await manual;
        await flush();
        done({ action });
      } else {
        throw new Error(`unknown action ${action}`);
      }
    })().catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
    """
)


def _run(action: str, tmp_path: pathlib.Path):
    panels_fn = extract_function(
        PANELS_JS.read_text(encoding="utf-8"), "checkUpdatesNow", prefix="async function"
    )
    fn_path = tmp_path / "check_updates_now.js"
    fn_path.write_text(panels_fn, encoding="utf-8", newline="\n")
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(BOOT_JS), str(fn_path), action],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"node driver failed for action {action!r}:\n{proc.stderr}"
    return json.loads(proc.stdout)


@requires_node
class TestUpdateCheckAuthority:
    """The manual check owns the channel a later automatic response cannot revoke."""

    def test_late_automatic_experimental_does_not_overwrite_stable_manual(self, tmp_path):
        r = _run("auto-then-manual", tmp_path)
        assert r["channelAfterManual"] == "stable"
        assert r["finalChannel"] == "stable", (
            "an automatic check that started on the abandoned channel must not "
            f"replace the manual payload (got {r['finalChannel']!r}, "
            f"banner channels {r['bannerChannels']})"
        )
        assert r["bannerChannels"] == ["stable"], (
            "the stale automatic response must be discarded before _showUpdateBanner:"
            f" rendered channels were {r['bannerChannels']}"
        )
        body = json.loads(r["postBodies"][1])
        assert body.get("force") is True and body.get("channel") == "stable"

    def test_late_automatic_stable_does_not_overwrite_experimental_manual(self, tmp_path):
        r = _run("auto-then-manual-reverse", tmp_path)
        assert r["channelAfterManual"] == "experimental"
        assert r["finalChannel"] == "experimental", (
            "the reverse channel switch must be protected too "
            f"(got {r['finalChannel']!r})"
        )
        assert r["bannerChannels"] == ["experimental"]

    def test_automatic_trigger_does_not_start_while_a_manual_check_is_pending(self, tmp_path):
        r = _run("focus-during-manual", tmp_path)
        assert r["postsDuringManual"] == 1, (
            "a focus/interval tick must not POST while the manual check is in flight "
            f"(posts were {r['postsDuringManual']})"
        )
        assert r["posts"] == 1
        assert r["finalChannel"] == "stable"

    def test_disabled_response_stops_further_triggers(self, tmp_path):
        r = _run("disabled-stops-triggers", tmp_path)
        assert r["postsRightAfterDisabled"] == 1
        assert r["posts"] == 1, (
            "after the server answers disabled the scheduler must stop POSTing "
            f"(posts were {r['posts']})"
        )
        assert r["bannerChannels"] == []

    def test_control_automatic_check_still_renders_without_a_manual_check(self, tmp_path):
        r = _run("control-auto-renders", tmp_path)
        assert r["posts"] == 1
        assert r["bannerChannels"] == ["experimental"], (
            "a plain automatic check must still render its banner"
        )
        assert r["finalChannel"] == "experimental"

    def test_control_manual_check_after_settled_automatic_renders(self, tmp_path):
        r = _run("control-manual-after-auto-settled", tmp_path)
        assert r["posts"] == 2
        assert r["bannerChannels"] == ["stable"]
        assert r["finalChannel"] == "stable"
        assert r["finalBehind"] == 2
