from pathlib import Path
import re


REPO = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _locale_blocks(src: str) -> dict[str, str]:
    matches = list(
        re.finditer(
            r"\n  (?:(['\"])([A-Za-z][A-Za-z0-9-]*)\1|([A-Za-z][A-Za-z0-9-]*)): \{",
            src,
        )
    )
    blocks: dict[str, str] = {}
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else src.rfind("\n};")
        blocks[match.group(2) or match.group(3)] = src[start:end]
    return blocks


def test_selected_text_reply_button_is_selection_scoped_and_frontend_only():
    js = read("static/messages.js")

    assert "window.getSelection" in js
    assert "selection.isCollapsed" in js
    assert "range.getBoundingClientRect" in js
    assert "_selectedTextReplyRoot" in js
    assert "$('messages')||$('msgInner')" in js
    assert "root.contains(el)" in js
    assert "document.addEventListener('selectionchange', _updateSelectedTextReplyButton)" in js

    assert "id='selectedTextReplyBtn'" in js
    assert "selected-text-reply-btn" in js
    assert "data-i18n', 'selected_text_reply'" in js
    assert "data-i18n-title', 'selected_text_reply_title'" in js
    assert "data-i18n-aria-label', 'selected_text_reply_title'" in js

    # MVP contract: selected text reply is entirely static/frontend; do not add
    # backend endpoints or change send payload routing.
    assert "/api/selected" not in js
    assert "selected_text" not in js.replace("selected_text_reply", "")


def test_selected_text_reply_appends_blockquote_and_preserves_draft_flow():
    js = read("static/messages.js")

    assert "function _formatSelectedTextReplyQuote" in js
    assert "replace(/\\r\\n?/g,'\\n')" in js
    assert "replace(/\\n{3,}/g,'\\n\\n')" in js
    assert "map(line=>`> ${line}`).join('\\n')" in js

    assert "function _appendSelectedTextReplyToComposer" in js
    assert "$('msg')" in js
    assert "current.trim()?" in js
    assert "${quote}\\n\\n" in js
    assert "composer.dispatchEvent(new Event('input', {bubbles:true}))" in js
    assert "if(typeof autoResize==='function') autoResize()" in js


def test_selected_text_reply_styles_and_i18n_exist_for_all_locales():
    css = read("static/style.css")
    i18n = read("static/i18n.js")

    assert ".selected-text-reply-btn" in css
    assert ".selected-text-reply-btn.visible" in css
    assert "position:fixed" in css
    assert "pointer-events:none" in css
    assert "pointer-events:auto" in css
    assert "border:2px solid var(--accent)" in css
    assert "background:var(--bg)" in css
    assert "color:var(--text)" in css
    assert "outline:2px solid var(--focus-ring)" in css

    blocks = _locale_blocks(i18n)
    assert blocks, "No locale blocks found"
    assert "zh-Hant" in blocks, "Locale parser must include quoted script locales"
    required = {
        "selected_text_reply",
        "selected_text_reply_title",
        "selected_text_reply_appended",
    }
    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    for locale, block in blocks.items():
        keys = set(key_pattern.findall(block))
        missing = sorted(required - keys)
        assert not missing, f"{locale} missing selected-text reply keys: {missing}"
