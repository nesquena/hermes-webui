from pathlib import Path


STYLE = Path(__file__).resolve().parents[1] / "static" / "style.css"


def test_message_body_wraps_long_inline_terms_after_late_layout_override():
    css = STYLE.read_text(encoding="utf-8")

    late_layout_marker = "/* ── Unified indent rail — every child of a turn lines up on --msg-rail ── */"
    late_layout = css.split(late_layout_marker, 1)[1]

    assert ".msg-body {" in late_layout
    assert "max-width: min(var(--msg-max), 100%);" in late_layout
    assert "min-width: 0;" in late_layout
    assert "overflow-wrap: anywhere;" in late_layout
    assert "word-break: break-word;" in late_layout

    # Assistant messages are nested as .assistant-turn > .assistant-turn-blocks >
    # .assistant-segment > .msg-body. Each flex/container layer must be allowed to
    # shrink, otherwise long mixed English/CJK medical terms can force horizontal
    # overflow even when the body itself has overflow-wrap.
    assert ".assistant-turn { width: 100%; min-width: 0; }" in late_layout
    assert ".assistant-turn-blocks { display: flex; flex-direction: column; min-width: 0; }" in late_layout
    assert ".assistant-segment { max-width: 100%; min-width: 0; }" in late_layout

    # Markdown renderers often wrap prose in inline spans/em/strong/a elements;
    # keep the actual prose descendants breakable too, not only the outer body.
    assert ".msg-body :where(p, li, blockquote" in late_layout
