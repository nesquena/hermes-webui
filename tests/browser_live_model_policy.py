#!/usr/bin/env python3
"""
Headless browser regression for the live-model discovered-vs-pinned policy.

WHY THIS EXISTS
  `static/ui.js` used to keep a browser response cache for `/api/models/live`,
  keyed by profile + provider. That cache could replay a broad *discovered*
  catalog after the same profile switched to a strict model pin, because the
  client copy never consulted the policy-keyed server cache. The server fix
  (60s TTL keyed by profile, provider, and a discovered-vs-pinned policy
  fingerprint) cannot help if the browser short-circuits on a local hit.
  #7404 review removed the browser cache; this gate locks that in behaviourally.

  It is a regression guard, not a unit test: it boots the real `server.py`
  agent-free and drives the real `populateModelDropdown()` entry point, stubbing
  `/api/models/live` with Playwright so no provider, credential, or agent is
  needed. Two assertions are the required regressions:
    1. Same-profile discovered -> strict pin: the stale unpinned model must not
       be re-applied, AND the endpoint must actually be re-requested (proving
       the browser did not short-circuit).
    2. Profile switch: a catalog served for profile A must not be applied when
       the active profile is B.
  A third case exercises the in-flight profile re-verification: a response for
  the profile captured at fetch start must be dropped if the profile changed
  before it was applied.
  A fourth case exercises the reference-counted pending entry (#7404 review):
  two overlapping fetches for the same profile+provider share one key, and the
  key must stay pending until the older one has resolved and only clear when the
  last one resolves. The route handler is put in "hold" mode so response timing
  is released deterministically from the test (never with a blocking sleep).

  Two more cases lock in the #7404 separation of policy authority from
  per-target authority: the composer (`#modelSelect`) and Settings
  (`#settingsModel`) selects are independent live-model publishers, so a rebuild
  of one must not reject an in-flight request for the other, and the pending
  projection must be per-target. Both directions are driven through the real
  production entry points (`loadSettingsPanel()` and `populateModelDropdown()`).

USAGE
  python tests/browser_live_model_policy.py
  (Requires: playwright + chromium. Boots server.py on an ephemeral port with an
  isolated temp state dir and no agent.)

EXIT CODES
  0 — all policy regressions held
  1 — a regression was observed (stale model re-applied, no re-fetch, or
      cross-profile leak)
  2 — environment/setup failure (server didn't boot, playwright missing, etc.)
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# 'openrouter' avoids the `@provider:` ID prefixing `_addLiveModelsToSelect`
# applies to portal-style providers, keeping option values easy to assert.
PROVIDER = "openrouter"

BROAD_DISCOVERED = [
    {"id": "broad-live-1", "label": "Broad Live 1"},
    {"id": "broad-live-2", "label": "Broad Live 2"},
    {"id": "broad-live-3", "label": "Broad Live 3"},
]
STRICT_PIN = [{"id": "strict-pin-1", "label": "Strict Pin 1"}]
PROFILE_A_CATALOG = [{"id": "a-live-1", "label": "A Live 1"}]
PROFILE_B_CATALOG = [{"id": "b-live-1", "label": "B Live 1"}]
INFLIGHT_CATALOG = [{"id": "inflight-live-1", "label": "Inflight Live 1"}]

PROFILE_A = "policy-profile-a"
PROFILE_B = "policy-profile-b"
PROFILE_INFLIGHT = "policy-profile-inflight"
PROFILE_INFLIGHT_RACED = "policy-profile-inflight-raced"
PROFILE_CONCURRENT = "policy-profile-concurrent"
CONCURRENT_CATALOG = [{"id": "concurrent-live-1", "label": "Concurrent Live 1"}]
PROFILE_SAME_PROFILE_RACE = "policy-profile-same-race"
# #7404 review P1 regression: advancing the policy generation without starting a
# replacement request drops the live catalog. Distinct catalog so the
# replacement's models are distinguishable from the held broad response's.
PROFILE_SAVE_POLICY = "policy-profile-save"
AFTER_POLICY_CATALOG = [{"id": "after-policy-live-1", "label": "After Policy Live 1"}]
# #7404 review: composer (modelSelect) and Settings (settingsModel) are
# independent live-model publishers. Their own catalogs make a cross-target
# rebuild that drops one of them observable.
PROFILE_TARGET_A = "policy-profile-target-a"
PROFILE_TARGET_B = "policy-profile-target-b"
PROFILE_TARGET_PENDING = "policy-profile-target-pending"
# #7404 review: the policy-change pending-visibility gap. The composer key must
# never be absent across _liveModelPolicyChanged()'s generation advance.
PROFILE_POLICY_GAP = "policy-profile-gap"
COMPOSER_CATALOG = [{"id": "composer-live-1", "label": "Composer Live 1"}]
SETTINGS_CATALOG = [{"id": "settings-live-1", "label": "Settings Live 1"}]
# #7404 review P1 (Settings target): a settingsModel live request in flight when
# a changed default model is saved is invalidated by the global generation
# advance; the replacement must rebuild the Settings picker, not just the
# composer, or live-only models vanish from Settings until the panel is rebuilt.
PROFILE_SETTINGS_POLICY = "policy-profile-settings-save"
SETTINGS_BEFORE_CATALOG = [{"id": "settings-before-1", "label": "Settings Before 1"}]
SETTINGS_AFTER_CATALOG = [{"id": "settings-after-1", "label": "Settings After 1"}]
SETTINGS_AFTER_CATALOG_2 = [{"id": "settings-after-2", "label": "Settings After 2"}]

BENIGN = [
    "favicon",
    "manifest.json",
    "serviceworker",
    "sw.js",
    "the server responded with a status of 404",
]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(base_url: str, timeout: float = 30.0, proc: subprocess.Popen | None = None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.25)
    return False


def _terminate_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _option_values(page) -> list[str]:
    return page.evaluate(
        "Array.from(document.querySelectorAll('#modelSelect option')).map(o => o.value)"
    )


def _wait_for_option(page, model_id: str, timeout: float = 8000.0) -> bool:
    try:
        page.wait_for_function(
            "id => Array.from(document.querySelectorAll('#modelSelect option'))"
            ".some(o => o.value === id)",
            arg=model_id,
            timeout=timeout,
        )
        return True
    except Exception:
        return False


def _settings_option_values(page) -> list[str]:
    return page.evaluate(
        "Array.from(document.querySelectorAll('#settingsModel option')).map(o => o.value)"
    )


def _wait_for_settings_option(page, model_id: str, timeout: float = 8000.0) -> bool:
    try:
        page.wait_for_function(
            "id => Array.from(document.querySelectorAll('#settingsModel option'))"
            ".some(o => o.value === id)",
            arg=model_id,
            timeout=timeout,
        )
        return True
    except Exception:
        return False


def _wait_for_live_requests(page, count: int, timeout: float = 8000.0) -> bool:
    """Wait for the browser to issue more than *count* live-model fetches.

    MUST go through a Playwright call (``wait_for_function``) rather than a
    Python ``time.sleep`` busy-wait: route handlers are dispatched on
    Playwright's event loop, which only advances while a Playwright API call is
    pumping. A pure-Python poll never lets the intercepted request be fulfilled,
    so the counter would never move and the wait would hang until the CI job
    timeout. ``wait_for_function`` also measures the app's own fetch call, which
    is exactly the "did the browser really re-request?" property under test.
    """
    try:
        page.wait_for_function(
            "n => (window.__liveFetchCount || 0) > n",
            arg=count,
            timeout=timeout,
        )
        return True
    except Exception:
        return False


def _wait_for_js(page, expression: str, arg=None, timeout: float = 8000.0) -> bool:
    """Return True once *expression* is truthy in the page, else False.

    ``wait_for_function`` raises on timeout; this normalises that to a bool and
    always pumps Playwright's event loop until the condition holds.
    """
    try:
        page.wait_for_function(expression, arg=arg, timeout=timeout)
        return True
    except Exception:
        return False


def _wait_for_held_routes(stub: "LiveModelStub", count: int, page, timeout: float = 8000.0) -> bool:
    """Pump Playwright until *count* live routes are held by the stub.

    Uses ``page.wait_for_timeout`` so each iteration advances Playwright's event
    loop (which is what actually dispatches route handlers); a Python
    ``time.sleep`` would deadlock the interception. ``timeout`` is in
    milliseconds, matching the other helpers and Playwright's own API.
    """
    deadline = time.time() + timeout / 1000.0
    while len(stub.held_routes) < count:
        if time.time() > deadline:
            return False
        page.wait_for_timeout(25)
    return True


def _capture_page_errors(page) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []

    def on_console(message):
        if message.type != "error":
            return
        text = message.text
        if not any(needle in text.lower() for needle in BENIGN):
            errors.append(("console", text))

    page.on("console", on_console)
    page.on("pageerror", lambda error: errors.append(("pageerror", str(error))))
    return errors


class LiveModelStub:
    """Serves deterministic `/api/models` and `/api/models/live` payloads."""

    def __init__(self) -> None:
        self.active_provider = PROVIDER
        self.live_models: list[dict] = []
        self.live_request_count = 0
        self.live_requests: list[str] = []
        # When set, live responses are held instead of fulfilled so the test can
        # release overlapping requests one at a time, deterministically.
        self.hold_live = False
        self.held_routes: list = []
        self.fulfilled_count = 0

    @staticmethod
    def _is_live_url(url: str) -> bool:
        return url.split("?", 1)[0].endswith("/api/models/live")

    def _models_payload(self) -> dict:
        return {
            "active_provider": self.active_provider,
            "default_model": "static-base",
            "configured_model_badges": {},
            "groups": [
                {
                    "provider": "OpenRouter",
                    "provider_id": self.active_provider,
                    "models": [{"id": "static-base", "label": "Static Base"}],
                }
            ],
        }

    def _live_payload(self) -> dict:
        return {"provider": self.active_provider, "models": self.live_models}

    def handle_live(self, route) -> None:
        self.live_request_count += 1
        self.live_requests.append(route.request.url)
        if self.hold_live:
            self.held_routes.append(route)
            return
        self.release_live(route)

    def release_live(self, route) -> None:
        self.fulfilled_count += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(self._live_payload()),
        )

    def handle_models(self, route) -> None:
        if self._is_live_url(route.request.url):
            self.handle_live(route)
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(self._models_payload()),
        )


def _install_flip_on_live_fetch(page) -> None:
    """Test harness hook: flip the active profile when the next live fetch runs.

    Runs in the page before app scripts. `_fetchLiveModels()` captures the
    profile before awaiting `fetch`; this hook changes it synchronously inside
    that call so the captured value and the post-await value differ.
    """
    page.add_init_script(
        """
        (() => {
          const realFetch = window.fetch.bind(window);
          window.__flipProfileOnNextLiveFetch = null;
          window.__liveFetchCount = 0;
          window.fetch = function(input, init) {
            const url = (typeof input === 'string') ? input : (input && input.url) || '';
            if (url.includes('/api/models/live')) {
              window.__liveFetchCount += 1;
              if (window.__flipProfileOnNextLiveFetch) {
                const target = window.__flipProfileOnNextLiveFetch;
                window.__flipProfileOnNextLiveFetch = null;
                S.activeProfile = target;
              }
            }
            return realFetch(input, init);
          };
        })();
        """
    )


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SETUP FAIL: playwright is not installed", file=sys.stderr)
        return 2

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    if not os.path.exists(server_py):
        print(f"SETUP FAIL: server.py not found at {server_py}", file=sys.stderr)
        return 2

    port = int(os.getenv("HERMES_LIVE_MODEL_POLICY_PORT", "") or _free_port())
    base_url = f"http://127.0.0.1:{port}"
    state_dir = tempfile.mkdtemp(prefix="hermes-live-model-policy-")
    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY"):
            env.pop(key, None)
    env.update({
        "HERMES_WEBUI_PORT": str(port),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": state_dir,
        "HERMES_HOME": state_dir,
        "HERMES_BASE_HOME": state_dir,
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
    })

    log_path = os.path.join(state_dir, "server.log")
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, server_py],
        cwd=repo_root,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    browser = None
    playwright = None
    try:
        if not _wait_for_health(base_url, timeout=30, proc=proc):
            print("SETUP FAIL: server did not become healthy in 30s", file=sys.stderr)
            log.flush()
            with open(log_path) as handle:
                print(handle.read()[-2000:], file=sys.stderr)
            return 2

        stub = LiveModelStub()
        stub.live_models = list(BROAD_DISCOVERED)
        errors: list[tuple[str, str]] = []
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(base_url=base_url)
        page = context.new_page()
        _install_flip_on_live_fetch(page)
        page.route("**/api/models**", stub.handle_models)
        page.route("**/api/models/live**", stub.handle_live)
        errors = _capture_page_errors(page)
        page.goto("/", wait_until="domcontentloaded")
        page.wait_for_selector("#modelSelect", state="attached", timeout=15000)

        # Seed the dropdown with the broad discovered catalog (the app's own
        # boot hydration does this; wait for it to land).
        if not _wait_for_option(page, "broad-live-1"):
            raise AssertionError(
                "setup: discovered catalog was never applied on boot; "
                f"observed options={_option_values(page)!r}; "
                f"live requests={stub.live_request_count}; errors={errors!r}"
            )
        print("OK  discovered catalog applied:", _option_values(page))
        baseline_requests = stub.live_request_count

        # --- Regression 1: same-profile discovered -> strict pin --------------
        stub.live_models = list(STRICT_PIN)
        page.evaluate("void populateModelDropdown()")
        applied_pin = _wait_for_option(page, "strict-pin-1")
        ids_after_pin = _option_values(page)
        if not applied_pin:
            raise AssertionError(
                "same-profile discovered->pin: strict pin 'strict-pin-1' was never "
                f"applied; observed options={ids_after_pin!r}; "
                f"live requests before={baseline_requests} after={stub.live_request_count}"
            )
        if stub.live_request_count <= baseline_requests:
            raise AssertionError(
                "same-profile discovered->pin: browser did not re-request "
                f"/api/models/live after the policy change; observed requests="
                f"{stub.live_request_count} (baseline={baseline_requests}); "
                f"options={ids_after_pin!r}"
            )
        if "broad-live-1" in ids_after_pin:
            raise AssertionError(
                "same-profile discovered->pin: stale unpinned model 'broad-live-1' "
                f"was re-applied; observed options={ids_after_pin!r}"
            )
        print(
            "OK  same-profile pin replaced discovered catalog:",
            ids_after_pin,
            f"(live requests {baseline_requests}->{stub.live_request_count})",
        )

        # --- Regression 2: profile switch ------------------------------------
        page.evaluate(f"S.activeProfile = {PROFILE_A!r}")
        stub.live_models = list(PROFILE_A_CATALOG)
        page.evaluate("void populateModelDropdown()")
        if not _wait_for_option(page, "a-live-1"):
            raise AssertionError(
                "profile switch: catalog for profile A was never applied; "
                f"observed options={_option_values(page)!r}"
            )
        page.evaluate(f"S.activeProfile = {PROFILE_B!r}")
        stub.live_models = list(PROFILE_B_CATALOG)
        page.evaluate("void populateModelDropdown()")
        applied_b = _wait_for_option(page, "b-live-1")
        ids_after_switch = _option_values(page)
        if not applied_b:
            raise AssertionError(
                "profile switch: catalog for profile B was never applied; "
                f"observed options={ids_after_switch!r}"
            )
        if "a-live-1" in ids_after_switch:
            raise AssertionError(
                "profile switch: profile A catalog 'a-live-1' leaked into profile "
                f"B; observed options={ids_after_switch!r}"
            )
        print("OK  profile switch dropped the previous profile catalog:", ids_after_switch)

        # --- Case 3: in-flight profile re-verification -----------------------
        page.evaluate(f"S.activeProfile = {PROFILE_INFLIGHT!r}")
        stub.live_models = list(INFLIGHT_CATALOG)
        before_inflight = page.evaluate("window.__liveFetchCount || 0")
        page.evaluate(f"window.__flipProfileOnNextLiveFetch = {PROFILE_INFLIGHT_RACED!r}")
        page.evaluate("void populateModelDropdown()")
        if not _wait_for_live_requests(page, before_inflight):
            raise AssertionError(
                "in-flight profile check: /api/models/live was never requested; "
                f"observed requests={stub.live_request_count}"
            )
        page.wait_for_function(
            "profile => typeof S !== 'undefined' && S.activeProfile === profile",
            arg=PROFILE_INFLIGHT_RACED,
            timeout=5000,
        )
        page.wait_for_timeout(500)
        ids_after_inflight = _option_values(page)
        if "inflight-live-1" in ids_after_inflight:
            raise AssertionError(
                "in-flight profile check: catalog captured for profile "
                f"{PROFILE_INFLIGHT!r} was applied after the active profile changed "
                f"to {PROFILE_INFLIGHT_RACED!r}; observed options={ids_after_inflight!r}"
            )
        print("OK  in-flight response dropped after profile changed:", ids_after_inflight)

        # --- Case 4: composer held, Settings rebuild (direction A) -------------
        #
        # The reported bug: loadSettingsPanel() advanced the GLOBAL policy
        # generation, so a valid composer request already in flight was then
        # rejected as stale with no replacement. A Settings rebuild must only
        # supersede settingsModel; the composer must still publish its catalog.
        page.evaluate(f"S.activeProfile = {PROFILE_TARGET_A!r}")
        stub.live_models = list(COMPOSER_CATALOG)
        stub.hold_live = True
        stub.held_routes = []
        page.evaluate(
            "() => {"
            "  const sel = document.getElementById('modelSelect');"
            f"  void _fetchLiveModels({PROVIDER!r}, sel);"
            "}"
        )
        if not _wait_for_held_routes(stub, 1, page):
            raise AssertionError("direction A: composer fetch was never held")
        # Rebuild the Settings dropdown through its production path while the
        # composer request is in flight.
        stub.live_models = list(SETTINGS_CATALOG)
        page.evaluate("void loadSettingsPanel()")
        if not _wait_for_held_routes(stub, 2, page):
            raise AssertionError("direction A: Settings fetch was never held")
        # Release both: the composer (older, held across the Settings rebuild)
        # first, then the Settings response.
        stub.live_models = list(COMPOSER_CATALOG)
        stub.release_live(stub.held_routes[0])
        stub.live_models = list(SETTINGS_CATALOG)
        stub.release_live(stub.held_routes[1])
        if not _wait_for_option(page, "composer-live-1"):
            raise AssertionError(
                "direction A: the composer catalog was dropped when Settings was "
                "rebuilt — its in-flight request was rejected with no "
                f"replacement; composer options={_option_values(page)!r}"
            )
        if not _wait_for_settings_option(page, "settings-live-1"):
            raise AssertionError(
                "direction A: the Settings catalog did not publish; "
                f"settings options={_settings_option_values(page)!r}"
            )
        print(
            "OK  Settings rebuild left the composer catalog intact:",
            _option_values(page),
            "settings:",
            _settings_option_values(page),
        )

        # --- Case 5: Settings held, composer rebuild (direction B) -------------
        #
        # The reverse: a composer/session refresh (populateModelDropdown) advanced
        # the global generation, rejecting an in-flight settingsModel request.
        # A composer rebuild must only supersede the composer.
        page.evaluate(f"S.activeProfile = {PROFILE_TARGET_B!r}")
        stub.live_models = list(SETTINGS_CATALOG)
        stub.hold_live = True
        stub.held_routes = []
        page.evaluate("void loadSettingsPanel()")
        if not _wait_for_held_routes(stub, 1, page):
            raise AssertionError("direction B: Settings fetch was never held")
        # Production composer rebuild while the Settings request is in flight.
        stub.live_models = list(COMPOSER_CATALOG)
        page.evaluate("void populateModelDropdown()")
        if not _wait_for_held_routes(stub, 2, page):
            raise AssertionError("direction B: composer fetch was never held")
        stub.live_models = list(SETTINGS_CATALOG)
        stub.release_live(stub.held_routes[0])
        stub.live_models = list(COMPOSER_CATALOG)
        stub.release_live(stub.held_routes[1])
        if not _wait_for_settings_option(page, "settings-live-1"):
            raise AssertionError(
                "direction B: the Settings catalog was dropped when the composer "
                "was rebuilt — its in-flight request was rejected with no "
                f"replacement; settings options={_settings_option_values(page)!r}"
            )
        if not _wait_for_option(page, "composer-live-1"):
            raise AssertionError(
                "direction B: the composer catalog did not publish; "
                f"composer options={_option_values(page)!r}"
            )
        print(
            "OK  composer rebuild left the Settings catalog intact:",
            _settings_option_values(page),
            "composer:",
            _option_values(page),
        )

        # --- Case 6: overlapping requests must keep the pending key alive -----
        #
        # syncTopbar() defers a model correction while a fetch is pending, so the
        # observable requirement is: while ANY request for a key is in flight,
        # _liveModelFetchPending.has(key) must be true. This case asserts on
        # .has() only — .has() exists on both a Set (the pre-fix shape, which
        # cleared the key as soon as ANY request finished) and the current
        # reference-counted Map. That way it fails pre-fix for the RACE rather
        # than for an API mismatch (e.g. .get is not a function on a Set).
        page.evaluate(f"S.activeProfile = {PROFILE_CONCURRENT!r}")
        stub.live_models = list(CONCURRENT_CATALOG)
        pending_key = page.evaluate(f"() => _liveModelFetchKey({PROVIDER!r})")
        # Hold live responses so both fetches are genuinely in flight at once.
        # They are released explicitly below; no blocking sleep is used.
        stub.hold_live = True
        stub.held_routes = []
        page.evaluate(
            "() => {"
            "  const sel = document.getElementById('modelSelect');"
            f"  void _fetchLiveModels({PROVIDER!r}, sel);"
            f"  void _fetchLiveModels({PROVIDER!r}, sel);"
            "}"
        )
        if not _wait_for_held_routes(stub, 2, page):
            raise AssertionError(
                "concurrency: expected two overlapping /api/models/live "
                f"requests to be held; held={len(stub.held_routes)} "
                f"total={stub.live_request_count}"
            )
        if not page.evaluate("key => _liveModelFetchPending.has(key)", pending_key):
            raise AssertionError(
                "concurrency: key must be pending while both requests are in flight"
            )
        # Two overlapping requests for the same target must share ONE
        # reference-counted key (2), not two per-request keys.
        if not _wait_for_js(
            page, "key => _liveModelFetchPending.get(key) === 2", pending_key
        ):
            raise AssertionError(
                "concurrency: expected two overlapping same-target requests to "
                "share one pending reference count of 2"
            )

        # Under the per-target latest-request authority (#7404 review), the
        # SECOND request supersedes the first. Releasing the OLDER response must
        # therefore NOT apply its catalog, while the shared key decays 2 -> 1
        # because the newer request is still in flight. Pre-fix the older one
        # was still applicable, so the drop assertion below is the behavioural
        # delta (the 2 -> 1 decay is orthogonal and unchanged).
        stub.release_live(stub.held_routes[0])
        if not _wait_for_js(
            page,
            "key => _liveModelFetchPending.has(key) && _liveModelFetchPending.get(key) === 1",
            pending_key,
        ):
            raise AssertionError(
                "concurrency: pending entry did not decay 2 -> 1 after the older "
                "of two overlapping same-target requests completed while the "
                "newer was still in flight"
            )
        if "concurrent-live-1" in _option_values(page):
            raise AssertionError(
                "concurrency: the superseded older response applied its catalog "
                f"after a newer same-target request had claimed the target; "
                f"options={_option_values(page)!r}"
            )
        print(
            "OK  pending key decayed 2 -> 1 and the superseded response was "
            "rejected"
        )

        # Release the last (newest) response; now the catalog lands and only
        # then may the key clear.
        stub.release_live(stub.held_routes[1])
        if not _wait_for_option(page, "concurrent-live-1"):
            raise AssertionError(
                "concurrency: the newest overlapping request never applied its "
                f"catalog after being released; options={_option_values(page)!r}"
            )
        if not _wait_for_js(
            page, "key => !_liveModelFetchPending.has(key)", pending_key
        ):
            raise AssertionError(
                "concurrency: pending entry did not clear after the LAST "
                "overlapping request completed; still pending"
            )
        print("OK  pending key cleared after the last overlapping request")

        # --- Case 7: older broad response cannot win a same-profile race -------
        #
        # #7404 review blocking finding: response applicability was fenced only
        # by the active profile (and an optional requestSeq). The Settings caller
        # (`_fetchLiveModels(provider, modelSel)` with no requestSeq) is
        # unowned, so an older broad-discovered response could reach the
        # additive `_addLiveModelsToSelect()` AFTER a newer strict-pin refresh
        # already applied, re-adding a model the strict policy excludes. The fix
        # attaches an immutable owner token (profile + generation + select
        # identity) to every request and rejects it before DOM mutation.
        #
        # Sequence is the maintainer's exact ordering:
        #   broad-old held -> strict pin saved + refreshed -> release strict
        #   first -> release broad last -> broad-only model must be absent.
        page.evaluate(f"S.activeProfile = {PROFILE_SAME_PROFILE_RACE!r}")
        stub.live_models = list(BROAD_DISCOVERED)
        stub.hold_live = True
        stub.held_routes = []
        # Start the OLD broad request exactly the way loadSettingsPanel() does:
        # no requestSeq, so the pre-fix code fences it on the profile alone.
        page.evaluate(
            "() => {"
            "  const sel = document.getElementById('modelSelect');"
            f"  void _fetchLiveModels({PROVIDER!r}, sel);"
            "}"
        )
        if not _wait_for_held_routes(stub, 1, page):
            raise AssertionError(
                "same-profile race: broad request was never held; "
                f"held={len(stub.held_routes)} total={stub.live_request_count}"
            )
        broad_requests = stub.live_request_count

        # Same-profile policy change through the production save-refresh path
        # the provider key save (`_saveProviderKey`) invokes. This advances the
        # live-model generation and starts the strict refresh (held below).
        stub.live_models = list(STRICT_PIN)
        page.evaluate("() => _refreshModelDropdownsAfterProviderChange()")
        # A policy change now drives BOTH live-model targets, so collect both
        # replacements before releasing them. Pre-fix (Settings target absent)
        # only the composer replacement exists; the wait is best-effort and the
        # assertions below are authoritative.
        _wait_for_held_routes(stub, 3, page, timeout=4000)
        if len(stub.held_routes) < 2:
            raise AssertionError(
                "same-profile race: strict refresh was never held; "
                f"held={len(stub.held_routes)} total={stub.live_request_count}"
            )
        if stub.live_request_count <= broad_requests:
            raise AssertionError(
                "same-profile race: the endpoint was not re-requested after the "
                f"policy change; before={broad_requests} "
                f"after={stub.live_request_count}"
            )

        # Release the STRICT (newer) replacements first and let them apply. All
        # routes after index 0 are replacements (composer and/or Settings).
        for _route in stub.held_routes[1:]:
            stub.release_live(_route)
        if not _wait_for_option(page, "strict-pin-1"):
            raise AssertionError(
                "same-profile race: strict pin was never applied after its "
                f"response was released; options={_option_values(page)!r}"
            )

        # Release the BROAD (older) response last. Its stale token must be
        # rejected so its broad-only model never lands.
        stub.live_models = list(BROAD_DISCOVERED)
        stub.release_live(stub.held_routes[0])
        page.wait_for_timeout(300)
        ids_after_race = _option_values(page)
        if "strict-pin-1" not in ids_after_race:
            raise AssertionError(
                "same-profile race: strict pin disappeared after the stale broad "
                f"response completed; options={ids_after_race!r}"
            )
        if "broad-live-1" in ids_after_race:
            raise AssertionError(
                "same-profile race: older broad-discovered response re-appended "
                "'broad-live-1' after the newer strict result; "
                f"options={ids_after_race!r}"
            )
        print(
            "OK  older broad response rejected after newer strict result:",
            ids_after_race,
            f"(live requests {broad_requests}->{stub.live_request_count})",
        )

        # --- Case 8: saveSettings policy change invalidates AND re-requests ----
        #
        # #7404 review P1: saveSettings() advanced the live-model policy
        # generation on a changed default-model save but started no replacement
        # request. The in-flight live-model response was then rejected as stale,
        # so live-only models silently disappeared from the dropdown. The fix
        # routes both save branches through _liveModelPolicyChanged(), which
        # BOTH invalidates the in-flight response AND starts a replacement.
        #
        # This case drives that production chokepoint. Pre-fix the symbol does
        # not exist, so fall back to the real pre-fix helper (advance only),
        # which reproduces the dropped-catalog defect without a missing-symbol
        # error.
        page.evaluate(f"S.activeProfile = {PROFILE_SAVE_POLICY!r}")
        stub.live_models = list(BROAD_DISCOVERED)
        stub.hold_live = True
        stub.held_routes = []
        # In-flight request captured under the pre-save policy (the Settings open
        # path starts one with an unrequestSeq'd _fetchLiveModels call). Held so
        # it arrives AFTER the save's policy change.
        page.evaluate(
            "() => {"
            "  const sel = document.getElementById('modelSelect');"
            f"  void _fetchLiveModels({PROVIDER!r}, sel);"
            "}"
        )
        if not _wait_for_held_routes(stub, 1, page):
            raise AssertionError(
                "save-policy: in-flight broad request was never held; "
                f"held={len(stub.held_routes)} total={stub.live_request_count}"
            )
        requests_before_save = stub.live_request_count

        # The replacement request will carry this catalog. Responses are
        # generated at release time, so the held broad route is re-armed with
        # BROAD_DISCOVERED again just before it is released below.
        stub.live_models = list(AFTER_POLICY_CATALOG)
        page.evaluate(
            "() => {"
            "  if (typeof _liveModelPolicyChanged === 'function') {"
            "    _liveModelPolicyChanged();"
            "  } else if (typeof _liveModelAdvancePolicyGeneration === 'function') {"
            "    _liveModelAdvancePolicyGeneration();"
            "  }"
            "}"
        )

        # Wait for the replacements (composer and/or Settings). Pre-fix none is
        # issued, so this returns False after the timeout and the stale response
        # below is still released to prove the invalidation half.
        replacement_held = _wait_for_held_routes(stub, 2, page, timeout=4000)
        if replacement_held:
            # Give BOTH targets' replacements a chance to arrive before draining
            # them all with the post-save catalog.
            _wait_for_held_routes(stub, 3, page, timeout=2000)
            for _route in stub.held_routes[1:]:
                stub.release_live(_route)
            _wait_for_option(page, "after-policy-live-1")

        # Release the held broad (older, now stale) response LAST. Its stale
        # token must be rejected so its broad-only model never lands.
        stub.live_models = list(BROAD_DISCOVERED)
        stub.release_live(stub.held_routes[0])
        page.wait_for_timeout(300)
        ids_after_save = _option_values(page)

        # Half 1 (stale rejected).
        if "broad-live-1" in ids_after_save:
            raise AssertionError(
                "save-policy: the stale in-flight broad response re-appended "
                f"'broad-live-1' after the policy change; options={ids_after_save!r}"
            )
        # Half 2 (catalog not dropped). Assert the live models are present
        # BEFORE the request-count check so the pre-fix failure reports the
        # dropped catalog (missing models), not merely a missing call.
        if "after-policy-live-1" not in ids_after_save:
            raise AssertionError(
                "save-policy dropped the live catalog: after the policy change "
                "the in-flight response was rejected and no replacement live "
                "models ever landed, so live-only models are MISSING from the "
                f"dropdown; options={ids_after_save!r}; "
                f"requests before={requests_before_save} "
                f"after={stub.live_request_count}; "
                f"replacement_held={replacement_held}"
            )
        if stub.live_request_count <= requests_before_save:
            raise AssertionError(
                "save-policy: the policy change did not re-request "
                f"/api/models/live; before={requests_before_save} "
                f"after={stub.live_request_count}"
            )
        print(
            "OK  saveSettings policy change invalidated the in-flight response "
            "AND restored the live catalog:",
            ids_after_save,
            f"(live requests {requests_before_save}->{stub.live_request_count}, "
            f"replacement_held={replacement_held})",
        )

        # --- Case 9: pending ownership is per-target --------------------------
        #
        # #7404 review: _liveModelFetchKey() carried profile+provider+policy
        # generation but no target identity, so a settingsModel fetch and a
        # modelSelect fetch shared one pending key. syncTopbar() queries that key
        # to defer a composer model correction, so a Settings-only fetch made
        # the composer look pending (and could hide a real composer fetch).
        # Assert the two targets keep independent pending state in both
        # directions. The composer/settings key expressions work pre-fix too
        # (the extra select arg is just ignored by the old two-arg signature),
        # so a pre-fix failure reports the collision rather than a missing API.
        page.evaluate(f"S.activeProfile = {PROFILE_TARGET_PENDING!r}")
        stub.live_models = list(SETTINGS_CATALOG)
        stub.hold_live = True
        stub.held_routes = []
        settings_pending_expr = (
            "_liveModelFetchPending.has(_liveModelFetchKey("
            "window._activeProvider, undefined, document.getElementById('settingsModel')))"
        )
        composer_pending_expr = (
            "_liveModelFetchPending.has(_liveModelFetchKey("
            "window._activeProvider, undefined, document.getElementById('modelSelect')))"
        )
        # Settings-only fetch in flight: the composer must NOT look pending.
        page.evaluate(
            "() => {"
            "  const sel = document.getElementById('settingsModel');"
            f"  void _fetchLiveModels({PROVIDER!r}, sel);"
            "}"
        )
        if not _wait_for_held_routes(stub, 1, page):
            raise AssertionError("pending isolation: Settings fetch was never held")
        if page.evaluate("() => " + composer_pending_expr):
            raise AssertionError(
                "pending isolation: a Settings-only live fetch is reported as "
                "composer pending — syncTopbar() would defer the composer's model "
                "correction and hide a real composer fetch"
            )
        if not page.evaluate("() => " + settings_pending_expr):
            raise AssertionError(
                "pending isolation: the in-flight Settings fetch should be pending"
            )
        stub.release_live(stub.held_routes[0])
        if not _wait_for_js(page, "() => !" + settings_pending_expr):
            raise AssertionError("pending isolation: Settings pending key never cleared")
        stub.held_routes = []

        # Composer-only fetch in flight: Settings must NOT look pending.
        stub.live_models = list(COMPOSER_CATALOG)
        page.evaluate(
            "() => {"
            "  const sel = document.getElementById('modelSelect');"
            f"  void _fetchLiveModels({PROVIDER!r}, sel);"
            "}"
        )
        if not _wait_for_held_routes(stub, 1, page):
            raise AssertionError("pending isolation: composer fetch was never held")
        if page.evaluate("() => " + settings_pending_expr):
            raise AssertionError(
                "pending isolation: a composer-only live fetch is reported as "
                "Settings pending"
            )
        if not page.evaluate("() => " + composer_pending_expr):
            raise AssertionError(
                "pending isolation: the in-flight composer fetch should be pending"
            )
        stub.release_live(stub.held_routes[0])
        if not _wait_for_js(page, "() => !" + composer_pending_expr):
            raise AssertionError("pending isolation: composer pending key never cleared")
        print("OK  composer and Settings pending state are independent")

        # --- Case 10: policy change never leaves the composer unpending ------
        #
        # #7404 review: _liveModelPolicyChanged() advances the global policy
        # generation, which changes the composer's pending key. The replacement
        # rebuild only re-registers that key after an asynchronous /api/models
        # round-trip, so without a synchronous placeholder there is a window in
        # which `has()` on the CURRENT composer key is false. syncTopbar() runs
        # from boot/messages/commands/session-loads and defers a model correction
        # only while that key is pending, so in that window it would persist a
        # static fallback for a fetch whose live catalog may yet arrive (#1169).
        #
        # The assertion reads the composer key in the SAME tick as the
        # chokepoint call, so it observes the transition itself rather than a
        # later rebuild. Pre-fix the value is False (the gap); post-fix the
        # synchronous retain makes it True.
        page.evaluate(f"S.activeProfile = {PROFILE_POLICY_GAP!r}")
        page.evaluate(f"window._activeProvider = {PROVIDER!r}")
        stub.live_models = list(COMPOSER_CATALOG)
        stub.hold_live = True
        stub.held_routes = []
        page.evaluate(
            "() => {"
            "  const sel = document.getElementById('modelSelect');"
            f"  void _fetchLiveModels({PROVIDER!r}, sel);"
            "}"
        )
        if not _wait_for_held_routes(stub, 1, page):
            raise AssertionError("policy-gap: composer fetch was never held")
        composer_key_expr = (
            "_liveModelFetchKey("
            f"{PROVIDER!r}, undefined, document.getElementById('modelSelect'))"
        )
        pending_in_change_tick = page.evaluate(
            "() => {"
            "  if (typeof _liveModelPolicyChanged === 'function') {"
            "    _liveModelPolicyChanged();"
            "  }"
            "  return _liveModelFetchPending.has(" + composer_key_expr + ");"
            "}"
        )
        if not pending_in_change_tick:
            raise AssertionError(
                "policy-gap: the composer pending entry vanished in the tick the "
                "policy changed (`has()` on the current composer key was false); "
                "a syncTopbar() in that window would persist a static fallback "
                "for the in-flight live fetch"
            )
        # The replacement live fetch is held too; release every held route, then
        # assert the composer key eventually clears. That proves the placeholder
        # retained by the chokepoint is released on its settle path (no leak).
        if not _wait_for_held_routes(stub, 2, page):
            raise AssertionError(
                "policy-gap: the replacement composer fetch was never held; "
                f"held={len(stub.held_routes)} total={stub.live_request_count}"
            )
        # Give the Settings replacement (now also driven by the chokepoint) a
        # chance to arrive too, then drain every held route.
        _wait_for_held_routes(stub, 3, page, timeout=2000)
        while stub.held_routes:
            stub.release_live(stub.held_routes.pop(0))
        if not _wait_for_js(
            page, "key => !_liveModelFetchPending.has(key)", composer_key_expr
        ):
            raise AssertionError(
                "policy-gap: the composer pending key never cleared after the "
                "replacement settled — the retained placeholder leaked"
            )
        print(
            "OK  composer stayed pending in the policy-change tick and the "
            "placeholder was released after settle"
        )

        # --- Case 11: Settings replacement on a policy change (new P1) --------
        #
        # Greptile P1: when a settingsModel live request is still in flight as
        # the user saves a changed default model, _liveModelPolicyChanged()
        # invalidated that response globally but started a replacement only for
        # modelSelect, leaving live-only models absent from the Settings picker
        # until it was rebuilt. This drives the real Settings open path
        # (loadSettingsPanel) and the real policy chokepoint, then requires the
        # Settings picker to end up with the replacement's live-only catalog.
        #
        # The second half is the no-accumulation guarantee: a second policy save
        # must REPLACE the Settings live catalog. A bare _fetchLiveModels()
        # re-append (no innerHTML clear) would leave the previous policy's
        # live-only model behind, so the stale-model assertion has teeth.
        while stub.held_routes:
            stub.release_live(stub.held_routes.pop(0))
        page.wait_for_timeout(50)
        page.evaluate(f"S.activeProfile = {PROFILE_SETTINGS_POLICY!r}")
        stub.live_models = list(SETTINGS_BEFORE_CATALOG)
        stub.hold_live = True
        stub.held_routes = []
        # Open Settings through its production loader so settingsModel is
        # populated and its live request is in flight under the pre-save policy.
        page.evaluate("void loadSettingsPanel()")
        if not _wait_for_held_routes(stub, 1, page):
            raise AssertionError("settings-policy: the Settings live fetch was never held")
        stale_settings_route = stub.held_routes[0]
        requests_before = stub.live_request_count

        # A changed default-model save through the production chokepoint.
        stub.live_models = list(SETTINGS_AFTER_CATALOG)
        page.evaluate(
            "() => {"
            "  if (typeof _liveModelPolicyChanged === 'function') {"
            "    _liveModelPolicyChanged();"
            "  }"
            "}"
        )
        # Give the replacements (composer + Settings) a chance to be issued.
        # Deliberately do NOT fail here: a pre-fix run must fail on the missing
        # Settings catalog below, not on a missing request symbol.
        _wait_for_held_routes(stub, 3, page, timeout=3000)
        # The held pre-save Settings response is now stale; its generation is
        # gone, so releasing it must NOT append its catalog.
        stub.live_models = list(SETTINGS_BEFORE_CATALOG)
        stub.release_live(stale_settings_route)
        # Every replacement carries the post-save catalog.
        stub.live_models = list(SETTINGS_AFTER_CATALOG)
        for route in list(stub.held_routes):
            if route is stale_settings_route:
                continue
            stub.release_live(route)
        stub.held_routes = []

        if not _wait_for_settings_option(page, "settings-after-1"):
            raise AssertionError(
                "settings-policy dropped the live catalog: after the policy change "
                "the in-flight Settings response was rejected and no replacement "
                "live models ever landed, so live-only models are MISSING from the "
                f"Settings picker; settings options={_settings_option_values(page)!r}; "
                f"requests before={requests_before} after={stub.live_request_count}"
            )
        settings_ids = _settings_option_values(page)
        if "settings-before-1" in settings_ids:
            raise AssertionError(
                "settings-policy: the stale pre-save Settings response was applied "
                f"after the policy change; settings options={settings_ids!r}"
            )
        if settings_ids.count("settings-after-1") != 1:
            raise AssertionError(
                "settings-policy duplicated the replacement's live model; "
                f"settings options={settings_ids!r}"
            )
        # The composer replacement is released with the same catalog; proving it
        # also published shows the chokepoint drives BOTH targets, not one.
        if not _wait_for_option(page, "settings-after-1"):
            raise AssertionError(
                "settings-policy: the composer replacement did not publish while "
                f"Settings was rebuilt; composer options={_option_values(page)!r}"
            )
        print(
            "OK  Settings policy change invalidated the in-flight response AND "
            "restored the Settings live catalog:",
            settings_ids,
            f"(live requests {requests_before}->{stub.live_request_count})",
        )

        # Second policy save: the Settings rebuild must clear the previous live
        # catalog rather than accumulate it.
        while stub.held_routes:
            stub.release_live(stub.held_routes.pop(0))
        stub.held_routes = []
        stub.live_models = list(SETTINGS_AFTER_CATALOG_2)
        page.evaluate(
            "() => {"
            "  if (typeof _liveModelPolicyChanged === 'function') {"
            "    _liveModelPolicyChanged();"
            "  }"
            "}"
        )
        _wait_for_held_routes(stub, 2, page, timeout=3000)
        for route in list(stub.held_routes):
            stub.release_live(route)
        stub.held_routes = []
        if not _wait_for_settings_option(page, "settings-after-2"):
            raise AssertionError(
                "settings-policy: the second policy change did not publish its "
                f"live catalog; settings options={_settings_option_values(page)!r}"
            )
        settings_ids = _settings_option_values(page)
        if "settings-after-1" in settings_ids:
            raise AssertionError(
                "settings-policy accumulated the previous policy's live model "
                "instead of replacing it (bare re-append, no innerHTML clear); "
                f"settings options={settings_ids!r}"
            )
        if settings_ids.count("settings-after-2") != 1:
            raise AssertionError(
                "settings-policy duplicated the second replacement's live model; "
                f"settings options={settings_ids!r}"
            )
        print(
            "OK  second Settings policy save replaced (did not accumulate) the "
            "live catalog:",
            settings_ids,
        )

        # Drain any still-held live routes so browser.close() does not abort
        # them (an intercepted-but-unreleased route surfaces as a Playwright
        # teardown traceback even when the gate itself passed).
        while stub.held_routes:
            stub.release_live(stub.held_routes.pop(0))
        page.wait_for_timeout(100)

        if errors:
            raise AssertionError(f"unexpected browser errors: {errors!r}")
        print("\nLIVE MODEL POLICY GATE PASSED")
        return 0
    except Exception as error:
        print(f"\nLIVE MODEL POLICY GATE FAILED: {error}", file=sys.stderr)
        return 1
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
        _terminate_process(proc)
        log.close()


if __name__ == "__main__":
    sys.exit(main())
