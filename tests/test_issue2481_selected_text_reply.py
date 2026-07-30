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


def test_selected_text_reply_collects_named_context_blocks_without_dumping_into_composer():
    js = read("static/messages.js")

    assert "function _formatSelectedTextReplyQuote" in js
    assert "replace(/\\r\\n?/g,'\\n')" in js
    assert "replace(/\\n{3,}/g,'\\n\\n')" in js
    assert "map(line=>`> ${line}`).join('\\n')" in js

    assert "function _addNamedContextBlock(text)" in js
    assert "function _renderSelectionChips()" in js
    assert "function _flushSelectionBlocksToComposer()" in js
    assert "_pendingSelections.push({id, name, text})" in js
    assert "_addNamedContextBlock(_selectedTextReplyText)" in js
    assert "**${s.name}:**\\n${_formatSelectedTextReplyQuote(s.text)}" in js
    assert "composer.dispatchEvent(new Event('input',{bubbles:true}))" in js
    assert "if(typeof autoResize==='function') autoResize()" in js


def test_selected_text_reply_context_cards_are_built_with_text_nodes():
    js = read("static/messages.js")

    assert "function _selectedContextPreview(text)" in js
    assert "card.className='selection-context-card'" in js
    assert "quote.className='selection-context-quote'" in js
    assert "name.textContent=s.name" in js
    assert "quote.textContent=_selectedContextPreview(s.text)" in js
    assert "quote.title=String(s.text||'')" in js
    assert "remove.addEventListener('click',()=>_removeNamedContextBlock(s.id))" in js
    assert "name.addEventListener('click',()=>_editSelectionChipName(s.id,card))" in js
    assert "e.key==='Enter'||e.key===' '||e.key==='F2'" in js
    assert "context_block_rename_aria" in js
    assert "context_block_remove" in js
    assert "${_selectedTextReplyT('context_block_remove','Remove context block')}: ${s.name}" in js
    assert "quote.title=_selectedContextPreview(s.text)" not in js
    assert "inp.setAttribute('aria-label'" in js
    assert "restoreFocus" in js
    assert "focus({preventScroll:true})" in js
    assert "innerHTML=`<span class=\"selection-chip-name\"" not in js


def test_selected_text_reply_styles_and_i18n_exist_for_all_locales():
    css = read("static/style.css")
    i18n = read("static/i18n.js")

    assert ".selected-text-reply-btn" in css
    assert ".selected-text-reply-btn.visible" in css
    assert ".selection-context-card" in css
    assert ".selection-context-accent" in css
    assert ".selection-context-quote" in css
    assert "-webkit-line-clamp:3" in css
    assert "white-space:pre-wrap" in css
    assert "max-width:clamp(780px,60vw,1100px)" in css
    assert "margin:0 auto" in css
    assert "max-height:min(32vh,280px)" in css
    assert "overflow-y:auto" in css
    assert "scrollbar-gutter:stable" in css
    assert "@media (min-width:1600px){.composer-selection-chips{max-width:1600px;}}" in css
    assert "min-width:28px" in css
    assert "min-height:28px" in css
    assert "min-width:44px" in css
    assert "min-height:44px" in css
    assert ".sent-selection-context" in css
    assert ".sent-selection-context-label" in css
    assert ".sent-selection-context-quote" in css
    ui = read("static/ui.js")
    assert "data-selected-context" in ui
    assert "const stashSelectedContextBlocks=(value)=>" in ui
    assert "<!-- hermes-selected-context -->" in ui
    assert "only blocks carrying the internal marker get custom treatment" in ui
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
        "context_block_rename_hint",
        "context_block_rename_aria",
        "context_block_remove",
    }
    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    for locale, block in blocks.items():
        keys = set(key_pattern.findall(block))
        missing = sorted(required - keys)
        assert not missing, f"{locale} missing selected-text reply keys: {missing}"


def test_selected_text_reply_button_has_user_select_none():
    css = read("static/style.css")
    # The base rule must carry user-select:none so browser selection never
    # renders on or through the button, regardless of hover background opacity.
    assert "user-select:none" in css
    # Confirm it lives inside the .selected-text-reply-btn rule, not elsewhere.
    rule_match = re.search(
        r'\.selected-text-reply-btn\{[^}]*user-select:none[^}]*\}', css
    )
    assert rule_match, ".selected-text-reply-btn base rule must include user-select:none"


def test_sent_selected_context_blocks_are_rendered_without_enabling_user_markdown():
    ui = read("static/ui.js")

    assert "const sentContextHtml=(label,quoteText)=>" in ui
    assert "const stashSelectedContextBlocks=(value)=>" in ui
    assert "<figure class=\"sent-selection-context\" data-selected-context=\"1\">" in ui
    assert "<figcaption class=\"sent-selection-context-label\">" in ui
    assert "<blockquote class=\"sent-selection-context-quote\">" in ui
    assert "${esc(safeLabel)}" in ui
    assert "${esc(safeQuote)}" in ui
    assert "s=esc(s).replace(/\\n/g,'<br>')" in ui
    assert "s=s.replace(/\\x00UC(\\d+)\\x00/g" in ui



def test_selected_text_reply_queue_path_includes_pending_selection_context():
    js = read("static/messages.js")

    assert "function _composerTextWithPendingSelections()" in js
    assert "function _clearComposerAfterQueuedSelectionSend()" in js
    assert "const _text=_composerTextWithPendingSelections().trim();" in js
    assert "_clearComposerAfterQueuedSelectionSend();" in js
    assert "if(!text&&!S.pendingFiles.length&&!_pendingSelections.length)" in js
    assert "_flushSelectionBlocksToComposer();\n  text=$('msg').value.trim();" in js


def test_selection_only_reply_enables_primary_send_button():
    """#4380 (Codex gate): selected context moved out of the textarea into
    _pendingSelections, so the primary Send button's content check must also
    recognize pending selections — otherwise a selection-only reply is
    un-sendable via click/tap/mobile (only desktop Enter, which calls send()
    directly, would work). Pin the predicate + its wiring."""
    msgs = read("static/messages.js")
    ui = read("static/ui.js")

    # messages.js exposes the predicate...
    assert "window._hasPendingSelections=function(){return _pendingSelections.length>0;};" in msgs, (
        "messages.js must expose window._hasPendingSelections for the composer content check"
    )
    # ...and refreshes the Send button whenever the selection set changes.
    assert "if(typeof updateSendBtn==='function') updateSendBtn();" in msgs, (
        "_renderSelectionChips must call updateSendBtn() so add/remove/clear updates the button"
    )
    # ui.js _composerHasContent() folds the predicate in.
    assert "window._hasPendingSelections==='function'&&window._hasPendingSelections()" in ui, (
        "_composerHasContent() must treat pending selections as sendable content (#4380)"
    )
