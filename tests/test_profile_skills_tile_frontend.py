"""Static frontend checks for the profile Skills tile count contract."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    m = re.search(rf"function {re.escape(name)}\s*\([^)]*\)\s*\{{", src)
    assert m, f"function {name} not found"
    i, depth = m.end(), 1
    while i < len(src) and depth > 0:
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"function {name} did not parse cleanly"
    return src[m.start():i]


def test_profile_skills_tile_waits_for_profile_skills_payload_for_counts():
    """The tile must not treat /api/profiles skill_count as total skills.

    /api/profiles carries profile-row metadata from hermes_cli, which can vary
    per profile. The selected profile tile's "enabled / total" contract must be
    filled by /api/profile/skills because that endpoint has the full available
    skill list plus per-profile disabled state.
    """
    tile = _extract_function(PANELS_JS, "_profileSkillsTile")
    assert "skill_total" not in tile
    assert "Math.max(enabled" not in tile
    assert "Loading skills" in tile
    assert " / ${" not in tile


def test_profile_skills_tile_hydrator_applies_enabled_and_total_counts():
    summary = _extract_function(PANELS_JS, "_applyProfileSkillsSummary")
    assert "data.enabled_count" in summary
    assert "data.total_count" in summary
    assert "`${enabled_count} / ${total_count} enabled`" in summary


def test_profile_list_and_dropdown_use_enabled_out_of_total_counts():
    helper = _extract_function(PANELS_JS, "_profileSkillsCountMeta")
    list_panel = _extract_function(PANELS_JS, "loadProfilesPanel")
    dropdown = _extract_function(PANELS_JS, "renderProfileDropdown")

    assert "p.skill_count" not in list_panel
    assert "p.skill_count" not in dropdown
    assert "_profileSkillsCountMeta(p)" in list_panel
    assert "_profileSkillsCountMeta(p)" in dropdown
    assert "skill_enabled_count" in helper
    assert "skill_total" in helper
