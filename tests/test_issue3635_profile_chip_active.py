"""Tests for #3635 — profile chip must reflect the ACTIVE profile, not the
loaded session's profile.

Regression from #3331 (shipped v0.51.204): #3331 changed the composer profile
chip label in syncTopbar()'s session-present branch to read
``(S.session&&S.session.profile)||S.activeProfile`` so the label would track the
profile of whatever session was being browsed. But the chip is the profile
*switcher* trigger (it fronts the profile dropdown), and message routing /
new-chat creation both follow the client active profile (the ``hermes_profile``
cookie, set only by ``/api/profile/switch``). ``loadSession()`` sets
``S.session`` but never updates ``S.activeProfile``, so opening a session that
belongs to a different profile than the active one made the chip diverge from
the dropdown checkmark and misrepresent where the next message would route.

The fix reverts JUST the chip label to ``S.activeProfile`` in both syncTopbar
branches. #3331's legitimate project/session-operation scoping (which keys on
the session's own profile) is unrelated to this line and stays in place.
"""

from pathlib import Path

import re


def _ui_js() -> str:
    return (Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")


def _sync_topbar_body(src: str) -> str:
    """Return the full source of the syncTopbar() function."""
    start = src.find("function syncTopbar(){")
    assert start != -1, "syncTopbar function not found in ui.js"
    # Walk braces to find the matching close.
    i = src.find("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError("could not find end of syncTopbar()")


class TestIssue3635ProfileChipActive:
    def test_session_present_chip_reads_active_profile(self):
        """The session-present chip-label update must read S.activeProfile."""
        body = _sync_topbar_body(_ui_js())
        # There are two profileChipLabel updates: the !S.session early-return
        # block and the session-present block. Both must key on S.activeProfile.
        updates = re.findall(
            r"profileChipLabel'\);\s*\n\s*if\([^)]*\)\s*[^.]*\.textContent=([^;]+);",
            body,
        )
        assert updates, "no profileChipLabel textContent assignment found in syncTopbar"
        for expr in updates:
            assert "S.activeProfile" in expr, (
                "profile chip label must read S.activeProfile, got: " + expr.strip()
            )

    def test_chip_does_not_key_on_session_profile(self):
        """Forbid the #3331 regression shape: chip keying on S.session.profile.

        This negative assertion stops a future change from re-pointing the
        switcher-trigger chip at the loaded session's profile (#3635).
        """
        body = _sync_topbar_body(_ui_js())
        assert "(S.session&&S.session.profile)||S.activeProfile" not in body, (
            "profile chip label must NOT key on S.session.profile — that is the "
            "#3331 regression that made the chip diverge from the active profile "
            "and misrepresent message routing (#3635)."
        )
        # Also guard the spaced variant.
        assert "(S.session && S.session.profile) || S.activeProfile" not in body, (
            "profile chip label must NOT key on S.session.profile (#3635)."
        )

    def test_both_chip_setters_consistent(self):
        """Both the no-session and session-present chip setters must agree.

        Before the fix the early-return (no session) branch read S.activeProfile
        while the session-present branch read S.session.profile — the
        inconsistency was the bug. They must now be identical.
        """
        body = _sync_topbar_body(_ui_js())
        setters = re.findall(r"\.textContent=(S\.activeProfile\|\|'default')", body)
        assert len(setters) >= 2, (
            "expected both syncTopbar chip-label setters to read "
            "S.activeProfile||'default'; found: " + str(setters)
        )


def _panels_js() -> str:
    return (Path(__file__).parent.parent / "static" / "panels.js").read_text(encoding="utf-8")


def _render_profile_dropdown_body(src: str) -> str:
    """Return the source of renderProfileDropdown()."""
    start = src.find("function renderProfileDropdown(")
    assert start != -1, "renderProfileDropdown not found in panels.js"
    i = src.find("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError("could not find end of renderProfileDropdown()")


class TestProfileSwitcherSourceOfTruthInvariant:
    """Standing invariant guard (generalizes #3635).

    The profile chip (the switcher trigger, in syncTopbar()/ui.js) and the
    profile dropdown's active/checkmark row (renderProfileDropdown()/panels.js)
    are two renderings of the SAME thing — "which profile is active." They must
    resolve from the same source of truth (S.activeProfile). #3331 broke this by
    pointing the chip at S.session.profile while the dropdown still used
    S.activeProfile, so the switcher trigger and the menu it opens disagreed
    (#3635). This invariant fails fast if any future change re-splits them.
    """

    def test_chip_keys_on_active_profile(self):
        body = _sync_topbar_body(_ui_js())
        # Every profileChipLabel.textContent assignment must read S.activeProfile.
        assignments = re.findall(r"profileChipLabel'\);[\s\S]{0,120}?\.textContent=([^;]+);", body)
        assert assignments, "no profileChipLabel assignment found in syncTopbar()"
        for expr in assignments:
            assert "S.activeProfile" in expr and "S.session.profile" not in expr, (
                "profile chip (switcher trigger) must resolve from S.activeProfile, "
                "not the loaded session's profile: " + expr.strip()
            )

    def test_dropdown_active_row_keys_on_active_profile(self):
        body = _render_profile_dropdown_body(_panels_js())
        # The dropdown's active row is computed into `const active = ...`.
        m = re.search(r"const active\s*=\s*([\s\S]*?);", body)
        assert m, "could not find the `const active =` computation in renderProfileDropdown()"
        expr = m.group(1)
        assert "S.activeProfile" in expr, (
            "dropdown active-row must resolve from S.activeProfile so it agrees "
            "with the chip (switcher source-of-truth invariant): " + expr.strip()
        )

    def test_chip_and_dropdown_share_source_of_truth(self):
        """The chip and the dropdown active-row must BOTH key on S.activeProfile.

        This is the cross-file invariant that #3635 violated: a passing version
        of this test means the switcher trigger and the menu it opens can never
        again silently disagree about which profile is active.
        """
        chip_body = _sync_topbar_body(_ui_js())
        dd_body = _render_profile_dropdown_body(_panels_js())
        chip_ok = "S.activeProfile" in chip_body and \
            "(S.session&&S.session.profile)||S.activeProfile" not in chip_body
        dd_ok = "S.activeProfile" in dd_body
        assert chip_ok and dd_ok, (
            "profile chip (ui.js syncTopbar) and dropdown active-row "
            "(panels.js renderProfileDropdown) must share S.activeProfile as the "
            "single source of truth for 'active profile' (#3635). "
            f"chip_ok={chip_ok} dd_ok={dd_ok}"
        )
