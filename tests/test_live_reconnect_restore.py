from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _static_source(path):
    return (ROOT / "static" / path).read_text(encoding="utf-8")


def test_session_load_passes_restored_live_turn_to_reconnect_attach():
    sessions_src = _static_source("sessions.js")

    restore_pos = sessions_src.index("const restoredLiveTurn=")
    attach_pos = sessions_src.index("attachLiveStream(sid, activeStreamId", restore_pos)
    attach_call = sessions_src[attach_pos : attach_pos + 220]

    assert "restoreLiveTurnHtmlForSession(sid)" in sessions_src[restore_pos : restore_pos + 160]
    assert "{reconnecting:true,restoredLiveTurn}" in attach_call


def test_restored_reconnect_does_not_arm_first_streaming_markdown_clear():
    messages_src = _static_source("messages.js")

    option_pos = messages_src.index("const restoredLiveTurn=reconnecting&&!!options.restoredLiveTurn")
    smd_pos = messages_src.index("let _smdReconnect=", option_pos)
    render_helper_pos = messages_src.index("function _renderRestoredReconnectDisplay", option_pos)
    render_path_pos = messages_src.index("_renderRestoredReconnectDisplay(displayText,false)", render_helper_pos)
    clear_pos = messages_src.index("if(_smdReconnect){assistantBody.innerHTML='';_smdReconnect=false;}", render_path_pos)

    assert "let _smdReconnect=reconnecting&&!restoredLiveTurn;" in messages_src[smd_pos : smd_pos + 80]
    assert render_path_pos < clear_pos


def test_restored_reconnect_replays_known_tool_cards_before_sse_attach():
    messages_src = _static_source("messages.js")

    helper_pos = messages_src.index("function _replayRestoredLiveToolCardsIfMissing")
    helper_block = messages_src[helper_pos : helper_pos + 900]

    assert "inner.querySelector('.tool-card-row[data-live-tid]')" in helper_block
    assert ".tool-call-group[data-live-tool-call-group]" not in helper_block
    assert "querySelector('.tool-card-row[data-live-tid]')) return true" not in helper_block
    assert "replayLiveToolCardsFromState(calls)" in helper_block
    assert "appendLiveToolCard(tc)" in helper_block

    preflight_pos = messages_src.index("const replayParams=replayOnly?_runJournalReplayParams():'';")
    preflight_block = messages_src[preflight_pos : preflight_pos + 300]
    assert "_replayRestoredLiveToolCardsIfMissing();" in preflight_block
    assert preflight_block.index("_replayRestoredLiveToolCardsIfMissing();") < preflight_block.index("_wireSSE(")


def test_restored_reconnect_display_deactivates_after_new_text_arrives():
    messages_src = _static_source("messages.js")

    option_pos = messages_src.index("const restoredLiveTurn=reconnecting&&!!options.restoredLiveTurn")
    helper_pos = messages_src.index("function _renderRestoredReconnectDisplay", option_pos)
    helper_block = messages_src[helper_pos : messages_src.index("Shared SSE handler", helper_pos)]

    assert "let _restoredReconnectDisplayActive=restoredLiveTurn;" in messages_src[option_pos : option_pos + 140]
    assert "if(!_restoredReconnectDisplayActive||!assistantBody) return false;" in helper_block
    assert "_restoredReconnectDisplayActive=false;" in helper_block
    divergent_block = helper_block[helper_block.index("_restoredReconnectDisplayActive=false;") :]
    assert "assistantBody.innerHTML='';" in divergent_block
    assert "renderMd(target)" not in helper_block
    assert "return false;" in divergent_block


def test_live_tool_replay_helper_is_idempotent_for_restored_tids():
    ui_src = _static_source("ui.js")

    helper_pos = ui_src.index("function replayLiveToolCardsFromState")
    helper_block = ui_src[helper_pos : ui_src.index("function clearLiveToolCards", helper_pos)]

    assert ".tool-card-row[data-live-tid=" in helper_block
    assert "if(existing) continue;" in helper_block
    assert "appendLiveToolCard(tc);" in helper_block
