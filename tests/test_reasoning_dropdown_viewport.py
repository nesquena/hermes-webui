"""#6018 gate blocker 4 — the 9-row reasoning dropdown must keep every row
reachable on short viewports.

The composer reasoning dropdown opens UPWARD (``bottom:calc(100% + 4px)``)
above its chip. With nine rows (Default..Ultra) and no height cap, a short
viewport (e.g. 390×300 landscape keyboard-overlap, or a small phone) pushes
the top rows — including the override-clearing ``Default`` row — above the
viewport with ``overflow:hidden`` and no scroll path to reach them.

Real-browser measurement (Playwright) of the production markup + styles:
the ``#composerReasoningDropdown`` block from ``static/index.html`` is mounted
inside a composer-footer-shaped anchor at the bottom of the page, with the real
``static/style.css`` applied. For desktop (1280×800), mobile portrait
(390×844), and the reviewer's short-landscape case (390×300) we assert:

  * the dropdown fits inside the viewport (no row above y=0);
  * every row — first (``Default``) and last (``Ultra``) — is reachable:
    either directly visible or scrollable-to via ``overflow-y:auto``;
  * after scrolling to top/bottom, both boundary rows are inside the dropdown's
    visible box.

Skips cleanly where Playwright or a chromium binary is unavailable (matching
the repo's other browser tests, e.g. test_wakeup_card_responsive.py).
"""

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _dropdown_markup() -> str:
    """Extract the real #composerReasoningDropdown block from index.html."""
    m = re.search(
        r'<div class="composer-reasoning-dropdown"[^>]*id="composerReasoningDropdown"[^>]*>'
        r".*?</div>\s*</div>",
        INDEX_HTML,
        re.S,
    )
    assert m, "composerReasoningDropdown markup not found in index.html"
    # Trim the trailing sibling-closing </div> captured for a complete block.
    markup = m.group(0)
    assert 'data-effort="ultra"' in markup, "Ultra row missing from dropdown markup"
    assert 'data-effort=""' in markup, "Default row missing from dropdown markup"
    return markup[: markup.rindex("</div>")]


def _reasoning_open_behavior() -> str:
    """Extract the shipped highlight/open/position functions from ui.js."""
    start = UI_JS.index("function _highlightReasoningOption(")
    end = UI_JS.index("function closeReasoningDropdown(", start)
    return UI_JS[start:end]


def _measure(width: int, height: int, selected_effort: str = ""):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the reasoning dropdown viewport test")

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    except Exception as exc:  # pragma: no cover - no browser binary in sandbox
        playwright.stop()
        pytest.skip(f"chromium unavailable for browser measurement: {exc}")

    try:
        page = browser.new_page(viewport={"width": width, "height": height})
        # Composer-footer-shaped anchor pinned to the bottom of the viewport,
        # mirroring the production placement of the upward-opening dropdown.
        page.set_content(
            "<!doctype html><html><head></head><body>"
            '<div class="composer-footer" '
            'style="position:fixed;left:0;right:0;bottom:0;">'
            '<button id="composerReasoningChip" type="button">Reasoning</button>'
            + _dropdown_markup()
            + "</div></body></html>"
        )
        page.add_style_tag(content=STYLE_CSS)
        page.add_script_tag(
            content=(
                "const $=id=>document.getElementById(id);"
                "const closeProfileDropdown=()=>{};"
                "const closeWsDropdown=()=>{};"
                "const closeModelDropdown=()=>{};"
                "const closeToolsetsDropdown=()=>{};"
                f"let _currentReasoningEffort={selected_effort!r};"
                + _reasoning_open_behavior()
            )
        )
        result = page.evaluate(
            """
            () => {
              const dd = document.getElementById('composerReasoningDropdown');
              toggleReasoningDropdown();
              const rows = Array.from(dd.querySelectorAll('.reasoning-option'));
              const first = rows[0];
              const last = rows[rows.length - 1];
              const selected = dd.querySelector('.reasoning-option.selected');
              const style = getComputedStyle(dd);
              const box = () => dd.getBoundingClientRect();
              const inBox = (el) => {
                const b = box();
                const r = el.getBoundingClientRect();
                return r.top >= b.top - 1 && r.bottom <= b.bottom + 1;
              };
              const out = {
                rowCount: rows.length,
                firstLabel: first.textContent.trim(),
                lastLabel: last.textContent.trim(),
                overflowY: style.overflowY,
                menuTop: box().top,
                menuBottom: box().bottom,
                viewportH: window.innerHeight,
                scrollable: dd.scrollHeight > dd.clientHeight + 1,
                selectedEffort: selected && selected.dataset.effort,
                selectedVisibleOnOpen: selected ? inBox(selected) : false,
                selectedTopOnOpen: selected ? selected.getBoundingClientRect().top : null,
                selectedBottomOnOpen: selected ? selected.getBoundingClientRect().bottom : null,
              };
              dd.scrollTop = 0;
              out.firstReachableAtTop = inBox(first);
              out.firstTopAtTop = first.getBoundingClientRect().top;
              dd.scrollTop = dd.scrollHeight;
              out.lastReachableAtBottom = inBox(last);
              out.lastBottomAtBottom = last.getBoundingClientRect().bottom;
              return out;
            }
            """
        )
    finally:
        browser.close()
        playwright.stop()
    return result


def _assert_all_rows_reachable(m):
    # The dropdown itself must sit fully inside the viewport.
    assert m["menuTop"] >= -1, f"dropdown top is above the viewport: {m}"
    assert m["menuBottom"] <= m["viewportH"] + 1, m
    # Nine rows: Default..Ultra.
    assert m["rowCount"] == 9, m
    assert m["firstLabel"] == "Default", m
    assert m["lastLabel"] == "Ultra", m
    # Both boundary rows are reachable: at scrollTop=0 the Default row is
    # inside the dropdown's box AND on-screen; after scrolling to the bottom
    # the Ultra row is inside the box.
    assert m["firstReachableAtTop"], f"Default row unreachable: {m}"
    assert m["firstTopAtTop"] >= -1, f"Default row rendered above the viewport: {m}"
    assert m["lastReachableAtBottom"], f"Ultra row unreachable: {m}"
    assert m["lastBottomAtBottom"] <= m["viewportH"] + 1, m
    # When content is taller than the capped menu, scrolling must be enabled —
    # overflow-y:auto like the sibling model/session dropdowns.
    if m["scrollable"]:
        assert m["overflowY"] in ("auto", "scroll"), (
            f"scrollable dropdown must not clip with overflow:{m['overflowY']}: {m}"
        )


def test_desktop_1280x800_every_row_reachable():
    _assert_all_rows_reachable(_measure(1280, 800))


def test_mobile_portrait_390x844_every_row_reachable():
    _assert_all_rows_reachable(_measure(390, 844))


def test_short_landscape_390x300_default_row_reachable():
    # The reviewer's reproduction: 390×300 previously measured menuTop=-50 with
    # overflow hidden — the Default row sat off-viewport with no scroll path.
    m = _measure(390, 300)
    _assert_all_rows_reachable(m)
    # At this height the 9-row list cannot fit uncapped; the viewport-bounded
    # max-height must engage and hand the overflow to the scroll container.
    assert m["scrollable"], f"expected the height cap to engage at 390x300: {m}"
    assert m["overflowY"] in ("auto", "scroll"), m


@pytest.mark.parametrize("effort", ["", "medium", "ultra"])
def test_short_landscape_selected_row_visible_immediately_on_open(effort):
    """Default, a middle row, and Ultra must open inside the visible menu box."""
    m = _measure(390, 300, selected_effort=effort)
    _assert_all_rows_reachable(m)
    assert m["selectedEffort"] == effort, m
    assert m["selectedVisibleOnOpen"], m
    assert m["selectedTopOnOpen"] >= -1, m
    assert m["selectedBottomOnOpen"] <= m["viewportH"] + 1, m
