"""Regression tests for #4571 insecure-origin microphone messaging.

Browser speech/microphone APIs can report permission-style errors when the real
problem is that the page was opened over insecure HTTP from a non-local origin.
The UI must explain the HTTPS/localhost requirement instead of only saying the
browser permission was denied.
"""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def _locale_count() -> int:
    return len(re.findall(r"^  ['\"]?[\w-]+['\"]?: \{", I18N_JS, re.MULTILINE))


def _slice_between(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def test_i18n_key_exists_in_every_locale_and_names_https_localhost():
    assert I18N_JS.count("mic_insecure_origin") == _locale_count()

    english = re.search(r"mic_insecure_origin: '([^']+)'", I18N_JS)
    assert english, "English mic_insecure_origin string must exist"
    value = english.group(1).lower()
    assert "https" in value
    assert "localhost" in value
    assert "microphone" in value


def test_secure_context_and_localhost_loopback_are_explicitly_preserved():
    assert "function _micOriginNeedsSecureContext()" in BOOT_JS
    assert "window.isSecureContext===true" in BOOT_JS
    assert "protocol==='http:'" in BOOT_JS
    assert "!_micIsLocalhostOrLoopback(loc.hostname)" in BOOT_JS

    localhost_helper = _slice_between(
        BOOT_JS,
        "function _micIsLocalhostOrLoopback(hostname)",
        "\n}\n\nfunction _micOriginNeedsSecureContext",
    )
    assert "host==='localhost'" in localhost_helper
    assert "host.endsWith('.localhost')" in localhost_helper
    assert "host==='::1'" in localhost_helper
    assert "/^127\\./.test(host)" in localhost_helper


def test_permission_style_speech_errors_use_insecure_origin_key_only_on_that_branch():
    helper = _slice_between(
        BOOT_JS,
        "function _micToastKeyForRecognitionError(error)",
        "\n}\n\n(function(){",
    )
    for error in ("not-allowed", "service-not-allowed", "audio-capture"):
        assert error in helper
    assert "_micOriginNeedsSecureContext()" in helper
    assert "return 'mic_insecure_origin';" in helper
    assert "'audio-capture':'mic_denied'" not in helper
    assert "'network':'mic_network'" in helper
    assert "'no-speech':'mic_no_speech'" in helper


def test_dictation_preflights_insecure_origin_before_speech_or_media_capture():
    body = _slice_between(
        BOOT_JS,
        "async function _startMicCapture(holdRequired=false){",
        "\n\n  async function _toggleMicCapture(){",
    )
    insecure_idx = body.index("_micOriginNeedsSecureContext()")
    speech_idx = body.index("recognition.start()")
    media_idx = body.index("navigator.mediaDevices.getUserMedia")

    assert insecure_idx < speech_idx
    assert insecure_idx < media_idx
    assert "showToast(t('mic_insecure_origin'))" in body


def test_dictation_speechrecognition_errors_route_through_shared_helper():
    body = _slice_between(
        BOOT_JS,
        "sr.onerror=(event)=>{",
        "\n    return sr;",
    )
    assert "_micToastKeyForRecognitionError(event.error)" in body
    assert "messageKey?t(messageKey):t('mic_error')+event.error" in body


def test_voice_mode_preflights_and_routes_permission_errors_through_shared_helper():
    activate_body = _slice_between(
        BOOT_JS,
        "function _activate(){",
        "\n  function _deactivate(){",
    )
    assert "_micOriginNeedsSecureContext()" in activate_body
    assert "showToast(t('mic_insecure_origin'))" in activate_body
    assert "_voiceModeActive=true" in activate_body
    assert activate_body.index("_micOriginNeedsSecureContext()") < activate_body.index("_voiceModeActive=true")

    start_body = _slice_between(
        BOOT_JS,
        "function _startListening(){",
        "\n  function _voiceModeSend(){",
    )
    assert "_micOriginNeedsSecureContext()" in start_body
    assert "showToast(t('mic_insecure_origin'))" in start_body
    assert "_micToastKeyForRecognitionError(event.error)" in start_body
    assert "messageKey?t(messageKey):t('mic_error')+event.error" in start_body
