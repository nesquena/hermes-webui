"""Regression guards for Anchor-owned live progress echo cleanup."""

from __future__ import annotations

import pathlib
import re


REPO = pathlib.Path(__file__).resolve().parent.parent
MESSAGES = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
UI = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _interim_listener_body() -> str:
    match = re.search(
        r"source\.addEventListener\('interim_assistant'\s*,\s*(?:e|ev)\s*=>\s*\{(.*?)\n\s*\}\);",
        MESSAGES,
        re.DOTALL,
    )
    assert match, "interim_assistant listener not found"
    return match.group(1)


def test_interim_reasoning_echo_cleans_live_and_anchor_thinking():
    body = _interim_listener_body()

    assert "const reasoningEcho=!!(d&&d.reasoning_echo);" in body
    assert "if(reasoningEcho) _stripLiveReasoningEcho(visible);" in body
    assert "function _stripAnchorReasoningEcho(visible)" in MESSAGES
    assert "function _removeLiveReasoningEchoRows(visible)" in MESSAGES
    assert "events.splice(i,1);" in MESSAGES
    assert '.agent-activity-thinking[data-anchor-scene-row="1"]' in MESSAGES
    assert "_removeLiveReasoningEchoRows(visible)" in MESSAGES
    assert "reasoningText=durable.text;" in MESSAGES
    assert "liveReasoningText=live.text;" in MESSAGES


def test_interim_anchor_render_runs_after_legacy_segment_flush_without_duplicate_process_row():
    body = _interim_listener_body()

    flush_idx = body.index("_flushPendingSegmentRender({force:true,skipAnchorProcessProse:true});")
    anchor_idx = body.index("_applyToAnchor('interim_assistant',d,e);")
    flush_fn_start = MESSAGES.index("function _flushPendingSegmentRender")
    flush_fn = MESSAGES[flush_fn_start : MESSAGES.index("function _resetAssistantSegment", flush_fn_start)]

    assert "const skipAnchorProcessProse=!!(options&&options.skipAnchorProcessProse);" in flush_fn
    assert "if(!skipAnchorProcessProse) _upsertAnchorProcessProse(displayText,{sealed:force});" in flush_fn
    assert flush_idx < anchor_idx, (
        "Anchor live scene must render after the legacy interim segment is flushed, "
        "so renderLiveAnchorActivityScene can hide that source segment immediately."
    )
    assert "_flushPendingSegmentRender({force:true});" in body, (
        "already_streamed interim updates must still flush the token-owned prose row."
    )


def test_live_anchor_scene_hides_legacy_live_assistant_sources():
    start = UI.index("function renderLiveAnchorActivityScene")
    body = UI[start : UI.index("function _renderLiveAnchorActivitySceneForStream", start)]

    assert "blocks.querySelectorAll('[data-live-assistant=\"1\"]').forEach" in body
    assert "assistant-segment-worklog-source" in body
    assert "aria-hidden" in body
