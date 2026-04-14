"""
Tests for sprint-42 fix: issue #441
Gateway sessions with [SYSTEM: prefix in title must be replaced
with a human-friendly platform name in the sidebar.
"""
import os

SESSIONS_JS = os.path.join(os.path.dirname(__file__), '..', 'static', 'sessions.js')

def _read_sessions_js():
    with open(SESSIONS_JS, 'r') as f:
        return f.read()


def test_system_prompt_title_guard_exists():
    """The guard that detects [SYSTEM: prefixes must be present in sessions.js."""
    content = _read_sessions_js()
    assert '[SYSTEM:' in content, \
        "sessions.js must contain the [SYSTEM: guard to intercept system-prompt titles"
    # Make sure it appears in an if-condition context, not just a comment
    assert "cleanTitle.startsWith('[SYSTEM:')" in content, \
        "sessions.js must have: cleanTitle.startsWith('[SYSTEM:') guard expression"


def test_source_display_map_defined():
    """The _SOURCE_DISPLAY lookup map must be present and include core gateway platforms."""
    content = _read_sessions_js()
    assert '_SOURCE_DISPLAY' in content, \
        "sessions.js must define _SOURCE_DISPLAY mapping for platform name lookup"
    # Verify key platform entries are present
    for platform in ("telegram:'Telegram'", "discord:'Discord'", "cli:'CLI'"):
        assert platform in content, \
            f"_SOURCE_DISPLAY must include entry for {platform}"


def test_cleanTitle_is_let_not_const():
    """cleanTitle must be declared with let (not const) to allow reassignment in the guard."""
    content = _read_sessions_js()
    assert 'let cleanTitle' in content, \
        "cleanTitle must be declared with 'let' (not 'const') to allow reassignment"
    # Make sure the old const form is gone in this context
    # (check the specific assignment line pattern)
    assert "const cleanTitle=tags.length" not in content, \
        "Old 'const cleanTitle=tags.length...' must be replaced by 'let cleanTitle=...'"
