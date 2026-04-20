"""Tests for small UX regressions fixed after v0.50.96.

Covers:
  - #633: slash command autocomplete dropdown should be constrained to the
          composer width rather than the full chat panel width.
"""

import pathlib


REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text()


def test_cmd_dropdown_moved_inside_composer_box():
    src = read("static/index.html")
    composer_start = src.index('<div class="composer-box" id="composerBox">')
    dropdown_idx = src.index('<div class="cmd-dropdown" id="cmdDropdown"></div>')
    textarea_idx = src.index('<textarea id="msg"')
    assert composer_start < dropdown_idx < textarea_idx, (
        "cmdDropdown should live inside composerBox, before the textarea, so its "
        "absolute positioning is scoped to the composer instead of the full chat panel"
    )


def test_cmd_dropdown_css_scoped_to_composer_width():
    src = read("static/style.css")
    assert ".cmd-dropdown{display:none;position:absolute;left:0;right:0;" in src, (
        "cmdDropdown should be absolutely positioned with left/right anchors"
    )
    assert "width:auto;max-width:100%;" in src, (
        "cmdDropdown width should be constrained to the positioned composer ancestor"
    )
    assert "bottom:calc(100% + 4px);" in src, (
        "cmdDropdown should sit just above the composer box"
    )
