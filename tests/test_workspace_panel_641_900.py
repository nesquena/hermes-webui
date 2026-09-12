"""
Regression tests: the workspace panel must be openable at 641-900px viewports.

`@media(max-width:900px)` used to set `.rightpanel{display:none}` while the
slide-in overlay only existed in the max-width:640px block. That left the
641-900px band (phone landscape / large phones / narrow desktop windows) with
no way to open the workspace panel at all.

The fix:
  1. Removes `.rightpanel{display:none}` from the 900px block.
  2. Adds an equivalent slide-in overlay for the 641-900px band, with
     `position:fixed!important` so it survives the later
     `@media(min-width:641px) .rightpanel{position:relative}` rule.

Tests below are static-source assertions following the pattern used by the
other workspace-panel regression tests in this directory.
"""
import pathlib

REPO = pathlib.Path(__file__).parent.parent
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


class TestNoDisplayNoneBlindSpot:
    def test_900px_block_does_not_hide_rightpanel(self):
        """The @media(max-width:900px) block must not force .rightpanel
        display:none — that hid the panel for every viewport between 641 and
        900px where the 640px-and-below drawer does not apply."""
        idx = STYLE_CSS.find("@media(max-width:900px){")
        assert idx > 0, "@media(max-width:900px) block not found"
        body = STYLE_CSS[idx:idx + 300]
        assert ".rightpanel{display:none}" not in body, (
            "max-width:900px block must not set .rightpanel{display:none}: "
            "that makes the workspace panel impossible to open at 641-900px"
        )


class TestSlideInOverlayFor641To900:
    def test_overlay_media_query_exists(self):
        """A dedicated 641-900px overlay block must exist."""
        assert "@media(max-width:900px) and (min-width:641px)" in STYLE_CSS, (
            "missing @media(max-width:900px) and (min-width:641px) overlay block"
        )

    def test_overlay_uses_mobile_open_slide_in(self):
        """The 641-900px overlay must slide in via .mobile-open, same as the
        max-width:640px drawer."""
        idx = STYLE_CSS.find("@media(max-width:900px) and (min-width:641px)")
        assert idx > 0, "641-900px overlay block not found"
        block = STYLE_CSS[idx:idx + 700]
        assert "--mobile-rightpanel-width" in block
        assert ".rightpanel.mobile-open{right:0!important" in block, (
            "641-900px overlay must include .rightpanel.mobile-open{right:0!important}"
        )

    def test_position_fixed_is_important(self):
        """position:fixed must carry !important so the later
        @media(min-width:641px) .rightpanel{position:relative} rule (which is
        scoped to the same band) cannot pull the panel back into the document
        flow and squeeze the chat pane."""
        idx = STYLE_CSS.find("@media(max-width:900px) and (min-width:641px)")
        assert idx > 0, "641-900px overlay block not found"
        block = STYLE_CSS[idx:idx + 700]
        assert "position:fixed!important" in block, (
            "641-900px overlay .rightpanel must use position:fixed!important"
        )
