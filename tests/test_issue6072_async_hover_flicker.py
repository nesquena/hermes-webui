"""Regression coverage for #6072 — settled async results re-shading on hover."""

from pathlib import Path

import pytest


STYLE_CSS = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_text(
    encoding="utf-8"
)


def _fixture_html() -> str:
    return f"""
<!doctype html>
<html class="dark">
<head>
  <meta charset="utf-8" />
  <style>{STYLE_CSS}</style>
</head>
<body>
  <main>
    <div class="assistant-turn" data-role="assistant">
      <div class="assistant-turn-blocks">
        <div class="assistant-segment" data-msg-idx="2" data-background-result="1">
          <div id="asyncResult" class="msg-body">
            <p><strong>Background task</strong></p>
            <p>The settled async result should keep one paint state on hover.</p>
          </div>
          <div class="msg-foot"><span class="msg-actions">Copy</span></div>
        </div>
      </div>
    </div>
  </main>
</body>
</html>
"""


@pytest.mark.parametrize(
    "viewport",
    (
        {"width": 1280, "height": 720},
        {"width": 800, "height": 700},
        {"width": 390, "height": 844},
    ),
    ids=("desktop", "narrow", "mobile"),
)
def test_settled_async_result_has_no_hover_color_transition(viewport):
    """Hovering a settled async result must not animate its paint properties."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the #6072 browser regression")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport=viewport)
        page.set_content(_fixture_html())

        def paint_state():
            return page.locator("#asyncResult").evaluate(
                """node => {
                  const style = getComputedStyle(node);
                  return {
                    color: style.color,
                    backgroundColor: style.backgroundColor,
                    borderColor: style.borderColor,
                    transitionProperty: style.transitionProperty,
                    transitionDuration: style.transitionDuration,
                    animations: node.getAnimations().length,
                  };
                }"""
            )

        before = paint_state()
        page.hover("#asyncResult")
        during = paint_state()
        page.wait_for_timeout(200)
        after = paint_state()
        browser.close()

    assert before["transitionProperty"] == "none"
    assert before["transitionDuration"] == "0s"
    assert during["animations"] == 0
    assert after["animations"] == 0
    for prop in ("color", "backgroundColor", "borderColor"):
        assert during[prop] == before[prop]
        assert after[prop] == before[prop]
