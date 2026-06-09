from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def test_live_smd_writes_schedule_incremental_katex_rendering():
    """Live markdown deltas should render math before the terminal done event."""
    assert "let _streamingKatexTimer=null" in MESSAGES_JS
    assert "function _scheduleStreamingKatex()" in MESSAGES_JS
    assert "setTimeout(()=>{" in MESSAGES_JS
    assert "renderKatexBlocks(assistantBody,{streaming:true})" in MESSAGES_JS

    smd_write_idx = MESSAGES_JS.index("function _smdWrite(displayText, fade=false){")
    done_idx = MESSAGES_JS.index("source.addEventListener('done'")
    smd_write_block = MESSAGES_JS[smd_write_idx:done_idx]
    assert "_scheduleStreamingKatex();" in smd_write_block


def test_streaming_katex_timer_is_cleared_when_smd_parser_ends():
    """The final done path should not leave a stale live KaTeX timer around."""
    end_idx = MESSAGES_JS.index("function _smdEndParser(){")
    write_idx = MESSAGES_JS.index("function _smdWrite(displayText, fade=false){")
    end_block = MESSAGES_JS[end_idx:write_idx]
    assert "if(_streamingKatexTimer){clearTimeout(_streamingKatexTimer);_streamingKatexTimer=null;}" in end_block


def test_katex_renderer_scans_live_and_settled_unrendered_nodes_under_container():
    assert "function renderKatexBlocks(container,options){" in UI_JS
    assert "const root=container||document;" in UI_JS
    assert "const streaming=Boolean(options&&options.streaming);" in UI_JS
    assert ".katex-block:not([data-rendered]),.katex-inline:not([data-rendered])," in UI_JS
    assert "equation-block:not([data-rendered]),equation-inline:not([data-rendered])" in UI_JS
    assert "const tagName=(el.tagName||'').toLowerCase();" in UI_JS
    assert "const displayMode=el.dataset.katex==='display'||tagName==='equation-block';" in UI_JS
