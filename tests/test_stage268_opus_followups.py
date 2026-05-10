"""Opus pre-release follow-up tests for v0.50.268.

Pin the three SHOULD-FIX items applied during stage-268 review:

- SF-1 (#1450): child-count UI uses i18n `session_meta_children` key, not hardcoded English.
- SF-2 (#1462): duplicate carries personality / enabled_toolsets / context_length / threshold_tokens.
- SF-3 (#1462): duplicate handles legacy null title via `(session.title or 'Untitled')` fallback.
"""

from pathlib import Path
import re

REPO_ROOT = Path(__file__).parent.parent
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


# --- SF-1 (#1450): child-count UI uses i18n key ---


def test_sf1_child_count_uses_i18n_in_sessions_js():
    """The child-count badge and meta line must call t('session_meta_children', ...).

    Pre-fix, the strings were hardcoded as `${childCount} child${childCount===1?'':'ren'}`
    which rendered English in all 9 locales.
    """
    # Two callsites
    assert "t('session_meta_children', childCount)" in SESSIONS_JS, (
        "session_meta_children i18n key not used in sessions.js — child-count UI "
        "would render English in non-English locales"
    )
    # Negative: hardcoded form must be gone
    assert "${childCount} child${childCount===1?'':'ren'}" not in SESSIONS_JS, (
        "hardcoded English child-count string still present — removes locale support"
    )


def test_sf1_session_meta_children_present_in_all_locales():
    """Every locale block in i18n.js that has session_meta_messages must also
    have session_meta_children — they're the analogous sidebar meta strings."""
    msg_count = len(re.findall(r"session_meta_messages:", I18N_JS))
    child_count = len(re.findall(r"session_meta_children:", I18N_JS))
    assert msg_count == child_count, (
        f"session_meta_messages appears {msg_count} times but "
        f"session_meta_children appears {child_count} — must be in every locale"
    )
    # Sanity: 9 known locales (en, ja, ru, es, de, zh, zh-Hant, plus the legacy zh-tw/zh-hk aliases)
    assert child_count >= 9, (
        f"expected >=9 locales with session_meta_children, got {child_count}"
    )


# --- SF-2 (#1462): duplicate carries per-session settings ---


def test_sf2_duplicate_carries_personality():
    """The duplicate must propagate `personality` from source to copy."""
    duplicate_start = ROUTES_PY.find('if parsed.path == "/api/session/duplicate":')
    assert duplicate_start != -1
    block = ROUTES_PY[duplicate_start : duplicate_start + 3000]
    assert "personality=session.personality" in block, (
        "duplicate must carry over personality — without it, customized "
        "personalities silently revert to default in the copy"
    )


def test_sf2_duplicate_carries_enabled_toolsets():
    """The duplicate must propagate `enabled_toolsets` (per-session toolset overrides)."""
    duplicate_start = ROUTES_PY.find('if parsed.path == "/api/session/duplicate":')
    block = ROUTES_PY[duplicate_start : duplicate_start + 3000]
    assert 'enabled_toolsets=getattr(session, "enabled_toolsets", None)' in block, (
        "duplicate must carry enabled_toolsets — without it, per-session "
        "toolset overrides silently revert to defaults in the copy"
    )


def test_sf2_duplicate_carries_context_settings():
    """The duplicate must propagate context_length + threshold_tokens."""
    duplicate_start = ROUTES_PY.find('if parsed.path == "/api/session/duplicate":')
    block = ROUTES_PY[duplicate_start : duplicate_start + 3000]
    assert 'context_length=getattr(session, "context_length", None)' in block
    assert 'threshold_tokens=getattr(session, "threshold_tokens", None)' in block


# --- SF-3 (#1462): None-title fallback ---


def test_sf3_duplicate_handles_none_title():
    """The duplicate handler must guard `session.title or 'Untitled'` to avoid
    `TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'`
    on legacy sessions with title=null."""
    duplicate_start = ROUTES_PY.find('if parsed.path == "/api/session/duplicate":')
    assert duplicate_start != -1
    block = ROUTES_PY[duplicate_start : duplicate_start + 3000]
    # Must use the (session.title or "Untitled") form, not raw session.title
    assert '(session.title or "Untitled") + " (copy)"' in block, (
        "duplicate must guard against None title — `session.title + ' (copy)'` "
        "TypeErrors when legacy JSON has title=null"
    )
    # Negative: the unguarded form must be gone
    # Allow it inside comment text but not as actual code
    code_lines = [
        ln
        for ln in block.split("\n")
        if not ln.lstrip().startswith("#") and 'title=session.title + " (copy)"' in ln
    ]
    assert not code_lines, (
        f"unguarded `session.title + ' (copy)'` still present in duplicate handler: {code_lines}"
    )
