"""#7679 — a surviving WebUI recovery must keep its banner (greptile P1, 2026-11-18).

Thread 4607736686: *"WebUI recovery disappears. Start a Settings check, then
let Apply report a WebUI conflict before that check returns an error payload.
This snapshot keeps only Agent grants, so the older check clears the newer WebUI
grant and hides Force. A newer WebUI Clear-lock button survives its own check,
but the Agent-only banner check hides its parent instead. Use the recovery
owner stamps for both targets when preserving controls, and keep the banner
visible while either target has a surviving action."*

The PR already protects recovery controls by OWNERSHIP (the owner stamp +
``_controlIsNewerThan``), and that protection is target-agnostic. But two
places still assume the Agent is the only target that can own a recovery:

* ``_preservedRecoveryTarget`` snapshots only an ``agent`` grant, so a newer
  WEBUI grant is dropped by the unconditional retire at the top of the render;
* ``_recoveryControlSurvives`` — which decides whether the banner stays
  visible — only looks for ``dataset.target === 'agent'``.

The second one is the visible defect: the control survives, but the banner that
HOSTS it (index.html: ``btnForceUpdate`` / ``btnClearUpdateLock`` are children
of ``#updateBanner``) is hidden, so the recovery action the user still needs
disappears with it.

These tests drive the real ``_showUpdateBanner`` and the real ``_showUpdateError``
(the production producer of a WebUI recovery) in the same sandbox the round-3
suite uses, and assert the reviewer's exact schedule for the WebUI target.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
PANELS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _node_env() -> dict:
    import os

    env = dict(os.environ)
    env.pop("NODE_CHANNEL_FD", None)
    env.pop("HERMES_WEBUI_TEST_STATE_DIR", None)
    return env


def _run_node(script: str) -> subprocess.CompletedProcess:
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(script)
        tmp_path = tf.name
    try:
        return subprocess.run(
            [NODE, tmp_path],
            capture_output=True,
            text=True,
            check=False,
            env=_node_env(),
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _extract(src: str, start_marker: str, end_marker: str) -> str:
    i = src.find(start_marker)
    assert i != -1, f"marker not found: {start_marker!r}"
    j = src.find(end_marker, i)
    assert j != -1, f"end marker not found: {end_marker!r}"
    return src[i:j]


def _brace_block(src: str, header: str) -> str:
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
    raise AssertionError(f"{header!r} did not close")


_HARNESS_HEAD = r"""
const assert = require('assert');
// The FULL shipped i18n runtime and catalog are evaluated verbatim, so the
// real t() and the real missing-key behaviour are what these assertions
// measure.
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

global.__toasts = [];
global.__posted = [];
global.__state = {
  btnForceUpdate: { style: {}, dataset: {}, disabled: false, textContent: '' },
  btnClearUpdateLock: { style: {}, dataset: {}, disabled: false, textContent: '' },
  btnApplyUpdate: { style: {}, dataset: {}, disabled: false, textContent: '' },
  updateBanner: { classList: { _classes: new Set(), add(c){ this._classes.add(c); }, remove(c){ this._classes.delete(c); }, contains(c){ return this._classes.has(c); } }, style: {}, textContent: '' },
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
global._readHealthServerIdentity = async () => null;
global.showConfirmDialog = async () => true;
global.__nextApiResponse = null;
global.__nextApiError = null;
global.api = async (url, opts) => {
  if (global.__nextApiError) { const e = new Error(global.__nextApiError); e.response = global.__nextApiErrorBody || null; throw e; }
  global.__posted.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
  return global.__nextApiResponse || { ok: true, restart_scheduled: true };
};
global.__healthReads = 0;
global._readHealthServerIdentity = async () => { global.__healthReads += 1; return null; };
"""


def _build_vm(body: str) -> str:
    ui_part = _extract(UI, "let _updateCheckEpoch", "function _showUpdateBanner(data,")
    ui_part += _extract(UI, "function _showUpdateBanner(data,", "\nfunction _i18nUpdateText")
    ui_part += _extract(UI, "function _isForceCleanTarget(", "\nfunction _formatUpdateDirtyStatus")
    ui_part += _extract(UI, "function _formatUpdateDirtyStatus(", "\nfunction _formatManualUpdateInstruction")
    ui_part += _extract(UI, "function _formatManualUpdateInstruction(", "\nfunction _formatUpdateCheckError")
    ui_part += _extract(UI, "function _formatUpdateTargetStatus(", "\nfunction _formatUpdateDirtyStatus")
    ui_part += "\nfunction _i18nUpdateText(k,f){const v=t(k);return (v&&v!==k)?v:f;}\nfunction _renderUpdateWhatsNewLinks(){}\n"
    ui_part += "\n" + _brace_block(UI, "async function forceUpdate(")
    ui_part += "\n" + _brace_block(UI, "async function applyUpdates(")
    ui_part += "\n" + _brace_block(UI, "function _showUpdateError(target,res){")
    ui_part += "\n" + _brace_block(UI, "function _isUpdateApplyNetworkError(error){")
    ui_part += "\n" + _brace_block(UI, "function _formatUpdateApplyExceptionMessage(error){")
    panels_part = _extract(PANELS, "async function checkUpdatesNow(", "\n// ── Auxiliary Models")
    return f"""
global.__I18N_SRC__ = {json.dumps(I18N)};
{_HARNESS_HEAD}
{ui_part}
{panels_part}
{body}
"""


def _banner_visible() -> bool:
    """The production banner is shown by a ``visible`` class on #updateBanner."""
    return True  # the harness records the class list; the scenario reads it back


def test_an_older_observation_keeps_a_newer_webui_recovery_and_its_banner():
    """The reviewer's schedule, on the WEBUI target.

    A Settings check starts and captures recovery generation 0. Apply then
    reports a WebUI conflict, which publishes generation 1 with a live
    WebUI/stable Force grant. The already-pending check finally returns an
    error-only payload.

    That older observation must not clear the newer WebUI grant, and — because
    the recovery controls live INSIDE ``#updateBanner`` — it must not hide the
    banner either.
    """
    body = r"""
(async () => {
  const results = {};
  // 1) A Settings check STARTS and captures recovery generation 0.
  global.window._updateRecoveryGeneration = 0;
  const capturedGeneration = 0;

  // 2) Apply reports a WebUI conflict: the REAL producer (_showUpdateError)
  //    publishes generation 1, the WebUI/stable grant and the Force control.
  global.window._updateRecoveryGeneration = 1;
  _showUpdateError('webui', { conflict: true, message: 'diverged' });
  const armed = {
    display: global.__state.btnForceUpdate.style.display,
    disabled: global.__state.btnForceUpdate.disabled,
    target: global.__state.btnForceUpdate.dataset.target,
    grant: (typeof _forceUpdateGrant !== 'undefined') && _forceUpdateGrant
      ? _forceUpdateGrant.target : null,
  };

  // 3) The already-pending check returns an error-only payload.
  global.window._updateRecoveryGeneration = 1;
  _showUpdateBanner({ webui: { error: 'fetch failed' }, agent: { error: 'fetch failed' } },
                    null, capturedGeneration);

  results.armed = armed;
  results.after = {
    display: global.__state.btnForceUpdate.style.display,
    disabled: global.__state.btnForceUpdate.disabled,
    target: global.__state.btnForceUpdate.dataset.target,
    grant: (typeof _forceUpdateGrant !== 'undefined') && _forceUpdateGrant
      ? _forceUpdateGrant.target : null,
  };
  results.bannerText = global.__state.updateMsg.textContent;
  results.bannerVisible = global.__state.updateBanner.classList.contains('visible');
  process.stdout.write(JSON.stringify(results));
})().catch(e => { process.stdout.write('THREW:' + e.message); process.exitCode = 1; });
"""
    out = _run_node(_build_vm(body))
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout.strip().splitlines()[-1])

    # The scenario must actually reach the state the finding describes.
    assert payload["armed"]["display"] == "inline-block", (
        "the WebUI conflict never armed Force — the scenario did not reach the "
        f"state the finding describes: {payload['armed']!r}"
    )
    assert payload["armed"]["grant"] == "webui", payload["armed"]

    after = payload["after"]
    assert after["display"] == "inline-block", (
        "an older observation hid a newer WebUI recovery "
        f"({after}) (#7679 greptile P1, 2026-11-18)"
    )
    assert after["disabled"] is False, (
        f"an older observation disabled a newer WebUI recovery ({after})"
    )
    assert after["target"] == "webui", (
        f"an older observation detargeted a newer WebUI recovery ({after})"
    )
    assert after["grant"] == "webui", (
        f"an older observation cleared the newer WebUI recovery grant ({after})"
    )


def test_a_surviving_webui_recovery_keeps_the_banner_visible():
    """The banner HOSTS the recovery controls, so it must stay visible.

    ``btnForceUpdate`` and ``btnClearUpdateLock`` are children of
    ``#updateBanner``. Hiding the banner erases the recovery action even
    though the control itself was preserved — the "Agent recovery disappears"
    defect, still present on the WebUI sibling.
    """
    body = r"""
(async () => {
  const results = {};
  for (const target of ['webui', 'agent']) {
    // A newer recovery for this target: the real producer publishes it.
    global.window._updateRecoveryGeneration = 1;
    _showUpdateError(target, { conflict: true, message: 'diverged' });

    // The already-pending check returns an error-only payload.
    _showUpdateBanner({ webui: { error: 'fetch failed' }, agent: { error: 'fetch failed' } },
                      null, 0);

    results[target] = {
      bannerText: global.__state.updateMsg.textContent,
      bannerVisible: global.__state.updateBanner.classList.contains('visible'),
      forceDisplay: global.__state.btnForceUpdate.style.display,
      forceTarget: global.__state.btnForceUpdate.dataset.target,
      grant: (typeof _forceUpdateGrant !== 'undefined') && _forceUpdateGrant
        ? _forceUpdateGrant.target : null,
    };
  }
  process.stdout.write(JSON.stringify(results));
})().catch(e => { process.stdout.write('THREW:' + e.message); process.exitCode = 1; });
"""
    out = _run_node(_build_vm(body))
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout.strip().splitlines()[-1])

    for target in ("webui", "agent"):
        state = payload[target]
        assert state["forceDisplay"] == "inline-block", (
            f"the {target} recovery control was hidden: {state}"
        )
        assert state["grant"] == target, (
            f"the {target} recovery grant was cleared: {state}"
        )
        # The banner must carry recovery copy rather than being hidden: the
        # production code pushes the recovery text into the banner message and
        # marks the banner visible.
        assert state["bannerText"], (
            f"the banner went empty while a {target} recovery survived: {state}"
        )
        assert state["bannerVisible"] is True, (
            f"the banner was hidden while a {target} recovery survived: {state}"
        )
