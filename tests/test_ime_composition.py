import pathlib
import re


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _ime_guarded_enter_pattern(event_var_pattern, require_no_shift=False):
    """Accept the IME guard in any of three shapes that have been used in the codebase:

    1. Original `e.isComposing` (pre-#1441)
    2. Module-local `_isImeEnter(e)` (PR #1441 — chat composer in boot.js)
    3. Window-exposed `window._isImeEnter(e)` (issue #1443 — promoted to ui.js,
       sessions.js so all 6 Enter-input sites use the same Safari-aware guard).
    """
    no_shift = rf"\s*&&\s*!\s*{event_var_pattern}\.shiftKey" if require_no_shift else ""
    # Either: if(e.isComposing) ... | if(_isImeEnter(e)) ... | if(window._isImeEnter&&window._isImeEnter(e)) ...
    guard = (
        rf"if\s*\(\s*"
        rf"(?:{event_var_pattern}\.isComposing"
        rf"|_isImeEnter\(\s*{event_var_pattern}\s*\)"
        rf"|window\._isImeEnter\s*&&\s*window\._isImeEnter\s*\(\s*{event_var_pattern}\s*\))"
        rf"\s*\)\s*"
    )
    return (
        rf"if\s*\(\s*{event_var_pattern}\.key\s*===\s*'Enter'{no_shift}\s*\)\s*\{{\s*"
        + guard +
        rf"(?:\{{\s*return\s*;?\s*\}}|return\s*;?)"
    )


def test_boot_chat_enter_send_respects_ime_composition():
    assert re.search(
        _ime_guarded_enter_pattern("e"),
        BOOT_JS,
        re.DOTALL,
    ), "Chat composer Enter handler must ignore IME composition Enter in static/boot.js"
    assert re.search(
        _ime_guarded_enter_pattern("e", require_no_shift=True),
        BOOT_JS,
        re.DOTALL,
    ), "Command dropdown Enter handler must ignore IME composition Enter in static/boot.js"


def test_ui_enter_submit_paths_respect_ime_composition():
    assert re.search(
        rf"document\.addEventListener\('keydown',e=>\{{[\s\S]*?{_ime_guarded_enter_pattern('e')}",
        UI_JS,
        re.DOTALL,
    ), \
        "App dialog Enter handler must ignore IME composition Enter in static/ui.js"
    assert re.search(
        _ime_guarded_enter_pattern("e", require_no_shift=True),
        UI_JS,
        re.DOTALL,
    ), \
        "Message edit Enter-to-save handler must ignore IME composition Enter in static/ui.js"
    assert re.search(
        rf"inp\.onkeydown=\(e2\)=>\{{\s*{_ime_guarded_enter_pattern('e2')}",
        UI_JS,
        re.DOTALL,
    ), \
        "Workspace rename Enter handler must ignore IME composition Enter in static/ui.js"


def test_sessions_enter_submit_paths_respect_ime_composition():
    matches = re.findall(
        _ime_guarded_enter_pattern(r"e2?"),
        SESSIONS_JS,
        re.DOTALL,
    )
    assert len(matches) >= 3, \
        "Session and project rename/create Enter handlers must ignore IME composition Enter in static/sessions.js"
