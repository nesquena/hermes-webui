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


def _observe_fallback_notifications(page) -> None:
    page.evaluate("""
      async () => {
        const NativeNotification = window.Notification;
        let directCount = 0;
        function ObservedNotification(...args) {
          directCount += 1;
          window.__issue6673DirectCount = directCount;
          return new NativeNotification(...args);
        }
        Object.setPrototypeOf(ObservedNotification, NativeNotification);
        ObservedNotification.prototype = NativeNotification.prototype;
        window.Notification = ObservedNotification;
        const registration = await navigator.serviceWorker.getRegistration();
        if (registration) {
          const nativeShowNotification = registration.showNotification.bind(registration);
          let registrationShowCount = 0;
          registration.showNotification = (...args) => {
            registrationShowCount += 1;
            window.__issue6673RegistrationShowCount = registrationShowCount;
            return nativeShowNotification(...args);
          };
        }
        window.__issue6673DirectCount = 0;
        window.__issue6673RegistrationShowCount = 0;
        window.__issue6673FallbackCount = 0;
      }
    """)


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


def _fallback_count(pages):
    return sum(page.evaluate("() => window.__issue6673DirectCount + window.__issue6673RegistrationShowCount") for page in pages)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("UNREACHED: playwright is unavailable; browser-service-worker owner proof requires exit 2", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    temp = tempfile.TemporaryDirectory(prefix="hermes-6673-browser-", ignore_cleanup_errors=True)
    state = Path(temp.name)
    process = browser = playwright = None
    try:
        process = _serve(root, state)
        if not _health():
            print("UNREACHED: served WebUI health check failed", file=sys.stderr)
            return 2
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(channel="chromium", headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", f"--unsafely-treat-insecure-origin-as-secure={BROWSER_BASE}"])
        context = browser.new_context(base_url=BROWSER_BASE, permissions=["notifications"], service_workers="block")
        context.grant_permissions(["notifications"], origin=BROWSER_BASE)
        page_a = context.new_page()
        page_b = context.new_page()
        for page in (page_a, page_b):
            page.goto("/", wait_until="domcontentloaded")
            context.grant_permissions(["notifications"], origin=BROWSER_BASE)
        permission = page_a.evaluate("() => Notification.permission")
        if permission != "granted":
            print(json.dumps({"status":"unreached", "reason":"headless Notification permission is not granted", "permission":permission}))
            return 2
        pages = (page_a, page_b)
        for page in pages:
            _observe_fallback_notifications(page)
            _listen(page)
        _emit(page_a, "stream-6673:1")
        page_a.wait_for_function("() => window.__issue6673DirectCount + window.__issue6673RegistrationShowCount >= 1", timeout=10000)
        fallback_before_no_id = _fallback_count(pages)
        _emit(page_b, "stream-6673:1")
        page_b.wait_for_timeout(2500)
        if _fallback_count(pages) != fallback_before_no_id:
            raise AssertionError({"same_event_page_owner": _fallback_count(pages), "before": fallback_before_no_id})
        _emit(page_a, "stream-6673:2")
        page_a.wait_for_function("count => window.__issue6673DirectCount + window.__issue6673RegistrationShowCount > count", arg=fallback_before_no_id, timeout=10000)
        next_count = _fallback_count(pages)
        if next_count != fallback_before_no_id + 1:
            raise AssertionError({"next_event_page_owner": next_count, "before": fallback_before_no_id})
        _emit(page_a, None)
        page_a.wait_for_function("count => window.__issue6673DirectCount + window.__issue6673RegistrationShowCount > count", arg=next_count, timeout=10000)
        if page_a.evaluate("() => window.__issue6673IdentityObservations.at(-1)") is not None:
            raise AssertionError(page_a.evaluate("() => window.__issue6673IdentityObservations"))
        context.close()
        context = browser.new_context(base_url=BROWSER_BASE, permissions=["notifications"])
        context.grant_permissions(["notifications"], origin=BROWSER_BASE)
        page_a = context.new_page()
        page_b = context.new_page()
        for page in (page_a, page_b):
            page.goto("/", wait_until="domcontentloaded")
            page.wait_for_function("async () => Boolean((await navigator.serviceWorker.ready).active && navigator.serviceWorker.controller)", timeout=15000)
            _observe_fallback_notifications(page)
            _listen(page)
        _emit(page_a, "stream-6673:3")
        _emit(page_b, "stream-6673:3")
        page_a.wait_for_function("async () => (await (await navigator.serviceWorker.ready).getNotifications({tag:'hermes-session-6673'})).some(n => n.data && n.data.eventId === 'stream-6673:3')", timeout=10000)
        same_event_records = _records(page_a)
        if [record["id"] for record in same_event_records if record.get("id") == "stream-6673:3"] != ["stream-6673:3"]:
            raise AssertionError(same_event_records)
        _emit(page_a, "stream-6673:4")
        page_a.wait_for_function("async () => (await (await navigator.serviceWorker.ready).getNotifications({tag:'hermes-session-6673'})).some(n => n.data && n.data.eventId === 'stream-6673:4')", timeout=10000)
        page_a.wait_for_timeout(1000)
        records = _records(page_a)
        source = page_a.evaluate("""async () => await (await fetch('/static/messages.js')).text()""")
        if "_NOTIFICATION_OWNER_STORE='event-identities'" not in source:
            raise AssertionError("served notification path is missing the page owner store")
        if not any(record["id"] == "stream-6673:4" and record["renotify"] for record in records):
            raise AssertionError(records)
        if not all(record["url"].startswith(BROWSER_BASE + "/") for record in records):
            raise AssertionError(records)
        print(json.dumps({"status":"passed", "permission":permission, "records":records}))
        return 0
    except Exception as error:
        print(f"REALITY GATE FAILED: {error}", file=sys.stderr)
        return 1
    finally:
        if browser is not None: browser.close()
        if playwright is not None: playwright.stop()
        if process is not None:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
            log = getattr(process, "_hermes_log", None)
            if log is not None: log.close()
        temp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
