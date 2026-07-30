"""Regression tests for issue #4324.

The Photon iMessage plugin writes three keys into ``auth.json`` →
``credential_pool``: ``photon``, ``photon_project`` and ``photon_user``.
These are messaging-platform credentials, NOT model providers.

Regression from #4247: the credential-pool provider-detection loop in
``api/config.py::_build_available_models_uncached`` added *every* non-ambient
pool key to ``detected_providers`` without checking that the key actually
names a model provider.  Unknown ids (``photon``, ``photon-project``,
``photon-user``) then fell through to the generic ``else`` branch of the
group builder, which copied the global ``auto_detected_models`` catalog (the
reporter's 251 base-url-probed models) into each phantom group — so the model
picker showed three bogus "providers" each listing the full catalog.

The fix gates pool detection behind ``_is_known_model_provider()`` (both the
``load_pool`` and the raw-dict ``ImportError`` branches) and, defensively,
stops the ``else`` branch from painting an unknown id with the global catalog.

These tests mock at the WebUI boundary (stub ``hermes_cli`` + a fake
``load_pool`` + a fake ``urlopen`` for the base-url probe) and never import
the bundled ``agent`` package, which is not installed in CI.
"""

import json
import sys
import types

import api.config as config
import api.profiles as profiles


def _install_fake_hermes_cli(monkeypatch, *, with_load_pool: bool = False, pool_data: dict | None = None):
    """Stub hermes_cli so detection is deterministic and offline.

    list_available_providers() returns [] so the ONLY way a provider can be
    detected is via the credential_pool path under test (mirrors the
    test_credential_pool_providers.py helper).
    """
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda pid: []

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)

    # Remove the real agent.credential_pool. When with_load_pool is False this
    # forces the ImportError raw-dict fallback path; when True we install a
    # fake load_pool so the primary path is exercised.
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)

    if with_load_pool:
        _pool_data = pool_data or {}

        class _FakeEntry:
            def __init__(self, d):
                self.source = d.get("source", "manual")
                self.label = d.get("label", "")
                self.key_source = d.get("key_source", "")
                self.id = d.get("id", "")
                self.runtime_api_key = (d.get("runtime_api_key") or d.get("access_token") or "")
                self.access_token = (d.get("access_token") or "")
                self.base_url = d.get("base_url", "")

        class _FakePool:
            def __init__(self, entries_list):
                self._entries = entries_list

            def entries(self):
                return self._entries

            def select(self):
                return self._entries[0] if self._entries else None

        def _fake_load_pool(pid):
            raw = _pool_data.get(pid, [])
            return _FakePool([_FakeEntry(e) for e in raw])

        fake_cp = types.ModuleType("agent.credential_pool")
        fake_cp.load_pool = _fake_load_pool
        monkeypatch.setitem(sys.modules, "agent.credential_pool", fake_cp)


def _install_fake_base_url_probe(monkeypatch, model_ids):
    """Make the /v1/models base-url probe return *model_ids*.

    This is what populates the global ``auto_detected_models`` catalog — the
    list the phantom-provider bug copied into every unknown group.
    """
    payload = {"data": [{"id": mid, "name": mid} for mid in model_ids]}

    class _Resp:
        def read(self):
            return json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=10: _Resp())
    # Avoid the SSRF private-IP guard rejecting the probe host.
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [])


def _call_get_available_models(monkeypatch, tmp_path, auth_payload, *,
                               with_load_pool=False, base_url=None, probe_models=None):
    _install_fake_hermes_cli(
        monkeypatch,
        with_load_pool=with_load_pool,
        pool_data=auth_payload.get("credential_pool", {}),
    )
    if base_url is not None:
        _install_fake_base_url_probe(monkeypatch, probe_models or [])

    (tmp_path / "auth.json").write_text(json.dumps(auth_payload), encoding="utf-8")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    for var in ("OPENAI_API_KEY", "HERMES_API_KEY", "HERMES_OPENAI_API_KEY",
                "LOCAL_API_KEY", "OPENROUTER_API_KEY", "API_KEY"):
        monkeypatch.delenv(var, raising=False)

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    if base_url is not None:
        config.cfg["model"] = {"base_url": base_url, "api_key": "sk-test"}
    else:
        config.cfg["model"] = {}
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


def _group_names(result):
    return [g["provider"] for g in result.get("groups", [])]


_PHOTON_POOL = {
    "photon": [
        {"id": "ph1", "label": "photon-device", "source": "manual", "auth_type": "api_key"}
    ],
    "photon_project": [
        {"id": "ph2", "label": "photon-project", "source": "manual", "auth_type": "api_key"}
    ],
    "photon_user": [
        {"id": "ph3", "label": "photon-user", "source": "manual", "auth_type": "api_key"}
    ],
}


def _photon_auth_payload(extra_pool=None):
    pool = dict(_PHOTON_POOL)
    if extra_pool:
        pool.update(extra_pool)
    return {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": pool,
    }


# ── load_pool path (primary) ─────────────────────────────────────────────────


def test_photon_pool_keys_do_not_appear_load_pool(monkeypatch, tmp_path):
    """The three Photon pool keys must NOT render as provider groups."""
    result = _call_get_available_models(
        monkeypatch, tmp_path, _photon_auth_payload(), with_load_pool=True,
    )
    names = [n.lower() for n in _group_names(result)]
    assert not any("photon" in n for n in names), (
        f"Photon messaging credentials must not appear as model providers; got {_group_names(result)}"
    )


def test_photon_pool_keys_hidden_even_with_base_url_catalog(monkeypatch, tmp_path):
    """Reproduces the reporter's exact symptom: a base-url probe populates the
    global catalog, and pre-fix each photon* group was painted with all of it.

    Post-fix: no photon group exists at all, regardless of the catalog size.
    """
    catalog = [f"model-{i}" for i in range(40)]  # stand-in for the 251 models
    result = _call_get_available_models(
        monkeypatch, tmp_path, _photon_auth_payload(),
        with_load_pool=True, base_url="http://my-llm.local:11434/v1", probe_models=catalog,
    )
    groups = {g["provider"]: g["models"] for g in result.get("groups", [])}
    assert not any("photon" in n.lower() for n in groups), (
        f"No photon* group should exist; got {list(groups)}"
    )
    # And none of the catalog models leaked into a phantom photon group:
    for name, models in groups.items():
        if "photon" in name.lower():
            raise AssertionError(f"phantom photon group {name!r} carries {len(models)} models")


def test_real_provider_still_detected_alongside_photon(monkeypatch, tmp_path):
    """The #4247 intent is preserved: a real model provider whose key lives in
    the pool (copilot) is still detected even when photon* keys are present.
    """
    extra = {
        "copilot": [
            {"id": "cp1", "label": "explicit-copilot", "source": "manual",
             "auth_type": "api_key", "base_url": "https://api.githubcopilot.com"}
        ]
    }
    result = _call_get_available_models(
        monkeypatch, tmp_path, _photon_auth_payload(extra), with_load_pool=True,
    )
    names = _group_names(result)
    assert "GitHub Copilot" in names, f"Real pool provider must still appear; got {names}"
    assert not any("photon" in n.lower() for n in names), (
        f"Photon keys must still be filtered out; got {names}"
    )


# ── ImportError raw-dict fallback path ───────────────────────────────────────


def test_photon_pool_keys_do_not_appear_importerror_path(monkeypatch, tmp_path):
    """Same guard must hold on the raw-dict fallback (agent.credential_pool absent)."""
    result = _call_get_available_models(
        monkeypatch, tmp_path, _photon_auth_payload(), with_load_pool=False,
    )
    names = [n.lower() for n in _group_names(result)]
    assert not any("photon" in n for n in names), (
        f"Photon keys must be filtered on the ImportError path too; got {_group_names(result)}"
    )


def test_real_provider_still_detected_importerror_path(monkeypatch, tmp_path):
    extra = {
        "copilot": [
            {"id": "cp1", "label": "explicit-copilot", "source": "manual",
             "auth_type": "api_key", "base_url": "https://api.githubcopilot.com"}
        ]
    }
    result = _call_get_available_models(
        monkeypatch, tmp_path, _photon_auth_payload(extra), with_load_pool=False,
    )
    names = _group_names(result)
    assert "GitHub Copilot" in names, f"Real pool provider must still appear; got {names}"
    assert not any("photon" in n.lower() for n in names), (
        f"Photon keys must still be filtered out; got {names}"
    )


def test_aliased_pool_key_still_detected_alongside_photon(monkeypatch, tmp_path):
    """#4247 intent (aliased pool keys) is preserved end-to-end.

    A pool key of ``google`` resolves to canonical ``gemini`` (in
    _PROVIDER_MODELS, not _PROVIDER_DISPLAY) and must still surface as the
    Gemini group even when non-model photon* keys are filtered out. Drives a
    real pool key through detection rather than asserting on the helper alone.
    """
    extra = {
        "google": [
            {"id": "gp1", "label": "explicit-gemini", "source": "manual",
             "auth_type": "api_key", "base_url": "https://generativelanguage.googleapis.com"}
        ]
    }
    result = _call_get_available_models(
        monkeypatch, tmp_path, _photon_auth_payload(extra), with_load_pool=True,
    )
    names = _group_names(result)
    assert "Gemini" in names, f"Aliased google→gemini pool key must surface as Gemini; got {names}"
    assert not any("photon" in n.lower() for n in names), (
        f"Photon keys must still be filtered out; got {names}"
    )


# ── helper unit coverage ─────────────────────────────────────────────────────


def test_is_known_model_provider_rejects_photon():
    assert config._is_known_model_provider("photon") is False
    assert config._is_known_model_provider("photon-project") is False
    assert config._is_known_model_provider("photon-user") is False
    assert config._is_known_model_provider("") is False


def test_is_known_model_provider_accepts_static_and_custom():
    assert config._is_known_model_provider("copilot") is True
    assert config._is_known_model_provider("anthropic") is True
    assert config._is_known_model_provider("gemini") is True
    assert config._is_known_model_provider("custom:my-endpoint") is True
