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
        const payload = JSON.stringify({description:'Approve the tool call.', session_id:'session-6673'});
        if (eventId === null) window.__issue6673Source.emit('approval', payload);
        else window.__issue6673Source.emit('approval', payload, eventId);
      }
    """, event_id)


def _records(page):
    return page.evaluate("""async () => (await (await navigator.serviceWorker.ready).getNotifications({tag:'hermes-session-6673'})).map(n => ({id:n.data.eventId, tag:n.tag, renotify:n.renotify, url:n.data.url}))""")


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
        S.session = {...(S.session || {}), session_id:'session-6673'};
        attachLiveStream('session-6673', 'stream-6673');
      }
    """)
    page.wait_for_function("() => Boolean(window.__issue6673Source?.listeners?.has('approval'))", timeout=15000)


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
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", f"--unsafely-treat-insecure-origin-as-secure={BASE}"])
        context = browser.new_context(base_url=BASE, permissions=["notifications"])
        context.grant_permissions(["notifications"], origin=BASE)
        legacy = (root / "tests" / "fixtures" / "issue6673_legacy_sw.js").read_text(encoding="utf-8")
        context.route("**/sw.js", lambda route: route.fulfill(status=200, content_type="application/javascript", body=legacy))
        page_a = context.new_page()
        page_b = context.new_page()
        for page in (page_a, page_b):
            page.goto("/", wait_until="domcontentloaded")
            page.wait_for_function("async () => Boolean((await navigator.serviceWorker.ready).active && navigator.serviceWorker.controller)", timeout=15000)
        permission = page_a.evaluate("() => Notification.permission")
        if permission != "granted":
            print(json.dumps({"status":"unreached", "reason":"headless Notification permission is not granted", "permission":permission}))
            return 2
        _listen(page_a)
        _emit(page_a, "stream-6673:1")
        page_a.wait_for_function("async () => (await (await navigator.serviceWorker.ready).getNotifications({tag:'hermes-session-6673'})).some(n => n.data && n.data.eventId === 'stream-6673:1')", timeout=10000)
        _emit(page_a, None)
        page_a.wait_for_timeout(250)
        if any(record.get("id") for record in _records(page_a)):
            raise AssertionError("journal-less event reused the previous EventSource identity")
        context.unroute("**/sw.js")
        page_a.evaluate("""
          () => {
            window.__issue6673UpgradeStates = [];
            window.__issue6673UpgradeDone = false;
            const start = async () => {
              const registration = await navigator.serviceWorker.getRegistration('/');
              registration.addEventListener('updatefound', () => {
                window.__issue6673UpgradeStates.push('updatefound');
                const worker = registration.installing;
                if (!worker) return;
                window.__issue6673UpgradeStates.push(worker.state);
                worker.addEventListener('statechange', () => window.__issue6673UpgradeStates.push(worker.state));
              });
              await registration.update();
              window.__issue6673UpgradeDone = true;
            };
            window.__issue6673UpgradePromise = start();
          }
        """)
        _emit(page_a, "stream-6673:2")
        page_a.wait_for_function("() => window.__issue6673UpgradeDone", timeout=20000)
        page_a.wait_for_function("async () => (await (await navigator.serviceWorker.ready).getNotifications({tag:'hermes-session-6673'})).length > 0", timeout=10000)
        page_a.reload(wait_until="domcontentloaded")
        page_b.reload(wait_until="domcontentloaded")
        for page in (page_a, page_b):
            page.wait_for_function("async () => Boolean((await navigator.serviceWorker.ready).active && navigator.serviceWorker.controller)", timeout=15000)
            _listen(page)
        _emit(page_a, "stream-6673:3")
        _emit(page_b, "stream-6673:3")
        page_a.wait_for_timeout(500)
        _emit(page_a, "stream-6673:4")
        page_a.wait_for_timeout(500)
        records = _records(page_a)
        source = page_a.evaluate("""async () => await (await fetch('/static/messages.js')).text()""")
        if "indexedDB.open(" in source:
            raise AssertionError("served notification path still contains indexedDB.open(")
        if not any(record["id"] == "stream-6673:4" and record["renotify"] for record in records):
            raise AssertionError(records)
        if "activated" not in page_a.evaluate("() => window.__issue6673UpgradeStates"):
            raise AssertionError(page_a.evaluate("() => window.__issue6673UpgradeStates"))
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
