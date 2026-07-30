"""Verify #updateBanner lives outside #mainChat so it is visible from any panel."""
import pathlib

INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"


def test_update_banner_outside_main_chat():
    src = INDEX.read_text(encoding="utf-8")
    banner_pos = src.find('id="updateBanner"')
    main_chat_pos = src.find('id="mainChat"')
    assert banner_pos != -1, "#updateBanner not found in index.html"
    assert main_chat_pos != -1, "#mainChat not found in index.html"
    assert banner_pos < main_chat_pos, (
        "#updateBanner must appear before #mainChat in the DOM "
        "so it is not hidden when non-Chat panels are active"
    )


def test_update_banner_inside_main_element():
    src = INDEX.read_text(encoding="utf-8")
    main_pos = src.find('<main class="main">')
    main_end = src.find('</main>')
    banner_pos = src.find('id="updateBanner"')
    assert main_pos != -1, "<main class='main'> not found"
    assert main_end != -1, "</main> not found"
    assert main_pos < banner_pos < main_end, (
        "#updateBanner must be inside <main class='main'>, "
        "not before it or after the closing </main>"
    )
