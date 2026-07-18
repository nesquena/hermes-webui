"""Tests for the Mixture-of-Agents (MoA) settings UI — /api/model/moa
(api/config.py get_moa_config/set_moa_config, api/routes.py GET+PUT) plus the
panels.js + index.html + i18n.js wiring.

The "moa" config.yaml key and its {reference_models, aggregator, enabled,
reference_temperature, aggregator_temperature, max_tokens,
reference_max_tokens, fanout} shape are owned by hermes_cli/moa_config.py in
hermes-agent (the runtime that actually executes MoA turns — see
api/commands.py resolve_moa_config). hermes_cli is an optional dependency
here, so these routes read/write the same config.yaml structure without
importing it.
"""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent
PANELS_JS_PATH = ROOT / "static" / "panels.js"
PANELS_JS = PANELS_JS_PATH.read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
NODE = shutil.which("node")


class TestMoaSettingsHTML:
    """The Mixture of Agents card must be present in the settings preferences pane."""

    def test_moa_card_markers_exist(self):
        for marker in (
            'id="moaEnabled"',
            'id="moaFields"',
            'id="moaAgentsContainer"',
            'id="btnMoaAddAgent"',
            'id="moaAggregatorContainer"',
            'id="btnSaveMoa"',
            'id="moaOtherPresetsNote"',
        ):
            assert marker in INDEX_HTML, f"Missing {marker} in index.html"

    def test_moa_card_after_auxiliary_models(self):
        """MoA card should come after the Auxiliary Models card in the DOM."""
        aux_idx = INDEX_HTML.find('id="auxModelsContainer"')
        moa_idx = INDEX_HTML.find('id="moaAgentsContainer"')
        assert aux_idx >= 0, "Auxiliary Models container not found"
        assert moa_idx >= 0, "MoA agents container not found"
        assert moa_idx > aux_idx, "MoA card must appear after Auxiliary Models in the DOM"

    def test_i18n_labels_on_moa_card(self):
        for key in ("settings_label_moa", "settings_desc_moa", "settings_moa_enable"):
            assert f'data-i18n="{key}"' in INDEX_HTML, f"Missing data-i18n='{key}' on MoA card"


class TestMoaSettingsJS:
    """panels.js must load/save the MoA preset via /api/model/moa."""

    def test_load_and_save_functions_exist(self):
        assert "async function _loadMoaConfig" in PANELS_JS
        assert "async function _saveMoaConfig" in PANELS_JS

    def test_calls_moa_api(self):
        idx = PANELS_JS.find("async function _loadMoaConfig")
        assert idx >= 0
        load_body = PANELS_JS[idx:idx + 1500]
        assert "/api/model/moa" in load_body
        assert "/api/models" in load_body

    def test_save_uses_put_method(self):
        idx = PANELS_JS.find("async function _saveMoaConfig")
        assert idx >= 0
        save_body = PANELS_JS[idx:idx + 1200]
        assert "/api/model/moa" in save_body
        assert "method:'PUT'" in save_body

    def test_save_round_trips_unexposed_advanced_fields(self):
        """Saving must not silently drop fields this UI doesn't expose (e.g. set via CLI)."""
        idx = PANELS_JS.find("async function _saveMoaConfig")
        assert idx >= 0
        save_body = PANELS_JS[idx:idx + 1200]
        for field in ("reference_temperature", "aggregator_temperature", "max_tokens", "reference_max_tokens", "fanout"):
            assert field in save_body, f"_saveMoaConfig must round-trip '{field}'"

    def test_load_called_from_settings_panel(self):
        assert "_loadMoaConfig();" in PANELS_JS

    def test_enable_toggle_controls_field_visibility(self):
        assert "function _updateMoaFieldsVisibility" in PANELS_JS
        assert "moaEnabled" in PANELS_JS
        assert "moaFields" in PANELS_JS

    def test_add_and_remove_agent_wiring(self):
        assert "btnMoaAddAgent" in PANELS_JS
        assert "_moaAgentsState.push" in PANELS_JS
        assert "_moaAgentsState.splice" in PANELS_JS

    def test_custom_model_prompt_reused(self):
        """MoA model dropdowns should reuse the same custom-model prompt as auxiliary models."""
        idx = PANELS_JS.find("async function _onMoaModelSelectChange")
        assert idx >= 0
        body = PANELS_JS[idx:idx + 700]
        assert "__custom__" in body
        assert "showPromptDialog" in body

    def test_dirty_flag_marking(self):
        assert "function _markMoaDirty" in PANELS_JS
        idx = PANELS_JS.find("function _markMoaDirty")
        body = PANELS_JS[idx:idx + 200]
        assert "_markSettingsDirty" in body

    def test_load_preserves_reasoning_effort_for_agents_and_aggregator(self):
        """#audit MEDIUM: reasoning_effort has no UI control, but the backend
        persists it per slot (agents and aggregator alike) -- a load must not
        drop it, or the very next save silently erases it."""
        idx = PANELS_JS.find("async function _loadMoaConfig")
        assert idx >= 0
        body = PANELS_JS[idx:idx + 1800]
        assert "reasoning_effort:(a&&a.reasoning_effort)||''" in body, (
            "_loadMoaConfig must carry reasoning_effort into _moaAgentsState"
        )
        assert "reasoning_effort:(_moaMeta.aggregator&&_moaMeta.aggregator.reasoning_effort)||''" in body, (
            "_loadMoaConfig must carry reasoning_effort into _moaAggregatorState"
        )

    def test_save_uses_moa_slot_payload_for_agents_and_aggregator(self):
        """Both the agents map and the aggregator must build their save payload
        through the same helper, so reasoning_effort round-trips for both."""
        assert "function _moaSlotPayload" in PANELS_JS
        idx = PANELS_JS.find("async function _saveMoaConfig")
        assert idx >= 0
        save_body = PANELS_JS[idx:idx + 500]
        assert ".map(_moaSlotPayload)" in save_body
        assert "_moaSlotPayload(_moaAggregatorState)" in save_body

    @pytest.mark.skipif(NODE is None, reason="node not on PATH")
    def test_moa_slot_payload_preserves_reasoning_effort_and_omits_when_blank(self):
        """Live-executes _moaSlotPayload (pure, DOM-free) to prove the fix, not
        just that the right substrings are present."""
        script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');

function extract(name){
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if(start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 0;
  while(i < src.length){
    const ch = src[i];
    if(ch === '{') depth += 1;
    else if(ch === '}') {
      depth -= 1;
      if(depth === 0) break;
    }
    i += 1;
  }
  if(depth !== 0) throw new Error(name + ' parse failed');
  return src.slice(start, i + 1);
}

eval(extract('_moaSlotPayload'));

const withEffort = _moaSlotPayload({provider:'openai-codex', model:'gpt-5.5', reasoning_effort:'low'});
const blankEffort = _moaSlotPayload({provider:'openrouter', model:'x', reasoning_effort:''});
const missingField = _moaSlotPayload({provider:'openrouter', model:'x'});

console.log(JSON.stringify({withEffort, blankEffort, missingField}));
"""
        proc = subprocess.run(
            [NODE, "-e", script, str(PANELS_JS_PATH)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert proc.returncode == 0, f"node probe failed:\n{proc.stderr}"
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["withEffort"] == {
            "provider": "openai-codex", "model": "gpt-5.5", "reasoning_effort": "low",
        }
        assert result["blankEffort"] == {"provider": "openrouter", "model": "x"}
        assert result["missingField"] == {"provider": "openrouter", "model": "x"}


class TestMoaSettingsI18n:
    """MoA i18n keys must be translated into every mandatory-parity locale.

    This repo enforces exact key-parity between LOCALES.en and 10 locales
    (zh, zh-Hant, cs, ja, ko, pl, ru, es, tr, vi) via dedicated coverage
    tests (test_chinese_locale.py etc.) that fail hard on any EN-only key --
    this package's keys were originally EN-only per the task's initial
    scope, then translated (see the i18n(models) parity commit) so this
    branch stays self-contained for an upstream PR. it/de/fr/pt have no
    such coverage test and are intentionally left on the English fallback.
    """

    REQUIRED_KEYS = [
        "settings_label_moa",
        "settings_desc_moa",
        "settings_moa_enable",
        "settings_moa_agents_label",
        "settings_moa_agent_provider_placeholder",
        "settings_moa_agent_model_placeholder",
        "settings_moa_btn_add_agent",
        "settings_moa_btn_remove_agent",
        "settings_moa_aggregator_label",
        "settings_moa_aggregator_desc",
        "settings_moa_btn_save",
        "settings_moa_saved",
        "settings_moa_save_failed",
        "settings_moa_loading",
        "settings_moa_load_failed",
        "settings_moa_other_presets_note",
    ]

    # Every locale block in i18n.js. settings_label_moa/settings_desc_moa are
    # referenced from index.html via data-i18n, and
    # test_provider_quota_status.py::test_settings_label_and_description_i18n_keys_exist_for_all_locales
    # requires such keys in ALL locale blocks — so the whole MoA key set keeps
    # full parity rather than only the 10 locales with dedicated parity suites.
    ALL_LOCALE_COUNT = 15

    def test_all_keys_present(self):
        for key in self.REQUIRED_KEYS:
            assert key in I18N_JS, f"Missing i18n key '{key}' in i18n.js"

    def test_keys_translated_in_all_locales(self):
        """Each key must appear exactly once per locale block (no accidental
        duplicate insertion, no locale left behind)."""
        for key in self.REQUIRED_KEYS:
            count = I18N_JS.count(f"{key}:")
            assert count == self.ALL_LOCALE_COUNT, (
                f"i18n key '{key}' found {count} times — expected exactly "
                f"{self.ALL_LOCALE_COUNT} (one per locale block)"
            )

    def test_moa_keys_present_in_english_locale_block(self):
        """Every key must (at minimum) live inside LOCALES.en."""
        en_start = I18N_JS.find("\n  en: {")
        it_start = I18N_JS.find("\n  it: {")
        assert en_start >= 0 and it_start > en_start
        for key in self.REQUIRED_KEYS:
            key_idx = I18N_JS.find(f"{key}:")
            assert en_start < key_idx < it_start, f"'{key}' must be inside LOCALES.en"


class TestMoaBackendRoutes:
    """Route registration in routes.py."""

    def test_get_route_registered(self):
        assert '"/api/model/moa"' in ROUTES_PY

    def test_get_and_put_functions_exist(self):
        assert "def get_moa_config" in CONFIG_PY
        assert "def set_moa_config" in CONFIG_PY

    def test_route_uses_put_not_new_write_mechanism(self):
        """The write path must go through set_moa_config → _cfg_lock / _save_yaml_config_file, the
        same primitives set_auxiliary_model uses — no bespoke config writer."""
        idx = CONFIG_PY.find("def set_moa_config")
        assert idx >= 0
        body = CONFIG_PY[idx:idx + 4000]
        assert "_get_config_path()" in body
        assert "with _cfg_lock:" in body
        assert "_load_yaml_config_file(config_path)" in body
        assert "_save_yaml_config_file(config_path, config_data)" in body
        assert "reload_config()" in body


class TestMoaBackendBehavior:
    """Behavioral tests for get_moa_config / set_moa_config."""

    def test_get_returns_disabled_default_for_empty_config(self, monkeypatch):
        from api import config

        monkeypatch.setattr(config, "reload_config", lambda: None)
        monkeypatch.setattr(config, "cfg", {})

        data = config.get_moa_config()

        assert data["enabled"] is False
        assert data["reference_models"] == []
        assert data["aggregator"] == {"provider": "", "model": ""}
        assert data["preset"] == "default"
        assert data["other_presets"] == []

    def test_get_reflects_saved_incomplete_state_without_substituting_defaults(self, monkeypatch):
        """A config editor must show the user's real (possibly incomplete) data,
        not hermes_cli's runtime fallback to hardcoded example agents."""
        from api import config

        monkeypatch.setattr(config, "reload_config", lambda: None)
        monkeypatch.setattr(config, "cfg", {
            "moa": {"enabled": False, "reference_models": [{"provider": "openai", "model": ""}], "aggregator": {}},
        })

        data = config.get_moa_config()

        assert data["reference_models"] == [{"provider": "openai", "model": ""}]
        assert data["aggregator"] == {"provider": "", "model": ""}

    def test_set_writes_flat_moa_key_and_round_trips(self, monkeypatch, tmp_path):
        """set_moa_config() itself returns the saved config shape (no "ok" wrapper —
        that's added by the route, see TestMoaRouteDispatch)."""
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text("model:\n  provider: openai\n  default: gpt-5.5\n", encoding="utf-8")
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

        result = config.set_moa_config({
            "enabled": True,
            "reference_models": [
                {"provider": "openai-codex", "model": "gpt-5.5"},
                {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
            ],
            "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
        })

        assert result["enabled"] is True
        assert len(result["reference_models"]) == 2

        text = config_path.read_text(encoding="utf-8")
        assert "openai-codex" in text
        assert "deepseek/deepseek-v4-pro" in text
        assert "anthropic/claude-opus-4.8" in text
        assert "presets:" not in text  # flat write for a config with no prior "moa" key

        saved = config._load_yaml_config_file(config_path)
        assert saved["moa"]["enabled"] is True
        assert saved["moa"]["reference_models"][0] == {"provider": "openai-codex", "model": "gpt-5.5"}

    def test_set_rejects_agent_missing_model_when_enabled(self, monkeypatch, tmp_path):
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text("model:\n  provider: openai\n  default: gpt-5.5\n", encoding="utf-8")
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

        with pytest.raises(ValueError, match="agent 1"):
            config.set_moa_config({
                "enabled": True,
                "reference_models": [{"provider": "openai", "model": ""}],
                "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            })

    def test_set_rejects_empty_agents_when_enabled(self, monkeypatch, tmp_path):
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text("model:\n  provider: openai\n  default: gpt-5.5\n", encoding="utf-8")
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

        with pytest.raises(ValueError, match="at least one agent"):
            config.set_moa_config({
                "enabled": True,
                "reference_models": [],
                "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            })

    def test_set_allows_empty_agents_when_disabled(self, monkeypatch, tmp_path):
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text("model:\n  provider: openai\n  default: gpt-5.5\n", encoding="utf-8")
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

        result = config.set_moa_config({"enabled": False, "reference_models": [], "aggregator": {}})

        assert result["enabled"] is False

    def test_set_rejects_unknown_top_level_field(self, monkeypatch, tmp_path):
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text("model:\n  provider: openai\n  default: gpt-5.5\n", encoding="utf-8")
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

        with pytest.raises(ValueError, match="Unknown field"):
            config.set_moa_config({"enabled": False, "bogus_field": 1})

    def test_set_rejects_unknown_slot_field(self, monkeypatch, tmp_path):
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text("model:\n  provider: openai\n  default: gpt-5.5\n", encoding="utf-8")
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

        with pytest.raises(ValueError, match="unknown field"):
            config.set_moa_config({
                "enabled": True,
                "reference_models": [{"provider": "openai", "model": "gpt-5.5", "bogus": 1}],
                "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            })

    def test_set_rejects_recursive_moa_provider(self, monkeypatch, tmp_path):
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text("model:\n  provider: openai\n  default: gpt-5.5\n", encoding="utf-8")
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

        with pytest.raises(ValueError, match="recursive MoA"):
            config.set_moa_config({
                "enabled": True,
                "reference_models": [{"provider": "moa", "model": "default"}],
                "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            })

    def test_set_preserves_other_named_presets(self, monkeypatch, tmp_path):
        """Writing the default preset must not clobber sibling presets a user
        built via the CLI/dashboard."""
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "moa:\n"
            "  default_preset: default\n"
            "  presets:\n"
            "    default:\n"
            "      enabled: true\n"
            "      reference_models:\n"
            "        - {provider: openai, model: gpt-5.5}\n"
            "      aggregator: {provider: openai, model: gpt-5.5}\n"
            "    Frontier Tuned:\n"
            "      enabled: true\n"
            "      reference_models:\n"
            "        - {provider: openrouter, model: anthropic/claude-opus-4.8}\n"
            "      aggregator: {provider: openrouter, model: anthropic/claude-opus-4.8}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

        result = config.set_moa_config({
            "enabled": True,
            "reference_models": [{"provider": "copilot", "model": "claude-sonnet-4.6"}],
            "aggregator": {"provider": "copilot", "model": "gpt-5.5"},
        })

        assert result["other_presets"] == ["Frontier Tuned"]
        saved = config._load_yaml_config_file(config_path)
        assert "Frontier Tuned" in saved["moa"]["presets"]
        assert saved["moa"]["presets"]["Frontier Tuned"]["aggregator"]["provider"] == "openrouter"
        assert saved["moa"]["presets"]["default"]["reference_models"][0]["provider"] == "copilot"

    def test_get_after_set_round_trip(self, monkeypatch, tmp_path):
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text("model:\n  provider: openai\n  default: gpt-5.5\n", encoding="utf-8")
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

        config.set_moa_config({
            "enabled": True,
            "reference_models": [{"provider": "openai-codex", "model": "gpt-5.5", "reasoning_effort": "low"}],
            "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            "max_tokens": 2048,
            "fanout": "user_turn",
        })

        data = config.get_moa_config()

        assert data["enabled"] is True
        assert data["reference_models"] == [{"provider": "openai-codex", "model": "gpt-5.5", "reasoning_effort": "low"}]
        assert data["aggregator"] == {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"}
        assert data["max_tokens"] == 2048
        assert data["fanout"] == "user_turn"


class TestMoaRouteDispatch:
    """PUT /api/model/moa route wiring (routes.py handle_put)."""

    def test_put_route_returns_400_on_validation_error(self, monkeypatch):
        from api import routes

        monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
        monkeypatch.setattr(routes, "read_body", lambda _handler: {
            "enabled": True,
            "reference_models": [],
            "aggregator": {},
        })
        monkeypatch.setattr(
            routes,
            "bad",
            lambda _handler, msg, status=400: {"ok": False, "error": msg, "status": status},
        )
        monkeypatch.setattr(
            routes,
            "_guard_request_session_visibility",
            lambda *_a, **_k: True,
        )
        monkeypatch.setattr(
            routes,
            "_handle_extension_sidecar_proxy",
            lambda *_a, **_k: False,
        )

        result = routes.handle_put(object(), SimpleNamespace(path="/api/model/moa", query=""))

        assert result["status"] == 400
        assert "agent" in result["error"]

    def test_put_route_returns_ok_payload_on_success(self, monkeypatch):
        from api import routes

        monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
        monkeypatch.setattr(routes, "read_body", lambda _handler: {
            "enabled": False,
            "reference_models": [],
            "aggregator": {},
        })
        monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
        monkeypatch.setattr(
            routes,
            "_guard_request_session_visibility",
            lambda *_a, **_k: True,
        )
        monkeypatch.setattr(
            routes,
            "_handle_extension_sidecar_proxy",
            lambda *_a, **_k: False,
        )

        from api import config as api_config
        monkeypatch.setattr(
            api_config,
            "set_moa_config",
            lambda _payload: {"enabled": False, "reference_models": [], "aggregator": {"provider": "", "model": ""}},
        )

        result = routes.handle_put(object(), SimpleNamespace(path="/api/model/moa", query=""))

        assert result["ok"] is True
        assert result["enabled"] is False
