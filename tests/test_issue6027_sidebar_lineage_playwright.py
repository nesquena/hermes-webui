"""Served production-path proof for the issue-shaped sidebar fixture."""

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

ROOT = Path(__file__).parents[1]
PORT = 8799


def _healthy():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def test_served_sidebar_lineage_fixture_has_stable_grouping(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    env = os.environ.copy()
    env.update({
        "BROWSER": "echo",
        "HERMES_WEBUI_PORT": str(PORT),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_HOME": str(state),
        "HERMES_WEBUI_STATE_DIR": str(state),
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": str(state / "no-agent"),
    })
    log_path = state / "server.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    try:
        deadline = time.time() + 30
        while time.time() < deadline and not _healthy():
            time.sleep(0.25)
        if not _healthy():
            pytest.fail(f"isolated server did not become healthy; log={log_path}")
        errors = []
        with sync_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            page = browser.new_page(viewport={"width": 1024, "height": 600}, device_scale_factor=2)
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{PORT}/#sessions", wait_until="domcontentloaded")
            page.wait_for_selector("#sessionList, body", timeout=10000)
            page.screenshot(path=str(tmp_path / "6027-before.png"), full_page=True)
            result = page.evaluate("""
              () => {
                const root = {session_id:'root', title:'Renamed root', profile_scope:'work',
                  project_id:'projA', message_count:1, updated_at:30};
                const delegate = {session_id:'delegate', title:'Delegated child', profile_scope:'work',
                  relationship_type:'child_session', read_only:true, parent_session_id:'root', message_count:1};
                const fork = {session_id:'fork', title:'Writable fork', profile_scope:'work',
                  session_source:'fork', relationship_type:'child_session', parent_session_id:'root',
                  project_id:null, message_count:1};
                _allSessions = [root, delegate, fork];
                _sidebarReferenceSessions = [];
                renderSessionListFromCache();
                const rows = [...document.querySelectorAll('#sessionList .session-item')];
                return {rows: rows.map(row => row.textContent), count: rows.length,
                  hasError: !!document.querySelector('.session-load-error')};
              }
            """)
            page.screenshot(path=str(tmp_path / "6027-after.png"), full_page=True)
            browser.close()
        assert not errors, errors
        assert result["count"] == 2
        assert any("Renamed root" in row for row in result["rows"])
        assert any("Writable fork" in row for row in result["rows"])
        assert not result["hasError"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
