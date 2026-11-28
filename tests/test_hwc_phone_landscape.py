"""A short coarse screen is phone layout even when it is wider than 640px."""

import re
from pathlib import Path

BOOT = (Path(__file__).resolve().parents[1] / "static" / "boot.js").read_text(encoding="utf-8")

_HEIGHT = "(max-height: 600px) and (pointer: coarse)"


def test_height_query_follows_the_first_match_media():
    start = BOOT.index("function _isDesktopWidth")
    body = BOOT[start : BOOT.index("function _isSidebarCollapsed", start)]
    assert _HEIGHT in body
    first = body.index("matchMedia(")
    assert body.index(_HEIGHT) > first
    assert "startBrowserLiveStage" not in body
    found = re.search(
        r"function\s+_isDesktopWidth[^}]*?matchMedia\('([^']+)'\)",
        BOOT,
        re.DOTALL,
    )
    assert found is not None
    assert found.group(1).replace(" ", "") == "(min-width:641px)"
