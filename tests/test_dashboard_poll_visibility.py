"""Regression test — the dashboard status poll is skipped while the tab is hidden.

`_initDashboardLinkProbe` sets `setInterval(refreshDashboardStatus, 60000)`, and
the interval equals `DASHBOARD_STATUS_TTL_MS`, so every background tick was a
real `/api/dashboard/status` fetch that never hit the cache — a needless wakeup
on a tab nobody is looking at. It was the only frontend poll without a
`document.hidden` guard (the gateway / streaming / hidden-stream polls all skip
while hidden, #4704/#2476).

`refreshDashboardStatus` now short-circuits an unforced call while hidden, and
`_initDashboardLinkProbe` refreshes once on `visibilitychange` back to visible.
Forced calls (settings save, init, the catch-up) still run.
"""
import pathlib
import shutil
import subprocess
import tempfile
import textwrap

import pytest

REPO = pathlib.Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def test_guard_and_catchup_present():
    # Guard lives inside refreshDashboardStatus (keeps the setInterval anchor).
    assert "setInterval(refreshDashboardStatus,DASHBOARD_STATUS_TTL_MS)" in UI_JS
    assert "if(!force&&typeof document!=='undefined'&&document.hidden){" in UI_JS
    assert "visibilitychange" in UI_JS


_DRIVER = textwrap.dedent(
    """\
    const fs = require('fs');
    function extractFn(src, name) {
      const markers = [`async function ${name}(`, `function ${name}(`];
      let start = -1;
      for (const m of markers) { start = src.indexOf(m); if (start >= 0) break; }
      if (start < 0) throw new Error(`${name}() not found`);
      let i = src.indexOf('{', start), depth = 0, s = null, esc = false, lc = false, bc = false;
      for (; i < src.length; i++) {
        const ch = src[i], nx = src[i + 1] || '';
        if (lc) { if (ch === '\\n') lc = false; continue; }
        if (bc) { if (ch === '*' && nx === '/') bc = false; continue; }
        if (s) { if (esc) esc = false; else if (ch === '\\\\') esc = true; else if (ch === s) s = null; continue; }
        if (ch === '/' && nx === '/') { lc = true; continue; }
        if (ch === '/' && nx === '*') { bc = true; continue; }
        if (ch === '\\'' || ch === '"' || ch === '`') { s = ch; continue; }
        if (ch === '{') depth += 1;
        if (ch === '}') { depth -= 1; if (depth === 0) return src.slice(start, i + 1); }
      }
      throw new Error(`could not extract ${name}`);
    }

    const uiSrc = fs.readFileSync(process.argv[2], 'utf8');
    const hidden = process.argv[3] === 'hidden';
    let fetches = 0;
    global.document = { hidden };
    global._dashboardStatusCache = null;
    global._dashboardStatusFetchedAt = 0;
    global.DASHBOARD_STATUS_TTL_MS = 60000;
    global._applyDashboardStatus = () => {};
    global.api = (url) => { if (String(url) === '/api/dashboard/status') fetches += 1; return Promise.resolve({ running: true }); };
    eval(extractFn(uiSrc, 'refreshDashboardStatus'));

    (async () => {
      // Unforced call (what the interval does).
      await refreshDashboardStatus();
      // Forced call always runs regardless of visibility.
      await refreshDashboardStatus(true);
      console.log(JSON.stringify({ fetches }));
    })();
    """
)


def _run(hidden):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(_DRIVER)
        driver = f.name
    out = subprocess.run(
        [NODE, driver, str(REPO / "static" / "ui.js"), "hidden" if hidden else "visible"],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    import json
    return json.loads(out.stdout.strip())


@requires_node
def test_hidden_tab_skips_unforced_poll_but_forced_still_runs():
    r = _run(hidden=True)
    # Only the forced call fetched; the unforced (interval) call was skipped.
    assert r["fetches"] == 1, "hidden tab should skip the unforced poll"


@requires_node
def test_visible_tab_polls_normally():
    r = _run(hidden=False)
    # Both the unforced and forced calls fetched while visible.
    assert r["fetches"] == 2, "visible tab should poll on both calls"
