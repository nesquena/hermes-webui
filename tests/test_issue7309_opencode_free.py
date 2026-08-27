"""Behavioral coverage for the built-in OpenCode Free provider."""

import copy
import json
import queue
import shutil
import sys
import subprocess
import types
from pathlib import Path
from unittest import mock

import api.config as config
import api.oauth
import api.streaming as streaming
import pytest
from tests.js_source_extract import extract_function


ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


_CARD_DRIVER = r'''
const fs = require('fs');
const fnSource = fs.readFileSync(process.argv[2], 'utf8');
function element(tag) {
  return {
    tagName: tag.toUpperCase(), children: [], dataset: {}, style: {},
    classList: { values: new Set(), toggle(name) { this.values.has(name) ? this.values.delete(name) : this.values.add(name); } },
    appendChild(child) { this.children.push(child); return child; },
    addEventListener() {}, setAttribute() {},
    set innerHTML(value) { this._innerHTML = value; },
    get innerHTML() { return this._innerHTML || ''; }, textContent: '',
  };
}
globalThis.document = { createElement: element };
globalThis.t = (key) => ({
  providers_status_keyless: 'Keyless',
  providers_keyless_hint: 'Ready to use, no API key required.',
}[key] || key);
globalThis.esc = (value) => String(value ?? '');
eval(fnSource);
const card = _buildProviderCard({
  id: 'opencode-free', display_name: 'OpenCode Free', models_total: 6,
  is_keyless: true, has_key: false, key_source: 'keyless',
});
function text(node) { return [node.textContent || '', node.innerHTML || '', ...(node.children || []).map(text)].join(' '); }
function countInputs(node) { return (node.tagName === 'INPUT' ? 1 : 0) + (node.children || []).reduce((n, child) => n + countInputs(child), 0); }
process.stdout.write(JSON.stringify({text: text(card), inputs: countInputs(card), childCount: card.children.length}));
'''


def test_keyless_capability_uses_canonical_alias(monkeypatch):
    fake = types.ModuleType("hermes_cli.providers")
    fake.HERMES_OVERLAYS = {
        "opencode-free": types.SimpleNamespace(keyless=True),
        "opencode-zen": types.SimpleNamespace(keyless=False),
    }
    monkeypatch.setitem(sys.modules, "hermes_cli.providers", fake)
    assert config.provider_is_keyless("free") is True
    assert config.provider_is_keyless("opencode-zen") is False
    assert config.provider_is_keyless("vendor-free") is False
    assert config.provider_is_keyless(" OpenCode_FREE ") is True
    assert config.provider_is_keyless("OPENCODE") is False


def test_fallback_catalog_contains_only_curated_free_models(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli.models", None)
    original = copy.deepcopy(config.cfg)
    try:
        config.cfg.clear()
        config.cfg.update({"model": {}})
        result = config._static_models_catalog_without_live_probes()
        group = next(g for g in result["groups"] if g["provider_id"] == "opencode-free")
        assert [m["id"] for m in group["models"]] == [
            m["id"] for m in config._PROVIDER_MODELS["opencode-free"]
        ]
    finally:
        config.cfg.clear()
        config.cfg.update(original)


def test_only_configured_keeps_keyless_free_and_paid_free_suffix_is_separate(monkeypatch):
    import api.providers as providers

    monkeypatch.setattr(providers, "_provider_has_key", lambda pid: False)
    original = copy.deepcopy(config.cfg)
    try:
        config.cfg.clear()
        config.cfg.update({"model": {}, "providers": {"only_configured": True}})
        result = config._static_models_catalog_without_live_probes()
        ids = {g["provider_id"] for g in result["groups"]}
        assert "opencode-free" in ids
        go = next(g for g in result["groups"] if g["provider_id"] == "opencode-go") if "opencode-go" in ids else None
        assert go is None or all("free" not in m["id"] for m in go["models"])
    finally:
        config.cfg.clear()
        config.cfg.update(original)


def test_gateway_payload_contract_is_canonical_and_credential_free():
    assert config._resolve_provider_alias("free") == "opencode-free"
    assert config.provider_is_keyless("opencode-free") is True


def test_opencode_free_preserves_namespaced_model_routing():
    original = copy.deepcopy(config.cfg)
    try:
        config.cfg.clear()
        config.cfg.update({"model": {"provider": "OPENCODE_FREE"}})
        model, provider, base_url = config.resolve_model_provider("opencode-free/x-preview-f-free")
        assert model == "opencode-free/x-preview-f-free"
        assert provider == "opencode-free"
        assert base_url is None
    finally:
        config.cfg.clear()
        config.cfg.update(original)


@pytest.mark.parametrize("model_id", [
    "opencode-free/arbitrary-free-model",
    "@opencode-free:arbitrary-free-model",
])
def test_opencode_free_rejects_non_curated_models(model_id):
    with pytest.raises(ValueError, match="six curated free models"):
        config.resolve_model_provider(model_id)


def test_configured_free_provider_rejects_non_curated_namespaced_model(monkeypatch):
    original = copy.deepcopy(config.cfg)
    try:
        config.cfg.clear()
        config.cfg.update({
            "model": {
                "provider": "opencode-free",
                "default": "x-preview-f-free",
            },
        })
        with pytest.raises(ValueError, match="six curated free models"):
            config.resolve_model_provider("opencode-free/arbitrary-free-model")
    finally:
        config.cfg.clear()
        config.cfg.update(original)


def test_provider_hints_and_qualified_ids_are_canonicalized():
    assert config._parse_provider_qualified_model_id("@FREE:x-preview-f-free") == (
        "x-preview-f-free", "opencode-free"
    )
    model, provider, base_url = config.resolve_model_provider(
        "OPENCODE_FREE/x-preview-f-free"
    )
    assert (model, provider, base_url) == (
        "opencode-free/x-preview-f-free", "opencode-free", None
    )


def test_free_catalog_rejects_configured_and_live_extras(monkeypatch):
    original = copy.deepcopy(config.cfg)
    try:
        config.cfg.clear()
        config.cfg.update({
            "model": {},
            "providers": {"FREE": {"models": ["paid-model"]}},
        })
        monkeypatch.setattr(
            config, "_read_live_provider_model_ids",
            lambda pid: ["paid-model", "x-preview-f-free"]
            if pid == "opencode-free" else [],
        )
        result = config._static_models_catalog_without_live_probes()
        group = next(g for g in result["groups"] if g["provider_id"] == "opencode-free")
        assert {m["id"] for m in group["models"]} == {
            m["id"] for m in config._PROVIDER_MODELS["opencode-free"]
        }
    finally:
        config.cfg.clear()
        config.cfg.update(original)


def test_active_free_default_cannot_escape_curated_catalogs(monkeypatch):
    original = copy.deepcopy(config.cfg)
    try:
        config.cfg.clear()
        config.cfg.update({
            "model": {"provider": "opencode-free", "default": "arbitrary-free-model"},
            "providers": {"opencode-free": {"models": ["arbitrary-free-model"]}},
        })
        static = config._static_models_catalog_without_live_probes()
        static_free = next(g for g in static["groups"] if g["provider_id"] == "opencode-free")
        assert [m["id"] for m in static_free["models"]] == [
            m["id"] for m in config._PROVIDER_MODELS["opencode-free"]
        ]
        assert all("arbitrary-free-model" not in str(g) for g in static["groups"])
    finally:
        config.cfg.clear()
        config.cfg.update(original)


def test_set_default_validates_provider_override_before_persisting(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "model:\n  provider: openai\n  default: gpt-5.4\n", encoding="utf-8"
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)
    monkeypatch.setattr(
        config, "resolve_model_provider", lambda model: (model, "openai", None)
    )

    with pytest.raises(ValueError, match="six curated free models"):
        config.set_hermes_default_model(
            "arbitrary-free-model", provider="opencode-free"
        )
    assert "arbitrary-free-model" not in config_path.read_text(encoding="utf-8")


def test_minimal_and_stale_fallbacks_scrub_invalid_free_defaults(monkeypatch, tmp_path):
    original = copy.deepcopy(config.cfg)
    try:
        config.cfg.clear()
        config.cfg.update({
            "model": {"provider": "opencode-free", "default": "arbitrary-free-model"},
        })
        minimal = config._minimal_static_models_catalog()
        assert minimal["default_model"] == ""
        assert minimal["groups"] == []

        cache_path = tmp_path / "models.json"
        cache_path.write_text(json.dumps({
            "_schema_version": config._MODELS_CACHE_SCHEMA_VERSION,
            "active_provider": "opencode-free",
            "default_model": "arbitrary-free-model",
            "configured_model_badges": {},
            "groups": [
                {"provider": "Default", "provider_id": "opencode-free", "models": [
                    {"id": "arbitrary-free-model", "label": "Default"},
                ], "extra_models": [
                    {"id": "paid-free-model", "label": "Paid"},
                    {"id": "x-preview-f-free", "label": "Curated"},
                ]},
                {"provider": "OpenCode Free", "provider_id": "opencode-free", "models": [
                    {"id": "arbitrary-free-model", "label": "Invalid"},
                ], "extra_models": [
                    {"id": "another-invalid", "label": "Invalid"},
                ]},
            ],
        }), encoding="utf-8")
        monkeypatch.setattr(config, "_get_models_cache_path", lambda: cache_path)
        stale = config._load_stale_models_cache_from_disk()
        assert stale["default_model"] == ""
        assert all("arbitrary-free-model" not in str(group) for group in stale["groups"])
        assert all("paid-free-model" not in str(group) for group in stale["groups"])
        assert any("x-preview-f-free" in str(group) for group in stale["groups"])

        monkeypatch.setattr(config, "_is_loadable_disk_cache", lambda _cache: True)
        loaded = config._load_models_cache_from_disk()
        assert loaded["default_model"] == ""
        assert all("arbitrary-free-model" not in str(group) for group in loaded["groups"])
        assert all("paid-free-model" not in str(group) for group in loaded["groups"])
        assert any("x-preview-f-free" in str(group) for group in loaded["groups"])
    finally:
        config.cfg.clear()
        config.cfg.update(original)


def test_onboarding_rejects_non_curated_free_model(monkeypatch, tmp_path):
    import api.onboarding as onboarding

    monkeypatch.setenv("HERMES_WEBUI_SKIP_ONBOARDING", "0")
    monkeypatch.setattr(onboarding, "_get_config_path", lambda: tmp_path / "config.yaml")
    with pytest.raises(ValueError, match="six curated free models"):
        onboarding.apply_onboarding_setup({
            "provider": "opencode-free",
            "model": "arbitrary-free-model",
            "confirm_overwrite": True,
        })


def test_onboarding_switch_scrubs_paid_model_credentials(monkeypatch, tmp_path):
    import api.onboarding as onboarding

    saved = {}
    cfg = {
        "model": {
            "provider": "openai",
            "default": "gpt-4o",
            "api_key": "secret",
            "key_env": "OPENAI_API_KEY",
            "access_token": "token",
            "base_url": "https://api.openai.com/v1",
        }
    }
    monkeypatch.setenv("HERMES_WEBUI_SKIP_ONBOARDING", "0")
    monkeypatch.setattr(onboarding, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(onboarding, "_get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(onboarding, "_load_yaml_config", lambda _path: copy.deepcopy(cfg))
    monkeypatch.setattr(onboarding, "_save_yaml_config", lambda _path, value: saved.update(value))
    monkeypatch.setattr(onboarding, "get_onboarding_status", lambda: {"ok": True})
    monkeypatch.setattr(onboarding, "reload_config", lambda: None)
    monkeypatch.setattr(onboarding, "invalidate_models_cache", lambda: None)
    monkeypatch.setattr(onboarding, "verify_hermes_imports", lambda: (True, [], []))

    result = onboarding.apply_onboarding_setup({
        "provider": "OPENCODE_FREE",
        "model": "x-preview-f-free",
        "confirm_overwrite": True,
    })
    assert result == {"ok": True}
    assert not {"api_key", "key_env", "access_token"} & saved["model"].keys()
    assert saved["model"]["provider"] == "opencode-free"


def test_gateway_and_streaming_use_canonical_free_route():
    from api.gateway_chat import _gateway_model_field

    assert _gateway_model_field("@FREE:x-preview-f-free") == "x-preview-f-free"
    assert config.resolve_model_provider("@OpEnCoDe_FrEe:x-preview-f-free")[1] == (
        "opencode-free"
    )


def test_provider_settings_canonicalize_duplicate_config_keys(monkeypatch):
    import api.providers as providers

    monkeypatch.setattr(providers, "get_config", lambda: {
        "providers": {
            "OPENCODE_FREE": {},
            "opencode_free": {},
        },
    })
    monkeypatch.setattr(providers, "_get_cached_providers", lambda _key: None)
    monkeypatch.setattr(providers, "plugin_model_provider_ids", lambda: [])
    monkeypatch.setattr(providers, "_provider_has_key", lambda _pid: False)
    rows = providers.get_providers()["providers"]
    assert [row["id"] for row in rows].count("opencode-free") == 1


def test_provider_settings_does_not_append_configured_free_models(monkeypatch):
    import api.providers as providers

    monkeypatch.setattr(providers, "get_config", lambda: {
        "providers": {"opencode-free": {"models": ["arbitrary-free-model"]}},
    })
    monkeypatch.setattr(providers, "_get_cached_providers", lambda _key: None)
    monkeypatch.setattr(providers, "_provider_has_key", lambda _pid: False)
    monkeypatch.setattr(providers, "plugin_model_provider_ids", lambda: [])
    free = next(
        row for row in providers.get_providers()["providers"]
        if row["id"] == "opencode-free"
    )
    assert [model["id"] for model in free["models"]] == [
        model["id"] for model in config._PROVIDER_MODELS["opencode-free"]
    ]


def test_status_and_onboarding_catalog_declare_keyless_without_credentials(monkeypatch, tmp_path):
    import api.onboarding as onboarding
    import api.providers as providers

    monkeypatch.setattr(onboarding, "_get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(onboarding, "_provider_api_key_present", lambda *args: False)
    monkeypatch.setattr(onboarding, "verify_hermes_imports", lambda: (True, [], []))
    monkeypatch.setattr(onboarding, "_HERMES_FOUND", True)
    status = onboarding._status_from_runtime(
        {"model": {"provider": "opencode_free", "default": "x-preview-f-free"}},
        True,
    )
    assert status["provider_ready"] is True
    assert status["chat_ready"] is True
    invalid_status = onboarding._status_from_runtime(
        {"model": {"provider": "opencode-free", "default": "arbitrary-free-model"}},
        True,
    )
    assert invalid_status["provider_ready"] is False
    assert invalid_status["chat_ready"] is False

    monkeypatch.setattr(providers, "_provider_has_key", lambda _pid: False)
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
    free = next(row for row in providers.get_providers()["providers"] if row["id"] == "opencode-free")
    assert free["is_keyless"] is True
    assert free["key_source"] == "keyless"
    assert free["configurable"] is False

    catalog = onboarding._build_setup_catalog({"model": {}})
    setup = next(row for row in catalog["providers"] if row["id"] == "opencode-free")
    assert setup["is_keyless"] is True


def test_settings_provider_card_renders_keyless_row_without_credential_controls(tmp_path):
    if NODE is None:
        pytest.skip("node is required to execute the provider-card DOM harness")
    function_path = tmp_path / "provider-card.js"
    function_path.write_text(
        extract_function(PANELS_JS, "_buildProviderCard", prefix="function"),
        encoding="utf-8",
    )
    driver_path = tmp_path / "provider-card-driver.js"
    driver_path.write_text(_CARD_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver_path), str(function_path)],
        capture_output=True, text=True, check=True,
    )
    rendered = json.loads(result.stdout)
    assert "6 models" in rendered["text"]
    assert "Keyless" in rendered["text"]
    assert "Ready to use, no API key required." in rendered["text"]
    assert rendered["inputs"] == 0
    assert rendered["childCount"] == 2


def test_gateway_request_serialization_keeps_free_provider_and_omits_auth(monkeypatch):
    import api.gateway_chat as gateway

    captured = []

    def capture_request(request, **_kwargs):
        captured.append(request)
        raise RuntimeError("stop after request construction")

    monkeypatch.setattr(gateway.urllib.request, "urlopen", capture_request)
    with pytest.raises(RuntimeError, match="stop after request construction"):
        gateway._run_gateway_runs_api_streaming(
            "session-7309", "hello", "@opencode-free:x-preview-f-free", "/tmp",
            "stream-7309", "https://gateway.example", None, [], {"provider": "opencode-free"},
            put_gateway_event=lambda *_args, **_kwargs: None,
            cancel_event=__import__("threading").Event(),
        )

    request = captured[0]
    body = json.loads(request.data)
    assert body["provider"] == "opencode-free"
    assert body["model"] == "x-preview-f-free"
    assert request.get_header("Authorization") is None
    assert "dummy-key" not in request.headers.values()


def test_streaming_runtime_seam_passes_canonical_free_provider_without_key(monkeypatch):
    """The installed Agent lacks this provider, so lock the WebUI seam narrowly."""
    captured = {}

    class FakeSession:
        session_id = "session-7309-runtime"
        title = "OpenCode Free"
        workspace = "/tmp"
        model = "x-preview-f-free"
        messages = []
        personality = None
        input_tokens = output_tokens = 0
        estimated_cost = None
        tool_calls = []
        active_stream_id = "stream-7309-runtime"
        pending_user_message = pending_attachments = pending_started_at = None

        def save(self, touch_updated_at=True):
            pass

        def compact(self):
            return {"session_id": self.session_id, "title": self.title, "workspace": self.workspace}

    class CapturingAgent:
        def __init__(self, **kwargs):
            captured["init"] = kwargs
            self.context_compressor = None
            self.session_prompt_tokens = self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.reasoning_config = self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            captured["run"] = kwargs
            return {"messages": [{"role": "assistant", "content": "ok"}]}

        def interrupt(self, _message):
            pass

    fake_runtime = types.ModuleType("hermes_cli.runtime_provider")
    resolver = mock.Mock(return_value={
        "provider": "opencode-free", "base_url": "https://opencode.ai/zen", "api_key": None,
    })
    fake_runtime.resolve_runtime_provider = resolver
    fake_cli = types.ModuleType("hermes_cli")
    fake_cli.__path__ = []
    fake_cli.runtime_provider = fake_runtime
    fake_state = types.ModuleType("hermes_state")
    fake_state.SessionDB = mock.Mock(return_value=object())
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_state)
    monkeypatch.setattr(api.oauth, "resolve_runtime_provider_with_anthropic_env_lock", lambda fn, **kw: fn(**kw))
    monkeypatch.setattr(streaming, "get_session", lambda _sid: FakeSession())
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: CapturingAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *_a, **_kw: (
        "x-preview-f-free", "opencode-free", None,
    ))
    monkeypatch.setattr("api.config.get_config", lambda: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_a, **_kw: [])
    stream_id = "stream-7309-runtime"
    streaming.STREAMS[stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            FakeSession.session_id, "hello", "x-preview-f-free", "/tmp", stream_id,
        )
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)

    resolver.assert_called_once_with(requested="opencode-free", target_model="x-preview-f-free")
    assert captured["init"]["provider"] == "opencode-free"
    assert captured["init"]["api_key"] is None
    assert "Authorization" not in captured["init"]
