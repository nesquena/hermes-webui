import pathlib


REPO = pathlib.Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")


def _extract_block(src: str, anchor: str) -> str:
    start = src.find(anchor)
    assert start != -1, f"{anchor!r} not found"
    brace = src.find("{", start)
    assert brace != -1, f"{anchor!r} body not found"
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    raise AssertionError(f"{anchor!r} body did not terminate cleanly")


def test_clear_message_render_cache_clears_both_render_layers():
    fn = _extract_block(UI_JS, "function clearMessageRenderCache()")
    assert "_clearRenderCache();" in fn, (
        "clearMessageRenderCache() must clear the per-message markdown cache "
        "before resetting the session HTML cache (#3870)"
    )
    assert "_sessionHtmlCache.clear();" in fn


def test_render_user_markdown_toggle_uses_full_cache_clear():
    block = _extract_block(PANELS_JS, "renderUserMarkdownCb.onchange=function()")
    assert "clearMessageRenderCache();" in block, (
        "Render-user-markdown toggle must clear the session HTML cache as well "
        "as the per-message render cache (#3870)"
    )
    assert "_clearRenderCache();" not in block, (
        "Render-user-markdown toggle should route through clearMessageRenderCache() "
        "instead of bypassing the session cache reset (#3870)"
    )
    assert "renderMessages();" in block
