import pathlib
import re


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _ime_guarded_enter_pattern(event_var_pattern, require_no_shift=False):
    no_shift = rf"\s*&&\s*!\s*{event_var_pattern}\.shiftKey" if require_no_shift else ""
    return (
        rf"if\s*\(\s*{event_var_pattern}\.key\s*===\s*'Enter'{no_shift}\s*\)\s*\{{\s*"
        rf"if\s*\(\s*{event_var_pattern}\.isComposing\s*\)\s*"
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
