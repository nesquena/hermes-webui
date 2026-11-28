"""Browser-level pin for the #7303 context-disclosure focus ring.

The 9/21 review asked for "a source or browser assertion that the summary
has a visible focus style". The source-level pin
(``test_issue7303_re_gate_9_21_nested_html_and_focus.py``) proves the
``:focus-visible`` rule text exists in ``static/style.css``, but it cannot
see a rule overridden by a later or higher-specificity declaration, a
``--focus-ring`` token that resolves to a transparent value, or a class
drift between the renderer and the stylesheet. Those failures keep the
regex green while the ring never paints.

This test runs the production disclosure-building block extracted verbatim
from ``static/panels.js`` inside headless Chromium with the real
``static/style.css`` loaded, moves focus to the summary with real Tab
keypresses, and asserts the computed outline. A deleted or overridden rule
turns it red.

Skips when Playwright is unavailable (same contract as
``test_session_action_menu_focus.py``).
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest


REPO = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _disclosure_block_source() -> str:
    """Extract the production ``if (parsed.context) { ... }`` block that
    builds ``<details class="cron-run-context-disclosure">``.

    The block is brace-matched out of the shipped renderer so the test
    executes the real construction (class names included) instead of a
    re-implementation that could drift from it.
    """
    start = PANELS_JS.index(
        "    if (parsed.context) {\n      const details = document.createElement('details');"
    )
    assert "cron-run-context-disclosure" in PANELS_JS[start : start + 400]
    brace = PANELS_JS.index("{", start)
    depth = 0
    index = brace
    while index < len(PANELS_JS):
        char = PANELS_JS[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return PANELS_JS[start : index + 1]
        index += 1
    raise AssertionError("disclosure block did not close")


def _page_script() -> str:
    """Stubs + the production disclosure block, mounted in a live page."""
    context_text = (
        "# Cron Job: nightly-backup\\n\\n"
        "**Job ID:** nightly-backup\\n\\n"
        "Prompt line one.\\n"
        "Prompt line two with a <pre> sample.\\n"
        "Skill text line three.\\n"
        "Intermediate tool output line four."
    )
    return "\n".join(
        [
            # Minimal globals the extracted block expects.
            "const t = (key) => ({",
            "  cron_run_show_prompt_context: 'Show prompt & context',",
            "}[key] || key);",
            "const parsed = {",
            f"  context: '{context_text}',",
            "  response: 'Backup completed for 3 volumes.',",
            "};",
            # A focusable control BEFORE the disclosure so a real Tab press
            # lands keyboard focus on the summary. The row structure mirrors
            # production: the run body only displays inside an open row
            # (.detail-run-item.open .detail-run-body in style.css).
            "const rowItem = document.createElement('div');",
            "rowItem.className = 'detail-run-item open';",
            "document.body.appendChild(rowItem);",
            "const beforeBtn = document.createElement('button');",
            "beforeBtn.textContent = 'before';",
            "rowItem.appendChild(beforeBtn);",
            "const body = document.createElement('div');",
            "body.className = 'detail-run-body';",
            "rowItem.appendChild(body);",
            # ---- the production block, verbatim ----
            _disclosure_block_source(),
            "window.__disclosure = body.querySelector('.cron-run-context-disclosure');",
            "window.__summary = window.__disclosure.querySelector('summary');",
            "window.__summaryStyle = () => {",
            "  const s = getComputedStyle(window.__summary);",
            "  return {",
            "    outlineStyle: s.outlineStyle,",
            "    outlineWidth: s.outlineWidth,",
            "    outlineColor: s.outlineColor,",
            "    activeTag: document.activeElement.tagName,",
            "    activeClass: document.activeElement.className,",
            "    ariaLabel: window.__summary.getAttribute('aria-label'),",
            "  };",
            "};",
        ]
    )


def test_context_disclosure_summary_has_visible_focus_ring_in_browser():
    """The summary of the run-detail context disclosure must paint a
    visible focus ring under keyboard focus, proven by computed style in
    real Chromium with the real stylesheet, and Enter must open the
    disclosure with the full context mounted."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the browser focus test with playwright installed")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.set_content("<!doctype html><html><body></body></html>")
        page.add_style_tag(content=STYLE_CSS)
        page.evaluate(_page_script())

        # Real Tab keypresses: trusted input, so Chromium's own sequential
        # focus navigation and :focus-visible matching apply.
        page.evaluate("document.querySelector('button').focus()")
        for _ in range(3):
            if page.evaluate("document.activeElement === window.__summary"):
                break
            page.keyboard.press("Tab")
            page.wait_for_timeout(20)
        focused = page.evaluate("document.activeElement === window.__summary")
        assert focused, "Tab keypresses must move keyboard focus to the disclosure summary"

        style = page.evaluate("window.__summaryStyle()")
        assert style["activeTag"] == "SUMMARY", style
        assert style["activeClass"] == "", (
            f"the focused element must be the bare <summary> inside the disclosure: {style}"
        )
        assert style["ariaLabel"], f"the summary must keep its aria-label: {style}"
        assert style["outlineStyle"] not in ("none", ""), (
            "keyboard focus on the context disclosure summary must paint a focus ring; "
            f"computed outline-style was {style['outlineStyle']!r} — the :focus-visible rule is "
            "missing, overridden, or resolves to no outline"
        )
        width_px = float(re.sub(r"[^0-9.]", "", style["outlineWidth"]) or 0)
        assert width_px >= 2, f"the focus ring must be at least 2px wide (computed {style['outlineWidth']!r})"
        assert style["outlineColor"] not in ("rgba(0, 0, 0, 0)", "transparent"), (
            f"the focus ring must be visible (computed color {style['outlineColor']!r})"
        )

        # Keyboard operability + full reachability: Enter on the focused
        # summary opens the disclosure and the context text is mounted.
        page.keyboard.press("Enter")
        page.wait_for_timeout(20)
        opened = page.evaluate(
            "(() => {"
            "  const pre = window.__disclosure.querySelector('.cron-run-context-pre code');"
            "  return {"
            "    open: window.__disclosure.open,"
            "    contextText: pre ? pre.textContent : null,"
            "  };"
            "})()"
        )
        assert opened["open"] is True, "Enter on the summary must open the disclosure"
        assert opened["contextText"] and "nightly-backup" in opened["contextText"], (
            f"the pre-response context must stay fully reachable inside the disclosure: {opened}"
        )
        browser.close()


def test_disclosure_block_source_is_pinned():
    """Guard the extraction itself: the renderer must still build the
    disclosure with the class the stylesheet targets."""
    block = _disclosure_block_source()
    assert "cron-run-context-disclosure" in block
    assert "createElement('details')" in block
    assert "createElement('summary')" in block
    assert "cron-run-context-pre" in block
    # The stylesheet must target the same class the renderer builds.
    assert ".cron-run-context-disclosure>summary:focus-visible" in STYLE_CSS
