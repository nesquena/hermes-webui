"""Continuation-key and expiry contract for PR #9056 (scoped supplement, 2026-11-18).

Maintainer review (2026-11-18T22:05:07Z) on ``c6a063fcb``: two narrower
continuation problems remain inside ``checkUpdatesNow()``:

1. The saved continuation overrides a new requested key. A stable check that
   leaves ``window._pendingUpdatePollUrl`` makes a later
   ``checkUpdatesNow('experimental')`` (the real
   ``_saveUpdateChannelFromSelector()`` caller) fetch the stable URL only, skip
   the experimental POST and render the stable answer — whether the saved
   continuation is still pending or already complete. A saved change to the
   Agent-update preference is ignored the same way. A continuation may only be
   reused when its channel and include-Agent key match the effective request.

2. An expired continuation swallows a fresh manual check. With automatic checks
   disabled the real manual authorization expires after ``CACHE_TTL`` and the
   continuation GET answers ``{"disabled": true}``. That truthy payload skips
   the forced POST, clears the continuation and renders "Updates are disabled"
   instead of performing the click's manual check. An unavailable continuation
   must fall through to the existing forced-POST path on the same click, without
   enabling automatic checks and without starting Git work from a GET.

Every case drives the extracted production ``checkUpdatesNow()`` from
``static/panels.js`` (no reimplementation) and replays the real consumer
payload shapes.
"""

from __future__ import annotations

import json

from tests.js_source_extract import extract_function
from tests.test_issue9056_manual_force_completion import (
    PANELS_JS_PATH,
    _run_node,
)

STABLE_URL = "/api/updates/check?channel=stable&include_agent=1"
STABLE_URL_NO_AGENT = "/api/updates/check?channel=stable&include_agent=0"

STABLE_COMPLETE = {
    "webui": {"behind": 7, "release_based": True},
    "agent": {"behind": 0, "ignored": False},
}
EXPERIMENTAL_COMPLETE = {
    "webui": {"behind": 2, "release_based": True},
    "agent": {"behind": 0, "ignored": False},
}
PENDING_STABLE = {"pending": True, "channel": "stable", "include_agent": True}
PENDING_EXPERIMENTAL = {"pending": True, "channel": "experimental", "include_agent": True}
DISABLED = {"disabled": True}


def _consumer_script(
    *,
    post_payload: dict,
    get_payloads: list,
    continuation: str | None = None,
    override: str | None = None,
    selector: str = "stable",
    ignore_agent: bool = False,
    has_selector: bool = True,
) -> str:
    """Run the real checkUpdatesNow() with the click's context and replay answers."""
    panels_js = PANELS_JS_PATH.read_text(encoding="utf-8")
    extracted_fn = extract_function(panels_js, "checkUpdatesNow", prefix="async function")
    selector_element = (
        "{ value: %s }" % json.dumps(selector) if has_selector else "null"
    )
    override_js = "undefined" if override is None else json.dumps(override)
    return f"""
const requests = [];
const postBodies = [];
const postPayload = {json.dumps(post_payload)};
const getPayloads = {json.dumps(get_payloads)};
let getIndex = 0;
let postCount = 0;
async function api(url, opts) {{
  const method = (opts && opts.method) || 'GET';
  requests.push({{ url, method }});
  if (method === 'POST') {{
    postCount++;
    postBodies.push(opts && opts.body ? JSON.parse(opts.body) : null);
    return postPayload;
  }}
  const payload = getPayloads[Math.min(getIndex, getPayloads.length - 1)];
  getIndex++;
  return payload;
}}

const elements = {{
  btnCheckUpdatesNow: {{ disabled: false }},
  checkUpdatesLabel: {{ textContent: '' }},
  checkUpdatesSpinner: {{ style: {{ display: 'none' }} }},
  checkUpdatesStatus: {{ textContent: '', style: {{ color: '' }} }},
  settingsUpdateChannel: {selector_element},
  settingsIgnoreAgentUpdates: {{ checked: {json.dumps(ignore_agent)} }},
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
let _confirmedUpdateChannel = {json.dumps(selector)};
const window = {{ _updateRecoveryGeneration: 0, _pendingUpdatePollUrl: {json.dumps(continuation)} }};
setTimeout = (fn, ms) => setImmediate(fn);

{extracted_fn}

(async () => {{
  await checkUpdatesNow({override_js});
  console.log(JSON.stringify({{
    requestCount: requests.length,
    requests,
    postCount,
    postBodies,
    getCount: getIndex,
    statusText: elements.checkUpdatesStatus.textContent,
    spinnerDisplay: elements.checkUpdatesSpinner.style.display,
    btnDisabled: elements.btnCheckUpdatesNow.disabled,
    bannerCalls: bannerCalls.length,
    continuation: window._pendingUpdatePollUrl || null,
  }}));
}})();
"""


# ── Defect 1a: a completed stable continuation answers an experimental click ──


def test_completed_stable_continuation_does_not_answer_experimental_click():
    result = _run_node(
        _consumer_script(
            post_payload=EXPERIMENTAL_COMPLETE,
            get_payloads=[STABLE_COMPLETE],
            continuation=STABLE_URL,
            override="experimental",
        )
    )

    assert result["postCount"] == 1, (
        f"experimental click reused the stable continuation instead of starting "
        f"the requested check: {result['requests']}"
    )
    assert result["postBodies"][0].get("channel") == "experimental", (
        f"forced POST did not carry the requested channel: {result['postBodies']}"
    )
    assert "WebUI: 2 updates" in result["statusText"], (
        f"panel rendered the stable answer for an experimental click: "
        f"{result['statusText']!r}"
    )
    assert result["continuation"] is None


# ── Defect 1b: a pending stable continuation keeps polling the wrong channel ──


def test_pending_stable_continuation_does_not_hijack_experimental_click():
    result = _run_node(
        _consumer_script(
            post_payload=PENDING_EXPERIMENTAL,
            get_payloads=[PENDING_STABLE, EXPERIMENTAL_COMPLETE],
            continuation=STABLE_URL,
            override="experimental",
        )
    )

    assert result["postCount"] == 1, (
        f"experimental click never reached the forced POST: {result['requests']}"
    )
    assert result["requests"][0]["method"] == "POST", (
        f"the stale continuation was fetched before the requested check: "
        f"{result['requests']}"
    )
    polled = [r["url"] for r in result["requests"] if r["method"] == "GET"]
    assert polled, f"the requested check was never observed: {result['requests']}"
    assert all("channel=experimental" in url for url in polled), (
        f"observation followed the stable continuation: {polled}"
    )
    assert "WebUI: 2 updates" in result["statusText"], (
        f"experimental result was not rendered: {result['statusText']!r}"
    )
    assert result["continuation"] is None


# ── Defect 1c: a saved Agent-update preference change is ignored ──


def test_saved_agent_preference_change_does_not_reuse_continuation():
    result = _run_node(
        _consumer_script(
            post_payload=STABLE_COMPLETE,
            get_payloads=[STABLE_COMPLETE],
            continuation=STABLE_URL,
            override=None,
            selector="stable",
            ignore_agent=True,
        )
    )

    assert result["postCount"] == 1, (
        f"the saved include-Agent change reused the include_agent=1 continuation: "
        f"{result['requests']}"
    )
    assert result["requests"][0]["method"] == "POST", (
        f"the click went to the stale continuation first: {result['requests']}"
    )
    assert "Updates available" in result["statusText"], (
        f"the requested check was not rendered: {result['statusText']!r}"
    )


# ── Defect 2: an expired continuation swallows the fresh manual check ──


def test_expired_continuation_falls_through_to_the_forced_post():
    result = _run_node(
        _consumer_script(
            post_payload=STABLE_COMPLETE,
            get_payloads=[DISABLED],
            continuation=STABLE_URL,
            override=None,
            selector="stable",
            ignore_agent=False,
        )
    )

    assert result["postCount"] == 1, (
        f"an expired authorization answered the click instead of performing the "
        f"manual check: {result['requests']}"
    )
    assert result["requests"][0]["method"] == "GET", (
        "the click must try the continuation before falling through"
    )
    assert result["requests"][-1]["method"] == "POST", (
        f"no forced POST followed the unavailable continuation: {result['requests']}"
    )
    assert "Updates available" in result["statusText"], (
        f"the manual result was not rendered: {result['statusText']!r}"
    )
    assert "disabled" not in result["statusText"].lower(), (
        f"panel still claims updates are disabled: {result['statusText']!r}"
    )
    assert result["continuation"] is None


# ── Controls: matching-key retrieval stays cache-only, terminal errors stay errors ──


def test_matching_key_continuation_stays_cache_only():
    result = _run_node(
        _consumer_script(
            post_payload=STABLE_COMPLETE,
            get_payloads=[STABLE_COMPLETE],
            continuation=STABLE_URL,
            override=None,
            selector="stable",
            ignore_agent=False,
        )
    )

    assert result["postCount"] == 0, f"retrieval started new work: {result['requests']}"
    assert result["requests"] == [{"url": STABLE_URL, "method": "GET"}], (
        f"retrieval did not target the matching continuation: {result['requests']}"
    )
    assert "WebUI: 7 updates" in result["statusText"]
    assert result["continuation"] is None


def test_terminal_failure_in_continuation_stays_an_error():
    terminal = {
        "webui": {"error": "read failed", "on_branch": True, "no_git": False},
        "agent": {"behind": 0, "ignored": False},
    }
    result = _run_node(
        _consumer_script(
            post_payload=STABLE_COMPLETE,
            get_payloads=[terminal],
            continuation=STABLE_URL,
            override=None,
            selector="stable",
            ignore_agent=False,
        )
    )

    assert result["postCount"] == 0, (
        f"a terminal failure started redundant Git work: {result['requests']}"
    )
    assert "Update check failed" in result["statusText"], (
        f"terminal failure was not preserved as an error: {result['statusText']!r}"
    )
    assert result["continuation"] is None
