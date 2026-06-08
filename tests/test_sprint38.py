"""
Sprint 38 Tests: Think-tag stripping with leading whitespace (PR #327).

Covers the static render path (ui.js regex logic, verified against the JS source)
and the streaming render path (messages.js _streamDisplay logic).
"""
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS     = (REPO_ROOT / "static" / "ui.js").read_text()
MSG_JS    = (REPO_ROOT / "static" / "messages.js").read_text()


# ── ui.js: static render path ────────────────────────────────────────────────

def test_think_regex_is_leading_only_after_optional_whitespace():
    """The <think> regex in ui.js must anchor after optional whitespace."""
    # Find the thinkMatch line by locating the .match( call on that line
    idx = UI_JS.find("const thinkMatch=content.match(")
    assert idx >= 0, "thinkMatch line not found in ui.js"
    line = UI_JS[idx:idx+100]
    assert "/^\\s*<think>" in line, \
        f"thinkMatch regex must only match leading <think> blocks after whitespace — found: {line.strip()}"
    assert "/^<think>" not in line, \
        f"thinkMatch regex must still allow leading whitespace — found: {line.strip()}"


def test_gemma_regex_is_leading_only_after_optional_whitespace():
    """The MiniMax channel-token regex in ui.js must anchor after optional whitespace."""
    idx = UI_JS.find("const gemmaMatch=content.match(")
    assert idx >= 0, "gemmaMatch line not found in ui.js"
    line = UI_JS[idx:idx+140]
    assert "/^\\s*<\\|channel\\|?>thought\\n?" in line, \
        f"gemmaMatch regex must only match leading channel blocks after whitespace — found: {line.strip()}"


def test_think_content_removal_uses_replace_not_slice():
    """After extracting thinkingText, content must use .replace() not .slice() to remove the tag."""
    # Find the block that handles thinkMatch
    idx = UI_JS.find("if(thinkMatch){")
    assert idx >= 0, "thinkMatch handler block not found"
    block = UI_JS[idx:idx+200]
    assert "content.replace(" in block, \
        "ui.js must use content.replace() to remove <think> block (not .slice())"
    assert "content.replace(/^\\s*<think>" in block, \
        "ui.js must remove only leading <think> blocks after optional whitespace"
    assert ".trimStart()" in block, \
        "ui.js must call .trimStart() on content after removing the <think> block"


def test_gemma_content_removal_uses_replace_not_slice():
    """Gemma channel token removal must also use .replace() not .slice()."""
    idx = UI_JS.find("if(gemmaMatch){")
    assert idx >= 0, "gemmaMatch handler block not found"
    block = UI_JS[idx:idx+200]
    assert "content.replace(" in block, \
        "ui.js must use content.replace() to remove Gemma channel block (not .slice())"
    assert "content.replace(/^\\s*<\\|channel\\|?>thought\\n?" in block, \
        "ui.js must remove only leading Gemma channel blocks after optional whitespace"
    assert ".trimStart()" in block, \
        "ui.js must call .trimStart() on content after removing the Gemma channel block"


def test_gemma_turn_regex_in_ui_js():
    """The Gemma 4 <|turn|>thinking\\n...<turn|> pattern must be extracted from persisted content."""
    # Detection in _messageHasReasoningPayload (correct double-pipe format)
    assert "<\\|turn\\|>thinking" in UI_JS, (
        "ui.js _messageHasReasoningPayload must detect Gemma 4 <|turn|>thinking\\n...<turn|> pattern"
        " (note: double-pipe: <|turn|> not <|turn>)"
    )
    # Extraction block
    idx = UI_JS.find("const gemmaTurnMatch=content.match(")
    assert idx >= 0, "gemmaTurnMatch line not found in ui.js"
    line = UI_JS[idx:idx+140]
    assert "/^\\s*<\\|turn\\|>thinking\\n" in line, (
        f"gemmaTurnMatch regex must only match leading Gemma 4 blocks after whitespace — found: {line.strip()}"
    )


def test_gemma_turn_content_removal_uses_replace_not_slice():
    """Gemma 4 turn token removal must use .replace() not .slice()."""
    idx = UI_JS.find("if(gemmaTurnMatch){")
    assert idx >= 0, "gemmaTurnMatch handler block not found in ui.js"
    block = UI_JS[idx:idx+240]
    assert "content.replace(" in block, (
        "ui.js must use content.replace() to remove Gemma 4 turn block (not .slice())"
    )
    assert "content.replace(/^\\s*<\\|turn\\|>thinking\\n" in block, (
        "ui.js must remove only leading Gemma 4 turn blocks after optional whitespace"
    )
    assert ".trimStart()" in block, (
        "ui.js must call .trimStart() on content after removing the Gemma 4 turn block"
    )


def test_message_reasoning_payload_detection_is_leading_only():
    """Persisted literal tag discussion later in content must not create a thinking card."""
    idx = UI_JS.find("function _messageHasReasoningPayload(m)")
    assert idx >= 0, "_messageHasReasoningPayload function not found in ui.js"
    block = UI_JS[idx:idx+500]
    assert "return /^\\s*(?:<think>" in block, (
        "_messageHasReasoningPayload must only detect leading provider thinking wrappers"
    )


# ── messages.js: streaming render path ───────────────────────────────────────

def test_stream_display_trims_before_startswith():
    """_streamDisplay in messages.js must call .trimStart() before .startsWith() check."""
    fn_idx = MSG_JS.find("function _streamDisplay()")
    assert fn_idx >= 0, "_streamDisplay function not found in messages.js"
    fn_end = MSG_JS.find("\n  }", fn_idx) + 4
    fn_body = MSG_JS[fn_idx:fn_end]
    assert "trimStart()" in fn_body, \
        "_streamDisplay must call trimStart() to handle models that emit leading whitespace before <think>"


def test_stream_display_uses_trimmed_for_startswith():
    """_streamDisplay must check trimmed.startsWith(open), not raw.startsWith(open)."""
    fn_idx = MSG_JS.find("function _streamDisplay()")
    fn_end = MSG_JS.find("\n  }", fn_idx) + 4
    fn_body = MSG_JS[fn_idx:fn_end]
    assert "trimmed.startsWith(open)" in fn_body, \
        "_streamDisplay must use trimmed.startsWith(open) not raw.startsWith(open)"


def test_stream_display_partial_tag_uses_trimmed():
    """The partial-tag guard in _streamDisplay must also use trimmed, not raw."""
    fn_idx = MSG_JS.find("function _streamDisplay()")
    fn_end = MSG_JS.find("\n  }", fn_idx) + 4
    fn_body = MSG_JS[fn_idx:fn_end]
    assert "open.startsWith(trimmed)" in fn_body, \
        "Partial-tag guard must use open.startsWith(trimmed) not open.startsWith(raw)"


def test_stream_display_trims_return_after_close():
    """After stripping a completed think block, _streamDisplay must trim leading whitespace from the result."""
    fn_idx = MSG_JS.find("function _streamDisplay()")
    fn_end = MSG_JS.find("\n  }", fn_idx) + 4
    fn_body = MSG_JS[fn_idx:fn_end]
    # The return after finding close must strip whitespace from the result
    assert ".replace(/^" in fn_body and "s+/,'')" in fn_body, \
        "_streamDisplay must strip leading whitespace from content after the closing think tag"


# ── Regression: existing anchored patterns must be gone ──────────────────────

def test_no_strictly_anchored_think_regex_in_ui_js():
    """The old /^<think>/ shape must not return; leading whitespace remains supported."""
    assert "/^<think>" not in UI_JS, \
        "Old anchored /^<think>/ regex still present in ui.js — fix not applied"


def test_no_strictly_anchored_gemma_regex_in_ui_js():
    """The old /^<|channel>/ shape must not return; leading whitespace remains supported."""
    assert "/^<|channel>" not in UI_JS, \
        "Old anchored /^<|channel>/ regex still present in ui.js — fix not applied"
