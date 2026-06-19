"""Regression tests for #3952 composer Escape keyboard navigation."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _block_from_opening_brace(src: str, brace: int, label: str) -> str:
    assert brace >= 0, f"{label} opening brace must exist"
    depth = 1
    idx = brace + 1
    while idx < len(src) and depth:
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
        idx += 1
    assert depth == 0, f"{label} block must close"
    return src[brace + 1 : idx - 1]


def _escape_block() -> str:
    document_keydown = BOOT_JS.index("// B14: Cmd/Ctrl+K creates a new chat from anywhere")
    start = BOOT_JS.index("if(e.key==='Escape'){", document_keydown)
    brace = BOOT_JS.index("{", start)
    return _block_from_opening_brace(BOOT_JS, brace, "document Escape handler")


def _composer_keydown_block() -> str:
    start = BOOT_JS.index("$('msg').addEventListener('keydown',e=>{")
    brace = BOOT_JS.index("{", start)
    return _block_from_opening_brace(BOOT_JS, brace, "composer keydown handler")


def test_escape_blurs_focused_composer_after_higher_priority_escape_actions():
    """Escape should let keyboard-only users leave the composer and use j/k nav."""
    block = _escape_block()

    blur_idx = block.find("document.activeElement===$('msg')")
    assert blur_idx != -1, "Escape handler must detect the focused composer"
    assert "$('msg').blur();" in block[blur_idx:], "Escape handler must blur the composer"
    # The blur must be skipped while an IME candidate window is composing, so
    # Escape dismisses the candidate (CJK input) rather than blurring the composer.
    blur_line = block[blur_idx : block.find("\n", blur_idx)]
    assert "!e.isComposing" in blur_line and "!_imeComposing" in blur_line, (
        "Escape blur must be guarded against IME composition"
    )

    assert block.index("skipOnboarding") < blur_idx, "onboarding dismissal stays higher priority"
    assert block.index("_closeSettingsPanel") < blur_idx, "settings dismissal stays higher priority"
    assert block.index("clearSessionSearch") < blur_idx, "session-search clearing stays higher priority"
    assert block.index("msg-edit-cancel") < blur_idx, "message-edit cancel stays higher priority"


def test_jk_session_navigation_still_ignores_interactive_targets():
    """Composer text entry still owns j/k until Escape blurs it."""
    nav_start = SESSIONS_JS.index("// Keyboard session navigation — J/K bindings")
    nav_block = SESSIONS_JS[nav_start:]
    assert "if(typeof _isInteractiveSwipeTarget==='function'&&_isInteractiveSwipeTarget(e.target)) return;" in nav_block


def test_escape_dismisses_command_dropdown_without_blurring_composer():
    """Escape should close slash-command autocomplete without bubbling to blur."""
    block = _composer_keydown_block()
    escape_idx = block.find("if(e.key==='Escape'){")
    assert escape_idx != -1, "composer keydown handler must handle dropdown Escape"
    escape_line = block[escape_idx : block.find("}", escape_idx) + 1]

    assert "e.preventDefault();" in escape_line
    assert "e.stopPropagation();" in escape_line
    assert "hideCmdDropdown();" in escape_line
