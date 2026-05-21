"""Tests for auxiliary models settings UI — panels.js + index.html + i18n.js.

Verifies that the auxiliary models card is present in the settings HTML,
that the JS loading/saving logic is wired up, and that all locales have the
required i18n keys.
"""
from pathlib import Path

ROOT = Path(__file__).parent.parent
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


class TestAuxiliaryModelsHTML:
    """The auxiliary models card must be present in the settings preferences pane."""

    def test_aux_models_container_exists(self):
        """The #auxModelsContainer div must exist in the preferences pane."""
        assert 'id="auxModelsContainer"' in INDEX_HTML, (
            "Missing #auxModelsContainer in index.html — auxiliary models card not rendered"
        )

    def test_reset_button_exists(self):
        assert 'id="btnResetAuxModels"' in INDEX_HTML, (
            "Missing #btnResetAuxModels button in index.html"
        )

    def test_apply_button_exists(self):
        assert 'id="btnApplyAuxModels"' in INDEX_HTML, (
            "Missing #btnApplyAuxModels button in index.html"
        )

    def test_aux_card_after_default_model(self):
        """Auxiliary Models card should come after the Default Model card in the DOM."""
        model_idx = INDEX_HTML.find('id="settingsModel"')
        aux_idx = INDEX_HTML.find('id="auxModelsContainer"')
        assert model_idx >= 0, "Default Model select not found in index.html"
        assert aux_idx >= 0, "Auxiliary Models container not found in index.html"
        assert aux_idx > model_idx, (
            "Auxiliary Models container must appear after Default Model in the DOM"
        )

    def test_i18n_label_on_aux_card(self):
        """The auxiliary models card label must use data-i18n attribute."""
        assert 'data-i18n="settings_label_auxiliary_models"' in INDEX_HTML, (
            "Missing data-i18n='settings_label_auxiliary_models' on auxiliary card label"
        )


class TestAuxiliaryModelsJS:
    """The JS logic for loading and saving auxiliary models must be in panels.js."""

    def test_load_function_exists(self):
        assert "async function _loadAuxiliaryModels" in PANELS_JS, (
            "Missing _loadAuxiliaryModels() in panels.js"
        )

    def test_apply_function_exists(self):
        assert "async function _applyAuxModels" in PANELS_JS, (
            "Missing _applyAuxModels() in panels.js"
        )

    def test_aux_task_slots_defined(self):
        """_AUX_TASK_SLOTS must list the 9 canonical task slots."""
        assert "_AUX_TASK_SLOTS" in PANELS_JS, (
            "Missing _AUX_TASK_SLOTS constant in panels.js"
        )
        # Verify all 9 tasks are present
        for key in ("vision", "compression", "web_extract", "session_search",
                     "approval", "mcp", "title_generation", "skills_hub", "curator"):
            assert f"key:'{key}'" in PANELS_JS, (
                f"Missing auxiliary task slot '{key}' in _AUX_TASK_SLOTS"
            )

    def test_calls_model_auxiliary_api(self):
        """_loadAuxiliaryModels must call /api/model/auxiliary."""
        assert "/api/model/auxiliary" in PANELS_JS, (
            "panels.js must call /api/model/auxiliary to fetch current config"
        )

    def test_calls_model_set_api(self):
        """_applyAuxModels must call /api/model/set to save changes."""
        assert "/api/model/set" in PANELS_JS, (
            "panels.js must call /api/model/set to save auxiliary model changes"
        )

    def test_provider_cascade(self):
        """Changing provider must rebuild model dropdown."""
        assert "_onAuxProviderChange" in PANELS_JS, (
            "Missing _onAuxProviderChange() for provider→model cascade"
        )
        assert "_buildAuxModelOptions" in PANELS_JS, (
            "Missing _buildAuxModelOptions() for model dropdown rebuild"
        )

    def test_custom_model_prompt(self):
        """Selecting 'Custom model…' must prompt for model ID."""
        assert "__custom__" in PANELS_JS, (
            "Missing __custom__ sentinel option for custom model input"
        )

    def test_reset_calls_api_with_reset_task(self):
        """Reset button must call /api/model/set with task='__reset__'."""
        idx = PANELS_JS.find("btnResetAuxModels")
        assert idx >= 0, "btnResetAuxModels not found in panels.js"
        # Check that __reset__ is sent in the reset handler
        body_after = PANELS_JS[idx:idx + 2000]
        assert "__reset__" in body_after, (
            "Reset handler must send task='__reset__' to /api/model/set"
        )

    def test_load_called_from_loadSettingsPanel(self):
        """_loadAuxiliaryModels must be called from loadSettingsPanel."""
        assert "_loadAuxiliaryModels()" in PANELS_JS, (
            "_loadAuxiliaryModels() is not called from loadSettingsPanel"
        )

    def test_dirty_flag_marking(self):
        """Changing an auxiliary dropdown must mark settings dirty."""
        assert "_markAuxDirty" in PANELS_JS, (
            "Missing _markAuxDirty() for dirty detection"
        )
        # _markAuxDirty should call _markSettingsDirty
        idx = PANELS_JS.find("function _markAuxDirty")
        body = PANELS_JS[idx:idx + 200]
        assert "_markSettingsDirty" in body, (
            "_markAuxDirty must call _markSettingsDirty"
        )


class TestAuxiliaryModelsI18n:
    """All locales must have the auxiliary model i18n keys."""

    REQUIRED_KEYS = [
        "settings_label_auxiliary_models",
        "settings_desc_auxiliary_models",
        "settings_btn_reset_aux_models",
        "settings_btn_apply_aux_models",
        "settings_aux_provider_auto",
        "settings_aux_model_auto",
        "settings_aux_model_custom",
        "settings_aux_model_custom_prompt",
        "settings_aux_loading",
        "settings_aux_load_failed",
        "settings_aux_reset_confirm_title",
        "settings_aux_reset_confirm_msg",
        "settings_aux_reset_done",
        "settings_aux_save_failed",
        "settings_aux_saved",
        "settings_aux_no_changes",
    ]

    def test_all_i18n_keys_present(self):
        """Every required key must exist in i18n.js at least once."""
        for key in self.REQUIRED_KEYS:
            assert key in I18N_JS, (
                f"Missing i18n key '{key}' in i18n.js"
            )

    def test_all_locales_have_auxiliary_keys(self):
        """Count of each key should equal the number of locales (11)."""
        for key in self.REQUIRED_KEYS:
            count = I18N_JS.count(f"{key}:")
            assert count == 11, (
                f"i18n key '{key}' found {count} times — expected 11 (one per locale)"
            )


class TestAuxiliaryModelsBackend:
    """WebUI backend must expose /api/model/auxiliary and /api/model/set."""

    ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")

    def test_model_auxiliary_route_exists(self):
        """/api/model/auxiliary route must be registered in routes.py."""
        assert '"/api/model/auxiliary"' in self.ROUTES_PY, (
            "Missing /api/model/auxiliary route in routes.py"
        )

    def test_model_set_route_exists(self):
        """/api/model/set route must be registered in routes.py."""
        assert '"/api/model/set"' in self.ROUTES_PY, (
            "Missing /api/model/set route in routes.py"
        )

    def test_get_auxiliary_models_function_exists(self):
        """get_auxiliary_models() must exist in api/config.py."""
        assert "def get_auxiliary_models" in self.CONFIG_PY, (
            "Missing get_auxiliary_models() in api/config.py"
        )

    def test_set_auxiliary_model_function_exists(self):
        """set_auxiliary_model() must exist in api/config.py."""
        assert "def set_auxiliary_model" in self.CONFIG_PY, (
            "Missing set_auxiliary_model() in api/config.py"
        )

    def test_aux_task_slots_constant_exists(self):
        """AUX_TASK_SLOTS must be defined in api/config.py."""
        assert "AUX_TASK_SLOTS" in self.CONFIG_PY, (
            "Missing AUX_TASK_SLOTS constant in api/config.py"
        )

    def test_js_uses_models_endpoint_not_options(self):
        """Frontend must use /api/models (WebUI's own API) not /api/model/options (agent API)."""
        # _loadAuxiliaryModels should call /api/models, not /api/model/options
        idx = PANELS_JS.find("async function _loadAuxiliaryModels")
        assert idx >= 0, "_loadAuxiliaryModels not found"
        body = PANELS_JS[idx:idx + 800]
        assert "/api/models" in body, (
            "_loadAuxiliaryModels must call /api/models for provider/model lists"
        )
        assert "/api/model/options" not in body, (
            "_loadAuxiliaryModels must NOT call /api/model/options (agent-only endpoint)"
        )
