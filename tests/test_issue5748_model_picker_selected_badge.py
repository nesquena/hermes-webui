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


def test_toggle_model_dropdown_scrolls_active_row_after_open():
    body = _body_between(UI_JS, "async function toggleModelDropdown()", "function closeModelDropdown")

    assert "_positionModelDropdown();" in body
    assert "scrollIntoView({block:'nearest'})" in body
    assert body.index("_positionModelDropdown();") < body.index("scrollIntoView({block:'nearest'})")


def test_model_picker_renders_selected_badge_without_replacing_configured_badge():
    assert "const _selectedModelBadge=(m)=>" in UI_JS
    assert "model-opt-badge--selected" in UI_JS
    assert "t('model_badge_selected')||'Selected'" in UI_JS
    assert "_getConfiguredModelBadge(modelId,badgeMap,providerId)" in UI_JS


def test_selected_badge_is_keyed_to_current_model_value():
    # #7400 re-gate: the selected row must be matched by CANONICAL identity on
    # BOTH sides — the bare model derived with _qualifiedCatalogOptionMeta for a
    # provider-qualified row (@provider:model). Comparing a canonicalized row
    # against a RAW selected value drops the active row and the Selected badge
    # whenever the selected option comes from a producer that does not stamp
    # dataset.model (Settings population, live-model insertion).
    assert "const _canonicalRowModelForCompare=(m)=>{" in UI_JS
    assert "const _canonicalSelectedModelForCompare=()=>{" in UI_JS
    assert "const _selectedModelForCompare=_canonicalSelectedModelForCompare();" in UI_JS
    assert "const _rowModel=String(_canonicalRowModelForCompare(m));" in UI_JS
    assert "_rowModel===_selectedModelForCompare" in UI_JS
    # The raw-value comparison is what silently lost the badge: it must not return.
    assert "String(_canonicalRowModelForCompare(m))===String((_selectedModelState&&_selectedModelState.model)||(sel&&sel.value)||'')" not in UI_JS
    # #6895: catalog rows can carry a qualified @custom:<slug>:<model> value
    # whose host:port slug _qualifiedCatalogOptionMeta leaves raw, while the
    # outgoing state model is bare, so both sides also go through the picker
    # dedup identity (_modelPickerOptionIdentity) before comparison.
    assert "const _normIdentity=(model,provider)=>typeof _modelPickerOptionIdentity==='function'" in UI_JS
    assert "_normIdentity(_rowModel,_rowProvider)===_normIdentity(_selectedModelForCompare,_stateProvider)" in UI_JS


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
