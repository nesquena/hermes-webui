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


def _start_claim(page, stream_id: str, event_id: str, notification_url: str | None = None) -> None:
    page.evaluate(
        """
        ({streamId, lastEventId, notificationUrl}) => {
          window.__issue6673Claim = (async () => {
            const registration = await navigator.serviceWorker.ready;
            if (!registration.active) throw new Error('service worker is not active');
            const channel = new MessageChannel();
            const response = new Promise((resolve, reject) => {
              const timeout = setTimeout(() => reject(new Error('claim response timeout')), 5000);
              channel.port1.onmessage = event => {
                clearTimeout(timeout);
                resolve(event.data);
              };
              channel.port1.start();
            });
            registration.active.postMessage({
              type: 'hermes.notification.claim',
              title: 'Response complete',
              identity: {streamId, lastEventId},
              options: {
                body: 'The task finished.',
                tag: 'hermes-session-6673',
                renotify: true,
                icon: 'static/favicon-192.png',
                badge: 'static/favicon-32.png',
                data: {url: notificationUrl || (location.origin + '/?session=session-6673')},
              },
            }, [channel.port2]);
            return await response;
          })();
        }
        """,
        {"streamId": stream_id, "lastEventId": event_id, "notificationUrl": notification_url},
    )


def _await_claim(page) -> dict:
    return page.evaluate("async () => await window.__issue6673Claim")


def _claim(page, stream_id: str, event_id: str, notification_url: str | None = None) -> dict:
    _start_claim(page, stream_id, event_id, notification_url)
    return _await_claim(page)


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

        _start_claim(page_a, "stream-6673", "stream-6673:1")
        _start_claim(page_b, "stream-6673", "stream-6673:1")
        first_a = _await_claim(page_a)
        first_b = _await_claim(page_b)
        _start_claim(page_a, "stream-6673", "stream-6673:2")
        _start_claim(page_b, "stream-6673", "stream-6673:2")
        second_a = _await_claim(page_a)
        second_b = _await_claim(page_b)
        page_a.reload(wait_until="domcontentloaded")
        page_a.wait_for_function(
            """async () => Boolean((await navigator.serviceWorker.ready).active)""",
            timeout=15000,
        )
        replay = _claim(page_a, "stream-6673", "stream-6673:1")
        valid_url = _claim(page_a, "stream-6673", "stream-6673:3", "/?session=session-6673")
        cross_origin_url = _claim(page_a, "stream-6673", "stream-6673:4", "https://evil.test/")
        count = _notification_count(page_a)
        urls = _notification_urls(page_a)
        permission = page_a.evaluate("() => Notification.permission")
        statuses = [first_a["status"], first_b["status"], second_a["status"], second_b["status"], replay["status"]]
        owner_statuses = {"shown", "fallback-owner"}
        if sorted(statuses[:2].count(status) for status in owner_statuses) != [0, 1] or statuses[:2].count("duplicate") != 1:
            raise AssertionError(statuses)
        if sorted(statuses[2:4].count(status) for status in owner_statuses) != [0, 1] or statuses[2:4].count("duplicate") != 1:
            raise AssertionError(statuses)
        if replay["status"] != "duplicate" or valid_url["status"] not in owner_statuses or cross_origin_url["status"] != "invalid":
            raise AssertionError({"statuses": statuses, "validUrl": valid_url, "crossOriginUrl": cross_origin_url})
        expected_count = 1 if "shown" in statuses or valid_url["status"] == "shown" else 0
        expected_urls = [f"{BASE}/?session=session-6673"] if expected_count else []
        if count != expected_count or urls != expected_urls:
            raise AssertionError({"notificationCount": count, "urls": urls})
        print({"scope": metadata["scope"], "scriptURL": metadata["scriptURL"], "permission": permission, "statuses": statuses, "validUrl": valid_url, "crossOriginUrl": cross_origin_url, "notificationCount": count, "urls": urls})
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
