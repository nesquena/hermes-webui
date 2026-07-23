"""Regression tests for issue #3957.

On a **non-default profile**, two read-only endpoints broke because they
resolved provider credentials / model cache from the process-global *default*
profile instead of the per-request (cookie-scoped, issue #798) active profile:

  Facet A — ``/api/providers`` + ``/api/models`` did not apply the active
    profile's ``.env`` around the read, so ``get_auth_status()`` /
    ``provider_model_ids()`` / custom-key lookups resolved against the default
    profile's credentials.  On a non-default profile the auth probes could stall
    past the 30s frontend abort → "Failed to load providers: Request timed out."

  Facet B — the ``/api/models`` disk cache path was a single import-time
    ``STATE_DIR / "models_cache.json"`` shared across every profile, while the
    cache *fingerprint* is profile-specific → a non-default profile rejected the
    shared snapshot on every read and cold-rebuilt (the slow path).

The fix:
  - ``api.config._get_models_cache_path()`` returns a profile-keyed path
    (``models_cache.<profile>.json`` for named profiles; unchanged
    ``models_cache.json`` for the default/root profile).
  - ``api.profiles.profile_env_for_active_request_readonly()`` applies the active
    per-request profile's env around the read; no-op for the default profile.
"""

import os
import sys
import types
from pathlib import Path

import api.config as config
import api.profiles as profiles


# ─────────────────────────────────────────────────────────────────────────────
# Facet B — profile-keyed models disk cache
# ─────────────────────────────────────────────────────────────────────────────


def _force_active_profile(monkeypatch, name, *, root=False):
    """Make get_active_profile_name() return *name* and control root detection.

    Avoids the subprocess list_profiles_api() call inside _is_root_profile by
    patching it to a pure function of the name.
    """
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: name)
    monkeypatch.setattr(
        profiles, "_is_root_profile", lambda n: bool(root) or n in ("", "default")
    )
    # config imports these names lazily inside _get_models_cache_path, so the
    # patches on the profiles module are what matter.


def test_models_cache_path_default_profile_unchanged(monkeypatch):
    """Default/root profile keeps the original models_cache.json filename."""
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    assert config._get_models_cache_path() == config._models_cache_path
    assert config._get_models_cache_path().name == "models_cache.json"


def test_models_cache_path_empty_profile_unchanged(monkeypatch):
    """An empty/unset active profile falls back to the default path."""
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "")
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    assert config._get_models_cache_path() == config._models_cache_path


def test_models_cache_path_named_profile_is_distinct(monkeypatch):
    """A named profile gets its own cache file, not the default's."""
    _force_active_profile(monkeypatch, "work")
    path = config._get_models_cache_path()
    assert path != config._models_cache_path
    assert path.name == "models_cache.work.json"
    assert path.parent == config._models_cache_path.parent


def test_models_cache_path_two_named_profiles_do_not_collide(monkeypatch):
    """Distinct non-default profiles never share a cache file (the bug)."""
    _force_active_profile(monkeypatch, "work")
    work = config._get_models_cache_path()
    _force_active_profile(monkeypatch, "personal")
    personal = config._get_models_cache_path()
    assert work != personal
    assert work != config._models_cache_path
    assert personal != config._models_cache_path


def test_models_cache_path_sanitizes_unsafe_chars(monkeypatch):
    """Defense in depth: the on-disk filename is always filesystem-safe."""
    _force_active_profile(monkeypatch, "weird/../name")
    path = config._get_models_cache_path()
    # No path separators or traversal can leak into the filename.
    assert path.parent == config._models_cache_path.parent
    assert "/" not in path.name
    assert ".." not in path.name.replace("models_cache.", "").replace(".json", "")


def test_models_cache_path_falls_back_on_resolution_error(monkeypatch):
    """If profile resolution raises, fall back to the default path (no crash)."""
    def _boom():
        raise RuntimeError("profiles unavailable")

    monkeypatch.setattr(profiles, "get_active_profile_name", _boom)
    assert config._get_models_cache_path() == config._models_cache_path


# ─────────────────────────────────────────────────────────────────────────────
# Facet A — profile-env applied around the read-only endpoints
# ─────────────────────────────────────────────────────────────────────────────


def test_active_request_env_noop_for_default_profile(monkeypatch):
    """The context manager is a true no-op for the default profile."""
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    monkeypatch.delenv("ISSUE_3957_PROBE", raising=False)
    with profiles.profile_env_for_active_request_readonly("test"):
        # No env mutation, no HERMES_HOME change for the default profile.
        assert os.environ.get("ISSUE_3957_PROBE") is None
    assert os.environ.get("ISSUE_3957_PROBE") is None


def test_active_request_env_applies_named_profile_env(monkeypatch, tmp_path):
    """A named profile's .env is bound to thread-local state, process env untouched."""
    base = tmp_path / ".hermes"
    (base / "profiles" / "work").mkdir(parents=True)
    (base / "profiles" / "work" / ".env").write_text(
        "ISSUE_3957_PROBE=from-work-profile\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.delenv("ISSUE_3957_PROBE", raising=False)

    # Simulate the per-request cookie context (issue #798).
    monkeypatch.setenv("ISSUE_3957_PROBE", "from-process-env")
    profiles.set_request_profile("work")
    try:
        assert profiles.get_active_profile_name() == "work"
        assert config._thread_local_env_value("ISSUE_3957_PROBE") == "from-process-env"
        with profiles.profile_env_for_active_request_readonly("test"):
            assert config._thread_local_env_value("ISSUE_3957_PROBE") == "from-work-profile"
            assert os.environ.get("ISSUE_3957_PROBE") == "from-process-env"
        # Restored after the block exits.
        assert config._thread_local_env_value("ISSUE_3957_PROBE") == "from-process-env"
        assert os.environ.get("ISSUE_3957_PROBE") == "from-process-env"
    finally:
        profiles.clear_request_profile()


def test_active_request_env_restores_on_exception(monkeypatch, tmp_path):
    """Env is restored even if the wrapped read raises."""
    base = tmp_path / ".hermes"
    (base / "profiles" / "work").mkdir(parents=True)
    (base / "profiles" / "work" / ".env").write_text(
        "ISSUE_3957_PROBE=from-work-profile\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.delenv("ISSUE_3957_PROBE", raising=False)
    monkeypatch.setenv("ISSUE_3957_PROBE", "from-process-env")

    profiles.set_request_profile("work")
    try:
        with_raised = False
        try:
            with profiles.profile_env_for_active_request_readonly("test"):
                assert config._thread_local_env_value("ISSUE_3957_PROBE") == "from-work-profile"
                assert os.environ.get("ISSUE_3957_PROBE") == "from-process-env"
                raise ValueError("boom")
        except ValueError:
            with_raised = True
        assert with_raised
        assert config._thread_local_env_value("ISSUE_3957_PROBE") == "from-process-env"
        assert os.environ.get("ISSUE_3957_PROBE") == "from-process-env"
    finally:
        profiles.clear_request_profile()


def test_active_request_scope_prefers_profile_key_over_process_env_for_custom_provider(
    monkeypatch,
    tmp_path,
):
    """Profile-scope thread env resolves custom-provider env vars before process env."""
    base = tmp_path / ".hermes"
    (base / "profiles" / "work").mkdir(parents=True)
    (base / "profiles" / "work" / ".env").write_text(
        "ISSUE_3957_CUSTOM_KEY=from-work-profile\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("ISSUE_3957_CUSTOM_KEY", "from-process-env")
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "custom_providers": [
                {"name": "Team", "api_key": "${ISSUE_3957_CUSTOM_KEY}"}
            ]
        },
    )

    profiles.set_request_profile("work")
    try:
        assert config.resolve_custom_provider_connection("custom:team") == (
            "from-process-env",
            None,
        )
        with profiles.profile_env_for_active_request_readonly("test"):
            assert config.resolve_custom_provider_connection("custom:team") == (
                "from-work-profile",
                None,
            )
            assert os.environ.get("ISSUE_3957_CUSTOM_KEY") == "from-process-env"
        assert config._thread_local_env_value("ISSUE_3957_CUSTOM_KEY") == (
            "from-process-env"
        )
    finally:
        profiles.clear_request_profile()


def test_active_request_scope_sets_context_local_hermes_home(monkeypatch, tmp_path):
    """Request scope keeps agent-side Hermes-home readers on the active profile."""
    base = tmp_path / ".hermes"
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    current_home = {"value": None}

    fake_constants = types.SimpleNamespace()

    def _set_override(path):
        previous = current_home["value"]
        current_home["value"] = Path(path)
        return previous

    def _reset_override(token):
        current_home["value"] = token

    fake_constants.set_hermes_home_override = _set_override
    fake_constants.reset_hermes_home_override = _reset_override
    fake_constants.get_hermes_home = lambda: current_home["value"]
    monkeypatch.setitem(sys.modules, "hermes_constants", fake_constants)

    profiles.set_request_profile("work")
    try:
        with profiles.profile_env_for_active_request_readonly("test"):
            assert fake_constants.get_hermes_home() == work_home
    finally:
        profiles.clear_request_profile()


def test_active_request_scope_restores_state_when_home_reset_fails(monkeypatch, tmp_path):
    """Readonly scope still clears thread-local state if Hermes-home reset raises."""
    base = tmp_path / ".hermes"
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    (work_home / ".env").write_text(
        "ISSUE_3957_PROBE=from-work-profile\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("ISSUE_3957_PROBE", "from-process-env")
    current_home = {"value": None}

    fake_constants = types.SimpleNamespace()

    def _set_override(path):
        previous = current_home["value"]
        current_home["value"] = Path(path)
        return previous

    def _reset_override(token):
        current_home["value"] = token
        raise RuntimeError("reset failed")

    fake_constants.set_hermes_home_override = _set_override
    fake_constants.reset_hermes_home_override = _reset_override
    fake_constants.get_hermes_home = lambda: current_home["value"]
    monkeypatch.setitem(sys.modules, "hermes_constants", fake_constants)

    profiles.set_request_profile("work")
    try:
        with profiles.profile_env_for_active_request_readonly("test"):
            assert config._thread_local_env_value("ISSUE_3957_PROBE") == "from-work-profile"
            assert fake_constants.get_hermes_home() == work_home
        assert config._thread_local_env_value("ISSUE_3957_PROBE") == "from-process-env"
        assert getattr(config._thread_ctx, "block_process_env_fallback", False) is False
    finally:
        profiles.clear_request_profile()


def test_active_request_legacy_scope_still_mirrors_process_env(monkeypatch, tmp_path):
    """Live-model request scope still mirrors env for agent-side readers."""
    base = tmp_path / ".hermes"
    (base / "profiles" / "work").mkdir(parents=True)
    (base / "profiles" / "work" / ".env").write_text(
        "ISSUE_3957_PROBE=from-work-profile\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("ISSUE_3957_PROBE", "from-process-env")

    profiles.set_request_profile("work")
    try:
        with profiles.profile_env_for_active_request("test"):
            assert os.environ.get("ISSUE_3957_PROBE") == "from-work-profile"
        assert os.environ.get("ISSUE_3957_PROBE") == "from-process-env"
    finally:
        profiles.clear_request_profile()


def test_active_request_readonly_scope_blocks_process_env_fallback(monkeypatch, tmp_path):
    """Named profiles without a key should not inherit the process-default key."""
    from api.providers import _provider_has_key

    base = tmp_path / ".hermes"
    (base / "profiles" / "work").mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("OPENAI_API_KEY", "from-process-env")

    profiles.set_request_profile("work")
    try:
        assert _provider_has_key("openai") is True
        with profiles.profile_env_for_active_request_readonly("test"):
            assert _provider_has_key("openai") is False
    finally:
        profiles.clear_request_profile()


def test_active_request_readonly_scope_blocks_pool_env_seed(monkeypatch, tmp_path):
    """Readonly profile reads must not let load_pool seed process-default keys."""
    from api.providers import _get_provider_api_key, _provider_has_key

    base = tmp_path / ".hermes"
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    # Point HERMES_HOME at this test's own tmp base too (#4740). The readonly scope
    # blocks the process-env credential fallback correctly, but the credential pool's
    # global-root fallback (read_credential_pool -> _load_global_auth_store) resolves
    # the root via get_default_hermes_root(), which reads os.environ["HERMES_HOME"]
    # DIRECTLY — not the thread-local/scoped home. Under full-suite ordering that env
    # var points at the shared persistent test state dir, where an EARLIER test
    # persisted an openrouter credential_pool entry (source: env:OPENROUTER_API_KEY)
    # into auth.json. read_credential_pool then falls back to that global store, finds
    # the leaked openrouter entry, and _has_explicit_pool_credentials returns True —
    # an order-dependent false failure that doesn't reproduce in isolation. Pinning
    # HERMES_HOME to this test's empty tmp base makes the global-root fallback resolve
    # to a clean dir with no leaked auth.json.
    monkeypatch.setenv("HERMES_HOME", str(base))
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-process-env")

    profiles.set_request_profile("work")
    try:
        with profiles.profile_env_for_active_request_readonly("test"):
            assert _provider_has_key("openrouter") is False
            assert _get_provider_api_key("openrouter") is None
    finally:
        profiles.clear_request_profile()

    assert (work_home / "auth.json").exists() is False


def test_providers_and_models_routes_wrap_in_profile_env():
    """The two read routes are profile-scoped for non-default profiles (#3957).

    Structural guard: a future refactor that drops the wiring would silently
    reintroduce the bug, so pin it at the source level.
      - /api/providers and /api/provider/quota wrap the synchronous read in
        profile_env_for_active_request_readonly.
      - /api/models/live stays on the mirrored profile_env_for_active_request
        path because provider_model_ids() still delegates into agent helpers
        that read process env / HERMES_HOME directly.
      - /api/models relies on get_available_models() using the mirrored request
        scope for the budget<=0 sync rebuild plus profile_scope_for_detached_worker
        for the detached rebuild worker (the request-thread wrapper cannot reach
        the worker thread — Codex CORE finding).
    """
    routes_src = Path(profiles.__file__).resolve().parent.joinpath("routes.py").read_text(
        encoding="utf-8"
    )
    assert 'with profile_env_for_active_request("/api/models/live"' in routes_src
    assert "profile_env_for_active_request_readonly" in routes_src
    config_src = Path(config.__file__).resolve().read_text(encoding="utf-8")
    assert "profile_env_for_active_request as _prof_env_request" in config_src
    assert "profile_scope_for_detached_worker" in config_src
    assert "_get_models_cache_path" in config_src


def test_models_sync_rebuild_uses_legacy_mirrored_env(monkeypatch, tmp_path):
    """The budget<=0 sync rebuild still mirrors profile env into os.environ."""
    base = tmp_path / ".hermes"
    (base / "profiles" / "work").mkdir(parents=True)
    (base / "profiles" / "work" / ".env").write_text(
        "ISSUE_3957_PROBE=from-work-profile\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("ISSUE_3957_PROBE", "from-process-env")
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0)
    monkeypatch.setattr(config, "_available_models_cache", None)
    monkeypatch.setattr(config, "_available_models_cache_ts", 0.0)
    monkeypatch.setattr(config, "_available_models_cache_source_fingerprint", None)
    monkeypatch.setattr(config, "_cache_build_in_progress", False)
    monkeypatch.setattr(config, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(config, "_save_models_cache_to_disk", lambda result: None)
    monkeypatch.setattr(config, "_models_cache_source_fingerprint", lambda: "issue-3957")
    seen = {}

    def _capture_rebuild(_builder):
        seen["process_env"] = os.environ.get("ISSUE_3957_PROBE")
        seen["thread_env"] = config._thread_local_env_value("ISSUE_3957_PROBE")
        return {"active_provider": None, "default_model": "", "groups": []}

    monkeypatch.setattr(config, "_invoke_models_rebuild", _capture_rebuild)

    profiles.set_request_profile("work")
    try:
        result = config.get_available_models()
    finally:
        profiles.clear_request_profile()

    assert seen["process_env"] == "from-work-profile"
    assert seen["thread_env"] == "from-work-profile"
    assert os.environ.get("ISSUE_3957_PROBE") == "from-process-env"
    assert result["groups"] == []


def test_thread_local_env_value_none_default_returns_empty_string(monkeypatch):
    """A None default never escapes the string-return contract."""
    monkeypatch.setattr(config._thread_ctx, "env", {"ISSUE_3957_NONE": None}, raising=False)
    monkeypatch.setattr(
        config._thread_ctx, "block_process_env_fallback", False, raising=False
    )
    assert config._thread_local_env_value("ISSUE_3957_NONE", None) == ""


# ─────────────────────────────────────────────────────────────────────────────
# Facet A (worker thread) — the detached models-rebuild worker is profile-scoped
# (Codex CORE finding: the request-thread wrapper cannot reach the worker thread)
# ─────────────────────────────────────────────────────────────────────────────


def test_detached_worker_scope_binds_tls_for_default_profile(monkeypatch):
    """profile_scope_for_detached_worker binds TLS for the default profile (#6326).

    For 'default', the scope must set the request-profile TLS so the worker
    resolves the default profile's configuration even when the process-wide
    active profile is a named profile, AND must set block_process_env_fallback
    on the worker thread's TLS so _thread_local_env_value() callers cannot see
    named-profile credentials (#6327).  The process-wide os.environ is NOT
    mutated (that would break concurrent named-profile requests).
    """
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    monkeypatch.delenv("ISSUE_3957_WPROBE", raising=False)

    # Set process-wide active to a named profile to verify TLS overrides it.
    profiles._active_profile = "work"
    try:
        # Default name → TLS bound, block_process_env_fallback set.
        with profiles.profile_scope_for_detached_worker("default", "test"):
            assert profiles.get_active_profile_name() == "default"
            assert os.environ.get("ISSUE_3957_WPROBE") is None
    finally:
        profiles._active_profile = "default"
    # Empty name → still no-op.
    with profiles.profile_scope_for_detached_worker("", "test"):
        assert os.environ.get("ISSUE_3957_WPROBE") is None
    # After the scope, TLS is cleared; falls back to process-wide active.
    assert profiles.get_active_profile_name() in ("", "default")


def test_detached_worker_scope_binds_profile_on_new_thread(monkeypatch, tmp_path):
    """A worker thread re-binds the captured profile's TLS + env + cache path.

    Reproduces the Codex CORE finding: WITHOUT the scope a new thread resolves
    the default profile (cache path models_cache.json, no profile env); WITH it
    the thread resolves the captured profile's cache file + .env.
    """
    import threading

    base = tmp_path / ".hermes"
    (base / "profiles" / "work").mkdir(parents=True)
    (base / "profiles" / "work" / ".env").write_text(
        "ISSUE_3957_WPROBE=worker-env\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    # Point the default models cache at an isolated tmp file so the named path
    # derives from it (models_cache.work.json under the same dir).
    default_cache = tmp_path / "state" / "models_cache.json"
    default_cache.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "_models_cache_path", default_cache)
    monkeypatch.delenv("ISSUE_3957_WPROBE", raising=False)

    out = {}

    def worker():
        # No TLS on this fresh thread yet → default profile resolution (the bug).
        out["before_name"] = config._get_models_cache_path().name
        out["before_env"] = os.environ.get("ISSUE_3957_WPROBE")
        with profiles.profile_scope_for_detached_worker("work", "test-worker"):
            out["inside_name"] = config._get_models_cache_path().name
            out["inside_env"] = os.environ.get("ISSUE_3957_WPROBE")
        out["after_name"] = config._get_models_cache_path().name
        out["after_env"] = os.environ.get("ISSUE_3957_WPROBE")

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert out["before_name"] == "models_cache.json"  # default (no scope)
    assert out["before_env"] is None
    assert out["inside_name"] == "models_cache.work.json"  # profile-scoped
    assert out["inside_env"] == "worker-env"
    assert out["after_name"] == "models_cache.json"  # restored
    assert out["after_env"] is None


def test_detached_worker_prefers_profile_key_for_custom_provider(monkeypatch, tmp_path):
    """Detached worker scope resolves custom-provider env from thread profile, not process env."""
    import threading

    base = tmp_path / ".hermes"
    (base / "profiles" / "work").mkdir(parents=True)
    (base / "profiles" / "work" / ".env").write_text(
        "ISSUE_3957_CUSTOM_KEY=from-worker-profile\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("ISSUE_3957_CUSTOM_KEY", "from-process-env")
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "custom_providers": [
                {"name": "team", "api_key": "${ISSUE_3957_CUSTOM_KEY}"}
            ]
        },
    )

    out = {}

    def worker():
        with profiles.profile_scope_for_detached_worker("work", "test-worker"):
            out["value"] = config.resolve_custom_provider_connection("custom:team")[0]

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert out["value"] == "from-worker-profile"


def test_detached_worker_scope_blocks_pool_env_seed(monkeypatch, tmp_path):
    """Detached worker scope must not let load_pool seed process-default keys."""
    base = tmp_path / ".hermes"
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-process-env")

    with profiles.profile_scope_for_detached_worker("work", "test-worker"):
        assert os.environ.get("OPENROUTER_API_KEY") is None
        assert config._has_explicit_pool_credentials("openrouter") is False
        assert getattr(config._thread_ctx, "block_process_env_fallback", False) is True

    assert os.environ.get("OPENROUTER_API_KEY") == "from-process-env"
    assert getattr(config._thread_ctx, "block_process_env_fallback", False) is False
    assert (work_home / "auth.json").exists() is False


def test_detached_worker_scope_scrubs_absent_custom_provider_key_env(monkeypatch, tmp_path):
    """Detached worker scope clears missing custom-provider key_env fallbacks too."""
    base = tmp_path / ".hermes"
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    (work_home / "config.yaml").write_text(
        "custom_providers:\n"
        "  - name: Team\n"
        "    base_url: https://example.invalid/v1\n"
        "    key_env: ISSUE_3957_CUSTOM_KEY\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("ISSUE_3957_CUSTOM_KEY", "from-process-env")

    with profiles.profile_scope_for_detached_worker("work", "test-worker"):
        assert os.environ.get("ISSUE_3957_CUSTOM_KEY") is None
        assert config._thread_local_env_value("ISSUE_3957_CUSTOM_KEY") == ""

    assert os.environ.get("ISSUE_3957_CUSTOM_KEY") == "from-process-env"


def test_account_usage_subprocess_env_blocks_process_default_key(monkeypatch, tmp_path):
    """Readonly quota probes must not inherit process-default provider keys."""
    from api.providers import _account_usage_subprocess_env

    base = tmp_path / ".hermes"
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("OPENAI_API_KEY", "from-process-env")

    profiles.set_request_profile("work")
    try:
        with profiles.profile_env_for_active_request_readonly("quota probe"):
            env = _account_usage_subprocess_env(work_home, "openai", None)
    finally:
        profiles.clear_request_profile()

    assert env["HERMES_HOME"] == str(work_home)
    assert "OPENAI_API_KEY" not in env


def test_active_request_scope_installs_secret_scope(monkeypatch, tmp_path):
    """Inside readonly scope, agent.secret_scope sees profile env, not process env."""
    import types

    base = tmp_path / ".hermes"
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("OPENROUTER_API_KEY", "process-default-key")

    # Inject fake agent.secret_scope that records calls
    call_log = {}

    def fake_set_secret_scope(scope_dict):
        call_log["set_scope"] = dict(scope_dict)
        return "fake_token"

    def fake_reset_secret_scope(token):
        call_log["reset_called"] = True

    fake_secret_scope = types.ModuleType("agent.secret_scope")
    fake_secret_scope.set_secret_scope = fake_set_secret_scope
    fake_secret_scope.reset_secret_scope = fake_reset_secret_scope
    prev_agent = sys.modules.get("agent")
    prev_ss = sys.modules.get("agent.secret_scope")
    sys.modules["agent.secret_scope"] = fake_secret_scope
    sys.modules["agent"] = types.ModuleType("agent")

    profiles.set_request_profile("work")
    try:
        with profiles.profile_env_for_active_request_readonly("test"):
            pass
    finally:
        profiles.clear_request_profile()
        if prev_ss is not None:
            sys.modules["agent.secret_scope"] = prev_ss
        else:
            sys.modules.pop("agent.secret_scope", None)
        if prev_agent is not None:
            sys.modules["agent"] = prev_agent
        else:
            sys.modules.pop("agent", None)
        profiles._secret_scope_available = None

    # Verify the scope was set with profile env only
    assert "set_scope" in call_log
    assert "OPENROUTER_API_KEY" not in call_log["set_scope"]
    assert "HERMES_HOME" in call_log["set_scope"]
    # Verify reset was called
    assert call_log.get("reset_called") is True


def test_detached_worker_scope_installs_secret_scope(monkeypatch, tmp_path):
    """Inside detached worker scope, agent.secret_scope sees profile env, not process env."""
    import threading
    import types

    base = tmp_path / ".hermes"
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("OPENROUTER_API_KEY", "process-default-key")

    # Inject fake agent.secret_scope that records calls
    call_log = {}

    def fake_set_secret_scope(scope_dict):
        call_log["set_scope"] = dict(scope_dict)
        return "fake_token"

    def fake_reset_secret_scope(token):
        call_log["reset_called"] = True

    fake_secret_scope = types.ModuleType("agent.secret_scope")
    fake_secret_scope.set_secret_scope = fake_set_secret_scope
    fake_secret_scope.reset_secret_scope = fake_reset_secret_scope
    prev_agent = sys.modules.get("agent")
    prev_ss = sys.modules.get("agent.secret_scope")
    sys.modules["agent.secret_scope"] = fake_secret_scope
    sys.modules["agent"] = types.ModuleType("agent")

    result = {"scope_was_set": False}

    def worker_body():
        result["scope_was_set"] = "set_scope" in call_log

    # Capture the profile on the main thread
    profiles.set_request_profile("work")
    captured_profile = profiles.get_active_profile_name()
    try:
        with profiles.profile_scope_for_detached_worker(captured_profile):
            thread = threading.Thread(target=worker_body)
            thread.start()
            thread.join()
    finally:
        profiles.clear_request_profile()
        if prev_ss is not None:
            sys.modules["agent.secret_scope"] = prev_ss
        else:
            sys.modules.pop("agent.secret_scope", None)
        if prev_agent is not None:
            sys.modules["agent"] = prev_agent
        else:
            sys.modules.pop("agent", None)
        profiles._secret_scope_available = None

    # Verify the scope was set with profile env only
    assert "set_scope" in call_log
    assert "OPENROUTER_API_KEY" not in call_log["set_scope"]
    assert "HERMES_HOME" in call_log["set_scope"]
    # Verify reset was called
    assert call_log.get("reset_called") is True


def test_account_usage_subprocess_env_strips_bedrock_keys(monkeypatch, tmp_path):
    """Quota probes must not inherit AWS/Bedrock keys when block_process_env_fallback is set."""
    from api.providers import _account_usage_subprocess_env

    base = tmp_path / ".hermes"
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "aws-key-id")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")

    profiles.set_request_profile("work")
    try:
        with profiles.profile_env_for_active_request_readonly("quota probe"):
            env = _account_usage_subprocess_env(work_home, "bedrock", None)
    finally:
        profiles.clear_request_profile()

    assert env["HERMES_HOME"] == str(work_home)
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_account_usage_subprocess_env_strips_custom_key_env(monkeypatch, tmp_path):
    """Quota probes must strip custom provider key_env when block_process_env_fallback is set."""
    from api.providers import _account_usage_subprocess_env

    base = tmp_path / ".hermes"
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)

    # Create a config.yaml with a custom provider that has key_env
    config_yaml = work_home / "config.yaml"
    config_yaml.write_text(
        """
custom_providers:
  - key_env: MY_CUSTOM_API_KEY
"""
    )

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("MY_CUSTOM_API_KEY", "custom-secret")

    profiles.set_request_profile("work")
    try:
        with profiles.profile_env_for_active_request_readonly("quota probe"):
            env = _account_usage_subprocess_env(work_home, "openai", None)
    finally:
        profiles.clear_request_profile()

    assert env["HERMES_HOME"] == str(work_home)
    assert "MY_CUSTOM_API_KEY" not in env


def test_account_usage_subprocess_env_strips_anthropic_token_aliases(monkeypatch, tmp_path):
    """Quota probes must not inherit the process-default Anthropic OAuth/token env
    vars (ANTHROPIC_TOKEN / CLAUDE_CODE_OAUTH_TOKEN) for an empty named profile.

    These are agent-runtime credential env vars absent from the WebUI's settable
    _PROVIDER_ENV_VAR map, so the strip set must derive them from the agent
    registry — otherwise the anthropic quota subprocess resolves them via
    resolve_anthropic_token() and leaks the server-process credential (#3961)."""
    from api.providers import _account_usage_subprocess_env

    base = tmp_path / ".hermes"
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("ANTHROPIC_TOKEN", "process-default-anthropic-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "process-default-oauth-token")

    profiles.set_request_profile("work")
    try:
        with profiles.profile_env_for_active_request_readonly("quota probe"):
            env = _account_usage_subprocess_env(work_home, "anthropic", None)
    finally:
        profiles.clear_request_profile()

    assert env["HERMES_HOME"] == str(work_home)
    assert "ANTHROPIC_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_detached_worker_scope_scrubs_anthropic_token_aliases(monkeypatch, tmp_path):
    """Detached/sync model-rebuild scope must scrub the process-default Anthropic
    OAuth/token env vars too — verified agent model code can resolve Anthropic
    models through raw os.getenv() of these names, so an empty named profile
    must not see the server-process token (#3961 detached-worker leak)."""
    base = tmp_path / ".hermes"
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("ANTHROPIC_TOKEN", "process-default-anthropic-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "process-default-oauth-token")

    with profiles.profile_scope_for_detached_worker("work", "test-worker"):
        assert os.environ.get("ANTHROPIC_TOKEN") is None
        assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") is None
        assert config._thread_local_env_value("ANTHROPIC_TOKEN") == ""
        assert config._thread_local_env_value("CLAUDE_CODE_OAUTH_TOKEN") == ""

    assert os.environ.get("ANTHROPIC_TOKEN") == "process-default-anthropic-token"
    assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "process-default-oauth-token"


def test_account_usage_subprocess_env_strips_non_registry_agent_creds(monkeypatch, tmp_path):
    """Quota probes must not inherit process-default credential env vars the agent
    resolves via raw os.getenv() but that are NOT in the auth registry — the
    generic CUSTOM_API_KEY and the AWS/Bedrock credential family. Otherwise a
    custom/AWS-backed provider quota probe leaks the server-process credential
    to an empty named profile (#3961 residual leak class)."""
    from api.providers import _account_usage_subprocess_env

    base = tmp_path / ".hermes"
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("CUSTOM_API_KEY", "process-default-custom-key")
    monkeypatch.setenv("AWS_PROFILE", "process-default-aws-profile")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "process-default-bedrock-token")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://169.254.170.2/creds")

    profiles.set_request_profile("work")
    try:
        with profiles.profile_env_for_active_request_readonly("quota probe"):
            env = _account_usage_subprocess_env(work_home, "openai", None)
    finally:
        profiles.clear_request_profile()

    assert env["HERMES_HOME"] == str(work_home)
    assert "CUSTOM_API_KEY" not in env
    assert "AWS_PROFILE" not in env
    assert "AWS_BEARER_TOKEN_BEDROCK" not in env
    assert "AWS_CONTAINER_CREDENTIALS_FULL_URI" not in env


def test_detached_worker_scope_scrubs_non_registry_agent_creds(monkeypatch, tmp_path):
    """Detached/sync model-rebuild scope must scrub the non-registry agent
    credential env vars (CUSTOM_API_KEY, AWS/Bedrock family) too — the agent's
    custom-provider and bedrock-adapter paths resolve them via raw os.getenv(),
    so an empty named profile must not see the server-process value (#3961)."""
    base = tmp_path / ".hermes"
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("CUSTOM_API_KEY", "process-default-custom-key")
    monkeypatch.setenv("AWS_PROFILE", "process-default-aws-profile")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "process-default-aws-secret")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "process-default-azure-secret")
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "process-default-foundry-key")
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://169.254.169.254/msi")
    monkeypatch.setenv("MSI_ENDPOINT", "http://169.254.169.254/msi")

    with profiles.profile_scope_for_detached_worker("work", "test-worker"):
        assert os.environ.get("CUSTOM_API_KEY") is None
        assert os.environ.get("AWS_PROFILE") is None
        assert os.environ.get("AWS_SECRET_ACCESS_KEY") is None
        assert os.environ.get("AZURE_CLIENT_SECRET") is None
        assert os.environ.get("AZURE_FOUNDRY_API_KEY") is None
        assert os.environ.get("IDENTITY_ENDPOINT") is None
        assert os.environ.get("MSI_ENDPOINT") is None
        assert config._thread_local_env_value("CUSTOM_API_KEY") == ""

    assert os.environ.get("CUSTOM_API_KEY") == "process-default-custom-key"
    assert os.environ.get("AWS_PROFILE") == "process-default-aws-profile"
    assert os.environ.get("AWS_SECRET_ACCESS_KEY") == "process-default-aws-secret"
    assert os.environ.get("AZURE_CLIENT_SECRET") == "process-default-azure-secret"
    assert os.environ.get("AZURE_FOUNDRY_API_KEY") == "process-default-foundry-key"
    assert os.environ.get("IDENTITY_ENDPOINT") == "http://169.254.169.254/msi"
    assert os.environ.get("MSI_ENDPOINT") == "http://169.254.169.254/msi"



def test_default_detached_worker_isolates_from_named_active_profile(monkeypatch, tmp_path):
    """Default-scoped detached worker must resolve default, not named active (#6326).

    When the process-wide active profile is a named profile (e.g. 'work'),
    a detached worker spawned for the 'default' profile must bind the
    request-profile TLS so that get_active_profile_name() returns 'default',
    not 'work', AND must set block_process_env_fallback on the worker's
    TLS so _thread_local_env_value() callers cannot see named-profile
    credentials (#6327).  The process-wide os.environ is NOT mutated
    (that would break concurrent named-profile requests).
    """
    import threading

    base = tmp_path / ".hermes"
    (base / "profiles" / "work").mkdir(parents=True)
    (base / "profiles" / "work" / ".env").write_text(
        "ISSUE_3957_WPROBE=worker-env\nOPENROUTER_API_KEY=named-profile-credential-leaked\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    monkeypatch.delenv("ISSUE_3957_WPROBE", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    # Load the named profile's .env into os.environ so the worker thread
    # can observe it before and after the scope (#6327).
    profiles._reload_dotenv(base / "profiles" / "work")

    # Simulate process-wide active profile == 'work'
    profiles._active_profile = "work"
    try:
        out: dict = {}
        worker_exc: list[BaseException] = []

        def worker():
            try:
                # On this fresh thread, get_active_profile_name() returns 'work'
                # (process-wide active) because no TLS is set yet.
                out["before"] = profiles.get_active_profile_name()
                with profiles.profile_scope_for_detached_worker("default", "test"):
                    out["inside"] = profiles.get_active_profile_name()
                    out["inside_env"] = os.environ.get("ISSUE_3957_WPROBE")
                out["after"] = profiles.get_active_profile_name()
            except BaseException as exc:
                worker_exc.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        if worker_exc:
            raise worker_exc[0]

        # Before scope: process-wide active 'work' is visible on the new thread.
        assert out["before"] == "work"
        # Inside scope: TLS bound to 'default' overrides process-wide 'work'.
        assert out["inside"] == "default"
        # Env mirroring skipped for root — work's .env remains visible via
        # raw os.getenv() (only TLS-aware _thread_local_env_value() readers
        # are blocked by block_process_env_fallback).
        # After scope: TLS cleared, falls back to process-wide 'work'.
        assert out["after"] == "work"
    finally:
        profiles._active_profile = "default"
        # Clean up the .env state loaded by _reload_dotenv so it does not
        # leak into subsequent tests.
        profiles._loaded_profile_env_keys.discard("OPENROUTER_API_KEY")
        profiles._loaded_profile_env_keys.discard("ISSUE_3957_WPROBE")
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ.pop("ISSUE_3957_WPROBE", None)

def test_default_detached_worker_preserves_root_credentials_when_root_active(monkeypatch):
    """Default-scoped worker preserves root's own credentials when root is active (#6327).

    When the process-wide active profile IS the root/default profile (the common
    single-profile deployment), the scope must NOT mutate os.environ — root's
    own credentials survive intact inside and after the scope.
    """
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))

    # Simulate root profile having loaded its own .env credentials.
    monkeypatch.setenv("OPENROUTER_API_KEY", "root-own-key")

    # Process-wide active profile is root (default state).
    profiles._active_profile = "default"
    try:
        with profiles.profile_scope_for_detached_worker("default", "test"):
            assert profiles.get_active_profile_name() == "default"
            # Root's own credential must survive inside the default scope.
            assert os.environ.get("OPENROUTER_API_KEY") == "root-own-key"
        # Credential intact after scope exit too.
        assert os.environ.get("OPENROUTER_API_KEY") == "root-own-key"
    finally:
        profiles._active_profile = "default"


def test_expand_env_vars_does_not_leak_process_env_under_block_scope(monkeypatch):
    """Config ${VAR} expansion must not reconstruct a server-process credential
    for a profile-scoped readonly/background read (#3961 config-template vector).

    A profile config.yaml of e.g. `api_key: ${ANTHROPIC_TOKEN}` previously
    expanded via raw os.environ in _expand_env_vars, so _get_provider_api_key
    could rebuild the process token and pass it through even when the scrub
    stripped the child env. The expansion now routes through the thread-local
    accessor and refuses the process-env fallback when block_process_env_fallback
    is set."""
    monkeypatch.setenv("ANTHROPIC_TOKEN", "process-default-anthropic-token")

    # No active scope: normal behavior — expands from the process env.
    assert config._expand_env_vars({"api_key": "${ANTHROPIC_TOKEN}"}) == {
        "api_key": "process-default-anthropic-token"
    }

    # Profile-scoped readonly/background scope with no profile value for the var:
    # must NOT fall back to the process env (leaves the reference unexpanded).
    prev_block = getattr(config._thread_ctx, "block_process_env_fallback", False)
    prev_env = getattr(config._thread_ctx, "env", None)
    config._thread_ctx.block_process_env_fallback = True
    config._thread_ctx.env = {}
    try:
        assert config._expand_env_vars({"api_key": "${ANTHROPIC_TOKEN}"}) == {
            "api_key": "${ANTHROPIC_TOKEN}"
        }
        # A value present in the profile's thread-local env IS used (own value).
        config._thread_ctx.env = {"ANTHROPIC_TOKEN": "profile-own-token"}
        assert config._expand_env_vars({"api_key": "${ANTHROPIC_TOKEN}"}) == {
            "api_key": "profile-own-token"
        }
    finally:
        config._thread_ctx.block_process_env_fallback = prev_block
        if prev_env is None:
            config._thread_ctx.env = {}
        else:
            config._thread_ctx.env = prev_env

