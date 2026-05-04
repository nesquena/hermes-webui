from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text()
CSS = (REPO / "static" / "style.css").read_text()


def test_message_windowing_caps_initial_dom_to_recent_messages():
    assert "const MESSAGE_RENDER_WINDOW_DEFAULT=50" in UI_JS
    assert "_messageRenderWindowSize=MESSAGE_RENDER_WINDOW_DEFAULT" in UI_JS
    assert "const windowStart=Math.max(0, visWithIdx.length-renderWindowSize)" in UI_JS
    assert "const renderVisWithIdx=visWithIdx.slice(windowStart)" in UI_JS
    assert "for(let vi=0;vi<renderVisWithIdx.length;vi++)" in UI_JS
    assert "Load earlier messages (${hiddenBeforeCount} hidden)" in UI_JS


def test_load_earlier_expands_local_window_before_server_pagination_and_preserves_scroll():
    assert "function _showEarlierRenderedMessages()" in UI_JS
    assert "prevScrollH=container?container.scrollHeight:0" in UI_JS
    assert "prevScrollTop=container?container.scrollTop:0" in UI_JS
    assert "container.scrollTop=prevScrollTop+(newScrollH-prevScrollH)" in UI_JS
    assert "if(_messageHiddenBeforeCount()>0) _showEarlierRenderedMessages();" in UI_JS
    assert "else if(typeof _loadOlderMessages==='function') _loadOlderMessages();" in UI_JS


def test_windowed_render_keeps_streaming_and_tool_activity_anchored_to_rendered_messages():
    assert "if(S.activeStreamId){\n    scrollIfPinned();" in UI_JS
    assert "const assistantIdxs=[...assistantSegments.keys()].sort((a,b)=>a-b);" in UI_JS
    assert "if(aIdx<assistantIdxs[0]) continue;" in UI_JS
    assert "const renderedAssistantIdxs=[...assistantSegments.keys()].sort((a,b)=>a-b);" in UI_JS
    assert "const seg=assistantSegments.get(mi);" in UI_JS


def test_window_state_participates_in_cache_and_cached_button_is_rewired():
    assert "cached.renderWindowSize===renderWindowSize" in UI_JS
    assert "_sessionHtmlCache.set(sid,{html:_html,msgCount,renderWindowSize})" in UI_JS
    assert "function _wireMessageWindowLoadEarlierButton()" in UI_JS
    assert "_wireMessageWindowLoadEarlierButton();" in UI_JS
    assert UI_JS.count("_wireMessageWindowLoadEarlierButton();") >= 2


def test_load_earlier_affordance_has_button_styling_hook():
    assert "message-window-load-earlier" in UI_JS
    assert ".message-window-load-earlier" in CSS
    assert "border-radius:999px" in CSS
