"""Regression tests for issue #2147 profile/workspace mental-model copy."""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_profiles_panel_surfaces_profiles_vs_workspaces_help_card():
    src = read("static/panels.js")
    assert "t('profile_concept_title')" in src
    assert "t('profile_concept_subtitle')" in src
    assert "_renderProfileConceptHelp" in src
    assert "explainer.onclick = () => _renderProfileConceptHelp" in src


def test_profile_concept_help_distinguishes_how_from_where():
    i18n = read("static/i18n.js")
    assert "Agent identity, memory, skills, model/provider config, and connected tools" in i18n
    assert "Create profiles for roles like researcher, writer, marketer, or developer" in i18n
    assert "Project or product folders on disk" in i18n
    assert 'Profiles answer' in i18n
    assert 'who is working' in i18n
    assert 'where are they working' in i18n
    src = read("static/panels.js")
    assert "t('profile_concept_desc_profiles')" in src
    assert "t('profile_concept_desc_workspaces')" in src
    assert "t('profile_concept_desc_together')" in src


def test_empty_profiles_state_keeps_help_card_visible():
    src = read("static/panels.js")
    assert "panel.innerHTML = ''" in src
    assert "panel.appendChild(explainer)" in src
    assert "emptyMsg.textContent = t('profiles_no_profiles')" in src
    assert "panel.appendChild(emptyMsg)" in src
