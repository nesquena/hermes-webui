"""
Regression test for #1413 — every li('NAME', ...) call in static/*.js must
reference an icon name registered in LI_PATHS in static/icons.js.

History
-------
- v0.50.255 #1411 fixed the CSS specificity collision on .msg-tts-btn (#1409),
  making the TTS speaker button no longer display:none. But the button still
  rendered empty because li('volume-2', 13) hit the unknown-icon branch and
  returned ''. Reported by @AvidFuturist 2026-05-01.
- Audit at fix time also found 'chevron-up' (queue pill in ui.js), 'hash'
  / 'cpu' / 'dollar-sign' (insights panel stat cards in panels.js, shipped
  in v0.50.255 #1405) silently failing the same way. All five added in this
  fix.

Why this guard matters
----------------------
li() returns the empty string and only console.warns when an icon name is
missing. The button or container is then visually empty but the DOM, CSS,
and click handler all still work — so manual QA passes, automated DOM tests
pass, and the regression ships. Walking the call sites at test time is the
only cheap way to catch this entire class of bug.

What the test does
------------------
1. Parse static/icons.js to extract every key in the LI_PATHS object literal.
2. Walk every other static/*.js file and collect every li('NAME', ...) call.
3. Assert each NAME is registered.

If this test fires, fix it by adding the missing icon to LI_PATHS. Lucide
SVG paths are at https://lucide.dev/icons/<name>.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


STATIC_DIR = Path(__file__).parent.parent / "static"


def _load_registered_icons() -> set[str]:
    """Return the set of icon names registered in LI_PATHS in icons.js."""
    icons_js = (STATIC_DIR / "icons.js").read_text(encoding="utf-8")
    # Match `const LI_PATHS = { ... };` (multiline, dotall)
    obj_match = re.search(
        r"const\s+LI_PATHS\s*=\s*\{(.+?)^\};",
        icons_js,
        re.DOTALL | re.MULTILINE,
    )
    assert obj_match, "Could not locate LI_PATHS object literal in icons.js"
    body = obj_match.group(1)
    # Each key is a single-quoted identifier (allowed: letters, digits, hyphen,
    # underscore) followed by a colon. Comments are stripped first to avoid
    # picking up example names from inline `//` comments.
    body_no_comments = re.sub(r"//[^\n]*", "", body)
    return set(re.findall(r"'([\w\-]+)'\s*:", body_no_comments))


def _collect_li_call_sites() -> list[tuple[str, int, str]]:
    """Return [(file_name, line_number, icon_name), ...] for every li() call
    in static JS files (excluding icons.js which defines li itself)."""
    pattern = re.compile(r"\bli\(\s*['\"]([\w\-]+)['\"]")
    sites: list[tuple[str, int, str]] = []
    for path in sorted(STATIC_DIR.glob("*.js")):
        if path.name == "icons.js":
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for match in pattern.finditer(line):
                sites.append((path.name, lineno, match.group(1)))
    return sites


def test_all_li_calls_reference_registered_icons() -> None:
    """Every li('NAME', ...) in static/*.js must have NAME in LI_PATHS."""
    registered = _load_registered_icons()
    assert len(registered) > 20, (
        "Sanity check: parsed only %d LI_PATHS keys — parser may be broken."
        % len(registered)
    )

    call_sites = _collect_li_call_sites()
    assert len(call_sites) > 10, (
        "Sanity check: parser found only %d li() call sites across static/*.js"
        % len(call_sites)
    )

    missing: dict[str, list[str]] = {}
    for file_name, lineno, icon in call_sites:
        if icon not in registered:
            missing.setdefault(icon, []).append(f"{file_name}:{lineno}")

    if missing:
        report = "\n".join(
            f"  {icon!r} — referenced from: {', '.join(sites)}"
            for icon, sites in sorted(missing.items())
        )
        pytest.fail(
            "li() called with icon name(s) not registered in LI_PATHS in "
            "static/icons.js. The button/container will render empty in "
            "production. Add the Lucide SVG path to LI_PATHS for each:\n"
            + report
        )


def test_specific_icons_present_after_1413_fix() -> None:
    """Pin the five icons added by the #1413 fix so regressions get a clear
    error message (not just a generic "missing icon" failure)."""
    registered = _load_registered_icons()
    for icon in ("volume-2", "chevron-up", "hash", "cpu", "dollar-sign"):
        assert icon in registered, (
            f"Icon {icon!r} was added to LI_PATHS by the #1413 fix and must "
            f"not be removed. Re-adding required: see static/icons.js."
        )


def test_li_helper_warns_on_unknown_icon() -> None:
    """Assert the li() helper still uses the warn+empty-string fallback shape
    we relied on when diagnosing #1413. If this contract changes (e.g. li
    starts returning a placeholder glyph instead of ''), we lose the
    deterministic "invisible button" symptom and these tests need updating."""
    icons_js = (STATIC_DIR / "icons.js").read_text(encoding="utf-8")
    # The helper body has the shape:
    #   const p = LI_PATHS[name];
    #   if (!p) { console.warn('li(): unknown icon', name); return ''; }
    assert "console.warn('li(): unknown icon'" in icons_js, (
        "li() helper no longer uses console.warn fallback — update the "
        "#1413 audit story in this test file to match the new contract."
    )
    assert "return '';" in icons_js, (
        "li() helper no longer returns empty string on unknown icon — "
        "update the #1413 audit story in this test file."
    )
