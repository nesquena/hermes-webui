#!/usr/bin/env python3
"""#7616 clarify field must grow on a PHONE too (#8304 gate, finding 1).

The free-text answer field auto-grows with its content. Under
``@media (max-width:640px)`` the response row becomes a COLUMN flexbox
(``.clarify-response{flex-direction:column}``), so the base ``.clarify-input``
rule's ``flex:1`` (style.css:2080) resolves on the HEIGHT axis and collapses the
flex-basis to 0. A zero basis beats the inline ``height`` that
``_autoResizeClarifyInput()`` writes, so on a phone the field never grew: a
726-character answer stayed 46 px tall with a scroll height over 600, and the
original #7616 complaint was still there on exactly the devices that have the
least room.

This is a real-browser layout assertion, not a source check: it runs the
PRODUCTION ``_autoResizeClarifyInput`` against the PRODUCTION stylesheet and
measures the rendered box at 390 px and 640 px (inside the media query) and at
641 px (outside it, the already-working desktop case).

Fails on the reviewed head with: at 390 px the rendered height stayed at the
one-line 46 px while scrollHeight reported the full 726-character answer.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STYLE_CSS = ROOT / "static" / "style.css"
MESSAGES_JS = ROOT / "static" / "messages.js"

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to extract the production resize helper"
)

LONG_ANSWER = "\n".join(
    f"line {i}: a clarification answer that runs long enough to need more than one row"
    for i in range(12)
)


def _extract_function(src: str, name: str) -> str:
    """Return one top-level ``function name(...) { ... }`` definition verbatim."""
    marker = f"function {name}("
    start = src.find(marker)
    if start == -1:
        raise AssertionError(f"production function {name} not found in messages.js")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _harness_js() -> str:
    src = MESSAGES_JS.read_text(encoding="utf-8")
    parts = [
        _extract_function(src, "_clarifyFieldIsHidden"),
        _extract_function(src, "_autoResizeClarifyInput"),
    ]
    constants = [
        line for line in src.splitlines() if line.startswith("const CLARIFY_INPUT_")
    ]
    if not constants:
        raise AssertionError("CLARIFY_INPUT_* constants not found in messages.js")
    return "\n".join(constants) + "\n" + "\n".join(parts)


def _render_height(page, viewport_width: int) -> dict:
    """Lay the card out at ``viewport_width`` and return the measured geometry."""
    page.set_viewport_size({"width": viewport_width, "height": 844})
    page.evaluate("() => document.getElementById('clarifyInput').value = window.__ANSWER__")
    # The production resize path: measure with height:auto, then write the px.
    page.evaluate("() => window._autoResizeClarifyInput()")
    return page.evaluate(
        """() => {
            const input = document.getElementById('clarifyInput');
            const cs = getComputedStyle(input);
            return {
              rendered: input.getBoundingClientRect().height,
              scrollHeight: input.scrollHeight,
              inlineHeight: input.style.height,
              flexBasis: cs.flexBasis,
              flexGrow: cs.flexGrow,
            };
        }"""
    )


@pytest.fixture(scope="module")
def clarify_page():
    css = STYLE_CSS.read_text(encoding="utf-8")
    harness = _harness_js()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(
            """<!doctype html><html><head><style>%s</style></head><body>
            <div class="clarify-card visible"><div class="clarify-inner">
              <div class="clarify-response" id="clarifyResponse">
                <textarea class="clarify-input" id="clarifyInput"></textarea>
                <button class="clarify-submit" id="clarifySubmit">Send</button>
              </div>
            </div></div>
            <script>window.__ANSWER__ = %s;</script>
            <script>%s</script>
            <script>
              // Production helpers expect these globals; supply the minimum.
              window.requestAnimationFrame = (cb) => cb();
              window.setTimeout = setTimeout;
              window._syncClarifyTranscriptSpace = () => {};
              window.$ = (id) => document.getElementById(id);
            </script>
            </body></html>"""
            % (css, json.dumps(LONG_ANSWER), harness)
        )
        yield page
        browser.close()


@pytest.mark.parametrize("width", [390, 640])
def test_clarify_field_grows_at_phone_widths(clarify_page, width):
    """Inside the mobile media query the field must honour the inline height."""
    geom = _render_height(clarify_page, width)

    assert geom["scrollHeight"] > 60, (
        f"precondition: the long answer must actually overflow one row "
        f"(scrollHeight={geom['scrollHeight']})"
    )
    assert geom["inlineHeight"] != "", "the resize helper must write an inline height"
    assert geom["flexBasis"] == "auto", (
        f"at {width}px .clarify-input must have a content-sized flex-basis so the "
        f"inline height applies; computed flex-basis is {geom['flexBasis']!r} "
        f"(a zero basis resolves on the height axis and overrides the inline height)"
    )
    # The whole point: the RENDERED box grew past the single-row minimum.
    assert geom["rendered"] > 60, (
        f"at {width}px the field must visibly grow for a long answer, but it "
        f"rendered at {geom['rendered']:.1f}px with a scroll height of "
        f"{geom['scrollHeight']}px — the answer is there but the user can only "
        f"see the first line (inline height was {geom['inlineHeight']})"
    )


def test_clarify_field_still_grows_above_the_breakpoint(clarify_page):
    """641 px is outside the media query and already worked — it must stay fixed."""
    geom = _render_height(clarify_page, 641)
    assert geom["scrollHeight"] > 60, "precondition: the answer must overflow one row"
    assert geom["rendered"] > 60, (
        f"at 641px the field rendered at {geom['rendered']:.1f}px for a "
        f"{geom['scrollHeight']}px answer"
    )