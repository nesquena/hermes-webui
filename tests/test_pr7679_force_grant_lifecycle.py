"""Tests for #7679 lifecycle repairs — force-grant supersession (finding 1)
and check-publication ownership (finding 2).

These compose the REAL producers (checkUpdatesNow / _showUpdateBanner /
_showUpdateError / forceUpdate / applyUpdates) with click-and-POST harnesses,
so a stale authority can actually be exercised rather than merely asserted.

Finding 1: an open Force confirmation must not authorize a destructive
/api/updates/force POST after a newer check, a channel change, or an Apply
retry supersedes the check/apply that armed it.

Finding 2: an older check resolving after a newer one began must not overwrite
the banner/status the newer check published (latest-owner lifecycle).
"""

import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding='utf-8')


def extract_js_function(src, name):
    match = re.search(rf'(async\s+)?function\s+{re.escape(name)}\b', src)
    assert match, f'{name}() not found'
    open_paren = src.index('(', match.start())
    paren_depth = 1
    idx = open_paren + 1
    while paren_depth > 0 and idx < len(src):
        ch = src[idx]
        if ch == '(':
            paren_depth += 1
        elif ch == ')':
            paren_depth -= 1
        idx += 1
    brace = src.index('{', idx)
    depth = 0
    end = None
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    assert end is not None, f'{name}() body was not balanced'
    return src[match.start():end]


def _grant_block(src):
    start = src.index('let _updateCheckEpoch = 0;')
    # The banner signature gained a second counter (recoveryGenerationAtCheck)
    # when #8040's manual-update guard was merged in — anchor on the function
    # NAME, not the exact parameter list, so this keeps working.
    end = src.index('function _showUpdateBanner(data,')
    return src[start:end]


ui = read('static/ui.js')
panels = read('static/panels.js')

GRANT = _grant_block(ui)
FN_FORMAT = extract_js_function(ui, '_formatUpdateTargetStatus')
FN_INSTRUCTION = extract_js_function(ui, '_formatManualUpdateInstruction')
FN_ERROR = extract_js_function(ui, '_formatUpdateCheckError')
FN_PREDICATE = extract_js_function(ui, '_isForceCleanTarget')
FN_DIRTY = extract_js_function(ui, '_formatUpdateDirtyStatus')
FN_SHOW = extract_js_function(ui, '_showUpdateBanner')
FN_SHOW_ERROR = extract_js_function(ui, '_showUpdateError')
FN_APPLY = extract_js_function(ui, 'applyUpdates')
FN_FORCE = extract_js_function(ui, 'forceUpdate')
FN_CHECK = extract_js_function(panels, 'checkUpdatesNow')

IO_T = """
function t(key, fallback, ...args) {
  const values = {
    update_dirty_local_changes: 'Local changes detected',
    update_force: 'Force update',
    force_no_longer_applicable: 'Force update is no longer applicable. Please check again.',
    settings_update_manual_docker: 'Manual update required: run {0}, then recreate the container.',
  };
  return ((values[key] || fallback || key) + '').replace(/\\{(\\d+)\\}/g, (_, i) => args[Number(i)] ?? '');
}
"""

COMMON = """
global.window = {};
global.document = { baseURI: 'http://127.0.0.1:8788/' };
global.showToast = () => {};
global.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global._renderUpdateWhatsNewLinks = () => {};
global._readHealthServerIdentity = async () => null;
global._waitForServerThenReload = () => {};
global.showConfirmDialog = () => Promise.resolve(true);
global.__posted = [];
global.api = async (url, opts) => {
  global.__posted.push({ url: url, body: opts && opts.body ? JSON.parse(opts.body) : null });
  return { ok: true, restart_scheduled: true };
};
"""

BANNER_STATE = """
global.__state = {
  updateBanner: { classList: { added: false, removed: false, add() { this.added = true; }, remove() { this.removed = true; } } },
  updateMsg: { textContent: '' },
  updateError: { textContent: '', style: { display: 'none' } },
  btnApplyUpdate: { disabled: false, style: { display: '' } },
  btnForceUpdate: { disabled: false, style: { display: 'none' }, dataset: { target: '' } },
  btnClearUpdateLock: { disabled: false, style: { display: 'none' }, dataset: { target: '' } },
  updateWhatsNewLinks: { style: { display: 'none' }, replaceChildren() { this.cleared = true; } },
};
global.$ = (id) => global.__state[id] || null;
"""

UI_FNS = (
    GRANT + FN_FORMAT + FN_INSTRUCTION + FN_ERROR + FN_PREDICATE + FN_DIRTY
    + FN_SHOW + FN_SHOW_ERROR + FN_APPLY + FN_FORCE
)


def _run(node_source):
    subprocess.run(['node', '-e', node_source], check=True, capture_output=True, text=True)


def _force_harness(scenario):
    return BANNER_STATE + COMMON + IO_T + UI_FNS + scenario


# ── Finding 1: Force grant is retired on supersession ───────────────────────


def test_open_force_confirm_loses_authority_on_newer_clean_check():
    body = r"""
_showUpdateBanner({ webui: { dirty: true, behind: 0, channel: 'stable' }, agent: null });
if (!_forceUpdateGrant || _forceUpdateGrant.target !== 'webui') throw new Error('no grant after dirty render');
let resolveConfirm;
global.showConfirmDialog = () => new Promise((r) => { resolveConfirm = r; });
const btn = { dataset: { target: 'webui' } };
const pending = forceUpdate(btn);
// A NEWER clean stable check supersedes the grant while the confirm is open.
_beginUpdateCheck();
_showUpdateBanner({ webui: { dirty: false, behind: 0 }, agent: null });
resolveConfirm(true);
(async () => {
  await pending;
  const forcePosts = global.__posted.filter((p) => p.url === '/api/updates/force');
  if (forcePosts.length !== 0) throw new Error('stale grant must NOT post /api/updates/force: ' + JSON.stringify(forcePosts));
})().catch((err) => { console.error(err.stack || err.message); process.exit(1); });
"""
    _run(_force_harness(body))


def test_open_force_confirm_loses_authority_on_channel_change():
    body = r"""
_showUpdateBanner({ webui: { dirty: true, behind: 0, channel: 'stable' }, agent: null });
if (!_forceUpdateGrant || _forceUpdateGrant.channel !== 'stable') throw new Error('grant should start stable');
let resolveConfirm;
global.showConfirmDialog = () => new Promise((r) => { resolveConfirm = r; });
const btn = { dataset: { target: 'webui' } };
const pending = forceUpdate(btn);
// The operator switches to Experimental: a newer experimental check re-arms a
// DIFFERENT grant while the stable confirm is open.
_beginUpdateCheck();
_showUpdateBanner({ webui: { dirty: true, behind: 0, channel: 'experimental' }, agent: null });
resolveConfirm(true);
(async () => {
  await pending;
  const forcePosts = global.__posted.filter((p) => p.url === '/api/updates/force');
  if (forcePosts.length !== 0) throw new Error('superseded grant must not dispatch force: ' + JSON.stringify(forcePosts));
})().catch((err) => { console.error(err.stack || err.message); process.exit(1); });
"""
    _run(_force_harness(body))


def test_open_force_confirm_loses_authority_on_apply_retry():
    body = r"""
window._updateData = { webui: { behind: 3, channel: 'stable' } };
_showUpdateBanner({ webui: { dirty: false, behind: 3, channel: 'stable' }, agent: null });
_showUpdateError('webui', { ok: false, conflict: true, message: 'merge conflict' });
if (!_forceUpdateGrant || _forceUpdateGrant.target !== 'webui') throw new Error('conflict must grant webui');
let resolve;
global.showConfirmDialog = () => new Promise((r) => { resolve = r; });
global.__posted = [];
global.api = async (url) => (url === '/api/updates/apply' ? { ok: false, lock_conflict: true, message: 'stale lock' } : { ok: true });
const btn = { dataset: { target: 'webui' } };
const forceP = forceUpdate(btn);
(async () => {
  // While the confirm is open, the user retries Apply -> lock-only failure.
  await applyUpdates();
  resolve(true);
  await forceP;
  const forcePosts = global.__posted.filter((p) => p.url === '/api/updates/force');
  if (forcePosts.length !== 0) throw new Error('apply retry must retire the force grant, got: ' + JSON.stringify(forcePosts));
})().catch((err) => { console.error(err.stack || err.message); process.exit(1); });
"""
    _run(_force_harness(body))


def test_conflict_force_recovery_preserved_when_not_superseded():
    body = r"""
window._updateData = { webui: { behind: 3, channel: 'stable' } };
_showUpdateBanner({ webui: { dirty: false, behind: 3, channel: 'stable' }, agent: null });
_showUpdateError('webui', { ok: false, conflict: true, message: 'merge conflict' });
if (!_forceUpdateGrant || _forceUpdateGrant.target !== 'webui' || _forceUpdateGrant.channel !== 'stable') throw new Error('conflict must grant webui/stable');
global.__posted = [];
const btn = { dataset: { target: 'webui' } };
(async () => {
  await forceUpdate(btn);
  const forcePosts = global.__posted.filter((p) => p.url === '/api/updates/force');
  if (forcePosts.length !== 1) throw new Error('expected exactly one force POST, got ' + JSON.stringify(forcePosts));
  const payload = forcePosts[0].body;
  if (payload.target !== 'webui' || payload.channel !== 'stable') throw new Error('force POST must carry the frozen target/channel: ' + JSON.stringify(payload));
})().catch((err) => { console.error(err.stack || err.message); process.exit(1); });
"""
    _run(_force_harness(body))


def test_stale_error_check_does_not_overwrite_newer_dirty_success():
    """An older check whose rejection resolves AFTER a newer dirty success must
    not publish 'Check failed' / overwrite the banner (owner guard)."""
    body = r"""
const state = {
  btnCheckUpdatesNow: { disabled: false },
  checkUpdatesLabel: { textContent: '' },
  checkUpdatesSpinner: { style: { display: 'none' } },
  checkUpdatesStatus: { textContent: '', style: { color: '' } },
};
global.__state = state;
global.$ = (id) => state[id] || null;
global.t = function (key) {
  const v = { settings_checking: 'Checking', settings_check_now: 'Check now',
    settings_updates_available: '{count} available', settings_update_no_git: 'Cannot check',
    settings_up_to_date: 'Up to date', settings_update_check_failed: 'Check failed' };
  return v[key] || key;
};
let bannerCalls = [];
global._showUpdateBanner = (data, epoch) => { bannerCalls.push({ data, epoch }); };
function makeDeferred() { let res; const p = new Promise((r) => { res = r; }); return { promise: p, resolve: res }; }
const A = makeDeferred();
const B = makeDeferred();
let apiIndex = 0;
global.api = () => (apiIndex++ === 0 ? A.promise : B.promise);
(async () => {
  const p1 = checkUpdatesNow(); // A (older, slow)
  const p2 = checkUpdatesNow(); // B (newer)
  B.resolve({ webui: { dirty: true, behind: 0 }, agent: null }); // newer dirty success
  await p2;
  A.resolve({ webui: { error: 'fetch failed: timeout' }, agent: null }); // older rejection
  await p1;
  if (state.checkUpdatesStatus.textContent.indexOf('Check failed') !== -1) throw new Error('older rejection must not publish Check failed: ' + state.checkUpdatesStatus.textContent);
  if (bannerCalls.length !== 1) throw new Error('only the newer publication may reach the banner, got ' + bannerCalls.length);
})().catch((err) => { console.error(err.stack || err.message); process.exit(1); });
"""
    _run(_check_harness(body))


def _check_harness(body):
    return (
        "global.window = {};\n"
        "global.$ = () => null;\n"
        "global._renderUpdateWhatsNewLinks = () => {};\n"
        + GRANT + FN_FORMAT + FN_ERROR + FN_INSTRUCTION
        + "global._showUpdateBanner = () => {};\n"
        + FN_CHECK + "\n" + body
    )