from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _boot_iife_body() -> str:
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    start = src.index("(async()=>{")
    end = src.index("})().catch", start)
    return src[start:end]


def test_chat_boot_defers_completed_setup_hydration_off_critical_path():
    body = _boot_iife_body()

    assert "function _deferBootHydration" in (ROOT / "static" / "boot.js").read_text(
        encoding="utf-8"
    )
    assert "await loadWorkspaceList();" not in body
    assert "window._workspaceListReady=_workspaceListReady" in body
    assert "_deferBootHydration(()=>loadWorkspaceList())" in body


def test_completed_chat_boot_does_not_wait_for_onboarding_status():
    body = _boot_iife_body()

    assert "await loadOnboardingWizard();" not in body
    assert "_bootSettings&&_bootSettings.onboarding_completed===false" in body
    assert "await _onboardingWizardReady;" in body
    assert "_deferBootHydration(()=>loadOnboardingWizard())" in body
    assert "window._onboardingWizardReady=_onboardingWizardReady" in body


def test_boot_hydration_runs_after_first_paint():
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    helper_start = src.index("function _deferBootHydration")
    helper_end = src.index("(async()=>{", helper_start)
    helper = src[helper_start:helper_end]

    assert "requestAnimationFrame" in helper
    assert "setTimeout(run,0)" in helper


def test_boot_reconciles_pre_listener_composer_text_before_async_boot():
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    input_listener = src.index("$('msg').addEventListener('input'")
    async_boot = src.index("(async()=>{")
    setup_block = src[input_listener:async_boot]

    assert "if(typeof updateSendBtn==='function') updateSendBtn();" in setup_block
