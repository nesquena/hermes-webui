"""#7679 round 2 — the four lifecycle repairs, driven through the REAL sources.

The re-gate re-ran the actual ``ui.js`` / ``panels.js`` / ``i18n.js`` composition
in a clean sandbox rather than relying on mocked contributor tests, and found the
grant/epoch machinery still had holes. Four of them:

1. **Retained Agent Force is visible but inert.** ``_showUpdateBanner`` retires
   every grant at the top of a render, then the manual-Agent preservation branch
   keeps the control without issuing a replacement. ``forceUpdate`` then finds no
   grant and returns silently — zero confirmations, zero POSTs, from a button
   that looks operable.

2. **Actual rejection and ``finally`` have no owner guard.** The success path
   gained an epoch guard last round; the ``catch`` that writes the status and the
   ``finally`` that restores the controls never did. An older rejected check
   overwrites a newer successful status, and either an older resolution or
   rejection re-enables Check and clears the spinner while the newer request is
   still pending.

3. **Invalid follow-up checks leave stale enabled Force.** The disabled / error /
   no-git branches do not reconcile the banner, so a dirty result leaves the
   previous render's Force visible and enabled with no grant behind it.

4. **No revalidation after the last await.** ``forceUpdate`` validates the grant,
   then awaits health and posts without checking again. A newer check completing
   during that await leaves the handler holding a snapshot it already proved
   stale, and the old handler still posts.

Plus three smaller items: dirty-only Settings said "Up to date" while the banner
above it said "Local changes detected"; ``t('force_no_longer_applicable', …)``
rendered the raw key because the real locale runtime has no such entry; and a
backend no-op (``{ok:true,up_to_date:true}``) announced a restart and ran the
restart waiter, leaving Force disabled on an install that never restarted.

These tests extract the real functions from ``static/ui.js`` and
``static/panels.js`` and evaluate the **real** ``t()`` from
``static/i18n.js`` — not a stub — so a missing locale key fails here instead of
reaching a user as a raw key name.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest


REPO = pathlib.Path(__file__).parent.parent
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

NODE_BIN = str(NODE)


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    match = re.search(rf"(async\s+)?function\s+{re.escape(name)}\b", src)
    assert match, f"{name}() not found"
    open_paren = src.index("(", match.start())
    depth = 1
    idx = open_paren + 1
    while depth > 0 and idx < len(src):
        ch = src[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        idx += 1
    brace = src.index("{", idx)
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[match.start() : i + 1]
    raise AssertionError(f"{name}() body was not balanced")


UI = _read("static/ui.js")
PANELS = _read("static/panels.js")
I18N = _read("static/i18n.js")

_GRANT_START = UI.index("let _updateCheckEpoch = 0;")
_GRANT_END = UI.index("function _showUpdateBanner(data,")
GRANT = UI[_GRANT_START:_GRANT_END]

FN_FORMAT = _extract_js_function(UI, "_formatUpdateTargetStatus")
FN_INSTRUCTION = _extract_js_function(UI, "_formatManualUpdateInstruction")
FN_ERROR = _extract_js_function(UI, "_formatUpdateCheckError")
FN_PREDICATE = _extract_js_function(UI, "_isForceCleanTarget")
FN_DIRTY = _extract_js_function(UI, "_formatUpdateDirtyStatus")
FN_SHOW = _extract_js_function(UI, "_showUpdateBanner")
FN_SHOW_ERROR = _extract_js_function(UI, "_showUpdateError")
FN_APPLY = _extract_js_function(UI, "applyUpdates")
FN_FORCE = _extract_js_function(UI, "forceUpdate")
FN_CHECK = _extract_js_function(PANELS, "checkUpdatesNow")

# ── the FULL shipped runtime + catalog from static/i18n.js ──────────────────
# The reviewer asked for the actual translation contract rather than a stub.
# The whole file is evaluated verbatim in its own context and the real t() is
# taken from it, so a key that does not exist behaves exactly as it does in
# production: it returns the key itself.
_REAL_T = r"""
// #7679 finding 3 (re-gate 10-31): evaluate the FULL shipped runtime and
// catalog instead of a reconstruction. The previous harness re-parsed the
// English block line by line and silently dropped every function-valued entry
// it could not parse, so it asserted strings the shipped runtime never emits.
// static/i18n.js is now evaluated VERBATIM in its own context (it ends with
// loadLocale(), which needs only the stubs below) and the real t() is taken
// from that context: every locale, every function-valued entry and the real
// missing-key behaviour stay exactly what ships.
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


def _build(extra_js: str) -> str:
    """Assemble a runnable node program from the real sources plus a scenario."""
    # static/i18n.js is ~27k lines — far past argv limits — so it is injected as
    # a global before the program body rather than passed as an argument.
    real_t = _REAL_T
    parts = [
        f"global.__I18N_SRC__ = {json.dumps(I18N)};",
        
        real_t,
        GRANT,
        FN_FORMAT,
        FN_INSTRUCTION,
        FN_ERROR,
        FN_PREDICATE,
        FN_DIRTY,
        FN_SHOW,
        FN_SHOW_ERROR,
        FN_APPLY,
        FN_FORCE,
        FN_CHECK,
        extra_js,
    ]
    return "\n".join(parts)


def _run(program: str) -> dict:
    # The assembled program (real i18n source + the extracted functions) is far
    # past argv limits, so it is written to a temp file and run from there.
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(program)
        path = handle.name
    try:
        proc = subprocess.run(
            [NODE_BIN, path],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        pathlib.Path(path).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr[:4000]
    return json.loads(proc.stdout)


# ── shared harness ──────────────────────────────────────────────────────────

_HARNESS = r"""
global.window = { _updateRecoveryGeneration: 0 };
global.document = { baseURI: 'http://127.0.0.1:8788/' };
global.showToast = (msg) => { global.__toasts.push(String(msg)); };
global.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global._renderUpdateWhatsNewLinks = () => {};
global._waitForServerThenReload = () => {};
// Force reads the server identity to detect a restart after the POST. Tests that
// care stub this; the default returns null, which is the "unknown identity" case.
global._readHealthServerIdentity = async () => null;
global.showConfirmDialog = () => Promise.resolve(true);
global.__toasts = [];
global.__posted = [];
global.api = async (url, opts) => {
  if (url === '/api/updates/check') global.__checkCalls = (global.__checkCalls || 0) + 1;
  global.__posted.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
  return global.__nextApiResponse || { ok: true, restart_scheduled: true };
};

global.__state = {
  updateBanner: { classList: { add() { this.added = true; }, remove() { this.removed = true; } } },
  updateMsg: { textContent: '' },
  updateError: { textContent: '', style: { display: 'none' } },
  btnApplyUpdate: { disabled: false, style: { display: '' } },
  btnForceUpdate: { disabled: false, style: { display: 'none' }, dataset: { target: '' }, textContent: '' },
  btnClearUpdateLock: { disabled: false, style: { display: 'none' }, dataset: { target: '' } },
  updateWhatsNewLinks: { style: { display: 'none' }, replaceChildren() { this.cleared = true; } },
  // checkUpdatesNow's own controls
  btnCheckUpdatesNow: { disabled: false },
  checkUpdatesLabel: { textContent: '' },
  checkUpdatesSpinner: { style: { display: 'none' } },
  checkUpdatesStatus: { textContent: '', style: { color: '' } },
};
global.$ = (id) => global.__state[id] || null;
"""


# ── finding 1: a retained Agent Force must be operable ──────────────────────

_F1 = r"""
(async () => {
  // A manual-update WebUI whose Agent target is still recoverable: the
  // preservation branch keeps the Agent Force visible and enabled.
  // The preservation branch needs a manual-update WebUI that is BEHIND (that is
  // what ``webuiManual`` means) and a Force button already armed on the Agent
  // target from a previous render.
  const payload = {
    webui: { behind: 3, manual_update: true, dirty: false },
    agent: { behind: 2, channel: 'stable', recovery: { force: true } },
  };
  global.window._defaultModel = '';
  global.__state.btnForceUpdate.style.display = 'inline-block';
  global.__state.btnForceUpdate.disabled = false;
  global.__state.btnForceUpdate.dataset.target = 'agent';
  _showUpdateBanner(payload, null, 0);

  const btn = global.__state.btnForceUpdate;
  const visibleAndEnabled = btn.style.display === 'inline-block' && !btn.disabled;
  const granted = (typeof _forceUpdateGrant !== 'undefined') && _forceUpdateGrant
    && _forceUpdateGrant.target === 'agent';

  // Now click it: a retained control must actually be able to act.
  global.__posted.length = 0;
  await forceUpdate(btn);

  process.stdout.write(JSON.stringify({
    visible_and_enabled: visibleAndEnabled,
    granted,
    target: btn.dataset.target,
    posts: global.__posted,
    confirmations: global.__confirmations || 0,
  }));
})();
"""


def test_a_retained_agent_force_is_operable():
    """Finding 1: visible + enabled must mean clickable, not silently inert."""
    program = _build(_HARNESS + "\nglobal.__confirmations = 0;\n"
                 "global.showConfirmDialog = () => { global.__confirmations += 1; return Promise.resolve(true); };\n"
                 + _F1)
    result = _run(program)
    assert result["visible_and_enabled"] is True, (
        "the manual-Agent Force was not retained at all; this scenario no longer "
        "exercises the preservation branch"
    )
    assert result["granted"] is True, (
        "the retained Agent Force has no grant behind it, so forceUpdate() "
        "returns silently: zero confirmations and zero POSTs from a button that "
        "looks operable (#7679 finding 1)"
    )
    assert result["target"] == "agent"
    assert result["confirmations"] >= 1, (
        "clicking a visible, enabled Force opened no confirmation dialog"
    )
    assert [p["url"] for p in result["posts"]] == ["/api/updates/force"], (
        f"a retained Agent Force did not POST: {result['posts']!r}"
    )
    assert result["posts"][0]["body"]["target"] == "agent"


def test_the_retained_grant_is_not_issued_without_agent_recovery():
    """Negative control: re-arming must not resurrect an unsupported grant.

    Granting unconditionally in the preservation branch would be worse than the
    original bug — it would arm the destructive control on a payload that no
    longer supports it.
    """
    program = _build(
        _HARNESS
        + r"""
(async () => {
  // Agent is NOT recoverable: the preservation branch must not be reached.
  const payload = {
    webui: { behind: 0, manual_update: true, dirty: false },
    agent: { behind: 0, channel: 'stable', recovery: { force: false } },
  };
  _showUpdateBanner(payload, null, 0);
  const btn = global.__state.btnForceUpdate;
  const granted = (typeof _forceUpdateGrant !== 'undefined') && _forceUpdateGrant;
  process.stdout.write(JSON.stringify({
    hidden: btn.style.display === 'none' || btn.disabled,
    granted: !!granted,
  }));
})();
"""
    )
    result = _run(program)
    assert result["hidden"] is True, (
        "a payload with no Agent recovery left the Force visible"
    )
    assert result["granted"] is False, (
        "a payload with no Agent recovery still armed a grant — the "
        "re-authorisation is not gated on the current check"
    )


# ── finding 2: rejection and finally respect ownership ──────────────────────

_F2 = r"""
(async () => {
  // Two checks. The OLDER one rejects; the NEWER one succeeds and publishes.
  const older = { webui: { behind: 3 } };
  const newer = { webui: { behind: 0 } };

  let releaseOlder;
  const olderGate = new Promise((res) => { releaseOlder = res; });

  const realApi = global.api;
  global.api = async (url, opts) => {
    if (url === '/api/updates/check') {
      // Distinguish by call order: first call is the older check.
      global.__checkCalls = (global.__checkCalls || 0) + 1;
      if (global.__checkCalls === 1) {
        await olderGate;
        throw new Error('older check failed');
      }
      return newer;
    }
    return realApi(url, opts);
  };

  // Start the older check, let it get as far as its await, then start the newer.
  const olderPromise = checkUpdatesNow();
  await new Promise((r) => setTimeout(r, 0));
  const newerPromise = checkUpdatesNow();
  await new Promise((r) => setTimeout(r, 0));

  // The newer check completes first and publishes its status.
  await newerPromise;
  const afterNewer = {
    status: global.__state.checkUpdatesStatus.textContent,
    buttonDisabled: global.__state.btnCheckUpdatesNow.disabled,
    spinnerHidden: global.__state.checkUpdatesSpinner.style.display === 'none',
    label: global.__state.checkUpdatesLabel.textContent,
  };

  // Now the older one rejects. It must not overwrite anything.
  releaseOlder();
  await olderPromise;

  process.stdout.write(JSON.stringify({
    after_newer: afterNewer,
    after_older_rejection: {
      status: global.__state.checkUpdatesStatus.textContent,
      buttonDisabled: global.__state.btnCheckUpdatesNow.disabled,
      spinnerHidden: global.__state.checkUpdatesSpinner.style.display === 'none',
      label: global.__state.checkUpdatesLabel.textContent,
    },
  }));
})();
"""


def test_an_older_rejection_does_not_overwrite_a_newer_status():
    """Finding 2: the catch path needs the same epoch guard as the success path."""
    result = _run(_build(_HARNESS + _F2))
    after_newer = result["after_newer"]
    after_older = result["after_older_rejection"]
    assert after_older["status"] == after_newer["status"], (
        "an older REJECTED check overwrote the status a newer successful check "
        f"published: {after_newer['status']!r} -> {after_older['status']!r}"
    )


def test_an_older_finally_does_not_restore_controls_mid_flight():
    """Finding 2: only the latest owner may restore Check/spinner/label."""
    result = _run(_build(_HARNESS + _F2))
    after_newer = result["after_newer"]
    after_older = result["after_older_rejection"]
    # The newer request finished, so its own finally legitimately restored the
    # controls. What must NOT happen is the older one changing them again.
    assert after_older["buttonDisabled"] == after_newer["buttonDisabled"]
    assert after_older["spinnerHidden"] == after_newer["spinnerHidden"]
    assert after_older["label"] == after_newer["label"], (
        "an older request's finally reset the Check label while a newer request "
        f"owned the controls: {after_newer['label']!r} -> {after_older['label']!r}"
    )


# ── finding 3: every terminal branch reconciles the banner ──────────────────

_F3 = r"""
(async () => {
  // First publish a state that arms the Force button, then follow it with an
  // invalid check (disabled / error / no-git) and observe whether the banner
  // reconciled.
  const results = {};
  const scenarios = {
    disabled: { disabled: true },
    error: { webui: { error: 'git failed' }, agent: { error: 'git failed' } },
    no_git: { webui: { no_git: true }, agent: { no_git: true } },
  };

  for (const [name, payload] of Object.entries(scenarios)) {
    // Reset the shared state between scenarios.
    global.__state.btnForceUpdate.style.display = 'inline-block';
    global.__state.btnForceUpdate.disabled = false;
    global.__state.btnForceUpdate.dataset.target = 'agent';
    // finding 3 (round 3): establish the grant through the PRODUCTION path.
    // Writing `global._forceUpdateGrant = ...` only created a property on the
    // sandbox object — the production `let _forceUpdateGrant` is a separate
    // lexical binding, so the grant the scenario claims to set was never seen
    // by forceUpdate()/applyUpdates() and every assertion below it was vacuous.
    // _showUpdateBanner is the real arming path and it re-derives the grant
    // from the payload, so use it.
    _showUpdateBanner({
      webui: { behind: 3, manual_update: true, dirty: false },
      agent: { behind: 1, channel: 'stable', recovery: { force: true } },
    }, null, 0);

    global.__nextApiResponse = payload;
    global.__posted.length = 0;
    await checkUpdatesNow();

    results[name] = {
      hidden: global.__state.btnForceUpdate.style.display === 'none',
      disabled: global.__state.btnForceUpdate.disabled,
      grant: (typeof _forceUpdateGrant !== 'undefined') ? !!_forceUpdateGrant : false,
    };
  }
  process.stdout.write(JSON.stringify(results));
})();
"""


@pytest.mark.parametrize("branch", ["disabled", "error", "no_git"])
def test_an_invalid_follow_up_check_reconciles_the_banner(branch):
    """Finding 3: disabled / error / no-git must not leave a stale Force armed."""
    result = _run(_build(_HARNESS + _F3))
    row = result[branch]
    assert row["hidden"] or row["disabled"], (
        f"the {branch!r} branch left the Force button visible and enabled; the "
        "grant behind it was retired, so clicking it does nothing"
    )


# ── finding 4: revalidate after the last await ──────────────────────────────

_F4 = r"""
(async () => {
  // A stable Force is confirmed; health is paused; a newer clean check completes
  // during that pause; then health settles. The POST must not happen.
  //
  // #7679 finding 2 (re-gate 10-10): the previous revision retired the grant
  // BEFORE releasing the confirmation, so forceUpdate() saw the mismatch at its
  // post-confirm revalidation and returned BEFORE _readHealthServerIdentity().
  // The scenario then asserted "no POST" for a handler that never reached the
  // guard it claims to exercise — releasing the unused health gate afterwards
  // proved nothing, and removing the post-health guard alone would not turn it
  // red.
  //
  // The corrected ordering keeps the grant VALID through the confirmation (so
  // the handler really does reach the health read), waits for an explicit
  // health-ENTERED signal, and only then completes the newer production Check
  // while health is still paused. The grant must be retired through the
  // production setter (finding 3, round 3), and the newer check runs through
  // the real _beginUpdateCheck/_showUpdateBanner path rather than a direct
  // assignment, so the scenario covers the caller/rejection composition.
  let releaseConfirm;
  const confirmGate = new Promise((res) => { releaseConfirm = res; });
  // finding 3 (round 3): resolve with a TRUE confirmation. Resolving with
  // nothing made forceUpdate() return at `if(!confirmed) return` before it
  // ever read health, so the scenario asserted health_reads:0 and never
  // exercised the contract it claims to test. A confirmed dialog is what puts
  // the handler past the confirm and onto the health await.
  global.showConfirmDialog = async () => { await confirmGate; return true; };

  let releaseHealth;
  let healthEntered;
  const healthEnteredSignal = new Promise((res) => { healthEntered = res; });
  const healthGate = new Promise((res) => { releaseHealth = res; });
  global._readHealthServerIdentity = async () => {
    global.__healthReads = (global.__healthReads || 0) + 1;
    healthEntered(true);
    await healthGate;
    return null;
  };

  const payload = {
    // Manual-update WebUI that is behind: the preservation branch keeps the
    // Agent Force and (after this round) re-arms it, so the confirm is genuinely
    // authorised when the race begins.
    webui: { behind: 3, manual_update: true, dirty: false },
    agent: { behind: 1, channel: 'stable', recovery: { force: true } },
  };
  global.__state.btnForceUpdate.style.display = 'inline-block';
  global.__state.btnForceUpdate.disabled = false;
  global.__state.btnForceUpdate.dataset.target = 'agent';
  _showUpdateBanner(payload, null, 0);
  const btn = global.__state.btnForceUpdate;
  const grantAtConfirm = _forceUpdateGrant;

  global.__posted.length = 0;
  const clickPromise = forceUpdate(btn);

  // 1) The confirmation is answered with the grant STILL VALID, so the handler
  //    proceeds past the confirm and onto the health read.
  releaseConfirm();
  await healthEnteredSignal;
  const healthReadsAtPause = global.__healthReads;

  // 2) While health is paused, a newer production Check COMPLETES — the real
  //    checkUpdatesNow(), not a hand-written retire/hide. The re-gate pinned
  //    this: the scene used to retire the grant and hide the button itself, so
  //    it proved the guard can detect a mutated grant but never that the
  //    PRODUCER (a newer Check) is what supersedes it. Its clean payload now
  //    takes the grant and the button down through the real renderer, which is
  //    the producer-composed composition the reviewer asked for.
  global.__nextApiResponse = {
    webui: { behind: 0, dirty: false, manual_update: false, channel: 'stable' },
    agent: { behind: 0, dirty: false, manual_update: false, channel: 'stable' },
  };
  await checkUpdatesNow();
  const grantAfterNewerCheck = _forceUpdateGrant;
  const checkStatusAfterNewerCheck = global.__state.checkUpdatesStatus.textContent;

  // 3) Health settles. Only now may the handler resume past the await.
  releaseHealth();
  await clickPromise;

  process.stdout.write(JSON.stringify({
    had_grant_at_confirm: !!grantAtConfirm,
    health_reads_at_pause: healthReadsAtPause,
    newer_check_ran: global.__checkCalls >= 1,
    newer_check_status: checkStatusAfterNewerCheck,
    grant_replaced_by_newer_check: grantAfterNewerCheck !== grantAtConfirm,
    posts: global.__posted.filter((p) => p.url === '/api/updates/force'),
    button_display: btn.style.display,
    button_disabled: btn.disabled,
  }));
})();
"""


def test_no_post_when_a_newer_check_completes_during_the_health_await():
    """Finding 4: the grant must be revalidated after the LAST await too."""
    result = _run(_build(_HARNESS + _F4))
    assert result["had_grant_at_confirm"] is True, (
        "the scenario no longer arms a grant at confirm time"
    )
    # #7679 finding 2 (re-gate 10-10): the handler must actually REACH the
    # health read. Without this assertion the scenario passes vacuously — the
    # pre-fix revision returned before health and still asserted "no POST".
    # It is also what makes the test discriminating: removing ONLY the
    # post-health guard (keeping the post-confirm one) turns this red, because
    # the handler then proceeds to the POST after the gate.
    assert result["health_reads_at_pause"] >= 1, (
        "the handler never reached _readHealthServerIdentity, so the "
        "post-health guard was not exercised (the pre-fix ordering returned "
        "before it)"
    )
    # #7679 (re-gate 10-31): the superseding observation must be a REAL newer
    # Check, completed through checkUpdatesNow() while health was paused — not
    # a hand-written retire/hide. Without these assertions the scene would
    # still pass if the producer never ran at all.
    assert result["newer_check_ran"] is True, (
        "the paused-health scene never completed a real checkUpdatesNow(), so "
        "it does not cover the producer-composed supersession the reviewer "
        "asked for"
    )
    assert result["newer_check_status"], (
        "the newer Check published no status line, so it did not really run"
    )
    assert result["grant_replaced_by_newer_check"] is True, (
        "the newer Check did not replace the grant the handler holds, so the "
        "post-health guard would never fire and the scenario is vacuous"
    )
    assert result["posts"] == [], (
        "a stale handler POSTed to /api/updates/force after a newer check had "
        f"already retired its grant: {result['posts']!r}"
    )
    assert result["button_disabled"] is False, (
        "the handler left the Force button disabled after refusing, so the user "
        "cannot act on the newer state"
    )


# ── the smaller items ───────────────────────────────────────────────────────


def test_the_real_locale_defines_force_no_longer_applicable():
    """The key must exist in the real locale runtime, not only as a fallback."""
    assert "force_no_longer_applicable:" in I18N, (
        "static/i18n.js has no force_no_longer_applicable entry, so the real "
        "locale runtime renders the raw key name to the user"
    )
    # And the English block's text must not be the key itself.
    program = _build(_HARNESS + "\nprocess.stdout.write(JSON.stringify({ v: t('force_no_longer_applicable') }));\n")
    result = _run(program)
    assert result["v"] != "force_no_longer_applicable", (
        f"t() returned the key itself: {result['v']!r}"
    )


def test_the_harness_runs_the_full_shipped_catalog():
    """The sandbox must evaluate the FULL runtime/catalog, not a reconstruction.

    The previous harness re-parsed the English block line by line and swallowed
    function-entry parse failures, so it could assert strings the shipped
    runtime never emits. This pins the shipped catalog: every locale block in
    static/i18n.js is loaded, and function-valued entries are invoked with
    their arguments.
    """
    program = _build(
        _HARNESS
        + "\nprocess.stdout.write(JSON.stringify({ locales: __catalogLocales, active: t('update_force_active_runs', 2), single: t('update_force_active_runs', 1) }));\n"
    )
    result = _run(program)
    shipped = [
        m.group(1) or m.group(2) or m.group(3)
        for m in re.finditer(
            r"^  (?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_-]*)): \{",
            I18N,
            re.MULTILINE,
        )
    ]
    assert result["locales"] == shipped, (
        "the sandbox catalog is not the shipped one: "
        f"{result['locales']!r} != {shipped!r}"
    )
    assert len(shipped) > 1, "the shipped catalog has only one locale?"
    assert result["active"] == "Update anyway (interrupts 2 runs)", result
    assert result["single"] == "Update anyway (interrupts 1 run)", result


_DIRTY_ONLY = r"""
(async () => {
  // Dirty at latest: no behind count, no error, not no-git. This is the branch
  // that used to report "Up to date".
  global.__nextApiResponse = {
    webui: { behind: 0, dirty: true },
    agent: { behind: 0, dirty: false },
  };
  await checkUpdatesNow();
  process.stdout.write(JSON.stringify({
    status: global.__state.checkUpdatesStatus.textContent,
  }));
})();
"""


def test_dirty_only_settings_does_not_say_up_to_date():
    """A dirty install must not be reported as up to date."""
    result = _run(_build(_HARNESS + _DIRTY_ONLY))
    status = result["status"]
    assert "up to date" not in status.lower(), (
        f"a dirty-at-latest install reported {status!r} while the banner above "
        "it said 'Local changes detected'"
    )
    assert "local changes" in status.lower(), (
        f"expected the dirty status, got {status!r}"
    )


_NOOP = r"""
(async () => {
  const payload = {
    webui: { behind: 3, manual_update: true, dirty: false },
    agent: { behind: 1, channel: 'stable', recovery: { force: true } },
  };
  global.__state.btnForceUpdate.style.display = 'inline-block';
  global.__state.btnForceUpdate.disabled = false;
  global.__state.btnForceUpdate.dataset.target = 'agent';
  _showUpdateBanner(payload, null, 0);
  const btn = global.__state.btnForceUpdate;
  global.__nextApiResponse = { ok: true, up_to_date: true };
  let reloadCalled = false;
  global._waitForServerThenReload = () => { reloadCalled = true; };
  global.__toasts.length = 0;
  await forceUpdate(btn);
  process.stdout.write(JSON.stringify({
    reload_called: reloadCalled,
    button_disabled: btn.disabled,
    toasts: global.__toasts,
  }));
})();
"""


def test_a_backend_noop_is_not_announced_as_a_restart():
    """``{ok:true,up_to_date:true}`` must not run the restart waiter."""
    result = _run(_build(_HARNESS + _NOOP))
    assert result["reload_called"] is False, (
        "a backend no-op ran the restart waiter on an install that never "
        "restarted"
    )
    assert result["button_disabled"] is False, (
        "a backend no-op left the Force button disabled for the whole poll window"
    )
    assert any("up to date" in toast.lower() for toast in result["toasts"]), (
        f"the no-op outcome was not reported to the user: {result['toasts']!r}"
    )


# ── source-level guards for the structural parts ────────────────────────────


def test_the_rejection_path_has_an_epoch_guard():
    src = PANELS
    idx = src.find("async function checkUpdatesNow(")
    assert idx > 0
    body = src[idx : idx + 9000]
    catch_idx = body.find("} catch(e){")
    assert catch_idx > 0, "the check's catch arm is gone"
    catch_body = body[catch_idx : catch_idx + 900]
    assert "_isUpdateCheckStale" in catch_body, (
        "the rejection path publishes a status without an epoch guard, so an "
        "older failed check overwrites a newer successful one (#7679 finding 2)"
    )


def test_the_finally_path_has_an_owner_guard():
    src = PANELS
    idx = src.find("async function checkUpdatesNow(")
    # #7679 round 4: the outer window must reach past the finally arm's
    # guard, not just its opening brace. The round-4 ownership comments
    # above the reconciler call push the guard further from the function
    # start, and the old 11000-byte window cut the body off before the
    # guard — the pin then failed on a file whose guard was present and
    # correct.
    body = src[idx : idx + 14000]
    finally_idx = body.find("} finally {")
    assert finally_idx > 0, "the check's finally arm is gone"
    # #7679 round 4: the window covers the finally arm's comment block
    # plus its guard. The round-4 ownership comments above the reconciler
    # call push the guard past the old 700-byte window, which is why this
    # pin used to fail on a file whose guard was present and correct.
    finally_body = body[finally_idx : finally_idx + 1600]
    # #7679 (Greptile P1, 10-14): the guard is the SETTINGS-CONTROL owner,
    # not the banner epoch. The banner epoch is advanced by every check,
    # including the unattended boot-time one, which does not own the
    # Settings panel: using it left the Check button disabled forever
    # after a manual check lost the race with the boot check.
    assert "_isUpdateSettingsCheckStale" in finally_body, (
        "the finally path restores the controls without an owner guard, so a "
        "stale request re-enables Check mid-flight (#7679 finding 2)"
    )


def test_the_manual_agent_keep_branch_rearms_the_grant():
    src = UI
    idx = src.find("_forceBtnManualAgentKeep")
    assert idx > 0
    # The re-arm must be gated on the same predicate the block above uses.
    window = src[idx : idx + 3000]
    assert "_grantForceUpdate" in window, (
        "the manual-Agent preservation branch keeps the control but never "
        "re-arms its grant, so the retained Force is inert (#7679 finding 1)"
    )
    assert "agentUpdatable" in window, (
        "the re-arm is not gated on the current check's Agent-recovery "
        "predicate, so it could arm the destructive control on a payload that "
        "no longer supports it"
    )
    # The predicate must be re-derived locally, not read from the block above:
    # ``_recoveryGone`` is declared inside that block and is out of scope here,
    # so referencing it throws a ReferenceError from inside the banner render.
    assert "_recoveryGone(" not in window.split("_keptRecoveryGone")[0][-200:], (
        "the preservation branch references _recoveryGone, which is out of "
        "scope there — the banner render would throw"
    )


def test_force_update_revalidates_after_the_health_await():
    src = UI
    idx = src.find("async function forceUpdate(")
    assert idx > 0
    # The window must reach past the health read to the destructive POST. The
    # handler grew (the bound active-runs retry), so size it off the function
    # itself rather than a hand-counted constant: slice to the next top-level
    # declaration after forceUpdate.
    end = src.find("\n// Poll /health after an update-triggered restart", idx)
    assert end > idx, "the declaration that follows forceUpdate is gone"
    body = src[idx:end]
    health_idx = body.find("_readHealthServerIdentity()")
    assert health_idx > 0, "the health read is gone from forceUpdate"
    after = body[health_idx:]
    post_idx = after.find("/api/updates/force")
    assert post_idx > 0, "the destructive POST is gone from forceUpdate"
    between = after[:post_idx]
    assert "grantSnapshot" in between, (
        "forceUpdate does not re-check the grant between the health await and "
        "the destructive POST, so a check that completes during that await "
        "leaves a stale handler posting (#7679 finding 4)"
    )
    # #7679 finding 1 (re-gate 10-31): the active-runs retry is a REPLAY bound
    # to the grant the offer captured, so the handler must not read its
    # target/channel from the live button or the live grant.
    assert "boundGrant" in body, (
        "forceUpdate no longer honours the bound grant an active-runs offer "
        "passes, so a retry can be retargeted by a newer check "
        "(#7679 finding 1)"
    )
