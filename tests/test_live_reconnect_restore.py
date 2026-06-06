from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
MESSAGES_SRC = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_SRC = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_session_load_passes_restored_live_turn_to_reconnect_attach():
    restore_pos = SESSIONS_SRC.index("const restoredLiveTurn=")
    attach_pos = SESSIONS_SRC.index("attachLiveStream(sid, activeStreamId", restore_pos)
    attach_call = SESSIONS_SRC[attach_pos : attach_pos + 220]

    assert "restoreLiveTurnHtmlForSession(sid)" in SESSIONS_SRC[restore_pos : restore_pos + 160]
    assert "{reconnecting:true,restoredLiveTurn}" in attach_call


def test_restored_reconnect_does_not_arm_first_streaming_markdown_clear():
    option_pos = MESSAGES_SRC.index("const restoredLiveTurn=reconnecting&&!!options.restoredLiveTurn")
    smd_pos = MESSAGES_SRC.index("let _smdReconnect=", option_pos)
    render_helper_pos = MESSAGES_SRC.index("function _renderRestoredReconnectDisplay", option_pos)
    render_path_pos = MESSAGES_SRC.index("_renderRestoredReconnectDisplay(displayText,false)", render_helper_pos)
    clear_pos = MESSAGES_SRC.index("if(_smdReconnect){assistantBody.innerHTML='';_smdReconnect=false;}", render_path_pos)

    assert "let _smdReconnect=reconnecting&&!restoredLiveTurn;" in MESSAGES_SRC[smd_pos : smd_pos + 80]
    assert render_path_pos < clear_pos


def test_restored_reconnect_replays_known_tool_cards_before_sse_attach():
    helper_pos = MESSAGES_SRC.index("function _replayRestoredLiveToolCardsIfMissing")
    helper_block = MESSAGES_SRC[helper_pos : helper_pos + 900]

    assert "inner.querySelector('.tool-card-row[data-live-tid],.tool-call-group[data-live-tool-call-group]')" in helper_block
    assert "replayLiveToolCardsFromState(calls)" in helper_block
    assert "appendLiveToolCard(tc)" in helper_block

    preflight_pos = MESSAGES_SRC.index("const replayParams=replayOnly?_runJournalReplayParams():'';")
    preflight_block = MESSAGES_SRC[preflight_pos : preflight_pos + 300]
    assert "_replayRestoredLiveToolCardsIfMissing();" in preflight_block
    assert preflight_block.index("_replayRestoredLiveToolCardsIfMissing();") < preflight_block.index("_wireSSE(")


def test_live_tool_replay_helper_is_idempotent_for_restored_tids():
    helper_pos = UI_SRC.index("function replayLiveToolCardsFromState")
    helper_block = UI_SRC[helper_pos : UI_SRC.index("function clearLiveToolCards", helper_pos)]

    assert ".tool-card-row[data-live-tid=" in helper_block
    assert "if(existing) continue;" in helper_block
    assert "appendLiveToolCard(tc);" in helper_block
