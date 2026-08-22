#!/usr/bin/env python3
"""Headless served-build owner gate for the #6673 notification protocol."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = int(os.environ.get("ISSUE6673_BROWSER_PORT", "8797"))
BASE = f"http://127.0.0.1:{PORT}"
BROWSER_BASE = f"http://localhost:{PORT}"


def _health(timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    return False


def _serve(root: Path, state: Path) -> subprocess.Popen:
    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY"):
            env.pop(key, None)
    env.update({
        "HERMES_WEBUI_PORT": str(PORT), "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": str(state / "webui-state"),
        "HERMES_HOME": str(state / "hermes-home"), "HERMES_BASE_HOME": str(state / "hermes-home"),
        "HERMES_WEBUI_SKIP_ONBOARDING": "1", "HERMES_WEBUI_AGENT_DIR": str(state / "no-agent"),
    })
    log = (state / "server.log").open("w", encoding="utf-8")
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    process = subprocess.Popen([sys.executable, str(root / "server.py")], cwd=root, env=env,
                               stdout=log, stderr=subprocess.STDOUT, **flags)
    process._hermes_log = log  # type: ignore[attr-defined]
    return process


def _emit(page, event_id: str | None = None) -> None:
    page.evaluate("""
      eventId => {
        window._notificationsEnabled = true;
        const payload = JSON.stringify({description:'Approve the tool call.', session_id:'session-6673'});
        window.__issue6673Source.emit('approval', payload, eventId === null ? '' : eventId);
      }
    """, event_id)


def _records(page):
    return page.evaluate("""async () => (await (await navigator.serviceWorker.ready).getNotifications({tag:'hermes-session-6673'})).map(n => ({id:n.data.eventId, tag:n.tag, renotify:n.renotify, url:n.data.url}))""")


def _owner_row(page, event_id: str):
    return page.evaluate("""
      async eventId => new Promise((resolve, reject) => {
        const request = indexedDB.open('hermes-notifications', 1);
        request.onerror = () => reject(request.error || new Error('owner db open failed'));
        request.onsuccess = () => {
          const db = request.result;
          const tx = db.transaction('event-identities', 'readonly');
          const get = tx.objectStore('event-identities').get(['stream-6673', eventId]);
          get.onerror = () => reject(get.error || new Error('owner row read failed'));
          get.onsuccess = () => resolve(get.result || null);
        };
      })
    """, event_id)


def _observe_fallback_notifications(page, *, disable_constructor: bool = False, disable_worker_channel: bool = False, reject_first_registration: bool = False) -> None:
    page.evaluate("""
      async ({disableConstructor, disableWorkerChannel, rejectFirstRegistration}) => {
        const NativeNotification = window.Notification;
        let directCount = 0;
        function ObservedNotification(...args) {
          directCount += 1;
          window.__issue6673DirectCount = directCount;
          if (disableConstructor) throw new TypeError('page constructor unavailable');
          return new NativeNotification(...args);
        }
        Object.setPrototypeOf(ObservedNotification, NativeNotification);
        ObservedNotification.prototype = NativeNotification.prototype;
        window.Notification = ObservedNotification;
        if (disableWorkerChannel) window.MessageChannel = undefined;
        window.__issue6673RegistrationShowIds = [];
        window.__issue6673RegistrationDisplayedIds = [];
        window.__issue6673RegistrationShowErrors = [];
        const registration = await navigator.serviceWorker.getRegistration();
        if (registration) {
          const nativeShowNotification = registration.__issue6673NativeShowNotification || registration.showNotification.bind(registration);
          registration.__issue6673NativeShowNotification = nativeShowNotification;
          let registrationShowCount = 0;
          registration.showNotification = (...args) => {
            registrationShowCount += 1;
            window.__issue6673RegistrationShowCount = registrationShowCount;
            window.__issue6673RegistrationShowIds.push(args[1]?.data?.eventId || null);
            if (rejectFirstRegistration && registrationShowCount === 1) {
              window.__issue6673TransitionRegistration?.update?.().catch(() => {});
              const error = new Error('forced transition presentation rejection');
              window.__issue6673RegistrationShowErrors.push({id: args[1]?.data?.eventId || null, error: String(error)});
              return Promise.reject(error);
            }
            return Promise.resolve(nativeShowNotification(...args)).then(async result => {
              const displayed = await registration.getNotifications({tag: args[1]?.tag});
              window.__issue6673RegistrationDisplayedIds.push(...displayed.map(item => item?.data?.eventId || null));
              return result;
            }).catch(error => {
              window.__issue6673RegistrationShowErrors.push({id: args[1]?.data?.eventId || null, error: String(error)});
              throw error;
            });
          };
        }
        window.__issue6673DirectCount = 0;
        window.__issue6673RegistrationShowCount = 0;
      }
    """, {"disableConstructor": disable_constructor, "disableWorkerChannel": disable_worker_channel, "rejectFirstRegistration": reject_first_registration})


def _listen(page) -> None:
    page.evaluate("""
      () => {
        class BoundaryEventSource {
          constructor(url) { this.listeners = new Map(); this.readyState = 1; this.url = String(url || ''); this._lastEventId = ''; if (this.url.includes('/api/chat/stream?stream_id=')) window.__issue6673Source = this; }
          addEventListener(name, handler) { const list = this.listeners.get(name) || []; list.push(handler); this.listeners.set(name, list); }
          close() { this.readyState = 2; }
          emit(name, data, lastEventId) { if (lastEventId !== undefined) this._lastEventId = lastEventId; for (const handler of this.listeners.get(name) || []) handler({type:name, data, lastEventId:this._lastEventId}); }
        }
        window.EventSource = BoundaryEventSource;
        window._notificationsEnabled = true;
        window.__hermesSetBackgrounded(true);
        const originalSend = window._sendStreamNotification;
        window.__issue6673IdentityObservations = [];
        window._sendStreamNotification = (...args) => {
          const identity = args[2];
          window.__issue6673IdentityObservations.push(identity ? identity.lastEventId : null);
          return originalSend(...args);
        };
        S.session = {...(S.session || {}), session_id:'session-6673'};
        attachLiveStream('session-6673', 'stream-6673');
      }
    """)
    page.wait_for_function("() => Boolean(window.__issue6673Source?.listeners?.has('approval'))", timeout=15000)


def _counts(pages):
    return {
        "direct": sum(page.evaluate("() => window.__issue6673DirectCount") for page in pages),
        "registration": sum(page.evaluate("() => window.__issue6673RegistrationShowCount") for page in pages),
    }


def _wait_for_activity(page, delay: int = 1200) -> None:
    page.wait_for_timeout(delay)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("UNREACHED: playwright is unavailable; browser-service-worker owner proof requires exit 2", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    legacy_source = (root / "tests" / "fixtures" / "issue6673_legacy_sw.js").read_text(encoding="utf-8")
    temp = tempfile.TemporaryDirectory(prefix="hermes-6673-browser-", ignore_cleanup_errors=True)
    state = Path(temp.name)
    process = browser = playwright = None
    blocked_context = legacy_context = None
    try:
        process = _serve(root, state)
        if not _health():
            print(json.dumps({"status": "unreached", "reason": "served WebUI health check failed"}))
            return 2
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(channel="chromium", headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", f"--unsafely-treat-insecure-origin-as-secure={BROWSER_BASE}"])
        blocked_context = browser.new_context(base_url=BROWSER_BASE, permissions=["notifications"], service_workers="block")
        blocked_context.grant_permissions(["notifications"], origin=BROWSER_BASE)
        blocked_page = blocked_context.new_page()
        blocked_page.goto("/", wait_until="domcontentloaded")
        permission = blocked_page.evaluate("() => Notification.permission")
        if permission != "granted":
            print(json.dumps({"status": "unreached", "reason": "headless Notification permission is not granted", "permission": permission}))
            return 2
        _observe_fallback_notifications(blocked_page)
        _listen(blocked_page)
        _emit(blocked_page, "stream-6673:constructor-only")
        _wait_for_activity(blocked_page)
        constructor_only = _counts((blocked_page,))
        if constructor_only["direct"] < 1:
            raise AssertionError({"constructor_only": constructor_only})
        blocked_context.close()
        blocked_context = None

        legacy_context = browser.new_context(base_url=BROWSER_BASE, permissions=["notifications"])
        legacy_context.grant_permissions(["notifications"], origin=BROWSER_BASE)
        legacy_context.route("**/sw.js*", lambda route: route.fulfill(status=200, content_type="application/javascript", body=legacy_source))
        page_a = legacy_context.new_page()
        page_b = legacy_context.new_page()
        for page in (page_a, page_b):
            page.goto("/", wait_until="domcontentloaded")
            page.wait_for_function("async () => Boolean((await navigator.serviceWorker.ready).active)", timeout=15000)
            _observe_fallback_notifications(page, disable_constructor=True, disable_worker_channel=True)
            _listen(page)

        _emit(page_a, "stream-6673:registration-only")
        _wait_for_activity(page_a)
        registration_only = {**_counts((page_a,)), "records": _records(page_a)}
        if not any(record["id"] == "stream-6673:registration-only" for record in registration_only["records"]):
            raise AssertionError({"registration_only": registration_only})
        degraded_before = _counts((page_a, page_b))
        _emit(page_a, "stream-6673:degraded-two-tab")
        _emit(page_b, "stream-6673:degraded-two-tab")
        _wait_for_activity(page_a)
        _wait_for_activity(page_b, 100)
        degraded_after = _counts((page_a, page_b))
        degraded_two_tab = {
            "direct": degraded_after["direct"] - degraded_before["direct"],
            "registration": degraded_after["registration"] - degraded_before["registration"],
            "records": _records(page_a),
        }
        if degraded_two_tab["direct"] + degraded_two_tab["registration"] != 1:
            raise AssertionError({"degraded_two_tab": degraded_two_tab})
        if not any(record["id"] == "stream-6673:degraded-two-tab" for record in degraded_two_tab["records"]):
            raise AssertionError({"degraded_two_tab": degraded_two_tab})
        databases = page_a.evaluate("async () => (await indexedDB.databases()).map(item => item.name).filter(Boolean)")
        source = page_a.evaluate("async () => await (await fetch('/static/messages.js')).text()")
        registration = page_a.evaluate("""async () => {
          const old = await navigator.serviceWorker.getRegistration();
          await old.unregister();
          const version = encodeURIComponent(window.__HERMES_WEBUI_BUNDLE_VERSION__ || 'test');
          const current = await navigator.serviceWorker.register('/sw.js?v=' + version + '&issue6673_current=1');
          await navigator.serviceWorker.ready;
          window.__issue6673TransitionRegistration = current;
          return {scriptURL: current.active?.scriptURL || '', state: current.active?.state || ''};
        }""")
        legacy_context.unroute("**/sw.js*")
        _observe_fallback_notifications(page_a, disable_constructor=True, disable_worker_channel=True, reject_first_registration=True)
        page_a.evaluate("""async () => {
          const payload = JSON.stringify({description:'Approve the tool call.', session_id:'session-6673'});
          window.__issue6673Source.emit('approval', payload, 'stream-6673:activation-transition');
          return true;
        }""")
        page_a.wait_for_timeout(3000)
        page_a.wait_for_function("""() => navigator.serviceWorker.getRegistration().then(reg => Boolean(
          reg && reg.active && reg.active.state === 'activated' &&
          reg.active.scriptURL.includes('/sw.js?v=') &&
          reg.active.scriptURL.includes('issue6673_current=1')
        ))""", timeout=15000)
        registration = page_a.evaluate("""async () => {
          const current = await navigator.serviceWorker.getRegistration();
          return {scriptURL: current?.active?.scriptURL || '', state: current?.active?.state || ''};
        }""")
        activation_transition = {
            "records": page_a.evaluate("() => window.__issue6673RegistrationDisplayedIds.filter(id => id === 'stream-6673:activation-transition').map(id => ({id}))"),
            "registrationIds": page_a.evaluate("() => window.__issue6673RegistrationShowIds"),
        }
        if not any(record["id"] == "stream-6673:activation-transition" for record in activation_transition["records"]):
            raise AssertionError({"activation_transition": activation_transition, "counts": _counts((page_a,)), "identities": page_a.evaluate("() => window.__issue6673IdentityObservations"), "errors": page_a.evaluate("() => window.__issue6673RegistrationShowErrors")})
        page_a.close()
        page_b.close()
        page_a = legacy_context.new_page()
        page_b = legacy_context.new_page()
        for page in (page_a, page_b):
            page.goto("/", wait_until="domcontentloaded")
            page.wait_for_function("async () => Boolean((await navigator.serviceWorker.ready).active && navigator.serviceWorker.controller && navigator.serviceWorker.controller.scriptURL.includes('issue6673_current'))", timeout=15000)
            _observe_fallback_notifications(page)
            _listen(page)
        page_a.wait_for_timeout(500)
        _emit(page_a, "stream-6673:capable-same")
        _emit(page_b, "stream-6673:capable-same")
        page_a.wait_for_function("async () => (await (await navigator.serviceWorker.ready).getNotifications({tag:'hermes-session-6673'})).some(n => n.data && n.data.eventId === 'stream-6673:capable-same')", timeout=10000)
        same_event_records = _records(page_a)
        same_event_ids = [record["id"] for record in same_event_records if record.get("id") == "stream-6673:capable-same"]
        if same_event_ids != ["stream-6673:capable-same"]:
            raise AssertionError({"capable_same_event": same_event_records})
        page_a.wait_for_timeout(1000)
        _emit(page_a, "stream-6673:capable-next")
        page_a.wait_for_timeout(2000)
        records = _records(page_a)
        if not any(record["id"] == "stream-6673:capable-next" for record in records):
            raise AssertionError({"next_event": records, "observations": page_a.evaluate("() => window.__issue6673IdentityObservations"), "counts": _counts((page_a, page_b))})
        if not any(record["id"] == "stream-6673:capable-next" and record["renotify"] for record in records):
            raise AssertionError({"next_event": records})
        if not all(record["url"].startswith(BROWSER_BASE + "/") for record in records):
            raise AssertionError({"click_data": records})
        if "indexedDB.open(" not in source or "NOTIFICATION_OWNER_LEASE_MS" not in source or "BroadcastChannel" in source:
            raise AssertionError({"served_source": "page notification owner is absent"})
        exact_expired_id = "stream-6673:browser-exact-expired-lease"
        page_a.evaluate("""
          async eventId => {
            const registration = await navigator.serviceWorker.getRegistration();
            await registration.__issue6673NativeShowNotification('Done', {
              body: 'body', tag: 'hermes-session-6673', renotify: true,
              data: {eventId, url: location.href},
            });
            await new Promise((resolve, reject) => {
              const request = indexedDB.open('hermes-notifications', 1);
              request.onerror = () => reject(request.error || new Error('owner db open failed'));
              request.onsuccess = () => {
                const db = request.result;
                const tx = db.transaction('event-identities', 'readwrite');
                tx.objectStore('event-identities').put({
                  streamId: 'stream-6673', lastEventId: eventId, state: 'pending',
                  phase: 'presenting', token: 'browser-expired-token', expiresAt: Date.now() - 1,
                });
                tx.oncomplete = resolve;
                tx.onerror = () => reject(tx.error || new Error('owner seed failed'));
              };
            });
          }
        """, exact_expired_id)
        _observe_fallback_notifications(page_a, disable_constructor=True, disable_worker_channel=True)
        _emit(page_a, exact_expired_id)
        _wait_for_activity(page_a)
        exact_expired_lease = {
            "registration": page_a.evaluate("() => window.__issue6673RegistrationShowCount"),
            "records": [record for record in _records(page_a) if record["id"] == exact_expired_id],
            "row": _owner_row(page_a, exact_expired_id),
        }
        if exact_expired_lease["registration"] != 0 or len(exact_expired_lease["records"]) != 1:
            raise AssertionError({"exact_expired_lease": exact_expired_lease})
        remaining_databases = page_a.evaluate("async () => (await indexedDB.databases()).map(item => item.name).filter(Boolean)")
        if "hermes-notifications" not in remaining_databases:
            raise AssertionError({"served_databases": remaining_databases})
        if not registration or "sw.js?v=" not in registration["scriptURL"] or "issue6673_current=1" not in registration["scriptURL"] or registration["state"] != "activated":
            raise AssertionError({"updated_registration": registration})
        if registration_only["registration"] < 1:
            raise AssertionError({"registration_only": registration_only})
        if degraded_two_tab["registration"] != 1 or degraded_two_tab["direct"] != 0:
            raise AssertionError({"degraded_two_tab": degraded_two_tab})
        result = {
            "status": "passed",
            "permission": permission,
            "constructor_only": constructor_only,
            "registration_only": registration_only,
            "degraded_two_tab": degraded_two_tab,
            "activation_transition": activation_transition,
            "exact_expired_lease": exact_expired_lease,
            "databases_before_rebuild": databases,
            "records": records,
        }
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(f"REALITY GATE FAILED: {error}", file=sys.stderr)
        return 1
    finally:
        if legacy_context is not None:
            legacy_context.close()
        if blocked_context is not None:
            blocked_context.close()
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log = getattr(process, "_hermes_log", None)
            if log is not None:
                log.close()
        temp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
