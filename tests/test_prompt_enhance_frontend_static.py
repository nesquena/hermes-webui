from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_composer_has_prompt_enhance_actions_and_preview_after_textarea():
    textarea_idx = INDEX_HTML.index('id="msg"')
    actions_idx = INDEX_HTML.index('id="composerDraftActions"')
    preview_idx = INDEX_HTML.index('id="composerEnhancePreview"')
    footer_idx = INDEX_HTML.index('class="composer-footer"', preview_idx)
    assert textarea_idx < actions_idx < preview_idx < footer_idx
    assert 'id="btnPromptEnhance"' in INDEX_HTML
    assert 'id="composerEnhanceLabel"' in INDEX_HTML
    assert 'id="btnPromptEnhanceUndo"' in INDEX_HTML
    assert 'id="composerEnhanceSuggested"' in INDEX_HTML


def test_prompt_enhance_ui_uses_idle_preview_and_apply_flow():
    assert 'const _PROMPT_ENHANCE_IDLE_MS=5000;' in UI_JS
    assert 'function _schedulePromptEnhancePreview()' in UI_JS
    assert "void _requestPromptEnhancePreview({text:latest, original:live.value, auto:true});" in UI_JS
    assert "label.textContent='Apply enhance';" in UI_JS
    assert "ta.value=_promptEnhancePreview;" in UI_JS
    assert 'composerEnhancePreview' in UI_JS


def test_prompt_enhance_preview_calls_endpoint_with_session_aware_kimi_request():
    start = UI_JS.index('async function _requestPromptEnhancePreview(')
    end = UI_JS.index('function syncPromptEnhanceButton()', start)
    block = UI_JS[start:end]
    assert '/api/prompt/enhance' in block
    assert "session_id:S.session&&S.session.session_id" in block
    assert "workspace:S.session&&S.session.workspace" in block
    assert "profile:S.activeProfile||S.session&&S.session.profile||'default'" in block
    assert 'timeoutMs:120000' in block
    assert 'modelState.model' not in block
    assert 'prefer_current_model' not in block


def test_prompt_enhance_button_and_preview_sync_with_input_and_send_clear():
    assert "if(window._suppressNextPromptEnhanceSchedule){" in BOOT_JS
    assert "window._suppressNextPromptEnhanceSchedule=false;" in BOOT_JS
    assert "}else if(typeof _schedulePromptEnhancePreview==='function') _schedulePromptEnhancePreview();" in BOOT_JS
    assert "if(typeof _clearPromptEnhancePreview==='function') _clearPromptEnhancePreview();" in MESSAGES_JS
    assert 'function _clearPromptEnhancePreview(' in UI_JS
    assert '_promptEnhanceRequestSeq++;' in UI_JS
    assert 'let _promptEnhanceAbortController=null;' in UI_JS
    assert "window._suppressNextPromptEnhanceSchedule=false;" in UI_JS
    assert '_promptEnhanceAbortController.abort();' in UI_JS
    assert "signal:controller?controller.signal:undefined" in UI_JS
    assert "window._suppressNextPromptEnhanceSchedule=true;" in UI_JS


def test_prompt_enhance_preview_styles_support_side_by_side_layout():
    assert '.composer-enhance-preview' in STYLE_CSS
    assert '.composer-enhance-preview-grid{display:grid;grid-template-columns:minmax(0,1fr);gap:10px;}' in STYLE_CSS
    assert '.composer-enhance-pane' in STYLE_CSS
    assert '.composer-enhance-btn.is-ready' in STYLE_CSS


def test_prompt_enhance_preview_no_longer_renders_original_draft_pane():
    assert 'composerEnhanceOriginal' not in INDEX_HTML
    assert "const original=$('composerEnhanceOriginal');" not in UI_JS
