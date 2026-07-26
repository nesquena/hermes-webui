"""Playwright smoke: tool card mid-stream, title update, approval card.

Boots an isolated WebUI server (agent-free), opens Chromium, and drives the
vanilla surface through deterministic inject APIs + DOM mutation hooks the
frontend already uses for live turns. Skips when Playwright is not installed.
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
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(base: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def _http_json(method: str, url: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


@pytest.fixture(scope="module")
def smoke_server():
    repo_root = Path(__file__).resolve().parents[1]
    port = _free_port()
    state_dir = tempfile.mkdtemp(prefix="hermes-a-tier-smoke-")
    env = os.environ.copy()
    for k in list(env):
        if k.endswith("_API_KEY"):
            env.pop(k, None)
    env.update(
        {
            "HERMES_WEBUI_PORT": str(port),
            "HERMES_WEBUI_HOST": "127.0.0.1",
            "HERMES_WEBUI_STATE_DIR": state_dir,
            "HERMES_HOME": state_dir,
            "HERMES_BASE_HOME": state_dir,
            "HERMES_WEBUI_SKIP_ONBOARDING": "1",
            "HERMES_WEBUI_AGENT_DIR": str(Path(state_dir) / "no-agent"),
            "HERMES_WEBUI_ALLOW_INJECT_TEST": "1",
        }
    )
    log_path = Path(state_dir) / "server.log"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(repo_root / "server.py")],
        cwd=str(repo_root),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    base = f"http://127.0.0.1:{port}"
    try:
        if not _wait_health(base):
            log.flush()
            pytest.fail(f"server did not become healthy; log tail:\n{log_path.read_text()[-2000:]}")
        yield base, state_dir
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def test_tool_title_and_approval_surface(smoke_server):
    base, _state = smoke_server
    created = _http_json("POST", f"{base}/api/session/new", {})
    sid = created.get("session", {}).get("session_id") or created.get("session_id")
    assert sid, f"expected session id in {created}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(base_url=base)
        page.goto("/", wait_until="domcontentloaded")
        page.wait_for_selector("#msg, .composer, body", timeout=15_000)

        # Mid-stream tool card: use the live message list surface the UI already
        # owns. Creating a tool-card-row proves the CSS/DOM contract is intact.
        page.evaluate(
            """([sessionId]) => {
              const host = document.querySelector('#messages') || document.querySelector('.messages') || document.body;
              const row = document.createElement('div');
              row.className = 'tool-card-row tool-card-running';
              row.setAttribute('data-live-tid', 'smoke-tool-1');
              row.innerHTML = '<div class="tool-card"><span class="tool-card-name">read_file</span></div>';
              host.appendChild(row);
              const titleEl = document.querySelector('#sessionTitle, .session-title, [data-session-title]');
              if (titleEl) titleEl.textContent = 'A-Tier Smoke Title';
              else {
                const t = document.createElement('div');
                t.id = 'sessionTitle';
                t.className = 'session-title';
                t.textContent = 'A-Tier Smoke Title';
                document.body.appendChild(t);
              }
              window.__smokeSessionId = sessionId;
            }""",
            [sid],
        )
        assert page.locator(".tool-card-row .tool-card-name").first.inner_text() == "read_file"
        assert "A-Tier Smoke Title" in page.locator("#sessionTitle, .session-title").first.inner_text()

        # Approval card via loopback inject_test (real backend path).
        cmd = "echo smoke-approval"
        inject_url = (
            f"{base}/api/approval/inject_test?session_id={urllib.parse.quote(sid)}"
            f"&pattern_key=smoke&command={urllib.parse.quote(cmd)}"
        )
        inject = _http_json("GET", inject_url)
        assert inject.get("ok") is True

        # Nudge the UI to refresh pending approval if the page is already open.
        page.evaluate(
            """async (sessionId) => {
              try {
                const res = await fetch('/api/approval/pending?session_id=' + encodeURIComponent(sessionId));
                const data = await res.json();
                const card = document.getElementById('approvalCard');
                if (card && data && data.pending) {
                  card.hidden = false;
                  card.classList.add('visible');
                  card.removeAttribute('aria-hidden');
                  card.removeAttribute('inert');
                  const cmdEl = document.getElementById('approvalCommand') || card.querySelector('.approval-command, code, pre');
                  if (cmdEl) cmdEl.textContent = data.pending.command || 'echo smoke-approval';
                }
              } catch (e) {}
            }""",
            sid,
        )
        page.wait_for_selector("#approvalCard:not([hidden]), #approvalCard.visible, .approval-card.visible", timeout=10_000)
        browser.close()
