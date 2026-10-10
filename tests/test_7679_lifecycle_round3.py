"""#7679 review 2026-10-10 round 3 — the two ownership findings and the copy bug.

The re-gate found the PR *not yet converged*. Round 2's repairs were verified
(retained Agent recovery clickable, older rejection no longer overwriting newer
success, stale finally leaving a newer pending Check alone, last-await
supersession blocking the Force POST, real locale lookup, backend no-op not
waiting for a restart) — but the reviewer independently replayed three failures
in the clean sandbox:

**Finding 1 — a current rejected check leaves an enabled but inert Force.**
A dirty WebUI check enables Force; the next actual API promise rejects. The
rejection path updated the failure status but never reconciled the obsolete
controls, so Force stayed visible and enabled while its grant was null. Clicking
it opened zero confirmations and sent zero POSTs.

**Finding 2 — the invalid-result reconciliation erases a NEWER Agent recovery.**
`_showUpdateBanner` starts by retiring every grant and re-arms only what its own
payload validates. So an observation captured *before* a newer recovery was
established — an error-only, no-git-only or disabled payload, none of which
carry authoritative recovery state — hid, disabled or detargeted Force and
cleared that newer grant. The actual rejected-promise path preserves the newer
recovery and posts once, so the two schedules genuinely need different handling.

**Finding 4 — the dirty-only banner copy.** Settings said "Local changes
detected" while the banner directly above it rendered
`WebUI: Local changes detected available`. There is nothing to download for a
dirty-only install; "available" belongs to real upstream updates.

These tests exercise the PRODUCTION functions (`checkUpdatesNow`,
`_showUpdateBanner`, `_reconcileObsoleteUpdateControls`,
`_recoveryObservationMayRetire`) through the same sandbox harness the round-2
file uses, and they drive both clocks (the check epoch and the recovery
generation) rather than collapsing them.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
PANELS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _node_env() -> dict:
    """Strip the runtime-injected fds before spawning node.

    An inherited NODE_CHANNEL_FD makes the child abort with SIGABRT (rc -6) as
    it shuts its IPC channel down, which turns a passing harness into a crash.
    """
    import os

    env = dict(os.environ)
    env.pop("NODE_CHANNEL_FD", None)
    env.pop("HERMES_WEBUI_TEST_STATE_DIR", None)
    return env


def _run_node(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        check=False,
        env=_node_env(),
    )


def _extract(src: str, start_marker: str, end_marker: str) -> str:
    i = src.find(start_marker)
    assert i != -1, f"marker not found: {start_marker!r}"
    j = src.find(end_marker, i)
    assert j != -1, f"end marker not found: {end_marker!r}"
    return src[i:j]


# ── source-shape contracts (fast, no VM) ─────────────────────────────────────


def test_the_rejection_path_reconciles_obsolete_controls():
    """Finding 1: the catch block must retire the grant it invalidated.

    Without this the button stays visible and enabled with a null grant — a
    decoy that opens zero confirmations and sends zero POSTs.
    """
    idx = PANELS.find("async function checkUpdatesNow(")
    assert idx > 0
    body = PANELS[idx:]
    catch_idx = body.find("} catch(e){")
    assert catch_idx > 0, "checkUpdatesNow has no catch block"
    catch_body = body[catch_idx:]
    assert "_retireForceUpdate()" in catch_body, (
        "the rejection path never retires the Force grant it invalidated, so "
        "the control it leaves behind is inert (#7679 finding 1)"
    )
    assert "_reconcileObsoleteUpdateControls" in catch_body, (
        "the rejection path does not reconcile the obsolete controls "
        "(#7679 finding 1)"
    )


def test_the_rejection_reconciliation_preserves_a_newer_agent_recovery():
    """Finding 1 + 2 together: retire the OWNED control, not the recovery.

    Rendering an empty payload from the catch block would hide independently
    newer recovery controls, which is finding 2's bug. The carve-out must be
    visible in the source.
    """
    idx = PANELS.find("async function checkUpdatesNow(")
    body = PANELS[idx:]
    catch_body = body[body.find("} catch(e){"):]
    assert "dataset.target!=='agent'" in catch_body or (
        "_reconcileObsoleteUpdateControls(data)" in catch_body
    ), (
        "the rejection path does not distinguish the owned WebUI control from "
        "an independently newer Agent recovery (#7679 findings 1 and 2)"
    )


def test_the_banner_preserves_a_newer_recovery_grant():
    """Finding 2: snapshot the recovery authority before the unconditional retire.

    `_showUpdateBanner` retires every grant on entry. An observation captured
    before a newer recovery — carrying no authoritative recovery state — must
    not erase it, so the grant has to be snapshotted and restored.
    """
    idx = UI.find("function _showUpdateBanner(data,")
    assert idx > 0
    body = UI[idx : idx + 20000]
    retire_idx = body.find("_retireForceUpdate()")
    assert retire_idx > 0
    before = body[:retire_idx]
    assert "_preservedRecoveryTarget" in before, (
        "the recovery grant is not snapshotted before the unconditional "
        "retire, so an older observation erases a newer recovery "
        "(#7679 finding 2)"
    )
    after = body[retire_idx:]
    assert "_grantForceUpdate('agent'" in after, (
        "the snapshotted recovery grant is never restored, leaving a preserved "
        "Agent control inert (#7679 finding 2)"
    )


def test_only_an_explicitly_gone_recovery_may_be_retired():
    """Finding 2: the retire rule needs the two clocks, not one.

    `_recoveryObservationMayRetire` must refuse when the payload carries no
    authoritative recovery state, when the observation is older than the current
    recovery generation, when the payload is a cache hit, or when nothing
    explicitly proves the condition gone.
    """
    assert "function _recoveryObservationMayRetire(" in UI, (
        "the retire rule is not a single named predicate, so the two "
        "ownership clocks are being collapsed (#7679 finding 2)"
    )
    fn = _extract(UI, "function _recoveryObservationMayRetire(", "\nfunction ")
    assert "recovery.force===false" in fn and "recovery.clear_lock===false" in fn, (
        "the predicate does not require an explicit 'condition gone' proof "
        "(#7679 finding 2)"
    )
    assert "data.cached" in fn, (
        "a cache hit may retire a recovery recorded after it (#7679 finding 2)"
    )
    assert "_updateRecoveryGeneration" in fn, (
        "the predicate does not compare recovery generations, so an older "
        "observation may retire a newer recovery (#7679 finding 2)"
    )


def test_the_dirty_only_banner_does_not_say_available():
    """Finding 4: 'available' belongs to real upstream updates only.

    The banner rendered `WebUI: Local changes detected available` — nothing is
    downloadable for a dirty-only install, and the Settings line directly below
    already says "Local changes detected".
    """
    idx = UI.find("function _showUpdateBanner(data,")
    body = UI[idx : idx + 20000]
    msg_idx = body.find("const msg=$('updateMsg');")
    assert msg_idx > 0
    block = body[msg_idx : msg_idx + 1600]
    assert "_dirtyOnlyParts" in block and "_upstreamParts" in block, (
        "the banner does not separate upstream updates from dirty-only parts, "
        "so 'available' is appended to a dirty-only install (#7679 finding 4)"
    )
    # The word must be attached to the upstream segment, never to the whole join.
    assert "_upstreamParts.join(', ')+' available'" in block, (
        "'available' is not attached to the upstream segment only "
        "(#7679 finding 4)"
    )


# ── behavioural contracts through the production VM ──────────────────────────

_HARNESS_HEAD = r"""
const assert = require('assert');
// Minimal locale runtime: a known key returns its text, an unknown key returns
// the key itself (production behaviour). The extra arguments are interpolation
// values, not fallback text — finding 3 (round 3) was precisely that confusion.
global.__enEntries = {
  update_dirty_local_changes: 'Local changes detected',
  update_force: 'Force update',
  settings_up_to_date: 'Up to date',
  settings_check_now: 'Check now',
  settings_update_check_failed: 'Update check failed',
  settings_update_no_git: 'No git repository',
  update_now: 'Update Now',
  update_updating: 'Updating\u2026',
};
function t(key, ...args) {
  let text;
  if (Object.prototype.hasOwnProperty.call(global.__enEntries, key)) text = global.__enEntries[key];
  else if (args.length && args[0]) text = String(args[0]);
  else return String(key);
  let i = 0;
  return String(text).replace(/\{(\w+)\}/g, (m) => (i < args.length ? String(args[i++]) : m));
}
global.__toasts = [];
global.__posted = [];
global.__state = {
  btnForceUpdate: { style: {}, dataset: {}, disabled: false, textContent: '' },
  btnClearUpdateLock: { style: {}, dataset: {}, disabled: false, textContent: '' },
  btnApplyUpdate: { style: {}, dataset: {}, disabled: false, textContent: '' },
  updateBanner: { classList: { add(){}, remove(){} }, style: {} },
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
    """Assemble the real ui.js + panels.js plus a scenario in one VM context."""
    ui_part = _extract(UI, "let _updateCheckEpoch", "function _showUpdateBanner(data,")
    ui_part += _extract(UI, "function _showUpdateBanner(data,", "\nfunction _i18nUpdateText")
    ui_part += _extract(UI, "function _isForceCleanTarget(", "\nfunction _formatUpdateDirtyStatus")
    ui_part += _extract(UI, "function _formatUpdateDirtyStatus(", "\nfunction _formatManualUpdateInstruction")
    ui_part += _extract(UI, "function _formatManualUpdateInstruction(", "\nfunction _formatUpdateCheckError")
    ui_part += _extract(UI, "function _formatUpdateTargetStatus(", "\nfunction _formatUpdateDirtyStatus")
    ui_part += "\nfunction _i18nUpdateText(k,f){const v=t(k);return (v&&v!==k)?v:f;}\nfunction _renderUpdateWhatsNewLinks(){}\n"
    panels_part = _extract(PANELS, "async function checkUpdatesNow(", "\n// ── Auxiliary Models")
    return f"""
{_HARNESS_HEAD}
{ui_part}
{panels_part}
{body}
"""


def test_a_current_rejection_retires_the_inert_force_button():
    """Finding 1: a dirty check enables Force, then the next check rejects.

    Force must not stay visible and enabled with a null grant — clicking it
    would open zero confirmations and send zero POSTs.
    """
    body = r"""
(async () => {
  // 1) A dirty WebUI check exposes and arms Force.
  global.__nextApiResponse = {
    webui: { behind: 0, dirty: true, manual_update: false, channel: 'stable' },
    agent: { behind: 0, dirty: false },
  };
  await checkUpdatesNow();
  const armed = {
    display: global.__state.btnForceUpdate.style.display,
    disabled: global.__state.btnForceUpdate.disabled,
    target: global.__state.btnForceUpdate.dataset.target,
    grant: (typeof _forceUpdateGrant !== 'undefined') ? !!_forceUpdateGrant : false,
  };

  // 2) The next actual check REJECTS (current epoch — not stale).
  global.__nextApiResponse = null;
  global.__nextApiError = 'boom';
  global.__nextApiErrorBody = JSON.stringify({ error: 'check failed' });
  await checkUpdatesNow();

  process.stdout.write(JSON.stringify({
    armed,
    after_reject: {
      display: global.__state.btnForceUpdate.style.display,
      disabled: global.__state.btnForceUpdate.disabled,
      target: global.__state.btnForceUpdate.dataset.target,
      grant: (typeof _forceUpdateGrant !== 'undefined') ? !!_forceUpdateGrant : false,
    },
    status: global.__state.updateStatus.textContent,
  }));
})().catch(e => { process.stdout.write('THREW:' + e.message); process.exitCode = 1; });
"""
    out = _run_node(_build_vm(body))
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload["armed"]["display"] == "inline-block", (
        "the dirty check did not arm Force — the scenario never reached the "
        "state finding 1 describes"
    )
    after = payload["after_reject"]
    assert after["display"] != "inline-block" or after["disabled"] is True or not after["target"], (
        f"a current rejection left an operable-looking Force button: {after} "
        "(#7679 finding 1)"
    )
    assert after["grant"] is False, (
        "a current rejection left the Force grant armed (#7679 finding 1)"
    )


def test_an_older_observation_does_not_erase_a_newer_agent_recovery():
    """Finding 2: the exact reviewer schedule.

    A valid manual-WebUI + updatable-Agent check completes; a second Check
    starts and captures recovery generation 0; applyUpdates() publishes
    generation 1 with a live Agent/stable Force grant; and the already-pending
    Check then returns an error-only, no-git-only or disabled payload. None of
    those three may hide, disable, detarget or disarm the newer recovery.
    """
    body = r"""
(async () => {
  const results = {};
  // 1) A valid manual-WebUI + updatable-Agent check completes.
  global.__nextApiResponse = {
    webui: { behind: 3, manual_update: true, dirty: false, channel: 'stable' },
    agent: { behind: 1, channel: 'stable', recovery: { force: true } },
  };
  await checkUpdatesNow();

  // 2) A second Check STARTS and captures recovery generation 0. We model the
  //    in-flight observation by resolving the payload while a newer recovery
  //    has already been published in between.
  global.window._updateRecoveryGeneration = 0;
  const capturedGeneration = 0;

  // 3) applyUpdates() publishes generation 1 with a live Agent/stable grant.
  global.window._updateRecoveryGeneration = 1;
  _grantForceUpdate('agent', 'stable');

  // 4) The already-pending Check returns one of the three blind payloads.
  const blind = {
    error_only: { webui: { error: 'fetch failed' }, agent: { error: 'fetch failed' } },
    no_git_only: { webui: { no_git: true }, agent: { no_git: true } },
    disabled: { webui: { behind: 0, manual_update: false, dirty: false }, agent: { behind: 0, dirty: false } },
  };
  for (const [name, payload] of Object.entries(blind)) {
    // Re-arm the newer recovery before each observation, exactly as the
    // reviewer's schedule does (the recovery was published once, before the
    // stale observation landed).
    global.window._updateRecoveryGeneration = 1;
    _grantForceUpdate('agent', 'stable');
    global.__state.btnForceUpdate.style.display = 'inline-block';
    global.__state.btnForceUpdate.disabled = false;
    global.__state.btnForceUpdate.dataset.target = 'agent';
    _showUpdateBanner(payload, null, capturedGeneration);
    results[name] = {
      display: global.__state.btnForceUpdate.style.display,
      disabled: global.__state.btnForceUpdate.disabled,
      target: global.__state.btnForceUpdate.dataset.target,
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
    assert isinstance(payload, dict), payload
    for name, state in payload.items():
        assert state["display"] == "inline-block", (
            f"{name}: a blind observation hid a newer Agent recovery "
            f"({state}) (#7679 finding 2)"
        )
        assert state["disabled"] is False, (
            f"{name}: a blind observation disabled a newer Agent recovery "
            f"({state}) (#7679 finding 2)"
        )
        assert state["target"] == "agent", (
            f"{name}: a blind observation detargeted a newer Agent recovery "
            f"({state}) (#7679 finding 2)"
        )
        assert state["grant"] == "agent", (
            f"{name}: a blind observation cleared the newer Agent recovery "
            f"grant ({state}) (#7679 finding 2)"
        )


def test_an_equally_current_observation_that_proves_it_gone_may_retire():
    """Control for finding 2: the preservation must not become permanent.

    An observation at the SAME recovery generation that explicitly reports the
    condition gone (`recovery.force === false`) must still retire the control —
    otherwise a conflict resolved outside the UI leaves a destructive button
    armed (the Greptile P1 on #8040 that this carve-out could regress).
    """
    body = r"""
(async () => {
  global.window._updateRecoveryGeneration = 1;
  _grantForceUpdate('agent', 'stable');
  global.__state.btnForceUpdate.style.display = 'inline-block';
  global.__state.btnForceUpdate.disabled = false;
  global.__state.btnForceUpdate.dataset.target = 'agent';

  const payload = {
    webui: { behind: 3, manual_update: true, dirty: false, channel: 'stable' },
    agent: { behind: 1, channel: 'stable', recovery: { force: false } },
  };
  _showUpdateBanner(payload, null, 1);
  process.stdout.write(JSON.stringify({
    display: global.__state.btnForceUpdate.style.display,
    disabled: global.__state.btnForceUpdate.disabled,
    target: global.__state.btnForceUpdate.dataset.target,
    grant: (typeof _forceUpdateGrant !== 'undefined') ? !!_forceUpdateGrant : false,
  }));
})().catch(e => { process.stdout.write('THREW:' + e.message); process.exitCode = 1; });
"""
    out = _run_node(_build_vm(body))
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload["display"] != "inline-block" or payload["disabled"] is True or not payload["target"], (
        f"a recovery explicitly proven gone stayed operable: {payload} "
        "(#8040 Greptile P1 regression)"
    )
    assert payload["grant"] is False, (
        f"a recovery explicitly proven gone kept its grant: {payload} "
        "(#8040 Greptile P1 regression)"
    )


def test_the_dirty_only_banner_omits_available():
    """Finding 4: a dirty-only install gets no 'available' suffix."""
    body = r"""
(async () => {
  const results = {};
  const scenarios = {
    dirty_only: { webui: { behind: 0, dirty: true, manual_update: false, channel: 'stable' }, agent: { behind: 0, dirty: false } },
    upstream: { webui: { behind: 2, dirty: false, manual_update: false, channel: 'stable' }, agent: { behind: 0, dirty: false } },
    mixed: { webui: { behind: 2, dirty: false, manual_update: false, channel: 'stable' }, agent: { behind: 0, dirty: true, manual_update: false } },
  };
  for (const [name, payload] of Object.entries(scenarios)) {
    _showUpdateBanner(payload, null, null);
    results[name] = global.__state.updateMsg.textContent;
  }
  process.stdout.write(JSON.stringify(results));
})().catch(e => { process.stdout.write('THREW:' + e.message); process.exitCode = 1; });
"""
    out = _run_node(_build_vm(body))
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert "available" not in payload["dirty_only"], (
        f"a dirty-only banner claims an update is available: "
        f"{payload['dirty_only']!r} (#7679 finding 4)"
    )
    assert "Local changes detected" in payload["dirty_only"], (
        f"the dirty-only banner lost its own copy: {payload['dirty_only']!r}"
    )
    assert "available" in payload["upstream"], (
        f"a real upstream update lost its 'available': {payload['upstream']!r}"
    )
    assert "available" in payload["mixed"] and payload["mixed"].count("Local changes detected") >= 1, (
        f"a mixed install must list both accurately: {payload['mixed']!r}"
    )
    # The word must sit on the upstream segment, not on the dirty one.
    dirty_segment = payload["mixed"].split("\u00b7")[-1].strip() if "\u00b7" in payload["mixed"] else payload["mixed"]
    assert "available" not in dirty_segment or "Local changes" not in dirty_segment, (
        f"'available' is attached to the dirty segment: {payload['mixed']!r} "
        "(#7679 finding 4)"
    )
