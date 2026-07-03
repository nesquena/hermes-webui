import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    start = source.find(f"function {name}(")
    assert start != -1, f"{name}() not found"
    end = source.find("\nfunction ", start + 1)
    if end == -1:
        end = len(source)
    return source[start:end]


def test_conversation_transcript_is_not_a_named_region():
    match = re.search(r'<div class="messages" id="messages"[^>]*>', INDEX_HTML)
    assert match, "#messages container not found"
    tag = match.group(0)
    assert 'role="region"' not in tag
    assert 'Conversation transcript' not in tag
    assert "tabindex" not in tag.lower()


def test_latest_assistant_landmark_helper_is_named_but_not_focusable():
    helper = _function_body(UI_JS, "_setLatestAssistantTurnLandmark")
    assert "Latest Hermes response" in helper
    assert "setAttribute('role','region')" in helper
    assert "setAttribute('aria-label',label)" in helper
    assert "data-latest-assistant-response" in helper
    assert "tabindex" not in helper.lower()
    assert "<h" not in helper.lower()


def test_settled_render_marks_only_the_latest_assistant_turn():
    render_messages = _function_body(UI_JS, "renderMessages")
    assert "const latestRenderedAssistantRawIdx=(()=>{" in render_messages
    assert "if(entry&&entry.m&&entry.m.role==='assistant'&&!entry.m._live) return entry.rawIdx;" in render_messages
    assert "_setLatestAssistantTurnLandmark(currentAssistantTurn, !m._live&&rawIdx===latestRenderedAssistantRawIdx);" in render_messages
    assert "seg.setAttribute('role','region')" not in render_messages
    assert "orderedSeg.setAttribute('role','region')" not in render_messages


def test_live_assistant_turn_paths_do_not_own_final_response_landmark():
    restore_live_turn = _function_body(UI_JS, "restoreLiveTurnHtmlForSession")
    render_live_scene = _function_body(UI_JS, "renderLiveAnchorActivityScene")
    append_thinking = _function_body(UI_JS, "appendThinking")

    assert "_setLatestAssistantTurnLandmark(restored, true);" not in restore_live_turn
    assert "_setLatestAssistantTurnLandmark(turn, true);" not in render_live_scene
    assert "_setLatestAssistantTurnLandmark(turn, true);" not in append_thinking
