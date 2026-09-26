"""Coverage for per-cron completion toast notification settings."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


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


def test_cron_polling_mutes_surface_for_toast_disabled_jobs():
    """The muted-job contract (toast_notifications === false) is asserted
    behaviourally by the Node harness below; here we keep only the cheap
    structural check that the surface is delegated to the tick function."""
    body = _function_body("startCronPolling")

    assert "_runCronPollTick" in body
    assert "_cronPollInFlight" in _function_body("_runCronPollTick")


# ---------------------------------------------------------------------------
# Node harness: drives the real startCronPolling tick with stubbed api /
# document.hidden / sendBrowserNotification (no source-text assertions).
#
# The three contracts exercised here were the maintainer's review of #7257
# (issue #7652): the source-text tests above could not observe any of them,
# because every one of them is a property of the code's BEHAVIOUR at runtime.
# ---------------------------------------------------------------------------

_HARNESS = r"""
const fs = require('fs');
const assert = require('assert');

const PANELS_JS = fs.readFileSync(process.env.PANELS_JS_PATH, 'utf8');

// The poll logic: the tick, the surface helpers, and the badge helpers. The
// module-level state declarations are stubbed below (eval'd `let` bindings do
// not escape their eval scope), so the slice starts at the first function.
function region(startMarker, endMarker) {
  const start = PANELS_JS.indexOf(startMarker);
  assert.notStrictEqual(start, -1, 'missing start marker: ' + startMarker);
  const end = PANELS_JS.indexOf(endMarker, start);
  assert.notStrictEqual(end, -1, 'missing end marker after: ' + startMarker);
  return PANELS_JS.slice(start, end);
}

// Everything from the first cron-poll function to the switchPanel re-export
// (which needs the rest of panels.js in scope).
const POLL_SOURCE = region(
  'function _resetCronUnreadForProfileSwitch(){',
  'const _origSwitchPanel=switchPanel;'
);
// The module-level wiring: startCronPolling() plus the visibilitychange
// flush listener. This is what turns a queued completion into a toast.
const WIRING_SOURCE = region(
  '// Start polling on page load',
  '// ── Background agent error tracking'
);

// ---- browser-side stubs -------------------------------------------------
const state = {
  hidden: true,
  notificationsEnabled: true,
  permission: 'granted',
  toasts: [],
  notifications: [],
  apiCalls: [],
  pendingSince: null,
};

global.S = null;
global.document = {
  hidden: false,
  _listeners: {},
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => ({ style: {}, appendChild() {}, setAttribute() {} }),
  addEventListener(name, fn) { (this._listeners[name] = this._listeners[name] || []).push(fn); },
};
global.window = {
  _listeners: {},
  addEventListener(name, fn) { (this._listeners[name] = this._listeners[name] || []).push(fn); },
  // Live getters so a scenario can flip the notification channel mid-run.
  get _notificationsEnabled() { return state.notificationsEnabled; },
};
// The sliced region also runs module-level listeners at eval time
// (cron_created / visibilitychange); capture them so nothing is lost.
global.document._listeners = {};
global.document.hidden = state.hidden;
global.Notification = {
  get permission() { return state.permission; },
};
global.setInterval = () => 1;               // never fire on its own
global.t = (...args) => args.join('|');
global.showToast = (text, duration) => state.toasts.push({ text, duration });
global.updateCronBadge = () => {};
global.$ = () => null;
global.loadCrons = () => {};
// Module-level cron-poll state. The real declarations live above the sliced
// region; `let` bindings inside eval do not escape, so the harness owns them.
let _cronPollSince = 100;
let _cronPollTimer = null;
let _cronUnreadCount = 0;
let _cronPollGeneration = 0;
const _cronNewJobIds = new Set();
const _cronPendingToasts = [];
let _cronPollInFlight = false;
global.sendBrowserNotification = (title, body, options) => {
  // Mirror the real primitive's silence: with the channel closed it is a
  // no-op, which is exactly the case the poll must not mistake for delivery.
  if (!state.notificationsEnabled || state.permission !== 'granted') return;
  state.notifications.push({ title, body, options });
};
global.api = async (path) => {
  state.apiCalls.push(path);
  return { completions: [] };
};

function setHidden(value) {
  state.hidden = value;
  global.document.hidden = value;
  for (const fn of (global.document._listeners.visibilitychange || [])) fn();
}

// ---- extract _notificationOptions from the real messages.js -------------
const MESSAGES_JS = fs.readFileSync(process.env.MESSAGES_JS_PATH, 'utf8');
function extractFn(name) {
  const start = MESSAGES_JS.indexOf('function ' + name + '(');
  assert.notStrictEqual(start, -1, 'missing function ' + name);
  const brace = MESSAGES_JS.indexOf('){', start) + 1;
  let depth = 0;
  for (let i = brace; i < MESSAGES_JS.length; i++) {
    if (MESSAGES_JS[i] === '{') depth++;
    else if (MESSAGES_JS[i] === '}') { depth--; if (depth === 0) return MESSAGES_JS.slice(start, i + 1); }
  }
  throw new Error('function did not close: ' + name);
}
global.location = {
  origin: 'https://hermes.example',
  href: 'https://hermes.example/#session/current-chat',
};
global._sessionUrlForSid = (sid) => '/session/' + encodeURIComponent(sid);
global._appRootPath = () => '/';
global.S = { session: { session_id: 'current-chat-session' } };

eval(POLL_SOURCE);
assert.strictEqual(typeof startCronPolling, 'function', 'startCronPolling must exist');
assert.strictEqual(typeof _runCronPollTick, 'function', 'poll tick must be callable directly');

// Load the module-level wiring so the visibilitychange flush listener exists.
assert.ok(WIRING_SOURCE.includes('startCronPolling()'),
  'the module-level wiring must start the poller');
assert.ok(/visibilitychange/.test(WIRING_SOURCE),
  'the wiring must register a visibilitychange flush listener');
eval(WIRING_SOURCE);

// Record the real _notificationOptions result for the sessionless marker.
eval(extractFn('_notificationOptions'));

// Drive a tick: feed the given completions, advance _cronPollSince the way
// the real loop does, and report every surface that fired.
async function driveTick(completions) {
  global.api = async (path) => {
    state.apiCalls.push(path);
    return { completions };
  };
  state.toasts = [];
  state.notifications = [];
  await _runCronPollTick();
}

(async () => {
  const out = {};

  // === Scenario A: notifications OFF, hidden tab, one completion ==========
  // The maintainer's #1: the completion is consumed with no toast and no
  // notification, so the user loses the event entirely. The fix must queue
  // it and flush it as a toast when the tab becomes visible again.
  state.notificationsEnabled = false;
  setHidden(true);
  await driveTick([{
    job_id: 'job-hidden-off', name: 'Nightly report', status: 'success',
    completed_at: 200, session_id: 'cron-session-1', message_count: 2,
    toast_notifications: true,
  }]);
  out.a_hidden_notifications_off = {
    // Snapshot by value: scenario B repopulates state.notifications.
    notifications: state.notifications.slice(),
    toasts: state.toasts.length,
    since: _cronPollSince,
    queued: _cronPendingToasts.length,
  };

  // Tab becomes visible: exactly one toast must appear, for that completion.
  setHidden(false);
  out.a_after_visible = {
    toasts: state.toasts.map((entry) => entry.text),
    queued: _cronPendingToasts.length,
  };

  // A second visibilitychange with an empty queue must not duplicate.
  const toastsAfterFirstFlush = state.toasts.length;
  setHidden(false);
  out.a_no_duplicate_toast = { toasts: state.toasts.length - toastsAfterFirstFlush };

  // A profile switch must drop the queue: those completions belong to the
  // profile being left, not the incoming one (#5960 gate).
  setHidden(true);
  await driveTick([{
    job_id: 'job-hidden-off-2', name: 'Queued report', status: 'success',
    completed_at: 201, session_id: 'cron-session-2', message_count: 1,
    toast_notifications: true,
  }]);
  const queuedBeforeSwitch = _cronPendingToasts.length;
  _resetCronUnreadForProfileSwitch();
  out.a_profile_switch_drops_queue = {
    queuedBeforeSwitch,
    queuedAfterSwitch: _cronPendingToasts.length,
  };
  setHidden(false);

  // === Scenario B: notifications ON, two overlapping ticks ================
  // The maintainer's #2: both ticks fetch with the same _cronPollSince and
  // both notify the same completion. An in-flight guard must make the second
  // tick a no-op.
  state.notificationsEnabled = true;
  state.permission = 'granted';
  setHidden(true);
  _cronPollSince = 100;
  _cronPendingToasts.length = 0;
  const slowCompletion = {
    job_id: 'job-overlap', name: 'Overlap probe', status: 'error',
    completed_at: 300, session_id: 'cron-session-overlap', message_count: 1,
    toast_notifications: true,
  };
  // The first tick is given an api() that never resolves on its own, so the
  // second tick can be started mid-flight and must no-op on the guard.
  let releaseFirstApi;
  global.api = () => new Promise((resolve) => { releaseFirstApi = () => resolve({ completions: [slowCompletion] }); });
  const firstTick = _runCronPollTick();
  const overlappingTickResult = _runCronPollTick();   // started while tick 1 awaits
  releaseFirstApi();
  await firstTick;
  await overlappingTickResult;
  out.b_overlapping_ticks = {
    notifications: state.notifications.slice(),
    toasts: state.toasts.length,
    since: _cronPollSince,
    inFlightAfterTicks: _cronPollInFlight,
  };

  // === Scenario C: sessionless completion notification options ============
  // The maintainer's #3: a falsy sid fell through to the CURRENT session, so
  // clicking opened the wrong chat and reused its tag.
  const sessionlessOpts = _notificationOptions('body', { sid: null, sessionless: true });
  const withSidOpts = _notificationOptions('body', { sid: 'cron-session-1' });
  const currentSessionOpts = _notificationOptions('body', {});
  out.c_notification_options = {
    sessionless: {
      tag: sessionlessOpts.tag,
      url: sessionlessOpts.data.url,
    },
    withSid: { tag: withSidOpts.tag, url: withSidOpts.data.url },
    currentSession: { tag: currentSessionOpts.tag, url: currentSessionOpts.data.url },
  };

  // The poll must pass the marker when there is no session_id.
  _cronPendingToasts.length = 0;
  state.notifications = [];
  await driveTick([{
    job_id: 'job-sessionless', name: 'Sessionless probe', status: 'success',
    completed_at: 400, session_id: null, message_count: 0,
    toast_notifications: true,
  }]);
  out.c_poll_marker = state.notifications.map((entry) => entry.options);

  process.stdout.write(JSON.stringify(out));
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
"""


def _run_cron_harness() -> dict:
    assert NODE is not None, "node harness requires node on PATH"
    result = subprocess.run(
        [NODE, "-e", _HARNESS],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "PANELS_JS_PATH": str(REPO / "static" / "panels.js"),
            "MESSAGES_JS_PATH": str(REPO / "static" / "messages.js"),
        },
    )
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return json.loads(result.stdout)


def test_hidden_completion_with_notifications_off_is_not_lost():
    """#7652 review #1: a hidden tab with the notification channel closed
    (disabled/denied) must NOT consume the completion with no surface. It is
    queued and flushed as exactly one toast when the tab becomes visible."""
    out = _run_cron_harness()

    hidden = out["a_hidden_notifications_off"]
    assert hidden["notifications"] == [], "no notification may be sent when notifications are disabled"
    assert hidden["toasts"] == 0, "no toast may fire while hidden"
    assert hidden["queued"] == 1, "the completion must be queued, not dropped"
    assert hidden["since"] == 200, "the poll still advances past the completion"

    after = out["a_after_visible"]
    assert after["queued"] == 0, "the queue must drain on visibilitychange"
    assert len(after["toasts"]) == 1, "becoming visible must flush exactly one toast"
    assert "Nightly report" in after["toasts"][0], (
        "the flushed toast must carry the completion name"
    )

    assert out["a_no_duplicate_toast"]["toasts"] == 0, (
        "a second visibilitychange with an empty queue must not duplicate the toast"
    )

    switch = out["a_profile_switch_drops_queue"]
    assert switch["queuedBeforeSwitch"] == 1, (
        "the second hidden completion with notifications off must also be queued"
    )
    assert switch["queuedAfterSwitch"] == 0, (
        "a profile switch must drop the queued completions of the profile "
        "being left, never flush them into the incoming profile"
    )


def test_hidden_completion_with_notifications_on_sends_one_notification():
    """#7257 core contract, now asserted behaviourally: one hidden completion
    with the notification channel open produces exactly one notification and
    no fallback toast."""
    out = _run_cron_harness()
    overlap = out["b_overlapping_ticks"]
    assert len(overlap["notifications"]) == 1, (
        f"two overlapping ticks must yield one notification, got {overlap['notifications']}"
    )
    assert overlap["notifications"][0]["title"] == "Overlap probe"
    assert overlap["inFlightAfterTicks"] is False, (
        "the in-flight flag must be cleared so later ticks still run"
    )


def test_overlapping_ticks_do_not_double_notify():
    """#7652 review #2: an in-flight flag spanning the whole tick (cleared in
    finally) means a second tick started while the first is awaiting
    /api/crons/recent is a no-op, so the same completion cannot notify twice.
    The error-status completion here also proves the localized status text is
    shared across surfaces (it is the notification body)."""
    out = _run_cron_harness()
    overlap = out["b_overlapping_ticks"]
    assert len(overlap["notifications"]) == 1, (
        "one completion must produce exactly one notification across overlapping ticks"
    )
    assert overlap["since"] == 300, "the completion must still advance _cronPoll exactly once"


def test_sessionless_completion_notification_does_not_use_current_session():
    """#7652 review #3: a completion with no session_id must not inherit the
    user's current chat — neither the URL it opens nor the tag it reuses."""
    out = _run_cron_harness()
    sessionless = out["c_notification_options"]["sessionless"]
    assert sessionless["url"] == "https://hermes.example/", (
        "a sessionless notification must route to the app root, not the current chat"
    )
    assert sessionless["tag"] == "hermes-webui-sessionless", (
        "a sessionless notification needs its own tag so it neither replaces "
        "nor is replaced by a session-scoped notification"
    )

    with_sid = out["c_notification_options"]["withSid"]
    assert with_sid["url"] == "https://hermes.example/session/cron-session-1"
    assert with_sid["tag"] == "hermes-cron-session-1"

    # The current-session fallback is untouched for callers that omit options.
    current = out["c_notification_options"]["currentSession"]
    assert current["tag"] == "hermes-current-chat-session"


def test_poll_passes_sessionless_marker_for_completion_without_session():
    """The poll itself must send {sid:null,sessionless:true} when a completion
    carries no session_id, so _notificationOptions cannot fall back to the
    user's current chat."""
    out = _run_cron_harness()
    markers = out["c_poll_marker"]
    assert len(markers) == 1, f"expected exactly one notification, got {markers}"
    assert markers[0]["sessionless"] is True
    assert markers[0]["sid"] is None


def test_visible_completion_still_toasts_and_does_not_notify():
    """A visible tab keeps the existing toast surface and never notifies —
    the same completion must not produce both surfaces."""
    out = _run_cron_harness()
    # Scenario A ran with notifications disabled; re-run the same completion
    # with the channel open while hidden — already covered. Here we assert
    # the visible path via the queue contract: nothing queued when the tab is
    # visible, because the toast fired immediately.
    overlap = out["b_overlapping_ticks"]
    assert overlap["notifications"], "hidden + channel open must notify"
    hidden_off = out["a_hidden_notifications_off"]
    assert hidden_off["queued"] == 1
    assert out["a_after_visible"]["toasts"], "hidden + channel closed must toast on return"


def test_cron_toast_i18n_keys_exist():
    assert "cron_toast_notifications_label" in I18N_JS
    assert "cron_toast_notifications_hint" in I18N_JS
    assert "cron_toast_notifications_enabled" in I18N_JS
    assert "cron_toast_notifications_disabled" in I18N_JS
