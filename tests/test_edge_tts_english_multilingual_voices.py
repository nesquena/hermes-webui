"""English multilingual Edge TTS voices: server allowlist and voice-picker parity.

Each ``en-US-*MultilingualNeural`` voice offered by the Edge voice picker in
``static/panels.js`` must be accepted by the server-side allowlist in
``_handle_tts`` and reach synthesis (HTTP 200) through the real ``/api/tts``
handler, with ``edge_tts`` stubbed so no network call happens. The picker side
runs the real ``window._populateTtsVoices`` under Node (shared helper).
"""
import pytest

from tests.test_edge_tts_french_voices import _render_edge_picker_options, _synthesize

VOICES = [
    "en-US-AndrewMultilingualNeural",
    "en-US-AvaMultilingualNeural",
    "en-US-BrianMultilingualNeural",
    "en-US-EmmaMultilingualNeural",
]


def _reset_limiter():
    from api import routes

    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter


@pytest.mark.parametrize("voice", VOICES)
def test_english_multilingual_voice_reaches_synthesis(monkeypatch, voice):
    _reset_limiter()
    status, captured, body = _synthesize(monkeypatch, voice, f"10.97.0.{VOICES.index(voice) + 1}")
    assert status == [200], body
    assert captured == [voice]


def test_edge_picker_lists_english_multilingual_voices_after_aria_and_guy(monkeypatch):
    selected_voice = "en-US-AndrewMultilingualNeural"
    options = _render_edge_picker_options(selected_voice)
    values = [o["value"] for o in options]

    assert len(values) == len(set(values)), f"duplicate picker options: {values}"
    english = [v for v in values if v.startswith("en-US-")]
    # Existing English voices keep their place; the multilingual ones follow them
    # inside the same English group.
    assert english == ["en-US-AriaNeural", "en-US-GuyNeural"] + VOICES
    assert [o["value"] for o in options if o["selected"]] == [selected_voice]
    labels = {o["value"]: o["label"] for o in options}
    assert labels["en-US-AndrewMultilingualNeural"] == "Andrew (English, Male, Multilingual)"
    assert labels["en-US-EmmaMultilingualNeural"] == "Emma (English, Female, Multilingual)"

    # Every English voice the picker renders is accepted by the server allowlist.
    _reset_limiter()
    for idx, voice in enumerate(english, start=1):
        status, captured, body = _synthesize(monkeypatch, voice, f"10.97.1.{idx}")
        assert status == [200], (voice, body)
        assert captured == [voice]
