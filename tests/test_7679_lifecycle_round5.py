"""Production-composed regression tests for the #7679 review round 5.

Every scenario here drives the REAL producers \u2014 the real ``applyUpdates()``
(which is what creates a recovery control), the real ``checkUpdatesNow()``
(which is what settles it) and the real ``forceUpdate()`` (which is what
POSTs) \u2014 inside a Node VM assembled from the production sources. Nothing
is stubbed at the caller boundary: the tests set up the SERVER responses and
then observe what the production code does with them.

That is the point of this file. The previous round's regression test
(``test_the_rejected_check_preserves_a_successor_recovery_and_one_legitimate_post``)
asserted the state by assigning ``window._updateRecoveryGeneration``, calling
``_grantForceUpdate()`` and writing the button's display/disabled/target by
hand. It therefore passed even when the real caller stopped creating the
successor at all, which is exactly what the reviewer asked to be fixed
(maintainer 10-12; Greptile P2 "Recovery tests skip real producers").

The findings covered here:

  1. Greptile P1 (10-14) "New Clear-lock action disappears" \u2014 a check that
     was already pending when Apply reported a lock conflict must not erase
     the Clear-lock action that Apply created.
  2. Greptile P1 (10-10) "Agent recovery disappears" \u2014 the recovery controls
     live INSIDE #updateBanner, so hiding the banner erases them.
  3. Greptile P1 (10-14) "Check button stays disabled" \u2014 the Settings
     controls have their own owner clock, separate from the banner epoch that
     the boot-time check advances.
  4. Maintainer 10-12 \u2014 an obsolete pre-check recovery control must be
     retired on an owned rejection, while a newer one survives and still
     produces exactly one legitimate POST.
"""

from pathlib import Path
import json
import os
import subprocess
import tempfile

REPO = Path(__file__).resolve().parent.parent
UI = (REPO / 'static' / 'ui.js').read_text(encoding='utf-8')
PANELS = (REPO / 'static' / 'panels.js').read_text(encoding='utf-8')
I18N = (REPO / 'static' / 'i18n.js').read_text(encoding='utf-8')


def _extract(src: str, start_marker: str, end_marker: str) -> str:
    i = src.find(start_marker)
    assert i != -1, f'marker not found: {start_marker!r}'
    j = src.find(end_marker, i)
    assert j != -1, f'end marker not found: {end_marker!r}'
    return src[i:j]


def _brace_block(src: str, header: str) -> str:
    """Return the whole ``{ ... }`` block starting at *header*."""
    start = src.find(header)
    assert start != -1, f'{header!r} not found'
    i = src.index('{', start)
    depth = 0
    for idx in range(i, len(src)):
        ch = src[idx]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    raise AssertionError('unbalanced braces')


HARNESS_HEAD = r"""
global.__toasts = [];
global.__posted = [];
global.__state = {
  btnForceUpdate: { style: {}, dataset: {}, disabled: false, textContent: '' },
  btnClearUpdateLock: { style: {}, dataset: {}, disabled: false, textContent: '' },
  btnApplyUpdate: { style: {}, dataset: {}, disabled: false, textContent: '' },
  updateBanner: { classList: { _cls: new Set(), add(c){ this._cls.add(c); }, remove(c){ this._cls.delete(c); }, has(c){ return this._cls.has(c); } }, style: {} },
  updateMsg: { textContent: '' },
  updateError: { textContent: '', style: {} },
  updateStatus: { textContent: '', style: {} },
  btnCheckUpdatesNow: { disabled: false, style: {}, textContent: '' },
  checkUpdatesSpinner: { style: {} },
  checkUpdatesLabel: { textContent: '' },
  checkUpdatesStatus: { textContent: '', style: {} },
};
global.$ = (id) => global.__state[id] || null;
global.window = global;
global.showToast = (m) => { global.__toasts.push(String(m)); };
global._renderUpdateWhatsNewLinks = () => {};
global._waitForServerThenReload = () => {};
global.showConfirmDialog = async () => true;
global.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.__healthReads = 0;
global._readHealthServerIdentity = async () => { global.__healthReads += 1; return null; };
global.__apiQueue = [];
global.__apiDefault = null;
global.__nextApiError = null;
global.__nextApiErrorBody = null;
global.api = async (url, opts) => {
  if (global.__nextApiError) {
    const e = new Error(global.__nextApiError);
    e.response = global.__nextApiErrorBody || null;
    throw e;
  }
  global.__posted.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
  if (global.__apiQueue.length) {
    const next = global.__apiQueue.shift();
    if (next && next.error) {
      const e = new Error(next.error);
      e.response = next.errorBody || null;
      throw e;
    }
    return next;
  }
  return global.__apiDefault || { ok: true, restart_scheduled: true };
};
"""


def _locale_runtime() -> str:
    """Evaluate the FULL shipped runtime and catalog, not a reconstruction.

    The reviewer pinned the previous harness: it re-parsed the English block
    line by line and silently dropped every function-valued entry it could not
    parse, so the sandbox asserted strings the shipped runtime never emits.
    ``static/i18n.js`` is now evaluated VERBATIM in its own context (it ends
    with ``loadLocale()``, which needs only the stubs below) and the real
    ``t()`` is taken from that context — the whole catalog, every locale, every
    function-valued entry, the real missing-key behaviour.
    """
    return r"""
const __vm = require('vm');
const __i18nCtx = {
  console,
  localStorage: { getItem() { return null; }, setItem() {} },
  navigator: { languages: ['en-US'], language: 'en-US' },
  document: { documentElement: {}, querySelectorAll() { return []; } },
};
__vm.createContext(__i18nCtx);
__vm.runInContext(global.__I18N_SRC__, __i18nCtx);
const t = __i18nCtx.t;
__vm.runInContext('globalThis.__catalogLocales = Object.keys(LOCALES);', __i18nCtx);
const __catalogLocales = __i18nCtx.__catalogLocales;

"""


def _build_vm(body: str) -> str:
    """Assemble the real ui.js + panels.js plus a scenario in one VM context."""
    ui_part = _extract(UI, 'let _updateCheckEpoch', 'function _showUpdateBanner(data,')
    ui_part += _extract(UI, 'function _showUpdateBanner(data,', '\nfunction _i18nUpdateText')
    ui_part += _extract(UI, 'function _isForceCleanTarget(', '\nfunction _formatUpdateDirtyStatus')
    ui_part += _extract(UI, 'function _formatUpdateDirtyStatus(', '\nfunction _formatManualUpdateInstruction')
    ui_part += _extract(UI, 'function _formatManualUpdateInstruction(', '\nfunction _formatUpdateCheckError')
    ui_part += _extract(UI, 'function _formatUpdateTargetStatus(', '\nfunction _formatUpdateDirtyStatus')
    # The real producers under test.
    ui_part += '\n' + _brace_block(UI, 'async function forceUpdate(')
    ui_part += '\n' + _brace_block(UI, 'async function applyUpdates(')
    ui_part += '\n' + _brace_block(UI, 'function _showUpdateError(target,res){')
    ui_part += '\nfunction _i18nUpdateText(k,f){const v=t(k);return (v&&v!==k)?v:f;}\nfunction _renderUpdateWhatsNewLinks(){}\n'
    panels_part = _extract(PANELS, 'async function checkUpdatesNow(', '\n// \u2500\u2500 Auxiliary Models')
    return f"""
global.__I18N_SRC__ = {json.dumps(I18N)};
{_locale_runtime()}
{HARNESS_HEAD}
{ui_part}
{panels_part}
{body}
"""


def _run_node(script: str) -> dict:
    # The assembled VM embeds the whole locale table, so the command line
    # would exceed the OS limit: write the script to a file instead.
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as fh:
        fh.write(script)
        path = fh.name
    try:
        proc = subprocess.run(
            ['node', path],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(REPO),
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed:\n{proc.stdout}\n{proc.stderr}'
    payload = proc.stdout.strip().splitlines()[-1]
    return json.loads(payload)


# \u2500\u2500 1. The Clear-lock action survives a pending check \u2500\u2500


def test_the_new_clear_lock_action_survives_the_pending_check():
    """Greptile P1 (10-14): a lock conflict reported by Apply while a check
    is pending must not be erased by that check's result.

    The scenario is the reviewer's: Apply reports an Agent lock conflict, the
    Clear-lock button appears, and the already-pending check then returns an
    error-only payload. Before the fix that payload hid and cleared the
    button, so the operator had to retry Apply to recover the action.

    The check's response is GATED so it really settles after Apply: without
    the gate the check resolves first and the schedule never happens.
    """
    body = r"""
(async () => {
  const out = {};
  // Gate the pending check's response until Apply has reported.
  let release = null;
  const gate = new Promise((resolve) => { release = resolve; });
  const realApi = global.api;
  global.api = async (url, opts) => {
    if (url === '/api/updates/check') {
      await gate;
      return { webui: { error: 'fetch failed', channel: 'stable' }, agent: null };
    }
    return realApi(url, opts);
  };

  // The pending check starts FIRST.
  const checkPromise = checkUpdatesNow();

  // Apply then reports an Agent lock conflict. This is the REAL producer:
  // it creates the Clear-lock control and bumps the recovery generation.
  global.__apiQueue.push({ ok: false, lock_conflict: true, message: 'index.lock present' });
  global.window._updateData = { agent: { behind: 1, channel: 'stable' } };
  await applyUpdates();

  out.afterApply = {
    display: global.__state.btnClearUpdateLock.style.display,
    disabled: global.__state.btnClearUpdateLock.disabled,
    target: global.__state.btnClearUpdateLock.dataset.target,
  };

  // Now the pending check settles with its error-only payload.
  release();
  await checkPromise;
  await new Promise((r) => setTimeout(r, 0));

  out.afterCheck = {
    display: global.__state.btnClearUpdateLock.style.display,
    disabled: global.__state.btnClearUpdateLock.disabled,
    target: global.__state.btnClearUpdateLock.dataset.target,
  };
  out.bannerVisible = global.__state.updateBanner.classList.has('visible');
  out.msg = global.__state.updateMsg.textContent;
  process.stdout.write(JSON.stringify(out));
})();
"""
    result = _run_node(_build_vm(body))
    assert result['afterApply']['display'] == 'inline-block', result
    assert result['afterApply']['disabled'] is False, result
    assert result['afterApply']['target'] == 'agent', result
    # The pending check must NOT have erased it.
    assert result['afterCheck']['display'] == 'inline-block', result
    assert result['afterCheck']['disabled'] is False, result
    assert result['afterCheck']['target'] == 'agent', result
    # And the banner that contains the control must stay visible.
    assert result['bannerVisible'] is True, result


def test_the_clear_lock_action_is_still_retired_by_a_later_owning_check():
    """The ownership rule must not make the Clear-lock control immortal.

    A check that starts AFTER the lock conflict owns the control: it may
    retire it. The stamp is what separates "a check older than the control"
    from "a check that owns the control".
    """
    body = r"""
(async () => {
  const out = {};
  // Apply reports the lock conflict first.
  global.__apiQueue.push({ ok: false, lock_conflict: true, message: 'index.lock present' });
  global.window._updateData = { agent: { behind: 1, channel: 'stable' } };
  await applyUpdates();
  out.afterApply = global.__state.btnClearUpdateLock.style.display;

  // A NEW check starts now, captures the current recovery generation, and
  // settles with a payload that proves the lock is gone.
  global.__apiQueue.push({
    webui: { dirty: false, behind: 0, channel: 'stable' },
    agent: { behind: 0, channel: 'stable', recovery: { clear_lock: false } },
  });
  await checkUpdatesNow();
  out.afterLaterCheck = {
    display: global.__state.btnClearUpdateLock.style.display,
    target: global.__state.btnClearUpdateLock.dataset.target,
  };
  process.stdout.write(JSON.stringify(out));
})();
"""
    result = _run_node(_build_vm(body))
    assert result['afterApply'] == 'inline-block', result
    # A later, owning check may retire it.
    assert result['afterLaterCheck']['display'] == 'none', result
    assert result['afterLaterCheck']['target'] == '', result


# \u2500\u2500 2. The banner stays visible while a recovery control survives \u2500\u2500


def test_the_banner_stays_visible_while_a_recovery_control_survives():
    """Greptile P1 (10-10) "Agent recovery disappears".

    The Force/Clear-lock buttons are children of #updateBanner, so hiding the
    banner hides the recovery action. An error-only payload carries no banner
    text, which used to take the banner down with it.

    The schedule is the reviewer's: a check is pending, Apply reports the
    conflict (a NEWER recovery), and the pending check then returns an
    error-only payload. The check's response is gated so it really settles
    after Apply.
    """
    body = r"""
(async () => {
  const out = {};
  let release = null;
  const gate = new Promise((resolve) => { release = resolve; });
  const realApi = global.api;
  global.api = async (url, opts) => {
    if (url === '/api/updates/check') {
      await gate;
      return { webui: { error: 'fetch failed', channel: 'stable' }, agent: null };
    }
    return realApi(url, opts);
  };

  // The pending check starts FIRST.
  const checkPromise = checkUpdatesNow();

  // Apply reports an Agent conflict \u2014 the real producer creates the Force
  // control and publishes the recovery generation.
  global.__apiQueue.push({ ok: false, conflict: true, message: 'merge conflict' });
  global.window._updateData = { agent: { behind: 1, channel: 'stable' } };
  await applyUpdates();
  out.afterApply = {
    display: global.__state.btnForceUpdate.style.display,
    disabled: global.__state.btnForceUpdate.disabled,
    target: global.__state.btnForceUpdate.dataset.target,
  };

  // The pending check settles with an error-only payload: no banner text.
  release();
  await checkPromise;
  await new Promise((r) => setTimeout(r, 0));

  out.afterCheck = {
    display: global.__state.btnForceUpdate.style.display,
    disabled: global.__state.btnForceUpdate.disabled,
    target: global.__state.btnForceUpdate.dataset.target,
  };
  out.bannerVisible = global.__state.updateBanner.classList.has('visible');
  out.msg = global.__state.updateMsg.textContent;
  process.stdout.write(JSON.stringify(out));
})();
"""
    result = _run_node(_build_vm(body))
    assert result['afterApply']['display'] == 'inline-block', result
    assert result['afterCheck']['display'] == 'inline-block', result
    assert result['afterCheck']['disabled'] is False, result
    assert result['afterCheck']['target'] == 'agent', result
    assert result['bannerVisible'] is True, result
    # The banner must carry recovery text, not be empty.
    assert result['msg'], result


def test_the_banner_is_hidden_when_no_recovery_survives():
    """The ownership rule must not keep the banner up forever.

    With no recovery control left, an error-only payload still hides it.
    """
    body = r"""
(async () => {
  const out = {};
  global.__apiQueue.push({ error: 'boom', errorBody: JSON.stringify({ error: 'check failed' }) });
  try { await checkUpdatesNow(); } catch (e) { out.rejected = String(e && e.message || e); }
  await new Promise((r) => setTimeout(r, 0));
  out.bannerVisible = global.__state.updateBanner.classList.has('visible');
  process.stdout.write(JSON.stringify(out));
})();
"""
    result = _run_node(_build_vm(body))
    assert result['bannerVisible'] is False, result


# \u2500\u2500 3. The Settings controls keep their own owner \u2500\u2500


def test_a_boot_style_check_does_not_strand_the_check_button():
    """Greptile P1 (10-14) "Check button stays disabled".

    A manual Settings check that is already running when another check
    starts (the boot-time one does exactly this) must still restore the Check
    button and spinner. The Settings controls have their own owner clock, so
    the other check's banner epoch does not take them away.
    """
    body = r"""
(async () => {
  const out = {};
  // A manual check starts and its request is still in flight.
  let releaseCheck = null;
  const checkApi = new Promise((resolve) => { releaseCheck = resolve; });
  const realApi = global.api;
  global.api = async (url, opts) => {
    if (url === '/api/updates/check') return checkApi;
    return realApi(url, opts);
  };

  const manual = checkUpdatesNow();
  out.whileRunning = { disabled: global.__state.btnCheckUpdatesNow.disabled };

  // Another check starts now \u2014 this is what the boot-time check does: it
  // advances the shared banner epoch, which used to make the manual check
  // "stale" and skip the control restoration entirely.
  _beginUpdateCheck();

  releaseCheck({ webui: { dirty: false, behind: 0, channel: 'stable' }, agent: null });
  try { await manual; } catch (e) { out.rejected = String(e && e.message || e); }
  await new Promise((r) => setTimeout(r, 0));

  out.after = {
    disabled: global.__state.btnCheckUpdatesNow.disabled,
    spinner: global.__state.checkUpdatesSpinner.style.display,
    label: global.__state.checkUpdatesLabel.textContent,
  };
  process.stdout.write(JSON.stringify(out));
})();
"""
    result = _run_node(_build_vm(body))
    assert result['whileRunning']['disabled'] is True, result
    # The button must be usable again \u2014 not stuck disabled until a reload.
    assert result['after']['disabled'] is False, result
    assert result['after']['spinner'] == 'none', result
    assert result['after']['label'], result


def test_a_newer_manual_check_still_owns_the_controls():
    """The Settings owner clock must still block an older check from
    restoring the controls while a newer manual check is running."""
    body = r"""
(async () => {
  const out = {};
  let releaseFirst = null;
  const firstApi = new Promise((resolve) => { releaseFirst = resolve; });
  const realApi = global.api;
  let call = 0;
  global.api = async (url, opts) => {
    if (url === '/api/updates/check') {
      call += 1;
      if (call === 1) return firstApi;
      return new Promise(() => {});
    }
    return realApi(url, opts);
  };

  const first = checkUpdatesNow();
  // A second manual check starts while the first is still in flight.
  const second = checkUpdatesNow();
  releaseFirst({ webui: { dirty: false, behind: 0, channel: 'stable' }, agent: null });
  try { await first; } catch (e) { out.rejected = String(e && e.message || e); }
  await new Promise((r) => setTimeout(r, 0));

  // The older check must NOT have restored the controls: the newer one owns
  // them and is still running.
  out.disabled = global.__state.btnCheckUpdatesNow.disabled;
  out.spinner = global.__state.checkUpdatesSpinner.style.display;
  void second;
  process.stdout.write(JSON.stringify(out));
})();
"""
    result = _run_node(_build_vm(body))
    assert result['disabled'] is True, result
    assert result['spinner'] != 'none', result


# \u2500\u2500 4. Ownership on an owned rejection \u2500\u2500


def test_the_older_check_retires_the_obsolete_recovery_control():
    """Maintainer 10-12: an obsolete pre-check control must be retired.

    The control exists before the check starts; the check clears its grant
    when it starts, so keeping it visible and enabled would leave a decoy
    that does nothing when clicked.
    """
    body = r"""
(async () => {
  const out = {};
  // A recovery control exists from an earlier Apply.
  global.__apiQueue.push({ ok: false, conflict: true, message: 'merge conflict' });
  global.window._updateData = { agent: { behind: 1, channel: 'stable' } };
  await applyUpdates();
  out.before = {
    display: global.__state.btnForceUpdate.style.display,
    disabled: global.__state.btnForceUpdate.disabled,
  };

  // A check starts (clearing the grant) and is then rejected.
  global.__apiQueue.push({ error: 'boom', errorBody: JSON.stringify({ error: 'check failed' }) });
  try { await checkUpdatesNow(); } catch (e) { out.rejected = String(e && e.message || e); }
  await new Promise((r) => setTimeout(r, 0));

  out.after = {
    display: global.__state.btnForceUpdate.style.display,
    disabled: global.__state.btnForceUpdate.disabled,
    target: global.__state.btnForceUpdate.dataset.target,
  };
  process.stdout.write(JSON.stringify(out));
})();
"""
    result = _run_node(_build_vm(body))
    assert result['before']['display'] == 'inline-block', result
    assert result['after']['display'] == 'none', result
    assert result['after']['disabled'] is True, result
    assert result['after']['target'] == '', result


def test_the_newer_recovery_survives_and_produces_one_legitimate_post():
    """The reviewer's composition, driven by the real producers.

    A check is pending; Apply then reports an Agent conflict (a NEWER
    recovery); the pending check is REJECTED. The newer control must survive,
    stay armed, and a display/disabled-aware click must produce exactly ONE
    legitimate Agent/stable POST.

    The check's rejection is GATED so it really happens after Apply: without
    the gate the request fails before Apply publishes anything, and the
    reconciler's ownership branch would never run at all — the test would pass
    without exercising the behaviour it claims to cover.
    """
    body = r"""
(async () => {
  const out = { posts: [] };
  let release = null;
  const gate = new Promise((resolve) => { release = resolve; });
  const realApi = global.api;
  global.api = async (url, opts) => {
    if (url === '/api/updates/check') {
      await gate;
      throw new Error('check failed');
    }
    return realApi(url, opts);
  };

  // The pending check starts FIRST.
  const checkPromise = checkUpdatesNow();

  // Apply then reports the Agent conflict while the check is in flight. This
  // is the real producer — no hand-written generation, grant or button
  // state.
  global.__apiQueue.push({ ok: false, conflict: true, message: 'merge conflict' });
  global.window._updateData = { agent: { behind: 1, channel: 'stable' } };
  await applyUpdates();

  // NOW the pending check is rejected — after the successor exists.
  release();
  try { await checkPromise; } catch (e) { out.rejected = String(e && e.message || e); }
  await new Promise((r) => setTimeout(r, 0));

  out.state = {
    display: global.__state.btnForceUpdate.style.display,
    disabled: global.__state.btnForceUpdate.disabled,
    target: global.__state.btnForceUpdate.dataset.target,
    grant: _forceUpdateGrant ? _forceUpdateGrant.target : null,
    channel: _forceUpdateGrant ? _forceUpdateGrant.channel : null,
  };

  // A display/disabled-aware click.
  if (global.__state.btnForceUpdate.style.display === 'inline-block' && !global.__state.btnForceUpdate.disabled) {
    global.__apiQueue.push({ ok: true, restart_scheduled: true });
    await forceUpdate(global.__state.btnForceUpdate);
  }
  out.posts = global.__posted.filter((p) => p.url === '/api/updates/force');
  process.stdout.write(JSON.stringify(out));
})();
"""
    result = _run_node(_build_vm(body))
    assert result['state']['display'] == 'inline-block', result
    assert result['state']['disabled'] is False, result
    assert result['state']['target'] == 'agent', result
    assert result['state']['grant'] == 'agent', result
    assert result['state']['channel'] == 'stable', result
    assert len(result['posts']) == 1, result
    assert result['posts'][0]['body'] == {'target': 'agent', 'channel': 'stable'}, result


def test_the_obsolete_control_does_not_fire_alongside_the_newer_one():
    """The obsolete pre-check control must not POST when the newer one does.

    Both controls are the same DOM button, so this asserts the click path is
    bound to the surviving (newer) recovery: exactly one POST, carrying the
    successor's own target/channel.
    """
    body = r"""
(async () => {
  const out = { posts: [] };
  // An obsolete control from an earlier Apply.
  global.__apiQueue.push({ ok: false, conflict: true, message: 'merge conflict' });
  global.window._updateData = { agent: { behind: 1, channel: 'stable' } };
  await applyUpdates();

  // A newer conflict while a check is pending, and the check is rejected
  // AFTER the newer conflict was published.
  let release = null;
  const gate = new Promise((resolve) => { release = resolve; });
  const realApi = global.api;
  global.api = async (url, opts) => {
    if (url === '/api/updates/check') {
      await gate;
      throw new Error('check failed');
    }
    return realApi(url, opts);
  };
  const checkPromise = checkUpdatesNow();
  global.__apiQueue.push({ ok: false, conflict: true, message: 'merge conflict again' });
  await applyUpdates();
  release();
  try { await checkPromise; } catch (e) { out.rejected = String(e && e.message || e); }
  await new Promise((r) => setTimeout(r, 0));

  if (global.__state.btnForceUpdate.style.display === 'inline-block' && !global.__state.btnForceUpdate.disabled) {
    global.__apiQueue.push({ ok: true, restart_scheduled: true });
    await forceUpdate(global.__state.btnForceUpdate);
  }
  out.posts = global.__posted.filter((p) => p.url === '/api/updates/force');
  out.state = {
    display: global.__state.btnForceUpdate.style.display,
    target: global.__state.btnForceUpdate.dataset.target,
  };
  process.stdout.write(JSON.stringify(out));
})();
"""
    result = _run_node(_build_vm(body))
    assert result['state']['display'] == 'inline-block', result
    assert result['state']['target'] == 'agent', result
    assert len(result['posts']) == 1, result
    assert result['posts'][0]['body']['target'] == 'agent', result


def test_the_harness_runs_the_full_shipped_catalog():
    """The sandbox must evaluate the FULL runtime/catalog, not a reconstruction.

    The previous harness re-parsed the English block line by line and swallowed
    function-entry parse failures, so it could assert strings the shipped
    runtime never emits.
    """
    body = r"""
process.stdout.write(JSON.stringify({
  locales: __catalogLocales,
  active: t('update_force_active_runs', 2),
  missing: t('__no_such_key__'),
}));
"""
    result = _run_node(_build_vm(body))
    import re as _re

    shipped = [
        m.group(1) or m.group(2) or m.group(3)
        for m in _re.finditer(
            r"^  (?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_-]*)): \{",
            I18N,
            _re.MULTILINE,
        )
    ]
    assert result["locales"] == shipped, (
        f"the sandbox catalog is not the shipped one: {result['locales']!r} != {shipped!r}"
    )
    assert result["active"] == "Update anyway (interrupts 2 runs)", result
    assert result["missing"] == "__no_such_key__", result
