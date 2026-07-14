import pathlib


REPO = pathlib.Path(__file__).parent.parent


def read(path):
    return (REPO / path).read_text(encoding="utf-8")


def test_index_contains_onboarding_overlay_markup():
    html = read("static/index.html")
    assert 'id="onboardingOverlay"' in html
    assert 'id="onboardingBody"' in html
    assert 'id="onboardingNextBtn"' in html
    assert 'src="static/onboarding.js?v=__WEBUI_VERSION__"' in html


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
    assert "api('/api/onboarding/setup'" in js
    assert "api('/api/onboarding/complete'" in js


def test_onboarding_uses_i18n_helpers():
    html = read("static/index.html")
    js = read("static/onboarding.js")
    i18n = read("static/i18n.js")
    assert 'data-i18n="onboarding_title"' in html
    assert 'data-i18n="onboarding_continue"' in html
    assert "t('onboarding_step_system_title')" in js
    assert "t('onboarding_step_setup_title')" in js
    assert "t('onboarding_complete')" in js
    assert "onboarding_title: 'Welcome to Hermes Web UI'" in i18n
    assert "onboarding_title: 'Bienvenido a Hermes Web UI'" in i18n


def test_onboarding_provider_notice_uses_i18n_key():
    js = read("static/onboarding.js")
    py = read("api/onboarding.py")
    assert '"provider_note_key": note_key' in py
    assert '"provider_note_args": note_args' in py
    assert "function _localizedOnboardingProviderNote(system)" in js
    assert "Array.isArray(system&&system.provider_note_args)" in js
    assert "t(key,...args)" in js
    assert "!/\\{\\d+\\}/.test(localized)" in js
    assert "system.provider_note|| (setupOk?" not in js


def test_bootstrap_script_contains_official_installer_and_windows_guard():
    src = read("bootstrap.py")
    assert (
        "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh"
        in src
    )
    # Native Windows is now experimental-supported (#1952), not hard-blocked:
    # ensure_supported_platform() warns instead of raising, but auto-install
    # (which shells out to /bin/bash) still guards native Windows explicitly.
    assert "Native Windows bootstrap is experimental" in src
    assert "Auto-install is not supported on native Windows" in src
