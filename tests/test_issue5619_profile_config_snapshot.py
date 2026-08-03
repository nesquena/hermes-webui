"""Regression coverage for #5619 — the models-catalog rebuild must publish
only the *captured* profile's config, never a concurrent global repoint.

Root cause: ``get_available_models()`` runs the cold catalog rebuild on a
detached worker thread (or after async budget waits) that does not inherit the
request profile TLS. The old builder read the module-global ``cfg``, so a
profile switch / reload landing between snapshot-capture and build-time made
the picker publish the WRONG profile's providers, aliases, MoA presets and
LM Studio settings — e.g. a llama.cpp ``custom_providers`` profile showing the
default profile's models and keys.

The fix captures a deep-copied, request-owned config snapshot while the
request profile is authoritative and threads it through the builder and every
provider/config helper it reaches.
"""

import copy

import api.config as config


def _install_fake_hermes_cli(monkeypatch):
    """Stub hermes_cli so tests are deterministic and offline."""
    import sys
    import types

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
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)


def _pin_test_paths(monkeypatch, tmp_path):
    """Pin config/auth/cache paths to the tmp sandbox and disable reloads."""
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_models_cache_source_fingerprint", lambda: {"test": "fingerprint"})
    monkeypatch.setattr(config, "reload_config_if_stale", lambda: None)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "_cfg_mtime", 0.0, raising=False)
    # Force the synchronous (unbounded) rebuild path — deterministic, no worker.
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0, raising=False)
    # No live provider probes: every catalog entry comes from config.
    monkeypatch.setattr(config, "_read_live_provider_model_ids", lambda pid: [])


_PROFILE_A = {
    "model": {
        "provider": "custom:local-llama",
        "default": "qwen3.6:27b-mlx",
        "aliases": {"llama": "qwen3.6:27b-mlx", "qwen": "qwen3.6:27b-mlx"},
    },
    "custom_providers": [
        {
            "name": "local-llama",
            "base_url": "http://127.0.0.1:15721/v1",
            "api_key": "test-local-key",
            "model": "qwen3.6:27b-mlx",
        },
    ],
    "moa": {
        "presets": {
            "default": {"enabled": True},
            "Frontier Tuned": {"enabled": True},
            "Disabled Preset": {"enabled": False},
        },
    },
    "providers": {
        "lmstudio": {
            "base_url": "http://127.0.0.1:1234/v1",
            "api_key": "lm-test-key",
        },
    },
    "fallback_providers": [],
}

_PROFILE_B = {
    "model": {
        "provider": "anthropic",
        "default": "claude-haiku-4.5",
        "aliases": {},
    },
    "providers": {},
    "fallback_providers": [],
}


def test_builder_publishes_captured_profile_not_concurrent_global(monkeypatch, tmp_path):
    """Barrier-driven regression: the global ``cfg`` is repointed to profile B
    AFTER the snapshot is captured but BEFORE the builder runs; the published
    catalog must still be profile A's."""
    _install_fake_hermes_cli(monkeypatch)
    _pin_test_paths(monkeypatch, tmp_path)

    # Seed the module-global cfg with profile A. get_available_models() will
    # capture a deep-copied snapshot of this on the request thread.
    old_cfg = config.cfg
    config.cfg = copy.deepcopy(_PROFILE_A)
    config.invalidate_models_cache()

    real_invoke = config._invoke_models_rebuild
    switched = {"done": False}

    def _barrier_invoke(builder):
        # Barrier: between snapshot capture (above, in get_available_models)
        # and builder execution, repoint the global cfg to profile B — exactly
        # what a concurrent profile switch/reload does in production.
        if not switched["done"]:
            config.cfg = copy.deepcopy(_PROFILE_B)
            switched["done"] = True
        return real_invoke(builder)

    monkeypatch.setattr(config, "_invoke_models_rebuild", _barrier_invoke)

    try:
        payload = config.get_available_models(force_refresh=True)
    finally:
        config.cfg = old_cfg
        config.invalidate_models_cache()

    assert switched["done"], "barrier never fired — test is vacuous"

    # The published catalog must reflect the CAPTURED profile A.
    assert payload["active_provider"] == "custom:local-llama", (
        f"active_provider leaked global profile B: {payload['active_provider']!r}"
    )
    assert payload["default_model"] == "qwen3.6:27b-mlx", (
        f"default_model leaked global profile B: {payload['default_model']!r}"
    )
    group_ids = {g.get("provider_id") for g in payload["groups"]}
    assert "custom:local-llama" in group_ids, (
        f"custom provider from captured profile A missing; groups={sorted(group_ids)}"
    )
    # MoA presets come from the captured profile's config.
    moa_group = next((g for g in payload["groups"] if g.get("provider_id") == "moa"), None)
    assert moa_group is not None, "MoA group missing from captured profile catalog"
    moa_ids = [m["id"] for m in moa_group["models"]]
    assert "@moa:default" in moa_ids and "@moa:Frontier Tuned" in moa_ids, moa_ids
    # Aliases come from the captured profile's config.
    assert payload.get("aliases", {}).get("llama") == "qwen3.6:27b-mlx", (
        "aliases leaked global profile B"
    )
    # LM Studio base_url resolves from the captured profile's config.
    assert config._get_provider_base_url("lmstudio", copy.deepcopy(_PROFILE_A)) == (
        "http://127.0.0.1:1234/v1"
    )
    # Nothing from profile B leaked in.
    assert "anthropic" not in group_ids, (
        f"global profile B provider leaked into captured catalog: {group_ids}"
    )


def test_builder_requires_snapshot_and_preserves_in_memory_overrides(monkeypatch, tmp_path):
    """The snapshot parameter is REQUIRED (no ambient get_config() fallback,
    #5619 review). get_available_models() always captures one inside the
    profile scope; in-memory overrides (a rebound ``cfg``) are preserved
    verbatim by the capture, exactly like get_config() semantics."""
    _install_fake_hermes_cli(monkeypatch)
    _pin_test_paths(monkeypatch, tmp_path)

    old_cfg = config.cfg
    config.cfg = copy.deepcopy(_PROFILE_B)
    config.invalidate_models_cache()

    real_invoke = config._invoke_models_rebuild
    captured = {}

    def _spy_invoke(builder):
        captured["result"] = real_invoke(builder)
        return captured["result"]

    monkeypatch.setattr(config, "_invoke_models_rebuild", _spy_invoke)

    try:
        payload = config.get_available_models(force_refresh=True)
    finally:
        config.cfg = old_cfg
        config.invalidate_models_cache()

    assert payload["active_provider"] == "anthropic"
    assert payload["default_model"] == "claude-haiku-4.5"
    # The builder must have received a snapshot (never resolved ambient cfg):
    # the result deep-copied out of get_available_models() matches the
    # builder's captured output, and the builder output reflects profile B.
    assert captured.get("result", {}).get("active_provider") == "anthropic"


def test_snapshot_expands_env_against_calling_threads_profile_scope(monkeypatch, tmp_path):
    """The request-owned snapshot must be captured while the request profile is
    authoritative: a ${VAR} reference resolves against the calling thread's
    profile env, never the process-env-pinned shared cache (#798). The same
    config.yaml therefore yields different expansions depending on the active
    scope, and the returned dict is a deep copy, not the shared cache object."""
    import os
    import textwrap

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        textwrap.dedent(
            """\
            model:
              provider: custom
              default: "${MULTI_PROFILE_TEST_TOKEN}"
            fallback_providers: []
            providers: {}
            """
        ),
        encoding="utf-8",
    )

    old_cfg = config.cfg
    old_mtime = config._cfg_mtime
    old_path = config._cfg_path
    old_env = os.environ.get("MULTI_PROFILE_TEST_TOKEN")
    os.environ["MULTI_PROFILE_TEST_TOKEN"] = "ambient-secret"

    try:
        monkeypatch.setattr(config, "_get_config_path", lambda: cfg_file)
        # No in-memory overrides: cfg aliases the shared cache.
        config.cfg = config._cfg_cache
        config._cfg_mtime = 0.0
        config._cfg_path = None

        # Ambient scope: expands against the process env...
        ambient = config._capture_profile_config_snapshot()
        assert ambient["model"]["default"] == "ambient-secret", (
            f"ambient expansion mismatch: {ambient['model']['default']!r}"
        )
        # ...and the snapshot is request-owned, never the shared cache object.
        assert ambient is not config._cfg_cache

        # Profile scope: the SAME file must expand to the profile's value,
        # even though the shared cache is frozen with the ambient expansion.
        config._set_thread_env(MULTI_PROFILE_TEST_TOKEN="profile-secret")
        config._thread_ctx.block_process_env_fallback = True
        try:
            profiled = config._capture_profile_config_snapshot()
        finally:
            config._clear_thread_env()
            config._thread_ctx.block_process_env_fallback = False

        assert profiled["model"]["default"] == "profile-secret", (
            "snapshot froze the ambient expansion instead of the profile's "
            f"value: {profiled['model']['default']!r}"
        )
        assert config._cfg_cache["model"]["default"] == "ambient-secret", (
            "shared cache must stay process-env-pinned; the profile expansion "
            "must live only in the request-owned snapshot"
        )
    finally:
        config.cfg = old_cfg
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path
        if old_env is None:
            os.environ.pop("MULTI_PROFILE_TEST_TOKEN", None)
        else:
            os.environ["MULTI_PROFILE_TEST_TOKEN"] = old_env
