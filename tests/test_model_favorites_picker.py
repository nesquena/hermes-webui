"""Regression coverage for model favorites picker feature.

Validates localStorage helpers, favorite key composition, rendering
structure, star button markup, deterministic sorting, i18n key
coverage, and provider identity fallback via _modelFavoriteProviderId.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text()
STYLE_CSS = (ROOT / "static" / "style.css").read_text()
I18N_JS = (ROOT / "static" / "i18n.js").read_text()


def test_model_favorites_key_constant_exists():
    assert "const MODEL_FAVORITES_KEY" in UI_JS
    assert "hermes-webui-model-favorites" in UI_JS


def test_localstorage_helpers_exist():
    assert "function _readModelFavorites" in UI_JS
    assert "function _writeModelFavorites" in UI_JS
    assert "function _favoriteModelKey" in UI_JS
    assert "function _isModelFavorite" in UI_JS
    assert "function _toggleModelFavorite" in UI_JS


def test_favorite_key_includes_provider_identity():
    assert "\\u0000" in UI_JS
    body = UI_JS[UI_JS.index("function _favoriteModelKey"):]
    body = body[:body.index("\n}")+2]
    assert "providerId" in body
    assert "_providerFromModelValue" in body


def test_model_favorite_provider_id_helper_exists():
    assert "function _modelFavoriteProviderId" in UI_JS
    body = UI_JS[UI_JS.index("function _modelFavoriteProviderId"):]
    body = body[:body.index("\n}")+2]
    assert "m&&m.badge&&m.badge.provider" in body
    assert "_providerFromModelValue" in body


def test_model_favorite_provider_id_used_in_render_button():
    btn_section = UI_JS[UI_JS.index("function _renderModelFavoriteButton"):]
    btn_body = btn_section[:btn_section.index("\n}")+2]
    assert "_modelFavoriteProviderId" in btn_body
    assert "favProvider" in btn_body
    assert "data-fav-prov" in btn_body
    assert "esc(favProvider)" in btn_body


def test_model_favorite_provider_id_used_in_favorite_filter():
    filter_section = UI_JS[UI_JS.index("const favoriteModels"):]
    filter_line = filter_section[:filter_section.index(";")+1]
    assert "_modelFavoriteProviderId" in filter_line


def test_model_favorite_provider_id_used_in_favorite_by_key():
    key_section = UI_JS[UI_JS.index("const favoriteByKey"):]
    key_area = key_section[:300]
    assert "_favoriteModelKey(m.value,_modelFavoriteProviderId(m))" in key_area


def test_model_favorite_provider_id_used_in_click_handler():
    click_section = UI_JS[UI_JS.index(".model-opt-favorite"):]
    click_area = click_section[:click_section.index("});")+2]
    assert "_modelFavoriteProviderId" in click_area
    assert "_modelFavoriteProviderId(x)" in click_area
    assert "toggleProviderId=_modelFavoriteProviderId" in click_area


def test_favorite_dedup_value_set_removed():
    assert "favoriteDedupValueSet" not in UI_JS


def test_synthetic_favorite_helper_exists():
    assert "function _syntheticFavoriteModelData" in UI_JS


def test_synthetic_favorite_helper_reads_persisted_favorites():
    body = UI_JS[UI_JS.index("function _syntheticFavoriteModelData"):]
    body = body[:body.index("\n}\n") + 2]
    assert "_readModelFavorites()" in body, "Helper must read persisted favorites"
    # Ignores malformed records without a usable value.
    assert "if(!value) continue;" in body
    # Provider-aware identity: stored providerId then _providerFromModelValue fallback.
    assert "rec&&rec.providerId" in body
    assert "_providerFromModelValue(value)" in body
    # Dedupe by provider-aware favorite key against existing data.
    assert "_favoriteModelKey" in body
    assert "existingKeys.has(key)" in body
    # Carries provider identity for star/click/select paths.
    assert "providerId" in body
    # Flagged so it does not leak into provider/configured groups.
    assert "syntheticFavorite:true" in body


def test_synthetic_favorites_injected_before_render():
    inject = UI_JS[UI_JS.index("function renderModelDropdown"):UI_JS.index("// Create search input FIRST")]
    assert "_syntheticFavoriteModelData(_modelData)" in inject
    assert "_modelData.push(fav)" in inject


def test_synthetic_favorites_excluded_from_provider_and_configured_groups():
    filter_section = UI_JS[UI_JS.index("const _filterModels=(term)=>"):UI_JS.index("// Restore focus to search input")]
    # Not treated as a configured catalog entry.
    assert "m.badge&&!m.syntheticFavorite&&matches(m)" in filter_section
    # Not counted toward or rendered inside regular provider groups.
    assert "configuredIds.has(m.value)||m.syntheticFavorite||!matches(m)" in filter_section


def test_render_model_dropdown_includes_favorites_group():
    assert "model_group_favorites" in UI_JS
    fav_idx = UI_JS.index("model_group_favorites")
    conf_idx = UI_JS.index("model_group_configured")
    assert fav_idx < conf_idx, "Favorites group must render before Configured group"


def test_favorite_button_is_real_button_with_aria():
    assert 'class="model-opt-favorite"' in UI_JS
    assert 'aria-pressed=' in UI_JS
    assert 'type="button"' in UI_JS
    assert "data-fav-value" in UI_JS
    assert "data-fav-prov" in UI_JS


def test_favorite_button_stops_propagation():
    btn_handler_section = UI_JS[UI_JS.index(".model-opt-favorite"):]
    assert "e.preventDefault()" in btn_handler_section[:500]
    assert "e.stopPropagation()" in btn_handler_section[:500]


def test_favorite_toggle_does_not_call_select_model():
    toggle_section = UI_JS[UI_JS.index("_toggleModelFavorite"):UI_JS.index("_toggleModelFavorite")+300]
    assert "selectModelFromDropdown" not in toggle_section


def test_favorite_sorting_is_deterministic():
    sort_section = UI_JS[UI_JS.index("favoriteRows"):UI_JS.index("favoriteRows")+300]
    assert "localeCompare" in sort_section


def test_search_term_preserved_across_renders():
    assert "_modelDropdownSearchTerm" in UI_JS
    assert "_existingSearch" in UI_JS
    assert "_si.value=_modelDropdownSearchTerm" in UI_JS or "_modelDropdownSearchTerm=_si.value" in UI_JS


def test_model_opt_favorite_css_exists():
    assert ".model-opt-favorite" in STYLE_CSS
    assert 'aria-pressed="true"' in STYLE_CSS or '[aria-pressed="true"]' in STYLE_CSS
    assert '.model-opt-favorite[aria-pressed="true"] svg{fill:currentColor;}' in STYLE_CSS


def test_i18n_keys_in_english():
    assert "model_group_favorites" in I18N_JS
    assert "model_favorite_add" in I18N_JS
    assert "model_favorite_remove" in I18N_JS


def test_i18n_keys_in_all_locales():
    locale_matches = re.findall(
        r"^  (?:(['\"])([\w-]+)\1|([\w-]+)): \{",
        I18N_JS,
        re.MULTILINE,
    )
    locale_codes = [quoted or bare for _, quoted, bare in locale_matches]
    assert len(locale_codes) > 0, "No locale blocks found in i18n.js"
    for code in locale_codes:
        locale_header = re.search(
            rf"^  (?:['\"]{re.escape(code)}['\"]|{re.escape(code)}): \{{",
            I18N_JS,
            re.MULTILINE,
        )
        assert locale_header, f"Missing locale header for {code}"
        locale_block = I18N_JS[locale_header.start():]
        locale_block = locale_block[:locale_block.index("\n  }")]
        assert "model_group_favorites" in locale_block, f"Missing model_group_favorites in {code} locale"
        assert "model_favorite_add" in locale_block, f"Missing model_favorite_add in {code} locale"
        assert "model_favorite_remove" in locale_block, f"Missing model_favorite_remove in {code} locale"


def test_star_icon_used_in_favorite_button():
    assert "li('star'" in UI_JS
    star_btn_section = UI_JS[UI_JS.index("_renderModelFavoriteButton"):]
    assert "li('star'" in star_btn_section[:600]


def _configured_row_block():
    start = UI_JS.index("model_group_configured")
    return UI_JS[start:start + 2000]


def test_configured_row_uses_friendly_model_name():
    block = _configured_row_block()
    assert "modelName = m.name || getModelLabel(rawId) || rawId" in block, \
        "Configured row should derive modelName from m.name or getModelLabel(rawId), not rawId alone"


def test_configured_row_raw_id_still_in_opt_id():
    block = _configured_row_block()
    assert "model-opt-id" in block
    assert "m.id" in block, "Configured row subtext must still emit m.id (raw ID)"


def test_configured_badge_label_uses_badge_label_not_raw_id():
    block = _configured_row_block()
    assert "badgeLabel = m.badge.label || 'Configured'" in block, \
        "Configured badge label should use m.badge.label, not rawId"


def test_configured_row_preserves_custom_ids():
    assert "@custom:" in UI_JS
    get_model_label_block = UI_JS[UI_JS.index("function getModelLabel"):]
    get_model_label_body = get_model_label_block[:get_model_label_block.index("\n}") + 2]
    assert "@custom:" in get_model_label_body
    assert "rawId.startsWith('@custom:')" in get_model_label_body