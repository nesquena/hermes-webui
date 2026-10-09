"""Regression tests for #7711: visual profile markers for sidebar sessions.

When the 'Show N from other profiles' toggle is active, sessions from other
profiles must display a colored dot in the sidebar title row and include the
profile name in the row tooltip, so users can identify ownership at a glance.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
STYLE_CSS_PATH = REPO_ROOT / "static" / "style.css"


def _sessions_js() -> str:
    return SESSIONS_JS_PATH.read_text(encoding="utf-8")


def _style_css() -> str:
    return STYLE_CSS_PATH.read_text(encoding="utf-8")


# ── Color palette ────────────────────────────────────────────────────────

def test_profile_color_palette_exists():
    """The _PROFILE_COLORS palette must be defined in sessions.js."""
    js = _sessions_js()
    assert "const _PROFILE_COLORS" in js
    assert "_PROFILE_COLORS = [" in js


def test_profile_color_hash_function_exists():
    """A deterministic _profileColor(name) function must exist."""
    js = _sessions_js()
    assert "function _profileColor(name)" in js


def test_profile_color_cache_exists():
    """A Map-based cache must avoid recomputing the hash on every render."""
    js = _sessions_js()
    assert "_profileColorCache" in js
    assert "new Map()" in js


# ── Sidebar dot rendering ────────────────────────────────────────────────

def test_profile_dot_rendered_in_title_row():
    """When _showAllProfiles is true, a .session-profile-dot must be appended
    to the title row for sessions belonging to a different profile."""
    js = _sessions_js()
    assert "session-profile-dot" in js
    assert "_showAllProfiles && s.profile" in js


def test_profile_dot_only_for_other_profiles():
    """The dot must only appear for sessions NOT matching the active profile.
    The active profile's own sessions should not carry a dot."""
    js = _sessions_js()
    assert "sessionProfileName!==activeProfileName" in js


def test_profile_dot_gets_colored():
    """The dot's background must be set via _profileColor()."""
    js = _sessions_js()
    assert "const dotColor=_profileColor(sessionProfileName);" in js


def test_profile_dot_tooltip():
    """The dot element must carry a title attribute with the profile name."""
    js = _sessions_js()
    assert "profileDot.title=sessionProfileName;" in js


# ── CSS styling ──────────────────────────────────────────────────────────

def test_profile_dot_css_exists():
    """The .session-profile-dot CSS class must be defined in style.css."""
    css = _style_css()
    assert ".session-profile-dot{" in css


def test_profile_dot_is_circle():
    """The profile dot must be a 7px circle via border-radius:50%."""
    css = _style_css()
    assert ".session-profile-dot{" in css
    # Extract the full rule for the class
    start = css.index(".session-profile-dot{")
    # Find the closing brace
    depth = 0
    end = start
    for i in range(start, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    rule = css[start:end]
    assert "border-radius:50%" in rule
    assert "width:7px" in rule
    assert "height:7px" in rule


def test_profile_dot_flex_siblings():
    """The profile dot must be flex-shrink:0 so it stays visible."""
    css = _style_css()
    start = css.index(".session-profile-dot{")
    depth = 0
    end = start
    for i in range(start, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    rule = css[start:end]
    assert "flex-shrink:0" in rule


# ── Tooltip enrichment ───────────────────────────────────────────────────

def test_tooltip_includes_profile_name():
    """_sessionFullTitleTooltip must append [profile] when cross-profile list
    is active and the session belongs to a different profile."""
    js = _sessions_js()
    assert "title=title + ' [' + sessionProfileName + ']'" in js


def test_tooltip_profile_guard():
    """The tooltip enrichment must check _showAllProfiles and profile mismatch
    to avoid polluting the tooltip for single-profile mode."""
    js = _sessions_js()
    assert "_showAllProfiles && session && typeof session.profile" in js
    assert "sessionProfileName!==activeProfileName" in js
