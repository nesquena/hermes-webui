"""
Sprint 20 Tests: Voice input (mic button) via Web Speech API.

These tests verify the static assets contain the correct HTML structure,
CSS rules, and JS logic for the mic feature — all of which runs purely in
the browser with no server-side component.
"""
import re
import urllib.request
import json
import pathlib

from tests._pytest_port import BASE


def get_text(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.read().decode(), r.status


# ── index.html ────────────────────────────────────────────────────────────


def test_mic_button_present_in_html():
    """index.html must contain the mic button with id='btnMic'."""
    html, status = get_text("/")
    assert status == 200
    assert 'id="btnMic"' in html


def test_mic_button_has_mic_btn_class():
    """btnMic must carry the mic-btn CSS class for styling hooks."""
    html, _ = get_text("/")
    assert 'class="icon-btn mic-btn"' in html


def test_mic_button_hidden_by_default():
    """btnMic starts hidden (display:none) — JS shows it only if supported."""
    html, _ = get_text("/")
    # The button element should have display:none in its style attribute
    assert 'id="btnMic"' in html
    btn_match = re.search(r'id="btnMic"[^>]*>', html)
    assert btn_match, "btnMic element not found"
    assert 'display:none' in btn_match.group(0)


def test_mic_button_has_title():
    """btnMic should have a descriptive title for accessibility."""
    html, _ = get_text("/")
    btn_match = re.search(r'id="btnMic"[^>]*>', html)
    assert btn_match
    assert 'title=' in btn_match.group(0)


def test_mic_status_div_present():
    """index.html must contain the #micStatus listening indicator."""
    html, _ = get_text("/")
    assert 'id="micStatus"' in html


def test_mic_status_hidden_by_default():
    """#micStatus starts hidden — only shown during active recording."""
    html, _ = get_text("/")
    status_match = re.search(r'id="micStatus"[^>]*>', html)
    assert status_match, "#micStatus element not found"
    assert 'display:none' in status_match.group(0)


def test_mic_status_has_mic_dot():
    """#micStatus must contain a .mic-dot element for the pulse animation."""
    html, _ = get_text("/")
    # mic-dot should appear after micStatus
    idx_status = html.find('id="micStatus"')
    idx_dot = html.find('mic-dot', idx_status)
    assert idx_status != -1 and idx_dot != -1
    assert idx_dot > idx_status


def test_mic_status_has_listening_text():
    """#micStatus should display a 'Listening' label."""
    html, _ = get_text("/")
    assert 'Listening' in html


def test_mic_button_svg_microphone_shape():
    """btnMic SVG must include the rect (mic body) and path (mic arc)."""
    html, _ = get_text("/")
    # Find mic button section
    btn_start = html.find('id="btnMic"')
    btn_end = html.find('</button>', btn_start) + len('</button>')
    btn_html = html[btn_start:btn_end]
    assert '<rect' in btn_html, "mic SVG missing rect (mic body)"
    assert '<path' in btn_html, "mic SVG missing path (arc)"
    assert '<line' in btn_html, "mic SVG missing line (stand)"


def test_mic_button_inside_composer_left():
    """btnMic must be inside .composer-left, next to the attach button."""
    html, _ = get_text("/")
    composer_left_start = html.find('class="composer-left"')
    composer_left_end = html.find('</div>', composer_left_start)
    section = html[composer_left_start:composer_left_end]
    assert 'btnAttach' in section
    assert 'btnMic' in section


# ── style.css ────────────────────────────────────────────────────────────


def test_mic_btn_css_rule_exists():
    """style.css must define .mic-btn rule."""
    css, status = get_text("/static/style.css")
    assert status == 200
    assert '.mic-btn' in css


def test_mic_btn_recording_state_css():
    """.mic-btn.recording must be defined for active recording visual state."""
    css, _ = get_text("/static/style.css")
    assert '.mic-btn.recording' in css


def test_mic_recording_color_error():
    """.mic-btn.recording must use the error color variable or red."""
    css, _ = get_text("/static/style.css")
    recording_idx = css.find('.mic-btn.recording')
    # Find the rule block after the selector
    brace_open = css.find('{', recording_idx)
    brace_close = css.find('}', brace_open)
    rule = css[brace_open:brace_close]
    assert 'var(--error)' in rule or '#e94560' in rule


def test_mic_recording_has_animation():
    """.mic-btn.recording must use an animation for the pulse effect."""
    css, _ = get_text("/static/style.css")
    recording_idx = css.find('.mic-btn.recording')
    brace_open = css.find('{', recording_idx)
    brace_close = css.find('}', brace_open)
    rule = css[brace_open:brace_close]
    assert 'animation' in rule


def test_mic_pulse_keyframes_defined():
    """@keyframes mic-pulse must be defined for the pulsing animation."""
    css, _ = get_text("/static/style.css")
    assert 'mic-pulse' in css
    assert '@keyframes' in css


def test_mic_status_css_rule_exists():
    """style.css must define .mic-status rule."""
    css, _ = get_text("/static/style.css")
    assert '.mic-status' in css


def test_mic_dot_css_rule_exists():
    """style.css must define .mic-dot rule with animation."""
    css, _ = get_text("/static/style.css")
    assert '.mic-dot' in css
    dot_idx = css.find('.mic-dot')
    brace_open = css.find('{', dot_idx)
    brace_close = css.find('}', brace_open)
    rule = css[brace_open:brace_close]
    assert 'animation' in rule


def test_mic_btn_has_transition():
    """.mic-btn must define a transition for smooth state changes."""
    css, _ = get_text("/static/style.css")
    mic_btn_idx = css.find('.mic-btn{')
    if mic_btn_idx == -1:
        mic_btn_idx = css.find('.mic-btn ')
    brace_open = css.find('{', mic_btn_idx)
    brace_close = css.find('}', brace_open)
    rule = css[brace_open:brace_close]
    assert 'transition' in rule


# ── boot.js ──────────────────────────────────────────────────────────────


def test_boot_js_serves_ok():
    """boot.js must be served successfully."""
    _, status = get_text("/static/boot.js")
    assert status == 200


def test_boot_js_speech_recognition_check():
    """boot.js must check for SpeechRecognition (with webkit fallback)."""
    js, _ = get_text("/static/boot.js")
    assert 'SpeechRecognition' in js
    assert 'webkitSpeechRecognition' in js


def test_boot_js_recognition_config():
    """boot.js must configure recognition.continuous, interimResults, and lang."""
    js, _ = get_text("/static/boot.js")
    assert 'recognition.continuous' in js
    assert 'recognition.interimResults' in js
    assert 'recognition.lang' in js


def test_boot_js_recognition_not_continuous():
    """recognition.continuous must be false (auto-stop after silence)."""
    js, _ = get_text("/static/boot.js")
    assert 'recognition.continuous=false' in js or 'recognition.continuous = false' in js


def test_boot_js_recognition_interim_results():
    """recognition.interimResults must be true (live transcription preview)."""
    js, _ = get_text("/static/boot.js")
    assert 'recognition.interimResults=true' in js or 'recognition.interimResults = true' in js


def test_boot_js_recognition_lang_en():
    """recognition.lang must be set (static en-US or dynamic via _locale._speech)."""
    js, _ = get_text("/static/boot.js")
    # Accept either the old static value or the new locale-driven assignment
    assert (
        "recognition.lang='en-US'" in js
        or 'recognition.lang = "en-US"' in js
        or "recognition.lang=" in js  # dynamic: recognition.lang=(_locale._speech)||'en-US'
    )


def test_boot_js_onresult_handler():
    """boot.js must define recognition.onresult to handle transcription."""
    js, _ = get_text("/static/boot.js")
    assert 'recognition.onresult' in js


def test_boot_js_onend_handler():
    """boot.js must define recognition.onend to reset state when recording stops."""
    js, _ = get_text("/static/boot.js")
    assert 'recognition.onend' in js


def test_boot_js_onerror_handler():
    """boot.js must define recognition.onerror for graceful error handling."""
    js, _ = get_text("/static/boot.js")
    assert 'recognition.onerror' in js


def test_boot_js_not_allowed_error_message():
    """onerror must handle 'not-allowed' with a user-friendly message."""
    js, _ = get_text("/static/boot.js")
    assert 'not-allowed' in js
    assert 'permission' in js.lower() or 'denied' in js.lower() or 'access' in js.lower()


def test_boot_js_no_speech_error_message():
    """onerror must handle 'no-speech' with a user-friendly message."""
    js, _ = get_text("/static/boot.js")
    assert 'no-speech' in js


def test_boot_js_network_error_message():
    """onerror must handle 'network' error."""
    js, _ = get_text("/static/boot.js")
    assert "'network'" in js or '"network"' in js


def test_boot_js_mic_active_flag():
    """boot.js must track recording state via _micActive flag."""
    js, _ = get_text("/static/boot.js")
    assert '_micActive' in js


def test_boot_js_mic_recording_class_toggle():
    """boot.js must toggle 'recording' CSS class on the mic button."""
    js, _ = get_text("/static/boot.js")
    assert "'recording'" in js or '"recording"' in js


def test_boot_js_mic_status_toggle():
    """boot.js must show/hide #micStatus during recording."""
    js, _ = get_text("/static/boot.js")
    assert 'micStatus' in js


def test_boot_js_send_stops_mic():
    """btnSend onclick must stop mic before sending (send guard)."""
    js, _ = get_text("/static/boot.js")
    # The send button onclick should check _micActive and stop recording
    send_onclick_idx = js.find("$('btnSend').onclick")
    assert send_onclick_idx != -1
    # Find the handler code — check that _micActive check appears near send assignment
    handler_end = js.find(';', send_onclick_idx)
    handler = js[send_onclick_idx:handler_end + 1]
    assert '_micActive' in handler or 'stopMic' in handler.lower()


def test_boot_js_btn_mic_onclick():
    """boot.js must attach an onclick handler to btnMic."""
    js, _ = get_text("/static/boot.js")
    assert 'btn.onclick' in js or "btnMic.onclick" in js or "$('btnMic').onclick" in js


def test_boot_js_recognition_start():
    """boot.js must call recognition.start() to begin recording."""
    js, _ = get_text("/static/boot.js")
    assert 'recognition.start()' in js


def test_boot_js_recognition_stop():
    """boot.js must call recognition.stop() to end recording."""
    js, _ = get_text("/static/boot.js")
    assert 'recognition.stop()' in js


def test_boot_js_iife_guard():
    """Mic logic must be wrapped in an IIFE so it doesn't pollute global scope."""
    js, _ = get_text("/static/boot.js")
    # IIFE pattern: (function(){...})() or (() => {...})()
    assert '(function(){' in js or '(function () {' in js


def test_boot_js_browser_unsupported_guard_uses_fallback_capabilities():
    """boot.js must keep the mic available when either speech recognition OR recorder capture exists."""
    js, _ = get_text("/static/boot.js")
    assert 'navigator.mediaDevices' in js
    assert 'getUserMedia' in js
    assert 'MediaRecorder' in js
    assert '_canRecordAudio' in js or 'canRecordAudio' in js, \
        "boot.js should compute a recorder fallback instead of bailing only on SpeechRecognition"


def test_boot_js_media_recorder_fallback_posts_to_transcribe_api():
    """Desktop fallback must send recorded audio to /api/transcribe for transcription."""
    js, _ = get_text("/static/boot.js")
    assert 'api/transcribe' in js
    assert 'fetch(' in js


def test_routes_define_transcribe_endpoint():
    """Server routes must expose /api/transcribe for MediaRecorder fallback uploads."""
    routes = pathlib.Path(__file__).parent.parent.joinpath("api/routes.py").read_text(encoding="utf-8")
    assert '"/api/transcribe"' in routes


def test_boot_js_shows_mic_button_when_any_voice_path_is_supported():
    """boot.js must reveal btnMic when speech recognition or recorder fallback is available."""
    js, _ = get_text("/static/boot.js")
    assert "btn.style.display=''" in js or 'btn.style.display = ""' in js


def test_boot_js_show_toast_on_error():
    """boot.js must call showToast() for mic errors."""
    js, _ = get_text("/static/boot.js")
    assert 'showToast' in js


def test_boot_js_autoresize_called():
    """boot.js must call autoResize() after updating textarea from transcript."""
    js, _ = get_text("/static/boot.js")
    assert 'autoResize()' in js


# ── Append behaviour (fix: mic appends to existing text, not replace) ────


def test_boot_js_prefix_variable_declared():
    """boot.js must declare _prefix variable to snapshot pre-existing textarea content."""
    js, _ = get_text("/static/boot.js")
    assert "_prefix" in js


def test_boot_js_prefix_captured_on_start():
    """_prefix must be set from ta.value when the user starts recording."""
    js, _ = get_text("/static/boot.js")
    # _prefix assignment must happen in the btn.onclick else branch (before recognition.start)
    btn_onclick_idx = js.find("btn.onclick")
    btn_onclick_end = js.find("};", btn_onclick_idx)
    onclick_body = js[btn_onclick_idx:btn_onclick_end]
    assert "_prefix=ta.value" in onclick_body or "_prefix = ta.value" in onclick_body


def test_boot_js_onresult_prepends_prefix():
    """onresult must include _prefix when writing to textarea (append, not replace)."""
    js, _ = get_text("/static/boot.js")
    onresult_idx = js.find("recognition.onresult")
    onresult_end = js.find("};", onresult_idx)
    onresult_body = js[onresult_idx:onresult_end]
    # ta.value must be set to _prefix + something, not just the transcript alone
    assert "_prefix" in onresult_body


def test_boot_js_onend_commits_with_prefix():
    """onend must commit _prefix + _finalText so appended text survives after recognition ends."""
    js, _ = get_text("/static/boot.js")
    onend_idx = js.find("recognition.onend")
    onend_end = js.find("};", onend_idx)
    onend_body = js[onend_idx:onend_end]
    assert "_prefix" in onend_body


def test_boot_js_prefix_reset_on_stop():
    """_prefix must be reset when recording stops so next session starts clean."""
    js, _ = get_text("/static/boot.js")
    # _setRecording(false) clears both _finalText and _prefix
    set_rec_idx = js.find("function _setRecording")
    set_rec_end = js.find("}", set_rec_idx) + 1
    fn_body = js[set_rec_idx:set_rec_end]
    assert "_prefix" in fn_body


def test_boot_js_auto_space_between_prefix_and_transcript():
    """onend must insert a space between existing text and new transcript when needed."""
    js, _ = get_text("/static/boot.js")
    onend_idx = js.find("recognition.onend")
    onend_end = js.find("};", onend_idx)
    onend_body = js[onend_idx:onend_end]
    # Should handle spacing — look for trimStart or endsWith(' ') check
    has_spacing = ("trimStart" in onend_body or "endsWith(' ')" in onend_body
                   or "endsWith(\" \")" in onend_body or "endsWith('\\n')" in onend_body)
    assert has_spacing, "onend should handle spacing between prefix and new transcript"


# ── Regression: existing behaviour unchanged ──────────────────────────────


def test_attach_button_still_wired():
    """btnAttach onclick must still be wired up (no regression)."""
    js, _ = get_text("/static/boot.js")
    assert "$('btnAttach').onclick" in js


def test_file_input_onchange_still_wired():
    """fileInput onchange must still be wired up (no regression)."""
    js, _ = get_text("/static/boot.js")
    assert "$('fileInput').onchange" in js


def test_index_html_still_has_send_button():
    """btnSend must still be present in index.html (no regression)."""
    html, _ = get_text("/")
    assert 'id="btnSend"' in html


def test_index_html_still_has_attach_button():
    """btnAttach must still be present in index.html (no regression)."""
    html, _ = get_text("/")
    assert 'id="btnAttach"' in html
