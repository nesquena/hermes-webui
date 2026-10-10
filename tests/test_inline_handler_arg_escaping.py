"""Inline on* handler arguments must go through jsArg(), never esc() alone.

esc() is HTML-only escaping. The browser decodes entities in the attribute
value BEFORE the inline handler's JavaScript is parsed, so an argument
interpolated as onclick="fn('${esc(value)}')" lets a quote in the value
break out of the JS string literal (the #3797 bug class). Kanban dependency
buttons fixed this locally as _kanbanJsArg (#3797); that helper is promoted
to a shared jsArg() in static/ui.js. The Playwright test proves the
breakout and the round-trip fix; the source guard keeps the pattern from
coming back.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# on*="...'...${esc(x)}...'..." — esc()-only encoding used as a quoted JS
# string argument inside an inline handler (wrong escaping context).
_ESC_ONLY_INLINE_ARG = re.compile(r"""on[a-z]+\s*=\s*\"[^\"]*'\s*\$\{esc\(""")

_FIRST_PARTY_JS = sorted(
    p.name for p in (ROOT / "static").glob("*.js") if not p.name.endswith(".min.js")
)


def test_no_esc_only_inline_handler_args_remain():
    offenders = {}
    for name in _FIRST_PARTY_JS:
        src = (ROOT / "static" / name).read_text(encoding="utf-8")
        lines = [
            i + 1
            for i, line in enumerate(src.splitlines())
            if _ESC_ONLY_INLINE_ARG.search(line)
        ]
        if lines:
            offenders[name] = lines
    assert not offenders, f"esc()-only inline on* args found (use jsArg()): {offenders}"


def test_jsarg_inline_handler_round_trip():
    pw = pytest.importorskip("playwright.sync_api")
    from tests.js_source_extract import extract_function

    ui_src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    esc_line = re.search(r"^const esc=.*$", ui_src, re.M)
    assert esc_line, "esc() definition not found in static/ui.js"
    harness = "\n".join(
        [
            esc_line.group(0),
            extract_function(ui_src, "jsArg"),
            "window.__calls = [];",
            "window.__alerts = [];",
            "window.alert = (m) => window.__alerts.push(String(m));",
            "window.loadKanbanTask = (id) => window.__calls.push(id);",
        ]
    )
    with pw.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
        except Exception as exc:  # pragma: no cover - no browser binary
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            page = browser.new_page()
            page.set_content("<!doctype html><html><body></body></html>")
            page.add_script_tag(content=harness)
            result = page.evaluate(
                r"""() => {
                    const payload = "');alert('pwned');//";
                    // Pre-fix pattern: esc()-only argument in a quoted inline handler.
                    document.body.innerHTML =
                      '<button id="escBtn" onclick="loadKanbanTask(\'' + esc(payload) + '\')">x</button>';
                    document.getElementById('escBtn').click();
                    const escFormAlerts = window.__alerts.slice();
                    const escFormCalls = window.__calls.slice();
                    window.__alerts.length = 0;
                    // Fixed pattern: jsArg() argument, no manual quotes.
                    document.body.innerHTML =
                      '<button id="jsBtn" onclick="loadKanbanTask(' + jsArg(payload) + ')">x</button>';
                    document.getElementById('jsBtn').click();
                    return {
                        escFormAlerts,
                        escFormCalls,
                        jsFormAlerts: window.__alerts.slice(),
                        jsFormCall: window.__calls[window.__calls.length - 1],
                        payload,
                    };
                }"""
            )
        finally:
            browser.close()

    # esc()-only demonstrably breaks out of the handler string: injected code
    # runs and the received argument is corrupted.
    assert result["escFormAlerts"] == ["pwned"]
    assert result["escFormCalls"] == [""]
    # jsArg() fires no injected code and round-trips the payload exactly.
    assert result["jsFormAlerts"] == []
    assert result["jsFormCall"] == result["payload"]
