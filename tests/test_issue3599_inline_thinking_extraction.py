from pathlib import Path

from api.streaming import _split_thinking_from_content


REPO = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")


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


def test_split_clean_leading_think_block():
    content, reasoning = _split_thinking_from_content("<think>plan</think>\nanswer")

    assert content == "answer"
    assert reasoning == "plan"


def test_split_extracts_non_leading_complete_block():
    content, reasoning = _split_thinking_from_content("visible before <think>hidden</think> visible after")

    assert "<think>" not in content
    assert "visible before" in content
    assert "visible after" in content
    assert reasoning == "hidden"


def test_split_extracts_multiple_complete_blocks():
    content, reasoning = _split_thinking_from_content("<think>one</think><think>two</think> final")

    assert content == "final"
    assert reasoning == "one\n\ntwo"


def test_split_keeps_fenced_code_literal_think_visible():
    raw = "```html\n<think>literal</think>\n```\nanswer"
    content, reasoning = _split_thinking_from_content(raw)

    assert content == raw
    assert reasoning == ""


def test_split_merges_existing_reasoning_without_duplicate():
    content, reasoning = _split_thinking_from_content("<think>same</think>answer", "same")

    assert content == "answer"
    assert reasoning == "same"


def test_split_merges_existing_reasoning_with_new_inline_block():
    content, reasoning = _split_thinking_from_content("<think>inline</think>answer", "separate")

    assert content == "answer"
    assert reasoning == "separate\n\ninline"


def test_reasoning_only_content_survives_reload_source_fields():
    content, reasoning = _split_thinking_from_content("<think>only reasoning</think>")

    assert content == ""
    assert reasoning == "only reasoning"


def test_unclosed_inline_thinking_after_content_stays_visible_on_persist():
    """#3633 deep-review (Codex catch): on the PERSIST path an unclosed think tag
    that appears AFTER visible content is almost always a literal typed tag, so
    the prose after it must NOT be silently truncated into reasoning. A LEADING
    unclosed block (cut off mid-thought) is still treated as reasoning."""
    # Mid-body unclosed → stays fully visible, nothing moved to reasoning.
    content, reasoning = _split_thinking_from_content("answer<think>still thinking")
    assert content == "answer<think>still thinking"
    assert reasoning == ""

    # Leading unclosed → genuine cut-off thinking trace, moves to reasoning.
    lead_content, lead_reasoning = _split_thinking_from_content("<think>still thinking")
    assert lead_content == ""
    assert lead_reasoning == "still thinking"


def test_messages_js_live_and_persist_paths_share_extractor():
    stream_display = _function_body(MESSAGES_JS, "function _streamDisplay")
    parse_state = _function_body(MESSAGES_JS, "function _parseStreamState")
    split_persist = _function_body(MESSAGES_JS, "function _splitThinkFromContent")

    assert "_extractInlineThinkingFromContent(_stripXmlToolCalls(assistantText), liveReasoningText, {streaming:true}).content" in stream_display
    assert "return _extractInlineThinkingFromContent(_stripXmlToolCalls(assistantText), liveReasoningText, {streaming:true});" in parse_state
    assert "return _extractInlineThinkingFromContent(rawContent, existingReasoning, {streaming:false});" in split_persist
    assert "window._extractInlineThinkingFromContentForRender" in MESSAGES_JS
    assert "_thinkingFenceMarkerAt" in MESSAGES_JS


def test_render_messages_uses_shared_extractor_on_reload():
    render = _function_body(UI_JS, "function renderMessages")

    assert "window._extractInlineThinkingFromContentForRender(content, thinkingText)" in render
    assert "thinkingText=split.reasoning||thinkingText" in render
    assert "content=split.content" in render


def test_inline_and_separate_reasoning_merge_not_drop():
    """#3633: the extractor MERGES inline + an explicitly-passed separate reasoning
    payload (deduped) rather than dropping either. (The reload render path itself
    deliberately does NOT seed m.reasoning into this extractor — that separation
    is pinned by test_issue2565; the merge capability is exercised by the live
    streaming path which passes liveReasoningText.)"""
    content, reasoning = _split_thinking_from_content("<think>inline</think>answer", "separate")
    assert content == "answer"
    assert reasoning == "separate\n\ninline"

    # Identical inline + separate dedupe to one.
    content2, reasoning2 = _split_thinking_from_content("<think>same</think>answer", "same")
    assert content2 == "answer"
    assert reasoning2 == "same"

    # Separate-only (no inline tag) is preserved and content is untouched
    # (no promotion of reasoning into visible prose).
    content3, reasoning3 = _split_thinking_from_content("plain answer", "separate")
    assert content3 == "plain answer"
    assert reasoning3 == "separate"


def test_extraction_is_linear_on_long_no_newline_content():
    """#3633 Codex perf catch: the indented-code / leading checks must not be
    O(n^2). A 200k-char no-newline message must extract well under a second."""
    import time

    big = "x" * 200_000 + "answer"
    start = time.time()
    content, reasoning = _split_thinking_from_content(big)
    elapsed = time.time() - start
    assert content == big
    assert reasoning == ""
    assert elapsed < 1.0, f"extraction took {elapsed:.2f}s — likely quadratic"


def test_timeout_wrapper_remains_out_of_scope():
    assert "Request timed out. Please try again." in WORKSPACE_JS
    assert "AbortController" in WORKSPACE_JS
