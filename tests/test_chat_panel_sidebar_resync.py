"""Regression coverage for chat sidebar virtualization after panel navigation."""
from pathlib import Path

PANELS_JS = Path(__file__).parent.parent / "static" / "panels.js"


def _read_source() -> str:
    return PANELS_JS.read_text(encoding="utf-8")


def _function_block(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    if start == -1:
        marker = f"async function {name}"
        start = src.find(marker)
    assert start != -1, f"{name} not found"
    paren = src.find("(", start)
    assert paren != -1, f"{name} signature not found"
    paren_depth = 1
    j = paren + 1
    while j < len(src) and paren_depth:
        if src[j] == "(":
            paren_depth += 1
        elif src[j] == ")":
            paren_depth -= 1
        j += 1
    assert paren_depth == 0, f"{name} signature did not close"
    brace = src.find("{", j)
    assert brace != -1, f"{name} body not found"
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} body did not close"
    return src[start:i]


def test_switching_back_to_chat_schedules_sidebar_resync_after_panel_is_visible():
    """Chat navigation must re-render the sidebar after layout becomes visible.

    The sidebar session list is virtualized. After switching away to Settings,
    Logs, etc. and then back to Chat, the browser can clamp the preserved
    scrollTop during the layout transition. If no render runs after the chat
    view is visible again, stale virtual spacer/header DOM can remain until the
    next manual scroll event.
    """
    src = _read_source()
    switch_body = _function_block(src, "switchPanel")

    assert "_resyncChatSidebarAfterPanelSwitch();" in switch_body
    assert switch_body.index("if (panelEl) panelEl.classList.add('active');") < switch_body.index(
        "_resyncChatSidebarAfterPanelSwitch();"
    )


def test_chat_sidebar_resync_helper_is_guarded_and_bounded_to_one_animation_frame():
    """The resync must not poll or destroy rename state; one guarded rAF is enough."""
    src = _read_source()
    helper = _function_block(src, "_resyncChatSidebarAfterPanelSwitch")

    assert "if (_currentPanel !== 'chat') return;" in helper
    assert "typeof renderSessionListFromCache !== 'function'" in helper
    assert "requestAnimationFrame" in helper
    assert "setInterval" not in helper
    assert "setTimeout" not in helper
    assert "typeof _sessionActionMenu !== 'undefined' && _sessionActionMenu" in helper
    assert helper.index("typeof _sessionActionMenu") < helper.index("renderSessionListFromCache();")
    assert "renderSessionListFromCache();" in helper
