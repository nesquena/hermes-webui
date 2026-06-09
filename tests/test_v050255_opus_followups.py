"""Regression tests for v0.50.255 Opus pre-release follow-ups.

The v0.50.255 batch (#1390 + #1405) had four Opus advisor findings:

1. MUST-FIX — `api/rollback.py::checkpoint` parameter wasn't validated; the
   path-join `_checkpoint_root() / ws_hash / checkpoint` does NOT normalize
   `..`, so an authenticated caller could pass `../<other-ws-hash>/<sha>` and
   read or restore from another allowlisted workspace's checkpoint store.
   Fix: regex validation that rejects `/`, `..`, and `.`.

2. SHOULD-FIX — `api/helpers.py::_redact_text` called uncached `load_settings()`
   per string, recursed across all messages and tool_calls. For a 50-message
   session that's hundreds of disk reads per `/api/session?session_id=X`. Fix:
   thread `_enabled` once through `redact_session_data()`.

3. SHOULD-FIX — `static/boot.js` voice mode: the patched `autoReadLastAssistant`
   fires globally; if the user navigates to a different session between send
   and stream completion, TTS would speak the wrong session's last assistant
   message. Fix: capture the active session id in `_voiceModeSend` and bail
   out in `_speakResponse` if it doesn't match.

4. NIT — `api/rollback.py::_inspect_checkpoint` had a bare `Exception` in the
   except tuple alongside specific catches, swallowing everything (incl.
   KeyboardInterrupt's siblings). Fix: drop to the specific tuple.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


# ── 1: rollback checkpoint id validation ─────────────────────────────────────


def test_rollback_validates_checkpoint_id_against_path_traversal():
    """The checkpoint param must reject `..`, `/`, and any path-component
    traversal vector. Without this guard, a caller can join the checkpoint
    root with `../<other-ws-hash>/<sha>` and escape the workspace allowlist
    (Path() / '..' does NOT normalize)."""
    src = (REPO / "api" / "rollback.py").read_text(encoding="utf-8")
    # Validator function exists.
    assert "def _validate_checkpoint_id(" in src, (
        "_validate_checkpoint_id must exist as a defense-in-depth guard for "
        "the checkpoint parameter; without it, ../<ws>/<sha> escapes the "
        "workspace allowlist."
    )
    # Both diff + restore call it.
    assert src.count("_validate_checkpoint_id(checkpoint)") >= 2, (
        "both get_checkpoint_diff and restore_checkpoint must call "
        "_validate_checkpoint_id() on their checkpoint parameter."
    )
    # Validator rejects '..' and '.' explicitly.
    assert 'in (".", "..")' in src, (
        "_validate_checkpoint_id must reject literal '.' and '..' explicitly "
        "(not just rely on the regex)."
    )


def test_rollback_validate_checkpoint_id_runtime_behavior():
    """End-to-end test of the validator: traversal attempts raise ValueError."""
    import sys
    sys.path.insert(0, str(REPO))
    from api.rollback import _validate_checkpoint_id

    # Valid SHA-style IDs pass.
    assert _validate_checkpoint_id("abc123def456") == "abc123def456"
    assert _validate_checkpoint_id("a1b2c3-d4e5") == "a1b2c3-d4e5"
    assert _validate_checkpoint_id("checkpoint_2026-05-01") == "checkpoint_2026-05-01"

    # Traversal attempts blocked.
    for bad in (
        "../escape",
        "..",
        ".",
        "../../../etc/passwd",
        "abc/def",
        "abc def",  # space
        "abc\x00def",  # null byte
        "",
        "   ",
        ".hidden",  # leading dot → looks like dotfile escape
        "/abs/path",
        "x" * 65,  # too long
    ):
        with pytest.raises(ValueError):
            _validate_checkpoint_id(bad)


# ── 2: redact_session_data settings.json read-once optimization ──────────────


def test_redact_session_data_reads_settings_once():
    """`redact_session_data()` must read `api_redact_enabled` ONCE per call
    and thread it through the recursive walk via the `_enabled` keyword.
    Calling load_settings per string was a hot-path perf regression."""
    src = (REPO / "api" / "helpers.py").read_text(encoding="utf-8")

    # The function reads settings once and threads _enabled through.
    redact_fn_idx = src.find("def redact_session_data(")
    assert redact_fn_idx != -1, "redact_session_data missing"
    body = src[redact_fn_idx : redact_fn_idx + 1500]
    assert "load_settings()" in body, (
        "redact_session_data must read load_settings() once at the top"
    )
    assert body.count("_enabled=_enabled") >= 3, (
        "redact_session_data must thread _enabled through to title, "
        "messages, and tool_calls (3 call sites)"
    )

    # _redact_text and _redact_value accept _enabled kwarg.
    assert "def _redact_text(text: str, *, _enabled" in src
    assert "def _redact_value(v, *, _enabled" in src


def test_redact_session_data_threads_enabled_once_across_recursion():
    """End-to-end: a session payload with N strings should result in 1 read
    of api_redact_enabled, not N. We verify by counting load_settings calls
    via monkeypatch."""
    import sys
    sys.path.insert(0, str(REPO))
    from api import helpers

    call_count = [0]
    real_load_settings = helpers.__dict__.get("load_settings")

    def counting_load_settings():
        call_count[0] += 1
        return {"api_redact_enabled": True}

    # The from-import inside redact_session_data resolves at call time, so
    # patch in api.config where it lives.
    from api import config
    original = config.load_settings
    config.load_settings = counting_load_settings
    try:
        # Simulate a session payload with many strings
        session = {
            "title": "Test session",
            "messages": [
                {"role": "user", "content": "hello world " * 10}
                for _ in range(20)
            ],
            "tool_calls": [
                {"name": "tool", "args": {"x": "y", "z": ["a", "b", "c"]}}
                for _ in range(10)
            ],
        }
        helpers.redact_session_data(session)
    finally:
        config.load_settings = original

    # Should be called exactly once for the entire response, not per string.
    assert call_count[0] == 1, (
        f"redact_session_data called load_settings() {call_count[0]} times; "
        f"expected exactly 1 (read-once + thread-through optimization)."
    )


# ── 3: voice mode session-id capture ─────────────────────────────────────────


def test_voice_mode_speakresponse_guards_against_session_switch():
    """The `_speakResponse` callback fires from a global override of
    `autoReadLastAssistant`. If the user navigates to a different session
    between sending and stream completion, the callback would TTS-read the
    new session's last assistant message instead of the one they sent to.
    Fix: capture session_id at thinking-time, bail in _speakResponse if it
    doesn't match the current S.session.session_id."""
    src = (REPO / "static" / "boot.js").read_text(encoding="utf-8")

    # Session-id capture state exists.
    assert "let _voiceModeThinkingSid=" in src, (
        "voice mode must declare _voiceModeThinkingSid to pin the active "
        "session id at send-time"
    )

    # _voiceModeSend captures current session_id at thinking transition.
    send_idx = src.find("function _voiceModeSend(")
    assert send_idx != -1
    send_body = src[send_idx : send_idx + 1200]
    assert "_voiceModeThinkingSid=" in send_body, (
        "_voiceModeSend must capture the current session_id at thinking-time"
    )
    assert "S.session.session_id" in send_body, (
        "_voiceModeSend must read S.session.session_id"
    )

    # _speakResponse compares current sid to captured sid and bails on mismatch.
    speak_idx = src.find("function _speakResponse(")
    assert speak_idx != -1
    speak_body = src[speak_idx : speak_idx + 1500]
    assert "_voiceModeThinkingSid" in speak_body, (
        "_speakResponse must consult _voiceModeThinkingSid"
    )
    assert "_startListening()" in speak_body, (
        "_speakResponse mismatch path must drop back to listening, not silently exit"
    )


# ── 4: rollback _inspect_checkpoint except tuple ─────────────────────────────


def test_rollback_inspect_checkpoint_except_no_bare_exception():
    """The bare `Exception` in `(subprocess.TimeoutExpired, OSError, Exception)`
    swallowed everything including KeyboardInterrupt's siblings and made the
    specific catches redundant. Should be the specific tuple only."""
    src = (REPO / "api" / "rollback.py").read_text(encoding="utf-8")
    # No bare Exception in the inspect-checkpoint except tuple.
    assert "(subprocess.TimeoutExpired, OSError, Exception)" not in src, (
        "_inspect_checkpoint must not catch bare Exception alongside specific "
        "catches — the bare Exception swallows everything and makes the "
        "specific ones redundant."
    )
    # The specific tuple is in place.
    assert "(subprocess.TimeoutExpired, OSError)" in src
