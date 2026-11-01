import base64
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
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env.update({
        "HERMES_WEBUI_PORT": str(port),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": str(state_dir),
        "HERMES_HOME": str(state_dir),
        "HERMES_BASE_HOME": str(state_dir),
        # conftest points HERMES_CONFIG_PATH / HERMES_WEBUI_DEFAULT_WORKSPACE at the
        # shared test server's state, and os.environ.copy() above inherits them. Earlier
        # tests in a full run write a configured provider into that shared config.yaml,
        # so this server's /api/onboarding/status auto-completes onboarding and persists
        # onboarding_completed=true into settings.json. Every later browser context then
        # skips the status fetch the test waits for, and the run times out. Keep this
        # server on its own (absent) config and workspace so it is order-independent.
        "HERMES_CONFIG_PATH": str(state_dir / "config.yaml"),
        "HERMES_WEBUI_DEFAULT_WORKSPACE": str(workspace),
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
            for width in (320, 400, 480, 768, 844, 1280):
                for locale in ("en", "ru", "de"):
                    context = browser.new_context(
                        viewport={"width": width, "height": 320},
                        is_mobile=True,
                        has_touch=True,
                    )
                    page = context.new_page()
                    page.add_init_script(f"localStorage.setItem('hermes-lang', {locale!r});")
                    with page.expect_response(
                        lambda response: response.url.endswith("/api/onboarding/status")
                    ) as onboarding_response:
                        page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
                    assert onboarding_response.value.json()["completed"] is True
                    page.wait_for_function(
                        "document.getElementById('onboardingOverlay').style.display !== 'flex'"
                    )
                    page.wait_for_selector("#btnAttach", timeout=10000)
                    attach = page.locator("#btnAttach")
                    popup = page.locator("#attachChoicePopup")
                    attach.click()
                    popup.wait_for(state="visible", timeout=5000)
                    attach.click()
                    playwright.expect(popup).to_be_hidden()
                    playwright.expect(attach).to_have_attribute("aria-expanded", "false")
                    attach.click()
                    if width == 400 and locale == "en":
                        page.screenshot(path=str(tmp_path / "attachment-choice.png"))
                    labels = {
                        "en": ("Take photo", "Choose files"),
                        "ru": ("Сделать фото", "Выбрать файлы"),
                        "de": ("Foto aufnehmen", "Dateien auswählen"),
                    }[locale]
                    photo = popup.locator('[data-i18n="composer_attach_photo"]')
                    files = popup.locator('[data-i18n="composer_attach_files"]')
                    playwright.expect(photo).to_have_text(labels[0])
                    playwright.expect(files).to_have_text(labels[1])
                    popup.wait_for(state="visible", timeout=5000)
                    box = popup.bounding_box()
                    assert box and box["x"] >= 0 and box["y"] >= 0
                    assert box["x"] + box["width"] <= width
                    assert box["y"] + box["height"] <= 320
                    assert popup.locator(".ws-opt-icon svg").count() == 2
                    page.keyboard.press("ArrowDown")
                    assert popup.is_visible()
                    page.keyboard.press("Tab")
                    assert popup.is_hidden()
                    playwright.expect(page.locator("#btnAttach")).to_have_attribute("aria-expanded", "false")
                    for choice, input_id in ((photo, "cameraFileInput"), (files, "fileInput")):
                        for outcome in ("select", "cancel"):
                            page.locator("#btnAttach").click()
                            with page.expect_file_chooser() as chooser:
                                choice.click()
                            page.locator("#btnAttach").evaluate("button => button.blur()")
                            if outcome == "select":
                                chooser.value.set_files({"name": "photo.png", "mimeType": "image/png", "buffer": base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")})
                            else:
                                # Playwright emits change for empty files; native cancellation emits cancel.
                                page.locator(f"#{input_id}").evaluate("input => input.addEventListener('change', event => event.stopImmediatePropagation(), {capture:true, once:true})")
                                chooser.value.set_files([])
                                page.locator(f"#{input_id}").dispatch_event("cancel")
                            page.bring_to_front()
                            playwright.expect(page.locator("#btnAttach")).to_be_focused()
                            assert popup.is_hidden()
                    context.close()
            context = browser.new_context(viewport={"width": 1280, "height": 720})
            page = context.new_page()
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            attach = page.locator("#btnAttach")
            attach.wait_for()
            for attr in ("aria-expanded", "aria-controls", "aria-haspopup"):
                assert attach.get_attribute(attr) is None
            with page.expect_file_chooser() as chooser:
                attach.click()
            chooser.value.set_files([])
            assert page.locator("#attachChoicePopup").is_hidden()
            context.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
