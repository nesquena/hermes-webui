"""Computed-layout regressions for the attachment-tray composer cap.

Source-string checks cannot see that an unscoped ``.composer-box`` max-height
clips Send. This loads ``static/style.css`` in headless Chromium and measures
the real boxes. It skips when ``chrome-headless-shell`` is not installed.
"""
from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CSS_PATH = REPO / "static" / "style.css"

_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="FILE_CSS">
</head>
<body>
  <div class="main">
    <div style="flex:1;min-height:0"></div>
    <div class="composer-wrap">
      <div class="composer-box" id="composerBox">
        <div class="attach-tray" id="attachTray"></div>
        <textarea id="msg" rows="1"></textarea>
        <div class="composer-footer">
          <div class="composer-left"></div>
          <div class="composer-right">
            <button class="send-btn" id="btnSend" type="button">Send</button>
          </div>
        </div>
      </div>
    </div>
  </div>
  <script>
    const params = new URLSearchParams(location.search);
    const files = Number(params.get("files") || "0");
    const draft = params.get("draft") || "short";
    const tray = document.getElementById("attachTray");
    if (files > 0) {
      tray.classList.add("has-files");
      for (let i = 0; i < files; i++) {
        const chip = document.createElement("div");
        chip.className = "attach-chip";
        const name = document.createElement("span");
        name.className = "attach-chip-name";
        name.textContent = "notes-" + String(i).padStart(2, "0") + ".txt";
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "×";
        chip.append(name, remove);
        tray.appendChild(chip);
      }
    }
    const field = document.getElementById("msg");
    field.value = draft === "long"
      ? Array.from({length: 40}, () => "long draft line that should reach the textarea cap").join("\\n")
      : "hi";
    const box = document.querySelector(".composer-box");
    const send = document.querySelector("#btnSend");
    const boxRect = box.getBoundingClientRect();
    const sendRect = send.getBoundingClientRect();
    const trayRect = tray.getBoundingClientRect();
    const fieldRect = field.getBoundingClientRect();
    document.documentElement.setAttribute("data-metrics", JSON.stringify({
      innerWidth, innerHeight,
      boxH: boxRect.height,
      boxMax: getComputedStyle(box).maxHeight,
      sendH: sendRect.height,
      sendClip: Math.max(0, sendRect.bottom - innerHeight),
      sendOutsideBox: Math.max(0, sendRect.bottom - boxRect.bottom),
      trayClient: tray.clientHeight,
      trayScroll: tray.scrollHeight,
      trayOverflow: getComputedStyle(tray).overflowY,
      taH: fieldRect.height,
      taMax: getComputedStyle(field).maxHeight,
    }));
  </script>
</body>
</html>
"""

_VIEWPORTS = (
    (1100, 300),  # short desktop window from the review
    (1100, 600),  # desktop height where 48 files must still scroll
    (390, 844),   # phone portrait
    (844, 390),   # phone landscape
    (639, 700),
    (640, 700),
    (641, 700),
)
_CASES = [
    (width, height, files, draft)
    for width, height in _VIEWPORTS
    for files in (0, 1, 48)
    for draft in ("short", "long")
]


def _headless_shell() -> Path | None:
    env = os.environ.get("CHROME_HEADLESS_SHELL")
    if env and Path(env).is_file():
        return Path(env)
    found = shutil.which("chrome-headless-shell")
    if found:
        return Path(found)
    roots = []
    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browsers:
        roots.append(Path(browsers))
    roots.append(Path.home() / ".cache" / "ms-playwright")
    for root in roots:
        matches = sorted(
            root.glob(
                "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"
            )
        )
        if matches:
            return matches[-1]
    return None


@pytest.fixture(scope="module")
def composer_page(tmp_path_factory):
    shell = _headless_shell()
    if shell is None:
        pytest.skip("chrome-headless-shell is not installed")
    page = tmp_path_factory.mktemp("attach-tray") / "composer.html"
    page.write_text(_PAGE.replace("FILE_CSS", CSS_PATH.as_uri()), encoding="utf-8")
    return shell, page


def _measure(composer_page, width, height, files, draft):
    shell, page = composer_page
    profile = page.parent / f"profile-{width}x{height}-{files}-{draft}"
    completed = subprocess.run(
        [
            str(shell),
            "--no-sandbox",
            "--disable-gpu",
            "--no-first-run",
            "--disable-background-networking",
            f"--user-data-dir={profile}",
            f"--window-size={width},{height}",
            f"--ozone-override-screen-size={width},{height}",
            "--dump-dom",
            f"{page.as_uri()}?files={files}&draft={draft}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=25,
    )
    match_at = completed.stdout.find('data-metrics="')
    assert completed.returncode == 0 and match_at >= 0, (
        f"headless shell failed ({completed.returncode}) for "
        f"{width}x{height} files={files} {draft}\n{completed.stderr[-500:]}"
    )
    raw = completed.stdout[match_at + len('data-metrics="'):]
    raw = raw.split('"', 1)[0]
    return json.loads(html.unescape(raw))


@pytest.mark.parametrize(
    ("width", "height", "files", "draft"),
    _CASES,
    ids=[f"{w}x{h}-files{n}-{draft}" for w, h, n, draft in _CASES],
)
def test_composer_budget(composer_page, width, height, files, draft):
    metrics = _measure(composer_page, width, height, files, draft)
    assert metrics["innerWidth"] == width, metrics
    assert metrics["innerHeight"] == height, metrics
    assert metrics["sendClip"] < 1, metrics
    assert metrics["sendOutsideBox"] < 1, metrics
    if files == 0:
        assert metrics["boxMax"] == "none", metrics
        assert metrics["taMax"] == "200px", metrics
        if draft == "long":
            assert metrics["taH"] >= 170, metrics
            if (width, height) == (1100, 300):
                # Unscoped cap measured 210px here and clipped Send.
                assert metrics["boxH"] >= 230, metrics
        return
    assert metrics["boxMax"] not in ("none", "0px"), metrics
    assert metrics["trayOverflow"] == "auto", metrics
    if width <= 639:
        assert metrics["taMax"] == "88px", metrics
    else:
        assert metrics["taMax"] == "200px", metrics
    if files == 48:
        assert metrics["trayScroll"] > metrics["trayClient"] + 16, metrics
        if (width, height) == (1100, 300):
            assert metrics["trayClient"] >= 56, metrics
    if draft == "short":
        assert metrics["taH"] < 80, metrics
    elif (width, height) == (1100, 300):
        assert metrics["taH"] < 170, metrics
    elif height >= 600 and width >= 640:
        assert metrics["taH"] >= 170, metrics
