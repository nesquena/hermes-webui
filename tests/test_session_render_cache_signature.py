from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    start = src.find(f"function {name}(")
    assert start != -1, f"{name} not found"
    brace = src.find("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1:idx]
    raise AssertionError(f"{name} body did not close")


def test_session_html_cache_uses_message_signature_not_count_only():
    render = _function_body(UI_JS, "renderMessages")
    assert "const cacheEligible=!!(sid&&sid!==_sessionHtmlCacheSid&&!INFLIGHT[sid]&&!hasTransientTranscriptUi);" in render
    assert "const cacheSignature=cacheEligible?_messageRenderCacheSignature(S.messages, renderWindowSize):null;" in render
    assert "if(cacheEligible)" in render
    assert "cached.signature===cacheSignature" in render
    assert "signature:cacheSignature" in render
    assert "if(sid&&!hasTransientTranscriptUi&&cacheSignature)" in render
    assert "cached.msgCount===msgCount&&cached.renderWindowSize===renderWindowSize)" not in render


def test_message_render_cache_signature_covers_content_and_compression_anchor():
    helper = _function_body(UI_JS, "_messageRenderCacheSignature")
    part = _function_body(UI_JS, "_messageRenderCachePart")
    assert "compression_anchor_visible_idx" in helper
    assert "compression_anchor_message_key" in helper
    assert "compression_anchor_summary" in helper
    assert ".map(_messageRenderCachePart)" in helper
    assert "msgContent(m)" in part
    assert "rawContentHash" in part
    assert "JSON.stringify(m.content||'')" in part
    assert "m._pending?'pending'" in part
    assert "m._live?'live'" in part


def test_old_count_only_cache_limitation_comment_removed():
    assert "cache key is session_id + message count" not in UI_JS
    assert "serve stale HTML" not in UI_JS
