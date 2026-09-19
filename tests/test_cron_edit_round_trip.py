"""Regression coverage for issue #7352: cron edit/duplicate uses
``schedule_display`` ("once at ...") as editable input, which the Agent
parser at ``cron.jobs.parse_schedule`` rejects with ``ValueError`` and
the resulting round-trip has been failing with HTTP 500 since the
WebUI's initial public release.

Two layers are fixed in this regression:

1. ``static/panels.js`` adds ``_cronScheduleForEdit`` and uses it in
   both ``openCronEdit`` and ``duplicateCurrentCron`` so the editable
   field holds the canonical Agent-parseable value (one-shot
   ``schedule.run_at``; recurring ``schedule.expr``; interval keeps its
   existing text).

2. ``api/routes.py::_handle_cron_update`` now wraps the ``update_job``
   call in ``try/except ValueError`` and returns HTTP 400 with the
   parser message instead of letting the exception escape as a 500.
"""
from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")


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


def _function_body(name: str) -> str:
    marker = f"function {name}("
    start = PANELS_JS.find(marker)
    assert start != -1, f"{name} not found"
    paren = PANELS_JS.find("(", start)
    assert paren != -1, f"{name} params not found"
    depth = 0
    for idx in range(paren, len(PANELS_JS)):
        ch = PANELS_JS[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                brace = PANELS_JS.find("{", idx)
                break
    else:
        raise AssertionError(f"{name} params did not terminate")
    assert brace != -1, f"{name} body not found"
    depth = 0
    for idx in range(brace, len(PANELS_JS)):
        ch = PANELS_JS[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return PANELS_JS[brace + 1 : idx]
    raise AssertionError(f"{name} body did not terminate")


# --- Frontend: _cronScheduleForEdit helper exists and is referenced ----------


def test_cron_schedule_for_edit_helper_exists():
    """A canonical-by-kind schedule picker is required so that opening
    an existing one-shot job for edit/duplicate populates the editable
    field with the Agent-parseable ``run_at`` rather than the
    presentation-only ``schedule_display`` label."""
    body = _function_body("_cronScheduleForEdit")
    assert body, "_cronScheduleForEdit must exist"
    # One-shot: use run_at, never the display label.
    assert "kind === 'once'" in body
    assert "run_at" in body
    # Recurring cron: prefer the current Agent schema field ``expr``;
    # accept legacy ``expression`` for back-compat.
    assert "sched.expr" in body
    assert "sched.expression" in body
    # Final fallback: only use schedule_display if it isn't the unsafe
    # ``once at ...`` presentation form.
    assert "schedule_display" in body
    assert "once at" in body
    assert "return ''" in body


def test_open_cron_edit_uses_helper():
    """openCronEdit must call _cronScheduleForEdit, not the legacy
    ``schedule_display || schedule.expression`` chain that round-trips
    the unsafe display label back to the Agent parser."""
    body = _function_body("openCronEdit")
    assert "schedule: _cronScheduleForEdit(job)" in body
    # Legacy chain must be gone from this function.
    assert "job.schedule_display" not in body
    assert "job.schedule.expression" not in body


def test_duplicate_current_cron_uses_helper():
    """duplicateCurrentCron has the same root cause as openCronEdit; the
    fix must cover both call sites or duplicating a one-shot job
    would also fail."""
    body = _function_body("duplicateCurrentCron")
    assert "schedule: _cronScheduleForEdit(job)" in body
    assert "job.schedule_display" not in body


def test_save_cron_form_still_submits_schedule_value():
    """Regression guard: the saved-field name stays ``schedule`` and
    the helper must not have leaked into the submit path (which already
    reads ``schEl.value`` and submits it unchanged)."""
    body = _function_body("saveCronForm")
    assert "const schedule=schEl.value.trim();" in body
    assert "schedule," in body


# --- Backend: _handle_cron_update converts ValueError to HTTP 400 --------------


def _install_cron_jobs_stub(monkeypatch, *, update_fn):
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.update_job = update_fn
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)


def test_cron_update_value_error_returns_400(monkeypatch):
    """#7352: when the Agent parser rejects the submitted schedule with
    ValueError (the "once at ..." case before the frontend fix, or any
    user-typed garbage), the route must return 400 with the parser
    message instead of letting the exception escape as 500."""
    import api.routes as routes

    def _raise(job_id, updates):
        raise ValueError(
            "Invalid schedule 'once at 2026-08-28 16:05'. Use:\n"
            "  - Timestamp: '2026-02-03T14:00:00' (one-shot at time)"
        )

    _install_cron_jobs_stub(monkeypatch, update_fn=_raise)

    handler = _JSONHandler()
    routes._handle_cron_update(
        handler,
        {
            "job_id": "test-job",
            "schedule": "once at 2026-08-28 16:05",
        },
    )

    assert handler.status == 400, (
        "schedule validation failures must surface as 400, not 500 "
        "(#7352)"
    )
    payload = _payload(handler)
    assert "error" in payload
    assert "Invalid schedule" in payload["error"], (
        "parser message must propagate so the WebUI can show a useful "
        "validation toast"
    )
    assert "once at 2026-08-28 16:05" in payload["error"]


def test_cron_update_preserves_404_when_job_missing(monkeypatch):
    """The new ValueError handler must not swallow the not-found path:
    when ``update_job`` returns ``None`` (no such job), the route must
    still respond 404."""
    import api.routes as routes

    def _missing(job_id, updates):
        return None

    _install_cron_jobs_stub(monkeypatch, update_fn=_missing)

    handler = _JSONHandler()
    routes._handle_cron_update(
        handler,
        {"job_id": "ghost", "schedule": "0 9 * * *"},
    )

    assert handler.status == 404
    assert "not found" in _payload(handler).get("error", "").lower()


def test_cron_update_valid_schedule_still_succeeds(monkeypatch):
    """Happy path: a parseable schedule (the post-fix frontend submits
    ``run_at`` rather than ``schedule_display``) reaches ``update_job``
    and the route returns 200 with the job payload."""
    import api.routes as routes

    calls = []
    job = {
        "id": "test-job",
        "schedule": {
            "kind": "once",
            "run_at": "2026-08-28T16:05:00-05:00",
            "display": "once at 2026-08-28 16:05",
        },
        "schedule_display": "once at 2026-08-28 16:05",
    }

    def _ok(job_id, updates):
        calls.append((job_id, updates))
        return {**job, **updates}

    _install_cron_jobs_stub(monkeypatch, update_fn=_ok)

    handler = _JSONHandler()
    routes._handle_cron_update(
        handler,
        {
            "job_id": "test-job",
            "schedule": "2026-08-28T16:05:00-05:00",  # canonical ISO
        },
    )

    assert handler.status == 200
    assert calls and calls[0][0] == "test-job"
    assert calls[0][1]["schedule"] == "2026-08-28T16:05:00-05:00"
