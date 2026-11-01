"""Node harness for the #7293 active-runs force-update offer in static/ui.js.

The server refuses a WebUI-initiated update while WebUI agent runs are active
(``restart_blocked`` + ``force_available``). The banner must then offer ONE
explicit, confirmed override that re-sends the same request with
``force_active_runs: true``; cancelling the confirmation must send nothing.

The real ``applyUpdates`` / ``applyClearUpdateLock`` / ``forceUpdate`` /
``_showUpdateError`` / ``_renderActiveRunsForceOffer`` sources run in Node with
the real ``static/i18n.js`` loaded (so the English labels come from the locale
table), against a minimal DOM whose ``textContent`` setter drops children like
a browser does. Only the network (``api``), the confirm dialog and the
post-update reload are stubbed.
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
I18N_JS = ROOT / "static" / "i18n.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node required for the ui.js harness")

_FUNCTIONS = (
    ("function", "_i18nUpdateText"),
    ("async function", "applyUpdates"),
    ("function", "_showUpdateError"),
    ("function", "_activeRunsUpdateText"),
    ("function", "_renderActiveRunsForceOffer"),
    ("async function", "applyClearUpdateLock"),
    ("async function", "forceUpdate"),
)

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const scenario = JSON.parse(process.argv[1]);
const i18nSrc = fs.readFileSync(process.argv[2], 'utf8');
const uiSnippet = fs.readFileSync(process.argv[3], 'utf8');

const root = { id: '__root__', children: [], parentNode: null };
function makeEl(tag, id) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(), id: id || '', type: '', className: '',
    style: {}, dataset: {}, children: [], parentNode: null, disabled: false, onclick: null,
    _text: '',
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
    get() { let n = el; while (n) { if (n === root) return true; n = n.parentNode; } return false; },
  });
  el.appendChild = (c) => {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = el; el.children.push(c); return c;
  };
  el.removeChild = (c) => { el.children = el.children.filter((x) => x !== c); c.parentNode = null; return c; };
  el.remove = () => { if (el.parentNode) el.parentNode.removeChild(el); };
  return el;
}
root.removeChild = (c) => { root.children = root.children.filter((x) => x !== c); c.parentNode = null; return c; };
function mount(el) { el.parentNode = root; root.children.push(el); return el; }

const errorEl = mount(makeEl('div', 'updateError'));
errorEl.style.display = 'none';
const applyBtn = mount(makeEl('button', 'btnApplyUpdate'));
applyBtn.textContent = 'Update Now';
const forceBtn = mount(makeEl('button', 'btnForceUpdate'));
forceBtn.textContent = 'Force update';
forceBtn.dataset.target = scenario.target;
const clearLockBtn = mount(makeEl('button', 'btnClearUpdateLock'));
clearLockBtn.textContent = 'Clear lock and retry update';
clearLockBtn.dataset.target = scenario.target;

function findById(node, id) {
  for (const c of node.children || []) {
    if (c.id === id) return c;
    const hit = findById(c, id);
    if (hit) return hit;
  }
  return null;
}
function allById(node, id, out) {
  out = out || [];
  for (const c of node.children || []) {
    if (c.id === id) out.push(c);
    allById(c, id, out);
  }
  return out;
}

const apiCalls = [];
const confirmCalls = [];
const confirmAnswers = (scenario.confirmAnswers || []).slice();
const waitCalls = [];
const toasts = [];
const caught = [];
const responses = scenario.responses.slice();

const ctx = {
  console,
  navigator: { languages: ['en-US'], language: 'en-US' },
  localStorage: { getItem() { return null; }, setItem() {} },
  sessionStorage: { removed: [], removeItem(k) { this.removed.push(k); } },
  document: {
    documentElement: {},
    readyState: 'complete',
    addEventListener() {},
    querySelectorAll() { return []; },
    createElement: (tag) => makeEl(tag),
  },
  $: (id) => findById(root, id),
  api: async (url, opts) => {
    apiCalls.push({ url, body: JSON.parse(opts.body) });
    if (!responses.length) throw new Error('unexpected extra api call to ' + url);
    return responses.shift();
  },
  showConfirmDialog: async (opts) => {
    confirmCalls.push(opts);
    return confirmAnswers.length ? confirmAnswers.shift() : false;
  },
  showToast: (message) => { toasts.push(String(message)); },
  _readHealthServerIdentity: async () => 'baseline-id',
  _waitForServerThenReload: (opts) => { waitCalls.push(opts || {}); },
  _formatUpdateApplyExceptionMessage: (e) => { caught.push(String((e && e.stack) || e)); return 'Update failed: ' + e.message; },
  _isUpdateApplyNetworkError: () => false,
  _renderLockManualInstruction: () => { caught.push('unexpected lock manual instruction'); },
  setTimeout: (cb) => { cb(); return 1; },
  clearTimeout: () => {},
};
ctx.window = ctx;
ctx._updateData = scenario.updateData || {};
ctx._updateApplyInFlight = false;
vm.createContext(ctx);
vm.runInContext(i18nSrc, ctx);
vm.runInContext(uiSnippet, ctx);

function offerState() {
  const offers = allById(root, 'btnForceUpdateActiveRuns');
  const offer = offers[0] || null;
  return {
    count: offers.length,
    label: offer ? offer.textContent : null,
    target: offer ? offer.dataset.target : null,
    disabled: offer ? offer.disabled : null,
    connected: offer ? offer.isConnected : null,
    insideError: offer ? findById(errorEl, 'btnForceUpdateActiveRuns') === offer : null,
  };
}

(async () => {
  const entry = {
    apply: () => ctx.applyUpdates(),
    clear_lock: () => ctx.applyClearUpdateLock(clearLockBtn),
    force: () => ctx.forceUpdate(forceBtn),
  }[scenario.entry];
  for (let i = 0; i < (scenario.entryRepeats || 1); i += 1) await entry();
  const afterRefusal = {
    offer: offerState(),
    errorText: errorEl._text,
    errorDisplay: errorEl.style.display,
    apiCalls: apiCalls.length,
  };
  let afterClick = null;
  if (scenario.clickOffer) {
    const offer = findById(root, 'btnForceUpdateActiveRuns');
    if (!offer) throw new Error('no force offer to click');
    await offer.onclick();
    afterClick = { offer: offerState() };
  }
  console.log(JSON.stringify({
    afterRefusal, afterClick, apiCalls,
    confirmCalls: confirmCalls.map((c) => ({
      title: c.title, message: c.message, confirmLabel: c.confirmLabel,
      danger: !!c.danger, focusCancel: !!c.focusCancel,
    })),
    waitCalls: waitCalls.length, toasts, caught,
    applyInFlight: !!ctx._updateApplyInFlight,
  }));
})().catch((error) => { console.error(error.stack || String(error)); process.exit(1); });
"""


def _ui_snippet() -> str:
    src = UI_JS.read_text(encoding="utf-8")
    return "\n".join(extract_function(src, name, prefix=prefix) for prefix, name in _FUNCTIONS)


def _run(scenario: dict, tmp_path: Path) -> dict:
    snippet = tmp_path / "ui_snippet.js"
    snippet.write_text(_ui_snippet(), encoding="utf-8")
    result = subprocess.run(
        [NODE, "-e", _HARNESS, json.dumps(scenario), str(I18N_JS), str(snippet)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"node harness failed: {result.stderr or result.stdout}"
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["caught"] == [], out["caught"]
    return out


def _refusal(active_runs: int, **extra) -> dict:
    payload = {
        "ok": False,
        "message": f"Cannot update webui while {active_runs} WebUI agent runs are active.",
        "target": "webui",
        "restart_blocked": True,
        "force_available": True,
        "active_runs": active_runs,
        "active_streams": active_runs,
    }
    payload.update(extra)
    return payload


_WEBUI_BEHIND = {"webui": {"behind": 1, "channel": "stable"}}


def test_refusal_renders_force_offer_with_active_run_count(tmp_path):
    out = _run(
        {"entry": "apply", "target": "webui", "updateData": _WEBUI_BEHIND, "responses": [_refusal(2)]},
        tmp_path,
    )
    refusal = out["afterRefusal"]
    assert refusal["offer"] == {
        "count": 1,
        "label": "Update anyway (interrupts 2 runs)",
        "target": "webui",
        "disabled": False,
        "connected": True,
        "insideError": True,
    }
    # The offer is appended AFTER the error text, so the refusal stays visible.
    assert refusal["errorText"].startswith("Update failed (webui): Cannot update webui")
    assert refusal["errorDisplay"] == "block"
    assert out["apiCalls"] == [{"url": "/api/updates/apply", "body": {"target": "webui", "channel": "stable"}}]
    assert out["confirmCalls"] == []
    assert out["waitCalls"] == 0
    assert out["applyInFlight"] is False


def test_single_run_label_is_singular(tmp_path):
    out = _run(
        {"entry": "apply", "target": "webui", "updateData": _WEBUI_BEHIND, "responses": [_refusal(1)]},
        tmp_path,
    )
    assert out["afterRefusal"]["offer"]["label"] == "Update anyway (interrupts 1 run)"


def test_confirming_the_offer_resends_with_force_active_runs(tmp_path):
    out = _run(
        {
            "entry": "apply",
            "target": "webui",
            "updateData": _WEBUI_BEHIND,
            "responses": [_refusal(2), {"ok": True, "target": "webui", "restart_scheduled": True}],
            "clickOffer": True,
            "confirmAnswers": [True],
        },
        tmp_path,
    )
    assert out["apiCalls"] == [
        {"url": "/api/updates/apply", "body": {"target": "webui", "channel": "stable"}},
        {
            "url": "/api/updates/apply",
            "body": {"target": "webui", "channel": "stable", "force_active_runs": True},
        },
    ]
    assert out["confirmCalls"] == [
        {
            "title": "Interrupt active runs?",
            "message": (
                "2 WebUI agent runs are still working. Updating now restarts the server and "
                "stops them; each one is saved as interrupted. Wait for them to finish if you can."
            ),
            "confirmLabel": "Interrupt and update",
            "danger": True,
            "focusCancel": True,
        }
    ]
    # The forced re-send goes through the normal success path (toast + reload wait).
    assert out["waitCalls"] == 1
    assert "Update applied — restarting…" in out["toasts"]


def test_cancelling_the_offer_sends_nothing(tmp_path):
    out = _run(
        {
            "entry": "apply",
            "target": "webui",
            "updateData": _WEBUI_BEHIND,
            "responses": [_refusal(3)],
            "clickOffer": True,
            "confirmAnswers": [False],
        },
        tmp_path,
    )
    assert len(out["confirmCalls"]) == 1
    assert out["apiCalls"] == [{"url": "/api/updates/apply", "body": {"target": "webui", "channel": "stable"}}]
    assert out["waitCalls"] == 0
    assert out["toasts"] == []
    # The offer stays available and usable after a cancel.
    assert out["afterClick"]["offer"] == {
        "count": 1,
        "label": "Update anyway (interrupts 3 runs)",
        "target": "webui",
        "disabled": False,
        "connected": True,
        "insideError": True,
    }


@pytest.mark.parametrize(
    "response",
    [
        _refusal(2, force_available=False),
        _refusal(2, restart_blocked=False),
        {"ok": False, "message": "Update already in progress"},
    ],
    ids=["no-force-available", "not-restart-blocked", "plain-failure"],
)
def test_no_offer_without_restart_blocked_and_force_available(tmp_path, response):
    out = _run(
        {"entry": "apply", "target": "webui", "updateData": _WEBUI_BEHIND, "responses": [response]},
        tmp_path,
    )
    assert out["afterRefusal"]["offer"]["count"] == 0
    assert len(out["apiCalls"]) == 1


def test_repeated_refusals_keep_a_single_offer(tmp_path):
    out = _run(
        {
            "entry": "apply",
            "target": "webui",
            "updateData": _WEBUI_BEHIND,
            "responses": [_refusal(2), _refusal(4)],
            "entryRepeats": 2,
        },
        tmp_path,
    )
    assert out["afterRefusal"]["offer"]["count"] == 1
    assert out["afterRefusal"]["offer"]["label"] == "Update anyway (interrupts 4 runs)"


def test_clear_lock_refusal_offer_resends_clear_lock_with_force(tmp_path):
    out = _run(
        {
            "entry": "clear_lock",
            "target": "webui",
            "responses": [_refusal(1), {"ok": True}],
            "clickOffer": True,
            "confirmAnswers": [True],
        },
        tmp_path,
    )
    assert out["afterRefusal"]["offer"]["label"] == "Update anyway (interrupts 1 run)"
    assert out["apiCalls"] == [
        {"url": "/api/updates/clear_lock", "body": {"target": "webui"}},
        {"url": "/api/updates/clear_lock", "body": {"target": "webui", "force_active_runs": True}},
    ]
    assert len(out["confirmCalls"]) == 1
    assert out["waitCalls"] == 1


@pytest.mark.parametrize("interrupt_confirmed", [True, False])
def test_force_update_refusal_offer_does_not_repeat_discard_confirmation(tmp_path, interrupt_confirmed):
    out = _run(
        {
            "entry": "force",
            "target": "webui",
            "updateData": _WEBUI_BEHIND,
            "responses": [_refusal(2), {"ok": True}],
            "clickOffer": True,
            # 1st: the existing "discard local changes" confirm; 2nd: the
            # active-runs interrupt confirm from the offer.
            "confirmAnswers": [True, interrupt_confirmed],
        },
        tmp_path,
    )
    titles = [c["title"] for c in out["confirmCalls"]]
    assert titles == ["Force update webui?", "Interrupt active runs?"]
    first = {"url": "/api/updates/force", "body": {"target": "webui", "channel": "stable"}}
    if interrupt_confirmed:
        assert out["apiCalls"] == [
            first,
            {
                "url": "/api/updates/force",
                "body": {"target": "webui", "channel": "stable", "force_active_runs": True},
            },
        ]
        assert out["waitCalls"] == 1
    else:
        assert out["apiCalls"] == [first]
        assert out["waitCalls"] == 0
