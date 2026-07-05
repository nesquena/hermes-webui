"""Mobile sidebar header: the close (X) button must visually match the
new-conversation (+) button — same 14px glyph, vertically aligned on the
panel-head row — instead of a larger 18px glyph sitting ~6px lower. Source guard.
Mobile-only (@media max-width:640px); desktop hides the X entirely.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_mobile_close_glyph_matches_plus_button_size():
    # + button glyph is 14px (.panel-head-btn svg{width:14px}); the mobile X must match.
    assert ".panel-head-btn svg{width:14px;height:14px" in STYLE
    assert ".panel-head-btn.mobile-sidebar-close svg{width:14px;height:14px" in STYLE
    # The old oversized 18px glyph must be gone.
    assert ".panel-head-btn.mobile-sidebar-close svg{width:18px" not in STYLE


def test_mobile_close_is_centered_and_keeps_tap_target():
    close_rule = STYLE[STYLE.index(".panel-head-btn.mobile-sidebar-close{"):]
    close_rule = close_rule[: close_rule.index("}") + 1]
    # Glyph centered in the button.
    assert "align-items:center" in close_rule and "justify-content:center" in close_rule
    # 44x44 tap target preserved for mobile touch.
    assert "width:44px!important;height:44px!important" in close_rule
    # Keeps the safe-area offset (so it still clears the notch) — no extra blank
    # drawer padding.
    assert "var(--app-titlebar-safe-top)" in close_rule


def test_mobile_close_hidden_on_desktop():
    # Base (non-media) rule hides the X on desktop — this fix is mobile-only.
    assert ".mobile-sidebar-close{display:none;}" in STYLE
