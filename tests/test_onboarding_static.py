import pathlib


REPO = pathlib.Path(__file__).parent.parent


def read(path):
    return (REPO / path).read_text(encoding="utf-8")


def test_index_contains_onboarding_overlay_markup():
    html = read("static/index.html")
    assert 'id="onboardingOverlay"' in html
    assert 'id="onboardingBody"' in html
    assert 'id="onboardingNextBtn"' in html
    assert 'src="/static/onboarding.js"' in html


def test_onboarding_css_rules_exist():
    css = read("static/style.css")
    for selector in (
        ".onboarding-overlay",
        ".onboarding-card",
        ".onboarding-step",
        ".onboarding-status.warn",
    ):
        assert selector in css


def test_onboarding_js_exposes_bootstrap_hooks():
    js = read("static/onboarding.js")
    assert "async function loadOnboardingWizard()" in js
    assert "async function nextOnboardingStep()" in js
    assert "api('/api/onboarding/status')" in js
    assert "api('/api/onboarding/complete'" in js


def test_bootstrap_script_contains_official_installer_and_windows_guard():
    src = read("bootstrap.py")
    assert (
        "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh"
        in src
    )
    assert "Native Windows is not supported" in src
