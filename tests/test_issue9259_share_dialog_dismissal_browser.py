"""#9259: closing the "already shared" dialog must not refresh the public share snapshot (Chromium).

A chat that already has a public share link asks "Copy existing" or "Refresh snapshot". Only the
"Refresh snapshot" button may call ``POST /api/share/create``; the × button, Escape and a click on the
backdrop dismiss the dialog and do nothing. Both entry points are driven through the real page scripts:
the conversation panel button (``btnShareSession`` in boot.js) and ``_createOrRefreshSessionShare``
(sessions.js, the session menu).
"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
SESSION = {"session_id": "s1", "title": "Shared", "workspace": "/tmp/share-gate", "messages": [],
           "message_count": 0, "model": "", "profile": "default", "share_token": "tok1"}


class Server:
    def __init__(self):
        self.creates = 0

    def route(self, r):
        path = urlsplit(r.request.url).path
        if path == "/":
            html = (ROOT / "static/index.html").read_text(encoding="utf-8")
            html = (html.replace("__MAX_UPLOAD_BYTES__", "20971520").replace("__CSRF_TOKEN_JSON__", '""')
                    .replace("__CSP_IMG_EXTRA_JSON__", "[]"))
            r.fulfill(body=html, content_type="text/html")
        elif path == "/static/pwa-startup.js":
            r.fulfill(body="", content_type="application/javascript")
        elif path.startswith("/static/") and (ROOT / path.lstrip("/")).is_file():
            file = ROOT / path.lstrip("/")
            r.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(str(file))[0] or "application/octet-stream")
        elif path == "/api/share/create":
            self.creates += 1
            r.fulfill(json={"session": SESSION, "share": {"url": "/share/tok1"}})
        elif path == "/api/sessions":
            r.fulfill(json={"sessions": []})
        elif path.startswith("/api/"):
            r.fulfill(json={"active": True, "ok": True, "sessions": [], "models": [], "workspaces": []})
        else:
            r.fulfill(status=404, body="")


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as playwright:
        try:
            chromium = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        except Exception as exc:  # no browser binary in this environment
            pytest.skip(f"chromium unavailable: {exc}")
        yield chromium
        chromium.close()


@pytest.fixture
def server():
    return Server()


@pytest.fixture
def page(browser, server):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
    ctx.add_init_script("window.open=()=>null;")
    pg = ctx.new_page()
    pg.route("**/*", server.route)
    pg.goto("http://share-gate.test/", wait_until="load")
    pg.evaluate("(s)=>{S.session=JSON.parse(JSON.stringify(s));}", SESSION)
    yield pg
    ctx.close()


def _open_from_panel(page):
    page.evaluate("()=>{$('btnShareSession').onclick();}")


def _open_from_menu(page):
    page.evaluate("()=>{_createOrRefreshSessionShare(S.session);}")


def _dialog_open(page):
    return page.evaluate("()=>_isAppDialogOpen()")


def _dismiss(page, how):
    if how == "close":
        page.click("#appDialogClose")
    elif how == "escape":
        page.keyboard.press("Escape")
    else:
        page.evaluate("()=>$('appDialogOverlay').dispatchEvent(new MouseEvent('click',{bubbles:true}))")
    page.wait_for_timeout(100)


OPENERS = [("panel", _open_from_panel), ("menu", _open_from_menu)]


@pytest.mark.parametrize("how", ["close", "escape", "backdrop"])
@pytest.mark.parametrize("name,opener", OPENERS, ids=[o[0] for o in OPENERS])
def test_dismissing_the_dialog_does_not_refresh_the_snapshot(page, server, name, opener, how):
    opener(page)
    page.wait_for_selector("#appDialogOverlay", state="visible")
    _dismiss(page, how)
    assert not _dialog_open(page)
    assert server.creates == 0


@pytest.mark.parametrize("name,opener", OPENERS, ids=[o[0] for o in OPENERS])
def test_refresh_snapshot_button_still_refreshes(page, server, name, opener):
    opener(page)
    page.wait_for_selector("#appDialogOverlay", state="visible")
    page.click("#appDialogCancel")
    page.wait_for_timeout(150)
    assert server.creates == 1


@pytest.mark.parametrize("name,opener", OPENERS, ids=[o[0] for o in OPENERS])
def test_copy_existing_button_does_not_refresh(page, server, name, opener):
    opener(page)
    page.wait_for_selector("#appDialogOverlay", state="visible")
    page.click("#appDialogConfirm")
    page.wait_for_timeout(150)
    assert server.creates == 0


def test_other_confirm_dialogs_keep_resolving_false_on_dismissal(page):
    result = page.evaluate("""async()=>{
      const p=showConfirmDialog({title:'t',message:'m'});
      $('appDialogClose').click();
      return await p;
    }""")
    assert result is False
