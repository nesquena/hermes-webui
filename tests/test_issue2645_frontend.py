from pathlib import Path

PANELS_JS = Path("static/panels.js").read_text(encoding="utf-8")
INDEX_HTML = Path("static/index.html").read_text(encoding="utf-8")
STYLE_CSS = Path("static/style.css").read_text(encoding="utf-8")
BOOT_JS = Path("static/boot.js").read_text(encoding="utf-8")
CHANGELOG = Path("CHANGELOG.md").read_text(encoding="utf-8")


def test_sidebar_has_quick_profile_switcher_synced_to_profile_api():
    assert 'id="sidebarProfileSelect"' in INDEX_HTML
    assert 'onchange="switchToProfile(this.value)"' in INDEX_HTML
    assert "function refreshQuickProfileSelect(data)" in PANELS_JS
    assert "api('/api/profiles').then(refreshQuickProfileSelect)" in PANELS_JS
    assert "if(quickSel) quickSel.value = S.activeProfile;" in PANELS_JS
    assert "refreshQuickProfileSelectFromApi" in BOOT_JS
    assert ".sidebar-profile-quick" in STYLE_CSS


def test_changelog_mentions_quick_profile_switcher():
    unreleased = CHANGELOG.split("## [v0.51.103]", 1)[0]
    assert "sidebar quick profile switcher" in unreleased
