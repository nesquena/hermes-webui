from pathlib import Path


UI_JS = Path("static/ui.js").read_text(encoding="utf-8")


def test_session_html_cache_uses_render_signature_not_only_count():
    assert "function _messageRenderCacheSignature()" in UI_JS
    assert "const renderSignature=_messageRenderCacheSignature();" in UI_JS
    assert "cached.signature===renderSignature" in UI_JS
    assert "signature:renderSignature" in UI_JS


def test_render_signature_tracks_message_content_and_settled_tool_cards():
    signature_fn = UI_JS[UI_JS.index("function _messageRenderCacheSignature()"):UI_JS.index("function _clipCliToolSnippet")]
    assert "msgContent(m)" in signature_fn
    assert "m.tool_calls" in signature_fn
    assert "m._partial_tool_calls" in signature_fn
    assert "S.toolCalls" in signature_fn
    assert "tc.snippet" in signature_fn
    assert "compression_anchor_summary" in signature_fn


def test_documentation_no_longer_allows_same_count_stale_html():
    assert "Known limitation: cache key is session_id + message count" not in UI_JS
    assert "mutate message content without changing the count will serve stale HTML" not in UI_JS


def test_large_session_html_cache_uses_bounded_memory_lru():
    assert "_SESSION_HTML_CACHE_MAX_ENTRY_BYTES=2*1024*1024" in UI_JS
    assert "_SESSION_HTML_CACHE_MAX_TOTAL_BYTES=8*1024*1024" in UI_JS
    assert "function _sessionHtmlCacheEntryBytes(html)" in UI_JS
    assert "function _sessionHtmlCacheSet(sid,entry)" in UI_JS
    assert "_sessionHtmlCacheBytes>_SESSION_HTML_CACHE_MAX_TOTAL_BYTES" in UI_JS
    assert "_sessionHtmlCacheDelete(oldestSid)" in UI_JS
    assert "_html.length<300_000" not in UI_JS


def test_session_html_cache_hit_refreshes_lru_order_and_budgeted_delete():
    get_fn = UI_JS[
        UI_JS.index("function _sessionHtmlCacheGet(sid)"):
        UI_JS.index("function _sessionHtmlCacheSet(sid,entry)")
    ]
    assert "_sessionHtmlCache.delete(sid)" in get_fn
    assert "_sessionHtmlCache.set(sid,cached)" in get_fn
    assert "const cached=_sessionHtmlCacheGet(sid);" in UI_JS
    assert "_sessionHtmlCacheDelete(_sessionHtmlCacheSid);" in UI_JS
