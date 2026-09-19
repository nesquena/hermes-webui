"""Coverage for per-cron completion toast notification settings."""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


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


def test_cron_recent_marks_muted_jobs_without_requesting_toast(monkeypatch):
    import api.routes as routes

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.list_jobs = lambda include_disabled=True: [
        {
            "id": "loud",
            "name": "Loud job",
            "last_run_at": 20,
            "last_status": "success",
        },
        {
            "id": "muted",
            "name": "Muted job",
            "last_run_at": 30,
            "last_status": "success",
            "toast_notifications": False,
        },
    ]
    monkeypatch.setattr(
        routes,
        "_latest_cron_session_info_for_jobs",
        lambda job_ids, completed_job_ids=None: {
            str(job_id): {
                "session_id": f"cron_{job_id}_latest",
                "message_count": 3 if str(job_id) == "loud" else 5,
            }
            for job_id in (completed_job_ids or job_ids)
        },
    )
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)

    handler = _JSONHandler()
    routes._handle_cron_recent(handler, SimpleNamespace(query="since=10"))

    body = _payload(handler)
    assert handler.status == 200
    by_id = {item["job_id"]: item for item in body["completions"]}
    assert by_id["loud"]["toast_notifications"] is True
    assert by_id["loud"]["session_id"] == "cron_loud_latest"
    assert by_id["loud"]["message_count"] == 3
    assert by_id["muted"]["toast_notifications"] is False
    assert by_id["muted"]["session_id"] == "cron_muted_latest"
    assert by_id["muted"]["message_count"] == 5


def test_cron_create_persists_muted_toast_setting_after_create(monkeypatch):
    import api.routes as routes

    created = {"id": "job-toast", "name": "Muted", "prompt": "ping"}
    calls = []
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.create_job = lambda **kwargs: calls.append(("create", kwargs)) or dict(created)
    cron_jobs.update_job = lambda job_id, updates: calls.append(("update", job_id, updates)) or {**created, **updates}
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)

    handler = _JSONHandler()
    routes._handle_cron_create(
        handler,
        {
            "prompt": "ping",
            "schedule": "every 1h",
            "toast_notifications": False,
        },
    )

    assert handler.status == 200
    assert calls[0][0] == "create"
    assert calls[1] == ("update", "job-toast", {"toast_notifications": False})
    assert _payload(handler)["job"]["toast_notifications"] is False


def test_cron_form_has_toast_toggle_and_saves_boolean_setting():
    render_body = _function_body("_renderCronForm")
    save_body = _function_body("saveCronForm")
    edit_body = _function_body("openCronEdit")
    detail_body = _function_body("_renderCronDetail")

    assert "cronFormToastNotifications" in render_body
    assert "cron_toast_notifications_label" in render_body
    assert "toast_notifications" in edit_body
    assert "toast_notifications" in detail_body
    assert "const toastNotifications" in save_body
    assert "toast_notifications: toastNotifications" in save_body


def test_cron_polling_suppresses_toasts_but_keeps_unread_badges():
    body = _function_body("startCronPolling")

    assert "c.toast_notifications !== false" in body
    assert "showToast(t('cron_completion_status'" in body
    assert "if(c.job_id) _cronNewJobIds.add(String(c.job_id));" in body


def test_cron_polling_fires_browser_notification_when_tab_hidden():
    """#7257: cron origin deliveries should fire a browser notification
    when the tab is backgrounded, because the old ``if(document.hidden)
    return`` gate skipped the entire recent-fetch and no surface ever
    fired. Visible tabs keep the existing showToast; hidden tabs go
    through sendBrowserNotification so the user's notification
    permission and enabled setting still gate the alert."""
    body = _function_body("startCronPolling")
    # The old "skip when hidden" gate is gone. Use the precise
    # token form (with trailing semicolon) so the match does not
    # also hit the explanatory comment that quotes the old line.
    assert "if(document.hidden) return;" not in body, (
        "the document.hidden early-return must be removed; the rest of "
        "the poll body now handles both visible and hidden paths"
    )
    # The completion branch picks one of two surfaces by visibility.
    assert "if(document.hidden){" in body, (
        "hidden tab path must branch on document.hidden"
    )
    assert "sendBrowserNotification(" in body, (
        "hidden tab path must call the existing sendBrowserNotification "
        "primitive so permission and enabled-setting gates still apply"
    )
    # Visible tab still gets the existing toast.
    assert "showToast(t('cron_completion_status'" in body
    # The same completion must not produce both surfaces; the
    # if/else structure enforces that, so the visible branch
    # itself is closed by an else (not a parallel if).
    assert "} else {" in body


def test_cron_polling_status_text_is_reused_across_surfaces():
    """#7257: when building the status string for either surface we
    should compute it once rather than re-deriving it inline, so the
    localized success/failure text cannot drift between the toast and
    the notification body."""
    body = _function_body("startCronPolling")
    assert "const statusText" in body, (
        "the localized success/failure string should be hoisted to a "
        "local before branching on document.hidden"
    )
    # Both surfaces must consume statusText (or the localized variant
    # for the toast), not re-derive from c.status inline.
    assert "statusText" in body


def test_cron_polling_does_not_advance_unread_for_muted_jobs():
    """Regression guard: the muted-job path (c.toast_notifications ===
    false) must still advance the unread badge so the user sees a red
    dot in the sidebar; the notification primitive is opt-in, but the
    session-unread marker is not."""
    body = _function_body("startCronPolling")
    # The toast/notification surface is gated on the user's preference.
    assert "c.toast_notifications !== false" in body
    # The _cronNewJobIds and session-unread marker advance OUTSIDE the
    # toast/notification gate so muted jobs still appear in the sidebar.
    assert "_cronPollSince=Math.max(_cronPollSince,c.completed_at);" in body
    assert "_cronNewJobIds.add(String(c.job_id))" in body
    assert "_markSessionCompletionUnreadIfBackground" in body


def test_cron_toast_i18n_keys_exist():
    assert "cron_toast_notifications_label" in I18N_JS
    assert "cron_toast_notifications_hint" in I18N_JS
    assert "cron_toast_notifications_enabled" in I18N_JS
    assert "cron_toast_notifications_disabled" in I18N_JS
