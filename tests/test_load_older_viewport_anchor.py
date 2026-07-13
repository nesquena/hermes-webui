from pathlib import Path

ROOT = Path(__file__).parent.parent
UI = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_load_older_button_is_a_first_class_viewport_anchor():
    capture = UI[UI.index("function _captureMessageViewportAnchor"):UI.index("function _suppressBrowserOverflowAnchor")]
    assert "container.querySelector('#loadOlderIndicator')" in capture
    assert "key:'__load_older_indicator__'" in capture
    assert "special:'load-older'" in capture
    # The button must only win while visible in the viewport.
    assert "rect.bottom>containerRect.top+1&&rect.top<containerRect.bottom-1" in capture


def test_load_older_restore_never_degrades_to_message_raw_zero():
    restore = UI[UI.index("function _restoreMessageViewportAnchor"):UI.index("let _messageViewportAnchorRemounting")]
    special = restore.index("anchor.special==='load-older'")
    button = restore.index("container.querySelector('#loadOlderIndicator')", special)
    missing_return = restore.index("if(!button||typeof button.getBoundingClientRect!=='function') return false;", button)
    raw_fallback = restore.index("const targetIdx=Number(anchor.rawIdx)")
    assert special < button < missing_return < raw_fallback
    assert "container.scrollTop+=(rect.top-containerRect.top)-targetTop;" in restore[special:raw_fallback]


def test_loading_button_sentinel_is_distinct_from_content_derived_message_keys():
    assert "__load_older_indicator__" not in UI[UI.index("function _messageViewportAnchorKeyForMessage"):UI.index("function _messageVisibleIndexForAnchorKey")]
