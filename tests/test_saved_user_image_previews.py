"""Browser regression for persisted Agent image-marker user messages.

Uses the real session import/load/reload path and real media endpoint, with an
agent-free localhost server and disposable state. Playwright is optional, as in
other browser tests; install it and Chromium to run this gate.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.browser_conversation_lifecycle import _start_webui_server, _terminate_process

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def image_preview_browser(tmp_path_factory):
    playwright = pytest.importorskip("playwright.sync_api")
    root = tmp_path_factory.mktemp("saved-image-previews")
    agent = root / "no-agent"
    agent.mkdir()
    (agent / "run_agent.py").write_text('"""Test-only agent stub."""\n')
    home = root / "hermes-home"
    images = home / "cache" / "images"
    images.mkdir(parents=True)
    # Two generated solid-colour images; no real screenshots are used.
    from PIL import Image
    paths = []
    for name, color in (("preview_a.jpg", "red"), ("preview_b.jpg", "blue")):
        path = images / name
        Image.new("RGB", (16, 16), color).save(path)
        paths.append(str(path))
    workspace = root / "workspace"
    workspace.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")}
    for key in ("API_SERVER_KEY", "HERMES_WEBUI_PASSWORD", "HERMES_WEBUI_EXTENSION_DIR", "HERMES_WEBUI_EXTENSION_MANIFEST"):
        env.pop(key, None)
    env.update({
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": str(root / "webui-state"),
        "HERMES_HOME": str(home), "HERMES_BASE_HOME": str(home),
        "HERMES_CONFIG_PATH": str(home / "config.yaml"),
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": str(agent),
        "HERMES_WEBUI_DEFAULT_WORKSPACE": str(workspace),
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    })
    proc, log, _, base = _start_webui_server(ROOT, env, root)
    try:
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                yield browser, base, paths, root
            finally:
                browser.close()
    finally:
        _terminate_process(proc)
        log.close()


def _load_message(page, base, content, attachments=None):
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_function("typeof loadSession === 'function' && typeof window._autoScrollFollow === 'boolean'")
    return page.evaluate("""async ({content, attachments}) => {
        const response = await fetch('/api/session/import', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({title:'Saved image preview regression', messages:[
            {role:'user', content, ...(attachments ? {attachments} : {})}
          ]})
        });
        const data = await response.json();
        if (!response.ok) throw new Error('fixture import failed');
        await loadSession(data.session.session_id);
        return data.session.session_id;
    }""", {"content": content, "attachments": attachments})


@pytest.mark.parametrize("width", [1280, 390])
def test_saved_marker_images_load_and_survive_reload(image_preview_browser, width):
    browser, base, paths, root = image_preview_browser
    content = "Look at these screenshots.\n" + "\n".join(f"[Image attached at: {p}]" for p in paths) + "\n[screenshot]\n[screenshot]"
    context = browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        sid = _load_message(page, base, content)
        for reload in (False, True):
            if reload:
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector('#msgInner [data-role="user"]')
            # Exercise both user text rendering modes, not only markdown.
            for markdown in (False, True):
                page.evaluate("value => { window._renderUserMarkdown=value; renderMessages(); }", markdown)
                row = page.locator('#msgInner [data-role="user"]')
                page.screenshot(path=str(root / f"{'after-reload' if reload else 'loaded'}-{width}-{markdown}.png"))
                assert row.locator("img.msg-media-img").count() == 2
                page.wait_for_function("""() => [...document.querySelectorAll('#msgInner [data-role="user"] img')].every(i => i.complete && i.naturalWidth === 16)""")
                assert "[Image attached at:" not in row.inner_text()
                assert "[screenshot]" not in row.inner_text()
                assert "Look at these screenshots." in row.inner_text()
                assert row.get_attribute("data-edit-text") == content
                assert "[Image attached at:" not in row.get_attribute("data-raw-text")
                assert page.evaluate("S.messages[0].content") == content
                sources = row.locator("img").evaluate_all("images => images.map(i => ({path:new URL(i.src).searchParams.get('path'), sid:new URL(i.src).searchParams.get('session_id')}))")
                assert sources == [{"path": path, "sid": sid} for path in paths]
        assert errors == []
    finally:
        context.close()


def _marker_presentation(text):
    src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    start = src.index("function _userImageMarkerPresentation(")
    i = src.index("{", start) + 1
    depth = 1
    while depth:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    script = src[start:i] + "\nprocess.stdout.write(JSON.stringify(_userImageMarkerPresentation(JSON.parse(process.argv[1]))));"
    result = subprocess.run(["node", "-e", script, json.dumps(text)], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_markers_inside_code_examples_stay_text(newline):
    marker = "[Image attached at: /tmp/example.png]"
    content = newline.join(["Example:", "```python", marker, "```js", marker, "```", "After"])
    presentation = _marker_presentation(content)
    assert presentation["paths"] == []
    assert presentation["text"].count(marker) == 2


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_markers_after_a_closed_code_example_become_previews(newline):
    content = newline.join(["```", "code", "```", "[Image attached at: /tmp/real.png]", "[screenshot]"])
    presentation = _marker_presentation(content)
    assert presentation["paths"] == ["/tmp/real.png"]
    assert "[Image attached at:" not in presentation["text"]
    assert "[screenshot]" not in presentation["text"]
