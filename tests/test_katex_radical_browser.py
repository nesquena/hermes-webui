"""Browser-level check for KaTeX radical color following the message color.

Renders a square root in both dark and light message themes and asserts the
radical SVG's computed fill equals the inherited foreground color of the
equation. Also asserts the MathML accessibility layer stays visually hidden
(sr-only) so its thin system-font radicals can never surface.

Requires playwright + chromium; skipped when unavailable (CI installs it —
see .github/workflows/tests.yml).
"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import pytest

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

REPO = Path(__file__).resolve().parents[1]

_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

_PAGE_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="/static/style.css">
<link rel="stylesheet" href="/static/vendor/katex/0.16.22/katex.min.css">
<style>
  /* style.css transitions theme colors over 150ms; disable so computed-style
     assertions are deterministic without waiting for the fade. */
  * { transition: none !important; }
</style>
</head>
<body>
<div class="msg-body">
  <div id="eq" class="katex-block" data-katex="display"></div>
</div>
<script src="/static/vendor/katex/0.16.22/katex.min.js"></script>
<script>
  var mode = new URLSearchParams(location.search).get('mode') || 'light';
  if (mode === 'dark') document.documentElement.classList.add('dark');
  var src = String.raw`v = \\sqrt{51.15^2 + 11.19^2}`;
  var el = document.getElementById('eq');
  el.textContent = src;
  katex.render(src, el, {displayMode: true, throwOnError: false});
</script>
</body>
</html>
"""


def _require_playwright():
    if sync_playwright is None:
        pytest.skip("playwright is unavailable; run `playwright install chromium`")
    return sync_playwright


def _serve(route):
    """Serve the page and /static/** straight from the repo — no server needed."""
    url = urlparse(route.request.url)
    if url.path in ("/", "/test.html"):
        route.fulfill(status=200, content_type="text/html", body=_PAGE_HTML)
        return
    path = (REPO / url.path.lstrip("/")).resolve()
    try:
        path.relative_to(REPO)
    except ValueError:
        route.fulfill(status=404, body="outside repo")
        return
    if path.is_file():
        route.fulfill(
            status=200,
            content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            path=str(path),
        )
    else:
        route.fulfill(status=404, body="not found")


def _open_page(pw, mode):
    browser = pw.chromium.launch(headless=True, args=_BROWSER_ARGS)
    try:
        page = browser.new_page(viewport={"width": 900, "height": 260})
        page.route("**/*", _serve)
        page.goto(f"http://katex.test/test.html?mode={mode}", wait_until="networkidle")
        page.wait_for_selector(".katex svg")
        return browser, page
    except Exception:
        browser.close()
        raise


def _radical_fill(page):
    return page.evaluate(
        "getComputedStyle(document.querySelector('.katex svg')).fill"
    )


def _equation_color(page):
    return page.evaluate("getComputedStyle(document.querySelector('.katex')).color")


def test_radical_follows_message_color_in_dark_and_light():
    """The radical SVG's computed fill must track the inherited message color
    in both themes — the browser-level guarantee that the √ stays visible."""
    sp = _require_playwright()
    with sp() as pw:
        browser, page = _open_page(pw, "dark")
        try:
            dark_fill = _radical_fill(page)
            dark_color = _equation_color(page)
            assert dark_fill, "radical SVG has no computed fill"
            assert dark_fill == dark_color, (
                f"radical fill {dark_fill} != message color {dark_color}"
            )

            # Flip to the light theme: the fill must follow the new inherited color.
            page.evaluate("document.documentElement.classList.remove('dark')")
            light_fill = _radical_fill(page)
            light_color = _equation_color(page)
            assert light_fill, "radical SVG has no computed fill in light mode"
            assert light_fill == light_color, (
                f"light radical fill {light_fill} != message color {light_color}"
            )
            assert light_fill != dark_fill, (
                "radical fill did not change with the theme"
            )
        finally:
            browser.close()


def test_mathml_layer_is_visually_hidden():
    """The MathML accessibility layer must be pinned to the sr-only pattern so
    its thin system-font radicals can never surface visually."""
    sp = _require_playwright()
    with sp() as pw:
        browser, page = _open_page(pw, "dark")
        try:
            page.wait_for_selector(".katex-mathml")
            info = page.evaluate(
                """() => {
                  const el = document.querySelector('.katex-mathml');
                  const cs = getComputedStyle(el);
                  return {position: cs.position, width: cs.width,
                          height: cs.height, clipPath: cs.clipPath};
                }"""
            )
            assert info["position"] == "absolute", info
            assert info["width"] == "1px" and info["height"] == "1px", info
            assert "inset(50%)" in info["clipPath"], info
        finally:
            browser.close()
