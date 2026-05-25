"""
Tests for PR #2910: Edge TTS server endpoint and client integration.

Covers:
- Static analysis: client-side functions exist and use POST
- Server-side: auth, optional dep, validation patterns in routes.py
- UI: engine selector, voice population
"""
import os
import re

import pytest

STATIC_DIR = os.path.join(os.path.dirname(__file__), '..', 'static')

# Allow optional import of api.routes for deep inspection
ROUTES_FILE = os.path.join(os.path.dirname(__file__), '..', 'api', 'routes.py')


def _read(filename):
    return open(os.path.join(STATIC_DIR, filename), encoding='utf-8').read()


def _read_routes():
    return open(ROUTES_FILE, encoding='utf-8').read()


# ── Static analysis: client-side code ────────────────────────────────────────

class TestClientEdgeTtsFunction:
    """_playEdgeTts function exists and uses POST."""

    def test_play_edge_tts_exists(self):
        src = _read('ui.js')
        assert 'function _playEdgeTts(' in src, \
            "_playEdgeTts function not found in ui.js"

    def test_play_edge_tts_uses_post(self):
        """_playEdgeTts must use POST with JSON body."""
        src = _read('ui.js')
        assert "method:'POST'" in src and "'Content-Type':'application/json'" in src, \
            "_playEdgeTts must POST JSON instead of GET query string"

    def test_play_edge_tts_uses_fetch(self):
        """_playEdgeTts must use fetch(), not Audio() with a GET URL."""
        src = _read('ui.js')
        fetch_line = [l for l in src.splitlines() if 'fetch(' in l and '/api/tts' in l]
        assert fetch_line, \
            "_playEdgeTts must call fetch('/api/tts', {method:'POST', ...})"


class TestClientEdgeTtsIntegration:
    """speakMessage and stopTTS integrate with Edge TTS."""

    def test_speak_message_dispatches_edge(self):
        """speakMessage must call _playEdgeTts for edge engine."""
        src = _read('ui.js')
        assert "_playEdgeTts(clean, btn)" in src or "_playEdgeTts(clean,btn)" in src, \
            "speakMessage must call _playEdgeTts"

    def test_speak_message_engine_condition(self):
        """speakMessage must check hermes-tts-engine before dispatching."""
        src = _read('ui.js')
        engine_lines = [l for l in src.splitlines()
                       if 'hermes-tts-engine' in l]
        assert engine_lines, \
            "hermes-tts-engine not referenced in speakMessage"

    def test_play_edge_tts_sets_speaking(self):
        """_playEdgeTts must set btn.dataset.speaking."""
        src = _read('ui.js')
        start = src.index('function _playEdgeTts(')
        block = src[start:start + 2000]
        assert "dataset.speaking='1'" in block, \
            "_playEdgeTts must set dataset.speaking='1'"

    def test_stop_tts_handles_edge_audio(self):
        """stopTTS must clean up _playingEdgeAudio."""
        src = _read('ui.js')
        assert '_playingEdgeAudio.pause()' in src or '_playingEdgeAudio.pause' in src, \
            "stopTTS must pause _playingEdgeAudio"


class TestClientEngineSelector:
    """TTS engine selector in settings UI."""

    def test_engine_selector_exists(self):
        """settingsTtsEngine must be present in index.html."""
        src = _read('index.html')
        assert 'settingsTtsEngine' in src, \
            "TTS engine selector not found in index.html"

    def test_edge_option_exists(self):
        """Edge TTS option must be present in the engine selector."""
        src = _read('index.html')
        assert 'value="edge"' in src and 'Edge TTS' in src, \
            "Edge TTS option not found in index.html"

    def test_engine_selector_wired_in_panels(self):
        """Engine selector must be wired in panels.js."""
        src = _read('panels.js')
        assert 'settingsTtsEngine' in src and 'hermes-tts-engine' in src, \
            "TTS engine selector not wired in panels.js"

    def test_voice_population_for_edge(self):
        """_populateTtsVoices must list Edge voices when engine=edge."""
        src = _read('panels.js')
        assert "engine==='edge'" in src or 'engine==="edge"' in src, \
            "Edge voice population not found in _populateTtsVoices"

    def test_edge_voices_listed(self):
        """At least one Edge voice must be defined."""
        src = _read('panels.js')
        assert 'Xiaoxiao' in src, \
            "Xiaoxiao Edge voice not found in panels.js"


class TestClientBootVoiceMode:
    """Voice mode in boot.js dispatches to Edge TTS."""

    def test_voice_mode_edge_branch(self):
        """boot.js must handle engine==='edge' in voice mode."""
        src = _read('boot.js')
        assert "engine===\"edge\"" in src or "engine==='edge'" in src, \
            "Edge TTS branch not found in boot.js voice mode"

    def test_voice_mode_edge_uses_post(self):
        """boot.js edge TTS must use POST with JSON."""
        src = _read('boot.js')
        voice_block = src[src.index('engine==="edge"'):][:2000]
        assert "method:'POST'" in voice_block or 'method:"POST"' in voice_block, \
            "boot.js Edge voice mode must use POST"


# ── Server-side static analysis (routes.py) ──────────────────────────────────

def _get_tts_function():
    """Extract the _handle_tts function block from routes.py source."""
    src = _read_routes()
    m = re.search(r'def _handle_tts\([^)]+\):.*?(?=\ndef |\Z)', src, re.DOTALL)
    assert m, "_handle_tts function not found in routes.py"
    return m.group()


class TestServerAuth:
    """Auth gating for /api/tts."""

    def test_auth_check_matches_media_pattern(self):
        """_handle_tts must have auth check like _handle_media (401 rejection)."""
        fn = _get_tts_function()
        assert 'send_response(401)' in fn or 'is_auth_enabled' in fn, \
            "_handle_tts must include auth check with 401 rejection"
        assert 'Authentication required' in fn, \
            "_handle_tts must return auth error message"

    def test_auth_uses_is_auth_enabled(self):
        """Auth gate must use is_auth_enabled / parse_cookie / verify_session."""
        src = _read_routes()
        # _handle_tts must import auth helpers inside the function
        assert 'from api.auth import is_auth_enabled, parse_cookie, verify_session' in src, \
            "_handle_tts must import auth helpers for cookie verification"

    def test_no_auth_bypass(self):
        """_handle_tts must not have the old broken auth pattern (if ... pass)."""
        fn = _get_tts_function()
        # The old pattern did `if _auth_enabled(): cv = ...; if cv and _verify_session(cv): pass`
        # This silently passed when auth failed instead of returning 401.
        assert 'if cv and _verify_session(cv): pass' not in fn, \
            "Old broken auth pattern found — must return 401 instead of no-op"


class TestServerPostEndpoint:
    """Endpoint must use POST with JSON body."""

    def test_removed_from_handle_get(self):
        """_handle_tts must NOT be called from handle_get (was moved to POST)."""
        src = _read_routes()
        get_section = src[src.index('def handle_get('):src.index('def handle_post(')]
        assert '/api/tts' not in get_section, \
            "/api/tts must not be handled in handle_get"

    def test_called_from_handle_post(self):
        """_handle_tts must be called from handle_post."""
        src = _read_routes()
        post_section = src[src.index('def handle_post('):]
        assert '/api/tts' in post_section, \
            "/api/tts must be handled in handle_post"

    def test_reads_from_body_dict(self):
        """_handle_tts must read text/voice/rate/pitch from body (POST JSON)."""
        fn = _get_tts_function()
        assert 'body.get' in fn, \
            "_handle_tts must read parameters from body dict, not parse_qs"
        assert 'parse_qs' not in fn, \
            "_handle_tts must not use parse_qs (GET query string)"

    def test_signature_takes_body(self):
        """_handle_tts signature must accept body parameter."""
        src = _read_routes()
        assert 'def _handle_tts(handler, parsed, body):' in src, \
            "_handle_tts must accept (handler, parsed, body)"


class TestServerOptionalDep:
    """edge-tts must be optional with 503 fallback."""

    def test_import_error_handled(self):
        """_handle_tts must catch ImportError for edge-tts."""
        fn = _get_tts_function()
        assert 'import edge_tts' in fn, \
            "edge_tts import must be inside _handle_tts"
        assert 'except ImportError' in fn, \
            "ImportError for edge_tts must be caught"

    def test_503_returned_when_missing(self):
        """_handle_tts must return 503 when edge-tts is not installed."""
        fn = _get_tts_function()
        assert '503' in fn[fn.index('except ImportError'):], \
            "503 status must be returned when edge_tts is unavailable"

    def test_no_top_level_import(self):
        """edge_tts must not be imported at module level."""
        src = _read_routes()
        lines = src.splitlines()
        top_imports = [l for l in lines[:100] if 'edge_tts' in l]
        assert not top_imports, \
            "edge_tts must not be imported at module level"


class TestServerPerfCleanup:
    """Async streaming and BytesIO instead of sync temp file."""

    def test_uses_async_stream(self):
        """_handle_tts must use edge_tts.Communicate.stream() async."""
        fn = _get_tts_function()
        assert 'communicate.stream()' in fn, \
            "Must use async stream() instead of save_sync()"
        assert 'save_sync' not in fn, \
            "Must not use blocking save_sync()"

    def test_uses_bytesio(self):
        """_handle_tts must use BytesIO instead of temp file."""
        fn = _get_tts_function()
        assert 'BytesIO' in fn, \
            "Must use BytesIO instead of temp file"
        assert 'tempfile' not in fn, \
            "Must not use tempfile for TTS audio"

    def test_uses_asyncio_timeout(self):
        """_handle_tts must use asyncio.wait_for with timeout."""
        fn = _get_tts_function()
        assert 'asyncio.wait_for' in fn or 'wait_for(' in fn, \
            "Must use asyncio.wait_for with timeout"
        assert 'timeout=30' in fn, \
            "Must set 30s timeout for TTS generation"

    def test_handles_timeout_error(self):
        """_handle_tts must handle asyncio.TimeoutError."""
        fn = _get_tts_function()
        assert 'TimeoutError' in fn, \
            "Must catch asyncio.TimeoutError"


class TestServerTextValidation:
    """Text parameter validation."""

    def test_requires_text(self):
        """_handle_tts must return 400 when text is missing."""
        fn = _get_tts_function()
        assert 'text parameter required' in fn, \
            "Must return 400 when text is missing"
        assert '400' in fn[:fn.index('import edge_tts')] if 'import edge_tts' in fn else True, \
            "text validation must happen before processing"

    def test_truncates_long_text(self):
        """_handle_tts must truncate text longer than 5000 chars."""
        fn = _get_tts_function()
        assert 'text = text[:5000]' in fn, \
            "Must truncate text to 5000 characters"

    def test_voice_default(self):
        """_handle_tts must default voice to zh-CN-XiaoxiaoNeural."""
        fn = _get_tts_function()
        assert 'zh-CN-XiaoxiaoNeural' in fn, \
            "Default voice must be zh-CN-XiaoxiaoNeural"
