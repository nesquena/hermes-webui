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


def test_cron_form_surfaces_one_shot_warning_copy_and_styles():
    panels = PANELS_JS.read_text(encoding="utf-8")
    style = STYLE_CSS.read_text(encoding="utf-8")
    i18n = I18N_JS.read_text(encoding="utf-8")

    assert "id=\"cronFormScheduleOnceWarning\"" in panels
    assert "cron_schedule_once_warning" in panels
    assert "_syncCronScheduleWarning" in panels
    assert "addEventListener('input', _syncCronScheduleWarning" in panels
    assert "addEventListener('change', _syncCronScheduleWarning" in panels

    assert ".cron-once-warning" in style
    assert i18n.count("cron_schedule_once_warning") >= 9
    assert "Duration forms like '30m' run once" in i18n
