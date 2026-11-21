"""#7679 re-gate 2026-10-31 — the two remaining findings, producer-composed.

The maintainer's re-gate of ``cb27d3c03`` accepted the earlier repairs and
left two findings plus two coverage gaps. This file closes all four, and every
scenario drives the REAL producers — the real ``checkUpdatesNow()`` (the Check
caller), the real ``applyUpdates()`` / ``_showUpdateError()`` (the Apply caller
that mints recovery controls and their grants), the real
``_renderActiveRunsForceOffer()`` / ``forceUpdate()`` (the offer and the
destructive POST) — inside a Node VM assembled from the shipped sources. Only
the network, the confirm dialog, the health read and the reload are stubbed.

**Finding 1 — an old active-runs offer borrowed a successor grant.**
``forceUpdate`` snapshotted the live grant on entry, but the offer's retry
stored only ``()=>forceUpdate(btn,{forceActiveRuns:true})``: it re-read the
SHARED legacy button and the live grant. A newer Check that retargeted that
button from WebUI to Agent (and minted the Agent grant) therefore turned a
confirmed WebUI destruction into an Agent one, and the retry skipped the
discard confirmation because ``forceActiveRuns`` was set.

**Finding 2 — a rejecting older Check erased a newer WebUI recovery.**
``_reconcileObsoleteUpdateControls``'s ``keepAgent`` predicate rejected any
non-Agent control BEFORE looking at the generation, so a WebUI Force /
Clear-lock control that a newer ``applyUpdates()`` had just published was
retired, hidden, disabled and detargeted by the older rejected Check.

**Remaining acceptance coverage.**
* the paused-health scene now completes a real ``checkUpdatesNow()`` instead of
  retiring the grant by hand (that rewrite lives in
  ``tests/test_7679_lifecycle_round2.py``);
* the lifecycle harnesses evaluate the FULL shipped ``static/i18n.js`` runtime
  and catalog instead of reconstructing the English block line by line and
  swallowing function-entry parse failures (rounds 2, 3 and 5).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
UI = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
PANELS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
I18N = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")

NODE = None
try:
    import shutil

    NODE = shutil.which("node")
except Exception:  # pragma: no cover - node is expected on the test hosts
    NODE = None

pytestmark = pytest.mark.skipif(NODE is None, reason="node required for the ui.js harness")


def _extract(src: str, start_marker: str, end_marker: str) -> str:
    i = src.find(start_marker)
    assert i != -1, f"marker not found: {start_marker!r}"
    j = src.find(end_marker, i)
    assert j != -1, f"end marker not found: {end_marker!r}"
    return src[i:j]


def _brace_block(src: str, header: str) -> str:
    """Return the whole ``{ ... }`` block starting at *header*."""
    start = src.find(header)
    assert start != -1, f"{header!r} not found"
    i = src.index("{", start)
    depth = 0
    for idx in range(i, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError("unbalanced braces")


# ── the FULL shipped runtime and catalog ─────────────────────────────────────
# static/i18n.js is evaluated VERBATIM in its own context and the real t() is
# taken from that context. The previous harnesses re-parsed the English block
# line by line and silently dropped every function-valued entry they could not
# parse, so they asserted strings the shipped runtime never emits. Loading the
# whole file (it ends with loadLocale(), which needs only the stubs below)
# keeps every locale, every function-valued entry and the real missing-key
# behaviour in play.
_LOCALE_RUNTIME = r"""
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


HARNESS_HEAD = r"""
// ── a minimal DOM: enough for _renderActiveRunsForceOffer, which creates the
// offer button and appends it to #updateError ──────────────────────────────
const __root = { id: '__root__', children: [], parentNode: null };
function makeEl(tag, id) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(), id: id || '', type: '', className: '',
    style: {}, dataset: {}, children: [], parentNode: null, disabled: false, onclick: null, _text: '',
  };
  Object.defineProperty(el, 'textContent', {
    get() { return el._text + el.children.map((c) => c.textContent).join(''); },
    set(v) {  // like the DOM: assigning text drops every child node
      for (const c of el.children) c.parentNode = null;
      el.children = [];
      el._text = String(v);
    },
  });
  Object.defineProperty(el, 'isConnected', {
    get() { let n = el; while (n) { if (n === __root) return true; n = n.parentNode; } return false; },
  });
  el.appendChild = (c) => { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = el; el.children.push(c); return c; };
  el.removeChild = (c) => { el.children = el.children.filter((x) => x !== c); c.parentNode = null; return c; };
  el.remove = () => { if (el.parentNode) el.parentNode.removeChild(el); };
  return el;
}
__root.removeChild = (c) => { __root.children = __root.children.filter((x) => x !== c); c.parentNode = null; return c; };
function mount(el) { el.parentNode = __root; __root.children.push(el); return el; }
function findById(node, id) {
  for (const c of node.children || []) {
    if (c.id === id) return c;
    const hit = findById(c, id);
    if (hit) return hit;
  }
  return null;
}

global.__toasts = [];
global.__posted = [];
global.__confirms = [];
global.__reloads = [];
global.__healthReads = 0;
global.__state = {
  btnApplyUpdate: { disabled: false, style: { display: '' }, dataset: {}, textContent: 'Update Now' },
  updateBanner: { classList: { _cls: new Set(), add(c) { this._cls.add(c); }, remove(c) { this._cls.delete(c); }, has(c) { return this._cls.has(c); } }, style: {} },
  updateMsg: { textContent: '' },
  updateStatus: { textContent: '', style: {} },
  btnCheckUpdatesNow: { disabled: false, style: {}, textContent: '' },
  checkUpdatesSpinner: { style: { display: 'none' } },
  checkUpdatesLabel: { textContent: '' },
  checkUpdatesStatus: { textContent: '', style: {} },
};
global.window = global;
global.document = { createElement: (tag) => makeEl(tag), baseURI: 'http://127.0.0.1:8788/' };
// The recovery controls live in the banner, so they are real elements — the
// active-runs offer is created with document.createElement and appended to
// #updateError, exactly as in a browser.
const __updateErrorEl = mount(makeEl('div', 'updateError'));
__updateErrorEl.style.display = 'none';
const __forceBtnEl = mount(makeEl('button', 'btnForceUpdate'));
__forceBtnEl.dataset.target = '';
const __lockBtnEl = mount(makeEl('button', 'btnClearUpdateLock'));
__lockBtnEl.dataset.target = '';
global.__state.updateError = __updateErrorEl;
global.__state.btnForceUpdate = __forceBtnEl;
global.__state.btnClearUpdateLock = __lockBtnEl;
global.$ = (id) => {
  const el = findById(__root, id);
  if (el) return el;
  return global.__state[id] || null;
};
global.showToast = (m) => { global.__toasts.push(String(m)); };
global._renderUpdateWhatsNewLinks = () => {};
global._waitForServerThenReload = (opts) => { global.__reloads.push(opts || {}); };
global._readHealthServerIdentity = async () => { global.__healthReads += 1; return null; };
global.showConfirmDialog = async (opts) => { global.__confirms.push(opts && opts.title); return true; };
global.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
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


def _build_vm(body: str) -> str:
    """Assemble the real ui.js + panels.js plus a scenario in one VM context."""
    ui_part = _extract(UI, "let _updateCheckEpoch", "function _showUpdateBanner(data,")
    ui_part += _extract(UI, "function _showUpdateBanner(data,", "\nfunction _i18nUpdateText")
    ui_part += _extract(UI, "function _isForceCleanTarget(", "\nfunction _formatUpdateDirtyStatus")
    ui_part += _extract(UI, "function _formatUpdateDirtyStatus(", "\nfunction _formatManualUpdateInstruction")
    ui_part += _extract(UI, "function _formatManualUpdateInstruction(", "\nfunction _formatUpdateCheckError")
    ui_part += _extract(UI, "function _formatUpdateTargetStatus(", "\nfunction _formatUpdateDirtyStatus")
    # The real producers under test.
    ui_part += "\n" + _brace_block(UI, "async function forceUpdate(")
    ui_part += "\n" + _brace_block(UI, "async function applyUpdates(")
    ui_part += "\n" + _brace_block(UI, "function _showUpdateError(target,res){")
    ui_part += "\n" + _brace_block(UI, "function _activeRunsUpdateText(")
    ui_part += "\n" + _brace_block(UI, "function _renderActiveRunsForceOffer(")
    ui_part += "\n" + _brace_block(UI, "async function applyClearUpdateLock(")
    ui_part += "\n" + _brace_block(UI, "function _renderLockManualInstruction(")
    ui_part += "\n" + _brace_block(UI, "function _isUpdateApplyNetworkError(error){")
    ui_part += "\n" + _brace_block(UI, "function _formatUpdateApplyExceptionMessage(error){")
    ui_part += "\nfunction _i18nUpdateText(k,f){const v=t(k);return (v&&v!==k)?v:f;}\nfunction _renderUpdateWhatsNewLinks(){}\n"
    panels_part = _extract(PANELS, "async function checkUpdatesNow(", "\n// ── Auxiliary Models")
    return f"""global.__I18N_SRC__ = {json.dumps(I18N)};
{_LOCALE_RUNTIME}
{HARNESS_HEAD}
{ui_part}
{panels_part}
{body}

"""


def _run_node(script: str) -> dict:
    # The assembled VM embeds the whole locale table, so the command line
    # would exceed the OS limit: write the script to a file instead.
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        env = dict(os.environ)
        env.pop("NODE_CHANNEL_FD", None)
        env.pop("HERMES_WEBUI_TEST_STATE_DIR", None)
        proc = subprocess.run(
            [NODE, path],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(REPO),
            env=env,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    assert proc.returncode == 0, f"node failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── finding 1: the offer is bound to the grant it was confirmed for ─────────

_REFUSAL = "{ ok: false, message: 'Cannot update webui while 2 WebUI agent runs are active.', target: 'webui', restart_blocked: true, force_available: true, active_runs: 2, active_streams: 2 }"
_DIRTY_WEBUI = "{ webui: { behind: 0, dirty: true, manual_update: false, channel: 'stable' }, agent: { behind: 0, dirty: false, manual_update: false, channel: 'stable' } }"
_CLEAN_WEBUI_DIRTY_AGENT = "{ webui: { behind: 0, dirty: false, manual_update: false, channel: 'stable' }, agent: { behind: 0, dirty: true, manual_update: false, channel: 'stable' } }"
_CLEAN_BOTH = "{ webui: { behind: 0, dirty: false, manual_update: false, channel: 'stable' }, agent: { behind: 0, dirty: false, manual_update: false, channel: 'stable' } }"


def _check_stub(second_payload: str) -> str:
    """A check endpoint that answers dirty-WebUI first, then *second_payload*."""
    return r"""
const __realApi = global.api;
global.__checks = 0;
global.api = async (url, opts) => {
  if (url === '/api/updates/check') {
    global.__checks += 1;
    return global.__checks === 1 ? __DIRTY_WEBUI__ : __SECOND_PAYLOAD__;
  }
  if (url === '/api/updates/force') {
    global.__posted.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
    global.__forceCalls = (global.__forceCalls || 0) + 1;
    if (global.__forceCalls === 1) return __REFUSAL__;
    return { ok: true, restart_scheduled: true };
  }
  return __realApi(url, opts);
};
""".replace("__DIRTY_WEBUI__", _DIRTY_WEBUI).replace(
        "__SECOND_PAYLOAD__", second_payload
    ).replace("__REFUSAL__", _REFUSAL)


def test_an_old_offer_cannot_borrow_a_successor_grant():
    """Finding 1: the reviewer's schedule, through the real producers.

    1. a dirty WebUI/stable Check arms Force; the discard confirmation is
       given and the server refuses for active runs, exposing the offer;
    2. before the offer is used, a new Check completes with clean WebUI and
       dirty-at-latest Agent — it retargets the shared legacy button to Agent
       and mints the Agent grant;
    3. the OLD, still-connected offer is used. It must not replay the request
       against the successor: no Agent POST, no duplicate discard
       confirmation, and the refusal must be reported.
    """
    body = (
        _check_stub(_CLEAN_WEBUI_DIRTY_AGENT)
        + r"""
(async () => {
  const out = { confirms: global.__confirms };
  // 1) A dirty WebUI/stable Check arms Force.
  await checkUpdatesNow();
  const btn = global.__state.btnForceUpdate;
  out.afterCheck1 = {
    display: btn.style.display,
    target: btn.dataset.target,
    grant: _forceUpdateGrant ? _forceUpdateGrant.target : null,
    channel: _forceUpdateGrant ? _forceUpdateGrant.channel : null,
  };
  // 2) Confirm discarding the WebUI changes; the request is refused for
  //    active runs and exposes the interrupt/retry offer.
  await forceUpdate(btn);
  const offer = findById(__root, 'btnForceUpdateActiveRuns');
  out.offerConnected = !!offer && offer.isConnected;
  out.forcePostsAfterRefusal = global.__posted.filter((p) => p.url === '/api/updates/force').length;
  // 3) Before using that offer, complete a new Check with clean WebUI and
  //    dirty-at-latest Agent.
  await checkUpdatesNow();
  out.afterCheck2 = {
    display: btn.style.display,
    target: btn.dataset.target,
    grant: _forceUpdateGrant ? _forceUpdateGrant.target : null,
  };
  // 4) Use the OLD, still-connected offer.
  if (offer) await offer.onclick();
  out.confirms = global.__confirms;
  out.forcePosts = global.__posted.filter((p) => p.url === '/api/updates/force');
  out.errorText = global.__state.updateError.textContent;
  out.toasts = global.__toasts.slice();
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stdout.write('THREW:' + (e && e.stack || e)); process.exitCode = 1; });
"""
    )
    result = _run_node(_build_vm(body))
    assert result["afterCheck1"]["target"] == "webui", (
        f"the dirty Check did not arm Force on WebUI: {result!r}"
    )
    assert result["afterCheck1"]["grant"] == "webui", result
    assert result["offerConnected"] is True, (
        "the active-runs offer was never rendered, so the schedule never "
        "reached the state finding 1 describes"
    )
    assert result["forcePostsAfterRefusal"] == 1, result
    # The retarget must really have happened, otherwise the scenario is vacuous.
    assert result["afterCheck2"]["target"] == "agent", (
        f"the second Check did not retarget the shared button: {result!r}"
    )
    assert result["afterCheck2"]["grant"] == "agent", result
    # One discard confirmation for WebUI and one interrupt confirmation — and
    # never a second discard confirmation.
    assert result["confirms"] == ["Force update webui?", "Interrupt active runs?"], (
        f"the confirmation sequence changed: {result['confirms']!r}"
    )
    # The only force POST is the refused WebUI one: the retry must not have
    # been replayed against the successor Agent grant.
    assert len(result["forcePosts"]) == 1, (
        "the old active-runs offer replayed its retry after a newer Check had "
        f"replaced the grant it was confirmed for: {result['forcePosts']!r}"
    )
    assert all(p["body"]["target"] == "webui" for p in result["forcePosts"]), (
        f"a force POST carried a target the user never confirmed: {result['forcePosts']!r}"
    )
    assert not any(p["body"].get("force_active_runs") for p in result["forcePosts"]), (
        "the stale retry POSTed with force_active_runs against a state the "
        f"user never confirmed: {result['forcePosts']!r}"
    )
    surfaced = (result["errorText"] or "") + " " + " ".join(result["toasts"])
    assert "no longer applicable" in surfaced.lower(), (
        f"the refused retry was not reported to the user: {result!r}"
    )


def test_the_offer_rejects_a_replaced_grant_even_for_the_same_target_and_channel():
    """Finding 1: identity, not shape, is the authority.

    A second Check that re-arms the SAME target and channel still replaces the
    grant object. The offer was confirmed against the original one, so the
    replay must be refused even though nothing about the target/channel looks
    different.
    """
    body = (
        _check_stub(_DIRTY_WEBUI)
        + r"""
(async () => {
  const out = {};
  await checkUpdatesNow();
  const btn = global.__state.btnForceUpdate;
  const grantAtRefusal = _forceUpdateGrant;
  await forceUpdate(btn);
  const offer = findById(__root, 'btnForceUpdateActiveRuns');
  out.offerConnected = !!offer && offer.isConnected;
  // A newer Check re-arms an equivalent-looking grant: same target, same
  // channel, DIFFERENT object.
  await checkUpdatesNow();
  out.replaced = !!_forceUpdateGrant && _forceUpdateGrant !== grantAtRefusal;
  out.replacement = _forceUpdateGrant
    ? { target: _forceUpdateGrant.target, channel: _forceUpdateGrant.channel }
    : null;
  if (offer) await offer.onclick();
  out.confirms = global.__confirms;
  out.forcePosts = global.__posted.filter((p) => p.url === '/api/updates/force');
  out.errorText = global.__state.updateError.textContent;
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stdout.write('THREW:' + (e && e.stack || e)); process.exitCode = 1; });
"""
    )
    result = _run_node(_build_vm(body))
    assert result["offerConnected"] is True, result
    assert result["replaced"] is True, (
        "the second Check did not mint a replacement grant, so the scenario "
        f"never reached the replaced-grant case: {result!r}"
    )
    assert result["replacement"] == {"target": "webui", "channel": "stable"}, result
    assert result["confirms"] == ["Force update webui?", "Interrupt active runs?"], (
        f"the confirmation sequence changed: {result['confirms']!r}"
    )
    assert len(result["forcePosts"]) == 1, (
        "a replaced grant — same target and channel, different object — was "
        f"accepted by the old offer: {result['forcePosts']!r}"
    )
    assert not any(p["body"].get("force_active_runs") for p in result["forcePosts"]), (
        f"the stale retry POSTed anyway: {result['forcePosts']!r}"
    )
    assert "no longer applicable" in (result["errorText"] or "").lower(), result


def test_an_unchanged_grant_still_completes_the_retry():
    """The positive control: an unchanged grant must still work.

    Nothing superseded the confirmation, so the offer's replay goes through:
    exactly ONE interrupt confirmation, NO duplicate discard confirmation, and
    the POST carries the originally confirmed target/channel plus
    ``force_active_runs``.
    """
    body = (
        _check_stub(_CLEAN_WEBUI_DIRTY_AGENT)
        + r"""
(async () => {
  const out = {};
  await checkUpdatesNow();
  const btn = global.__state.btnForceUpdate;
  await forceUpdate(btn);
  const offer = findById(__root, 'btnForceUpdateActiveRuns');
  out.offerConnected = !!offer && offer.isConnected;
  // No second Check: the grant the user confirmed is still the live one.
  if (offer) await offer.onclick();
  out.confirms = global.__confirms;
  out.forcePosts = global.__posted.filter((p) => p.url === '/api/updates/force');
  out.reloads = global.__reloads.length;
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stdout.write('THREW:' + (e && e.stack || e)); process.exitCode = 1; });
"""
    )
    result = _run_node(_build_vm(body))
    assert result["offerConnected"] is True, result
    assert result["confirms"] == ["Force update webui?", "Interrupt active runs?"], (
        "the unchanged-grant retry must show exactly one interrupt "
        f"confirmation and no second discard confirmation: {result['confirms']!r}"
    )
    assert result["forcePosts"] == [
        {"url": "/api/updates/force", "body": {"target": "webui", "channel": "stable"}},
        {
            "url": "/api/updates/force",
            "body": {"target": "webui", "channel": "stable", "force_active_runs": True},
        },
    ], f"the retry did not replay the original request: {result['forcePosts']!r}"
    assert result["reloads"] == 1, result


def test_the_bound_retry_is_revalidated_across_the_health_await():
    """Finding 1: the replay must also survive the health await.

    The interrupt confirmation is given, the health read is paused, a newer
    real Check completes during the pause, and only then does health settle.
    The handler holds the ORIGINAL grant, so it must refuse before the POST.
    """
    body = (
        _check_stub(_CLEAN_BOTH)
        + r"""
(async () => {
  const out = {};
  await checkUpdatesNow();
  const btn = global.__state.btnForceUpdate;
  await forceUpdate(btn);
  const offer = findById(__root, 'btnForceUpdateActiveRuns');
  const boundGrant = _forceUpdateGrant;

  // The health read is paused until the scenario releases it.
  let releaseHealth = null;
  const healthGate = new Promise((resolve) => { releaseHealth = resolve; });
  global._readHealthServerIdentity = async () => {
    global.__healthReads += 1;
    await healthGate;
    return null;
  };

  const clickPromise = offer.onclick();
  await new Promise((r) => setTimeout(r, 0));
  // While the (paused) health read is in flight, a newer real Check completes.
  global.__apiDefault = __CLEAN_BOTH__;
  await checkUpdatesNow();
  out.grantReplaced = _forceUpdateGrant !== boundGrant;
  out.healthReads = global.__healthReads;
  releaseHealth();
  await clickPromise;

  out.confirms = global.__confirms;
  out.forcePosts = global.__posted.filter((p) => p.url === '/api/updates/force');
  out.errorText = global.__state.updateError.textContent;
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stdout.write('THREW:' + (e && e.stack || e)); process.exitCode = 1; });
""".replace(
            "__CLEAN_BOTH__", _CLEAN_BOTH
        )
    )
    result = _run_node(_build_vm(body))
    assert result["healthReads"] >= 1, (
        "the retry never reached the health read, so the post-health "
        f"revalidation was not exercised: {result!r}"
    )
    assert result["grantReplaced"] is True, (
        "the newer Check did not supersede the bound grant, so the scenario "
        f"never reached the case it claims to cover: {result!r}"
    )
    assert len(result["forcePosts"]) == 1, (
        "the bound retry POSTed after a newer Check had replaced the grant "
        f"during the health await: {result['forcePosts']!r}"
    )
    assert not any(p["body"].get("force_active_runs") for p in result["forcePosts"]), (
        f"the stale retry POSTed anyway: {result['forcePosts']!r}"
    )
    assert "no longer applicable" in (result["errorText"] or "").lower(), result


# ── finding 2: a rejected Check must not erase a newer WebUI recovery ───────


def _rejected_check_scenario(target: str, kind: str) -> str:
    """A pending Check, a real Apply that publishes a recovery, then the
    Check's rejection — the reviewer's schedule, both targets, both kinds."""
    apply_response = (
        "{ ok: false, conflict: true, message: 'merge conflict' }"
        if kind == "conflict"
        else "{ ok: false, lock_conflict: true, message: 'index.lock present' }"
    )
    update_data = (
        "{ webui: { behind: 1, channel: 'stable' }, agent: { behind: 0, channel: 'stable' } }"
        if target == "webui"
        else "{ agent: { behind: 1, channel: 'stable' }, webui: { behind: 0, channel: 'stable' } }"
    )
    return r"""
(async () => {
  const out = {};
  // The Check starts FIRST and its request is gated, so it is genuinely
  // in flight when Apply publishes the recovery.
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

  // The REAL applyUpdates() reports the recovery for __TARGET__.
  global.__apiQueue.push(__APPLY_RESPONSE__);
  global.window._updateData = __UPDATE_DATA__;
  await applyUpdates();
  out.afterApply = {
    forceDisplay: global.__state.btnForceUpdate.style.display,
    forceTarget: global.__state.btnForceUpdate.dataset.target,
    lockDisplay: global.__state.btnClearUpdateLock.style.display,
    lockTarget: global.__state.btnClearUpdateLock.dataset.target,
    grant: _forceUpdateGrant ? _forceUpdateGrant.target : null,
    channel: _forceUpdateGrant ? _forceUpdateGrant.channel : null,
  };

  // NOW the pending Check is rejected — after the recovery was published.
  release();
  try { await checkPromise; } catch (e) { out.rejected = String(e && e.message || e); }
  await new Promise((r) => setTimeout(r, 0));

  out.afterRejection = {
    forceDisplay: global.__state.btnForceUpdate.style.display,
    forceDisabled: global.__state.btnForceUpdate.disabled,
    forceTarget: global.__state.btnForceUpdate.dataset.target,
    lockDisplay: global.__state.btnClearUpdateLock.style.display,
    lockDisabled: global.__state.btnClearUpdateLock.disabled,
    lockTarget: global.__state.btnClearUpdateLock.dataset.target,
    grant: _forceUpdateGrant ? _forceUpdateGrant.target : null,
    channel: _forceUpdateGrant ? _forceUpdateGrant.channel : null,
  };

  // A display/disabled-aware click on the surviving control, exactly as a
  // user could perform it.
  if ('__KIND__' === 'conflict') {
    const btn = global.__state.btnForceUpdate;
    if (btn.style.display === 'inline-block' && !btn.disabled) {
      global.__apiQueue.push({ ok: true, restart_scheduled: true });
      await forceUpdate(btn);
    }
    out.posts = global.__posted.filter((p) => p.url === '/api/updates/force');
  } else {
    const btn = global.__state.btnClearUpdateLock;
    if (btn.style.display === 'inline-block' && !btn.disabled) {
      global.__apiQueue.push({ ok: true });
      await applyClearUpdateLock(btn);
    }
    out.posts = global.__posted.filter((p) => p.url === '/api/updates/clear_lock');
  }
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stdout.write('THREW:' + (e && e.stack || e)); process.exitCode = 1; });
""".replace(
        "__APPLY_RESPONSE__", apply_response
    ).replace("__UPDATE_DATA__", update_data).replace("__TARGET__", target).replace(
        "__KIND__", kind
    )


@pytest.mark.parametrize("target", ["webui", "agent"])
def test_a_rejected_check_preserves_a_newer_force_recovery(target):
    """Finding 2: the WebUI sibling of the working Agent schedule.

    A pending Check is followed by a real Apply reporting a conflict for
    *target*, which stamps a newer owner and its own Force grant. When the
    Check rejects, that control must survive with its own target/channel/grant
    and still produce exactly ONE legitimate POST.
    """
    result = _run_node(_build_vm(_rejected_check_scenario(target, "conflict")))
    after_apply = result["afterApply"]
    assert after_apply["forceDisplay"] == "inline-block", (
        f"Apply did not publish the {target} Force control: {result!r}"
    )
    assert after_apply["forceTarget"] == target, result
    assert after_apply["grant"] == target, result
    after = result["afterRejection"]
    assert after["forceDisplay"] == "inline-block", (
        f"the rejected Check hid the newer {target} Force recovery: {result!r}"
    )
    assert after["forceDisabled"] is False, (
        f"the rejected Check disabled the newer {target} Force recovery: {result!r}"
    )
    assert after["forceTarget"] == target, (
        f"the rejected Check detargeted the newer {target} Force recovery: {result!r}"
    )
    assert after["grant"] == target and after["channel"] == "stable", (
        f"the successor's own grant was not preserved for {target}: {result!r}"
    )
    assert len(result["posts"]) == 1, (
        f"expected exactly one legitimate force POST, got {result['posts']!r}"
    )
    assert result["posts"][0]["body"] == {"target": target, "channel": "stable"}, (
        f"the POST did not carry the successor's own target/channel: {result!r}"
    )


@pytest.mark.parametrize("target", ["webui", "agent"])
def test_a_rejected_check_preserves_a_newer_clear_lock_recovery(target):
    """Finding 2: the Clear-lock control is owned independently of the grant.

    Apply reports a lock conflict for *target* while a Check is pending; the
    rejection must retire nothing but what it owns.
    """
    result = _run_node(_build_vm(_rejected_check_scenario(target, "lock_conflict")))
    after_apply = result["afterApply"]
    assert after_apply["lockDisplay"] == "inline-block", (
        f"Apply did not publish the {target} Clear-lock control: {result!r}"
    )
    assert after_apply["lockTarget"] == target, result
    after = result["afterRejection"]
    assert after["lockDisplay"] == "inline-block", (
        f"the rejected Check hid the newer {target} Clear-lock recovery: {result!r}"
    )
    assert after["lockDisabled"] is False, (
        f"the rejected Check disabled the newer {target} Clear-lock recovery: {result!r}"
    )
    assert after["lockTarget"] == target, (
        f"the rejected Check detargeted the newer {target} Clear-lock recovery: {result!r}"
    )
    assert len(result["posts"]) == 1, (
        f"expected exactly one legitimate clear_lock POST, got {result['posts']!r}"
    )
    assert result["posts"][0]["body"] == {"target": target}, (
        f"the clear_lock POST did not carry the successor's target: {result!r}"
    )


def test_a_rejected_check_still_retires_an_obsolete_webui_control():
    """The control for finding 2: ownership must not make a control immortal.

    A WebUI recovery that PREDATES the check is obsolete — the check cleared
    its grant when it started — so the rejection must retire it instead of
    leaving a visible, enabled, inert button behind.
    """
    body = r"""
(async () => {
  const out = {};
  // A WebUI recovery exists from an earlier Apply.
  global.__apiQueue.push({ ok: false, conflict: true, message: 'merge conflict' });
  global.window._updateData = { webui: { behind: 1, channel: 'stable' }, agent: { behind: 0 } };
  await applyUpdates();
  out.before = {
    display: global.__state.btnForceUpdate.style.display,
    target: global.__state.btnForceUpdate.dataset.target,
  };

  // A Check starts now (it clears the grant when it starts) and is rejected.
  global.__apiQueue.push({ error: 'boom', errorBody: JSON.stringify({ error: 'check failed' }) });
  try { await checkUpdatesNow(); } catch (e) { out.rejected = String(e && e.message || e); }
  await new Promise((r) => setTimeout(r, 0));

  out.after = {
    display: global.__state.btnForceUpdate.style.display,
    disabled: global.__state.btnForceUpdate.disabled,
    target: global.__state.btnForceUpdate.dataset.target,
    grant: _forceUpdateGrant ? _forceUpdateGrant.target : null,
  };
  process.stdout.write(JSON.stringify(out));
})();
"""
    result = _run_node(_build_vm(body))
    assert result["before"]["display"] == "inline-block", result
    assert result["before"]["target"] == "webui", result
    assert result["after"]["display"] == "none", (
        f"an obsolete pre-check WebUI control survived: {result!r}"
    )
    assert result["after"]["disabled"] is True, result
    assert result["after"]["target"] == "", result
    assert result["after"]["grant"] is None, (
        f"an obsolete control kept its grant: {result!r}"
    )


# ── source-shape contracts for the structural parts ─────────────────────────


def test_the_reconciler_resolves_both_targets_before_retirement():
    """Finding 2: the keep predicate must accept BOTH supported targets.

    The old predicate rejected any non-Agent control before it ever looked at
    the generation, which is what erased a newer WebUI recovery.
    """
    src = UI
    idx = src.find("function _reconcileObsoleteUpdateControls(")
    assert idx > 0
    body = src[idx : idx + 9000]
    keep_idx = body.find("const keepForce=")
    assert keep_idx > 0, "the reconciler's keep predicate is gone"
    predicate = body[:keep_idx]
    assert "'agent'" in predicate and "'webui'" in predicate, (
        "the keep predicate does not resolve ownership for both supported "
        "targets before retirement (#7679 finding 2)"
    )
    # The generation comparison must come after the target check, so a newer
    # control of EITHER target reaches it.
    target_idx = predicate.find("'webui'")
    generation_idx = predicate.find("_controlIsNewerThan")
    assert target_idx < generation_idx, (
        "the keep predicate still checks the generation before resolving the "
        "target, so a WebUI control can never be recognised as newer "
        "(#7679 finding 2)"
    )


def test_the_reconciler_rearms_from_the_grant_not_the_button():
    """Finding 2: a preserved control is re-armed from its OWN grant.

    Minting authority from the button's dataset would arm a destructive action
    the payload never validated.
    """
    src = UI
    idx = src.find("function _reconcileObsoleteUpdateControls(")
    assert idx > 0
    body = src[idx : idx + 9000]
    rearm_idx = body.find("if(keepForce&&typeof _grantForceUpdate==='function'){")
    assert rearm_idx > 0, "the reconciler no longer re-arms a preserved control"
    rearm = body[rearm_idx:]
    assert "_forceUpdateGrant.target" in rearm, (
        "the preserved control is not re-armed from the grant it already "
        "holds (#7679 finding 2)"
    )
    assert "dataset.target" not in rearm.split("return {keepForce")[0], (
        "the re-arm reads the button's dataset, i.e. it mints authority from "
        "button metadata (#7679 finding 2)"
    )


def test_the_full_shipped_catalog_is_in_play():
    """The harness must run the FULL runtime/catalog, not a reconstruction.

    The previous harness re-parsed the English block line by line and swallowed
    function-entry parse failures, so it could assert strings the shipped
    runtime never emits. This pins the shipped catalog: every locale block in
    static/i18n.js is loaded, and function-valued entries are invoked.
    """
    body = r"""
process.stdout.write(JSON.stringify({
  locales: __catalogLocales,
  forceLabel: t('update_force'),
  activeRunsLabel: t('update_force_active_runs', 2),
  singleRunLabel: t('update_force_active_runs', 1),
  missingKey: t('__no_such_key__'),
}));
"""
    result = _run_node(_build_vm(body))
    import re as _re

    # Locale keys may be bare (en) or quoted ('zh-Hant').
    shipped = [
        m.group(1) or m.group(2) or m.group(3)
        for m in _re.finditer(
            r"^  (?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_-]*)): \{",
            I18N,
            _re.MULTILINE,
        )
    ]
    assert result["locales"] == shipped, (
        "the sandbox catalog is not the shipped one: "
        f"{result['locales']!r} != {shipped!r}"
    )
    assert len(shipped) > 1, "the shipped catalog has only one locale?"
    assert result["forceLabel"] == "Force update", result
    # A function-valued entry, invoked with its argument by the real runtime.
    assert result["activeRunsLabel"] == "Update anyway (interrupts 2 runs)", result
    assert result["singleRunLabel"] == "Update anyway (interrupts 1 run)", result
    assert result["missingKey"] == "__no_such_key__", (
        f"the real missing-key behaviour changed: {result!r}"
    )
