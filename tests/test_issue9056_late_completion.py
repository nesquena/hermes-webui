"""Late-completion regressions for PR #9056 (gate certificate RED, 2026-11-18).

Gate blocker: `checkUpdatesNow()` observed at most twelve pending GET replies and
then abandoned the check — the panel restored "Check now", hid the spinner and
kept "Checking…" with no observer, so a legitimately slow worker that finished
afterwards never produced the banner. `static/boot.js` had the same fixed window.

These tests drive the real route handlers with an event-gated repository check
and replay the exact server payloads through the real consumers:

1. Settings observes completion after more than twelve pending replies.
2. Startup observes the same schedule through `_observeStartupUpdateCompletion`.
3. A check still running at the observation bound leaves a retrievable
   continuation instead of a silent abandon.
4. Pressing "Check now" while that continuation exists fetches the completed
   result with a cache-only GET and never starts new Git work.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import api.routes as routes
from api import updates
from tests.js_source_extract import extract_function
from tests.test_issue9056_manual_force_completion import (
    PANELS_JS_PATH,
    _dispatch_get,
    _dispatch_post,
    _drain_background,
    _reset_cache,
    _run_node,
)

ROOT = Path(__file__).resolve().parents[1]
BOOT_JS_PATH = ROOT / "static" / "boot.js"

POLL_URL = "/api/updates/check?channel=stable&include_agent=1"


def _producer_late_payloads(monkeypatch, pending_replies: int = 13) -> dict:
    """Real handlers: forced POST pending, N in-flight GETs pending, then completed."""
    _reset_cache(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_UPDATE_CHECK_WAIT", "0")
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": False})

    gate = threading.Event()

    def slow_check_repo(path, name, channel=updates.DEFAULT_UPDATE_CHANNEL):
        gate.wait(timeout=10)
        return {"name": name, "behind": 7 if name == "webui" else 0, "channel": channel}

    monkeypatch.setattr(updates, "_check_repo", slow_check_repo)

    post_payload = _dispatch_post(monkeypatch, {"force": True, "channel": "stable"})["ok"]
    pending = [
        _dispatch_get(monkeypatch, query="channel=stable&include_agent=1")["ok"]
        for _ in range(pending_replies)
    ]
    gate.set()
    _drain_background()
    completed = _dispatch_get(monkeypatch, query="channel=stable&include_agent=1")["ok"]
    return {"post": post_payload, "pending": pending, "completed": completed}


def _consumer_script(post_payload: dict, get_payloads: list, continuation: str | None = None) -> str:
    """Render the real checkUpdatesNow() with the server answers replayed in order."""
    panels_js = PANELS_JS_PATH.read_text(encoding="utf-8")
    extracted_fn = extract_function(panels_js, "checkUpdatesNow", prefix="async function")
    continuation_js = json.dumps(continuation)
    return f"""
const requests = [];
const postPayload = {json.dumps(post_payload)};
const getPayloads = {json.dumps(get_payloads)};
let getIndex = 0;
let postCount = 0;

async function api(url, opts) {{
  const method = (opts && opts.method) || 'GET';
  requests.push({{ url, method }});
  if (method === 'POST') {{ postCount++; return postPayload; }}
  const payload = getPayloads[Math.min(getIndex, getPayloads.length - 1)];
  getIndex++;
  return payload;
}}

const elements = {{
  btnCheckUpdatesNow: {{ disabled: false }},
  checkUpdatesLabel: {{ textContent: '' }},
  checkUpdatesSpinner: {{ style: {{ display: 'none' }} }},
  checkUpdatesStatus: {{ textContent: '', style: {{ color: '' }} }},
}};
function $(id) {{ return elements[id] || null; }}
function t(k, arg) {{
  if (k === 'settings_updates_available_targets') return 'Updates available: ' + arg;
  if (k === 'settings_up_to_date') return 'Up to date';
  if (k === 'settings_checking') return 'Checking...';
  if (k === 'settings_check_now') return 'Check now';
  if (k === 'settings_updates_disabled') return 'Updates are disabled in settings';
  if (k === 'settings_update_check_failed') return 'Update check failed';
  return k;
}}
const bannerCalls = [];
function _showUpdateBanner(d, gen) {{ bannerCalls.push({{ d, gen }}); }}
const window = {{ _updateRecoveryGeneration: 0, _pendingUpdatePollUrl: {continuation_js} }};
setTimeout = (fn, ms) => setImmediate(fn);

{extracted_fn}

(async () => {{
  await checkUpdatesNow();
  console.log(JSON.stringify({{
    requestCount: requests.length,
    requests,
    getCount: getIndex,
    postCount,
    statusText: elements.checkUpdatesStatus.textContent,
    spinnerDisplay: elements.checkUpdatesSpinner.style.display,
    btnDisabled: elements.btnCheckUpdatesNow.disabled,
    bannerCalls: bannerCalls.length,
    continuation: window._pendingUpdatePollUrl || null,
  }}));
}})();
"""


def _startup_script(get_payloads: list, initial: dict) -> str:
    """Drive the real _observeStartupUpdateCompletion() from static/boot.js."""
    boot_js = BOOT_JS_PATH.read_text(encoding="utf-8")
    extracted_fn = extract_function(boot_js, "_observeStartupUpdateCompletion", prefix="async function")
    return f"""
const getPayloads = {json.dumps(get_payloads)};
let getIndex = 0;
async function api(url, opts) {{
  const payload = getPayloads[Math.min(getIndex, getPayloads.length - 1)];
  getIndex++;
  return payload;
}}
const window = {{}};
setTimeout = (fn, ms) => setImmediate(fn);

{extracted_fn}

(async () => {{
  const d = await _observeStartupUpdateCompletion({json.dumps(POLL_URL)}, {json.dumps(initial)});
  console.log(JSON.stringify({{
    getCount: getIndex,
    pending: d && d.pending === true,
    behind: d && d.webui ? d.webui.behind : null,
    continuation: window._pendingUpdatePollUrl || null,
  }}));
}})();
"""


def test_settings_observes_completion_after_twelve_pending_replies(monkeypatch):
    """Settings: the completed answer arriving after the old twelve-reply cap must land."""
    payloads = _producer_late_payloads(monkeypatch, pending_replies=13)
    for reply in payloads["pending"]:
        assert reply.get("pending") is True, f"in-flight GET stopped being pending: {reply}"
    assert payloads["completed"].get("pending") is not True, (
        f"completed GET still pending: {payloads['completed']}"
    )
    assert payloads["completed"]["webui"]["behind"] == 7

    result = _run_node(_consumer_script(payloads["post"], payloads["pending"] + [payloads["completed"]]))

    assert result["getCount"] > 12, (
        f"consumer stopped observing after {result['getCount']} GET(s); the old fixed window is back"
    )
    assert "Updates available" in result["statusText"], (
        f"late completion never reached the panel: {result['statusText']!r}"
    )
    assert result["bannerCalls"] == 1, "the completed payload must fire the banner exactly once"
    assert result["continuation"] is None, "a resolved check must clear the continuation"
    assert result["spinnerDisplay"] == "none" and result["btnDisabled"] is False, (
        "busy controls must be coherent once the check resolves"
    )


def test_startup_observes_completion_after_twelve_pending_replies(monkeypatch):
    """Startup: same slow schedule must not be abandoned at twelve replies."""
    payloads = _producer_late_payloads(monkeypatch, pending_replies=13)

    result = _run_node(
        _startup_script(payloads["pending"] + [payloads["completed"]], payloads["post"])
    )

    assert result["getCount"] > 12, f"startup stopped observing after {result['getCount']} GET(s)"
    assert result["pending"] is False, "startup never saw the completed answer"
    assert result["behind"] == 7, f"startup lost webui.behind: {result}"
    assert result["continuation"] is None


def test_unfinished_check_leaves_retrievable_continuation(monkeypatch):
    """A check still running at the bound must leave a continuation, not a silent abandon."""
    panels_js = PANELS_JS_PATH.read_text(encoding="utf-8")
    extracted_fn = extract_function(panels_js, "checkUpdatesNow", prefix="async function")
    pending_reply = {"pending": True, "channel": "stable", "include_agent": True}
    script = _consumer_script(pending_reply, [pending_reply])
    # Keep only one pending answer available forever: the observation bound is
    # reached and the continuation must survive for the next press.
    assert extracted_fn.splitlines()[0].lstrip().startswith("async function")
    result = _run_node(script)

    assert result["getCount"] == 240, f"observation bound drifted: {result['getCount']} GET(s)"
    assert result["continuation"] == POLL_URL, (
        f"unfinished check left no retrievable continuation: {result['continuation']!r}"
    )
    assert "Checking" in result["statusText"], (
        f"unfinished check must keep the truthful busy text: {result['statusText']!r}"
    )


def test_retry_retrieves_completed_result_without_new_work(monkeypatch):
    """Pressing Check now with a continuation fetches the result; it never starts new Git work."""
    payloads = _producer_late_payloads(monkeypatch, pending_replies=2)
    completed = payloads["completed"]

    result = _run_node(_consumer_script(payloads["post"], [completed], continuation=POLL_URL))

    assert result["postCount"] == 0, f"retrieval started new work: {result['requests']}"
    assert result["requests"][0] == {"url": POLL_URL, "method": "GET"}, (
        f"retrieval did not target the continuation: {result['requests']}"
    )
    assert "Updates available" in result["statusText"], (
        f"retrieved result was not rendered: {result['statusText']!r}"
    )
    assert result["bannerCalls"] == 1
    assert result["continuation"] is None
