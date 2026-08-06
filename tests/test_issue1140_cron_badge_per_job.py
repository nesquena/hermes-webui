"""Tests for issue #1140 — Cron completion badge per-job indicator.

Verifies that:
1. _cronNewJobIds tracks job IDs with new completions
2. loadCrons() renders a dot indicator for new-run jobs
3. openCronDetail() clears the unread state for the viewed job
4. Badge only clears when all unread jobs are viewed (not on panel open)
5. _renderCronDetail() adds has-new-run class to Last Output card
6. loadCrons() bails on a superseded (stale) response (#6767 P1)
"""

import pytest
from pathlib import Path

# ── Static file tests (no server needed) ──

def test_cron_new_job_ids_tracking_in_panels_js():
    """panels.js should declare _cronNewJobIds Set and populate it in startCronPolling."""
    with open('static/panels.js') as f:
        src = f.read()

    # _cronNewJobIds declared as Set
    assert '_cronNewJobIds' in src, '_cronNewJobIds not found in panels.js'
    assert 'new Set()' in src, '_cronNewJobIds should be initialized as Set()'

    # In startCronPolling, job IDs are added to the set
    assert '_cronNewJobIds.add(String(c.job_id))' in src, \
        'startCronPolling should add job_id to _cronNewJobIds'


def test_cron_dot_indicator_rendered_in_load_crons():
    """loadCrons() should render a .cron-new-dot for jobs in _cronNewJobIds."""
    with open('static/panels.js') as f:
        src = f.read()

    # Dot indicator in cron-item rendering
    assert 'cron-new-dot' in src, 'cron-new-dot class not found'
    assert "_cronNewJobIds.has(String(job.id))" in src, \
        'loadCrons should check _cronNewJobIds for each job'


def test_open_cron_detail_clears_unread():
    """openCronDetail() should mark job as read and remove the dot."""
    with open('static/panels.js') as f:
        src = f.read()

    # _clearCronUnreadForJob called in openCronDetail
    assert '_clearCronUnreadForJob' in src, \
        '_clearCronUnreadForJob function not found'
    # Dot removal in openCronDetail
    assert "target.querySelector('.cron-new-dot')" in src, \
        'openCronDetail should remove the dot element'


def test_clear_cron_unread_for_job_function():
    """_clearCronUnreadForJob should delete from set and refresh badge.

    _cronUnreadCount is derived from _cronNewJobIds.size in updateCronBadge,
    so the function only needs to delete from the set and trigger a badge sync.
    """
    with open('static/panels.js') as f:
        src = f.read()

    # Locate the function body to make assertions order-dependent
    start = src.find('function _clearCronUnreadForJob(')
    assert start != -1, '_clearCronUnreadForJob should be defined'
    body = src[start:start + 400]
    assert '_cronNewJobIds.delete(id)' in body, \
        '_clearCronUnreadForJob should delete from _cronNewJobIds'
    assert 'updateCronBadge()' in body, \
        '_clearCronUnreadForJob should call updateCronBadge to re-sync count'


def test_switch_panel_no_longer_clears_badge():
    """switchPanel override should NOT clear badge on tasks panel open."""
    with open('static/panels.js') as f:
        src = f.read()

    # The old pattern "if(name==='tasks'){_cronUnreadCount=0" should NOT exist
    assert "if(name==='tasks'){_cronUnreadCount=0" not in src, \
        'switchPanel should NOT clear _cronUnreadCount on tasks open'


def test_has_new_run_class_in_render_detail():
    """_renderCronDetail() should add has-new-run class to Last Output card."""
    with open('static/panels.js') as f:
        src = f.read()

    # Check has-new-run class in the cronDetailRuns div
    assert 'has-new-run' in src, 'has-new-run class not found'


def test_cron_css_classes_exist():
    """style.css should contain .cron-new-dot and .has-new-run styles."""
    with open('static/style.css') as f:
        src = f.read()

    assert '.cron-new-dot{' in src, '.cron-new-dot CSS rule not found'
    assert '.has-new-run{' in src, '.has-new-run CSS rule not found'
    assert 'cron-dot-pulse' in src, 'cron-dot-pulse animation not found'


def test_cron_unread_markers_share_badge_red():
    """The new-run dot and the Last Output highlight must match the rail badge red (#e53e3e).

    #1140 regression: the dot used the green --success colour, so an unread job
    read as "success" instead of "unread". Both markers must be the same red as
    the sidebar .cron-badge so the per-task indicator visually matches the count.
    """
    with open('static/style.css') as f:
        src = f.read()

    RED = '#e53e3e'

    def rule_body(selector):
        start = src.find(selector + '{')
        assert start != -1, f'{selector} CSS rule not found'
        end = src.find('}', start)
        return src[start:end]

    dot = rule_body('.cron-new-dot')
    has_new_run = rule_body('.has-new-run')
    badge = rule_body('.cron-badge')

    # Both unread markers explicitly use the same red as the rail badge,
    # not a --success fallback.
    assert RED in dot, f'.cron-new-dot should use {RED} in rule: {dot}'
    assert RED in has_new_run, f'.has-new-run should use {RED} in rule: {has_new_run}'
    # The red is hard-coded (not the --success variable that used to leak in).
    assert '--success' not in dot, '.cron-new-dot must not fall back to --success green'
    assert '--success' not in has_new_run, '.has-new-run must not fall back to --success green'
    # Sanity: the badge this matches against really is the same red.
    assert RED in badge, f'.cron-badge should use {RED} in rule: {badge}'


# ── #6767 P1 stale-refresh guard (node harness) ──────────────────────────────
# Two overlapping loadCrons() calls must not let the older response clobber the
# newer one. We stub api() so /api/crons resolves on demand, fire two loads, and
# resolve the FIRST one's /api/crons LAST. If the generation guard works, the
# older (superseded) load bails before touching the DOM, and only the newer
# response's job list is applied.

PANELS_JS_PATH = Path(__file__).resolve().parents[1] / "static" / "panels.js"
import subprocess  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402

_NODE = shutil.which("node")

@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
def test_cron_load_stale_response_does_not_overwrite_newer():
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(PANELS_JS_PATH))}, 'utf8');
function extractFunc(name) {{
  const re = new RegExp('(async\\\\s+)?function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
// Minimal DOM / deps that loadCrons touches.
let _cronList = [];
let _cronOtherProfileCount = 0;
let _showAllCronProfiles = false;
const _cronNewJobIds = new Set();
let _currentCronDetail = null;
let _currentCronDetailKey = null;
let _cronMode = null;
let appliedList = null;   // what the LAST load actually wrote
let resolves = [];        // resolver for each /api/crons call, in order
const pending = [];       // deferred for each api call
function makeDeferred() {{
  let resolve; const p = new Promise(r => {{ resolve = r; }});
  pending.push({{ resolve }});
  return p;
}}
function loadCronProfiles() {{ return Promise.resolve(); }}
function loadCronGatewayNotice() {{}}
function api(url) {{ return makeDeferred(); }}
function $(id) {{
  if (id === 'cronList') return {{ innerHTML: '', appendChild() {{}}, style: {{}} }};
  if (id === 'cronRefreshBtn') return null;
  return null;
}}
function esc(s) {{ return String(s); }}
function t(s) {{ return s; }}
function _cronStatusMeta(job) {{ return {{ state: 'active', label: 'on', listClass: 'active' }}; }}
function _cronItemId(job) {{ return 'cron-' + job.id; }}
function _cronJobKey(job) {{ return String(job.id); }}
function _cronProfileLabel(p) {{ return p; }}
function _cronOwnerProfileName(job) {{ return 'default'; }}
function _appendCronProfileToggle(box) {{}}
function _clearCronDetail() {{}}
function _renderCronDetail(job) {{}}
let _cronLoadGeneration = 0;
eval(extractFunc('loadCrons'));
(async () => {{
  // Fire load #1 and load #2 without awaiting either.
  const p1 = loadCrons();
  const p2 = loadCrons();
  // Both suspend at await loadCronProfiles(); yield until each has reached its
  // api() call so pending[0] and pending[1] exist (p1 then p2, in order).
  while (pending.length < 2) await Promise.resolve();
  const d1 = pending[0];
  const d2 = pending[1];
  // Resolve the NEWER request first and let it fully apply, then resolve the
  // OLDER one last — exactly the race Greptile flagged.
  d2.resolve({{ jobs: [{{ id: 'newer', name: 'Newer' }}] }});
  await p2;                       // newer load applies -> _cronList = ['newer']
  d1.resolve({{ jobs: [{{ id: 'stale', name: 'Stale' }}] }});
  await p1;                       // older load must bail before applying
  console.log(JSON.stringify({{
    list: Array.isArray(_cronList) ? _cronList.map(j => j.id) : _cronList,
    loadGen: _cronLoadGeneration,
  }}));
}})().catch(err => {{ console.error(err); process.exit(1); }});
"""
    result = subprocess.run([_NODE, "-e", script], check=True,
                            capture_output=True, text=True, timeout=30)
    payload = json.loads(result.stdout)
    # The OLDER response (stale) resolved last, but must NOT have been applied —
    # only the newer render (job 'newer') should be in the list.
    assert payload["list"] == ["newer"], \
        f"stale response overwrote newer view; list={payload['list']}"
    # Both loads ran, so the generation token advanced by 2.
    assert payload["loadGen"] == 2, \
        f"expected generation to advance to 2, got {payload['loadGen']}"

