#!/usr/bin/env python3
"""Served-build proof for the real #6673 service-worker claim owner."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

PORT = int(os.environ.get("ISSUE6673_BROWSER_PORT", "8797"))
BASE = f"http://127.0.0.1:{PORT}"


def _wait_for_health(timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    return False


def _serve(repo_root: Path, state_dir: Path) -> subprocess.Popen:
    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY"):
            env.pop(key, None)
    env.update(
        {
            "HERMES_WEBUI_PORT": str(PORT),
            "HERMES_WEBUI_HOST": "127.0.0.1",
            "HERMES_WEBUI_STATE_DIR": str(state_dir / "webui-state"),
            "HERMES_HOME": str(state_dir / "hermes-home"),
            "HERMES_BASE_HOME": str(state_dir / "hermes-home"),
            "HERMES_WEBUI_SKIP_ONBOARDING": "1",
            "HERMES_WEBUI_AGENT_DIR": str(state_dir / "no-agent"),
        }
    )
    log = (state_dir / "server.log").open("w", encoding="utf-8")
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    process = subprocess.Popen(
        [sys.executable, str(repo_root / "server.py")],
        cwd=repo_root,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        **kwargs,
    )
    process._hermes_log = log  # type: ignore[attr-defined]
    return process


def _notification_count(page) -> int:
    return page.evaluate(
        """
        async () => {
          const registration = await navigator.serviceWorker.ready;
          const notifications = await registration.getNotifications({tag: 'hermes-session-6673'});
          return notifications.length;
        }
        """
    )


def _notification_urls(page) -> list[str]:
    return page.evaluate(
        """
        async () => {
          const registration = await navigator.serviceWorker.ready;
          const notifications = await registration.getNotifications({tag: 'hermes-session-6673'});
          return notifications.map(notification => notification.data.url);
        }
        """
    )


def _start_live_listener(page, stream_id: str) -> None:
    page.evaluate(
        """
        streamId => {
          class BrowserBoundaryEventSource {
            constructor() {
              this.listeners = new Map(); this.readyState = 1;
              window.__issue6673Source = this;
            }
            addEventListener(name, handler) {
              const handlers = this.listeners.get(name) || [];
              handlers.push(handler); this.listeners.set(name, handlers);
            }
            close() { this.readyState = 2; }
            emit(name, data, lastEventId) {
              for (const handler of this.listeners.get(name) || []) {
                handler({type: name, data, lastEventId});
              }
            }
          }
          window.EventSource = BrowserBoundaryEventSource;
          window.__hermesSetBackgrounded(true);
          S.session = {...(S.session || {}), session_id: 'session-6673'};
          S.messages = Array.isArray(S.messages) ? S.messages : [];
          attachLiveStream('session-6673', streamId);
        }
        """,
        stream_id,
    )


def _emit_live_event(page, event_name: str, event_id: str) -> None:
    page.evaluate(
        """
        ({eventName, eventId}) => {
          window.__issue6673Source.emit(
            eventName,
            JSON.stringify({description: 'Approve the tool call.', session_id: 'session-6673'}),
            eventId,
          );
        }
        """,
        {"eventName": event_name, "eventId": event_id},
    )


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parents[1]
    state_tmp = tempfile.TemporaryDirectory(prefix="hermes-6673-browser-", ignore_cleanup_errors=True)
    state_dir = Path(state_tmp.name)
    process = None
    browser = None
    playwright = None
    try:
        process = _serve(repo_root, state_dir)
        if not _wait_for_health():
            log = state_dir / "server.log"
            print(f"SETUP FAIL: server did not become healthy\n{log.read_text(encoding='utf-8')[-2000:]}", file=sys.stderr)
            return 2

        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            base_url=BASE,
            permissions=["notifications"],
        )
        page_a = context.new_page()
        page_b = context.new_page()
        for page in (page_a, page_b):
            page.goto("/", wait_until="domcontentloaded")
            page.wait_for_function(
                """async () => {
                  const registration = await navigator.serviceWorker.ready;
                  return Boolean(registration.active);
                }""",
                timeout=15000,
            )

        metadata = page_a.evaluate(
            """async () => {
              const registration = await navigator.serviceWorker.ready;
              return {scope: registration.scope, scriptURL: registration.active.scriptURL};
            }"""
        )
        script_path = urlparse(metadata["scriptURL"]).path
        if not script_path.endswith("/sw.js"):
            raise AssertionError({**metadata, "scriptPath": script_path})
        page_a.evaluate(
            """() => new Promise((resolve, reject) => {
              const request = indexedDB.deleteDatabase('hermes-webui-notification-claims-v1');
              request.onsuccess = request.onerror = request.onblocked = () => resolve();
            })"""
        )

        _start_live_listener(page_a, "stream-6673")
        _start_live_listener(page_b, "stream-6673")
        _emit_live_event(page_a, "approval", "stream-6673:1")
        _emit_live_event(page_b, "approval", "stream-6673:1")
        _emit_live_event(page_a, "approval", "stream-6673:2")
        _emit_live_event(page_b, "approval", "stream-6673:2")
        page_a.wait_for_timeout(250)
        count_before_reload = _notification_count(page_a)
        urls_before_reload = _notification_urls(page_a)
        page_a.reload(wait_until="domcontentloaded")
        page_a.wait_for_function(
            """async () => Boolean((await navigator.serviceWorker.ready).active)""",
            timeout=15000,
        )
        _start_live_listener(page_a, "stream-6673")
        _emit_live_event(page_a, "approval", "stream-6673:1")
        page_a.wait_for_timeout(250)
        count = _notification_count(page_a)
        urls = _notification_urls(page_a)
        permission = page_a.evaluate("() => Notification.permission")
        expected_urls = [f"{BASE}/?session=session-6673"]
        if permission == "granted" and (
            count_before_reload != 1
            or urls_before_reload != expected_urls
            or count != count_before_reload
            or urls != urls_before_reload
        ):
            raise AssertionError({
                "beforeReload": [count_before_reload, urls_before_reload],
                "afterReplay": [count, urls],
                "permission": permission,
            })
        print({"scope": metadata["scope"], "scriptURL": metadata["scriptURL"], "permission": permission, "notificationCount": count, "urls": urls})
        return 0
    except Exception as error:
        print(f"REALITY GATE FAILED: {error}", file=sys.stderr)
        return 1
    finally:
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
        state_tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
