#!/usr/bin/env python3
"""Rendered responsive proof for nested-subagent sidebar search results.

Boots an isolated real WebUI server and drives the shipped search/render path in
Chromium at desktop, narrow, and mobile viewports. The fixture includes both the
same-source and cross-surface nested-child forms whose parent does not match.

Run:
  python tests/browser_sidebar_nested_subagent_responsive.py

Set RESPONSIVE_SCREENSHOT_DIR to save deterministic sidebar screenshots.
"""

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


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_IDS = ["nested_subagent_child", "cross_surface_child"]
VIEWPORTS = [
    ("desktop", 1440, 900, False),
    ("narrow", 768, 900, False),
    ("mobile", 390, 844, True),
]
SCREENSHOT_DIR = os.environ.get("RESPONSIVE_SCREENSHOT_DIR")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(base_url: str, proc: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with code {proc.returncode}")
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    raise RuntimeError("server did not become healthy within 30 seconds")


def _isolated_env(port: int, state_dir: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY") or key.startswith("HERMES_WEBUI_OIDC_"):
            env.pop(key, None)
    env.update(
        {
            "HERMES_WEBUI_PORT": str(port),
            "HERMES_WEBUI_HOST": "127.0.0.1",
            "HERMES_WEBUI_STATE_DIR": state_dir,
            "HERMES_HOME": state_dir,
            "HERMES_BASE_HOME": state_dir,
            "HERMES_WEBUI_SKIP_ONBOARDING": "1",
            "HERMES_WEBUI_PASSWORD": "",
            "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
        }
    )
    return env


def _render_search_fixture(page) -> None:
    page.evaluate(
        """() => {
            const now = Date.now() / 1000;
            const parent = {
              session_id:'subagent_parent', title:'Unrelated parent', raw_source:'subagent',
              source_tag:'subagent', session_source:'other', message_count:12, updated_at:now-10
            };
            const sameSourceChild = {
              session_id:'nested_subagent_child', title:'matching_nested_same_source',
              parent_session_id:'subagent_parent', relationship_type:'child_session', raw_source:'subagent',
              source_tag:'subagent', session_source:'other', source_label:'Subagent', parent_source:'subagent',
              message_count:46, updated_at:now
            };
            const crossSurfaceChild = {
              ...sameSourceChild, session_id:'cross_surface_child', title:'matching_nested_cross_surface',
              raw_source:'webui', source_tag:'webui', session_source:'webui',
              _cross_surface_child_session:true, updated_at:now-1
            };
            _allSessions = [parent, sameSourceChild, crossSurfaceChild];
            _sidebarReferenceSessions = [];
            _contentSearchResults = [];
            _sessionSourceFilter = 'webui';
            _activeProject = null;
            _showArchived = false;
            _serverWebuiSessionCount = null;
            _serverCliSessionCount = null;
            const input = document.getElementById('sessionSearch');
            input.value = 'matching_nested';
            syncSessionSearchClear();
            renderSessionListFromCache();
        }"""
    )


def _measure(page) -> dict:
    return page.evaluate(
        """() => {
            const rect = el => {
              const r = el.getBoundingClientRect();
              return {x:r.x, y:r.y, width:r.width, height:r.height, right:r.right, bottom:r.bottom};
            };
            const sidebar = document.querySelector('.sidebar');
            const list = document.querySelector('#sessionList');
            const search = document.querySelector('#sessionSearch');
            const rows = [...list.querySelectorAll('.session-item[data-sid]')];
            return {
              ids: rows.map(row => row.dataset.sid),
              sidebar: rect(sidebar),
              list: {...rect(list), clientWidth:list.clientWidth, scrollWidth:list.scrollWidth},
              search: rect(search),
              rows: rows.map(row => ({...rect(row), sid:row.dataset.sid, clientWidth:row.clientWidth,
                scrollWidth:row.scrollWidth, text:row.innerText.trim()})),
              bodyScrollWidth:document.body.scrollWidth,
              viewport:{width:innerWidth, height:innerHeight},
              mobileOpen:sidebar.classList.contains('mobile-open'),
            };
        }"""
    )


def _assert_geometry(state: str, data: dict, mobile: bool) -> None:
    assert data["ids"] == EXPECTED_IDS, f"{state}: rendered {data['ids']}, expected {EXPECTED_IDS}"
    assert data["list"]["scrollWidth"] <= data["list"]["clientWidth"], f"{state}: session list overflows horizontally"
    assert data["search"]["x"] >= data["sidebar"]["x"], f"{state}: search escapes sidebar left edge"
    assert data["search"]["right"] <= data["sidebar"]["right"], f"{state}: search escapes sidebar right edge"
    for row in data["rows"]:
        assert row["x"] >= data["list"]["x"], f"{state}: {row['sid']} escapes list left edge"
        assert row["right"] <= data["list"]["right"] + 1, f"{state}: {row['sid']} escapes list right edge"
        assert row["scrollWidth"] <= row["clientWidth"], f"{state}: {row['sid']} overflows horizontally"
        if mobile:
            assert row["height"] >= 44, f"{state}: {row['sid']} touch target is under 44px"
    if mobile:
        assert data["mobileOpen"], "mobile: sidebar drawer did not open"
        assert abs(data["sidebar"]["x"]) <= 1, f"mobile: drawer is off-screen at x={data['sidebar']['x']}"
        assert data["bodyScrollWidth"] == data["viewport"]["width"], "mobile: page has horizontal overflow"


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SETUP FAIL: playwright is not installed", file=sys.stderr)
        return 2

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="hermes-sidebar-responsive-") as state_dir:
        log_path = Path(state_dir) / "server.log"
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "server.py")],
                cwd=ROOT,
                env=_isolated_env(port, state_dir),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        try:
            _wait_for_health(base_url, proc)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                try:
                    for state, width, height, mobile in VIEWPORTS:
                        context = browser.new_context(viewport={"width": width, "height": height})
                        page = context.new_page()
                        page_errors = []
                        page.on("pageerror", lambda error: page_errors.append(str(error)))
                        page.goto(base_url + "/", wait_until="domcontentloaded")
                        page.wait_for_selector("#sessionSearch", timeout=15000)
                        page.wait_for_function("typeof renderSessionListFromCache === 'function'")
                        page.wait_for_timeout(1200)
                        if mobile:
                            sidebar_before = page.locator(".sidebar").bounding_box()
                            assert sidebar_before and sidebar_before["x"] < 0, "mobile: drawer should start closed"
                            page.locator("#btnHamburger").click()
                            page.wait_for_selector(".sidebar.mobile-open")
                            page.wait_for_timeout(300)
                        _render_search_fixture(page)
                        data = _measure(page)
                        _assert_geometry(state, data, mobile)
                        assert not page_errors, f"{state}: uncaught browser errors: {page_errors}"
                        if SCREENSHOT_DIR:
                            output = Path(SCREENSHOT_DIR)
                            output.mkdir(parents=True, exist_ok=True)
                            page.locator(".sidebar").screenshot(path=str(output / f"pr7759-{state}-after.png"))
                        print(
                            f"OK  {state} {width}x{height} — rows={json.dumps(data['ids'])}, "
                            f"list={data['list']['clientWidth']}px, overflow=0"
                        )
                        context.close()
                finally:
                    browser.close()
            print("RESPONSIVE SIDEBAR SEARCH PASSED — desktop, narrow, and mobile")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
