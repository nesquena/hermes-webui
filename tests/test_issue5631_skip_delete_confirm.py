"""Spec for #5631 / #5649 — "Skip delete confirmation" setting.

Written before the port so the contract is fixed by measurement, not by the
implementation. The maintainer's 2026-10-29 review left five open items:

1. Vietnamese keys must live INSIDE the ``vi`` locale block.
2. The four Japanese ``wiki*`` values must keep master's Japanese text.
3. A session that owns a worktree keeps confirming even when the setting is on.
4. Tests must pin the ``False`` default, the save round-trip and both
   ``deleteSession()`` branches.
5. The batch-delete path must be decided and documented. Decision taken here:
   ``_deleteSelectedSessionsBatch()`` ALWAYS confirms — it is a multi-target
   destructive action whose dialog names how many sessions and worktrees are
   about to go, so the per-session setting does not reach it.
"""
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from tests._pytest_port import BASE


REPO_ROOT = Path(__file__).resolve().parents[1]
I18N_JS_PATH = REPO_ROOT / "static" / "i18n.js"
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
INDEX_HTML_PATH = REPO_ROOT / "static" / "index.html"
NODE = shutil.which("node")

LABEL_KEY = "settings_label_skip_delete_confirm"
DESC_KEY = "settings_desc_skip_delete_confirm"
SETTING_KEY = "skip_delete_confirm"

TOP_LEVEL_LOCALES = ("en", "it", "ja", "ru", "es", "de", "zh", "pt", "ko", "fr", "cs", "tr", "pl", "vi")

JAPANESE_WIKI_VALUES = {
    "wiki_browse": "Wikiを閲覧",
    "wiki_search_placeholder": "ページを検索...",
    "wiki_no_pages": "Wikiページが見つかりません",
    "wiki_not_configured": "Wikiが設定されていません",
}


def _post(path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        payload = e.read()
        return json.loads(payload or b"{}"), e.code


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read()), r.status


def _extract_locale_block(src, locale_key):
    start_match = re.search(rf"\b{re.escape(locale_key)}\s*:\s*\{{", src)
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


def test_setting_is_registered_with_false_default_and_boolean_coercion():
    from api import config as app_config

    assert SETTING_KEY in app_config._SETTINGS_DEFAULTS, (
        "skip_delete_confirm must be registered in the settings defaults so it saves and reads back"
    )
    assert app_config._SETTINGS_DEFAULTS[SETTING_KEY] is False, (
        "the existing confirm dialog must stay the default behaviour"
    )
    assert SETTING_KEY in app_config._SETTINGS_BOOL_KEYS, (
        "skip_delete_confirm must be coerced to a real boolean, not stored as a truthy string"
    )


def test_setting_round_trips_through_the_real_settings_api():
    settings, status = _get("/api/settings")
    assert status == 200
    settings = settings.get("settings", settings)
    assert settings.get(SETTING_KEY) in (False, None), "default must not enable the skip"

    saved, status = _post("/api/settings", {SETTING_KEY: True})
    assert status == 200, saved
    settings, _ = _get("/api/settings")
    settings = settings.get("settings", settings)
    assert settings.get(SETTING_KEY) is True, "the toggle must persist as a real boolean"

    saved, status = _post("/api/settings", {SETTING_KEY: False})
    assert status == 200, saved
    settings, _ = _get("/api/settings")
    settings = settings.get("settings", settings)
    assert settings.get(SETTING_KEY) is False, "turning the toggle back off must persist as False"


def test_settings_panel_wires_the_toggle_read_save_and_restore():
    index_html = INDEX_HTML_PATH.read_text(encoding="utf-8")
    assert 'id="settingsSkipDeleteConfirm"' in index_html, "the Appearance panel needs the checkbox"
    assert LABEL_KEY in index_html and DESC_KEY in index_html, "both i18n keys must be used by the panel"

    panels = PANELS_JS_PATH.read_text(encoding="utf-8")
    payload = _function_body(panels, "_appearancePayloadFromUi")
    assert re.search(rf"{SETTING_KEY}\s*:\s*!!\s*\(\$\('settingsSkipDeleteConfirm'\)", payload), (
        "_appearancePayloadFromUi() must save the checkbox value"
    )
    loader = _function_body(panels, "loadSettingsPanel")
    assert "settingsSkipDeleteConfirm" in loader, "loadSettingsPanel() must restore the checkbox"
    assert "_skipDeleteConfirm" in loader, "the loaded setting must reach the runtime flag"


def _function_body(src, name):
    marker = re.search(rf"(^|\n)(?:async\s+)?function\s+{re.escape(name)}\(", src)
    assert marker is not None, f"{name}() not found"
    start = marker.start()
    brace = src.find("{", start)
    depth = 1
    i = brace + 1
    while i < len(src) and depth > 0:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name}() braces unbalanced"
    return src[start:i]


def test_new_keys_exist_in_every_locale_block():
    src = I18N_JS_PATH.read_text(encoding="utf-8")
    for locale in TOP_LEVEL_LOCALES:
        block = _extract_locale_block(src, locale)
        assert f"{LABEL_KEY}:" in block, f"{locale}: {LABEL_KEY} missing from its locale block"
        assert f"{DESC_KEY}:" in block, f"{locale}: {DESC_KEY} missing from its locale block"
    # zh-Hant lives nested inside the zh block, so the total number of locale
    # _lang markers is the honest count of translated bundles.
    assert src.count("_lang: '") >= 15, "expected every shipped locale to carry the two new keys"
    assert src.count(f"{LABEL_KEY}:") >= 15, "the label key must be translated in all bundles"
    assert src.count(f"{DESC_KEY}:") >= 15, "the description key must be translated in all bundles"


def test_japanese_wiki_values_keep_masters_japanese_text():
    block = _extract_locale_block(I18N_JS_PATH.read_text(encoding="utf-8"), "ja")
    for key, expected in JAPANESE_WIKI_VALUES.items():
        match = re.search(rf"{re.escape(key)}\s*:\s*'([^']*)'", block)
        assert match, f"ja: {key} missing"
        assert match.group(1) == expected, f"ja: {key} drifted from master's Japanese value"


_DRIVER_SRC = r"""
const fs = require('fs');

function extractFunction(src, signature) {
  const start = src.indexOf(signature);
  if (start < 0) throw new Error(signature + ' not found');
  let depth = 0;
  const bodyStart = src.indexOf('{', src.indexOf(')', start));
  for (let i = bodyStart; i < src.length; i++) {
    const ch = src[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(signature + ' body not closed');
}

function extractFunctionIfPresent(src, signature) {
  return src.indexOf(signature) >= 0 ? extractFunction(src, signature) : '';
}

const src = fs.readFileSync(process.argv[2], 'utf8');
const args = JSON.parse(process.argv[3]);

const dialogCalls = [];
const toasts = [];
let deleteRequested = false;
let sessionsRequested = false;

function makeElement(tag) {
  return {
    tagName: String(tag || 'div').toUpperCase(),
    children: [],
    className: '',
    style: {},
    textContent: '',
    appendChild(child) { this.children.push(child); return child; },
    querySelectorAll() { return []; },
    set innerHTML(value) { this._innerHTML = value; if (value === '') this.children = []; },
    get innerHTML() { return this._innerHTML || ''; },
  };
}

const elements = {
  msgInner: makeElement('div'),
  emptyState: makeElement('div'),
  fileTree: makeElement('div'),
  topbarTitle: makeElement('div'),
  topbarMeta: makeElement('div'),
  batchActionBar: makeElement('div'),
};

globalThis.window = globalThis;
globalThis.$ = id => elements[id] || null;
globalThis.document = {createElement: makeElement};
globalThis.S = {session: {session_id: 'active-session'}, messages: [], entries: []};
globalThis.localStorage = {removeItem() {}};
globalThis._skipDeleteConfirm = !!args.skip;
globalThis._composerModelPickHost = globalThis;
globalThis._composerModelPick = null;
globalThis._emptyComposerModelOverrideHost = globalThis;
globalThis._emptyComposerModelOverride = null;
globalThis.showConfirmDialog = async opts => {
  dialogCalls.push(String(opts && opts.message || ''));
  return !!args.dialog;
};
globalThis._sessionSnapshotById = () => ({
  session_id: 'active-session',
  worktree_path: args.worktreePath || undefined,
});
globalThis._worktreeSessionCount = () => (args.worktreePath ? 1 : 0);
globalThis._worktreeResponseCount = () => 0;
globalThis._captureSessionReflowPositions = () => null;
globalThis._clearHandoffStorageForSession = () => {};
globalThis._clearPersistedSessionQueue = () => {};
globalThis._optimisticallyRemoveSessionFromList = () => {};
globalThis._optimisticallyRemovedSessionIds = new Set();
globalThis._pendingSessionReflowPositions = null;
globalThis._allSessions = [{session_id: 'active-session'}];
globalThis._selectedSessions = new Set(['active-session']);
globalThis._hydrateTodosFromSession = () => {};
globalThis._sessionListQueryString = () => '';
globalThis.assistantDisplayName = () => 'Hermes';
globalThis.syncAppTitlebar = () => {};
globalThis.setStatus = () => {};
globalThis.a11yTreeReset = () => {};
globalThis.renderSessionList = async () => {};
globalThis._repaintSidebarAfterSessionCompletion = () => {};
globalThis._updateBatchActionBar = () => {};
globalThis._forgetComposerDraftLocalFallback = () => {};
globalThis._sessionDeleteErrorMessage = err => String(err && err.message || err || 'delete failed');
globalThis._sessionResponseRetainsWorktree = () => false;
globalThis.showToast = msg => toasts.push(String(msg));
globalThis.t = (key, ...rest) => (rest.length ? key + ':' + rest.join(',') : key);
globalThis.api = async url => {
  if (url === '/api/session/delete') {
    deleteRequested = true;
    return {};
  }
  if (url.startsWith('/api/sessions')) {
    sessionsRequested = true;
    return {sessions: []};
  }
  return {};
};

const readComposerModelPickSrc = extractFunctionIfPresent(src, 'function _readComposerModelPick(');
if (readComposerModelPickSrc) eval(readComposerModelPickSrc);
eval(extractFunction(src, 'async function deleteSession('));
eval(extractFunction(src, 'async function _deleteSelectedSessionsBatch('));

async function settle(promise) {
  const result = {value: undefined, error: null};
  try {
    result.value = await promise;
  } catch (error) {
    result.error = String(error && error.message || error);
  }
  return result;
}

(async () => {
  const deletion = args.mode === 'batch'
    ? _deleteSelectedSessionsBatch()
    : deleteSession('active-session');
  const settled = await settle(deletion);
  process.stdout.write(JSON.stringify({
    mode: args.mode,
    dialogCalls: dialogCalls.length,
    dialogMessages: dialogCalls,
    deleteRequested,
    sessionsRequested,
    result: settled.value,
    threw: settled.error,
    toasts,
  }));
})();
"""


def _run_driver(skip, worktree_path, dialog, mode):
    assert NODE, "node is required for this browser-side spec"
    args = json.dumps({
        "skip": skip,
        "worktreePath": worktree_path,
        "dialog": dialog,
        "mode": mode,
    })
    proc = subprocess.run(
        [NODE, "-", str(SESSIONS_JS_PATH), args],
        input=_DRIVER_SRC,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout)


def test_delete_session_with_setting_off_confirms_and_honours_cancellation():
    accepted = _run_driver(skip=False, worktree_path=None, dialog=True, mode="single")
    assert accepted["dialogCalls"] == 1, "with the setting off the dialog must still be shown"
    assert accepted["deleteRequested"] is True, "an accepted dialog must send the delete request"

    cancelled = _run_driver(skip=False, worktree_path=None, dialog=False, mode="single")
    assert cancelled["dialogCalls"] == 1
    assert cancelled["deleteRequested"] is False, "cancelling must not delete anything"


def test_delete_session_with_setting_on_skips_the_dialog_but_still_deletes():
    result = _run_driver(skip=True, worktree_path=None, dialog=True, mode="single")
    assert result["dialogCalls"] == 0, "with the setting on the per-session dialog must not appear"
    assert result["deleteRequested"] is True, "the delete request must still be sent"


def test_delete_session_keeps_confirming_when_the_session_owns_a_worktree():
    cancelled = _run_driver(skip=True, worktree_path="/tmp/wt-owned", dialog=False, mode="single")
    assert cancelled["dialogCalls"] == 1, (
        "the worktree dialog names the path being removed, so it must survive the setting"
    )
    assert cancelled["dialogMessages"][0].startswith("session_delete_worktree_confirm"), (
        "the surviving dialog must be the worktree-specific one"
    )
    assert cancelled["deleteRequested"] is False, "cancelling the worktree dialog must not delete"


def test_batch_delete_always_confirms_even_with_the_setting_on():
    result = _run_driver(skip=True, worktree_path=None, dialog=False, mode="batch")
    assert result["dialogCalls"] == 1, (
        "batch delete stays confirmation-gated: the setting covers single-session deletes only"
    )
    assert result["deleteRequested"] is False, "a cancelled batch confirmation must delete nothing"
