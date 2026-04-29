"""Regression coverage for anomalous recurring cron UI state."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
PANELS_JS = ROOT / "static" / "panels.js"
STYLE_CSS = ROOT / "static" / "style.css"
I18N_JS = ROOT / "static" / "i18n.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _cron_helper_source() -> str:
    src = PANELS_JS.read_text(encoding="utf-8")
    start = src.index("function _isRecurringCronJob")
    end = src.index("async function loadCrons", start)
    return src[start:end]


def _run_node(script: str) -> str:
    proc = subprocess.run(
        [NODE, "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def test_legacy_broken_recurring_cron_is_needs_attention_not_off():
    script = _cron_helper_source() + r"""
function t(key){ return key; }
const legacyBroken = {
  id: 'legacy-broken',
  schedule: {kind: 'cron', expr: '0 7,15,23 * * *'},
  repeat: {times: null, completed: 17},
  enabled: false,
  state: 'completed',
  next_run_at: null,
  last_status: 'ok',
};
const oneShotCompleted = {
  id: 'oneshot-done',
  schedule: {kind: 'once', run_at: '2026-04-01T00:00:00+00:00'},
  repeat: {times: 1, completed: 1},
  enabled: false,
  state: 'completed',
  next_run_at: null,
  last_status: 'ok',
};
const scheduleError = {
  id: 'schedule-error',
  schedule: {kind: 'cron', expr: '0 7 * * *'},
  repeat: {times: null, completed: 1},
  enabled: true,
  state: 'error',
  next_run_at: null,
  last_status: 'error',
};
console.log(JSON.stringify({
  legacyBroken: _cronStatusMeta(legacyBroken),
  oneShotCompleted: _cronStatusMeta(oneShotCompleted),
  scheduleError: _cronStatusMeta(scheduleError),
}));
"""
    states = json.loads(_run_node(script))

    assert states["legacyBroken"]["state"] == "needs_attention"
    assert states["legacyBroken"]["listClass"] == "attention"
    assert states["legacyBroken"]["label"] == "cron_status_needs_attention"
    assert states["oneShotCompleted"]["state"] == "off"
    assert states["oneShotCompleted"]["listClass"] == "disabled"
    assert states["scheduleError"]["state"] == "schedule_error"
    assert states["scheduleError"]["listClass"] == "attention"


def test_cron_attention_ui_has_recovery_and_diagnostics_actions():
    panels = PANELS_JS.read_text(encoding="utf-8")
    style = STYLE_CSS.read_text(encoding="utf-8")
    i18n = I18N_JS.read_text(encoding="utf-8")

    assert "cron_status_needs_attention" in panels
    assert "resumeCurrentCron()" in panels
    assert "runCurrentCron()" in panels
    assert "copyCurrentCronDiagnostics()" in panels
    assert "_cronDiagnostics(_currentCronDetail)" in panels
    assert ".cron-status.attention" in style
    assert ".detail-alert" in style
    assert "cron_attention_resume: 'Resume and recalculate'" in i18n
    assert "cron_attention_copy_diagnostics: 'Copy diagnostics'" in i18n
