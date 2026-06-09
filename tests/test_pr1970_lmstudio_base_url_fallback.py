"""Regression for PR #1970 LM Studio provider × cfg.model.base_url shape.

PR #1970 added `_get_provider_base_url()` + a dedicated lmstudio branch in
`get_available_models()` for fetching live loaded models via the OpenAI-compatible
/v1/models endpoint.

The initial implementation only looked at `cfg["providers"]["lmstudio"]["base_url"]`,
missing the historical shape where users put `base_url` under `cfg["model"]`
(when `cfg["model"]["provider"] == "lmstudio"`). That shape is what
`tests/test_issue1527_lmstudio_base_url_classification.py` covers and what real
users have in their config.yaml — 3 pre-existing tests started failing on stage-337
because of this gap.

This regression test pins the helper's two-location lookup so a future change
can't accidentally drop the model.base_url fallback again.
"""
from __future__ import annotations

import api.config as config


class _RestoreCfg:
    """Context manager: snapshot cfg, restore on exit (test isolation)."""

    def __enter__(self):
        import copy
        self._snapshot = copy.deepcopy(config.cfg)
        return self

    def __exit__(self, *exc):
        config.cfg.clear()
        config.cfg.update(self._snapshot)


def test_get_provider_base_url_finds_explicit_providers_entry():
    """When providers.<id>.base_url is set, return that value."""
    with _RestoreCfg():
        config.cfg.clear()
        config.cfg.update({
            "providers": {
                "lmstudio": {"base_url": "http://10.0.0.5:1234/v1", "api_key": "x"},
            },
        })
        assert config._get_provider_base_url("lmstudio") == "http://10.0.0.5:1234/v1"


def test_get_provider_base_url_strips_trailing_slash():
    with _RestoreCfg():
        config.cfg.clear()
        config.cfg.update({
            "providers": {
                "lmstudio": {"base_url": "http://10.0.0.5:1234/v1/", "api_key": "x"},
            },
        })
        assert config._get_provider_base_url("lmstudio") == "http://10.0.0.5:1234/v1"


def test_get_provider_base_url_falls_back_to_model_base_url():
    """When providers.<id>.base_url is unset but cfg.model.base_url is set
    AND cfg.model.provider matches, the helper returns model.base_url."""
    with _RestoreCfg():
        config.cfg.clear()
        config.cfg.update({
            "model": {
                "provider": "lmstudio",
                "base_url": "http://192.168.1.22:1234/v1",
                "default": "qwen3.6-35b-a3b@q6_k",
            },
            "providers": {
                "lmstudio": {"api_key": "local-key"},  # no base_url here
            },
        })
        # Was returning None before the fix — the regression that broke
        # test_issue1527_lmstudio_base_url_classification.
        assert config._get_provider_base_url("lmstudio") == "http://192.168.1.22:1234/v1"


def test_get_provider_base_url_returns_none_when_unconfigured():
    """Unconfigured provider returns None (sentinel for 'use SDK default')."""
    with _RestoreCfg():
        config.cfg.clear()
        config.cfg.update({"providers": {}})
        assert config._get_provider_base_url("openai") is None
        assert config._get_provider_base_url("anthropic") is None
        assert config._get_provider_base_url("lmstudio") is None


def test_get_provider_base_url_model_block_only_matches_active_provider():
    """cfg.model.base_url must NOT leak to providers other than cfg.model.provider.

    If model.provider is anthropic but providers.openai exists without base_url,
    _get_provider_base_url("openai") must still return None — otherwise we'd
    silently rewrite the OpenAI SDK target to an Anthropic endpoint URL.
    """
    with _RestoreCfg():
        config.cfg.clear()
        config.cfg.update({
            "model": {
                "provider": "anthropic",
                "base_url": "https://my-anthropic-proxy.example.com/v1",
            },
            "providers": {
                "openai": {"api_key": "ok"},  # no base_url
                "anthropic": {"api_key": "ak"},  # no base_url
            },
        })
        # Active provider gets the model.base_url fallback.
        assert config._get_provider_base_url("anthropic") == "https://my-anthropic-proxy.example.com/v1"
        # OpenAI must NOT inherit it.
        assert config._get_provider_base_url("openai") is None


def test_get_provider_base_url_explicit_wins_over_model_fallback():
    """If both providers.<id>.base_url AND cfg.model.base_url are set with matching
    provider, the explicit providers entry wins."""
    with _RestoreCfg():
        config.cfg.clear()
        config.cfg.update({
            "model": {
                "provider": "lmstudio",
                "base_url": "http://wrong:1234/v1",
            },
            "providers": {
                "lmstudio": {"base_url": "http://correct:1234/v1", "api_key": "x"},
            },
        })
        assert config._get_provider_base_url("lmstudio") == "http://correct:1234/v1"



def test_lmstudio_fallback_works_when_hermes_cli_unavailable(tmp_path, monkeypatch):
    """The lmstudio branch must populate models from the urlopen fallback even
    when `from hermes_cli.models import provider_model_ids` raises ImportError.

    Pre-fix, the outer try/except in the lmstudio branch caught the ImportError
    and silently aborted the whole branch, never running the urlopen fallback —
    a CI-vs-local divergence where local environments with hermes_cli installed
    worked, and CI (clean editable install) failed with empty model groups.

    Caught in CI on stage-337; fix splits the hermes_cli try from the urlopen
    fallback so each runs independently.
    """
    import json as _json
    import socket as _socket
    import sys
    import urllib.request as _urlreq

    import api.config as config

    # Block hermes_cli import the way a CI runner without the package would.
    blocked_modules = [name for name in list(sys.modules) if name == "hermes_cli" or name.startswith("hermes_cli.")]
    for name in blocked_modules:
        monkeypatch.delitem(sys.modules, name, raising=False)

    class _Blocker:
        def find_module(self, name, path=None):
            if name == "hermes_cli" or name.startswith("hermes_cli."):
                return self
            return None

        def load_module(self, name):
            raise ImportError(f"hermes_cli blocked for test: {name}")

    blocker = _Blocker()
    sys.meta_path.insert(0, blocker)
    try:
        # Set up a config that points lmstudio at a fake base_url under cfg.model.
        cfgfile = tmp_path / "config.yaml"
        cfgfile.write_text(
            """
model:
  provider: lmstudio
  default: qwen3.6-35b-a3b@q6_k
  base_url: http://10.0.0.5:1234/v1
providers:
  lmstudio:
    api_key: local-key
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)
        config.reload_config()
        config.invalidate_models_cache()

        class _ModelsResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return _json.dumps(
                    {"data": [{"id": "qwen3.6-35b-a3b@q6_k"}, {"id": "another-model"}]}
                ).encode()

        monkeypatch.setattr(_urlreq, "urlopen", lambda *_a, **_kw: _ModelsResponse())
        monkeypatch.setattr(
            _socket,
            "getaddrinfo",
            lambda *_a, **_kw: [
                (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))
            ],
        )

        result = config.get_available_models()
        groups = {g["provider_id"]: g for g in result["groups"]}

        # Fallback must succeed despite hermes_cli being unimportable.
        assert "lmstudio" in groups, (
            f"lmstudio group missing when hermes_cli unavailable; groups={list(groups)}"
        )
        model_ids = {m["id"] for m in groups["lmstudio"]["models"]}
        assert "qwen3.6-35b-a3b@q6_k" in model_ids
        assert "another-model" in model_ids
    finally:
        try:
            sys.meta_path.remove(blocker)
        except ValueError:
            pass
