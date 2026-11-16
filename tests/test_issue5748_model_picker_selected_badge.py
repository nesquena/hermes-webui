"""Regression coverage for #5748 model picker selected badge and scroll state."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def _body_between(src: str, start: str, end: str) -> str:
    start_idx = src.index(start)
    end_idx = src.index(end, start_idx)
    return src[start_idx:end_idx]


def test_model_dropdown_uses_viewport_height_cap():
    assert "max-height:min(70vh,640px);overflow-y:auto;" in STYLE_CSS


def test_composer_picker_search_row_pins_under_the_scope_note():
    # Opening the picker centres the selected row, so a deep selection scrolls
    # the list; the composer picker must pin its search row under the sticky
    # scope note or the search box scrolls out of view. Same idiom as the
    # Settings picker (.settings-model-dropdown .model-search-row).
    assert ".model-dropdown .model-search-row{position:sticky;top:var(--model-scope-note-h,34px);background:var(--surface);z-index:1;}" in STYLE_CSS


def test_toggle_model_dropdown_scrolls_active_row_after_open():
    body = _body_between(UI_JS, "async function toggleModelDropdown()", "function closeModelDropdown")

    assert "_positionModelDropdown();" in body
    # The open path centres the selected row (block:'center') so a deep
    # selection opens with context above and below, instead of being pinned
    # to the list's edge the way a bare nearest-scroll left it.
    assert "scrollIntoView({block:'center'})" in body
    assert body.index("_positionModelDropdown();") < body.index("scrollIntoView({block:'center'})")


def test_arrow_key_navigation_keeps_the_nearest_scroll_block():
    """Keyboard navigation must keep block:'nearest' while the open path
    centres the selection. Pinned separately so an edit to the open path
    cannot silently change the arrow-key scroll behaviour.
    """
    body = _body_between(UI_JS, "const _highlightRow=(rows,idx)=>{", "_si.addEventListener('keydown',e=>{")

    assert "row.scrollIntoView({block:'nearest'})" in body
    assert "block:'center'" not in body


def test_model_picker_renders_selected_badge_without_replacing_configured_badge():
    assert "const _selectedModelBadge=(m)=>" in UI_JS
    assert "model-opt-badge--selected" in UI_JS
    assert "t('model_badge_selected')||'Selected'" in UI_JS
    assert "_getConfiguredModelBadge(modelId,badgeMap,providerId)" in UI_JS


def test_selected_badge_is_keyed_to_current_model_value():
    # The selected badge must key on the resolved model identity, not a raw
    # string of the dropdown value: catalog rows carry the qualified
    # @custom:<slug>:<model> value while the outgoing state model is bare
    # (#6884), so both sides are normalized via _modelPickerOptionIdentity
    # before comparison.
    assert "const _norm=(model,provider)=>typeof _modelPickerOptionIdentity==='function'" in UI_JS
    assert "_norm(_rowModel,_rowProvider)===_norm(_stateModel,_stateProvider)" in UI_JS


def test_selected_badge_is_keyed_to_current_model_provider():
    assert "const _selectedModelState=(typeof _modelStateForSelect==='function')?_modelStateForSelect(sel,sel.value)" in UI_JS
    assert "const _modelProviderForSelectedBadge=(m)=>" in UI_JS
    assert "return (_provider&&_provider!=='default')?_provider:null;" in UI_JS
    # Provider identity still gates the row match — the normalized model
    # comparison alone would collapse same-id rows across provider groups.
    assert "_rowProvider=String(_modelProviderForSelectedBadge(m)||'')" in UI_JS
    assert "_stateProvider=String((_selectedModelState&&_selectedModelState.model_provider)||'')" in UI_JS
    assert "_rowProvider===_stateProvider" in UI_JS
    assert "const _isSelectedModelRow=(m)=>{" in UI_JS
    assert "row.className='model-opt'+(_isSelectedModelRow(m)?' active':'');" in UI_JS


def test_selected_group_key_prefers_provider_matched_row_before_value_fallback():
    assert "const _hit=_modelData.find(m=>m&&!m.endpointErrorOnly&&_isSelectedModelRow(m)) || _modelData.find(m=>m&&!m.endpointErrorOnly&&String(m.value||'')===_selVal);" in UI_JS
    assert "_groupOpenState[groupKey]=(groupKey===_selectedGroupKey)" in UI_JS


def test_selected_badge_helper_does_not_accept_dead_select_parameter():
    assert "_selectedModelBadge(m,sel)" not in UI_JS
    assert "_selectedModelBadge(m)" in UI_JS
    assert "_buildModelRow=(m,sel" not in UI_JS
    assert "_makeModelRow(m,sel" not in UI_JS


def test_selected_badge_label_has_locale_entries():
    assert I18N_JS.count("model_badge_selected:") >= 14
