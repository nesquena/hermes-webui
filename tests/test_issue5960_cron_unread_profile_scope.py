"""Regression coverage for cross-profile cron unread badges (#5960)."""

from __future__ import annotations

import io
import json
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")


class _JSONHandler:
    def __init__(self):
        self.status = None
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


def test_recent_completions_are_read_from_the_active_profile_home(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    entered_homes = []

    @contextmanager
    def profile_context(home):
        entered_homes.append(home)
        yield

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")

    def list_jobs(include_disabled=True):
        assert entered_homes == ["/profiles/vops"]
        return [
            {
                "id": "vops-job",
                "name": "Vops job",
                "last_run_at": 20,
                "last_status": "success",
            }
        ]

    cron_jobs.list_jobs = list_jobs
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "vops")
    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile", lambda name: f"/profiles/{name}"
    )
    monkeypatch.setattr(profiles, "cron_profile_context_for_home", profile_context)
    monkeypatch.setattr(routes, "_latest_cron_session_info_for_jobs", lambda *args: {})

    handler = _JSONHandler()
    routes._handle_cron_recent(handler, SimpleNamespace(query="since=10"))

    assert handler.status == 200
    assert entered_homes == ["/profiles/vops"]
    assert [row["job_id"] for row in _payload(handler)["completions"]] == ["vops-job"]


def test_successful_profile_switch_resets_unread_cron_state():
    switch_start = PANELS_JS.index("async function switchToProfile(name) {")
    switch_end = PANELS_JS.index("// ── Cron completion alerts", switch_start)
    switch_body = PANELS_JS[switch_start:switch_end]

    state_update = switch_body.index("S.activeProfile = data.active || name;")
    reset_call = switch_body.index("_resetCronUnreadForProfileSwitch();")
    assert reset_call > state_update

    reset_start = PANELS_JS.index("function _resetCronUnreadForProfileSwitch(){")
    reset_end = PANELS_JS.index("\n}", reset_start)
    reset_body = PANELS_JS[reset_start:reset_end]
    assert "_cronNewJobIds.clear();" in reset_body
    assert "_cronPollSince=Date.now()/1000;" in reset_body
    assert "updateCronBadge();" in reset_body
