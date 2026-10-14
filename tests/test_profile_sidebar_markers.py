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


# ── Stylesheet parse: the dot rule must not eat the #8044 comment ────────

def _strip_css_comments(css: str) -> str:
    """Remove every ``/* ... */`` comment, refusing the two shapes that mean a
    comment was opened or closed in the wrong place: an unterminated comment
    and a stray ``*/`` with nothing open."""
    out = []
    i = 0
    while i < len(css):
        if css.startswith("/*", i):
            end = css.find("*/", i + 2)
            if end == -1:
                raise AssertionError(f"unterminated CSS comment at offset {i}")
            i = end + 2
            continue
        if css.startswith("*/", i):
            raise AssertionError(f"stray CSS comment close at offset {i}")
        out.append(css[i])
        i += 1
    return "".join(out)


def _top_level_selectors(css: str) -> list:
    """The prelude of every top-level rule in ``css``, comments removed.

    A comment body that survives as text shows up here as prose glued to the
    following selector, which is exactly how a browser loses that rule.
    """
    css = _strip_css_comments(css)
    selectors = []
    depth = 0
    start = 0
    i = 0
    while i < len(css):
        ch = css[i]
        if ch in "\"'":
            quote = ch
            i += 1
            while i < len(css) and css[i] != quote:
                i += 2 if css[i] == "\\" else 1
            i += 1
            continue
        if depth == 0 and ch == "{":
            selectors.append(css[start:i].strip())
            depth = 1
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                start = i + 1
        i += 1
    return [part.strip() for sel in selectors for part in sel.split(";")[-1].split(",")]


def test_the_dot_rule_leaves_the_picker_focus_rule_intact():
    """Merge resolution put ``.session-profile-dot`` where the ``/* #8044:`` opener
    of the comment above ``.project-picker-item:focus-visible`` used to start, and
    kept the comment's prose and its closing ``*/``. The result is not a comment:
    Chromium reads the prose together with the next selector and drops the focus
    rule, so the row loses its keyboard focus outline. Source-presence assertions
    cannot see that; the parsed stylesheet can."""
    css = _style_css()
    selectors = _top_level_selectors(css)
    assert ".session-profile-dot" in selectors
    assert ".project-picker-item:focus-visible" in selectors
    assert ".project-picker-item.active:focus-visible" in selectors
    # The comment's own words never reach a selector.
    assert "finger-sized row on a touch screen" not in _strip_css_comments(css)


def test_the_browser_stylesheet_keeps_the_picker_focus_rule():
    """The same failure observed the way a browser observes it: through CSSOM."""
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except Exception:  # pragma: no cover - dependency optional
        pytest.skip("playwright is unavailable; run `playwright install chromium`")

    with sync_playwright() as playwright:
        browser = None
        try:
            try:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            except PlaywrightError as exc:
                if "Executable doesn't exist at" in str(exc):
                    pytest.skip(
                        "playwright chromium executable is unavailable; "
                        "run `playwright install chromium`"
                    )
                raise
            page = browser.new_page()
            page.set_content(f"<style>{_style_css()}</style>")
            found = page.evaluate(
                """
                () => {
                  const sheet = document.styleSheets[0];
                  const seen = [];
                  const walk = rules => {
                    for (const rule of rules) {
                      if (rule.selectorText) seen.push(rule.selectorText);
                      if (rule.cssRules) walk(rule.cssRules);
                    }
                  };
                  walk(sheet.cssRules);
                  return seen;
                }"""
            )
        finally:
            if browser is not None:
                browser.close()

    joined = [part.strip() for sel in found for part in sel.split(",")]
    assert ".session-profile-dot" in joined
    assert ".project-picker-item:focus-visible" in joined
    assert ".project-picker-item.active:focus-visible" in joined
