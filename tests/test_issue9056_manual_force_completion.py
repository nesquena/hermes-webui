"""Regression tests for PR #9056: manual-force exception across completion retrieval.

Maintainer contract:
- Allow a cache-only, key-consistent poll for an explicitly initiated manual check
  even when automatic checks are off, without enabling unsolicited automatic checks
  or starting Git work from GET.
- Producer/route-to-consumer regression for disabled-auto + pending manual completion,
  plus enabled-auto and disabled non-forced controls.
"""

from __future__ import annotations

import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse

import api.routes as routes
from api import updates
from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
PANELS_JS_PATH = ROOT / "static" / "panels.js"
NODE = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"

CACHE = {
    "webui": None,
    "agent": None,
    "checked_at": 0,
    "include_agent": True,
    "channel": "stable",
}


class _FakeUpdatesHandler:
    """Minimal HTTP handler driving routes.handle_post and routes.handle_get."""

    def __init__(self, body_bytes: bytes = b"", command: str = "GET", path: str = "/api/updates/check"):
        self.rfile = io.BytesIO(body_bytes)
        self.wfile = io.BytesIO()
        self.headers = {
            "Content-Length": str(len(body_bytes)),
            "Content-Type": "application/json",
        }
        self.command = command
        self.path = path
        self.client_address = ("127.0.0.1", 12345)
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


def _reset_cache(monkeypatch, **overrides):
    """Start every test from an empty cache and clean queue/progress state, isolating manual registry."""
    cache = dict(CACHE)
    cache.update(overrides)
    monkeypatch.setattr(updates, "_update_cache", dict(cache))
    monkeypatch.setattr(updates, "_update_cache_by_key", {})
    if hasattr(updates, "_manual_update_checks"):
        monkeypatch.setattr(updates, "_manual_update_checks", {})
    if hasattr(updates, "_update_check_failures"):
        monkeypatch.setattr(updates, "_update_check_failures", {})
    monkeypatch.setattr(updates, "_check_in_progress", False)
    if hasattr(updates, "_running_key"):
        monkeypatch.setattr(updates, "_running_key", None)
    if hasattr(updates, "_refresh_queue"):
        monkeypatch.setattr(updates, "_refresh_queue", [])
    for attribute in ("_refresh_event", "_refresh_thread"):
        if hasattr(updates, attribute):
            monkeypatch.setattr(updates, attribute, None)
    return cache


def _drain_background():
    """Join background refresh thread until the queue is completely drained."""
    deadline = time.time() + 10
    while time.time() < deadline:
        thread = getattr(updates, "_refresh_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.1, deadline - time.time()))
        elif not getattr(updates, "_check_in_progress", False):
            break
        else:
            time.sleep(0.01)


def _dispatch_post(monkeypatch, body: dict, path: str = "/api/updates/check") -> dict:
    """Drive routes.handle_post and capture j() response."""
    cap = {}

    def _j(_handler, obj, *_a, **kw):
        cap["ok"] = obj
        cap["status"] = kw.get("status", 200)
        return True

    def _bad(_handler, msg, code=400, **_kw):
        cap["bad"] = (msg, code)
        return True

    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *a, **k: True)

    body_bytes = json.dumps(body).encode("utf-8")
    handler = _FakeUpdatesHandler(body_bytes, command="POST", path=path)
    routes.handle_post(handler, urlparse(path))
    return cap


def _dispatch_get(monkeypatch, path: str = "/api/updates/check", query: str = "") -> dict:
    """Drive routes.handle_get and capture j() response."""
    cap = {}

    def _j(_handler, obj, *_a, **kw):
        cap["ok"] = obj
        cap["status"] = kw.get("status", 200)
        return True

    def _bad(_handler, msg, code=400, **_kw):
        cap["bad"] = (msg, code)
        return True

    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *a, **k: True)

    full_path = f"{path}?{query}" if query else path
    handler = _FakeUpdatesHandler(b"", command="GET", path=full_path)
    routes.handle_get(handler, urlparse(full_path))
    return cap


def _run_node(script: str) -> dict:
    """Execute a JS script in node and parse its JSON stdout."""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(script)
        temp_path = f.name
    try:
        proc = subprocess.run(
            [NODE, temp_path],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        return json.loads(proc.stdout)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


# ── Test A (the regression) ───────────────────────────────────────────────────


def test_manual_force_completion_regression_disabled_auto(monkeypatch):
    """A: auto off + forced POST returns pending: True; same-key GET while in-flight

    answers pending: True and NEVER {"disabled": true}; after worker drains,
    same-key GET returns completed payload with webui.behind == 7 and no pending.
    """
    _reset_cache(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_UPDATE_CHECK_WAIT", "0")
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": False})

    worker_started = threading.Event()
    gate = threading.Event()

    def slow_check_repo(path, name, channel=updates.DEFAULT_UPDATE_CHANNEL):
        worker_started.set()
        gate.wait(timeout=5)
        return {"name": name, "behind": 7 if name == "webui" else 0, "channel": channel}

    monkeypatch.setattr(updates, "_check_repo", slow_check_repo)

    # 1. Initiating forced POST
    post_res = _dispatch_post(monkeypatch, {"force": True, "channel": "stable"})
    assert post_res.get("ok", {}).get("pending") is True, f"POST did not return pending: {post_res}"
    assert post_res["ok"].get("channel") == "stable"
    assert post_res["ok"].get("include_agent") is True
    assert worker_started.wait(timeout=5), "worker thread never started"

    # 2. Same-key GET while in-flight: must answer pending: True, NEVER disabled
    in_flight_get = _dispatch_get(monkeypatch, query="channel=stable&include_agent=1")
    assert in_flight_get.get("ok", {}).get("pending") is True, (
        f"in-flight GET did not answer pending: {in_flight_get}"
    )
    assert in_flight_get.get("ok") != {"disabled": True}, "in-flight GET returned disabled"

    # 3. Allow worker to finish and drain
    gate.set()
    _drain_background()

    # 4. Same-key GET after completion: must answer completed payload with webui.behind == 7
    after_get = _dispatch_get(monkeypatch, query="channel=stable&include_agent=1")
    assert after_get.get("ok", {}).get("pending") is not True, (
        f"completed GET still has pending: {after_get}"
    )
    assert after_get.get("ok") != {"disabled": True}, "completed GET returned disabled"
    assert after_get["ok"].get("webui", {}).get("behind") == 7, (
        f"webui.behind != 7 in completed payload: {after_get}"
    )


# ── Test B (control, enabled auto) ───────────────────────────────────────────


def test_manual_force_control_enabled_auto(monkeypatch):
    """B: forced POST with auto on returns completed payload without poll round-trip."""
    _reset_cache(monkeypatch)
    monkeypatch.delenv("HERMES_WEBUI_UPDATE_CHECK_WAIT", raising=False)
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": True})
    monkeypatch.setattr(
        updates,
        "_check_repo",
        lambda path, name, channel=updates.DEFAULT_UPDATE_CHANNEL: {
            "name": name,
            "behind": 7,
            "channel": channel,
        },
    )

    post_res = _dispatch_post(monkeypatch, {"force": True, "channel": "stable"})
    _drain_background()
    assert post_res.get("ok", {}).get("pending") is not True, f"POST returned pending: {post_res}"
    assert post_res.get("ok", {}).get("webui", {}).get("behind") == 7


# ── Test C (control, disabled non-forced) ─────────────────────────────────────


def test_manual_force_control_disabled_non_forced(monkeypatch):
    """C: POST without force answers {"disabled": True} and _check_repo is never called."""
    _reset_cache(monkeypatch)
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": False})
    scans = []
    monkeypatch.setattr(updates, "_check_repo", lambda *a, **k: scans.append(a))

    res1 = _dispatch_post(monkeypatch, {"channel": "stable"})
    assert res1.get("ok") == {"disabled": True}, res1
    assert len(scans) == 0, f"_check_repo was called {len(scans)} times"

    res2 = _dispatch_post(monkeypatch, {"force": False, "channel": "stable"})
    assert res2.get("ok") == {"disabled": True}, res2
    assert len(scans) == 0, f"_check_repo was called {len(scans)} times"


# ── Test D (control, unsolicited poll) ────────────────────────────────────────


def test_manual_force_control_unsolicited_poll(monkeypatch):
    """D: GET with auto off and no prior manual POST answers {"disabled": True}."""
    _reset_cache(monkeypatch)
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": False})
    scans = []
    monkeypatch.setattr(updates, "_check_repo", lambda *a, **k: scans.append(a))

    res1 = _dispatch_get(monkeypatch, query="channel=stable&include_agent=1")
    assert res1.get("ok") == {"disabled": True}, res1

    res2 = _dispatch_get(monkeypatch)
    assert res2.get("ok") == {"disabled": True}, res2
    assert len(scans) == 0, f"_check_repo was called {len(scans)} times"


# ── Test E (key consistency) ──────────────────────────────────────────────────


def test_manual_force_key_consistency(monkeypatch):
    """E: after ('stable', True) manual POST, GET with include_agent=0, GET with

    channel=experimental, and GET with no query string all answer {"disabled": True}.
    """
    _reset_cache(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_UPDATE_CHECK_WAIT", "0")
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": False})

    gate = threading.Event()
    monkeypatch.setattr(
        updates,
        "_check_repo",
        lambda path, name, channel=updates.DEFAULT_UPDATE_CHANNEL: (
            gate.wait(timeout=5),
            {"name": name, "behind": 7, "channel": channel},
        )[1],
    )

    # Register manual key ('stable', True)
    post_res = _dispatch_post(monkeypatch, {"force": True, "channel": "stable"})
    assert post_res.get("ok", {}).get("pending") is True

    # 1. GET with include_agent=0
    res_agent0 = _dispatch_get(monkeypatch, query="channel=stable&include_agent=0")
    assert res_agent0.get("ok") == {"disabled": True}, f"expected disabled, got {res_agent0}"

    # 2. GET with channel=experimental
    res_exp = _dispatch_get(monkeypatch, query="channel=experimental&include_agent=1")
    assert res_exp.get("ok") == {"disabled": True}, f"expected disabled, got {res_exp}"

    # 3. GET with no query string
    res_no_qs = _dispatch_get(monkeypatch, query="")
    assert res_no_qs.get("ok") == {"disabled": True}, f"expected disabled, got {res_no_qs}"

    # Allow drain
    gate.set()
    _drain_background()

    # Re-verify after worker drain
    assert _dispatch_get(monkeypatch, query="channel=stable&include_agent=0").get("ok") == {"disabled": True}
    assert _dispatch_get(monkeypatch, query="channel=experimental&include_agent=1").get("ok") == {"disabled": True}
    assert _dispatch_get(monkeypatch, query="").get("ok") == {"disabled": True}


# ── Test F (cache-only) ───────────────────────────────────────────────────────


def test_manual_force_get_is_cache_only(monkeypatch):
    """F: GET calls never increase a check_for_updates / _check_repo call counter,

    before or after completion.
    """
    _reset_cache(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_UPDATE_CHECK_WAIT", "0")
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": False})

    repo_calls = []
    gate = threading.Event()

    def fake_check(path, name, channel=updates.DEFAULT_UPDATE_CHANNEL):
        repo_calls.append((name, channel))
        gate.wait(timeout=5)
        return {"name": name, "behind": 7, "channel": channel}

    monkeypatch.setattr(updates, "_check_repo", fake_check)

    # POST initiates worker
    _dispatch_post(monkeypatch, {"force": True, "channel": "stable"})
    time.sleep(0.05)
    calls_midflight = len(repo_calls)

    # In-flight GET calls
    for _ in range(3):
        _dispatch_get(monkeypatch, query="channel=stable&include_agent=1")
    assert len(repo_calls) == calls_midflight, "in-flight GET triggered _check_repo scans"

    # Finish worker
    gate.set()
    _drain_background()
    calls_after_drain = len(repo_calls)

    # Post-completion GET calls
    for _ in range(3):
        _dispatch_get(monkeypatch, query="channel=stable&include_agent=1")
        _dispatch_get(monkeypatch, query="channel=stable&include_agent=0")
        _dispatch_get(monkeypatch, query="")
    assert len(repo_calls) == calls_after_drain, "post-completion GET triggered _check_repo scans"


# ── Test G (expiry) ───────────────────────────────────────────────────────────


def test_manual_force_registry_expiry(monkeypatch):
    """G: a registry entry older than CACHE_TTL stops authorizing the poll."""
    _reset_cache(monkeypatch)
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": False})

    # Fake registration time in the past beyond CACHE_TTL
    key = ("stable", True)
    updates._manual_update_checks[key] = time.time() - updates.CACHE_TTL - 10
    updates._update_cache_by_key[key] = {
        "webui": {"behind": 7},
        "agent": None,
        "checked_at": time.time(),
        "channel": "stable",
        "include_agent": True,
    }

    res = _dispatch_get(monkeypatch, query="channel=stable&include_agent=1")
    assert res.get("ok") == {"disabled": True}, f"expired manual check was authorized: {res}"
    assert key not in updates._manual_update_checks, "expired entry was not dropped from registry"


# ── Test H (route-to-consumer, coupled payloads) ──────────────────────────────


def _producer_payloads(monkeypatch) -> dict:
    """Drive the real route handlers and return the payloads a forced manual check produces.

    The consumer test below must replay these exact server answers: payloads built by
    hand in the test would pass even with the server short-circuit restored, which is
    precisely the regression this file has to catch.
    """
    _reset_cache(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_UPDATE_CHECK_WAIT", "0")
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": False})

    gate = threading.Event()

    def slow_check_repo(path, name, channel=updates.DEFAULT_UPDATE_CHANNEL):
        gate.wait(timeout=5)
        return {"name": name, "behind": 7 if name == "webui" else 0, "channel": channel}

    monkeypatch.setattr(updates, "_check_repo", slow_check_repo)

    post_payload = _dispatch_post(monkeypatch, {"force": True, "channel": "stable"})["ok"]
    in_flight_payload = _dispatch_get(monkeypatch, query="channel=stable&include_agent=1")["ok"]
    gate.set()
    _drain_background()
    completed_payload = _dispatch_get(monkeypatch, query="channel=stable&include_agent=1")["ok"]
    return {
        "post": post_payload,
        "in_flight": in_flight_payload,
        "completed": completed_payload,
    }


def _build_consumer_harness(post_payload: dict, get_payloads: list[dict]) -> str:
    """Render checkUpdatesNow() from static/panels.js with the server payloads replayed in order."""
    panels_js = PANELS_JS_PATH.read_text(encoding="utf-8")
    extracted_fn = extract_function(panels_js, "checkUpdatesNow", prefix="async function")
    return f"""
const requests = [];
const postPayload = {json.dumps(post_payload)};
const getPayloads = {json.dumps(get_payloads)};
let getIndex = 0;

async function api(url, opts) {{
  const method = (opts && opts.method) || 'GET';
  requests.push({{ url, method }});
  if (method === 'POST') return postPayload;
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
  return k;
}}
const bannerCalls = [];
function _showUpdateBanner(d, gen) {{ bannerCalls.push({{ d, gen }}); }}
const window = {{ _updateRecoveryGeneration: 0 }};
setTimeout = (fn, ms) => setImmediate(fn);

{extracted_fn}

(async () => {{
  await checkUpdatesNow();
  console.log(JSON.stringify({{
    requestCount: requests.length,
    requests,
    getCount: getIndex,
    statusText: elements.checkUpdatesStatus.textContent,
    statusColor: elements.checkUpdatesStatus.style.color,
    spinnerDisplay: elements.checkUpdatesSpinner.style.display,
    btnDisabled: elements.btnCheckUpdatesNow.disabled,
    bannerCalls: bannerCalls.length,
  }}));
}})();
"""


def test_manual_force_route_to_consumer(monkeypatch):
    """H: the payloads the real handlers return must drive the real consumer to "available".

    disabled-auto: the forced POST answers pending, the in-flight GET answers pending (never
    disabled) and the completed GET carries webui.behind == 7. Replaying exactly those three
    answers through the extracted checkUpdatesNow() must end on the updates-available text and
    fire the banner once. With the pre-fix handlers the in-flight GET answers {{"disabled": true}},
    the consumer stops polling and renders the disabled text, so this test fails.
    """
    payloads = _producer_payloads(monkeypatch)

    result = _run_node(_build_consumer_harness(payloads["post"], [payloads["in_flight"], payloads["completed"]]))
    assert result["requestCount"] >= 2, f"expected a poll round-trip, got {result['requestCount']} request(s)"
    assert "Updates available" in result["statusText"], (
        f"consumer rendered {result['statusText']!r} instead of the updates-available text"
    )
    assert "disabled" not in result["statusText"].lower(), (
        f"consumer rendered the disabled text: {result['statusText']!r}"
    )
    assert result["bannerCalls"] == 1, "banner must fire exactly once from the completed payload"
    assert result["requests"][1] == {
        "url": "/api/updates/check?channel=stable&include_agent=1",
        "method": "GET",
    }, f"poll did not target the key-consistent GET: {result['requests']}"

    # The producer-side contract behind the replayed answers, kept as diagnostics.
    assert payloads["post"].get("pending") is True, f"POST did not answer pending: {payloads['post']}"
    assert payloads["in_flight"].get("pending") is True, (
        f"in-flight GET did not answer pending: {payloads['in_flight']}"
    )
    assert payloads["in_flight"] != {"disabled": True}, "in-flight GET answered disabled"
    assert payloads["completed"].get("pending") is not True, (
        f"completed GET still pending: {payloads['completed']}"
    )
    assert payloads["completed"].get("webui", {}).get("behind") == 7, (
        f"completed payload lost webui.behind: {payloads['completed']}"
    )


def test_manual_force_route_to_consumer_control_enabled_auto(monkeypatch):
    """H-control: with auto on the real POST already answers the completed payload.

    The consumer must render updates-available from that single answer, without polling.
    """
    _reset_cache(monkeypatch)
    monkeypatch.delenv("HERMES_WEBUI_UPDATE_CHECK_WAIT", raising=False)
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": True})
    monkeypatch.setattr(
        updates,
        "_check_repo",
        lambda path, name, channel=updates.DEFAULT_UPDATE_CHANNEL: {
            "name": name,
            "behind": 7 if name == "webui" else 0,
            "channel": channel,
        },
    )

    post_payload = _dispatch_post(monkeypatch, {"force": True, "channel": "stable"})["ok"]
    _drain_background()
    assert post_payload.get("pending") is not True, f"enabled-auto POST answered pending: {post_payload}"
    assert post_payload.get("webui", {}).get("behind") == 7

    result = _run_node(_build_consumer_harness(post_payload, []))
    assert result["requestCount"] == 1, f"enabled-auto POST must not poll: {result['requests']}"
    assert "Updates available" in result["statusText"], (
        f"consumer rendered {result['statusText']!r} instead of the updates-available text"
    )
    assert result["bannerCalls"] == 1, "banner must fire from the completed POST payload"


def test_manual_force_route_to_consumer_control_disabled_non_forced(monkeypatch):
    """H-control: a non-forced POST on disabled auto answers {"disabled": true} for real.

    The consumer must render the disabled text and never fire the banner.
    """
    _reset_cache(monkeypatch)
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": False})
    scans = []
    monkeypatch.setattr(updates, "_check_repo", lambda *a, **k: scans.append(a))

    post_payload = _dispatch_post(monkeypatch, {"channel": "stable"})["ok"]
    assert post_payload == {"disabled": True}, post_payload
    assert len(scans) == 0, f"_check_repo was called {len(scans)} times"

    result = _run_node(_build_consumer_harness(post_payload, []))
    assert result["requestCount"] == 1, f"disabled POST must not poll: {result['requests']}"
    assert "Updates are disabled" in result["statusText"], (
        f"consumer rendered {result['statusText']!r} instead of the disabled text"
    )
    assert result["bannerCalls"] == 0, "banner must NOT fire when the server answers disabled"


# ── Terminal failure of a background refresh (gate on #9056) ──────────────────
#
# A refresh that raises publishes nothing, so the completing consumer used to
# read the cache: cold cache answered empty targets and warm cache answered the
# previous result (behind: 7). Both looked like a successful check. The producer
# now records a terminal failure per key; POST and the completion-recovery GET
# must both report it, with pending off because the worker is gone.


def _raise_incomplete_read(path, name, channel=updates.DEFAULT_UPDATE_CHANNEL):
    """Mirror the release-tag HTTP producer: urllib can raise on a short read."""
    raise http.client.IncompleteRead(b'{"name": "v0.52.7', 512)


def _healthy_check(behind: int):
    def _check(path, name, channel=updates.DEFAULT_UPDATE_CHANNEL):
        return {"name": name, "behind": behind if name == "webui" else 0, "channel": channel}

    return _check


def test_failed_refresh_reports_terminal_failure_cold_cache(monkeypatch):
    """Cold cache: the failed check must answer failed, never pending and never empty-success."""
    _reset_cache(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_UPDATE_CHECK_WAIT", "0")
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": True})
    monkeypatch.setattr(updates, "_check_repo", _raise_incomplete_read)

    post_payload = _dispatch_post(monkeypatch, {"force": True, "channel": "stable"})["ok"]
    assert post_payload.get("pending") is True, (
        f"a zero budget must still answer pending from the POST: {post_payload}"
    )
    _drain_background()

    completed = _dispatch_get(monkeypatch, query="channel=stable&include_agent=1")["ok"]
    assert completed.get("pending") is not True, (
        f"GET kept answering pending after the worker terminated: {completed}"
    )
    assert completed.get("failed") is True, (
        f"GET lost the terminal failure of the refresh: {completed}"
    )
    assert "IncompleteRead" in str(completed.get("error") or ""), (
        f"GET error does not name the producer exception: {completed}"
    )
    assert completed.get("webui", {}).get("behind") is None, (
        f"empty targets were published as an answer: {completed}"
    )
    assert "IncompleteRead" in str(completed.get("webui", {}).get("error") or "")
    assert "IncompleteRead" in str(completed.get("agent", {}).get("error") or "")

    # The POST observes the same terminal failure once the worker is gone.
    plain_post = _dispatch_post(monkeypatch, {"channel": "stable"})["ok"]
    assert plain_post.get("failed") is True, f"non-forced POST did not report the failure: {plain_post}"
    assert plain_post.get("pending") is not True

    # Control: a successful refresh supersedes the failure.
    monkeypatch.delenv("HERMES_WEBUI_UPDATE_CHECK_WAIT", raising=False)
    monkeypatch.setattr(updates, "_check_repo", _healthy_check(7))
    healthy_post = _dispatch_post(monkeypatch, {"force": True, "channel": "stable"})["ok"]
    _drain_background()
    assert healthy_post.get("pending") is not True, f"healthy POST answered pending: {healthy_post}"
    assert healthy_post.get("webui", {}).get("behind") == 7, (
        f"healthy refresh lost its answer: {healthy_post}"
    )
    assert healthy_post.get("failed") is not True
    assert "error" not in (healthy_post.get("webui") or {})

    healthy_get = _dispatch_get(monkeypatch, query="channel=stable&include_agent=1")["ok"]
    assert healthy_get.get("webui", {}).get("behind") == 7, (
        f"failure record outlived the successful refresh: {healthy_get}"
    )
    assert healthy_get.get("failed") is not True


def test_failed_refresh_reports_terminal_failure_warm_cache(monkeypatch):
    """Warm cache: the previous behind:7 must never stand in for the failed check."""
    _reset_cache(monkeypatch)
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": True})
    updates._publish_update_cache(
        {"name": "webui", "behind": 7},
        {"name": "agent", "behind": 0},
        True,
        "stable",
    )
    monkeypatch.setenv("HERMES_WEBUI_UPDATE_CHECK_WAIT", "0")
    monkeypatch.setattr(updates, "_check_repo", _raise_incomplete_read)

    post_payload = _dispatch_post(monkeypatch, {"force": True, "channel": "stable"})["ok"]
    assert post_payload.get("pending") is True, f"zero budget must answer pending: {post_payload}"
    _drain_background()

    completed = _dispatch_get(monkeypatch, query="channel=stable&include_agent=1")["ok"]
    assert completed.get("pending") is not True, (
        f"GET kept answering pending after the worker terminated: {completed}"
    )
    assert completed.get("failed") is True, (
        f"GET served the stale cache as a successful check: {completed}"
    )
    assert completed.get("webui", {}).get("behind") != 7, (
        f"stale behind:7 survived the failed refresh: {completed}"
    )
    assert completed.get("webui", {}).get("behind") is None
    assert "IncompleteRead" in str(completed.get("error") or "")

    # Control: the same warm cache is answered again once a refresh succeeds.
    monkeypatch.delenv("HERMES_WEBUI_UPDATE_CHECK_WAIT", raising=False)
    monkeypatch.setattr(updates, "_check_repo", _healthy_check(9))
    healthy_post = _dispatch_post(monkeypatch, {"force": True, "channel": "stable"})["ok"]
    _drain_background()
    assert healthy_post.get("pending") is not True
    assert healthy_post.get("webui", {}).get("behind") == 9, (
        f"healthy refresh lost its answer: {healthy_post}"
    )
    assert healthy_post.get("failed") is not True

    healthy_get = _dispatch_get(monkeypatch, query="channel=stable&include_agent=1")["ok"]
    assert healthy_get.get("webui", {}).get("behind") == 9
    assert healthy_get.get("failed") is not True


