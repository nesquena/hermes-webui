"""#5311 follow-up: OpenCode Go uses the live Hermes CLI catalog again.

Background: #5611 made WebUI skip the live ``/v1/models`` probe for OpenCode Go
because the probe returned public-catalog models not enabled on the Go tier.
That premise no longer holds: Hermes core v0.20.5 (commit fcbd1076, tag
v2026.8.19) ships a Go-specific provider profile that probes
``https://opencode.ai/zen/go/v1/models`` directly and merges results
live-first with its curated Go list (hermes_cli/models.py, #49129). With the
correct Go-tier endpoint in core, WebUI should delegate like every other
provider (#1240): live catalog first, the curated static ``_PROVIDER_MODELS``
list as offline fallback only.
"""

from __future__ import annotations

import sys
import types

import api.config as config
import api.profiles as profiles


_PROVIDER_ENV_VARS = (
    "OPENCODE_ZEN_API_KEY",
    "OPENCODE_GO_API_KEY",
    "OPENCODE_API_KEY",
)


def _scrub_provider_env(monkeypatch):
    for name in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _install_fake_hermes_cli(monkeypatch, *, provider_id: str, live_ids, raise_on_lookup: bool = False):
    """Install a hermes_cli stub that reports one authenticated provider."""
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: [
        {"id": provider_id, "authenticated": True}
    ]

    calls: list[str] = []

    def provider_model_ids(pid):
        calls.append(pid)
        if raise_on_lookup:
            raise RuntimeError("simulated provider_model_ids failure")
        return list(live_ids) if pid == provider_id else []

    fake_models.provider_model_ids = provider_model_ids

    fake_auth = types.ModuleType("hermes_cli.auth")

    def get_auth_status(pid):
        if pid == provider_id:
            return {"logged_in": True, "key_source": ""}
        return {"logged_in": False, "key_source": ""}

    fake_auth.get_auth_status = get_auth_status

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)
    config.invalidate_models_cache()
    return calls


def _configure(monkeypatch, tmp_path, *, provider: str, providers: dict | None = None):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "missing-config.yaml")
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(
        config,
        "cfg",
        {
            "model": {"provider": provider, "default": ""},
            "providers": providers if providers is not None else {},
            "fallback_providers": [],
        },
    )
    monkeypatch.setattr(config, "_cfg_mtime", 0.0)
    monkeypatch.setattr(config, "_cfg_path", config._get_config_path(), raising=False)
    config.invalidate_models_cache()


def _provider_group(result: dict, provider_id: str) -> dict:
    return next(g for g in result["groups"] if g.get("provider_id") == provider_id)


def _ids(group: dict) -> list[str]:
    return [m.get("id") for m in group.get("models", [])]


def test_opencode_go_probes_live_catalog(monkeypatch, tmp_path):
    """The Go picker group must come from the live Hermes CLI catalog.

    ``sentinel-go-model`` is intentionally absent from every static list. If
    WebUI special-cases OpenCode Go away from the live probe again (the #5611
    stopgap), the sentinel disappears and this test fails.
    """
    _scrub_provider_env(monkeypatch)
    calls = _install_fake_hermes_cli(
        monkeypatch,
        provider_id="opencode-go",
        live_ids=["sentinel-go-model", "kimi-k3"],
    )
    _configure(monkeypatch, tmp_path, provider="opencode-go")

    result = config.get_available_models()
    group = _provider_group(result, "opencode-go")

    assert calls == ["opencode-go"]
    ids = _ids(group)
    assert "sentinel-go-model" in ids
    assert "kimi-k3" in ids
    assert all(m.get("label") for m in group["models"])


def test_opencode_go_static_fallback_when_probe_fails(monkeypatch, tmp_path):
    """Offline / CLI failure must fall back to the curated static list."""
    _scrub_provider_env(monkeypatch)
    calls = _install_fake_hermes_cli(
        monkeypatch,
        provider_id="opencode-go",
        live_ids=[],
        raise_on_lookup=True,
    )
    _configure(monkeypatch, tmp_path, provider="opencode-go")

    result = config.get_available_models()
    group = _provider_group(result, "opencode-go")

    assert calls == ["opencode-go"]
    ids = _ids(group)
    assert "sentinel-go-model" not in ids
    assert "glm-5" in ids
    assert "kimi-k2.5" in ids


def test_opencode_go_config_allowlist_still_wins(monkeypatch, tmp_path):
    """An explicit ``providers.opencode-go.models`` allowlist remains the
    local source of truth (#644) and is not bypassed by the live probe."""
    _scrub_provider_env(monkeypatch)
    calls = _install_fake_hermes_cli(
        monkeypatch,
        provider_id="opencode-go",
        live_ids=["sentinel-go-model"],
    )
    _configure(
        monkeypatch,
        tmp_path,
        provider="opencode-go",
        providers={"opencode-go": {"models": ["glm-5"]}},
    )

    result = config.get_available_models()
    group = _provider_group(result, "opencode-go")

    assert calls == []  # probe never consulted
    assert _ids(group) == ["glm-5"]


# ── Static fallback list contract ─────────────────────────────────────
# The remaining tests pin the offline fallback list itself. It is an exact
# mirror of Hermes core's curated ``opencode-go`` list
# (hermes_cli/models.py) — core owns the sync duty against the live
# https://opencode.ai/zen/go/v1/models endpoint and
# https://opencode.ai/docs/go/, and WebUI only mirrors. This catches drift
# in either direction: ids core added (kimi-k3, gpt-5.6-luna, glm-5.3,
# grok-4.5, qwen3.8-max, hy3, hy3-preview, muse-spark-1.2-contributor,
# ox-alpha-free) and ids core dropped.

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "api" / "config.py"
CONFIG = CONFIG_PATH.read_text(encoding="utf-8")

EXPECTED_OPENCODE_GO_MODEL_IDS = [
    "kimi-k3",
    "kimi-k2.7-code",
    "kimi-k2.6",
    "kimi-k2.5",
    "gpt-5.6-luna",
    "grok-4.5",
    "glm-5.3",
    "glm-5.2",
    "glm-5.1",
    "glm-5",
    "mimo-v2.5-pro",
    "mimo-v2.5",
    "mimo-v2-pro",
    "mimo-v2-omni",
    "minimax-m3",
    "minimax-m2.7",
    "minimax-m2.5",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "qwen3.8-max",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.6-plus",
    "qwen3.5-plus",
    "hy3",
    "hy3-preview",
    "muse-spark-1.2-contributor",
    "ox-alpha-free",
]


def _opencode_go_static_models():
    tree = ast.parse(CONFIG, filename=str(CONFIG_PATH))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "_PROVIDER_MODELS" for target in node.targets):
            continue
        provider_models = ast.literal_eval(node.value)
        return provider_models["opencode-go"]
    raise AssertionError("_PROVIDER_MODELS assignment not found")


def test_opencode_go_static_models_match_core_curated_list():
    models = _opencode_go_static_models()
    assert [model["id"] for model in models] == EXPECTED_OPENCODE_GO_MODEL_IDS


def test_opencode_go_recent_additions_have_human_labels():
    labels = {model["id"]: model["label"] for model in _opencode_go_static_models()}
    assert labels["gpt-5.6-luna"] == "GPT 5.6 Luna"
    assert labels["kimi-k3"] == "Kimi K3"
    assert labels["glm-5.3"] == "GLM-5.3"
    assert labels["grok-4.5"] == "Grok 4.5"
    assert labels["qwen3.8-max"] == "Qwen3.8 Max"
    assert labels["muse-spark-1.2-contributor"] == "Muse Spark 1.2 Contributor"
    assert labels["ox-alpha-free"] == "Ox Alpha (Go)"
