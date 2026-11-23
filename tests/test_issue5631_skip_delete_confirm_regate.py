"""Spec for the 2026-11-23 review of #9145 — "Skip delete confirmation".

Two blocking items from the maintainer's review of ``ae610bd84``:

1. **The opt-in is lost on reload.** ``static/boot.js`` restores the sibling
   Appearance flags while applying ``/api/settings`` at boot but never writes
   ``window._skipDeleteConfirm``, so after a reload ``deleteSession()`` sees
   ``undefined`` and the dialog comes back until Settings is opened.
2. **An older save response can turn skipping back on.** Appearance saves are
   serialised, and ``_autosaveAppearanceSettings()`` writes the flag from every
   response. If the user unchecks the box while an earlier save carrying
   ``true`` is still in flight, that response re-enables the skip for about one
   round-trip — fail-open on a destructive action.

Item 3 of the review (smaller) is test-only and pinned here as well: the
``zh-Hant`` bundle is its own top-level locale block (the previous test claimed
it sat nested inside ``zh`` and skipped it), so the per-block coverage check
has to include it.

The behavioural cases drive the real ``_appearancePayloadFromUi()``,
``_scheduleAppearanceAutosave()`` and ``_autosaveAppearanceSettings()`` bodies
extracted from ``static/panels.js``; only the seams (settings POST, DOM, status
slot, unrelated appliers) are stubbed.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
I18N_JS_PATH = REPO_ROOT / "static" / "i18n.js"
NODE = shutil.which("node")

LABEL_KEY = "settings_label_skip_delete_confirm"
DESC_KEY = "settings_desc_skip_delete_confirm"


def _boot_src():
    return BOOT_JS_PATH.read_text(encoding="utf-8")


def _panels_src():
    return PANELS_JS_PATH.read_text(encoding="utf-8")


def test_boot_restores_the_skip_delete_confirm_mirror():
    """The boot settings-apply block must mirror the flag next to its siblings."""
    src = _boot_src()
    sibling = "window._projectQuickCreate=!!s.project_quick_create_buttons;"
    assert sibling in src, "the boot block that mirrors the Appearance flags moved"

    start = src.index(sibling)
    # Same neighbourhood as the other Appearance mirrors applied from `s`
    # (the flags live in one contiguous block inside the boot IIFE).
    window = src[max(0, start - 600) : start + 600]
    assert "window._skipDeleteConfirm=!!s.skip_delete_confirm;" in window, (
        "boot.js must restore window._skipDeleteConfirm from the settings payload, "
        "otherwise the saved opt-in is lost after a reload until Settings is opened"
    )
    # The restore must be driven by the server payload, never a hardcoded literal.
    assert "window._skipDeleteConfirm=true" not in src
    assert "window._skipDeleteConfirm=false" not in src


_DRIVER_SRC = r"""
const fs = require('fs');
const vm = require('vm');

function extractFunction(src, signature) {
  const start = src.indexOf(signature);
  if (start < 0) throw new Error(signature + ' not found');
  let i = src.indexOf('{', start + signature.length - 1);
  let depth = 0;
  let inSingle = false, inDouble = false, inBacktick = false, escape = false;
  for (; i < src.length; i++) {
    const ch = src[i];
    if (escape) { escape = false; continue; }
    if (inSingle) { if (ch === '\\') escape = true; else if (ch === "'") inSingle = false; continue; }
    if (inDouble) { if (ch === '\\') escape = true; else if (ch === '"') inDouble = false; continue; }
    if (inBacktick) { if (ch === '\\') escape = true; else if (ch === '`') inBacktick = false; continue; }
    if (ch === "'") inSingle = true;
    else if (ch === '"') inDouble = true;
    else if (ch === '`') inBacktick = true;
    else if (ch === '{') depth++;
    else if (ch === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error(signature + ' unbalanced');
}

const panels = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');

const payloadSrc = extractFunction(panels, 'function _appearancePayloadFromUi(');
const scheduleSrc = extractFunction(panels, 'function _scheduleAppearanceAutosave(');
const autosaveSrc = extractFunction(panels, 'async function _autosaveAppearanceSettings(');

const results = [];

function tick(n) {
  // Let the awaited chains settle: each `await` is a microtask hop and each
  // queued autosave adds its own; 50 is far beyond what the bodies need.
  return (async () => {
    for (let i = 0; i < n; i++) await Promise.resolve();
  })();
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

async function runScenario(name, steps) {
  // A fresh context per scenario: `_settingsAppearanceAutosaveTimer` and the
  // generation/ownership state must not leak between cases.
  const window = {};
  const checkbox = { checked: false };
  const el = { textContent: '', innerHTML: '', className: '', classList: { add() {}, contains() { return false; } } };
  const localStorage = {
    _v: {},
    getItem(k) { return Object.prototype.hasOwnProperty.call(this._v, k) ? this._v[k] : null; },
    setItem(k, v) { this._v[k] = String(v); },
  };

  const posts = [];
  const sandbox = {
    window,
    localStorage,
    console: { warn() {}, log() {} },
    JSON,
    Object,
    Array,
    String,
    Number,
    Boolean,
    Promise,
    setTimeout: (fn) => { fn(); return 1; },
    clearTimeout: () => {},
    $: (id) => (id === 'settingsSkipDeleteConfirm' ? checkbox : el),
    t: (k) => k,
    esc: (s) => String(s),
    api: async () => { throw new Error('api() must not be reached directly'); },
    // Seams: the settings POST that the real code serialises through.
    _enqueueSettingsPost: (options) => {
      const d = deferred();
      posts.push({ options, deferred: d });
      return d.promise;
    },
    _setAppearanceAutosaveStatus: () => {},
    _rememberAppearanceSaved: () => {},
    _applyStructuredCodeViewSettings: () => {},
    _syncStructuredCodeLinesEnabled: () => {},
    _ensureComposerControlVisibilityState: () => {},
    _setComposerControlOrder: () => [],
    _renderComposerControlChips: () => {},
    _renderComposerControlSituationalChips: () => {},
    _applyComposerFooterVisibilitySettings: () => {},
    _syncChatActivityDisplayModeControl: () => {},
    _syncTransparentEventTimestampsControl: () => {},
    _applySessionNavigationPrefs: () => {},
    _persistAutoScrollFollow: (v) => v,
    _structuredCodeViewFromUi: () => ({}),
    _composerControlVisibilityPayload: () => ({}),
    _getComposerControlOrder: () => [],
    _getHiddenTabs: () => [],
    _getTabOrder: () => [],
    clearMessageRenderCache: () => {},
    renderMessages: () => {},
    // Module state the extracted bodies read and write.
    _settingsAppearanceAutosaveTimer: null,
    _settingsAppearanceAutosaveRetryPayload: null,
    _settingsThemeOnOpen: 'dark',
    _settingsSkinOnOpen: 'default',
    _settingsFontSizeOnOpen: 'default',
    _appearanceSaveGeneration: 0,
    // Module state a generation-token implementation may keep beside the counter;
    // declared here so the extracted bodies run in isolation (the harness only
    // provides the slot, the bodies decide what to write into it).
    _appearanceSaveGenerationByPayload: new WeakMap(),
    WeakMap,
  };
  sandbox.globalThis = sandbox;
  const context = vm.createContext(sandbox);
  vm.runInContext(payloadSrc + '\n' + scheduleSrc + '\n' + autosaveSrc, context);

  const api = {
    context,
    checkbox,
    posts,
    window,
    async settle() { await tick(60); },
  };
  try {
    const outcome = await steps(api);
    results.push({ name, ok: true, ...outcome });
  } catch (e) {
    results.push({ name, ok: false, error: String((e && e.stack) || e) });
  }
}

(async () => {
  // Control 1: a single save whose response is the newest one still applies the
  // server's value, so the skip turns on and off as before.
  await runScenario('newest-response-is-authoritative', async ({ context, checkbox, posts, window, settle }) => {
    checkbox.checked = true;
    context._scheduleAppearanceAutosave();
    await settle();
    if (posts.length !== 1) throw new Error('expected exactly 1 queued save, got ' + posts.length);
    posts[0].deferred.resolve({ skip_delete_confirm: true });
    await settle();
    if (window._skipDeleteConfirm !== true) throw new Error('newest response did not apply the opt-in');

    checkbox.checked = false;
    context._scheduleAppearanceAutosave();
    await settle();
    posts[1].deferred.resolve({ skip_delete_confirm: false });
    await settle();
    if (window._skipDeleteConfirm !== false) throw new Error('newest response did not clear the opt-in');
    return { detail: 'true then false' };
  });

  // Defect: unchecking while an earlier save carrying `true` is still in flight
  // must not re-enable the skip when that older response lands.
  await runScenario('stale-response-cannot-reenable', async ({ context, checkbox, posts, window, settle }) => {
    checkbox.checked = true;
    context._scheduleAppearanceAutosave();
    await settle();
    if (posts.length !== 1) throw new Error('expected 1 queued save, got ' + posts.length);

    // User unchecks before the first round-trip completes. The real checkbox
    // onchange in loadSettingsPanel() mirrors `this.checked` into the global
    // immediately, so the harness models the user action the same way.
    checkbox.checked = false;
    window._skipDeleteConfirm = false;
    context._scheduleAppearanceAutosave();
    await settle();
    if (posts.length !== 2) throw new Error('expected 2 queued saves, got ' + posts.length);

    // The older save (payload carried `true`) answers first.
    posts[0].deferred.resolve({ skip_delete_confirm: true });
    await settle();
    const afterStale = window._skipDeleteConfirm;
    if (afterStale !== false) {
      throw new Error('a stale save response re-enabled the skip (fail-open): ' + String(afterStale));
    }

    posts[1].deferred.resolve({ skip_delete_confirm: false });
    await settle();
    if (window._skipDeleteConfirm !== false) throw new Error('final state must stay off');
    return { detail: 'stale true ignored, newest false applied' };
  });

  // Symmetric window: checking the box while an earlier `false` save is in
  // flight must not drop the user's new choice when that older response lands.
  await runScenario('stale-response-cannot-clear', async ({ context, checkbox, posts, window, settle }) => {
    checkbox.checked = false;
    context._scheduleAppearanceAutosave();
    await settle();
    checkbox.checked = true;
    // Same onchange mirror as above, for the direction that must not be cleared.
    window._skipDeleteConfirm = true;
    context._scheduleAppearanceAutosave();
    await settle();
    if (posts.length !== 2) throw new Error('expected 2 queued saves, got ' + posts.length);

    posts[0].deferred.resolve({ skip_delete_confirm: false });
    await settle();
    if (window._skipDeleteConfirm !== true) {
      throw new Error('a stale save response cleared the user new choice: ' + String(window._skipDeleteConfirm));
    }
    posts[1].deferred.resolve({ skip_delete_confirm: true });
    await settle();
    if (window._skipDeleteConfirm !== true) throw new Error('final state must stay on');
    return { detail: 'stale false ignored, newest true applied' };
  });

  console.log(JSON.stringify(results));
})().catch((e) => {
  console.log(JSON.stringify([{ name: 'driver', ok: false, error: String((e && e.stack) || e) }]));
});
"""


def _run_panels_driver():
    assert NODE, "node is required to drive the real panels.js bodies"
    proc = subprocess.run(
        [NODE, "-e", _DRIVER_SRC, str(PANELS_JS_PATH)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert proc.returncode == 0, f"node driver failed:\n{proc.stderr}"
    payload = proc.stdout.strip().splitlines()[-1]
    return json.loads(payload)


def test_newest_appearance_response_still_applies_the_opt_in():
    results = {r["name"]: r for r in _run_panels_driver()}
    case = results.get("newest-response-is-authoritative")
    assert case, results
    assert case["ok"], case.get("error")


def test_a_stale_appearance_response_cannot_reenable_the_skip():
    results = {r["name"]: r for r in _run_panels_driver()}
    case = results.get("stale-response-cannot-reenable")
    assert case, results
    assert case["ok"], case.get("error")


def test_a_stale_appearance_response_cannot_clear_the_skip():
    results = {r["name"]: r for r in _run_panels_driver()}
    case = results.get("stale-response-cannot-clear")
    assert case, results
    assert case["ok"], case.get("error")


def _extract_locale_block(src, locale_key):
    start_match = re.search(rf"(?m)^  '?{re.escape(locale_key)}'?\s*:\s*\{{", src)
    assert start_match, f"{locale_key} locale block not found"
    brace_start = start_match.end() - 1
    depth = 0
    in_single = in_double = in_backtick = False
    escape = False
    for i in range(brace_start, len(src)):
        ch = src[i]
        if escape:
            escape = False
            continue
        if in_single:
            if ch == "\\":
                escape = True
            elif ch == "'":
                in_single = False
            continue
        if in_double:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            continue
        if in_backtick:
            if ch == "\\":
                escape = True
            elif ch == "`":
                in_backtick = False
            continue
        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "`":
            in_backtick = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace_start : i + 1]
    raise AssertionError(f"{locale_key} block not closed")


def test_zh_hant_block_carries_the_new_keys_in_its_own_block():
    """`zh-Hant` is a top-level block (not nested inside `zh`), so check it directly."""
    src = I18N_JS_PATH.read_text(encoding="utf-8")
    block = _extract_locale_block(src, "zh-Hant")
    assert "_lang: 'zh-Hant'" in block
    assert f"{LABEL_KEY}:" in block, "zh-Hant: label key missing from its own block"
    assert f"{DESC_KEY}:" in block, "zh-Hant: description key missing from its own block"
