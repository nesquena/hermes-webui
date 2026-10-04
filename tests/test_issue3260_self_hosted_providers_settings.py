"""Regression coverage for self-hosted provider setup in Settings after onboarding (#3260)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import api.config as config
import api.onboarding as onboarding
import api.profiles as profiles
import pytest

from tests.js_source_extract import extract_function
from tests.test_provider_management import _install_fake_hermes_cli, _post


ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


_DRIVER = r"""
const fs = require('fs');
const scenario = JSON.parse(process.argv[2] || '{}');
const fnSource = fs.readFileSync(process.argv[3], 'utf8');
const calls = [];
const toasts = [];
let refreshCount = 0;
let reloadCount = 0;

globalThis._providerCardEls = new Map();
const saveBtn = { disabled: false, textContent: 'Save' };
const baseUrlInput = { value: scenario.baseUrl };
const apiKeyInput = { value: scenario.apiKey || '' };
const modelInput = { value: scenario.model };

globalThis._providerCardEls.set(scenario.providerId, {
  isSelfHosted: true,
  baseUrlInput,
  apiKeyInput,
  modelInput,
  saveBtn,
});

globalThis.api = async (url, opts) => {
  calls.push({
    url,
    method: opts.method,
    body: JSON.parse(opts.body),
  });
  return { ok: true, provider: scenario.providerId };
};
globalThis.showToast = (msg) => toasts.push(msg);
globalThis._refreshModelDropdownsAfterProviderChange = () => { refreshCount += 1; };
globalThis.loadProvidersPanel = async () => { reloadCount += 1; };

eval(fnSource);

(async () => {
  await _saveSelfHostedProvider(scenario.providerId);
  process.stdout.write(JSON.stringify({
    calls,
    toasts,
    refreshCount,
    reloadCount,
    apiKeyAfter: apiKeyInput.value,
  }));
})().catch((error) => {
  process.stderr.write(String(error && error.stack || error));
  process.exit(1);
});
"""


_PROBE_DRIVER = r"""
const fs = require('fs');
const scenario = JSON.parse(process.argv[2] || '{}');
const fnSource = fs.readFileSync(process.argv[3], 'utf8');
const calls = [];
const toasts = [];
const saveBtn = { disabled: true, textContent: 'Save' };
const testBtn = { disabled: false, textContent: 'Test connection' };
const probeStatus = { style: {}, textContent: '' };
const baseUrlInput = { value: scenario.baseUrl };
const apiKeyInput = { value: scenario.apiKey || '' };
const modelInput = { value: scenario.model || '' };

globalThis._providerCardEls = new Map();
globalThis._providerCardEls.set(scenario.providerId, {
  isSelfHosted: true,
  baseUrlInput,
  apiKeyInput,
  modelInput,
  saveBtn,
  testBtn,
  probeStatus,
  setModelChoices(models) {
    this.modelChoices = models;
  },
  updateSaveState() {
    saveBtn.disabled = !(baseUrlInput.value.trim() && modelInput.value.trim());
  },
});

globalThis.api = async (url, opts) => {
  calls.push({
    url,
    method: opts.method,
    body: JSON.parse(opts.body),
  });
  const discoveredModels = Array.isArray(scenario.discoveredModels)
    ? scenario.discoveredModels
    : [{ id: scenario.discoveredModel }];
  return {
    ok: true,
    models: discoveredModels,
  };
};
globalThis.showToast = (msg) => toasts.push(msg);

eval(fnSource);

(async () => {
  await _testSelfHostedConnection(scenario.providerId);
  process.stdout.write(JSON.stringify({
    calls,
    toasts,
    saveDisabled: saveBtn.disabled,
    modelValue: modelInput.value,
    probeText: probeStatus.textContent,
    testDisabled: testBtn.disabled,
  }));
})().catch((error) => {
  process.stderr.write(String(error && error.stack || error));
  process.exit(1);
});
"""


_CARD_DRIVER = r"""
const fs = require('fs');
const scenario = JSON.parse(process.argv[2] || '{}');
const fnSource = fs.readFileSync(process.argv[3], 'utf8');

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.className = '';
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.textContent = '';
    this.value = '';
    this.disabled = false;
    this.classList = { toggle() {}, contains() { return false; } };
  }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener() {}
  setAttribute(name, value) { this.attributes[name] = value; }
  set innerHTML(value) {
    this._innerHTML = value;
    this.textContent = String(value).replace(/<[^>]*>/g, '');
  }
  get innerHTML() { return this._innerHTML || ''; }
}

globalThis.document = { createElement: (tag) => new Element(tag) };
globalThis._providerCardEls = new Map();
globalThis._SELF_HOSTED_DEFAULT_BASE_URLS = {
  ollama: 'http://localhost:11434/v1',
  lmstudio: 'http://localhost:1234/v1',
};
globalThis.esc = (value) => String(value);
globalThis.t = (key) => ({
  providers_status_configured: 'API key configured',
  providers_status_configured_label: 'Configured',
  providers_status_not_configured_label: 'Not configured',
  providers_status_api_key: 'API key',
  providers_status_model: 'Model',
  providers_save: 'Save',
  providers_remove: 'Remove',
}[key] || key);

eval(fnSource);
const card = _buildProviderCard(scenario.provider);
const header = card.children[0];
  const fields = _providerCardEls.get(scenario.provider.id);
  process.stdout.write(JSON.stringify({
    headerText: header.textContent,
    headerHtml: header.innerHTML,
    baseUrlValue: fields && fields.baseUrlInput ? fields.baseUrlInput.value : null,
    modelValue: fields && fields.modelInput ? fields.modelInput.value : null,
    modelChoices: fields && fields.modelDatalist
      ? fields.modelDatalist.children.map(option => option.value)
      : [],
  }));
"""


@pytest.fixture
def isolated_self_hosted_env(monkeypatch, tmp_path):
    _install_fake_hermes_cli(monkeypatch)
    fake_config_path = tmp_path / "config.yaml"
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    config.cfg["providers"] = {}
    config._cfg_mtime = 0.0
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(onboarding, "_get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(onboarding, "_get_config_path", lambda: fake_config_path)
    monkeypatch.setattr(config, "_get_config_path", lambda: fake_config_path)
    yield tmp_path, fake_config_path
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime


def test_post_self_hosted_provider_succeeds_for_ollama(isolated_self_hosted_env):
    _tmp_path, _fake_config_path = isolated_self_hosted_env
    body, status = _post("/api/providers/self-hosted", {
        "provider": "ollama",
        "model": "qwen3:8b",
        "base_url": "http://127.0.0.1:11434/v1",
    })
    assert status == 200
    assert body["ok"] is True
    assert body["provider"] == "ollama"


def test_post_self_hosted_provider_succeeds_for_lmstudio(isolated_self_hosted_env):
    _tmp_path, _fake_config_path = isolated_self_hosted_env
    body, status = _post("/api/providers/self-hosted", {
        "provider": "lmstudio",
        "model": "local-model",
        "base_url": "http://127.0.0.1:1234/v1",
    })
    assert status == 200
    assert body["ok"] is True
    assert body["provider"] == "lmstudio"


def test_apply_self_hosted_provider_setup_persists_ollama_base_url_and_active_model(isolated_self_hosted_env, monkeypatch):
    _tmp_path, fake_config_path = isolated_self_hosted_env
    calls = []
    monkeypatch.setattr(onboarding, "invalidate_models_cache", lambda: calls.append("invalidate"))
    body = onboarding.apply_self_hosted_provider_setup({
        "provider": "ollama",
        "model": "qwen3:8b",
        "base_url": "http://127.0.0.1:11434/v1",
    })
    assert body["ok"] is True
    assert body["provider"] == "ollama"
    cfg = onboarding._load_yaml_config(fake_config_path)
    assert cfg["providers"]["ollama"]["base_url"] == "http://127.0.0.1:11434/v1"
    assert "model" not in cfg["providers"]["ollama"]
    assert cfg["model"]["provider"] == "ollama"
    assert cfg["model"]["base_url"] == "http://127.0.0.1:11434/v1"
    assert cfg["model"]["default"] == onboarding._normalize_model_for_provider("ollama", "qwen3:8b")
    assert calls == ["invalidate"]


def test_apply_self_hosted_provider_setup_persists_lmstudio_base_url_and_active_model(isolated_self_hosted_env, monkeypatch):
    _tmp_path, fake_config_path = isolated_self_hosted_env
    calls = []
    monkeypatch.setattr(onboarding, "invalidate_models_cache", lambda: calls.append("invalidate"))
    body = onboarding.apply_self_hosted_provider_setup({
        "provider": "lmstudio",
        "model": "local-model",
        "base_url": "http://127.0.0.1:1234/v1",
    })
    assert body["ok"] is True
    assert body["provider"] == "lmstudio"
    cfg = onboarding._load_yaml_config(fake_config_path)
    assert cfg["providers"]["lmstudio"]["base_url"] == "http://127.0.0.1:1234/v1"
    assert "model" not in cfg["providers"]["lmstudio"]
    assert cfg["model"]["provider"] == "lmstudio"
    assert cfg["model"]["base_url"] == "http://127.0.0.1:1234/v1"
    assert cfg["model"]["default"] == onboarding._normalize_model_for_provider("lmstudio", "local-model")
    assert calls == ["invalidate"]


def test_apply_self_hosted_provider_setup_persists_base_url_without_switching_active_provider(
    isolated_self_hosted_env,
    monkeypatch,
):
    _tmp_path, fake_config_path = isolated_self_hosted_env
    original_model_cfg = {
        "provider": "anthropic",
        "default": "claude-sonnet-4-5",
        "base_url": "",
    }
    onboarding._save_yaml_config(fake_config_path, {
        "model": {
            **original_model_cfg,
            "custom_flag": "preserve-me",
        },
        "providers": {},
    })
    calls = []
    monkeypatch.setattr(onboarding, "invalidate_models_cache", lambda: calls.append("invalidate"))
    body = onboarding.apply_self_hosted_provider_setup({
        "provider": "ollama",
        "model": "qwen3:8b",
        "base_url": "http://127.0.0.1:11434/v1",
        "activate": False,
    })
    assert body["ok"] is True
    assert body["provider"] == "ollama"
    assert "model" not in body
    cfg = onboarding._load_yaml_config(fake_config_path)
    assert cfg["providers"]["ollama"]["base_url"] == "http://127.0.0.1:11434/v1"
    assert cfg["model"] == {**original_model_cfg, "custom_flag": "preserve-me"}
    assert calls == ["invalidate"]


def test_apply_self_hosted_provider_setup_omits_env_write_for_empty_optional_key(
    isolated_self_hosted_env,
    monkeypatch,
):
    tmp_path, _fake_config_path = isolated_self_hosted_env
    calls = []
    monkeypatch.setattr(onboarding, "invalidate_models_cache", lambda: calls.append("invalidate"))
    body = onboarding.apply_self_hosted_provider_setup({
        "provider": "lmstudio",
        "model": "local-model",
        "base_url": "http://127.0.0.1:1234/v1",
        "api_key": "",
    })
    assert body["ok"] is True
    assert not (tmp_path / ".env").exists()
    assert calls == ["invalidate"]


def test_apply_self_hosted_provider_setup_writes_only_target_provider_key_and_skips_reload_config(
    isolated_self_hosted_env,
    monkeypatch,
):
    tmp_path, _fake_config_path = isolated_self_hosted_env
    invalidate_calls = []
    reload_calls = []
    monkeypatch.setattr(onboarding, "invalidate_models_cache", lambda: invalidate_calls.append("invalidate"))
    monkeypatch.setattr(onboarding, "reload_config", lambda: reload_calls.append("reload"))
    body = onboarding.apply_self_hosted_provider_setup({
        "provider": "lmstudio",
        "model": "local-model",
        "base_url": "http://127.0.0.1:1234/v1",
        "api_key": "lm-key-12345678",
    })
    assert body["ok"] is True
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "LM_API_KEY=lm-key-12345678" in env_text
    assert "OLLAMA_API_KEY" not in env_text
    assert invalidate_calls == ["invalidate"]
    assert reload_calls == []


def test_post_self_hosted_provider_rejects_invalid_provider(isolated_self_hosted_env):
    body, status = _post("/api/providers/self-hosted", {
        "provider": "custom",
        "model": "x",
        "base_url": "http://127.0.0.1:11434/v1",
    })
    assert status == 400
    assert "unsupported self-hosted provider" in body.get("error", "")


def test_get_providers_exposes_self_hosted_flags_and_base_url(monkeypatch, tmp_path):
    _install_fake_hermes_cli(monkeypatch)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    config.cfg["providers"] = {
        "ollama": {"base_url": "http://127.0.0.1:11434/v1"},
        "lmstudio": {"base_url": "http://127.0.0.1:1234/v1"},
    }
    try:
        config._cfg_mtime = 0.0
        from api.providers import get_providers

        result = get_providers()
        by_id = {p["id"]: p for p in result["providers"]}
        assert by_id["ollama"]["is_self_hosted"] is True
        assert by_id["ollama"]["base_url"] == "http://127.0.0.1:11434/v1"
        assert by_id["ollama"]["configurable"] is False
        assert by_id["lmstudio"]["is_self_hosted"] is True
        assert by_id["lmstudio"]["base_url"] == "http://127.0.0.1:1234/v1"
        assert by_id["ollama-cloud"]["is_self_hosted"] is False
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime


def test_provider_card_uses_backend_projection_authority_matrix(isolated_self_hosted_env, tmp_path):
    if NODE is None:
        pytest.skip("node is required to execute the self-hosted provider card harness")

    fn_path = tmp_path / "buildProviderCard.js"
    fn_path.write_text(extract_function(PANELS_JS, "_buildProviderCard", prefix="function"), encoding="utf-8")
    driver_path = tmp_path / "card-driver.js"
    driver_path.write_text(_CARD_DRIVER, encoding="utf-8")
    _tmp_path, fake_config_path = isolated_self_hosted_env
    endpoint_a = "http://provider-a/v1"
    endpoint_b = "http://provider-b/v1"
    saved_cfg = {
        "model": {
            "provider": "ollama",
            "default": "model-from-a",
            "base_url": endpoint_a,
        },
        "providers": {
            "ollama": {
                "base_url": endpoint_b,
                "api_key": "config-token",
                "models": ["model-from-b"],
            },
        },
    }
    onboarding._save_yaml_config(fake_config_path, saved_cfg)
    config.cfg.clear()
    config.cfg.update(saved_cfg)
    config._cfg_mtime = 0.0
    from api.providers import get_providers, invalidate_providers_cache

    invalidate_providers_cache()
    saved_row = next(p for p in get_providers()["providers"] if p["id"] == "ollama")
    assert saved_row["base_url"] == endpoint_b
    assert saved_row["key_source"] == "config_yaml"
    assert "model-from-a" not in [m["id"] for m in saved_row["models"]]
    assert "model-from-b" in [m["id"] for m in saved_row["models"]]
    def render_provider(row):
        result = subprocess.run(
            [NODE, str(driver_path), json.dumps({"provider": row}), str(fn_path)],
            capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)

    payload = render_provider(saved_row)
    assert "Configured" in payload["headerText"]
    assert "provider-card-badge" in payload["headerHtml"]
    assert payload["baseUrlValue"] == endpoint_b
    assert "model-from-b" in payload["modelChoices"]
    assert "model-from-a" not in payload["modelChoices"]
    assert payload["modelValue"] == ""

    keyless_cfg = {
        "model": {
            "provider": "openai",
            "default": "active-model",
            "base_url": "http://active/v1",
        },
        "providers": {
            "ollama": {"base_url": endpoint_b, "models": ["model-from-b"]},
        },
    }
    onboarding._save_yaml_config(fake_config_path, keyless_cfg)
    config.cfg.clear()
    config.cfg.update(keyless_cfg)
    config._cfg_mtime = 0.0
    invalidate_providers_cache()
    keyless_row = next(p for p in get_providers()["providers"] if p["id"] == "ollama")
    keyless_payload = render_provider(keyless_row)
    assert keyless_row["base_url"] == endpoint_b
    assert keyless_row["has_key"] is False
    assert "Configured" in keyless_payload["headerText"]
    assert "provider-card-badge" in keyless_payload["headerHtml"]

    missing_cfg = {
        "model": {
            "provider": "openai",
            "default": "active-model",
            "base_url": "http://active/v1",
        },
        "providers": {"ollama": {}},
    }
    onboarding._save_yaml_config(fake_config_path, missing_cfg)
    config.cfg.clear()
    config.cfg.update(missing_cfg)
    config._cfg_mtime = 0.0
    invalidate_providers_cache()
    missing_row = next(p for p in get_providers()["providers"] if p["id"] == "ollama")
    missing_payload = render_provider(missing_row)
    assert missing_row.get("base_url") in (None, "")
    assert "Not configured" in missing_payload["headerText"]
    assert "provider-card-badge" not in missing_payload["headerHtml"]


def test_saved_provider_payload_has_no_singular_model_authority(monkeypatch, tmp_path):
    _install_fake_hermes_cli(monkeypatch)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {
        "provider": "openai", "default": "active-model", "base_url": "http://active/v1",
    }
    config.cfg["providers"] = {
        "ollama": {"base_url": "http://127.0.0.1:11434/v1", "models": ["catalog-only"]},
    }
    try:
        config._cfg_mtime = 0.0
        from api.providers import get_providers, invalidate_providers_cache

        invalidate_providers_cache()
        row = next(p for p in get_providers()["providers"] if p["id"] == "ollama")
        assert row["is_self_hosted"] is True
        assert row["base_url"] == "http://127.0.0.1:11434/v1"
        assert "configured" not in row
        assert "configured_model" not in row
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime


def test_configured_status_key_is_present_once_in_all_locale_blocks():
    i18n = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    locale_blocks = re.findall(
        r"(?ms)^  (?P<locale>'[^']+'|[A-Za-z-]+): \{(?P<body>.*?)(?=^  (?:'[^']+'|[A-Za-z-]+): \{|\Z)",
        i18n,
    )
    blocks = [body for _, body in locale_blocks]
    assert len(blocks) == 15
    assert all(block.count("providers_status_configured_label:") == 1 for block in blocks)
    assert all(block.count("providers_status_configured:") == 1 for block in blocks)
    labels = {
        locale.strip("'"): re.search(r"providers_status_configured_label: '([^']+)'", block).group(1)
        for locale, block in locale_blocks
    }
    assert labels == {
        "en": "Configured",
        "it": "Configurato",
        "ja": "設定済み",
        "ru": "Настроено",
        "es": "Configurado",
        "de": "Konfiguriert",
        "zh": "已配置",
        "zh-Hant": "已設定",
        "pt": "Configurada",
        "ko": "구성됨",
        "fr": "Configuré",
        "cs": "Nakonfigurováno",
        "tr": "Yapılandırıldı",
        "pl": "Skonfigurowano",
        "vi": "Đã cấu hình",
    }


def test_save_self_hosted_provider_posts_expected_payload(tmp_path):
    if NODE is None:
        pytest.skip("node is required to execute the self-hosted provider harness")

    fn_path = tmp_path / "saveSelfHostedProvider.js"
    fn_path.write_text(
        extract_function(PANELS_JS, "_saveSelfHostedProvider", prefix="async function"),
        encoding="utf-8",
    )
    driver_path = tmp_path / "driver.js"
    driver_path.write_text(_DRIVER, encoding="utf-8")
    scenario = {
        "providerId": "ollama",
        "baseUrl": "http://127.0.0.1:11434/v1",
        "model": "qwen3:8b",
        "apiKey": "",
    }
    result = subprocess.run(
        [NODE, str(driver_path), json.dumps(scenario), str(fn_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["calls"] == [{
        "url": "/api/providers/self-hosted",
        "method": "POST",
        "body": {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3:8b",
        },
    }]
    assert payload["refreshCount"] == 1
    assert payload["reloadCount"] == 1
    assert payload["apiKeyAfter"] == ""


def test_self_hosted_provider_card_keeps_remove_key_path():
    card_src = extract_function(PANELS_JS, "_buildProviderCard", prefix="function")
    assert "if(p.has_key){" in card_src
    assert "removeBtn.onclick=()=>_removeProviderKey(p.id);" in card_src
    assert "removeBtn.textContent=t('providers_remove');" in card_src


def test_probe_self_hosted_provider_populates_model_and_enables_save(tmp_path):
    if NODE is None:
        pytest.skip("node is required to execute the self-hosted provider harness")

    fn_path = tmp_path / "testSelfHostedConnection.js"
    fn_path.write_text(
        extract_function(PANELS_JS, "_testSelfHostedConnection", prefix="async function"),
        encoding="utf-8",
    )
    driver_path = tmp_path / "probe-driver.js"
    driver_path.write_text(_PROBE_DRIVER, encoding="utf-8")
    scenario = {
        "providerId": "ollama",
        "baseUrl": "http://127.0.0.1:11434/v1",
        "apiKey": "",
        "model": "",
        "discoveredModel": "qwen3:8b",
    }
    result = subprocess.run(
        [NODE, str(driver_path), json.dumps(scenario), str(fn_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["calls"] == [{
        "url": "/api/onboarding/probe",
        "method": "POST",
        "body": {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
        },
    }]
    assert payload["modelValue"] == "qwen3:8b"
    assert payload["saveDisabled"] is False
    assert payload["probeText"] == "Connected. 1 model(s) available."
    assert payload["testDisabled"] is False


def test_probe_self_hosted_provider_accepts_string_models(tmp_path):
    if NODE is None:
        pytest.skip("node is required to execute the self-hosted provider harness")

    fn_path = tmp_path / "testSelfHostedConnection.js"
    fn_path.write_text(
        extract_function(PANELS_JS, "_testSelfHostedConnection", prefix="async function"),
        encoding="utf-8",
    )
    driver_path = tmp_path / "probe-driver-string.js"
    driver_path.write_text(_PROBE_DRIVER, encoding="utf-8")
    scenario = {
        "providerId": "ollama",
        "baseUrl": "http://127.0.0.1:11434/v1",
        "apiKey": "",
        "model": "",
        "discoveredModels": ["qwen3:8b"],
    }
    result = subprocess.run(
        [NODE, str(driver_path), json.dumps(scenario), str(fn_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["modelValue"] == "qwen3:8b"
    assert payload["saveDisabled"] is False
