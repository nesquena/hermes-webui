"""Regression test for #6335 / #6338 catalog admission gate.

Issue #6335: after removing a provider's API key, a metadata-only
``providers.<name>: {name: ...}`` entry in config.yaml still rendered the
provider in the chat model selector.  The fix gates known-provider admission
on credential evidence: a provider listed in config.yaml is only admitted
when it was *already* detected from a credential source (env vars, hermes
auth, credential pool) — or carries an explicit route (``api`` /
``base_url`` / ``api_key`` / ``key_env``), or is the active provider with a
configured models allowlist.

Follow-up #6338: Hermes reports detected credentials under canonical alias
names (``xai``, ``gemini``) while WebUI preserves config.yaml keys as
``x-ai``, ``google``.  The credential check must compare in an
alias-normalised identity space or authenticated providers disappear from
the selector.

This test drives ``api/config.py::get_available_models()`` end-to-end with
a stubbed ``hermes_cli`` (deterministic, offline) and asserts the
catalog-builder admission behaviour:

1. Mocked Hermes provider detection reports authenticated ``xai`` and
   ``gemini`` (the agent's canonical alias names).
2. ``providers.x-ai`` and ``providers.google`` are metadata-only entries
   (name only — no route, no models allowlist).
3. The active provider is a *different* provider (``openai-codex``), so the
   test cannot pass through the unconditional active-provider path.
4. The xAI and Google groups remain admitted (alias-normalised credential
   match).
5. A metadata-only known provider with no matching credential, route field,
   or configured model list (``openai-api``) stays absent.
"""

from __future__ import annotations

import json
import sys
import types

import api.config as config
import api.profiles as profiles


def _install_fake_hermes_cli(monkeypatch):
    """Stub hermes_cli so detection is deterministic and offline.

    ``list_available_providers()`` reports authenticated ``xai`` and
    ``gemini`` — the agent's canonical alias names — so the ONLY way the
    metadata-only ``providers.x-ai`` / ``providers.google`` entries can be
    admitted is via the alias-normalised ``_already_credentialed`` gate
    under test.
    """
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: [
        {"id": "xai", "authenticated": True},
        {"id": "gemini", "authenticated": True},
    ]
    fake_models.provider_model_ids = lambda pid: []
    fake_models._PROVIDER_ALIASES = {
        "x-ai": "xai",
        "x.ai": "xai",
        "grok": "xai",
        "google": "gemini",
        "google-gemini": "gemini",
        "google-ai-studio": "gemini",
    }

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)

    # Remove the real agent.credential_pool so no pool evidence leaks in
    # from the host environment.
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)


def _call_get_available_models(monkeypatch, tmp_path):
    _install_fake_hermes_cli(monkeypatch)

    # Active provider is a DIFFERENT provider (openai-codex), so the
    # x-ai/google entries can only be admitted through the credential gate,
    # never through the active-provider path.
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {},
                "active_provider": "openai-codex",
                "credential_pool": {},
            }
        ),
        encoding="utf-8",
    )
    # config.yaml must exist on disk: get_available_models() reloads the
    # config from disk when the path/mtime changed, wiping in-memory cfg
    # overrides.  Metadata-only entries only (name) — no api/base_url/
    # api_key/key_env route fields, no models allowlist.
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: openai-codex\nproviders:\n"
        "  x-ai:\n    name: xAI\n"
        "  google:\n    name: Google\n"
        "  openai-api:\n    name: OpenAI API\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    for var in (
        "OPENAI_API_KEY",
        "HERMES_API_KEY",
        "HERMES_OPENAI_API_KEY",
        "LOCAL_API_KEY",
        "OPENROUTER_API_KEY",
        "API_KEY",
        "XAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {"provider": "openai-codex"}
    config.cfg["providers"] = {
        "x-ai": {"name": "xAI"},
        "google": {"name": "Google"},
        "openai-api": {"name": "OpenAI API"},
    }
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    config.invalidate_models_cache()
    try:
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config.invalidate_models_cache()


def _group_ids(result):
    return [g.get("provider_id") for g in result.get("groups", [])]


def _group_names(result):
    return [g.get("provider") for g in result.get("groups", [])]


def test_credentialed_alias_config_providers_stay_admitted(monkeypatch, tmp_path):
    """Authenticated xai/gemini keep metadata-only x-ai/google entries in the picker.

    Covers review points 1-4: detection reports authenticated ``xai`` /
    ``gemini`` (alias names), config has metadata-only ``x-ai`` / ``google``
    entries, the active provider is ``openai-codex`` (a different provider),
    and the xAI / Google groups remain admitted through the alias-normalised
    credential check.
    """
    result = _call_get_available_models(monkeypatch, tmp_path)
    ids = _group_ids(result)

    # Point 3 sanity: the active provider is a different provider, so these
    # groups cannot be explained by the unconditional active-provider path.
    assert "openai-codex" in ids, f"active provider group missing; got {ids}"

    # Point 4: the alias-matched credential evidence keeps both groups.
    assert "x-ai" in ids, f"authenticated xai must admit configured x-ai group; got {ids}"
    assert "google" in ids, f"authenticated gemini must admit configured google group; got {ids}"
    assert "xAI" in _group_names(result)
    assert "Google" in _group_names(result)


def test_keyless_metadata_only_known_provider_stays_absent(monkeypatch, tmp_path):
    """A metadata-only known provider with no credential/route/models is dropped.

    Review point 5: ``providers.openai-api: {name: ...}`` is a *known*
    provider (in ``_PROVIDER_MODELS`` / ``_PROVIDER_DISPLAY``) but carries no
    credential evidence, no route field, and no configured model list — the
    #6335 bug.  It must not render in the model selector.
    """
    result = _call_get_available_models(monkeypatch, tmp_path)
    ids = _group_ids(result)
    assert "openai-api" not in ids, (
        f"keyless metadata-only openai-api must stay absent; got {ids}"
    )
