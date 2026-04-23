"""Regression checks for #856 pinned-star layout in the session list."""

from pathlib import Path


SESSIONS_JS = (Path(__file__).resolve().parent.parent / "static" / "sessions.js").read_text()
STYLE_CSS = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text()


def test_pinned_indicator_renders_inside_title_row():
    title_row_idx = SESSIONS_JS.find("titleRow.className='session-title-row';")
    assert title_row_idx != -1, "session title row construction not found"

    pin_idx = SESSIONS_JS.find("pinInd.className='session-pin-indicator';", title_row_idx)
    assert pin_idx != -1, "pinned indicator creation not found after title row"

    append_to_title_row_idx = SESSIONS_JS.find("titleRow.appendChild(pinInd);", pin_idx)
    assert append_to_title_row_idx != -1, "pinned indicator should be appended to titleRow"

    append_to_el_idx = SESSIONS_JS.find("el.appendChild(pinInd);", pin_idx)
    assert append_to_el_idx == -1, (
        "pinned indicator should not be appended to the outer session row; "
        "it must align inside the title row with the spinner/unread indicator"
    )


def test_pinned_indicator_uses_fixed_indicator_box():
    assert ".session-pin-indicator{" in STYLE_CSS, "session pin indicator CSS block missing"
    css_block = STYLE_CSS[STYLE_CSS.find(".session-pin-indicator{"):STYLE_CSS.find(".session-pin-indicator svg{")]
    assert "width:10px;" in css_block, "pin indicator should reserve a fixed 10px width"
    assert "height:10px;" in css_block, "pin indicator should reserve a fixed 10px height"
    assert "justify-content:center;" in css_block, "pin indicator should center the star inside its box"
