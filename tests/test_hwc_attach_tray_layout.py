"""Computed-layout regressions for the attachment-tray composer cap.

Source-string checks cannot see that an unscoped ``.composer-box`` max-height
clips Send. This loads ``static/style.css`` in headless Chromium and measures
the real boxes. It skips when ``chrome-headless-shell`` is not installed.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _slice_fn(source: str, start: str, end: str) -> str:
    idx = source.find(start)
    nxt = source.find(end, idx + len(start))
    assert idx >= 0 and nxt > idx, f"missing {start!r}"
    return source[idx:nxt]


def _real_composer_page(tmp_path: Path) -> Path:
    """Shell around the production composer and the production renderTray.

    The minimal page above invents a shrinkable name span and a one-button
    footer, so it never saw the real cf-icons footer or the bare filename text.
    """
    index = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    start = index.find('<div class="composer-box" id="composerBox">')
    end = index.find("\n    </div>\n    </div><!-- /#mainChat -->", start)
    assert start >= 0 and end > start, "composer markup markers moved"
    composer = index[start:end]
    ui = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    render = _slice_fn(ui, "function renderTray()", "\nfunction _uploadTooLargeMessage")
    fit = _slice_fn(ui, "function _fitComposerFooter()", "\nwindow._fitComposerFooter")
    kind = _slice_fn(ui, "function _mediaKindForName(name=''){", "\nfunction _mediaSpeedControlsHtml")
    page = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="CSS_URI">
</head>
<body>
<header class="app-titlebar"><span class="app-titlebar-title">Hermes</span></header>
<div class="layout">
  <nav class="rail"><button class="rail-btn" type="button">C</button></nav>
  <aside class="sidebar"><div class="sidebar-header"><h1>Hermes</h1></div></aside>
  <main class="main">
    <div id="mainChat" class="main-view">
      <div class="messages-shell"><div class="messages" id="messages"></div></div>
      <div class="composer-wrap" id="composerWrap">
        COMPOSER_HTML
      </div>
    </div>
  </main>
</div>
<script src="ICONS_URI"></script>
<script>
function $(id){return document.getElementById(id);}
const S={pendingFiles:[]};
function updateSendBtn(){}
function t(key){return key;}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _IMAGE_EXTS=/\\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS=/\\.svg$/i;
const _AUDIO_EXTS=/\\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm|oga)$/i;
const _VIDEO_EXTS=/\\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;
KIND_JS
RENDER_JS
FIT_JS
const params=new URLSearchParams(location.search);
if(params.get('skin')) document.documentElement.dataset.skin=params.get('skin');
$('composerModelLabel').textContent=params.get('model')||'';
const fileName=params.get('name')||'notes.txt';
S.pendingFiles=[{name:fileName}];
$('msg').value=params.get('draft')||'hi';
renderTray();
_fitComposerFooter();
function _trayProbe(){
  const send=$('btnSend').getBoundingClientRect();
  const box=$('composerBox').getBoundingClientRect();
  const footer=document.querySelector('.composer-footer');
  const tray=$('attachTray');
  const trayRect=tray.getBoundingClientRect();
  const chip=tray.querySelector('.attach-chip');
  const button=chip.querySelector('button');
  const buttonRect=button.getBoundingClientRect();
  const nameEl=chip.querySelector('.attach-chip-name');
  const nameRect=nameEl.getBoundingClientRect();
  const range=document.createRange();
  range.selectNodeContents(nameEl);
  const glyph=range.getBoundingClientRect();
  const icon=button.querySelector('svg');
  const iconRect=icon?icon.getBoundingClientRect():buttonRect;
  const overlap=(a,b)=>Math.max(0, Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top));
  const sx=(send.left+send.right)/2, sy=(send.top+send.bottom)/2;
  const bx=(buttonRect.left+buttonRect.right)/2, by=(buttonRect.top+buttonRect.bottom)/2;
  const nx=(nameRect.left+nameRect.right)/2, ny=(nameRect.top+nameRect.bottom)/2;
  const sendHit=document.elementFromPoint(sx, sy);
  const buttonHit=document.elementFromPoint(bx, by);
  const nameHit=document.elementFromPoint(nx, ny);
  return {
    innerWidth, innerHeight,
    pointer: matchMedia('(pointer: coarse)').matches ? 'coarse' : 'fine',
    footerClass: footer.className,
    sidebarW: document.querySelector('.sidebar').getBoundingClientRect().width,
    draft: $('msg').value,
    boxH: box.height,
    boxB: box.bottom,
    boxMax: getComputedStyle($('composerBox')).maxHeight,
    sendT: send.top,
    sendB: send.bottom,
    sendClip: Math.max(0, send.bottom-innerHeight),
    sendOutside: Math.max(0, send.bottom-box.bottom),
    sendHit: !!(sendHit && sendHit.closest && sendHit.closest('#btnSend')),
    trayClient: tray.clientHeight,
    trayScroll: tray.scrollHeight,
    scrollTop: tray.scrollTop,
    taH: $('msg').getBoundingClientRect().height,
    footerH: footer.getBoundingClientRect().height,
    chipText: nameEl ? nameEl.textContent : '',
    nameClient: nameEl ? nameEl.clientWidth : 0,
    nameScroll: nameEl ? nameEl.scrollWidth : 0,
    nameFont: nameEl ? getComputedStyle(nameEl).fontFamily : '',
    nameHit: !!(nameHit && nameEl.contains(nameHit)),
    glyphH: glyph.height,
    glyphTray: overlap(glyph, trayRect),
    btnL: buttonRect.left,
    btnR: buttonRect.right,
    btnT: buttonRect.top,
    btnB: buttonRect.bottom,
    btnW: buttonRect.width,
    btnH: buttonRect.height,
    btnHit: !!(buttonHit && button.contains(buttonHit)),
    btnTray: overlap(buttonRect, trayRect),
    iconH: iconRect.height,
    iconTray: overlap(iconRect, trayRect),
    chipW: chip.getBoundingClientRect().width,
    trayW: trayRect.width,
  };
}
window._trayProbe=_trayProbe;
document.documentElement.dataset.metrics=JSON.stringify(_trayProbe());
</script>
</body>
</html>
"""
    page = (
        page.replace("CSS_URI", (REPO / "static" / "style.css").as_uri())
        .replace("ICONS_URI", (REPO / "static" / "icons.js").as_uri())
        .replace("COMPOSER_HTML", composer)
        .replace("KIND_JS", kind)
        .replace("RENDER_JS", render)
        .replace("FIT_JS", fit)
    )
    path = tmp_path / "real-composer.html"
    path.write_text(page, encoding="utf-8")
    return path


async def _cdp(ws, ids, method, params=None):
    ids[0] += 1
    await ws.send(json.dumps({"id": ids[0], "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), 8))
        if msg.get("id") == ids[0]:
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg.get("result", {})


def _measure_real(shell: Path, page: Path, width: int, height: int, query: str, click: bool, scroll: bool = False):
    try:
        import websockets
    except ImportError:
        pytest.skip("websockets is not installed")
    port = _free_port()
    profile = page.parent / f"profile-{width}x{height}-{click}"
    proc = subprocess.Popen(
        [
            str(shell),
            "--no-sandbox",
            "--disable-gpu",
            "--no-first-run",
            "--disable-background-networking",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    ws_url = None
    try:
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=0.3) as resp:
                    pages = json.load(resp)
                if pages and pages[0].get("webSocketDebuggerUrl"):
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                time.sleep(0.1)
        if not ws_url:
            proc.kill()
            err = proc.stderr.read().decode()[-400:]
            raise AssertionError(f"headless shell did not open a debugging port\n{err}")

        async def run():
            ids = [0]
            async with websockets.connect(ws_url, max_size=8_000_000) as ws:
                await _cdp(ws, ids, "Runtime.enable")
                await _cdp(ws, ids, "Emulation.setDeviceMetricsOverride", {
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                })
                await _cdp(ws, ids, "Emulation.setEmulatedMedia", {
                    "media": "",
                    "features": [
                        {"name": "pointer", "value": "coarse"},
                        {"name": "hover", "value": "none"},
                    ],
                })
                await _cdp(ws, ids, "Emulation.setTouchEmulationEnabled", {
                    "enabled": True,
                    "maxTouchPoints": 1,
                })
                await _cdp(ws, ids, "Page.navigate", {"url": f"{page.as_uri()}?{query}"})
                metrics = None
                for _ in range(40):
                    result = await _cdp(ws, ids, "Runtime.evaluate", {
                        "expression": "document.documentElement.dataset.metrics||''",
                        "returnByValue": True,
                    })
                    raw = result.get("result", {}).get("value")
                    if raw:
                        metrics = json.loads(raw)
                        break
                    await asyncio.sleep(0.05)
                if metrics is None:
                    raise AssertionError("real composer page produced no metrics")
                if scroll:
                    moved = await _cdp(ws, ids, "Runtime.evaluate", {
                        "expression": (
                            "(()=>{const tray=document.getElementById('attachTray');"
                            "tray.scrollTop=tray.scrollHeight;"
                            "return JSON.stringify(window._trayProbe());})()"
                        ),
                        "returnByValue": True,
                    })
                    metrics = json.loads(moved["result"]["value"])
                if click:
                    x = (metrics["btnL"] + metrics["btnR"]) / 2
                    y = (metrics["btnT"] + metrics["btnB"]) / 2
                    for event in ("mousePressed", "mouseReleased"):
                        await _cdp(ws, ids, "Input.dispatchMouseEvent", {
                            "type": event,
                            "x": x,
                            "y": y,
                            "button": "left",
                            "clickCount": 1,
                        })
                    removed = await _cdp(ws, ids, "Runtime.evaluate", {
                        "expression": (
                            "JSON.stringify({files:S.pendingFiles.length,"
                            "chips:document.querySelectorAll('#attachTray .attach-chip').length,"
                            "has:document.getElementById('attachTray').classList.contains('has-files')})"
                        ),
                        "returnByValue": True,
                    })
                    metrics["afterClick"] = json.loads(removed["result"]["value"])
                return metrics

        return asyncio.run(run())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def _assert_chip_name_and_remove_reachable(metrics):
    """The filename glyphs and the remove control are on screen, and a
    native click on the button center drops the file.

    A 44px box or ``scrollHeight > clientHeight`` is not enough: the center
    has to land inside the tray's visible slice.
    """
    assert metrics["nameHit"] is True, metrics
    assert metrics["glyphH"] > 8, metrics
    assert metrics["glyphTray"] >= metrics["glyphH"] - 0.5, metrics
    assert metrics["iconH"] > 8, metrics
    assert metrics["iconTray"] >= metrics["iconH"] - 0.5, metrics
    assert metrics["btnW"] >= 44 and metrics["btnH"] >= 44, metrics
    assert metrics["btnTray"] > 18, metrics
    assert metrics["btnHit"] is True, metrics
    assert metrics["afterClick"] == {"files": 0, "chips": 0, "has": False}, metrics


def test_real_footer_stays_inside_short_touch_viewport(tmp_path):
    """One file, coarse 844×200, sidebar open, real cf-icons footer.

    The unshrinkable tray used to push Send to 214px (14px past the viewport).
    The draft stays at its minimum and the tray yields instead. Yielding must
    still leave the filename and the remove control's center on screen, at
    rest and after the tray is scrolled to its bottom.
    """
    shell = _headless_shell()
    if shell is None:
        pytest.skip("chrome-headless-shell is not installed")
    page = _real_composer_page(tmp_path)
    metrics = _measure_real(
        shell,
        page,
        844,
        200,
        "name=notes.txt&draft=hi&model=Claude%20Sonnet%204.6",
        click=True,
    )
    assert metrics["innerWidth"] == 844, metrics
    assert metrics["innerHeight"] == 200, metrics
    assert metrics["pointer"] == "coarse", metrics
    assert metrics["sidebarW"] >= 250, metrics
    assert metrics["draft"] == "hi", metrics
    assert metrics["chipText"] == "notes.txt", metrics
    assert "cf-icons" in metrics["footerClass"], metrics
    assert "cf-burger" not in metrics["footerClass"], metrics
    assert metrics["boxMax"] not in ("none", "0px"), metrics
    assert metrics["footerH"] >= 52, metrics
    assert 40 <= metrics["taH"] <= 48, metrics
    assert metrics["trayScroll"] > metrics["trayClient"] + 16, metrics
    assert metrics["sendClip"] < 1, metrics
    assert metrics["sendOutside"] < 1, metrics
    assert metrics["sendT"] >= 0, metrics
    assert metrics["sendHit"] is True, metrics
    assert metrics["scrollTop"] == 0, metrics
    _assert_chip_name_and_remove_reachable(metrics)
    scrolled = _measure_real(
        shell,
        page,
        844,
        200,
        "name=notes.txt&draft=hi&model=Claude%20Sonnet%204.6",
        click=True,
        scroll=True,
    )
    assert scrolled["scrollTop"] > 8, scrolled
    assert scrolled["sendClip"] < 1, scrolled
    assert scrolled["sendOutside"] < 1, scrolled
    assert scrolled["sendHit"] is True, scrolled
    assert scrolled["trayScroll"] > scrolled["trayClient"] + 16, scrolled
    _assert_chip_name_and_remove_reachable(scrolled)


def test_long_monospace_filename_keeps_remove_control(tmp_path):
    """47-character extensionless name under the Nous monospace stack.

    Production renderTray used to leave the filename as bare text, so the
    44px remove control was clipped. A coordinate click on its center removes
    the file only when that center is actually inside the viewport.
    """
    shell = _headless_shell()
    if shell is None:
        pytest.skip("chrome-headless-shell is not installed")
    page = _real_composer_page(tmp_path)
    name = "a" * 47
    metrics = _measure_real(
        shell,
        page,
        390,
        844,
        f"name={name}&draft=hi&skin=nous",
        click=True,
    )
    assert metrics["innerWidth"] == 390, metrics
    assert metrics["innerHeight"] == 844, metrics
    assert metrics["chipText"] == name, metrics
    assert "monospace" in metrics["nameFont"], metrics
    assert metrics["nameScroll"] > metrics["nameClient"] + 20, metrics
    assert metrics["chipW"] <= metrics["trayW"] + 1, metrics
    assert metrics["btnW"] >= 44 and metrics["btnH"] >= 44, metrics
    assert metrics["btnL"] >= -0.5, metrics
    assert metrics["btnR"] <= metrics["innerWidth"] + 0.5, metrics
    assert metrics["btnT"] >= -0.5, metrics
    assert metrics["btnB"] <= metrics["innerHeight"] + 0.5, metrics
    assert metrics["btnHit"] is True, metrics
    assert metrics["afterClick"] == {"files": 0, "chips": 0, "has": False}, metrics
