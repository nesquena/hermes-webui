"""Keep conversation titles usable beside source badges in the real header."""
import os
from pathlib import Path
import re

import pytest

playwright = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as pw:
        options = {"headless": True}
        if os.environ.get("CHROMIUM_EXECUTABLE_PATH"):
            options["executable_path"] = os.environ["CHROMIUM_EXECUTABLE_PATH"]
        instance = pw.chromium.launch(**options)
        yield instance
        instance.close()


@pytest.mark.parametrize("width", [320, 375, 390, 430, 640, 900, 1440])
@pytest.mark.parametrize("dark", [False, True])
def test_source_badge_leaves_room_for_title_and_touch_controls(browser, width, dark):
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    header = re.search(r'<header class="app-titlebar"[\s\S]*?</header>', html).group()
    source = (ROOT / "static/panels.js").read_text(encoding="utf-8")
    start = source.index("function syncAppTitlebar(")
    end = re.search(r"^(?:async )?function \w+\(", source[start + 1:], re.M)
    sync = source[start:start + 1 + end.start()]
    page = browser.new_page(viewport={"width": width, "height": 700},
                            is_mobile=width <= 640, has_touch=width <= 640)
    try:
        document = '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body>' + header + '</body></html>'
        page.route("**/*", lambda route: route.fulfill(body=document, content_type="text/html")
                   if route.request.url == "https://header.invalid/" else route.abort())
        # A new document applies Chromium's touch emulation; set_content on the
        # initial blank page does not expose its touch-event feature detection.
        page.goto("https://header.invalid/")
        page.add_style_tag(content=(ROOT / "static/style.css").read_text(encoding="utf-8"))
        page.evaluate("dark => document.documentElement.classList.toggle('dark', dark)", dark)
        page.add_script_tag(content="""
const S={session:{title:'Example conversation',model:'example'},messages:[]};
const _currentPanel='chat',_renamingAppTitlebar=false;
const t=key=>key;
""" + sync)
        for label in ("Desktop", "Telegram", "Other client", "A very long source label"):
            page.evaluate("""label => {
                S.session.source_label=label;
                S.session.chat_read_only_reason='Read-only in WebUI.';
                syncAppTitlebar();
            }""", label)
            metrics = page.evaluate("""() => {
                const title=document.getElementById('appTitlebarTitle');
                const badge=document.querySelector('#appTitlebarSub .topbar-source-badge');
                const rect=el=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,right:r.right,bottom:r.bottom,width:r.width,height:r.height}};
                return {title:rect(title),badge:rect(badge),subtitle:rect(document.getElementById('appTitlebarSub')),header:rect(document.querySelector('.app-titlebar')),
                    controls:['btnHamburger','btnTitlebarNewChat','btnReload'].map(id=>rect(document.getElementById(id)))};
            }""")
            assert metrics["title"]["width"] >= 72, (width, label, metrics)
            assert metrics["header"]["height"] <= (53 if width <= 640 else 39)
            assert metrics["badge"]["right"] <= width, (width, label, metrics)
            if width <= 640:
                assert metrics["badge"]["y"] >= metrics["title"]["bottom"] - 1
                assert metrics["badge"]["width"] >= 72
                assert metrics["badge"]["right"] <= metrics["subtitle"]["right"] + 1
                for control in metrics["controls"]:
                    assert control["width"] >= 44 and control["height"] >= 44
                    assert control["x"] >= 0 and control["right"] <= width
                page.locator('#appTitlebarTitle').tap(timeout=5000)
                assert page.locator('.app-titlebar-title-popover').inner_text(timeout=5000) == 'Example conversation'
        page.evaluate("() => {S.session.source_label='WebUI';delete S.session.chat_read_only_reason;syncAppTitlebar()}")
        assert page.locator('.topbar-source-badge').count() == 0
        assert page.locator('.app-titlebar-inner').evaluate("el => getComputedStyle(el).display") == 'flex'
    finally:
        page.close()
