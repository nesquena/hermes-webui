import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import pytest


playwright = pytest.importorskip("playwright.sync_api")

REPO = Path(__file__).resolve().parent.parent


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(url):
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    raise AssertionError("render server did not become healthy")


def test_attachment_choice_is_visible_across_mobile_render_matrix(tmp_path):
    port = _free_port()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY"):
            env.pop(key, None)
    env.update({
        "HERMES_WEBUI_PORT": str(port),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": str(state_dir),
        "HERMES_HOME": str(state_dir),
        "HERMES_BASE_HOME": str(state_dir),
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": str(state_dir / "no-agent"),
        "BROWSER": "echo",
    })
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "server.py")],
        cwd=REPO,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    try:
        _wait_for_health(f"http://127.0.0.1:{port}/health")
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            for width in (320, 400, 480, 768, 1280):
                for locale in ("en", "ru", "de"):
                    context = browser.new_context(
                        viewport={"width": width, "height": 320},
                        is_mobile=True,
                        has_touch=True,
                    )
                    page = context.new_page()
                    page.add_init_script(f"localStorage.setItem('lang', {locale!r});")
                    page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
                    page.wait_for_selector("#btnAttach", timeout=10000)
                    page.locator("#btnAttach").click()
                    popup = page.locator("#attachChoicePopup")
                    if width <= 640:
                        popup.wait_for(state="visible", timeout=5000)
                        box = popup.bounding_box()
                        assert box and box["x"] >= 0 and box["y"] >= 0
                        assert box["x"] + box["width"] <= width
                        assert box["y"] + box["height"] <= 320
                        for choice in popup.locator("[data-attach-choice]").all():
                            choice_box = choice.bounding_box()
                            assert choice_box and choice_box["x"] >= 0 and choice_box["y"] >= 0
                            assert choice_box["x"] + choice_box["width"] <= width
                            assert choice_box["y"] + choice_box["height"] <= 320
                    else:
                        assert page.locator("#fileInput").count() == 1
                        assert popup.is_hidden()
                    context.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
