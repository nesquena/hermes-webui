from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _compact(text: str) -> str:
    return "".join(text.split())


def test_clarify_card_reserves_transcript_space():
    compact_css = _compact(STYLE_CSS)

    assert ".messages.clarify-open" in STYLE_CSS
    assert "padding-bottom:var(--clarify-card-height,320px)" in compact_css
    assert "scroll-padding-bottom:var(--clarify-card-height,320px)" in compact_css


def test_clarify_collapse_uses_smaller_transcript_space():
    compact_css = _compact(STYLE_CSS)
    compact_js = _compact(MESSAGES_JS)

    assert ".messages.clarify-collapsed" in STYLE_CSS
    assert "padding-bottom:var(--clarify-dock-height,72px)" in compact_css
    assert 'classList.toggle("clarify-collapsed",collapsed)' in compact_js
    assert "--clarify-dock-height" in MESSAGES_JS


def test_clarify_show_hide_toggle_messages_padding_classes():
    compact_js = _compact(MESSAGES_JS)

    assert "_syncClarifyTranscriptSpace(card,{immediate:true})" in compact_js
    assert "_syncClarifyTranscriptSpace(null)" in compact_js
    assert 'classList.add("clarify-open")' in MESSAGES_JS
    assert 'classList.remove("clarify-open")' in MESSAGES_JS
    assert "--clarify-card-height" in MESSAGES_JS


def test_clarify_padding_remeasures_on_resize():
    compact_js = _compact(MESSAGES_JS)

    assert "function_ensureClarifyResizeListener()" in compact_js
    assert 'window.addEventListener("resize"' in MESSAGES_JS
    assert "_ensureClarifyResizeListener();" in MESSAGES_JS
