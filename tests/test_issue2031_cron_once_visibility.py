"""Regression coverage for #2031 one-shot cron schedule visibility."""

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


def _cron_schedule_source() -> str:
    src = PANELS_JS.read_text(encoding="utf-8")
    start = src.find("function _cronScheduleKindForInput")
    if start < 0:
        pytest.fail("_cronScheduleKindForInput is missing")
    end = src.find("function _hasUnlimitedRepeat", start)
    if end < 0:
        pytest.fail("_cronScheduleKindForInput must stay near the cron schedule helpers")
    return src[start:end]


def _cron_schedule_save_source() -> str:
    src = PANELS_JS.read_text(encoding="utf-8")
    start = src.find("async function saveCronForm()")
    if start < 0:
        pytest.fail("saveCronForm is missing")
    end = src.find("// Back-compat aliases for any stale callers", start)
    if end < 0:
        pytest.fail("saveCronForm boundary marker is missing")
    return src[start:end]


def _run_node(script: str) -> str:
    proc = subprocess.run(
        [NODE, "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def test_cron_schedule_input_classifier_flags_agent_one_shot_forms():
    script = _cron_schedule_source() + r"""
const cases = {
  "30m": _cronScheduleKindForInput("30m"),
  "2h": _cronScheduleKindForInput("2h"),
  "1 day": _cronScheduleKindForInput("1 day"),
  "2026-05-11": _cronScheduleKindForInput("2026-05-11"),
  "2026-05-11T08:00": _cronScheduleKindForInput("2026-05-11T08:00"),
  "every 30m": _cronScheduleKindForInput("every 30m"),
  "Every 2h": _cronScheduleKindForInput("Every 2h"),
  "0 9 * * *": _cronScheduleKindForInput("0 9 * * *"),
  "not_a_schedule": _cronScheduleKindForInput("not_a_schedule"),
};
console.log(JSON.stringify(cases));
"""
    kinds = json.loads(_run_node(script))

    assert kinds["30m"] == "once"
    assert kinds["2h"] == "once"
    assert kinds["1 day"] == "once"
    assert kinds["2026-05-11"] == "once"
    assert kinds["2026-05-11T08:00"] == "once"
    assert kinds["every 30m"] == "interval"
    assert kinds["Every 2h"] == "interval"
    assert kinds["0 9 * * *"] == "cron"
    assert kinds["not_a_schedule"] == ""


def test_cron_schedule_preset_matching():
    script = _cron_schedule_source() + r"""
const cases = {
  hourly: _cronSchedulePresetIdForValue("every 1h"),
  daily: _cronSchedulePresetIdForValue("0 9 * * *"),
  weekdays: _cronSchedulePresetIdForValue("0 9 * * 1-5"),
  weekly: _cronSchedulePresetIdForValue("0 9 * * 1"),
  monthly: _cronSchedulePresetIdForValue("0 9 1 * *"),
  empty: _cronSchedulePresetIdForValue(""),
  trimMatch: _cronSchedulePresetIdForValue("  0 9 * * *  "),
  custom: _cronSchedulePresetIdForValue("0 9 * * * 0"),
};
console.log(JSON.stringify(cases));
"""
    cases = json.loads(_run_node(script))

    assert cases["hourly"] == "hourly"
    assert cases["daily"] == "daily"
    assert cases["weekdays"] == "weekdays"
    assert cases["weekly"] == "weekly"
    assert cases["monthly"] == "monthly"
    assert cases["empty"] == "custom"
    assert cases["trimMatch"] == "daily"
    assert cases["custom"] == "custom"


def test_cron_schedule_preset_controls_sync_raw_and_preset_paths():
    script = _cron_schedule_source() + r"""
const elements = {};
function $(id) { return elements[id]; }
function makeElement() {
  return {
    value: '',
    style: {},
    listeners: {},
    addEventListener(type, handler) {
      (this.listeners[type] || (this.listeners[type] = [])).push(handler);
    },
    dispatchEvent(eventType) {
      const handlers = this.listeners[eventType] || [];
      for (const handler of handlers) handler({ type: eventType, target: this });
    },
  };
}
function t(key) {
  const dict = {
    cron_schedule_preset_label: 'Preset',
    cron_schedule_preset_hourly: 'Hourly',
    cron_schedule_preset_daily: 'Daily',
    cron_schedule_preset_weekdays: 'Weekdays',
    cron_schedule_preset_weekly: 'Weekly',
    cron_schedule_preset_monthly: 'Monthly',
    cron_schedule_preset_custom: 'Custom',
  };
  return dict[key];
}
function esc(value) { return value == null ? '' : String(value); }

elements.cronFormSchedule = makeElement();
elements.cronFormSchedulePreset = makeElement();
elements.cronFormScheduleOnceWarning = { style: {} };

_initCronSchedulePresetControls();

elements.cronFormSchedule.value = "0 9 * * *";
elements.cronFormSchedule.dispatchEvent('input');
const selectAfterDaily = elements.cronFormSchedulePreset.value;
const warningAfterDaily = elements.cronFormScheduleOnceWarning.style.display;

elements.cronFormSchedule.value = "30m";
elements.cronFormSchedule.dispatchEvent('input');
const selectAfterOnce = elements.cronFormSchedulePreset.value;
const warningAfterOnce = elements.cronFormScheduleOnceWarning.style.display;

elements.cronFormSchedulePreset.value = 'hourly';
elements.cronFormSchedulePreset.dispatchEvent('change');
const presetWriteValue = elements.cronFormSchedule.value;
const selectAfterPresetWrite = elements.cronFormSchedulePreset.value;

elements.cronFormSchedulePreset.value = "custom";
elements.cronFormSchedulePreset.dispatchEvent('change');
const preservedExactPresetValue = elements.cronFormSchedule.value;
const selectAfterExactPresetCustom = elements.cronFormSchedulePreset.value;

elements.cronFormSchedule.value = "advanced: cron expression";
elements.cronFormSchedule.dispatchEvent('input');
elements.cronFormSchedulePreset.value = "custom";
elements.cronFormSchedulePreset.dispatchEvent('change');
const preservedCustomValue = elements.cronFormSchedule.value;
const selectAfterCustom = elements.cronFormSchedulePreset.value;

console.log(JSON.stringify({
  selectAfterDaily,
  warningAfterDaily,
  selectAfterOnce,
  warningAfterOnce,
  presetWriteValue,
  selectAfterPresetWrite,
  preservedExactPresetValue,
  selectAfterExactPresetCustom,
  preservedCustomValue,
  selectAfterCustom,
}));
"""
    result = json.loads(_run_node(script))

    assert result["selectAfterDaily"] == "daily"
    assert result["warningAfterDaily"] == "none"
    assert result["selectAfterOnce"] == "custom"
    assert result["warningAfterOnce"] == ""
    assert result["presetWriteValue"] == "every 1h"
    assert result["selectAfterPresetWrite"] == "hourly"
    assert result["preservedExactPresetValue"] == "every 1h"
    assert result["selectAfterExactPresetCustom"] == "custom"
    assert result["preservedCustomValue"] == "advanced: cron expression"
    assert result["selectAfterCustom"] == "custom"


def test_cron_form_surfaces_one_shot_warning_copy_markers_and_preset_markup():
    panels = PANELS_JS.read_text(encoding="utf-8")
    style = STYLE_CSS.read_text(encoding="utf-8")
    i18n = I18N_JS.read_text(encoding="utf-8")

    assert "id=\"cronFormScheduleOnceWarning\"" in panels
    assert "id=\"cronFormSchedulePreset\"" in panels
    assert "cron_schedule_once_warning" in panels
    assert "_cronSchedulePresetIdForValue" in panels
    assert "_cronSchedulePresetOptionHtml" in panels
    assert "_initCronSchedulePresetControls" in panels
    assert "addEventListener('input', _syncCronSchedulePresetAndWarning" in panels
    assert "addEventListener('change', _syncCronSchedulePresetAndWarning" in panels
    assert "addEventListener('change', _applyCronSchedulePresetSelection" in panels
    assert ".cron-once-warning" in style
    assert "id: 'hourly'" in panels
    assert "id: 'daily'" in panels
    assert "id: 'weekdays'" in panels
    assert "id: 'weekly'" in panels
    assert "id: 'monthly'" in panels
    assert "id: 'custom'" in panels
    assert "Duration forms like '30m' run once" in i18n


def test_cron_form_save_payload_still_uses_visible_raw_schedule_only():
    save_block = _cron_schedule_save_source()
    panels = PANELS_JS.read_text(encoding="utf-8")

    assert "cronFormSchedulePreset" not in save_block
    assert "const schedule=schEl.value.trim();" in save_block
    assert "const updates = {job_id: _editingCronId, schedule, profile: profile, toast_notifications: toastNotifications}" in panels


def test_cron_form_i18n_has_preset_keys():
    i18n = I18N_JS.read_text(encoding="utf-8")
    required_keys = [
        "cron_schedule_preset_label",
        "cron_schedule_preset_hourly",
        "cron_schedule_preset_daily",
        "cron_schedule_preset_weekdays",
        "cron_schedule_preset_weekly",
        "cron_schedule_preset_monthly",
        "cron_schedule_preset_custom",
    ]

    for key in required_keys:
        assert i18n.count(key) >= 14
