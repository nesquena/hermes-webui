from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function body not found: {signature}")


def _scroll_listener_block() -> str:
    start = UI_JS.index("el.addEventListener('scroll'")
    return UI_JS[start : UI_JS.index("})();", start)]


def test_preserve_scroll_unpinned_branch_shows_new_message_cue_after_restore():
    helper = _function_body(UI_JS, "function _scrollAfterMessageRender")

    # The preserve-scroll branch keeps master's forced follow path for pinned /
    # near-bottom users (no regression on settled-response bottom-pinning), and
    # only the genuinely-scrolled-up cohort restores their viewport + gets the
    # new-message cue. (Codex CORE catch: scrollIfPinned() in the pinned branch
    # could leave a pinned reader short of the settled response.)
    assert "if(!_messageUserUnpinned && _followMessagesAfterDomReplace()) return;" in helper
    assert "_restoreMessageScrollSnapshot(scrollSnapshot);" in helper
    assert helper.index("_restoreMessageScrollSnapshot(scrollSnapshot)") < helper.index(
        "_maybeShowNewMessageScrollCue(scrollSnapshot)"
    )
    # The cue is only shown on the non-follow (restore) path, after the restore.
    assert helper.index("_followMessagesAfterDomReplace()") < helper.index(
        "_maybeShowNewMessageScrollCue(scrollSnapshot)"
    )
    assert helper.count("_maybeShowNewMessageScrollCue(scrollSnapshot)") == 1


def test_scroll_cue_uses_growth_below_restored_viewport():
    maybe = _function_body(UI_JS, "function _maybeShowNewMessageScrollCue")
    sync = _function_body(UI_JS, "function _syncScrollToBottomCue")

    assert "el.scrollHeight>previousHeight+24" in maybe
    assert "distance>80" in maybe
    assert "_showNewMessageScrollCue()" in maybe
    assert "scroll-to-bottom-btn--new-message" in sync
    assert "session_new_message" in sync
    assert "session_jump_end" in sync


def test_click_near_bottom_and_resets_clear_new_message_cue():
    scroll = _function_body(UI_JS, "function scrollToBottom")
    reset_direction = _function_body(UI_JS, "function _resetScrollDirectionTracker")
    reset_stream = _function_body(UI_JS, "function _resetStreamScrollFollow")
    listener = _scroll_listener_block()

    assert scroll.index("_clearNewMessageScrollCue();") < scroll.index("_scrollPinned=true")
    assert "if(nearBottom) _clearNewMessageScrollCue();" in listener
    assert "_syncScrollToBottomCue(showBottomButton,{newMessage:_newMessageCueVisible})" in listener
    assert "_clearNewMessageScrollCue();" in reset_direction
    assert "_clearNewMessageScrollCue();" in reset_stream


def test_new_message_cue_i18n_keys_exist_in_locale_blocks():
    assert I18N_JS.count("session_new_message:") >= 8
    assert I18N_JS.count("session_new_message_label:") >= 8
    assert "session_new_message: 'New message'" in I18N_JS
    assert "session_new_message_label: 'New message available, jump to end'" in I18N_JS


def test_new_message_cue_has_stable_pill_styling():
    assert ".scroll-to-bottom-btn.scroll-to-bottom-btn--new-message" in STYLE_CSS
    assert "max-width:min(220px,calc(100% - 40px))" in STYLE_CSS
    assert ".scroll-to-bottom-btn.scroll-to-bottom-btn--new-message .session-jump-btn__text" in STYLE_CSS
