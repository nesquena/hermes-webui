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
        save_body = PANELS_JS[idx:idx + 2600]
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

    def test_dirty_flag_is_section_owned(self):
        """Gate finding 6: MoA saves through its OWN button/transaction; the
        global Settings dirty/save path must not be involved (previously the
        global form went dirty but saveSettings() never saved MoA)."""
        assert "function _markMoaDirty" in PANELS_JS
        idx = PANELS_JS.find("function _markMoaDirty")
        body = PANELS_JS[idx:idx + 220]
        assert "_markSettingsDirty" not in body
        assert "_moaDirty=true" in body
        assert "_updateMoaSaveButtonState" in body

    def test_load_preserves_reasoning_effort_for_agents_and_aggregator(self):
        """#audit MEDIUM: reasoning_effort has no UI control, but the backend
        persists it per slot (agents and aggregator alike) -- a load must not
        drop it, or the very next save silently erases it."""
        idx = PANELS_JS.find("async function _loadMoaConfig")
        assert idx >= 0
        body = PANELS_JS[idx:idx + 3600]
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
        # Scanner-clean harness: the function under test is extracted in
        # PYTHON and embedded as ordinary program text, so node only ever runs
        # a literal source file (gate safety-boundary requirement).
        src = PANELS_JS
        start = src.find("function _moaSlotPayload(")
        assert start >= 0
        i = src.index("{", start)
        depth = 0
        while i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        fn_source = src[start:i + 1]
        script = fn_source + r"""

const withEffort = _moaSlotPayload({provider:'openai-codex', model:'gpt-5.5', reasoning_effort:'low'});
const blankEffort = _moaSlotPayload({provider:'openrouter', model:'x', reasoning_effort:''});
const missingField = _moaSlotPayload({provider:'openrouter', model:'x'});

console.log(JSON.stringify({withEffort, blankEffort, missingField}));
"""
        proc = subprocess.run(
            [NODE, "-e", script],
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


def _extract_js_function(src: str, name: str) -> str:
    """Return one brace-balanced function definition as ordinary program text.

    The extraction happens in PYTHON and the result is embedded as a literal
    source file, so node only ever runs code that already exists in the repo
    (gate safety-boundary requirement).
    """
    start = src.index(f"function {name}(")
    while start > 0 and src[start - 1] in "async ":
        start -= 1
    i = src.index("{", src.index(f"function {name}("))
    depth = 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return src[start:i + 1]


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
        # Blocker 3: the partial-persistence report and the explicit
        # Reload/Discard choice on a stale save are user-facing text too.
        "settings_saved_partial",
        "settings_moa_stale_title",
        "settings_moa_stale_body",
        "settings_moa_stale_choice",
        "settings_moa_stale_reload",
        "settings_moa_stale_keep",
        "settings_moa_stale_kept",
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
        body = CONFIG_PY[idx:idx + 9000]
        assert "_get_config_path()" in body
        assert "with _cfg_lock:" in body
        # RAW loader: the expanding one would materialize ${VAR} secrets from
        # elsewhere in config.yaml into the file on every MoA save.
        assert "_load_yaml_config_file_raw(config_path)" in body
        assert "_load_yaml_config_file(config_path)" not in body
        assert "_save_yaml_config_file(config_path, config_data)" in body
        assert "reload_config()" in body


class TestMoaBackendBehavior:
    """Behavioral tests for get_moa_config / set_moa_config."""

    @pytest.fixture(autouse=True)
    def _restore_config_module_state(self):
        """Tests here redirect _get_config_path to a tmp file and drive the
        real reload_config(), which repoints the module-global config cache
        (_cfg_cache/_cfg_path/_cfg_mtime) at that tmp file. Reload from the
        real path afterwards so later tests (e.g. test_model_resolver's
        in-place cfg mutations) don't see a poisoned cache."""
        from api import config

        real_get_config_path = config._get_config_path
        yield
        with config._cfg_lock:
            config._refresh_config_cache(real_get_config_path())

    def test_get_returns_disabled_default_for_empty_config(self, monkeypatch):
        from api import config

        monkeypatch.setattr(config, "reload_config", lambda: None)
        monkeypatch.setattr(config, "cfg", {})

        data = config.get_moa_config()

        assert data["enabled"] is False
        assert data["reference_models"] == []
        # No persisted aggregator mapping at all -> nothing to bind a handle to.
        assert data["aggregator"] == {"provider": "", "model": ""}
        assert data["preset"] == "default"
        assert data["other_presets"] == []

    def test_get_reflects_saved_incomplete_state_without_substituting_defaults(self, monkeypatch, tmp_path):
        """A config editor must show the user's real (possibly incomplete) data,
        not hermes_cli's runtime fallback to hardcoded example agents."""
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "moa:\n"
            "  enabled: false\n"
            "  reference_models:\n"
            "    - {provider: openai, model: ''}\n"
            "  aggregator: {}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

        data = config.get_moa_config()

        # Slots are pure content: provenance rides in its own envelope, so a
        # persisted field named `origin` can never be confused with a handle.
        assert data["reference_models"] == [{"provider": "openai", "model": ""}]
        assert data["aggregator"] == {"provider": "", "model": ""}
        # An empty aggregator mapping IS persisted state, so it gets a handle:
        # filling it in later must still merge onto that same singleton slot.
        assert data["slot_origins"] == {
            "reference_models": ["ref:0"], "aggregator": "aggregator",
        }

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
        assert data["reference_models"] == [
            {"provider": "openai-codex", "model": "gpt-5.5", "reasoning_effort": "low"}
        ]
        assert data["aggregator"] == {
            "provider": "openrouter", "model": "anthropic/claude-opus-4.8",
        }
        assert data["slot_origins"] == {
            "reference_models": ["ref:0"], "aggregator": "aggregator",
        }
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


class TestMoaGateFollowups:
    """Contracts from the config-data-loss gate review."""

    @pytest.fixture(autouse=True)
    def _restore_config_module_state(self):
        from api import config

        real_get_config_path = config._get_config_path
        yield
        with config._cfg_lock:
            config._refresh_config_cache(real_get_config_path())

    def _seed(self, tmp_path, monkeypatch, yaml_text):
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml_text, encoding="utf-8")
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        return config, config_path

    # ── Finding 1: merge-don't-replace ──────────────────────────────────

    def test_unknown_root_preset_and_slot_fields_survive_a_ui_save(
        self, monkeypatch, tmp_path
    ):
        config, config_path = self._seed(
            tmp_path,
            monkeypatch,
            "moa:\n"
            "  enabled: true\n"
            "  future_root_flag: keep-me\n"
            "  reference_models:\n"
            "    - provider: openai\n"
            "      model: gpt-5.5\n"
            "      future_slot_flag: keep-me-too\n"
            "  aggregator:\n"
            "    provider: openrouter\n"
            "    model: agg\n"
            "    agg_extra: keep-me-three\n",
        )
        # Round-trip exactly what the editor loaded, provenance envelope
        # included — that is what binds the merge (see TestMoaSlotOriginMerge).
        current = config.get_moa_config()
        config.set_moa_config({
            "enabled": True,
            "reference_models": current["reference_models"],
            "aggregator": current["aggregator"],
            "preset": current["preset"],
            "revision": current["revision"],
            "slot_origins": current["slot_origins"],
        })
        saved = config._load_yaml_config_file_raw(config_path)["moa"]
        assert saved["future_root_flag"] == "keep-me"
        assert saved["reference_models"][0]["future_slot_flag"] == "keep-me-too"
        assert saved["reference_models"][0]["model"] == "gpt-5.5"
        assert saved["aggregator"]["agg_extra"] == "keep-me-three"

    def test_unknown_named_preset_fields_survive(self, monkeypatch, tmp_path):
        config, config_path = self._seed(
            tmp_path,
            monkeypatch,
            "moa:\n"
            "  default_preset: main\n"
            "  presets:\n"
            "    main:\n"
            "      enabled: true\n"
            "      cli_only_field: precious\n"
            "      reference_models:\n"
            "        - provider: openai\n"
            "          model: gpt-5.5\n"
            "      aggregator: {provider: openrouter, model: agg}\n",
        )
        config.set_moa_config({
            "enabled": False,
            "reference_models": [],
            "aggregator": {},
        })
        saved = config._load_yaml_config_file(config_path)["moa"]["presets"]["main"]
        assert saved["cli_only_field"] == "precious"
        assert saved["enabled"] is False

    # ── Finding 2: strict scalar validation ─────────────────────────────

    @pytest.mark.parametrize("payload,fragment", [
        ({"enabled": "false"}, "enabled must be a boolean"),
        ({"fanout": "sideways"}, "fanout must be"),
        ({"reference_temperature": float("nan")}, "finite"),
        ({"reference_temperature": float("inf")}, "finite"),
        ({"aggregator_temperature": 5.0}, "between 0 and 2"),
        ({"reference_temperature": "0.5"}, "must be a number"),
        ({"max_tokens": 0}, "positive integer"),
        ({"max_tokens": -5}, "positive integer"),
        ({"max_tokens": "4096"}, "positive integer"),
        ({"max_tokens": 4.5}, "positive integer"),
        ({"max_tokens": True}, "positive integer"),
        ({"reference_max_tokens": -1}, "positive integer"),
        ({"max_tokens": 10**12}, "implausibly large"),
    ])
    def test_malformed_scalars_are_rejected_not_coerced(
        self, monkeypatch, tmp_path, payload, fragment
    ):
        config, _p = self._seed(tmp_path, monkeypatch, "model:\n  default: x\n")
        body = {"enabled": False, "reference_models": [], "aggregator": {}}
        body.update(payload)
        with pytest.raises(ValueError, match=fragment):
            config.set_moa_config(body)

    # ── Finding 4: preset identity / optimistic concurrency ─────────────

    def test_get_exposes_a_stable_revision(self, monkeypatch, tmp_path):
        config, _p = self._seed(
            tmp_path, monkeypatch,
            "moa: {enabled: true, reference_models: [{provider: a, model: b}], aggregator: {provider: c, model: d}}\n",
        )
        config.reload_config()
        first = config.get_moa_config()
        second = config.get_moa_config()
        assert first["revision"] and first["revision"] == second["revision"]

    def test_put_with_stale_revision_is_rejected(self, monkeypatch, tmp_path):
        config, _p = self._seed(
            tmp_path, monkeypatch,
            "moa: {enabled: true, reference_models: [{provider: a, model: b}], aggregator: {provider: c, model: d}}\n",
        )
        with pytest.raises(config.MoaStaleEditError):
            config.set_moa_config({
                "enabled": False, "reference_models": [], "aggregator": {},
                "revision": "deadbeefdeadbeef",
            })

    def test_put_with_wrong_target_preset_is_rejected(self, monkeypatch, tmp_path):
        config, _p = self._seed(
            tmp_path, monkeypatch,
            "moa:\n"
            "  default_preset: bravo\n"
            "  presets:\n"
            "    alpha: {enabled: true}\n"
            "    bravo: {enabled: true}\n",
        )
        with pytest.raises(config.MoaStaleEditError):
            config.set_moa_config({
                "enabled": False, "reference_models": [], "aggregator": {},
                "preset": "alpha",
            })

    def test_put_with_current_handles_succeeds(self, monkeypatch, tmp_path):
        config, config_path = self._seed(
            tmp_path, monkeypatch,
            "moa: {enabled: true, reference_models: [{provider: a, model: b}], aggregator: {provider: c, model: d}}\n",
        )
        config.reload_config()
        current = config.get_moa_config()
        result = config.set_moa_config({
            "enabled": False, "reference_models": [], "aggregator": {},
            "preset": current["preset"], "revision": current["revision"],
        })
        assert result["enabled"] is False

    # ── Finding 5: runtime-semantic parity ──────────────────────────────

    def test_preset_without_enabled_key_reads_as_enabled(self, monkeypatch, tmp_path):
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "moa:\n"
            "  reference_models:\n"
            "    - {provider: a, model: b}\n"
            "  aggregator: {provider: c, model: d}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        assert config.get_moa_config()["enabled"] is True

    def test_truly_unset_moa_still_reads_disabled(self, monkeypatch, tmp_path):
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text("model:\n  provider: openai\n", encoding="utf-8")
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        assert config.get_moa_config()["enabled"] is False

    def test_yaml_false_reasoning_effort_reads_and_saves_as_none(
        self, monkeypatch, tmp_path
    ):
        config, config_path = self._seed(
            tmp_path, monkeypatch,
            "moa:\n"
            "  enabled: true\n"
            "  reference_models:\n"
            "    - provider: a\n"
            "      model: b\n"
            "      reasoning_effort: false\n"
            "  aggregator: {provider: c, model: d}\n",
        )
        config.reload_config()
        read = config.get_moa_config()
        assert read["reference_models"][0]["reasoning_effort"] == "none"
        config.set_moa_config({
            "enabled": True,
            "reference_models": [read["reference_models"][0]],
            "aggregator": {"provider": "c", "model": "d"},
        })
        saved = config._load_yaml_config_file(config_path)["moa"]
        assert saved["reference_models"][0]["reasoning_effort"] == "none"

    # ── Finding 3/6 + secondary UI: static contracts ─────────────────────

    def test_save_is_gated_on_successful_load(self):
        idx = PANELS_JS.find("async function _loadMoaConfig")
        body = PANELS_JS[idx:idx + 1200]
        assert "_moaLoaded=false" in body
        assert "settings_moa_loading" in body
        assert "settings_moa_load_failed" in body
        save_idx = PANELS_JS.find("async function _saveMoaConfig")
        save_body = PANELS_JS[save_idx:save_idx + 600]
        assert "if(!_moaLoaded)" in save_body

    def test_save_pins_preset_and_revision(self):
        idx = PANELS_JS.find("async function _saveMoaConfig")
        body = PANELS_JS[idx:idx + 1600]
        assert "body.preset=_moaMeta.preset" in body
        assert "body.revision=_moaMeta.revision" in body

    def test_agent_rows_meet_touch_targets_and_have_accessible_names(self):
        idx = PANELS_JS.find("function _renderMoaAgents")
        body = PANELS_JS[idx:idx + 2600]
        assert "1fr 1fr 44px" in body
        assert "minWidth='44px'" in body
        assert "setAttribute('aria-label'" in body
        assert "_restoreMoaFocus" in body


class TestMoaSlotOriginMerge:
    """Slot extras bind by the ORIGIN HANDLE the editor round-trips.

    Matching on `(provider, model)` lost metadata in the two cases the editor
    actually produces: duplicating a row made the identity ambiguous, and
    editing a row's provider/model changed its identity. GET now stamps each
    slot with the persisted slot it came from and PUT echoes it back, so the
    merge follows the row rather than guessing from its content.
    """

    @pytest.fixture(autouse=True)
    def _restore_config_module_state(self):
        from api import config

        real_get_config_path = config._get_config_path
        yield
        with config._cfg_lock:
            config._refresh_config_cache(real_get_config_path())

    def _seed_two_slots(self, tmp_path, monkeypatch):
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "moa:\n"
            "  enabled: true\n"
            "  reference_models:\n"
            "    - provider: prov-a\n"
            "      model: model-a\n"
            "      future_owner: first-slot\n"
            "    - provider: prov-b\n"
            "      model: model-b\n"
            "      future_owner: second-slot\n"
            "  aggregator: {provider: agg, model: agg-m, future_owner: agg-slot}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        return config, config_path

    def _current(self, config):
        config.reload_config()
        return config.get_moa_config()

    def _put(self, config, refs, *, origins=None, aggregator=None,
             aggregator_origin="aggregator", current=None, pin=True):
        """Save *refs* with an explicit provenance envelope.

        Provenance is a top-level envelope, never a slot field: a slot key
        called `origin` is ordinary config, and the previous in-band handle
        deleted exactly such a field on every round trip.
        """
        current = current or self._current(config)
        payload = {
            "enabled": True,
            "reference_models": refs,
            "aggregator": aggregator if aggregator is not None else current["aggregator"],
            "slot_origins": {
                "reference_models": (
                    origins if origins is not None else [None] * len(refs)
                ),
                "aggregator": aggregator_origin,
            },
        }
        if pin:
            payload["preset"] = current["preset"]
            payload["revision"] = current["revision"]
        return config.set_moa_config(payload)

    def _saved(self, config, config_path):
        return config._load_yaml_config_file_raw(config_path)["moa"]

    def test_get_exposes_origins_outside_the_slots(self, monkeypatch, tmp_path):
        config, _config_path = self._seed_two_slots(tmp_path, monkeypatch)
        current = self._current(config)
        for slot in current["reference_models"]:
            assert "origin" not in slot
        assert "origin" not in current["aggregator"]
        assert current["slot_origins"]["reference_models"] == ["ref:0", "ref:1"]
        assert current["slot_origins"]["aggregator"] == "aggregator"

    def test_the_handle_is_never_persisted(self, monkeypatch, tmp_path):
        config, config_path = self._seed_two_slots(tmp_path, monkeypatch)
        current = self._current(config)
        self._put(config, current["reference_models"], origins=["ref:0", "ref:1"],
                  current=current)
        saved = self._saved(config, config_path)
        for slot in saved["reference_models"]:
            assert "origin" not in slot
        assert "origin" not in saved["aggregator"]

    def test_a_persisted_field_named_origin_survives_a_round_trip(
        self, monkeypatch, tmp_path
    ):
        """The collision the in-band handle caused: `origin` is ordinary config.

        The previous revision excluded every persisted key literally named
        `origin` from the extras merge, so an ordinary save deleted a
        future/tool-owned field with that name.
        """
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "moa:\n"
            "  enabled: true\n"
            "  reference_models:\n"
            "    - provider: prov-a\n"
            "      model: model-a\n"
            "      origin: tool-owned-value\n"
            "  aggregator: {provider: agg, model: agg-m, origin: agg-owned-value}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        current = self._current(config)
        edited = dict(current["reference_models"][0])
        edited["model"] = "model-a-v2"
        self._put(config, [edited], origins=["ref:0"], current=current)
        saved = self._saved(config, config_path)
        assert saved["reference_models"][0]["origin"] == "tool-owned-value"
        assert saved["reference_models"][0]["model"] == "model-a-v2"
        assert saved["aggregator"]["origin"] == "agg-owned-value"

    def test_removing_the_first_slot_keeps_extras_on_the_right_agent(
        self, monkeypatch, tmp_path
    ):
        config, config_path = self._seed_two_slots(tmp_path, monkeypatch)
        current = self._current(config)
        self._put(config, current["reference_models"][1:], origins=["ref:1"],
                  current=current)
        saved = self._saved(config, config_path)["reference_models"]
        assert len(saved) == 1
        assert saved[0]["future_owner"] == "second-slot"

    def test_reordering_slots_keeps_extras_with_their_rows(self, monkeypatch, tmp_path):
        config, config_path = self._seed_two_slots(tmp_path, monkeypatch)
        current = self._current(config)
        self._put(config, list(reversed(current["reference_models"])),
                  origins=["ref:1", "ref:0"], current=current)
        saved = self._saved(config, config_path)["reference_models"]
        assert saved[0]["future_owner"] == "second-slot"
        assert saved[1]["future_owner"] == "first-slot"

    def test_editing_a_slots_model_keeps_its_metadata(self, monkeypatch, tmp_path):
        """The reviewer's counter-case: an ordinary edit must not drop extras.

        Under identity matching, changing the model changed the identity and
        the slot's metadata was silently dropped.
        """
        config, config_path = self._seed_two_slots(tmp_path, monkeypatch)
        current = self._current(config)
        edited = dict(current["reference_models"][0])
        edited["model"] = "model-a-v2"
        self._put(config, [edited, current["reference_models"][1]],
                  origins=["ref:0", "ref:1"], current=current)
        saved = self._saved(config, config_path)["reference_models"]
        assert saved[0]["model"] == "model-a-v2"
        assert saved[0]["future_owner"] == "first-slot"
        assert saved[1]["future_owner"] == "second-slot"

    def test_editing_the_aggregator_keeps_its_metadata(self, monkeypatch, tmp_path):
        """The aggregator is a singleton — it can never be ambiguous."""
        config, config_path = self._seed_two_slots(tmp_path, monkeypatch)
        current = self._current(config)
        aggregator = dict(current["aggregator"])
        aggregator["model"] = "agg-m-v2"
        self._put(config, current["reference_models"], origins=["ref:0", "ref:1"],
                  aggregator=aggregator, current=current)
        saved = self._saved(config, config_path)
        assert saved["aggregator"]["model"] == "agg-m-v2"
        assert saved["aggregator"]["future_owner"] == "agg-slot"

    def test_replaying_one_handle_on_two_rows_is_rejected(self, monkeypatch, tmp_path):
        """Cloning hidden metadata onto a second row is not a save the client
        may ask for: one persisted slot has one successor."""
        config, config_path = self._seed_two_slots(tmp_path, monkeypatch)
        before = config_path.read_text(encoding="utf-8")
        current = self._current(config)
        first = current["reference_models"][0]
        with pytest.raises(ValueError, match="more than one row"):
            self._put(config, [first, dict(first)], origins=["ref:0", "ref:0"],
                      current=current)
        assert config_path.read_text(encoding="utf-8") == before, "rejected write touched the file"

    def test_a_brand_new_row_carries_nothing(self, monkeypatch, tmp_path):
        config, config_path = self._seed_two_slots(tmp_path, monkeypatch)
        current = self._current(config)
        added = {"provider": "prov-c", "model": "model-c"}  # no origin handle
        self._put(config, current["reference_models"] + [added],
                  origins=["ref:0", "ref:1", None], current=current)
        saved = self._saved(config, config_path)["reference_models"]
        assert "future_owner" not in saved[2]

    @pytest.mark.parametrize("origins,expected", [
        (["ref:99"], "does not refer to a saved agent"),
        (["nope"], "unrecognized origin"),
        (["ref:01"], "unrecognized origin"),
        (["ref:-1"], "unrecognized origin"),
        (["aggregator"], "unrecognized origin"),
        ([7], "must be a string or null"),
        (["ref:0", "ref:1"], "exactly one entry per agent row"),
    ])
    def test_bad_handles_are_rejected_with_no_write(
        self, monkeypatch, tmp_path, origins, expected
    ):
        """Degrading to "new row" hid a client bug and silently dropped the
        metadata it was asked to carry. These are 400s with the file untouched.
        """
        config, config_path = self._seed_two_slots(tmp_path, monkeypatch)
        before = config_path.read_text(encoding="utf-8")
        current = self._current(config)
        row = {"provider": "prov-c", "model": "model-c"}
        with pytest.raises(ValueError, match=expected):
            self._put(config, [row], origins=origins, current=current)
        assert config_path.read_text(encoding="utf-8") == before, "rejected write touched the file"

    @pytest.mark.parametrize("origins,aggregator_origin", [
        (["ref:0"], None),          # the reference-model path alone
        ([None], "aggregator"),     # the aggregator path alone
    ])
    def test_a_handle_without_a_pinned_snapshot_is_rejected(
        self, monkeypatch, tmp_path, origins, aggregator_origin
    ):
        """An index means nothing without the snapshot it was read from.

        Parametrized so each path carries its own guard: with both handles in
        one payload, a single surviving check would mask the other's removal.
        """
        config, config_path = self._seed_two_slots(tmp_path, monkeypatch)
        before = config_path.read_text(encoding="utf-8")
        current = self._current(config)
        with pytest.raises(ValueError, match="preset and revision"):
            self._put(config, [current["reference_models"][0]], origins=origins,
                      aggregator_origin=aggregator_origin, current=current, pin=False)
        assert config_path.read_text(encoding="utf-8") == before

    def test_an_aggregator_handle_without_a_saved_aggregator_is_rejected(
        self, monkeypatch, tmp_path
    ):
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "moa:\n"
            "  enabled: true\n"
            "  reference_models:\n"
            "    - {provider: prov-a, model: model-a}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        current = self._current(config)
        assert current["slot_origins"]["aggregator"] is None
        before = config_path.read_text(encoding="utf-8")
        with pytest.raises(ValueError, match="does not refer to a saved aggregator"):
            self._put(config, current["reference_models"], origins=["ref:0"],
                      aggregator={"provider": "agg", "model": "agg-m"},
                      aggregator_origin="aggregator", current=current)
        assert config_path.read_text(encoding="utf-8") == before

    def test_get_and_put_hash_the_same_authority(self, monkeypatch, tmp_path):
        """Blocker 1: an untouched GET→PUT must not 409 against itself.

        GET used to build the preset from the env-EXPANDED module-global `cfg`
        while PUT hashed the RAW document, so a `${VAR}` inside the selected
        preset produced two different revisions and an unchanged round trip
        failed its own concurrency check.
        """
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "moa:\n"
            "  enabled: true\n"
            "  reference_models:\n"
            "    - {provider: prov-a, model: model-a}\n"
            "  aggregator: {provider: agg, model: agg-m}\n"
            "  future_endpoint: ${MOA_SECRET_ENDPOINT}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("MOA_SECRET_ENDPOINT", "https://secret.example/inference")
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

        current = self._current(config)
        # The placeholder must not travel to the browser resolved, either.
        assert "secret.example" not in json.dumps(current)

        self._put(config, current["reference_models"], origins=["ref:0"], current=current)

        text = config_path.read_text(encoding="utf-8")
        assert "${MOA_SECRET_ENDPOINT}" in text
        assert "secret.example" not in text

    def test_a_moa_save_does_not_materialize_env_placeholders(
        self, monkeypatch, tmp_path
    ):
        """Saving MoA must not resolve unrelated ${VAR} references on disk."""
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "providers:\n"
            "  openai:\n"
            "    api_key: ${OPENAI_API_KEY}\n"
            "moa:\n"
            "  enabled: true\n"
            "  reference_models:\n"
            "    - {provider: prov-a, model: model-a}\n"
            "  aggregator: {provider: agg, model: agg-m}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OPENAI_API_KEY", "sk-live-super-secret")
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

        current = self._current(config)
        self._put(config, current["reference_models"], current=current)

        text = config_path.read_text(encoding="utf-8")
        assert "sk-live-super-secret" not in text, (
            "a MoA save materialized an environment-backed secret into config.yaml"
        )
        assert "${OPENAI_API_KEY}" in text



class TestMoaUnsavedEditorProtection:
    """The MoA editor's dirty state must participate in the panel guards.

    `_moaDirty` is section-owned because MoA saves through /api/config/moa
    rather than /api/settings — but leaving it out of the navigation/close
    guards meant closing the panel discarded a half-edited preset without a
    word, and the unsaved-changes bar's Save button closed the panel after
    saving only half the dirty state.
    """

    @staticmethod
    def _fn(name):
        idx = PANELS_JS.index(f"function {name}(")
        return PANELS_JS[idx:PANELS_JS.index("\n}", idx)]

    def test_navigation_guard_consults_the_moa_dirty_flag(self):
        body = self._fn("_beforePanelSwitch")
        assert "_settingsHasUnsavedChanges()" in body
        predicate = self._fn("_settingsHasUnsavedChanges")
        assert "_settingsDirty" in predicate
        assert "_moaDirty" in predicate

    def test_close_guard_consults_the_moa_dirty_flag(self):
        body = self._fn("_closeSettingsPanel")
        assert "_settingsHasUnsavedChanges()" in body
        assert "!_settingsDirty" not in body

    def test_discard_clears_the_moa_dirty_flag_too(self):
        body = self._fn("_discardSettings")
        assert "_moaDirty = false" in body

    def test_save_and_close_settles_moa_before_reporting_success(self):
        """Blocker 3: the overall result is coordinated BEFORE it is announced.

        Both branches used to show "settings saved", clear `_settingsDirty` and
        hide the unsaved bar and only THEN await the MoA write, so a MoA
        failure left the UI claiming a success that had not happened.
        """
        idx = PANELS_JS.index("async function saveSettings(andClose)")
        body = PANELS_JS[idx:PANELS_JS.index("\nasync function _saveDirtyMoaBeforeClose", idx)]
        # Both close paths (password branch and the ordinary one).
        assert body.count("_saveDirtyMoaBeforeClose()") == 2
        # Read the markers in source order. Splitting on the gate alone is not
        # enough: a success toast moved ahead of the SECOND gate still lands
        # after the first one, and a per-segment check would wave it through.
        markers = {
            "gate": "_saveDirtyMoaBeforeClose()",
            "partial": "settings_saved_partial",
            "success": "showToast(t('settings_saved')",
            "success_pw": "showToast(t(saved.auth_just_enabled?",
            "close": "_hideSettingsPanel()",
        }
        found = []
        for kind, needle in markers.items():
            position = body.find(needle)
            while position >= 0:
                found.append((position, "success" if kind == "success_pw" else kind))
                position = body.find(needle, position + 1)
        sequence = [kind for _position, kind in sorted(found)]
        assert sequence == [
            "gate", "partial", "success", "close",
            "gate", "partial", "success", "close",
        ], f"unexpected save/report/close order: {sequence}"

    def test_a_failed_moa_save_keeps_the_panel_open(self):
        body = self._fn("_saveDirtyMoaBeforeClose")
        assert "if(!ok)" in body
        assert "_showSettingsUnsavedBar" in body
        assert "return false" in body

    def test_the_moa_save_reports_whether_it_persisted(self):
        idx = PANELS_JS.index("async function _saveMoaConfig()")
        body = PANELS_JS[idx:PANELS_JS.index("\nasync function saveSettings", idx)]
        assert "return true" in body
        assert "return false" in body

    def test_the_editor_round_trips_provenance_outside_the_slots(self):
        """The handle must not ride inside a slot: `origin` is ordinary config."""
        payload = self._fn("_moaSlotPayload")
        assert "out.origin" not in payload
        save = self._fn("_saveMoaConfig")
        assert "body.slot_origins=" in save
        assert "reference_models:keptAgents.map(a=>a.origin||null)" in save
        # Only claimed against a pinned snapshot — the server rejects it otherwise.
        assert "if(_moaMeta.preset&&_moaMeta.revision){" in save

    @pytest.mark.skipif(NODE is None, reason="node not on PATH")
    @pytest.mark.parametrize("answer,expect_reload", [(False, False), (True, True)])
    def test_a_stale_save_discards_the_draft_only_when_the_user_says_so(
        self, answer, expect_reload
    ):
        """Blocker 3, executed rather than pattern-matched.

        The 409 path used to call `_loadMoaConfig()` immediately, and that
        loader replaces the editor contents outright — so the conflict path
        destroyed the very draft it exists to protect. A substring check cannot
        tell a real conditional from a short-circuited one, so this runs the
        function with a stubbed environment and observes what it does.
        """
        script = _extract_js_function(PANELS_JS, "_moaSlotPayload") + "\n" \
            + _extract_js_function(PANELS_JS, "_saveMoaConfig") + r"""

const calls = {load: 0, toasts: [], confirms: 0};
let _moaGeneration = 0;
let _moaLoaded = true;
let _moaDirty = true;
let _moaAgentsState = [{provider:'p', model:'m', origin:'ref:0'}];
let _moaAggregatorState = {provider:'agg', model:'aggm', origin:'aggregator'};
let _moaMeta = {preset:'default', revision:'rev-1', max_tokens:4096};
const $ = () => ({checked:true});
const t = (key) => key;
const showToast = (msg) => calls.toasts.push(msg);
const showConfirmDialog = async () => { calls.confirms += 1; return ANSWER; };
const _loadMoaConfig = async () => { calls.load += 1; };
const api = async () => { const e = new Error('Reload before saving'); e.status = 409; throw e; };

_saveMoaConfig().then(result => {
  console.log(JSON.stringify({result, ...calls}));
});
"""
        script = script.replace("ANSWER", "true" if answer else "false")
        proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=20)
        assert proc.returncode == 0, f"node probe failed:\n{proc.stderr}"
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["result"] is False, "a stale save must report that it did not persist"
        assert result["confirms"] == 1, "the user must be asked before the draft is touched"
        assert result["load"] == (1 if expect_reload else 0), (
            "the draft was reloaded against the user's answer"
        )
        if not expect_reload:
            assert "settings_moa_stale_kept" in result["toasts"]

    @pytest.mark.skipif(NODE is None, reason="node not on PATH")
    def test_a_stale_save_sends_provenance_outside_the_slots(self):
        """The envelope the backend now requires, observed on the real request."""
        script = _extract_js_function(PANELS_JS, "_moaSlotPayload") + "\n" \
            + _extract_js_function(PANELS_JS, "_saveMoaConfig") + r"""

let sent = null;
let _moaGeneration = 0;
let _moaLoaded = true;
let _moaDirty = true;
let _moaAgentsState = [
  {provider:'p', model:'m', origin:'ref:0'},
  {provider:'q', model:'n', origin:''},
];
let _moaAggregatorState = {provider:'agg', model:'aggm', origin:'aggregator'};
let _moaMeta = {preset:'default', revision:'rev-1'};
const $ = () => ({checked:true});
const t = (key) => key;
const showToast = () => {};
const showConfirmDialog = async () => false;
const _loadMoaConfig = async () => {};
const api = async (_url, opts) => { sent = JSON.parse(opts.body); return {}; };

_saveMoaConfig().then(() => { console.log(JSON.stringify(sent)); });
"""
        proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=20)
        assert proc.returncode == 0, f"node probe failed:\n{proc.stderr}"
        sent = json.loads(proc.stdout.strip().splitlines()[-1])
        for slot in sent["reference_models"]:
            assert "origin" not in slot
        assert "origin" not in sent["aggregator"]
        assert sent["slot_origins"] == {
            "reference_models": ["ref:0", None], "aggregator": "aggregator",
        }

    def test_loads_and_saves_are_generation_fenced(self):
        """A stale completion must not paint the editor or mark newer work clean."""
        load = self._fn("_loadMoaConfig")
        assert "const gen=++_moaGeneration;" in load
        assert load.count("if(gen!==_moaGeneration) return;") >= 2
        assert load.index("const gen=++_moaGeneration;") < load.index("await Promise.all")
        pre_request = load[:load.index("await Promise.all")]
        assert "_moaDirty=false" not in pre_request, (
            "a load that fails or is superseded would have marked the draft clean"
        )
        save = self._fn("_saveMoaConfig")
        assert "const gen=++_moaGeneration;" in save
        assert "if(gen!==_moaGeneration)" in save

    def test_the_editor_takes_provenance_from_the_envelope(self):
        """Handles come from `slot_origins`, not from a field inside a slot."""
        load = self._fn("_loadMoaConfig")
        assert "const slotOrigins=(_moaMeta&&_moaMeta.slot_origins)||{};" in load
        assert "origin:refOrigins[index]||''" in load
        assert "origin:slotOrigins.aggregator||''" in load
