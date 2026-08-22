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
import os
import textwrap
from contextlib import contextmanager

import api.config as config
import api.profiles as profiles


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


# ── #5619 re-gate: A/B ${VAR} profile-env ownership (sync + detached worker) ──
# The re-gate review requires production-composed regressions with conflicting
# A/B ${VAR} values, a capture/reload barrier, A→B→A alternation, and
# assertions on the final catalog/provider URL/auth ownership.

_MULTI_PROFILE_A_ENV = {
    "MULTI_PROFILE_TEST_URL": "http://127.0.0.1:15721/v1",
    "MULTI_PROFILE_TEST_KEY": "a-key-9f8e7d",
}
_MULTI_PROFILE_B_ENV = {
    "MULTI_PROFILE_TEST_URL": "http://b-profile.invalid:9999/v1",
    "MULTI_PROFILE_TEST_KEY": "b-key-1234",
}
_MULTI_PROFILE_TEST_VARS = tuple(_MULTI_PROFILE_A_ENV)

_MULTI_PROFILE_YAML = textwrap.dedent(
    """\
    model:
      provider: custom:local-llama
      default: "qwen3.6:27b-mlx"
      aliases:
        llama: "qwen3.6:27b-mlx"
        qwen: "qwen3.6:27b-mlx"
    custom_providers:
      - name: local-llama
        base_url: "${MULTI_PROFILE_TEST_URL}"
        api_key: "${MULTI_PROFILE_TEST_KEY}"
        model: "qwen3.6:27b-mlx"
    moa:
      presets:
        default:
          enabled: true
        "Frontier Tuned":
          enabled: true
    providers:
      lmstudio:
        base_url: "${MULTI_PROFILE_TEST_URL}"
        api_key: "${MULTI_PROFILE_TEST_KEY}"
    fallback_providers: []
    """
)


@contextmanager
def _profile_env_scope(env: dict, purpose: str = "", logger_override=None):
    """Stand-in for api.profiles.profile_env_for_active_request /
    profile_scope_for_detached_worker: applies the given profile's env on the
    CURRENT thread via the exact channel the real scope uses
    (_set_thread_env + block_process_env_fallback), so ${VAR} expansion inside
    the scope resolves that profile's values. ``purpose`` / ``logger_override``
    mirror the real signatures (called with positional args from
    get_available_models)."""
    config._set_thread_env(**env)
    config._thread_ctx.block_process_env_fallback = True
    try:
        yield
    finally:
        config._clear_thread_env()
        config._thread_ctx.block_process_env_fallback = False


def _profile_env_scope_a(purpose: str = "", logger_override=None):
    return _profile_env_scope(_MULTI_PROFILE_A_ENV, purpose, logger_override)


def _profile_env_scope_b(purpose: str = "", logger_override=None):
    return _profile_env_scope(_MULTI_PROFILE_B_ENV, purpose, logger_override)


def _install_env_config(monkeypatch, tmp_path):
    """Write a config.yaml whose provider URL/auth reference ${VAR}s, pin the
    config path, clear in-memory overrides, and stub hermes/live probes.
    Returns a state dict capturing cfg/_cfg_path/_cfg_mtime/_cfg_fingerprint
    so the caller can restore them exactly — leaving _cfg_path at None would
    make the NEXT test's reload_config_if_stale() see a path change, refresh
    the shared cache and clobber that test's in-memory override cfg."""
    _install_fake_hermes_cli(monkeypatch)
    _pin_test_paths(monkeypatch, tmp_path)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(_MULTI_PROFILE_YAML, encoding="utf-8")
    old_state = {
        "cfg": config.cfg,
        "path": config._cfg_path,
        "mtime": config._cfg_mtime,
        "fp": config._cfg_fingerprint,
    }
    # No in-memory overrides: the snapshot must be re-expanded from the raw
    # YAML against the calling thread's profile env, never a deep copy of a
    # process-env-pinned shared dict.
    config.cfg = config._cfg_cache
    config._cfg_mtime = 0.0
    config._cfg_path = None
    config.invalidate_models_cache()
    return old_state


def _restore_test_env(old_state, old_env):
    config.cfg = old_state["cfg"]
    config._cfg_path = old_state["path"]
    config._cfg_mtime = old_state["mtime"]
    config._cfg_fingerprint = old_state["fp"]
    config.invalidate_models_cache()
    for k in _MULTI_PROFILE_TEST_VARS:
        v = old_env.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _assert_catalog_is_profile_a(payload):
    """The final published catalog reflects the captured profile A."""
    assert payload["active_provider"] == "custom:local-llama", (
        f"active_provider leaked the ambient profile: {payload['active_provider']!r}"
    )
    assert payload["default_model"] == "qwen3.6:27b-mlx", payload["default_model"]
    group_ids = {g.get("provider_id") for g in payload["groups"]}
    assert "custom:local-llama" in group_ids, f"profile A custom provider missing; groups={sorted(group_ids)}"
    assert "anthropic" not in group_ids, f"foreign profile provider leaked; groups={sorted(group_ids)}"
    assert payload.get("aliases", {}).get("llama") == "qwen3.6:27b-mlx", (
        "aliases leaked the ambient profile"
    )
    moa_group = next((g for g in payload["groups"] if g.get("provider_id") == "moa"), None)
    assert moa_group is not None, "MoA group missing from captured profile catalog"
    moa_ids = [m["id"] for m in moa_group["models"]]
    assert "@moa:default" in moa_ids and "@moa:Frontier Tuned" in moa_ids, moa_ids


def _assert_snapshot_is_profile_a(snapshot):
    """The request-owned snapshot holds profile A's ${VAR} expansions and the
    builder's provider-URL/auth helpers resolve A's values from it."""
    cp = snapshot["custom_providers"][0]
    assert cp["base_url"] == _MULTI_PROFILE_A_ENV["MULTI_PROFILE_TEST_URL"], (
        f"custom provider URL froze the wrong expansion: {cp['base_url']!r}"
    )
    assert cp["api_key"] == _MULTI_PROFILE_A_ENV["MULTI_PROFILE_TEST_KEY"], (
        f"custom provider key froze the wrong expansion: {cp['api_key']!r}"
    )
    assert cp["base_url"] != _MULTI_PROFILE_B_ENV["MULTI_PROFILE_TEST_URL"]
    # Provider URL / auth ownership through the exact helpers the builder uses.
    assert config._get_provider_base_url("lmstudio", snapshot) == _MULTI_PROFILE_A_ENV[
        "MULTI_PROFILE_TEST_URL"
    ]
    assert (
        config._get_provider_cfg("lmstudio", snapshot).get("api_key")
        == _MULTI_PROFILE_A_ENV["MULTI_PROFILE_TEST_KEY"]
    )


def _spy_capture_and_url_helper(monkeypatch, box, lm_config_objs):
    """Spy on the capture seam (stash the exact snapshot the builder receives)
    and on _get_provider_base_url (record the config object the builder passes
    for lmstudio), so tests can assert identity — never a foreign global. The
    capture seam is probed with getattr: on a pre-fix head that still reads
    get_config() directly there is no seam to spy, and the catalog assertions
    below fail on their own."""
    real_capture = getattr(config, "_capture_profile_config_snapshot", None)
    if real_capture is not None:

        def _spy_capture():
            snap = real_capture()
            box["snapshot"] = snap
            return snap

        monkeypatch.setattr(config, "_capture_profile_config_snapshot", _spy_capture)

    real_gpbu = config._get_provider_base_url

    def _spy_gpbu(provider_id, config_obj=None):
        if provider_id == "lmstudio":
            lm_config_objs.append(config_obj)
        return real_gpbu(provider_id, config_obj)

    monkeypatch.setattr(config, "_get_provider_base_url", _spy_gpbu)


def _assert_snapshot_was_captured(box):
    snapshot = box.get("snapshot")
    assert snapshot is not None, (
        "no snapshot was captured through the profile-owned seam — the "
        "pre-fix shape (deepcopy of the shared cache outside the profile "
        "scope) never exercised the capture path"
    )
    assert snapshot is not config._cfg_cache, "snapshot aliased the shared cache"
    return snapshot


def _ab_a_barrier(monkeypatch, phases):
    """Capture/reload barrier between snapshot capture and builder execution:
    flips the ambient/global env to profile B (what a concurrent profile
    reload re-expanding ${VAR} would see), mutates the SHARED cache in place
    (what a reload does to the dict the old code deep-copied after its lock
    was released), runs the build, then flips back to A (A→B→A alternation)."""

    real_invoke = config._invoke_models_rebuild

    def _barrier_invoke(builder):
        phases["entered"] += 1
        config._set_thread_env(**_MULTI_PROFILE_B_ENV)
        config._thread_ctx.block_process_env_fallback = True
        _custom = config._cfg_cache.get("custom_providers")
        _restore = None
        if isinstance(_custom, list) and _custom and isinstance(_custom[0], dict):
            _orig_url = _custom[0].get("base_url")
            _orig_key = _custom[0].get("api_key")
            _custom[0]["base_url"] = _MULTI_PROFILE_B_ENV["MULTI_PROFILE_TEST_URL"]
            _custom[0]["api_key"] = _MULTI_PROFILE_B_ENV["MULTI_PROFILE_TEST_KEY"]
            _restore = (_custom[0], _orig_url, _orig_key)
        try:
            result = real_invoke(builder)
        finally:
            if _restore is not None:
                _entry, _orig_url, _orig_key = _restore
                _entry["base_url"] = _orig_url
                _entry["api_key"] = _orig_key
            # A→B→A alternation: back to profile A's env after the build.
            config._set_thread_env(**_MULTI_PROFILE_A_ENV)
            config._thread_ctx.block_process_env_fallback = True
        return result

    monkeypatch.setattr(config, "_invoke_models_rebuild", _barrier_invoke)


def test_sync_rebuild_captures_profile_a_env_through_ab_a_alternation(monkeypatch, tmp_path):
    """Production-composed SYNC regression (#5619 re-gate): a config.yaml whose
    provider URL/auth reference ${VAR}s. The request profile scope applies
    profile A's env; the capture/reload barrier flips the ambient env to B and
    mutates the shared cache between capture and build, then back to A. The
    published catalog AND the provider URL/auth the builder resolves must be
    profile A's — never B's and never the shared dict."""
    old_state = _install_env_config(monkeypatch, tmp_path)
    old_env = {k: os.environ.get(k) for k in _MULTI_PROFILE_TEST_VARS}
    try:
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "profile-a")
        monkeypatch.setattr(profiles, "profile_env_for_active_request", _profile_env_scope_a)

        box: dict = {}
        lm_config_objs: list = []
        _spy_capture_and_url_helper(monkeypatch, box, lm_config_objs)

        phases = {"entered": 0}
        _ab_a_barrier(monkeypatch, phases)

        payload = config.get_available_models(force_refresh=True)
    finally:
        _restore_test_env(old_state, old_env)

    assert phases["entered"] == 1, "barrier never fired — test is vacuous"
    _assert_catalog_is_profile_a(payload)
    snapshot = _assert_snapshot_was_captured(box)
    _assert_snapshot_is_profile_a(snapshot)
    assert lm_config_objs and all(c is snapshot for c in lm_config_objs), (
        "builder resolved the provider URL from a foreign config object, not "
        f"the captured snapshot: {[type(c).__name__ for c in lm_config_objs]}"
    )


def test_detached_worker_rebuild_captures_profile_a_env_through_ab_a_alternation(
    monkeypatch, tmp_path
):
    """Production-composed DETACHED-WORKER regression (#5619 re-gate): the
    bounded path spawns the models-catalog-rebuild daemon, which enters the
    captured profile's env scope and captures the snapshot ON THE WORKER
    thread. The barrier flips the worker's ambient env to B and mutates the
    shared cache between capture and build, then back to A. The worker-built
    catalog and the provider URL/auth it resolves must be profile A's."""
    old_state = _install_env_config(monkeypatch, tmp_path)
    old_env = {k: os.environ.get(k) for k in _MULTI_PROFILE_TEST_VARS}
    try:
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "profile-a")
        monkeypatch.setattr(
            profiles, "profile_scope_for_detached_worker", _profile_env_scope_a
        )
        # Bounded rebuild → the detached worker thread builds and publishes.
        monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 30.0)

        box: dict = {}
        lm_config_objs: list = []
        _spy_capture_and_url_helper(monkeypatch, box, lm_config_objs)

        phases = {"entered": 0}
        _ab_a_barrier(monkeypatch, phases)

        payload = config.get_available_models(force_refresh=True)
    finally:
        _restore_test_env(old_state, old_env)

    assert phases["entered"] == 1, "worker rebuild never ran — test is vacuous"
    _assert_catalog_is_profile_a(payload)
    snapshot = _assert_snapshot_was_captured(box)
    _assert_snapshot_is_profile_a(snapshot)
    assert lm_config_objs and all(c is snapshot for c in lm_config_objs), (
        "worker builder resolved the provider URL from a foreign config object, "
        f"not the captured snapshot: {[type(c).__name__ for c in lm_config_objs]}"
    )


def test_snapshot_fallback_is_empty_defaults_never_foreign_shared_cache(monkeypatch, tmp_path):
    """greptile P1 (#5619 review, optional): when the active profile's config
    file is missing/empty/invalid while the shared cache was populated by
    another profile, the snapshot fallback must produce THIS profile's
    empty/default configuration — never a copy of the shared cache, which
    would expose the previous profile's providers/models/aliases/endpoints."""
    _install_fake_hermes_cli(monkeypatch)
    _pin_test_paths(monkeypatch, tmp_path)
    cfg_file = tmp_path / "config.yaml"
    assert not cfg_file.exists(), "test needs a missing config file"

    old_cfg = config.cfg
    old_cache = config._cfg_cache
    old_mtime = config._cfg_mtime
    old_path = config._cfg_path
    old_fp = config._cfg_fingerprint
    try:
        # Another profile populated the shared cache with rich config...
        config._cfg_cache = copy.deepcopy(_PROFILE_A)
        config.cfg = config._cfg_cache
        # ...while the active profile's own config file is missing. Same
        # resolved path, fresh mtime and a matching fingerprint keep
        # reload_config_if_stale() (stubbed no-op here) from clearing it —
        # the exact state the fallback branch must not copy.
        config._cfg_path = cfg_file
        config._cfg_mtime = 0.0
        config._cfg_fingerprint = config._fingerprint_config(config._cfg_cache)

        snapshot = config._capture_profile_config_snapshot()

        assert snapshot is not config._cfg_cache, "snapshot aliased the shared cache"
        # Nothing from the foreign profile A leaked in.
        assert snapshot.get("custom_providers") in (None, []), (
            f"foreign profile's custom_providers leaked: {snapshot.get('custom_providers')!r}"
        )
        assert snapshot.get("moa") is None, "foreign profile's moa presets leaked"
        assert snapshot.get("model") is None, "foreign profile's model section leaked"
        assert snapshot.get("providers") is None, "foreign profile's providers leaked"
        assert snapshot.get("fallback_providers") is None
        # Default-only keys are still applied — the same shape get_config()
        # returns for a missing file.
        assert isinstance(snapshot.get("agent", {}).get("personalities"), dict)
        assert snapshot.get("experimental", {}).get("unified_session_db") is False
    finally:
        config.cfg = old_cfg
        config._cfg_cache = old_cache
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path
        config._cfg_fingerprint = old_fp
