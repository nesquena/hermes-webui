#!/usr/bin/env python3
"""Real-Chromium viewport check and optional screenshot capture for session squash.

The check boots the production server against disposable state, creates and
archives a real WebUI session through the browser API, then verifies the squash
control and confirmation dialog at desktop, narrow, and phone widths.

By default it checks the current tree without retaining screenshots. Maintainers
can compare a pre-feature tree with the current tree by pointing --server-root at
each checkout and selecting --expect before/after.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


VIEWPORTS = (
    ("desktop", 1440, 900, False),
    ("narrow", 768, 900, True),
    ("mobile", 390, 844, True),
)
BENIGN_CONSOLE = (
    "favicon",
    "manifest.json",
    "serviceworker",
    "sw.js",
    "the server responded with a status of 404",
)


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--server-root",
        type=Path,
        default=repo_root,
        help="checkout whose server/static files are exercised",
    )
    parser.add_argument(
        "--expect",
        choices=("before", "after"),
        default="after",
        help="whether squash controls must be absent or present",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="optional directory for viewport PNGs and results.json",
    )
    return parser.parse_args()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(base_url: str, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited before health check (exit {proc.returncode})")
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    raise RuntimeError("server did not become healthy in 30 seconds")


def _is_benign_console(text: str) -> bool:
    lowered = text.lower()
    return any(fragment in lowered for fragment in BENIGN_CONSOLE)


def _git_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _terminate(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _create_archived_session(page) -> str:
    return page.evaluate(
        """async () => {
          if (typeof newSession !== 'function' || typeof api !== 'function') {
            throw new Error('WebUI session APIs did not initialize');
          }
          await newSession(false, {worktree: false});
          const sid = S.session && S.session.session_id;
          if (!sid) throw new Error('new session has no id');
          const activeSession = {...S.session};
          const activeMessages = [...(S.messages || [])];
          const archived = await api('/api/session/archive', {
            method: 'POST',
            body: JSON.stringify({session_id: sid, archived: true}),
          });
          // The archive event removes the row from the default sidebar. Restore
          // the exact archived selection to model opening that row from Sessions.
          await new Promise(resolve => setTimeout(resolve, 500));
          S.session = {...activeSession, ...(archived.session || {}), archived: true};
          S.messages = activeMessages;
          syncTopbar();
          renderMessages();
          return sid;
        }"""
    )


def _layout_snapshot(page, selector: str | None) -> dict:
    return page.evaluate(
        """selector => {
          const node = selector ? document.querySelector(selector) : null;
          const rect = node ? node.getBoundingClientRect() : null;
          const style = node ? getComputedStyle(node) : null;
          return {
            viewport_width: window.innerWidth,
            document_width: document.documentElement.scrollWidth,
            horizontal_overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
            target: rect ? {
              left: Math.round(rect.left), top: Math.round(rect.top),
              right: Math.round(rect.right), bottom: Math.round(rect.bottom),
              width: Math.round(rect.width), height: Math.round(rect.height),
              display: style.display, visibility: style.visibility,
            } : null,
          };
        }""",
        selector,
    )


def _run_viewport(browser, base_url: str, expected: str, artifact_dir: Path | None, spec: tuple) -> dict:
    name, width, height, compact = spec
    context = browser.new_context(
        base_url=base_url,
        viewport={"width": width, "height": height},
        color_scheme="dark",
        reduced_motion="reduce",
        bypass_csp=True,
    )
    page = context.new_page()
    console_errors: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error" and not _is_benign_console(message.text)
        else None,
    )
    page.on("pageerror", lambda error: console_errors.append(str(error)))

    try:
        page.goto("/", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_function("() => typeof S !== 'undefined' && typeof newSession === 'function'", timeout=20_000)
        page.wait_for_selector(".composer-footer", state="visible", timeout=20_000)
        session_id = _create_archived_session(page)
        page.wait_for_timeout(250)

        desktop_count = page.locator("#btnSquash").count()
        mobile_count = page.locator("#composerMobileSquashBtn").count()
        target = None
        if expected == "before":
            assert desktop_count == 0, "pre-feature desktop squash control unexpectedly exists"
            assert mobile_count == 0, "pre-feature mobile squash control unexpectedly exists"
            selector = None
            dialog_visible = False
            if compact:
                toggle = page.locator("#composerMobileConfigBtn")
                assert toggle.is_visible(), f"{name}: compact composer menu toggle is hidden"
                toggle.click()
                page.wait_for_selector("#composerMobileConfigPanel.open", state="visible")
        else:
            assert desktop_count == 1, f"{name}: desktop squash control missing"
            assert mobile_count == 1, f"{name}: mobile squash control missing"
            if compact:
                toggle = page.locator("#composerMobileConfigBtn")
                assert toggle.is_visible(), f"{name}: compact composer menu toggle is hidden"
                toggle.click()
                page.wait_for_selector("#composerMobileConfigPanel.open", state="visible")
                target = page.locator("#composerMobileSquashBtn")
                selector = "#composerMobileSquashBtn"
                assert target.is_visible(), f"{name}: mobile squash action is hidden"
                box = target.bounding_box()
                assert box and box["height"] >= 44, f"{name}: mobile action is below 44px touch height"
            else:
                target = page.locator("#btnSquash")
                selector = "#btnSquash"
                assert target.is_visible(), "desktop squash control is hidden"
                target.hover()
                page.wait_for_timeout(150)

        layout = _layout_snapshot(page, selector)
        if layout["target"]:
            target_box = layout["target"]
            assert target_box["left"] >= 0 and target_box["right"] <= width + 1, (
                f"{name}: squash action is outside the viewport"
            )

        screenshot_name = f"{expected}-{name}.png"
        if artifact_dir:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifact_dir / screenshot_name), animations="disabled")

        if expected == "after":
            assert target is not None
            target.click()
            page.wait_for_selector("#appDialogOverlay", state="visible", timeout=10_000)
            page.wait_for_function(
                "() => document.querySelector('#appDialogTitle').textContent.trim() === 'Squash conversation'",
                timeout=10_000,
            )
            dialog_visible = page.locator("#appDialog").is_visible()
            assert dialog_visible, f"{name}: squash confirmation dialog did not open"
            assert page.evaluate("document.activeElement && document.activeElement.id") == "appDialogCancel", (
                f"{name}: safe cancel action did not receive initial focus"
            )
            page.locator("#appDialogCancel").click()
        else:
            dialog_visible = False

        assert not console_errors, f"{name}: browser errors: {console_errors}"
        return {
            "name": name,
            "viewport": [width, height],
            "session_id_present": bool(session_id),
            "desktop_control_count": desktop_count,
            "mobile_control_count": mobile_count,
            "compact_menu_opened": compact,
            "dialog_opened": dialog_visible,
            "layout": layout,
            "console_errors": console_errors,
            "screenshot": screenshot_name if artifact_dir else None,
        }
    finally:
        context.close()


def main() -> int:
    args = _parse_args()
    root = args.server_root.expanduser().resolve()
    server_py = root / "server.py"
    if not server_py.exists():
        print(f"SETUP FAIL: server.py not found under {root}", file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError:
        print("SETUP FAIL: playwright is not installed", file=sys.stderr)
        return 2

    state_root = Path(tempfile.mkdtemp(prefix="hermes-squash-visual-"))
    workspace = state_root / "workspace"
    workspace.mkdir()
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY"):
            env.pop(key, None)
    env.update(
        {
            "HERMES_WEBUI_PORT": str(port),
            "HERMES_WEBUI_HOST": "127.0.0.1",
            "HERMES_WEBUI_STATE_DIR": str(state_root / "webui-state"),
            "HERMES_HOME": str(state_root / "hermes-home"),
            "HERMES_BASE_HOME": str(state_root / "hermes-home"),
            "HERMES_CONFIG_PATH": str(state_root / "hermes-home" / "config.yaml"),
            "HERMES_WEBUI_DEFAULT_WORKSPACE": str(workspace),
            "HERMES_WEBUI_SKIP_ONBOARDING": "1",
            "HERMES_WEBUI_AGENT_DIR": str(state_root / "no-agent"),
        }
    )

    log_path = state_root / "server.log"
    proc: subprocess.Popen | None = None
    results: dict = {
        "expect": args.expect,
        "source_sha": _git_sha(root),
        "server_root_name": root.name,
        "viewports": [],
    }
    try:
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [sys.executable, str(server_py)],
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            _wait_for_health(base_url, proc)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                results["browser"] = f"Chromium {browser.version}"
                try:
                    for viewport in VIEWPORTS:
                        observation = _run_viewport(
                            browser,
                            base_url,
                            args.expect,
                            args.artifact_dir,
                            viewport,
                        )
                        results["viewports"].append(observation)
                        print(
                            f"OK  {observation['name']} {observation['viewport'][0]}x"
                            f"{observation['viewport'][1]} — squash UI {args.expect} contract"
                        )
                finally:
                    browser.close()
        results["passed"] = True
    except Exception as exc:
        results["passed"] = False
        results["error"] = str(exc)
        print(f"SQUASH VISUAL CHECK FAILED: {exc}", file=sys.stderr)
        try:
            print(log_path.read_text(encoding="utf-8")[-3000:], file=sys.stderr)
        except OSError:
            pass
    finally:
        _terminate(proc)
        if args.artifact_dir:
            args.artifact_dir.mkdir(parents=True, exist_ok=True)
            (args.artifact_dir / "results.json").write_text(
                json.dumps(results, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    if not results.get("passed"):
        return 1
    print("SQUASH VISUAL CHECK PASSED — desktop, narrow, and mobile states verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
