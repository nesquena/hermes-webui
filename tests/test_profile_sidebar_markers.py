"""Regression tests for #7711: visual profile markers for sidebar sessions.

When the 'Show N from other profiles' toggle is active, sessions from other
profiles must display a colored dot in the sidebar title row and include the
profile name in the row tooltip, so users can identify ownership at a glance.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

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
    The active profile's own sessions must not carry a dot — including a
    renamed root profile, whose own legacy `default` sessions match through
    the shared `_profileMatchesActiveProfile()` predicate rather than a raw
    string comparison."""
    js = _sessions_js()
    assert "sessionProfileName!==activeProfileName" not in js
    assert js.count("_profileMatchesActiveProfile(sessionProfileName, activeProfileName)") == 2


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
    assert js.count("_profileMatchesActiveProfile(sessionProfileName, activeProfileName)") == 2


# ── Behaviour: the foreign decision itself ───────────────────────────────
def _extract_function(source: str, name: str) -> str:
    start = source.find(f"function {name}(")
    assert start != -1, f"Could not find function {name}"
    brace = source.find("{", start)
    depth = 0
    for idx in range(brace, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:idx + 1]
    raise AssertionError(f"Could not extract complete function body for {name}")


def _foreign_decision(session_profile: str, active_profile: str, is_default_root: bool) -> bool:
    """Run the real production predicate in node and return the decision both
    #7711 sites now make: 'does this session belong to another profile?'."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    matcher = _extract_function(_sessions_js(), "_profileMatchesActiveProfile")
    script = "\n".join(
        [
            matcher,
            f"const S = {{ activeProfileIsDefault: {str(is_default_root).lower()} }};",
            f"const sessionProfileName = {json.dumps(session_profile)};",
            f"const activeProfileName = {json.dumps(active_profile)};",
            "const foreign = !!(sessionProfileName && "
            "!_profileMatchesActiveProfile(sessionProfileName, activeProfileName));",
            "console.log(JSON.stringify(foreign));",
        ]
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        script_path = handle.name
    try:
        proc = subprocess.run([node, script_path], check=True, capture_output=True, text=True)
        return json.loads(proc.stdout.strip())
    finally:
        Path(script_path).unlink(missing_ok=True)


def test_own_profile_session_is_not_foreign():
    assert _foreign_decision("bravo", "bravo", is_default_root=False) is False


def test_other_profile_session_is_foreign():
    assert _foreign_decision("bravo", "default", is_default_root=False) is True


def test_renamed_root_own_legacy_default_session_is_not_foreign():
    """The [SILENT] #7711 finding: with 'all profiles' on, a user who renamed
    the root profile saw their own legacy `default` sessions marked foreign."""
    assert _foreign_decision("default", "renamed-root", is_default_root=True) is False


def test_renamed_root_foreign_session_still_marked():
    assert _foreign_decision("bravo", "renamed-root", is_default_root=True) is True
