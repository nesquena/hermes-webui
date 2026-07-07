"""Regression tests for the shared layout-assertion helpers."""
from __future__ import annotations

import pytest

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

from tests._layout_helpers import (
    assert_layout_sane,
    assert_no_raw_i18n_keys,
    collect_layout_violations,
)


def _require_playwright():
    if sync_playwright is None:
        pytest.skip("playwright is unavailable; run `playwright install chromium`")
    return sync_playwright


_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


_SCOPE_FIXTURE_HTML = """\
<!doctype html>
<html><head><meta charset="utf-8" />
<style>
  body { margin: 0; overflow: hidden; }
  .layout { display: flex; width: 100%; }
  main.main { flex: 1; }
  aside.rightpanel { width: 300px; position: absolute; right: -300px; overflow: hidden; }
</style></head>
<body>
  <div class="layout">
    <main class="main">
      <button>New chat</button>
      <div>Content</div>
    </main>
    <aside class="rightpanel">
      <button id="btnNewFile">New</button>
      <button id="btnRefresh">Refresh</button>
    </aside>
  </div>
</body></html>"""


_OVERLAP_FIXTURE_HTML = """\
<!doctype html>
<html><head><meta charset="utf-8" />
<style>
  body { margin: 0; }
  .row { display: flex; width: 200px; }
  .label { white-space: nowrap; margin-right: -80px; }
  .value { flex: 1 1 auto; }
</style></head>
<body>
  <div class="row">
    <div class="label">A very long unbreakable column label</div>
    <div class="value">Value text</div>
  </div>
</body></html>"""


_RAW_STRING_FIXTURE_HTML = """\
<!doctype html>
<html><head><meta charset="utf-8" /></head>
<body>
  <span>workspace_hidden_files_visible</span>
</body></html>"""


def test_layout_sane_on_master_pages():
    """Clean-master assertion: the real app has zero violations in the safe scope."""
    sp = _require_playwright()
    from tests._pytest_port import BASE

    # degenerate excluded: the composer has two intentional off-screen a11y
    # proxies (fileInput, modelSelect) that are not layout bugs
    _LIVE_CHECKS = ["overlap", "clip", "container-escape", "raw-string"]

    with sp() as pw:
        browser = pw.chromium.launch(headless=True, args=_BROWSER_ARGS)
        try:
            for path in ["/", "/#settings", "/#sessions"]:
                ctx = browser.new_context(viewport={"width": 1280, "height": 720})
                page = ctx.new_page()
                page.goto(BASE + path, wait_until="domcontentloaded")
                page.wait_for_selector("#msg, .app, body", timeout=10000)
                assert_layout_sane(page, scope_selector=".layout > main", checks=_LIVE_CHECKS)
                ctx.close()
        finally:
            browser.close()


def test_default_scope_vs_full_body():
    """Scoping to main.main excludes collapsed rightpanel noise; body scope catches it."""
    sp = _require_playwright()
    with sp() as pw:
        browser = pw.chromium.launch(headless=True, args=_BROWSER_ARGS)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.set_content(_SCOPE_FIXTURE_HTML)
            assert_layout_sane(page, scope_selector="main.main")
            violations = collect_layout_violations(page, scope_selector="body")
            assert violations, "expected violations from collapsed rightpanel under body scope"
        finally:
            browser.close()


def test_injected_overlap_detected():
    """A flex row where a nowrap label's negative margin pulls it over its sibling's box."""
    sp = _require_playwright()
    with sp() as pw:
        browser = pw.chromium.launch(headless=True, args=_BROWSER_ARGS)
        try:
            page = browser.new_page(viewport={"width": 320, "height": 200})
            page.set_content(_OVERLAP_FIXTURE_HTML)
            violations = collect_layout_violations(page, checks=["overlap"])
            overlap = [v for v in violations if v["type"] == "overlap"]
            assert overlap, "expected an overlap violation from the unshrinkable flex column"
        finally:
            browser.close()


def test_raw_i18n_key_detected():
    """A raw snake_case i18n key rendered as text should fail the raw-string check."""
    sp = _require_playwright()
    with sp() as pw:
        browser = pw.chromium.launch(headless=True, args=_BROWSER_ARGS)
        try:
            page = browser.new_page(viewport={"width": 320, "height": 200})
            page.set_content(_RAW_STRING_FIXTURE_HTML)
            with pytest.raises(AssertionError):
                assert_no_raw_i18n_keys(page)
        finally:
            browser.close()
