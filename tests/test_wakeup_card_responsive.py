"""#6350 review finding 3 — process-wakeup card must not clip on narrow viewports.

Real-browser measurement (Playwright) of the collapsed summary row for a
worst-case long watch pattern at 390px (mobile) and 820px (tablet):

  * the summary must not overflow the card horizontally (no clipped chip); and
  * the disclosure target must be >= 44px tall under the mobile breakpoint.

The production ``_processWakeupCardHtml`` source and the real wakeup CSS block
are injected into a minimal page, so the measurement exercises the shipped
markup + styles, not a hand-built approximation. Skips cleanly where Playwright
or a browser binary is unavailable (matching the repo's other browser tests).
"""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

LONG_PATTERN = "ERROR.*(timeout|refused|reset|unavailable).*after [0-9]+ retries over [0-9]+ seconds"


def _extract_func(name: str) -> str:
    start = UI_JS.find(f"function {name}")
    assert start != -1, f"{name} not found"
    brace = UI_JS.index("{", start)
    depth = 0
    for i in range(brace, len(UI_JS)):
        if UI_JS[i] == "{":
            depth += 1
        elif UI_JS[i] == "}":
            depth -= 1
            if depth == 0:
                return UI_JS[start:i + 1]
    raise AssertionError(f"{name} body did not close")


def _fixture_script() -> str:
    return "\n".join(
        [
            "function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;');}",
            "function li(name,size){return '<svg data-icon=\"'+name+'\" style=\"width:11px;height:11px\"></svg>';}",
            "function t(k){return k==='process_wakeup_label'?'Background wakeup':(k==='process_wakeup_matched'?'Watch pattern matched':k);}",
            _extract_func("_parseProcessWakeupBody"),
            _extract_func("_processWakeupInfo"),
            _extract_func("_processWakeupCardHtml"),
            """
            window.__renderWakeupCard = (pattern) => {
              const body = '[IMPORTANT: Background process w1 matched watch pattern "' + pattern
                + '".\\nCommand: tail -f /var/log/app.log\\nMatched output:\\nERROR connection timeout]';
              const info = _processWakeupInfo({}, body);
              const extras = {timeHtml: '<span class="msg-time">14:32</span>', filesHtml: '', footHtml: ''};
              const wrap = document.createElement('div');
              wrap.className = 'process-wakeup-notice process-wakeup-notice-card';
              wrap.innerHTML = _processWakeupCardHtml(info, body, extras);
              document.body.appendChild(wrap);
              const card = wrap.querySelector('details.process-wakeup-card');
              const summary = wrap.querySelector('summary.process-wakeup-summary');
              const r = summary.getBoundingClientRect();
              return {
                summaryScrollWidth: summary.scrollWidth,
                summaryClientWidth: summary.clientWidth,
                cardClientWidth: card.clientWidth,
                summaryHeight: Math.round(r.height),
                docScrollWidth: document.documentElement.scrollWidth,
                docClientWidth: document.documentElement.clientWidth,
              };
            };
            """,
        ]
    )


def _measure(viewport_width: int):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the wakeup-card responsive browser test")

    playwright = sync_playwright().start()
    # Only a missing/unlaunchable browser binary should skip; everything after a
    # successful launch (page setup, the measurement itself) must fail loudly so
    # a real regression can't hide behind a skip.
    try:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    except Exception as exc:  # pragma: no cover - no browser binary in sandbox
        playwright.stop()
        pytest.skip(f"chromium unavailable for browser measurement: {exc}")

    try:
        page = browser.new_page(viewport={"width": viewport_width, "height": 844})
        page.set_content("<!doctype html><html><head></head><body></body></html>")
        page.add_style_tag(content=STYLE_CSS)
        page.add_script_tag(content=_fixture_script())
        result = page.evaluate("(pattern) => window.__renderWakeupCard(pattern)", LONG_PATTERN)
    finally:
        browser.close()
        playwright.stop()
    return result


def test_mobile_390_no_horizontal_overflow_and_44px_target():
    m = _measure(390)
    # No clipped chip: the summary content fits within its own box and the card.
    assert m["summaryScrollWidth"] <= m["summaryClientWidth"] + 1, m
    assert m["summaryClientWidth"] <= m["cardClientWidth"] + 1, m
    # The page itself must not scroll horizontally.
    assert m["docScrollWidth"] <= m["docClientWidth"] + 1, m
    # Disclosure target must meet the 44px mobile minimum.
    assert m["summaryHeight"] >= 44, m


def test_tablet_820_no_horizontal_overflow():
    m = _measure(820)
    assert m["summaryScrollWidth"] <= m["summaryClientWidth"] + 1, m
    assert m["docScrollWidth"] <= m["docClientWidth"] + 1, m
