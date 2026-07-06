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


def test_server_tts_helper_chunks_long_text_before_posting():
    """Server TTS must not send >5000-char messages in a single request."""
    body = _extract_function_body(
        (REPO / "static" / "ui.js").read_text(encoding="utf-8"),
        "_playServerTts",
    )
    assert body, "_playServerTts() must exist in ui.js"
    assert "_splitForTTS(text, opts.maxChars || 4500)" in body, (
        "Server TTS should split text below the server's 5000-char limit"
    )
    assert "chunks.forEach" in body, "Server TTS should play chunks sequentially"
    assert "JSON.stringify({text: chunk})" in body, (
        "Each /api/tts request should send one safe-size chunk"
    )
    assert "return chain" in body, "Server TTS helper should expose completion as a promise"


def test_stop_tts_invalidates_pending_server_tts_fetches():
    """Stopping during an in-flight server TTS fetch must not allow stale playback."""
    body = _extract_function_body(
        (REPO / "static" / "ui.js").read_text(encoding="utf-8"),
        "stopTTS",
    )
    assert body, "stopTTS() must exist in ui.js"
    assert "_ttsPlayGeneration++" in body, (
        "stopTTS() should invalidate pending _playServerTts fetch/audio callbacks"
    )


def test_tts_provider_label_lowercases_before_i18n_lookup():
    """Provider labels from config/plugin data should match lowercase i18n keys."""
    body = _extract_function_body(
        (REPO / "static" / "panels.js").read_text(encoding="utf-8"),
        "_ttsProviderLabel",
    )
    assert body, "_ttsProviderLabel() must exist in panels.js"
    assert "toLowerCase().replace(/[^a-z0-9_]/g,'_')" in body
    assert "const key='tts_provider_'+safe" in body


def test_tts_defaults_to_browser_when_no_saved_preference():
    """Never-configured installs should use browser speech, not server TTS."""
    ui_src = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    boot_src = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    panels_src = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    assert "localStorage.getItem('hermes-tts-engine')||'browser'" in ui_src
    assert 'localStorage.getItem("hermes-tts-engine")||"browser"' in boot_src
    assert "localStorage.getItem('hermes-tts-engine')||'browser'" in panels_src
    assert "localStorage.getItem('hermes-tts-engine')||'server'" not in ui_src
    assert 'localStorage.getItem("hermes-tts-engine")||"server"' not in boot_src


def test_tts_capability_population_preserves_browser_and_legacy_selection():
    """Capability discovery should populate options without clobbering user prefs."""
    body = _extract_function_body(
        (REPO / "static" / "panels.js").read_text(encoding="utf-8"),
        "_setTtsProviderSelectFromCapability",
    )
    assert body, "_setTtsProviderSelectFromCapability() must exist in panels.js"
    assert "const current=_normalizeTtsEngineValue(" in body, (
        "Saved browser/server/provider preferences must be normalized before applying capability"
    )
    assert "let desired=current==='server'?configOption:current" in body, (
        "Legacy generic server should migrate to the config provider, while browser stays browser"
    )
    normalizer = _extract_function_body(
        (REPO / "static" / "panels.js").read_text(encoding="utf-8"),
        "_normalizeTtsEngineValue",
    )
    assert "return 'server:'+raw" in normalizer, (
        "Legacy direct provider ids like edge/openai should migrate to server:<provider>"
    )


def test_no_tts_capability_falls_back_to_browser_not_broken_edge():
    """An agent with no TTS providers should render a disabled server option and use browser."""
    body = _extract_function_body(
        (REPO / "static" / "panels.js").read_text(encoding="utf-8"),
        "_setTtsProviderSelectFromCapability",
    )
    assert "if(!serverAvailable)" in body
    assert "opt.value='server:unavailable'" in body
    assert "opt.disabled=true" in body
    assert "ttsEngineSel.value='browser'" in body
    assert "localStorage.setItem('hermes-tts-engine','browser')" in body
