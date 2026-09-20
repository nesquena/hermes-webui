"""Regression coverage for #7352: one-shot cron edit/duplicate round-trip.

The edit and duplicate forms previously populated the schedule field from
``job.schedule_display`` (human-readable text like ``once at 2026-08-28 16:00``)
and submitted it back as the canonical ``schedule`` value, which the agent's
parser rejects -> HTTP 500. The forms must instead pre-fill the canonical
stored value (``schedule.run_at`` for one-shot, ``schedule.expr`` for cron),
and the update route must turn parser ``ValueError`` into a 400.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PANELS_JS = REPO / "static" / "panels.js"
ROUTES_PY = REPO / "api" / "routes.py"
NODE = shutil.which("node")

_ONE_SHOT_JOB = {
    "id": "once-job",
    "name": "One shot",
    "schedule": {
        "kind": "once",
        "run_at": "2026-08-28T16:05:00-05:00",
        "display": "once at 2026-08-28 16:05",
    },
    "schedule_display": "once at 2026-08-28 16:05",
}

_CRON_JOB = {
    "id": "cron-job",
    "name": "Daily",
    "schedule": {"kind": "cron", "expr": "0 9 * * *", "display": "0 9 * * *"},
    "schedule_display": "0 9 * * *",
}


# ---------------------------------------------------------------------------
# Frontend: the schedule pre-fill helper
# ---------------------------------------------------------------------------


def _schedule_helper_source() -> str:
    src = PANELS_JS.read_text(encoding="utf-8")
    start = src.find("function _cronScheduleEditableText")
    if start < 0:
        pytest.fail("_cronScheduleEditableText is missing from static/panels.js")
    end = src.find("function _syncCronScheduleWarning", start)
    if end < 0:
        pytest.fail("_cronScheduleEditableText must stay near the cron schedule helpers")
    return src[start:end]


def _run_node(script: str) -> str:
    proc = subprocess.run(
        [NODE, "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_cron_schedule_editable_text_returns_canonical_stored_value():
    script = _schedule_helper_source() + r"""
const cases = {
  once: _cronScheduleEditableText(%s),
  cron: _cronScheduleEditableText(%s),
  legacyOnly: _cronScheduleEditableText({ schedule_display: 'once at 2026-08-28 16:05' }),
  empty: _cronScheduleEditableText({}),
  noJob: _cronScheduleEditableText(null),
};
console.log(JSON.stringify(cases));
""" % (json.dumps(_ONE_SHOT_JOB), json.dumps(_CRON_JOB))
    got = json.loads(_run_node(script))

    # One-shot: canonical run_at, NOT the derived display text.
    assert got["once"] == "2026-08-28T16:05:00-05:00"
    # Cron: canonical expr (the old code looked for the wrong key,
    # schedule.expression, and fell back to display text).
    assert got["cron"] == "0 9 * * *"
    # Legacy/unknown shapes keep the display text as before.
    assert got["legacyOnly"] == "once at 2026-08-28 16:05"
    assert got["empty"] == ""
    assert got["noJob"] == ""


def _form_functions_source() -> str:
    src = PANELS_JS.read_text(encoding="utf-8")
    start = src.find("function duplicateCurrentCron")
    if start < 0:
        pytest.fail("duplicateCurrentCron is missing")
    end = src.find("function _renderCronForm", start)
    if end < 0:
        pytest.fail("duplicateCurrentCron/_renderCronForm boundary marker missing")
    helper_start = src.find("function _cronScheduleEditableText")
    helper_end = src.find("function _syncCronScheduleWarning", helper_start)
    return src[helper_start:helper_end] + "\n" + src[start:end]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize(
    "job,expected",
    [
        (_ONE_SHOT_JOB, "2026-08-28T16:05:00-05:00"),
        (_CRON_JOB, "0 9 * * *"),
    ],
)
def test_cron_edit_and_duplicate_forms_prefill_canonical_schedule(job, expected):
    script = _form_functions_source() + r"""
const captured = [];
function _renderCronForm(opts) { captured.push(opts); }
function _bindCronSkillPicker() {}
function _refreshCronProfileSelect() {}
function loadCronProfiles() { return Promise.resolve(); }
function api() { return { catch: () => {} }; }
function switchPanel() {}
function t(key) { return key; }
function showToast() {}
let _currentPanel = 'tasks';
let _cronList = [];
_cronSkillsCache = true;  // declared by the extracted source; skip api() branch
let _currentCronDetail = null;

const job = %s;
_currentCronDetail = job;
openCronEdit(job);
duplicateCurrentCron();
console.log(JSON.stringify({ edit: captured[0].schedule, duplicate: captured[1].schedule }));
""" % json.dumps(job)
    got = json.loads(_run_node(script))

    assert got["edit"] == expected
    assert got["duplicate"] == expected


def test_cron_forms_no_longer_read_schedule_display_or_expression():
    """Guard the exact regression shape: neither form path may pre-fill from
    schedule_display or the wrong schedule.expression key."""
    src = PANELS_JS.read_text(encoding="utf-8")
    edit = src[src.find("function openCronEdit"): src.find("function _renderCronForm")]
    duplicate = src[src.find("function duplicateCurrentCron"): src.find("function openCronCreate")]
    for body in (edit, duplicate):
        assert "_cronScheduleEditableText(job)" in body
        assert "schedule_display ||" not in body
        assert "schedule.expression" not in body


# ---------------------------------------------------------------------------
# Backend: the update route must 400 on parser ValueError
# ---------------------------------------------------------------------------


class _JSONHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass


def _payload(handler):
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _install_fake_cron_jobs(monkeypatch, update_job):
    import api.routes as routes

    calls = []

    def _update_job(job_id, updates):
        calls.append((job_id, updates))
        return update_job(job_id, updates)

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.update_job = _update_job
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    return routes, calls


def test_cron_update_invalid_schedule_returns_400_with_parser_message(monkeypatch):
    def _reject(job_id, updates):
        raise ValueError("invalid schedule: 'once at 2026-08-28 16:05'")

    routes_obj, calls = _install_fake_cron_jobs(monkeypatch, _reject)

    handler = _JSONHandler()
    routes_obj._handle_cron_update(
        handler,
        {"job_id": "once-job", "schedule": "once at 2026-08-28 16:05"},
    )

    assert handler.status == 400
    assert "invalid schedule" in _payload(handler)["error"]
    assert calls == [("once-job", {"schedule": "once at 2026-08-28 16:05"})]


def test_cron_update_missing_job_still_returns_404(monkeypatch):
    routes_obj, _ = _install_fake_cron_jobs(monkeypatch, lambda job_id, updates: None)

    handler = _JSONHandler()
    routes_obj._handle_cron_update(handler, {"job_id": "missing", "schedule": "0 9 * * *"})

    assert handler.status == 404
    assert "not found" in _payload(handler)["error"].lower()


def test_cron_update_valid_one_shot_schedule_persists(monkeypatch):
    updated = {
        "id": "once-job",
        "name": "One shot",
        "schedule": {
            "kind": "once",
            "run_at": "2026-08-28T16:05:00-05:00",
            "display": "once at 2026-08-28 16:05",
        },
        "schedule_display": "once at 2026-08-28 16:05",
    }

    routes_obj, calls = _install_fake_cron_jobs(
        monkeypatch, lambda job_id, updates: {**updated, **updates}
    )

    handler = _JSONHandler()
    routes_obj._handle_cron_update(
        handler,
        {"job_id": "once-job", "schedule": "2026-08-28T16:05:00-05:00"},
    )

    assert handler.status == 200
    assert _payload(handler)["ok"] is True
    assert calls == [("once-job", {"schedule": "2026-08-28T16:05:00-05:00"})]
