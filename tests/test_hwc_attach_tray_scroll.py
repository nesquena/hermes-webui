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


def test_viewport_cap_requires_files_in_the_tray():
    """An empty composer must not inherit the attachment max-height.

    The unscoped ``.composer-box`` cap clipped a long draft's Send button in a
    1100×300 window. The cap belongs only on a tray that has files.
    """
    bare = [
        rule for rule in _rules(".composer-box{")
        if "max-height:min(70dvh," in rule
    ]
    assert bare == [], "viewport max-height must not apply to an empty composer"
    scoped = [
        rule for rule in _rules(".composer-box:has(.attach-tray.has-files){")
        if "max-height:min(70dvh," in rule and "min-height:0" in rule
    ]
    assert len(scoped) == 1, "populated composer is missing its viewport max-height"


def test_short_window_shrinks_the_draft_before_the_footer():
    """The draft yields first. The tray yields only after that floor is hit."""
    marker = "@media (max-height: 599px){"
    idx = CSS.find(marker)
    assert idx >= 0, "short windows have no populated-draft budget"
    block = CSS[idx:CSS.find("\n  .attach-tray.has-files{", idx)]
    assert ".composer-box:has(.attach-tray.has-files) > textarea#msg{" in block
    assert "flex-shrink:1000" in block
    assert block.find("flex-shrink:1000") < block.find("flex-shrink:1;")
    assert "min-height:44px" in block
    assert ".composer-box:has(.attach-tray.has-files) > .attach-tray.has-files{" in block
    assert "flex-shrink:1;" in block
    assert "flex-shrink:0" not in block
    assert ".composer-box > .composer-footer{flex-shrink:0;}" in CSS


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
