"""Rendered layout coverage for the workspace preferences menu."""

import os
from pathlib import Path

import pytest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

from tests._layout_helpers import assert_layout_sane, assert_no_raw_i18n_keys


ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT = os.environ.get("WEBUI6066_SCREENSHOT")
BASE = os.environ.get("WEBUI6066_BASE_URL")


def _require_playwright():
    if sync_playwright is None:
        pytest.skip("playwright is unavailable; run `playwright install chromium`")
    return sync_playwright


def _require_test_base():
    if not BASE:
        pytest.skip("WEBUI6066_BASE_URL must point to an explicitly launched isolated test server")
    if BASE.rstrip("/") == "http://127.0.0.1:8787":
        pytest.fail("refusing to use the production WebUI port 8787 for rendered proof")
    return BASE.rstrip("/")


def _open_workspace_prefs(page, locale, entries=None):
    page.goto(_require_test_base() + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#btnWorkspacePrefs", timeout=10000)
    page.evaluate("""entries => {
        S.session={workspace:'/tmp/workspace'};
        S.currentDir='.';
        S.entries=entries;
        renderFileTree();
    }""", entries or [
        {"name": "link", "path": "link", "type": "symlink", "mtime_ns": 1, "birthtime_ns": 1},
        {"name": "Documents", "path": "Documents", "type": "dir", "mtime_ns": 2, "birthtime_ns": 2},
        {"name": "recent.txt", "path": "recent.txt", "type": "file", "mtime_ns": 3, "birthtime_ns": 3},
    ])
    page.locator("#btnWorkspacePrefs").evaluate("el => el.click()")
    menu = page.locator(".workspace-prefs-menu")
    menu.wait_for(state="visible", timeout=5000)
    assert page.locator(".workspace-prefs-item--radio").count() == 4
    assert page.locator("#workspaceShowHiddenFiles").count() == 1
    assert page.locator(".workspace-prefs-sep").count() == 1
    assert page.locator(".workspace-prefs-grouplabel").inner_text() == "SORT BY"
    return menu


def _assert_menu_inside_viewport(page, menu):
    rect = menu.bounding_box()
    assert rect is not None
    assert rect["x"] >= 0
    assert rect["y"] >= 0
    assert rect["x"] + rect["width"] <= page.viewport_size["width"]
    assert rect["y"] + rect["height"] <= page.viewport_size["height"]
    assert_layout_sane(page, scope_selector=".workspace-prefs-menu", checks=["overlap", "clip", "container-escape", "raw-string"])
    assert_no_raw_i18n_keys(page, scope_selector=".workspace-prefs-menu")


def test_prefs_menu_layout():
    sp = _require_playwright()
    with sp() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            for width, height in [(1280, 720), (1024, 600), (480, 320)]:
                context = browser.new_context(viewport={"width": width, "height": height})
                page = context.new_page()
                page.add_init_script("localStorage.setItem('hermes-webui-workspace-panel','open')")
                menu = _open_workspace_prefs(page, "en")
                _assert_menu_inside_viewport(page, menu)
                if width == 1024 and SCREENSHOT:
                    page.screenshot(path=SCREENSHOT, full_page=False)
                context.close()
        finally:
            browser.close()


def test_prefs_menu_repositions_after_created_sort_support_flip():
    sp = _require_playwright()
    with sp() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            context = browser.new_context(viewport={"width": 480, "height": 480})
            page = context.new_page()
            page.add_init_script("localStorage.setItem('hermes-webui-workspace-panel','open')")
            menu = _open_workspace_prefs(page, "en", [
                {"name": "link", "path": "link", "type": "symlink", "mtime_ns": 1},
                {"name": "Documents", "path": "Documents", "type": "dir", "mtime_ns": 2},
                {"name": "recent.txt", "path": "recent.txt", "type": "file", "mtime_ns": 3},
            ])
            page.locator("#btnWorkspacePrefs").evaluate("el => { el.style.position = 'fixed'; el.style.top = '430px'; el.style.right = '8px'; }")
            page.evaluate("_positionWorkspacePrefsMenu(_workspacePrefsAnchor)")
            created = page.locator("#workspaceSort_created-desc")
            assert created.is_disabled()
            assert created.locator("xpath=..").locator(".workspace-prefs-meta").count() == 1
            _assert_menu_inside_viewport(page, menu)
            unavailable_height = menu.bounding_box()["height"]

            page.evaluate("""() => {
                S.entries = [
                  {name:'link',path:'link',type:'symlink',mtime_ns:1,birthtime_ns:1},
                  {name:'Documents',path:'Documents',type:'dir',mtime_ns:2,birthtime_ns:2},
                  {name:'recent.txt',path:'recent.txt',type:'file',mtime_ns:3,birthtime_ns:3}
                ];
                _noteWorkspaceBirthtimeSupport(S.entries);
            }""")
            page.wait_for_function("""() => !document.querySelector('#workspaceSort_created-desc').disabled &&
                !document.querySelector('#workspaceSort_created-desc').closest('.workspace-prefs-item').querySelector('.workspace-prefs-meta')""")
            assert menu.bounding_box()["height"] < unavailable_height
            _assert_menu_inside_viewport(page, menu)
            anchor = page.locator("#btnWorkspacePrefs").bounding_box()
            rect = menu.bounding_box()
            assert rect["y"] == pytest.approx(anchor["y"] - rect["height"] - 6, abs=1)
            context.close()
        finally:
            browser.close()


@pytest.mark.parametrize("locale", ["de", "ru"])
def test_prefs_menu_layout_locales(locale):
    sp = _require_playwright()
    with sp() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            context = browser.new_context(viewport={"width": 480, "height": 320})
            context.add_init_script(f"localStorage.setItem('hermes-lang',{locale!r})")
            page = context.new_page()
            menu = _open_workspace_prefs(page, locale)
            _assert_menu_inside_viewport(page, menu)
            assert menu.locator(".workspace-prefs-name").count() == 5
            context.close()
        finally:
            browser.close()
