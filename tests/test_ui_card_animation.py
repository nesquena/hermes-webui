import pathlib
import re


def _function_body(source_text, function_name):
    marker = f"function {function_name}("
    start = source_text.index(marker)
    brace_start = source_text.index("{", start)
    depth = 0
    for index in range(brace_start, len(source_text)):
        char = source_text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source_text[start : index + 1]
    raise AssertionError(f"Could not extract {function_name}")


STYLE_CSS = (pathlib.Path(__file__).parent.parent / "static" / "style.css").read_text(encoding="utf-8")
UI_JS = (pathlib.Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")
COMPACT_CSS = re.sub(r"\s+", "", STYLE_CSS)


def test_tool_card_toggle_uses_transformable_layout_and_transition():
    assert ".tool-card-toggle,.tl-caret{" in COMPACT_CSS
    assert "display:inline-flex" in COMPACT_CSS
    assert "transition:transform.18sease" in COMPACT_CSS


def test_tool_card_detail_uses_transitionable_collapsed_state():
    assert ".tool-card-detail,.tl-detail{display:block;max-height:0;opacity:0;overflow:hidden;" in COMPACT_CSS
    assert re.search(
        r"\.tool-card\.open\s+\.tool-card-detail,\s*\.tl\.open\s+\.tl-detail\s*\{[^}]*max-height:\s*320px;[^}]*opacity:\s*1;",
        STYLE_CSS,
    )
    # Open state must set overflow to auto so the inner <pre> scroll is not clipped (#1170).
    assert re.search(
        r"\.tool-card\.open\s+\.tool-card-detail,\s*\.tl\.open\s+\.tl-detail\s*\{[^}]*overflow:\s*auto;",
        STYLE_CSS,
    )


def test_thinking_card_toggle_and_body_use_animation_friendly_state():
    assert ".thinking-card-btn-row{margin-left:auto;display:inline-flex;align-items:center;gap:6px;" in COMPACT_CSS
    assert ".thinking-card-toggle{font-size:10px;display:inline-flex;" in COMPACT_CSS
    assert ".thinking-card-header{display:flex;align-items:center;gap:8px;" in COMPACT_CSS
    # Body uses div default (display:block); canonical rule lives in the
    # consolidated block. Open state caps at 260px (intentional "quieter" sizing).
    assert ".thinking-card-body{max-height:0;opacity:0;overflow:hidden;" in COMPACT_CSS
    assert re.search(
        r"\.thinking-card\.open\s+\.thinking-card-body\s*\{[^}]*max-height:\s*260px;[^}]*opacity:\s*1;",
        STYLE_CSS,
    )


def test_tool_card_toggle_uses_same_chevron_icon_markup_as_thinking_card():
    assert "<span class=\"thinking-card-toggle\">${li('chevron-right',12)}</span>" in UI_JS
    assert "<span class=\"tool-card-toggle\">${li('chevron-right',12)}</span>" in UI_JS
    # ONE contiguous structural assertion, not four free-floating tokens.
    # Those tokens occur 1/7/2/2 times across ui.js, unrelated to each other,
    # so the split version asserted nothing about structure -- which is this
    # test's entire subject. Verified: moving the onclick off the header onto
    # the outer div and deleting the icon span (structurally breaking the very
    # toggle this test is named for) still left the split version passing.
    assert (
        '<div class="${classes}" data-thinking-card="1">'
        '<div class="thinking-card-header" '
        "onclick=\"this.parentElement.classList.toggle('open')\">"
        '<span class="thinking-card-icon">'
    ) in UI_JS


def test_thinking_card_header_includes_copy_button_that_does_not_toggle_card():
    assert "function _copyThinkingText(btn){" in UI_JS
    assert "const copyBtn=`<button class=\"thinking-copy-btn\"" in UI_JS
    assert "event.stopPropagation();_copyThinkingText(this)" in UI_JS
    # Copy must resolve canonical Thinking through identity BEFORE falling
    # back to the possibly-stale _canonicalReasoning expando -- a broad
    # "does this token appear anywhere in the file" check would pass even if
    # _copyThinkingText() read the stale expando first, or never consulted
    # S.messages at all. Assert the actual priority order inside the
    # function itself: the settled-message lookup runs, and is attempted,
    # before the expando fallback is ever consulted.
    copy_thinking_fn = _function_body(UI_JS, "_copyThinkingText")
    assert "S.messages" in copy_thinking_fn, (
        "_copyThinkingText() must resolve canonical reasoning via S.messages identity."
    )
    canonical_idx = copy_thinking_fn.index("_assistantReasoningPayloadText")
    expando_idx = copy_thinking_fn.index("card._canonicalReasoning")
    assert canonical_idx < expando_idx, (
        "The S.messages canonical lookup must be attempted before the "
        "_canonicalReasoning expando fallback, not after -- otherwise a "
        "stale expando can win over the current growing value."
    )
    assert "_copyText(text).then(()=>{" in UI_JS
    assert "btn.innerHTML=li('check',12);" in UI_JS
    assert ".thinking-copy-btn{" in COMPACT_CSS
    assert ".thinking-copy-btn:hover,.thinking-copy-btn:focus-visible{" in COMPACT_CSS


def test_live_thinking_updates_existing_card_body_in_place():
    assert "function _renderThinkingInto(row,text='')" in UI_JS
    assert "row.querySelector('.thinking-card-body pre')" in UI_JS
    assert "pre.textContent=clean" in UI_JS
    assert "_renderThinkingInto(row,text);" in UI_JS


def test_thinking_card_uses_panel_chrome_with_gold_palette():
    # Canonical thinking-card rule lives in the consolidated block (border-radius
    # tightened from 10px → 8px as part of the "quieter card" design pass).
    assert re.search(
        r"\.thinking-card\s*\{[^}]*background:\s*var\(--accent-bg\);[^}]*border:\s*1px\s+solid\s+var\(--accent-bg-strong\);[^}]*border-radius:\s*8px;",
        STYLE_CSS,
    )
    assert "border-left: 2px solid rgba(201,168,76,.4);" not in STYLE_CSS
