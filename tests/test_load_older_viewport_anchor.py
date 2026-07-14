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
    raw_fallback = restore.index("const targetIdx=Number(anchor.rawIdx)")
    # Sentinel branch precedes the generic rawIdx fallback and, when the button is
    # present, realigns to its own offset (button-present arm) — never degrades to
    # message rawIdx 0.
    assert special < button < raw_fallback
    assert "container.scrollTop+=(rect.top-containerRect.top)-targetTop;" in restore[special:raw_fallback]


def test_load_older_disappearance_uses_prepend_height_fallback_and_never_returns_false():
    # Maintainer gate #3: when the sentinel button is gone mid-render, the branch
    # MUST NOT return false (which would cascade to _remountMessageViewportAnchor's
    # generic message remounting → bad realign). Instead it applies a prepend-height
    # fallback (scrollHeightAtCapture delta) and returns true, keeping the sentinel
    # fully handled in this branch.
    restore = UI[UI.index("function _restoreMessageViewportAnchor"):UI.index("let _messageViewportAnchorRemounting")]
    special = restore.index("anchor.special==='load-older'")
    # Bound the sentinel block precisely — the generic anchor path starts with
    # `const anchorKey=String(anchor.key||'')`, so slice to there to avoid picking
    # up unrelated `return false;` statements from the generic branch.
    generic_start = restore.index("const anchorKey=String(anchor.key")
    sentinel_block = restore[special:generic_start]
    # No `if(!button ...) return false;` — that was the buggy fall-through that let
    # the sentinel cascade into generic message remounting.
    assert "if(!button||typeof button.getBoundingClientRect!=='function') return false;" not in sentinel_block
    # Disappear-fallback keys on scrollHeightAtCapture and shifts scrollTop by the
    # height delta (non-zero prepend/growth compensation).
    assert "scrollHeightAtCapture" in sentinel_block
    assert "container.scrollTop=Math.max(0,container.scrollTop+_grew)" in sentinel_block
    # The sentinel branch closes with `return true;` — never with a bare `return false;`
    # statement (only the "MUST NOT return false" comment mentions the phrase).
    assert "return true;" in sentinel_block
    assert "return false;" not in sentinel_block


def test_remount_refuses_load_older_sentinel():
    # Maintainer gate #3: _remountMessageViewportAnchor must early-return for
    # load-older sentinels so a disappeared button can never enter generic MESSAGE
    # remounting (which has no fallback for a special-anchor with null rawIdx/sessionIdx).
    remount = UI[UI.index("function _remountMessageViewportAnchor"):UI.index("function _compensateScrollForMeasurementDelta")]
    assert "anchor.special==='load-older'||anchor.key==='__load_older_indicator__'" in remount
    guard_idx = remount.index("anchor.special==='load-older'")
    # The guard sits before any generic message lookup (data-message-anchor-key search).
    generic_idx = remount.index("data-message-anchor-key")
    assert guard_idx < generic_idx


def test_loading_button_sentinel_is_distinct_from_content_derived_message_keys():
    assert "__load_older_indicator__" not in UI[UI.index("function _messageViewportAnchorKeyForMessage"):UI.index("function _messageVisibleIndexForAnchorKey")]
