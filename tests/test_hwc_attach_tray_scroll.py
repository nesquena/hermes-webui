"""Many attachments must scroll inside the tray so the field and send stay put."""
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_text(encoding="utf-8")

_SHORT = "@media (max-width: 639px), (max-height: 599px) and (pointer: coarse)"


def _rules(selector: str):
    start = 0
    while True:
        idx = CSS.find(selector, start)
        if idx < 0:
            return
        end = CSS.find("}", idx)
        yield CSS[idx:end]
        start = idx + len(selector)


def test_composer_box_caps_height():
    matched = [
        rule for rule in _rules(".composer-box{")
        if "max-height:min(70dvh," in rule and "min-height:0" in rule
    ]
    assert matched, ".composer-box has no viewport max-height"


def test_attach_tray_scrolls_when_it_has_files():
    matched = [
        rule for rule in _rules(".attach-tray.has-files{")
        if "overflow-y:auto" in rule and "max-height:" in rule
    ]
    assert matched, ".attach-tray.has-files does not scroll"


def test_short_or_narrow_screen_keeps_field_and_chip_target():
    idx = CSS.find(_SHORT)
    assert idx >= 0, "missing narrow or short-coarse media query"
    block = CSS[idx:idx + 900]
    assert ".composer-box:has(.attach-tray.has-files) > textarea#msg{max-height:88px;}" in block
    assert ".attach-chip button{min-width:44px;min-height:44px;}" in block
