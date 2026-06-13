from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _function_block(name: str) -> str:
    marker = f"function {name}"
    start = SESSIONS_JS.find(marker)
    assert start >= 0, f"{name} not found"
    brace = SESSIONS_JS.find("{", start)
    assert brace > start, f"{name} body not found"
    depth = 1
    i = brace + 1
    while depth and i < len(SESSIONS_JS):
        if SESSIONS_JS[i] == "{":
            depth += 1
        elif SESSIONS_JS[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} body did not close"
    return SESSIONS_JS[start:i]


def test_session_action_builder_exposes_visible_shortcut_badges():
    builder = _function_block("_buildSessionAction")
    assert "shortcutKey" in builder
    assert "data-shortcut-key" in builder
    assert "aria-keyshortcuts" in builder
    assert "session-action-shortcut" in builder
    assert "esc(shortcutKey.toUpperCase())" in builder


def test_common_session_actions_define_non_destructive_shortcuts():
    assert "shortcutKey:'C'" in SESSIONS_JS
    assert "shortcutKey:'R'" in SESSIONS_JS
    assert "shortcutKey:'P'" in SESSIONS_JS
    assert "shortcutKey:'M'" in SESSIONS_JS
    assert "shortcutKey:'A'" in SESSIONS_JS
    assert "shortcutKey:'D'" in SESSIONS_JS
    assert "shortcutKey:'G'" in SESSIONS_JS
    assert "shortcutKey:'H'" in SESSIONS_JS
    assert "shortcutKey:'X'" not in SESSIONS_JS


def test_open_action_menu_registers_menu_scoped_keydown_handler():
    assert "function _handleSessionActionMenuKeydown" in SESSIONS_JS
    handler = _function_block("_handleSessionActionMenuKeydown")
    assert "_sessionActionMenu" in handler
    assert "metaKey" in handler and "ctrlKey" in handler and "altKey" in handler
    assert "contentEditable" in handler
    assert ".session-action-opt[data-shortcut-key=\"" in handler
    assert "target.click()" in handler
    assert "document.addEventListener('keydown', _handleSessionActionMenuKeydown);" in SESSIONS_JS
    assert "document.removeEventListener('keydown', _handleSessionActionMenuKeydown);" in SESSIONS_JS


def test_shortcut_badges_are_styled_as_keycaps():
    assert ".session-action-shortcut" in STYLE_CSS
    assert "font-size:10px" in STYLE_CSS
    assert "border:1px solid var(--border2)" in STYLE_CSS
