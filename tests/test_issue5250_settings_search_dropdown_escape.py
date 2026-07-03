"""Regression test for the settings search dropdown escaping settings menu clipping."""

from pathlib import Path

import pytest


STYLE_CSS = (Path(__file__).parent.parent / "static" / "style.css").read_text(
    encoding="utf-8"
)


def _issue_html() -> str:
    item_lines = "".join(
        (
            f'<button type="button" class="settings-search-result" '
            f'onclick="window.__clicked = true">\n'
            f"  <span class='settings-search-label'>Result {i}</span>\n"
            f'  <span class="settings-search-section">Section</span>\n'
            f"</button>\n"
        )
        for i in range(1, 10)
    )
    menu_lines = "".join(
        (
            f'<button type="button" class="side-menu-item" '
            f"data-settings-section='section{i}' "
            f"onclick=\"switchSettingsSection('section{i}',{{fromSidebarItem:true}})\">\n"
            f"  <span>Section {i}</span>\n"
            f"</button>\n"
        )
        for i in range(1, 18)
    )
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <style>
    {STYLE_CSS}
    #settingsMenu {{
      width: 320px;
      height: 150px;
      margin: 12px;
    }}
    body {{ margin: 0; padding: 0; }}
  </style>
</head>
<body>
  <div id="settingsMenu" class="side-menu">
    <div class="settings-search sidebar-search">
      <input id="settingsSearch" value="result"/>
      <div id="settingsSearchResults" class="settings-search-results" style="display:block">
        {item_lines}
      </div>
    </div>
    <div class="settings-menu-items">
      {menu_lines}
    </div>
  </div>
</body>
</html>
"""


def test_issue5250_settings_search_dropdown_escape():
    """A point below the visible menu boundary should land on a search result."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip(
            "playwright is unavailable; run manual local browser hit-test for issue #5250"
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 480, "height": 320})
        page.set_content(_issue_html())
        hit = page.evaluate(
            """
            () => {
              const menu = document.querySelector('#settingsMenu');
              const results = document.querySelector('#settingsSearchResults');
              const menuRect = menu.getBoundingClientRect();
              const pointX = Math.floor(menuRect.left + 22);
              const pointY = Math.floor(menuRect.bottom + 12);
              const target = document.elementFromPoint(pointX, pointY);
              const targetResult = target && target.closest('.settings-search-result');
              const searchRect = results ? results.getBoundingClientRect() : { top: 0, bottom: 0 };
              return {
                menuBottom: Math.floor(menuRect.bottom),
                pointY: pointY,
                resultBottom: Math.floor(searchRect.bottom),
                isResultHit: !!targetResult,
                targetTag: target ? target.tagName : null,
                targetClass: target ? target.className : '',
              };
            }
            """
        )
        browser.close()

    assert hit["isResultHit"], (
        "the dropdown result should be hit-testable below #settingsMenu's visible boundary"
    )
    assert hit["pointY"] > hit["menuBottom"], (
        "the hit-test point should be below the visible settings menu boundary"
    )
    assert hit["resultBottom"] > hit["menuBottom"], (
        "rendered results must extend below #settingsMenu so the regression is meaningful"
    )
