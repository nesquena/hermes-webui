"""Regression tests for issue #2147 profile/workspace mental-model copy."""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_profiles_panel_surfaces_profiles_vs_workspaces_help_card():
    src = read("static/panels.js")
    i18n = read("static/i18n.js")
    assert "t('profiles_vs_workspaces_title')" in src
    assert "t('profiles_vs_workspaces_subtitle')" in src
    assert "Profiles vs workspaces" in i18n
    assert "Use profiles for how the agent works; use workspaces for what files it works on." in i18n
    assert "_renderProfileConceptHelp" in src
    assert "explainer.onclick = () => _renderProfileConceptHelp" in src


def test_profile_concept_help_distinguishes_how_from_where():
    src = read("static/panels.js")
    i18n = read("static/i18n.js")
    assert "t('profiles_vs_workspaces_profiles_body')" in src
    assert "t('profiles_vs_workspaces_workspaces_body')" in src
    assert "t('profiles_vs_workspaces_together_body')" in src
    assert "Agent identity, memory, skills, model/provider config, and connected tools" in i18n
    assert "Create profiles for roles like researcher, writer, marketer, or developer" in i18n
    assert "Project or product folders on disk" in i18n
    assert "Profiles answer “who is working?”; workspaces answer “where are they working?”" in i18n


def test_empty_profiles_state_keeps_help_card_visible():
    src = read("static/panels.js")
    assert "panel.innerHTML = ''" in src
    assert "panel.appendChild(explainer)" in src
    assert "emptyMsg.textContent = t('profiles_no_profiles')" in src
    assert "panel.appendChild(emptyMsg)" in src


def test_profile_concept_copy_is_localized_for_every_locale():
    src = read("static/i18n.js")
    expected = [
        "profiles_vs_workspaces_title",
        "profiles_vs_workspaces_subtitle",
        "profiles_vs_workspaces_heading",
        "profiles_vs_workspaces_profiles_label",
        "profiles_vs_workspaces_profiles_body",
        "profiles_vs_workspaces_workspaces_label",
        "profiles_vs_workspaces_workspaces_body",
        "profiles_vs_workspaces_together_label",
        "profiles_vs_workspaces_together_body",
    ]
    assert src.count("profiles_no_profiles:") == 10
    for key in expected:
        assert src.count(f"{key}:") == 10


def test_profile_help_card_has_distinct_visual_treatment():
    src = read("static/style.css")
    assert ".profile-help-card" in src
    assert "border-color:var(--border2)" in src
