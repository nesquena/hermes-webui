"""Regression tests for issue #2147 profile/workspace mental-model copy."""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_profiles_panel_surfaces_profiles_vs_workspaces_help_card():
    src = read("static/panels.js")
    assert "Profiles vs workspaces" in src
    assert "Use profiles for how the agent works; use workspaces for what files it works on." in src
    assert "_renderProfileConceptHelp" in src
    assert "explainer.onclick = () => _renderProfileConceptHelp" in src


def test_profile_concept_help_distinguishes_how_from_where():
    src = read("static/panels.js")
    assert "Agent identity, memory, skills, model/provider config, and connected tools" in src
    assert "Create profiles for roles like researcher, writer, marketer, or developer" in src
    assert "Project or product folders on disk" in src
    assert "Profiles answer “who is working?”; workspaces answer “where are they working?”" in src


def test_empty_profiles_state_keeps_help_card_visible():
    src = read("static/panels.js")
    assert "panel.innerHTML = ''" in src
    assert "panel.appendChild(explainer)" in src
    assert "emptyMsg.textContent = t('profiles_no_profiles')" in src
    assert "panel.appendChild(emptyMsg)" in src
