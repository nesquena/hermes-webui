"""#5385 / #5539: the mobile approval card must give command/tool approval
options enough height so they don't require scrolling within a cramped popup.

The mobile (@supports height:100dvh) rule caps .approval-inner height; the fix
raises that cap from min(52dvh,360px) to min(60dvh,420px). Source-level guard so
the taller cap can't silently regress.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_mobile_approval_inner_dvh_cap_raised():
    # The dvh-based cap must be the raised value, not the old cramped one.
    assert "max-height:min(60dvh,420px)" in STYLE
    assert "max-height:min(52dvh,360px)}" not in STYLE.replace(" ", "")


def test_mobile_approval_inner_still_scrolls_as_fallback():
    # The non-dvh fallback keeps overflow-y:auto so very tall content still
    # scrolls rather than clipping (the cap is a ceiling, not a fixed height).
    assert "overflow-y:auto" in STYLE
