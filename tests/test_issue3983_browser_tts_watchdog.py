"""Browser TTS watchdog — server TTS branch must stay separate.

The TTS delegation refactor unified the server TTS playback into
a shared helper (window._playServerTts in static/ui.js). boot.js's
_speakResponse function delegates to that helper for the else branch
and keeps the browser TTS branch independent so the watchdog can
guard it.

These tests use a brace-counting helper to extract function bodies
because the JS files contain nested functions, template literals
with braces, and string literals — a regex-based approach would be
brittle. The extracted bodies are then grepped for the patterns we
care about.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _extract_function_body(src: str, name: str) -> str:
    """Return the body (between braces) of `function NAME(`, or '' if not found."""
    needle = f"function {name}("
    start = src.find(needle)
    if start == -1:
        return ""
    open_idx = src.find("{", start)
    if open_idx == -1:
        return ""
    depth = 0
    for i in range(open_idx, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx + 1 : i]
    return ""


def test_browser_tts_watchdog_armed_in_speak_response():
    """_speakResponse() must arm the browser-TTS watchdog before utter.speak()."""
    body = _extract_function_body(
        (REPO / "static" / "boot.js").read_text(encoding="utf-8"),
        "_speakResponse",
    )
    assert body, "_speakResponse() must exist in boot.js"
    assert "_armBrowserTtsRecovery" in body, (
        "_speakResponse() must arm the browser-TTS watchdog before utter.speak()"
    )
    assert "_clearBrowserTtsRecovery" in body


def test_browser_tts_watchdog_cleared_in_speak_response():
    """_speakResponse() must clear the browser-TTS watchdog on completion."""
    body = _extract_function_body(
        (REPO / "static" / "boot.js").read_text(encoding="utf-8"),
        "_speakResponse",
    )
    assert body
    # The watchdog should be cleared at least once (in cleanup paths)
    assert body.count("_clearBrowserTtsRecovery") >= 1, (
        "_speakResponse() must clear the browser TTS watchdog at least once"
    )


def test_server_audio_branch_delegates_to_shared_helper():
    """Server TTS branch in _speakResponse delegates to window._playServerTts.

    The branch is structurally independent from the browser TTS watchdog
    and uses the shared playback helper from static/ui.js instead of
    inlining its own fetch+play logic.
    """
    body = _extract_function_body(
        (REPO / "static" / "boot.js").read_text(encoding="utf-8"),
        "_speakResponse",
    )
    assert body, "_speakResponse() must exist in boot.js"
    assert "window._playServerTts" in body, (
        "Server TTS else branch must delegate to window._playServerTts"
    )
    assert "onComplete" in body, (
        "Server TTS else branch must pass onComplete to resume voice mode"
    )
    # Shared helper must be defined in ui.js (single source of truth)
    ui_src = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    assert re.search(r"window\._playServerTts\s*=\s*_playServerTts", ui_src), (
        "ui.js must expose _playServerTts on window for boot.js to use"
    )
