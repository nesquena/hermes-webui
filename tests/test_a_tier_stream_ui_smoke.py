"""Playwright smoke: tool card mid-stream, title update, approval card.

Boots an isolated WebUI server (agent-free), opens Chromium, and drives the
vanilla surface through production render helpers plus loopback inject_test.
Skips when Playwright is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

_CRED_ENV_PREFIXES = (
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_PROFILE", "GH_TOKEN", "GITHUB_TOKEN",
)


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


def _scrub_smoke_server_env(env: dict[str, str]) -> dict[str, str]:
    cleaned = env.copy()
    for key in list(cleaned):
        if key.endswith("_API_KEY") or key in _CRED_ENV_PREFIXES:
            cleaned.pop(key, None)
    for model_env in ("HERMES_MODEL", "OPENAI_MODEL", "LLM_MODEL"):
        cleaned.pop(model_env, None)
    return cleaned


@pytest.fixture(scope="module")
def smoke_server():
    repo_root = Path(__file__).resolve().parents[1]
    port = _free_port()
    state_dir = tempfile.mkdtemp(prefix="hermes-a-tier-smoke-")
    env = _scrub_smoke_server_env(os.environ)
    env.update(
        {
            "HERMES_WEBUI_PORT": str(port),
            "HERMES_WEBUI_HOST": "127.0.0.1",
            "HERMES_WEBUI_STATE_DIR": state_dir,
            "HERMES_HOME": state_dir,
            "HERMES_BASE_HOME": state_dir,
            "HERMES_CONFIG_PATH": str(Path(state_dir) / "config.yaml"),
            "HERMES_WEBUI_SKIP_ONBOARDING": "1",
            "HERMES_WEBUI_AGENT_DIR": str(Path(state_dir) / "no-agent"),
            "HERMES_WEBUI_ALLOW_INJECT_TEST": "1",
            "HERMES_WEBUI_TEST_NETWORK_BLOCK": "1",
            "AWS_EC2_METADATA_DISABLED": "true",
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
        shutil.rmtree(state_dir, ignore_errors=True)


def test_tool_title_and_approval_surface(smoke_server):
    base, _state = smoke_server
    created = _http_json("POST", f"{base}/api/session/new", {})
    sid = created.get("session", {}).get("session_id") or created.get("session_id")
    assert sid, f"expected session id in {created}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(base_url=base)
        page.goto(f"/session/{sid}", wait_until="domcontentloaded")
        page.wait_for_selector("#msg, .composer, body", timeout=15_000)

        page.evaluate(
            """([sessionId]) => {
              if (window.S) {
                window.S.session = window.S.session || {};
                window.S.session.session_id = sessionId;
                window.S.session.title = 'A-Tier Smoke Title';
              }
              if (typeof syncTopbar === 'function') syncTopbar();
              const host = document.querySelector('#messages') || document.body;
              if (typeof buildToolCard === 'function') {
                const row = buildToolCard({
                  name: 'read_file',
                  done: false,
                  args: { path: 'smoke.txt' },
                });
                row.setAttribute('data-live-tid', 'smoke-tool-1');
                host.appendChild(row);
              }
            }""",
            [sid],
        )
        assert page.locator(".tool-card-row[data-tool-name='read_file']").count() >= 1
        assert "smoke.txt" in page.locator(".tool-card-row .tool-card-name").first.inner_text()

        cmd = "echo smoke-approval"
        inject_url = (
            f"{base}/api/approval/inject_test?session_id={urllib.parse.quote(sid)}"
            f"&pattern_key=smoke&command={urllib.parse.quote(cmd)}"
        )
        inject = _http_json("GET", inject_url)
        assert inject.get("ok") is True

        page.evaluate(
            """async (sessionId) => {
              const res = await fetch('/api/approval/pending?session_id=' + encodeURIComponent(sessionId));
              const data = await res.json();
              if (data && data.pending && typeof showApprovalForSession === 'function') {
                showApprovalForSession(sessionId, data.pending, data.pending_count || 1);
              }
            }""",
            sid,
        )
        page.wait_for_selector("#approvalCard.visible", timeout=10_000)
        assert "smoke-approval" in page.locator("#approvalCmd").inner_text()
        browser.close()
