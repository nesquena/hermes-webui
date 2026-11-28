"""Regression coverage for #9403: saving the cron edit form must not write back
fields that changed elsewhere while the form was open.

The edit form is filled once, by ``openCronEdit``. If ``hermes cron edit``, the
agent's ``cronjob`` tool or another tab changes the job meanwhile, the form still
shows the old values. ``update_job`` merges ``{**job, **updates}``, so every
field the form re-sends unchanged silently reverts that other change.

These tests run the real ``saveCronForm`` out of ``static/panels.js`` in Node,
capture the ``/api/crons/update`` body, and feed it through the real
``_handle_cron_update`` against a job that was changed after the form opened.
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
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")


def _function_source(name: str) -> str:
    marker = f"function {name}("
    start = PANELS_JS.find(marker)
    assert start != -1, f"{name} not found"
    if PANELS_JS[max(0, start - 6):start] == "async ":
        start -= 6
    paren = PANELS_JS.find("(", start)
    depth = 0
    brace = -1
    for idx in range(paren, len(PANELS_JS)):
        ch = PANELS_JS[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                brace = PANELS_JS.find("{", idx)
                break
    assert brace != -1, f"{name} body not found"
    depth = 0
    for idx in range(brace, len(PANELS_JS)):
        ch = PANELS_JS[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return PANELS_JS[start:idx + 1]
    raise AssertionError(f"{name} body did not terminate")


def _optional_source(name: str) -> str:
    return _function_source(name) if f"function {name}(" in PANELS_JS else ""


# The job as the form loaded it (the issue's "sales" job, an `every 1h` interval
# whose schedule_display is `every 60m`).
OPENED_JOB = {
    "id": "job9403",
    "name": "sales",
    "prompt": "P0 weekly sales report",
    "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
    "schedule_display": "every 60m",
    "deliver": "local",
    "profile": "",
    "model": None,
    "provider": None,
    "toast_notifications": True,
    "badge_notifications": True,
}


def _save_body(form: dict, *, picker_touched: bool = False, model_value: str = "") -> dict:
    """Run the real saveCronForm with the edit form showing ``form``."""
    sources = "\n".join(
        _optional_source(n)
        for n in (
            "_cronScheduleForEdit",
            "_modelBareNameForProvider",
            "_cronModelBareName",
            "_cronProviderForClear",
            "_cronEditChangedFields",
        )
    )
    script = f"""
{sources}
{_function_source('saveCronForm')}
let _cronPreFormDetail = {json.dumps(OPENED_JOB)};
let _editingCronId = {json.dumps(OPENED_JOB['id'])};
let _cronModelPickerTouched = {json.dumps(picker_touched)};
let _cronMode = 'edit', _currentPanel = 'tasks', _currentCronDetailKey = 'k';
let _cronIsDuplicate = false, _cronSelectedSkills = [], _cronList = [];
const form = {json.dumps(form)};
const fields = {{
  cronFormName: {{value: form.name}},
  cronFormSchedule: {{value: form.schedule}},
  cronFormPrompt: {{value: form.prompt}},
  cronFormDeliver: {{value: form.deliver}},
  cronFormProfile: {{value: form.profile}},
  cronFormToastNotifications: {{checked: form.toast}},
  cronFormBadgeNotifications: {{checked: form.badge}},
  cronFormError: {{style: {{}}, textContent: ''}},
  cronFormModel: {{value: {json.dumps(model_value)}, dataset: {{loaded: '1'}}}},
}};
const $ = id => fields[id] || null;
const t = k => k;
let sent = null;
async function api(url, opts) {{ sent = JSON.parse(opts.body); throw new Error('stop'); }}
saveCronForm().then(() => process.stdout.write(JSON.stringify(sent)));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _unchanged_form(**overrides) -> dict:
    form = {
        "name": OPENED_JOB["name"],
        "schedule": OPENED_JOB["schedule_display"],
        "prompt": OPENED_JOB["prompt"],
        "deliver": OPENED_JOB["deliver"],
        "profile": OPENED_JOB["profile"],
        "toast": True,
        "badge": True,
    }
    form.update(overrides)
    return form


class _Handler:
    def __init__(self):
        self.status = None
        self.wfile = io.BytesIO()

    def send_response(self, code):
        self.status = code

    def send_header(self, *_):
        pass

    def end_headers(self):
        pass


def _apply_through_route(monkeypatch, body: dict, current_job: dict) -> dict:
    """Send ``body`` through the real _handle_cron_update onto ``current_job``,
    with update_job merging the way hermes-agent's does ({**job, **updates})."""
    import api.routes as routes

    store = {current_job["id"]: dict(current_job)}

    def update_job(job_id, updates):
        job = store.get(job_id)
        if job is None:
            return None
        store[job_id] = {**job, **updates}
        return store[job_id]

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.update_job = update_job
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    monkeypatch.setattr(routes, "_cron_job_for_api", lambda job: job)

    handler = _Handler()
    routes._handle_cron_update(handler, body)
    assert handler.status == 200
    return store[current_job["id"]]


@pytest.mark.parametrize(
    "other_edit",
    [
        pytest.param({"prompt": "P1 include refunds"}, id="race_prompt"),
        pytest.param({"model": "gpt-other", "provider": "openrouter"}, id="race_model"),
        pytest.param({"deliver": "telegram", "schedule_display": "every 2h"}, id="race_deliver_schedule"),
    ],
)
def test_name_only_save_keeps_edit_made_while_form_was_open(monkeypatch, other_edit):
    # A opens the form (OPENED_JOB); B changes the job; A changes only Name and saves.
    body = _save_body(_unchanged_form(name="sales (weekly)"))
    assert body["job_id"] == OPENED_JOB["id"]
    assert body["name"] == "sales (weekly)"
    job_after_b = {**OPENED_JOB, **other_edit}
    after = _apply_through_route(monkeypatch, body, job_after_b)
    assert after["name"] == "sales (weekly)"
    for key, value in other_edit.items():
        assert after[key] == value, f"A's save reverted B's {key}: sent {body}"


def test_name_only_save_does_not_resend_schedule():
    # Re-sending `schedule` makes update_job recompute next_run_at (#9403).
    body = _save_body(_unchanged_form(name="sales (weekly)"))
    assert set(body) == {"job_id", "name"}, body


def test_fields_the_user_changed_are_still_sent():
    body = _save_body(
        _unchanged_form(prompt="P2 A's prompt", schedule="every 2h", deliver="telegram",
                        profile="work", toast=False, badge=False)
    )
    assert body == {
        "job_id": OPENED_JOB["id"],
        "prompt": "P2 A's prompt",
        "schedule": "every 2h",
        "deliver": "telegram",
        "profile": "work",
        "toast_notifications": False,
        "badge_notifications": False,
    }


def test_model_pick_is_sent_and_untouched_default_is_not():
    picked = _save_body(_unchanged_form(), picker_touched=True, model_value="gpt-5")
    assert picked["model"] == "gpt-5"
    assert "provider" in picked
    untouched = _save_body(_unchanged_form())
    assert "model" not in untouched and "provider" not in untouched, untouched
