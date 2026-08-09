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
import threading
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
    active profile is a named profile, AND must install root-owned
    thread/context credentials while blocking the process-env fallback so
    _thread_local_env_value() callers resolve the root credential — never a
    named-profile credential (#6327).  Raw os.getenv() readers must not see
    named-profile .env values for the duration of the body; they are scrubbed
    (values proven foreign to root) and restored afterwards.
    """
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    monkeypatch.delenv("ISSUE_3957_WPROBE", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    # Simulate a named profile having loaded its .env credentials (#6327):
    # the registry must record those keys as owned by a NAMED profile so the
    # root scope can prove they are foreign to root.
    profiles._loaded_profile_env_keys.add("OPENROUTER_API_KEY")
    profiles._loaded_profile_env_owner["OPENROUTER_API_KEY"] = "work"
    os.environ["OPENROUTER_API_KEY"] = "named-profile-credential-leaked"

    # Set process-wide active to a named profile to verify TLS overrides it.
    profiles._active_profile = "work"
    try:
        # Default name → TLS bound, root thread env installed, raw channel
        # scrubbed for the body.
        with profiles.profile_scope_for_detached_worker("default", "test"):
            assert profiles.get_active_profile_name() == "default"
            assert os.environ.get("ISSUE_3957_WPROBE") is None
            # Named profile credential is NOT visible inside default scope (#6327).
            assert os.environ.get("OPENROUTER_API_KEY") is None
        # Credential restored after scope exit.
        assert os.environ.get("OPENROUTER_API_KEY") == "named-profile-credential-leaked"
    finally:
        profiles._active_profile = "default"
        profiles._loaded_profile_env_keys.discard("OPENROUTER_API_KEY")
        profiles._loaded_profile_env_owner.pop("OPENROUTER_API_KEY", None)
        os.environ.pop("OPENROUTER_API_KEY", None)
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
    not 'work', AND must install root-owned thread/context credentials while
    closing the raw os.getenv() channel: the named profile's .env values
    (proven foreign to root) are scrubbed from os.environ for the worker body
    and restored afterwards (#6327).  Without this fix, the worker would
    resolve 'work's provider/model/credentials — breaking profile isolation.
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

    # Load the named profile's .env into os.environ and record its ownership
    # so the root scope can prove those values are foreign to root (#6327).
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
                out["before_key"] = os.environ.get("OPENROUTER_API_KEY")
                with profiles.profile_scope_for_detached_worker("default", "test"):
                    out["inside"] = profiles.get_active_profile_name()
                    out["inside_env"] = os.environ.get("ISSUE_3957_WPROBE")
                    out["inside_key"] = os.environ.get("OPENROUTER_API_KEY")
                out["after"] = profiles.get_active_profile_name()
                out["after_key"] = os.environ.get("OPENROUTER_API_KEY")
            except BaseException as exc:
                worker_exc.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        if worker_exc:
            raise worker_exc[0]

        # Before scope: process-wide active 'work' is visible on the new thread.
        assert out["before"] == "work"
        # Named profile credential IS visible before the scope (the leaked state).
        assert out["before_key"] == "named-profile-credential-leaked"
        # Inside scope: TLS bound to 'default' overrides process-wide 'work'.
        assert out["inside"] == "default"
        # Raw os.getenv() channel closed for the root worker body: the named
        # profile's .env values (proven foreign to root) are scrubbed (#6327).
        assert out["inside_env"] is None
        assert out["inside_key"] is None
        # After scope: TLS cleared, falls back to process-wide 'work', and the
        # scrubbed named credential is restored (no stale reinsertion).
        assert out["after"] == "work"
        assert out["after_key"] == "named-profile-credential-leaked"
    finally:
        profiles._active_profile = "default"
        # Clean up the .env state loaded by _reload_dotenv so it does not
        # leak into subsequent tests.
        profiles._loaded_profile_env_keys.discard("OPENROUTER_API_KEY")
        profiles._loaded_profile_env_keys.discard("ISSUE_3957_WPROBE")
        profiles._loaded_profile_env_owner.pop("OPENROUTER_API_KEY", None)
        profiles._loaded_profile_env_owner.pop("ISSUE_3957_WPROBE", None)
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ.pop("ISSUE_3957_WPROBE", None)

def test_default_detached_worker_preserves_root_credentials_when_root_active(monkeypatch, tmp_path):
    """Default-scoped worker preserves root's own credentials when root is active (#6327).

    Loads a REAL root ``.env`` through ``_reload_dotenv()`` and asserts BOTH
    the raw ``os.getenv()`` channel AND the TLS-aware
    ``_thread_local_env_value()`` / provider behavior preserve the root
    credential inside the default worker.  The root scope must install the
    canonical root runtime/thread env BEFORE blocking process fallback, and
    must NOT scrub root-owned keys (owner == "" in the ownership registry).
    """
    import threading

    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    (base / ".env").write_text(
        "OPENROUTER_API_KEY=root-own-key\nISSUE_3957_ROOT_PROBE=root-probe-value\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ISSUE_3957_ROOT_PROBE", raising=False)

    # Load the REAL root .env into os.environ; ownership registry must record
    # these keys as root-owned (owner == "") so they are never scrubbed.
    profiles._reload_dotenv(base)
    assert profiles._loaded_profile_env_owner.get("OPENROUTER_API_KEY") == ""
    assert profiles._loaded_profile_env_owner.get("ISSUE_3957_ROOT_PROBE") == ""
    assert os.environ.get("OPENROUTER_API_KEY") == "root-own-key"

    # Process-wide active profile is root (default state).
    profiles._active_profile = "default"
    try:
        out: dict = {}
        worker_exc: list[BaseException] = []

        def worker():
            try:
                with profiles.profile_scope_for_detached_worker("default", "test"):
                    out["name"] = profiles.get_active_profile_name()
                    # Raw os.getenv() channel preserves the root credential.
                    out["raw"] = os.environ.get("OPENROUTER_API_KEY")
                    out["raw_probe"] = os.environ.get("ISSUE_3957_ROOT_PROBE")
                    # TLS-aware channel resolves it from the installed root
                    # thread env (not the empty default).
                    out["tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
                    out["tls_probe"] = config._thread_local_env_value(
                        "ISSUE_3957_ROOT_PROBE"
                    )
                    out["blocked"] = getattr(
                        config._thread_ctx, "block_process_env_fallback", False
                    )
                    # Provider behavior: ${VAR} expansion in a custom provider
                    # config must resolve the root credential via the
                    # TLS-aware path inside the worker.
                    out["expanded"] = config._expand_env_vars(
                        {"api_key": "${OPENROUTER_API_KEY}"}
                    )
                # Restored after scope exit: raw still present, TLS cleared.
                out["after_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["after_blocked"] = getattr(
                    config._thread_ctx, "block_process_env_fallback", False
                )
                out["after_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
            except BaseException as exc:
                worker_exc.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        if worker_exc:
            raise worker_exc[0]

        # Credential intact inside the default scope (raw + TLS + provider).
        assert out["name"] == "default"
        assert out["raw"] == "root-own-key"
        assert out["raw_probe"] == "root-probe-value"
        assert out["tls"] == "root-own-key"
        assert out["tls_probe"] == "root-probe-value"
        assert out["blocked"] is True
        assert out["expanded"] == {"api_key": "root-own-key"}
        # Credential intact after scope exit too; TLS + block flag restored.
        assert out["after_raw"] == "root-own-key"
        assert out["after_blocked"] is False
        assert out["after_tls"] == "root-own-key"
    finally:
        profiles._active_profile = "default"
        # Clean up the .env state loaded by _reload_dotenv so it does not
        # leak into subsequent tests.
        profiles._loaded_profile_env_keys.discard("OPENROUTER_API_KEY")
        profiles._loaded_profile_env_keys.discard("ISSUE_3957_ROOT_PROBE")
        profiles._loaded_profile_env_owner.pop("OPENROUTER_API_KEY", None)
        profiles._loaded_profile_env_owner.pop("ISSUE_3957_ROOT_PROBE", None)
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ.pop("ISSUE_3957_ROOT_PROBE", None)


def test_detached_worker_named_root_overlap_named_first(monkeypatch, tmp_path):
    """Barrier-controlled named→root overlap: same key, different values (#6327).

    The named profile's .env carries OPENROUTER_API_KEY=named-value and the
    root profile's .env carries OPENROUTER_API_KEY=root-value.  The named
    scope enters FIRST; while it holds the scope, a root worker must NOT see
    the named value through raw os.getenv() (the ownership protocol serializes
    the process-env mutation + raw-read body), and after the named scope
    exits the root worker must resolve the ROOT value — never the named one.
    """
    import threading

    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    (base / ".env").write_text("OPENROUTER_API_KEY=root-value\n", encoding="utf-8")
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    (work_home / ".env").write_text(
        "OPENROUTER_API_KEY=named-value\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    # Load the NAMED profile's .env into os.environ (process-wide active =
    # named profile), recording ownership so the root scope can prove the
    # value is foreign.
    profiles._reload_dotenv(work_home)
    assert profiles._loaded_profile_env_owner.get("OPENROUTER_API_KEY") == "work"
    assert os.environ.get("OPENROUTER_API_KEY") == "named-value"
    profiles._active_profile = "work"

    named_entered = threading.Event()
    release_named = threading.Event()
    root_done = threading.Event()
    out: dict = {}
    worker_exc: list[BaseException] = []

    def named_worker():
        try:
            with profiles.profile_scope_for_detached_worker("work", "test"):
                named_entered.set()
                out["named_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["named_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
                # Hold the scope until the main thread has started the root
                # worker and confirmed it is blocked on the ownership lock.
                assert release_named.wait(5)
        except BaseException as exc:
            worker_exc.append(exc)

    def root_worker():
        try:
            with profiles.profile_scope_for_detached_worker("default", "test"):
                out["root_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["root_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
        except BaseException as exc:
            worker_exc.append(exc)
        finally:
            root_done.set()

    t_named = threading.Thread(target=named_worker)
    t_root = threading.Thread(target=root_worker)
    t_named.start()
    assert named_entered.wait(5)
    # Start the root worker while the named scope is active.  The ownership
    # lock serializes the process-env mutation + raw-read body, so the root
    # worker must NOT be able to enter (or see the named value) yet.
    t_root.start()
    import time as _time

    _time.sleep(0.1)
    assert not root_done.is_set()
    release_named.set()
    t_named.join(5)
    t_root.join(5)
    if worker_exc:
        raise worker_exc[0]

    # Named worker saw its own value on both channels.
    assert out["named_raw"] == "named-value"
    assert out["named_tls"] == "named-value"
    # Root worker, after the named scope exited, resolved the ROOT value via
    # the TLS-aware channel — never the named value.
    assert out["root_tls"] == "root-value"
    # Raw channel inside the root worker is scrubbed (named value proven
    # foreign to root) — the raw read must NOT see the named credential.
    assert out["root_raw"] is None
    # Process env restored to the pre-scope named state (no stale values).
    assert os.environ.get("OPENROUTER_API_KEY") == "named-value"
    profiles._active_profile = "default"
    profiles._loaded_profile_env_keys.discard("OPENROUTER_API_KEY")
    profiles._loaded_profile_env_owner.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("OPENROUTER_API_KEY", None)


def test_detached_worker_named_root_overlap_root_first(monkeypatch, tmp_path):
    """Barrier-controlled root→named overlap: same key, different values (#6327).

    Mirrors test_detached_worker_named_root_overlap_named_first but the ROOT
    scope enters FIRST.  While the root worker is active (holding the
    ownership lock), the named worker must not be able to enter or expose the
    named value; after the root scope exits, the named worker resolves the
    NAMED value on both channels.
    """
    import threading
    import time as _time

    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    (base / ".env").write_text("OPENROUTER_API_KEY=root-value\n", encoding="utf-8")
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    (work_home / ".env").write_text(
        "OPENROUTER_API_KEY=named-value\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    profiles._reload_dotenv(work_home)
    assert os.environ.get("OPENROUTER_API_KEY") == "named-value"
    profiles._active_profile = "work"

    root_entered = threading.Event()
    release_root = threading.Event()
    named_done = threading.Event()
    out: dict = {}
    worker_exc: list[BaseException] = []

    def root_worker():
        try:
            with profiles.profile_scope_for_detached_worker("default", "test"):
                root_entered.set()
                out["root_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["root_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
                assert release_root.wait(5)
        except BaseException as exc:
            worker_exc.append(exc)

    def named_worker():
        try:
            with profiles.profile_scope_for_detached_worker("work", "test"):
                out["named_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["named_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
        except BaseException as exc:
            worker_exc.append(exc)
        finally:
            named_done.set()

    t_root = threading.Thread(target=root_worker)
    t_named = threading.Thread(target=named_worker)
    t_root.start()
    assert root_entered.wait(5)
    # Start the named worker while the root scope is active.  The ownership
    # lock serializes the process-env mutation + raw-read body, so the named
    # worker must NOT enter while the root worker holds the scope.
    t_named.start()
    _time.sleep(0.1)
    assert not named_done.is_set()
    release_root.set()
    t_root.join(5)
    t_named.join(5)
    if worker_exc:
        raise worker_exc[0]

    # Root worker resolved the root value (raw scrubbed of the named value).
    assert out["root_tls"] == "root-value"
    assert out["root_raw"] is None
    # Named worker, after the root scope exited, resolved the NAMED value.
    assert out["named_raw"] == "named-value"
    assert out["named_tls"] == "named-value"
    assert os.environ.get("OPENROUTER_API_KEY") == "named-value"
    profiles._active_profile = "default"
    profiles._loaded_profile_env_keys.discard("OPENROUTER_API_KEY")
    profiles._loaded_profile_env_owner.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("OPENROUTER_API_KEY", None)


def test_detached_worker_root_scope_restores_on_exception(monkeypatch, tmp_path):
    """Root worker restores os.environ / TLS / fallback state on exception (#6327).

    A real root .env is loaded through _reload_dotenv(); a named profile's
    .env is loaded into os.environ as the foreign (leaked) state.  An
    exception raised inside the default worker body must NOT leave the scrubbed
    named value missing, must restore the root credential, and must restore the
    thread env + block_process_env_fallback on the worker thread.
    """
    import threading

    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    (base / ".env").write_text("OPENROUTER_API_KEY=root-value\n", encoding="utf-8")
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    (work_home / ".env").write_text(
        "OPENROUTER_API_KEY=named-value\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    profiles._reload_dotenv(work_home)
    assert os.environ.get("OPENROUTER_API_KEY") == "named-value"
    profiles._active_profile = "work"

    out: dict = {}
    worker_exc: list[BaseException] = []

    def worker():
        try:
            with profiles.profile_scope_for_detached_worker("default", "test"):
                # Raw channel scrubbed while inside the body.
                out["inside_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["inside_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
                raise RuntimeError("boom")
        except RuntimeError:
            pass  # expected — restoration must still happen
        except BaseException as exc:
            worker_exc.append(exc)
        # After the exception propagated through the scope's finally:
        out["after_raw"] = os.environ.get("OPENROUTER_API_KEY")
        out["after_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
        out["after_blocked"] = getattr(
            config._thread_ctx, "block_process_env_fallback", False
        )

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    if worker_exc:
        raise worker_exc[0]

    assert out["inside_raw"] is None  # foreign value scrubbed during the body
    assert out["inside_tls"] == "root-value"  # root thread env installed
    # Exception restoration: named value reinserted, no stale overwrite.
    assert out["after_raw"] == "named-value"
    # TLS-aware read after restore: root credential preserved via process env.
    assert out["after_tls"] == "named-value"
    assert out["after_blocked"] is False
    assert os.environ.get("OPENROUTER_API_KEY") == "named-value"
    profiles._active_profile = "default"
    profiles._loaded_profile_env_keys.discard("OPENROUTER_API_KEY")
    profiles._loaded_profile_env_owner.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("OPENROUTER_API_KEY", None)


def test_detached_worker_root_scope_no_stale_reinsertion(monkeypatch, tmp_path):
    """Root worker restore never reinserts a value the body itself changed (#6327).

    A named profile's .env (foreign to root) is scrubbed for the body, then
    the body overwrites that key with its own value.  On exit the scope must
    NOT reinsert the stale foreign value over the body's write — restoration
    only reinserts keys that are still absent.
    """
    import threading

    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    (base / ".env").write_text("OPENROUTER_API_KEY=root-value\n", encoding="utf-8")
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    (work_home / ".env").write_text(
        "OPENROUTER_API_KEY=named-value\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    profiles._reload_dotenv(work_home)
    profiles._active_profile = "work"

    out: dict = {}
    worker_exc: list[BaseException] = []

    def worker():
        try:
            with profiles.profile_scope_for_detached_worker("default", "test"):
                out["inside_raw"] = os.environ.get("OPENROUTER_API_KEY")
                # The worker body itself writes the key with a new value.
                os.environ["OPENROUTER_API_KEY"] = "body-wrote-value"
            out["after_raw"] = os.environ.get("OPENROUTER_API_KEY")
        except BaseException as exc:
            worker_exc.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    if worker_exc:
        raise worker_exc[0]

    assert out["inside_raw"] is None
    # No stale reinsertion/overwrite: the body's own value survives the exit.
    assert out["after_raw"] == "body-wrote-value"
    profiles._active_profile = "default"
    profiles._loaded_profile_env_keys.discard("OPENROUTER_API_KEY")
    profiles._loaded_profile_env_owner.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("OPENROUTER_API_KEY", None)


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



# ─────────────────────────────────────────────────────────────────────────────
# #6327 — ONE shared full-body process-env ownership lock across entrypoints
# ─────────────────────────────────────────────────────────────────────────────


def test_background_worker_direct_overlap_detached_direct_first(monkeypatch, tmp_path):
    """Direct background-worker scope overlapping a detached named scope (#6327).

    A DIRECT ``profile_env_for_background_worker`` scope (alpha) enters first
    and holds the shared full-body ownership lock.  A detached
    ``profile_scope_for_detached_worker`` (beta) started mid-body must NOT
    enter early; after the direct scope exits the detached worker must resolve
    ITS value on both the raw ``os.getenv()`` and TLS channels, and the
    ambient process env must be restored exactly (no stale alpha/beta value).
    """
    import threading
    import time as _time

    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    (base / ".env").write_text("OPENROUTER_API_KEY=root-value\n", encoding="utf-8")
    for prof, val in (("alpha", "alpha-value"), ("beta", "beta-value")):
        home = base / "profiles" / prof
        home.mkdir(parents=True)
        (home / ".env").write_text(f"OPENROUTER_API_KEY={val}\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-value")

    direct_entered = threading.Event()
    release_direct = threading.Event()
    detached_done = threading.Event()
    out: dict = {}
    worker_exc: list[BaseException] = []

    def direct_worker():
        try:
            with profiles.profile_env_for_background_worker("alpha", "test"):
                direct_entered.set()
                out["direct_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["direct_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
                assert release_direct.wait(5)
        except BaseException as exc:
            worker_exc.append(exc)

    def detached_worker():
        try:
            with profiles.profile_scope_for_detached_worker("beta", "test"):
                out["detached_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["detached_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
        except BaseException as exc:
            worker_exc.append(exc)
        finally:
            detached_done.set()

    t_direct = threading.Thread(target=direct_worker)
    t_detached = threading.Thread(target=detached_worker)
    t_direct.start()
    assert direct_entered.wait(5)
    # Start the detached scope while the DIRECT scope is mid-body.  The shared
    # ownership lock must serialize the full body — no early entry.
    t_detached.start()
    _time.sleep(0.1)
    assert not detached_done.is_set()
    release_direct.set()
    t_direct.join(5)
    t_detached.join(5)
    if worker_exc:
        raise worker_exc[0]

    # Direct scope saw its own value on both channels throughout.
    assert out["direct_raw"] == "alpha-value"
    assert out["direct_tls"] == "alpha-value"
    # Detached scope, after the direct scope exited, resolved the BETA value.
    assert out["detached_raw"] == "beta-value"
    assert out["detached_tls"] == "beta-value"
    # Exact ambient restoration — no stale alpha/beta value left behind.
    assert os.environ.get("OPENROUTER_API_KEY") == "ambient-value"


def test_background_worker_direct_overlap_detached_detached_first(monkeypatch, tmp_path):
    """Detached named scope overlapping a direct background-worker scope (#6327).

    Mirrors test_background_worker_direct_overlap_detached_direct_first with
    the DETACHED scope entering first: while it holds the shared ownership
    lock the direct scope must not enter early, and after the detached scope
    exits the direct scope must resolve ITS value on both channels with exact
    ambient restoration.
    """
    import threading
    import time as _time

    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    (base / ".env").write_text("OPENROUTER_API_KEY=root-value\n", encoding="utf-8")
    for prof, val in (("alpha", "alpha-value"), ("beta", "beta-value")):
        home = base / "profiles" / prof
        home.mkdir(parents=True)
        (home / ".env").write_text(f"OPENROUTER_API_KEY={val}\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-value")

    detached_entered = threading.Event()
    release_detached = threading.Event()
    direct_done = threading.Event()
    out: dict = {}
    worker_exc: list[BaseException] = []

    def detached_worker():
        try:
            with profiles.profile_scope_for_detached_worker("beta", "test"):
                detached_entered.set()
                out["detached_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["detached_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
                assert release_detached.wait(5)
        except BaseException as exc:
            worker_exc.append(exc)

    def direct_worker():
        try:
            with profiles.profile_env_for_background_worker("alpha", "test"):
                out["direct_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["direct_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
        except BaseException as exc:
            worker_exc.append(exc)
        finally:
            direct_done.set()

    t_detached = threading.Thread(target=detached_worker)
    t_direct = threading.Thread(target=direct_worker)
    t_detached.start()
    assert detached_entered.wait(5)
    # Start the direct scope while the detached scope is mid-body.  The shared
    # ownership lock must block it — no early entry.
    t_direct.start()
    _time.sleep(0.1)
    assert not direct_done.is_set()
    release_detached.set()
    t_detached.join(5)
    t_direct.join(5)
    if worker_exc:
        raise worker_exc[0]

    # Detached scope saw its own value on both channels throughout.
    assert out["detached_raw"] == "beta-value"
    assert out["detached_tls"] == "beta-value"
    # Direct scope, after the detached scope exited, resolved the ALPHA value.
    assert out["direct_raw"] == "alpha-value"
    assert out["direct_tls"] == "alpha-value"
    # Exact ambient restoration.
    assert os.environ.get("OPENROUTER_API_KEY") == "ambient-value"


def test_active_request_mirrored_overlap_detached_root_active_first(monkeypatch, tmp_path):
    """Active-request mirrored scope overlapping a detached ROOT scope (#6327).

    The active-request scope (``profile_env_for_active_request`` → the shared
    ``profile_env_for_background_worker`` path) enters first for the named
    "work" profile; a detached DEFAULT/root worker started mid-body must not
    enter early.  After the active scope exits, the root worker resolves the
    ROOT credential via TLS (raw channel scrubbed of the named value), and the
    ambient named state is restored exactly.
    """
    import threading
    import time as _time

    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    (base / ".env").write_text("OPENROUTER_API_KEY=root-value\n", encoding="utf-8")
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    (work_home / ".env").write_text(
        "OPENROUTER_API_KEY=active-value\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    # Load the named profile's .env as the process-wide (ambient) state and
    # record ownership so the root scope can prove the value is foreign.
    profiles._reload_dotenv(work_home)
    assert profiles._loaded_profile_env_owner.get("OPENROUTER_API_KEY") == "work"
    assert os.environ.get("OPENROUTER_API_KEY") == "active-value"
    profiles._active_profile = "work"

    active_entered = threading.Event()
    release_active = threading.Event()
    detached_done = threading.Event()
    out: dict = {}
    worker_exc: list[BaseException] = []

    def active_worker():
        profiles.set_request_profile("work")
        try:
            with profiles.profile_env_for_active_request("test"):
                active_entered.set()
                out["active_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["active_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
                assert release_active.wait(5)
        except BaseException as exc:
            worker_exc.append(exc)
        finally:
            profiles.clear_request_profile()

    def detached_worker():
        try:
            with profiles.profile_scope_for_detached_worker("default", "test"):
                out["detached_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["detached_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
        except BaseException as exc:
            worker_exc.append(exc)
        finally:
            detached_done.set()

    t_active = threading.Thread(target=active_worker)
    t_detached = threading.Thread(target=detached_worker)
    t_active.start()
    assert active_entered.wait(5)
    # Start the root detached worker while the active-request scope is
    # mid-body.  The shared ownership lock must block it — no early entry.
    t_detached.start()
    _time.sleep(0.1)
    assert not detached_done.is_set()
    release_active.set()
    t_active.join(5)
    t_detached.join(5)
    if worker_exc:
        raise worker_exc[0]

    # Active-request scope saw the NAMED value on both channels throughout.
    assert out["active_raw"] == "active-value"
    assert out["active_tls"] == "active-value"
    # Root worker, after the active scope exited, resolved the ROOT value via
    # TLS; the raw channel is scrubbed of the foreign named value.
    assert out["detached_tls"] == "root-value"
    assert out["detached_raw"] is None
    # Exact ambient restoration (the named process-wide state).
    assert os.environ.get("OPENROUTER_API_KEY") == "active-value"
    profiles._active_profile = "default"
    profiles._loaded_profile_env_keys.discard("OPENROUTER_API_KEY")
    profiles._loaded_profile_env_owner.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("OPENROUTER_API_KEY", None)


def test_active_request_mirrored_overlap_detached_root_detached_first(monkeypatch, tmp_path):
    """Detached ROOT scope overlapping an active-request mirrored scope (#6327).

    Mirrors test_active_request_mirrored_overlap_detached_root_active_first
    with the detached DEFAULT/root worker entering first: while it holds the
    shared ownership lock the active-request scope must not enter early, and
    after the root worker exits the active scope resolves the NAMED value on
    both channels with exact ambient restoration.
    """
    import threading
    import time as _time

    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    (base / ".env").write_text("OPENROUTER_API_KEY=root-value\n", encoding="utf-8")
    work_home = base / "profiles" / "work"
    work_home.mkdir(parents=True)
    (work_home / ".env").write_text(
        "OPENROUTER_API_KEY=active-value\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    profiles._reload_dotenv(work_home)
    assert os.environ.get("OPENROUTER_API_KEY") == "active-value"
    profiles._active_profile = "work"

    detached_entered = threading.Event()
    release_detached = threading.Event()
    active_done = threading.Event()
    out: dict = {}
    worker_exc: list[BaseException] = []

    def detached_worker():
        try:
            with profiles.profile_scope_for_detached_worker("default", "test"):
                detached_entered.set()
                out["detached_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["detached_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
                assert release_detached.wait(5)
        except BaseException as exc:
            worker_exc.append(exc)

    def active_worker():
        profiles.set_request_profile("work")
        try:
            with profiles.profile_env_for_active_request("test"):
                out["active_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["active_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
        except BaseException as exc:
            worker_exc.append(exc)
        finally:
            profiles.clear_request_profile()
            active_done.set()

    t_detached = threading.Thread(target=detached_worker)
    t_active = threading.Thread(target=active_worker)
    t_detached.start()
    assert detached_entered.wait(5)
    # Start the active-request scope while the root detached scope is
    # mid-body.  The shared ownership lock must block it — no early entry.
    t_active.start()
    _time.sleep(0.1)
    assert not active_done.is_set()
    release_detached.set()
    t_detached.join(5)
    t_active.join(5)
    if worker_exc:
        raise worker_exc[0]

    # Root worker saw the ROOT value via TLS (raw scrubbed) throughout.
    assert out["detached_tls"] == "root-value"
    assert out["detached_raw"] is None
    # Active-request scope, after the root worker exited, resolved the NAMED
    # value on both channels.
    assert out["active_raw"] == "active-value"
    assert out["active_tls"] == "active-value"
    # Exact ambient restoration.
    assert os.environ.get("OPENROUTER_API_KEY") == "active-value"
    profiles._active_profile = "default"
    profiles._loaded_profile_env_keys.discard("OPENROUTER_API_KEY")
    profiles._loaded_profile_env_owner.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("OPENROUTER_API_KEY", None)


# ─────────────────────────────────────────────────────────────────────────────
# Facet E — ROOT scopes under the shared full-body ownership lock (#6327)
#
# The shared _PROCESS_ENV_OWNERSHIP_LOCK must also serialize ROOT/default
# direct and mirrored active-request bodies: a root body can otherwise observe
# a named scope's raw env mid-overlap, and the named scope can restore a stale
# snapshot over a concurrent root-body change.  These tests use a recording
# lock proxy as a deterministic attempted-entry barrier — the proxy records the
# blocked thread's real acquire() call, so "no early entry" is proven without
# sleeps.
# ─────────────────────────────────────────────────────────────────────────────


class _AttemptRecordingOwnershipLock:
    """RLock-compatible proxy around the shared ownership lock.

    Records the first acquire() attempt made AFTER arm() (the first scope
    enters before arming, so its own acquire is not recorded).  Lets a test
    prove deterministically that a second thread actually tried to enter the
    lock — and was blocked — instead of sleeping and hoping.
    """

    def __init__(self, inner):
        self._inner = inner
        self._armed = threading.Event()
        self._attempted = threading.Event()

    def arm(self):
        self._armed.set()

    def disarm(self):
        """Stop recording subsequent acquire() attempts (e.g. between an
        armed apply assertion and a legitimately out-of-AGENT revalidation)."""
        self._armed.clear()

    @property
    def attempted(self):
        return self._attempted

    def acquire(self, *args, **kwargs):
        if self._armed.is_set():
            self._attempted.set()
        return self._inner.acquire(*args, **kwargs)

    def release(self, *args, **kwargs):
        return self._inner.release(*args, **kwargs)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc_info):
        self.release()
        return False


def test_background_worker_direct_root_overlap_detached_named_direct_first(
    monkeypatch, tmp_path
):
    """Direct ROOT background scope overlapping a detached NAMED scope (#6327).

    A DIRECT ``profile_env_for_background_worker("default")`` scope enters
    first and holds the shared full-body ownership lock.  A detached named
    worker started mid-body must not enter early — the recording proxy proves
    the acquire attempt happened while the root body still held the lock.
    Inside the root body the raw channel is scrubbed of the foreign named
    value while TLS resolves the ROOT credential; after the root scope exits
    the detached worker resolves ITS value on both channels, and the pre-test
    env is restored exactly.
    """
    import threading

    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    (base / ".env").write_text("OPENROUTER_API_KEY=root-value\n", encoding="utf-8")
    beta_home = base / "profiles" / "beta"
    beta_home.mkdir(parents=True)
    (beta_home / ".env").write_text("OPENROUTER_API_KEY=beta-value\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    # Simulate a named profile switch that loaded beta's .env as the current
    # process-wide state: the root scope must scrub this foreign value from
    # the raw channel for its whole body.
    profiles._reload_dotenv(beta_home)
    assert profiles._loaded_profile_env_owner.get("OPENROUTER_API_KEY") == "beta"
    assert os.environ.get("OPENROUTER_API_KEY") == "beta-value"

    proxy = _AttemptRecordingOwnershipLock(profiles._PROCESS_ENV_OWNERSHIP_LOCK)
    monkeypatch.setattr(profiles, "_PROCESS_ENV_OWNERSHIP_LOCK", proxy)

    root_entered = threading.Event()
    release_root = threading.Event()
    detached_done = threading.Event()
    out: dict = {}
    worker_exc: list[BaseException] = []

    def root_worker():
        try:
            with profiles.profile_env_for_background_worker("default", "test"):
                root_entered.set()
                out["root_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["root_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
                assert release_root.wait(5)
        except BaseException as exc:
            worker_exc.append(exc)

    def detached_worker():
        try:
            with profiles.profile_scope_for_detached_worker("beta", "test"):
                out["detached_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["detached_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
        except BaseException as exc:
            worker_exc.append(exc)
        finally:
            detached_done.set()

    t_root = threading.Thread(target=root_worker)
    t_detached = threading.Thread(target=detached_worker)
    t_root.start()
    assert root_entered.wait(5)
    proxy.arm()
    t_detached.start()
    # Deterministic attempted-entry barrier: the detached thread's acquire()
    # call must actually have happened (and blocked) — no sleeps.
    assert proxy.attempted.wait(5), "detached worker never attempted the ownership lock"
    assert not detached_done.is_set(), "detached worker entered before root released"
    release_root.set()
    t_root.join(5)
    t_detached.join(5)
    if worker_exc:
        raise worker_exc[0]

    # Root body: raw channel scrubbed of the foreign beta value; TLS root value.
    assert out["root_raw"] is None
    assert out["root_tls"] == "root-value"
    # Detached worker, after the root scope exited, resolved the BETA value.
    assert out["detached_raw"] == "beta-value"
    assert out["detached_tls"] == "beta-value"
    # Exact restoration — the pre-test named-loaded state, no stale root leak.
    assert os.environ.get("OPENROUTER_API_KEY") == "beta-value"
    profiles._loaded_profile_env_keys.discard("OPENROUTER_API_KEY")
    profiles._loaded_profile_env_owner.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("OPENROUTER_API_KEY", None)


def test_background_worker_direct_root_overlap_detached_named_detached_first(
    monkeypatch, tmp_path
):
    """Detached NAMED scope overlapping a direct ROOT background scope (#6327).

    Mirrors test_background_worker_direct_root_overlap_detached_named_direct_first
    with the DETACHED named worker entering first: while it holds the shared
    ownership lock the direct root scope must not enter early (attempted-entry
    barrier), and after the detached scope exits the root body resolves the
    ROOT credential via TLS with the raw channel scrubbed of the foreign named
    value — plus exact ambient restoration.
    """
    import threading

    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    (base / ".env").write_text("OPENROUTER_API_KEY=root-value\n", encoding="utf-8")
    beta_home = base / "profiles" / "beta"
    beta_home.mkdir(parents=True)
    (beta_home / ".env").write_text("OPENROUTER_API_KEY=beta-value\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    profiles._reload_dotenv(beta_home)
    assert os.environ.get("OPENROUTER_API_KEY") == "beta-value"

    proxy = _AttemptRecordingOwnershipLock(profiles._PROCESS_ENV_OWNERSHIP_LOCK)
    monkeypatch.setattr(profiles, "_PROCESS_ENV_OWNERSHIP_LOCK", proxy)

    detached_entered = threading.Event()
    release_detached = threading.Event()
    root_done = threading.Event()
    out: dict = {}
    worker_exc: list[BaseException] = []

    def detached_worker():
        try:
            with profiles.profile_scope_for_detached_worker("beta", "test"):
                detached_entered.set()
                out["detached_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["detached_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
                assert release_detached.wait(5)
        except BaseException as exc:
            worker_exc.append(exc)

    def root_worker():
        try:
            with profiles.profile_env_for_background_worker("default", "test"):
                out["root_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["root_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
        except BaseException as exc:
            worker_exc.append(exc)
        finally:
            root_done.set()

    t_detached = threading.Thread(target=detached_worker)
    t_root = threading.Thread(target=root_worker)
    t_detached.start()
    assert detached_entered.wait(5)
    proxy.arm()
    t_root.start()
    assert proxy.attempted.wait(5), "root worker never attempted the ownership lock"
    assert not root_done.is_set(), "root worker entered before detached released"
    release_detached.set()
    t_detached.join(5)
    t_root.join(5)
    if worker_exc:
        raise worker_exc[0]

    # Detached worker saw the NAMED value on both channels throughout.
    assert out["detached_raw"] == "beta-value"
    assert out["detached_tls"] == "beta-value"
    # Root worker, after the detached scope exited, resolved the ROOT value
    # via TLS; the raw channel is scrubbed of the foreign named value.
    assert out["root_tls"] == "root-value"
    assert out["root_raw"] is None
    # Exact restoration.
    assert os.environ.get("OPENROUTER_API_KEY") == "beta-value"
    profiles._loaded_profile_env_keys.discard("OPENROUTER_API_KEY")
    profiles._loaded_profile_env_owner.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("OPENROUTER_API_KEY", None)


def test_active_request_root_overlap_detached_named_active_first(
    monkeypatch, tmp_path
):
    """Active-request ROOT scope overlapping a detached NAMED scope (#6327).

    The active-request scope (``profile_env_for_active_request`` with the
    thread-local profile set to ``default`` → the shared
    ``profile_env_for_background_worker`` root branch) enters first and holds
    the ownership lock.  A detached named worker started mid-body must not
    enter early (attempted-entry barrier).  The root body RAISES to prove
    exact exception restoration: the scrubbed foreign value is restored and
    the lock released, then the detached worker resolves ITS value on both
    channels and the pre-test env is restored exactly.
    """
    import threading

    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    (base / ".env").write_text("OPENROUTER_API_KEY=root-value\n", encoding="utf-8")
    beta_home = base / "profiles" / "beta"
    beta_home.mkdir(parents=True)
    (beta_home / ".env").write_text("OPENROUTER_API_KEY=beta-value\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    profiles._reload_dotenv(beta_home)
    assert os.environ.get("OPENROUTER_API_KEY") == "beta-value"

    proxy = _AttemptRecordingOwnershipLock(profiles._PROCESS_ENV_OWNERSHIP_LOCK)
    monkeypatch.setattr(profiles, "_PROCESS_ENV_OWNERSHIP_LOCK", proxy)

    active_entered = threading.Event()
    detached_done = threading.Event()
    out: dict = {}
    worker_exc: list[BaseException] = []
    expected_boom: list[str] = []

    def active_worker():
        profiles.set_request_profile("default")
        try:
            with profiles.profile_env_for_active_request("test"):
                active_entered.set()
                out["active_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["active_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
                raise ValueError("boom")
        except ValueError:
            expected_boom.append("boom")
        except BaseException as exc:
            worker_exc.append(exc)
        finally:
            profiles.clear_request_profile()

    def detached_worker():
        try:
            with profiles.profile_scope_for_detached_worker("beta", "test"):
                out["detached_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["detached_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
        except BaseException as exc:
            worker_exc.append(exc)
        finally:
            detached_done.set()

    t_active = threading.Thread(target=active_worker)
    t_detached = threading.Thread(target=detached_worker)
    t_active.start()
    assert active_entered.wait(5)
    proxy.arm()
    t_detached.start()
    assert proxy.attempted.wait(5), "detached worker never attempted the ownership lock"
    assert not detached_done.is_set(), "detached worker entered before active released"
    t_active.join(5)
    t_detached.join(5)
    if worker_exc:
        raise worker_exc[0]
    assert expected_boom == ["boom"], "root active-request body exception not propagated"

    # Active-request root body saw the ROOT value via TLS; raw scrubbed.
    assert out["active_tls"] == "root-value"
    assert out["active_raw"] is None
    # Detached worker, after the raising root scope restored everything,
    # resolved the BETA value on both channels.
    assert out["detached_raw"] == "beta-value"
    assert out["detached_tls"] == "beta-value"
    # Exact restoration even though the root body raised.
    assert os.environ.get("OPENROUTER_API_KEY") == "beta-value"
    profiles._loaded_profile_env_keys.discard("OPENROUTER_API_KEY")
    profiles._loaded_profile_env_owner.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("OPENROUTER_API_KEY", None)


def test_active_request_root_overlap_detached_named_detached_first(
    monkeypatch, tmp_path
):
    """Detached NAMED scope overlapping an active-request ROOT scope (#6327).

    Mirrors test_active_request_root_overlap_detached_named_active_first with
    the DETACHED named worker entering first: while it holds the shared
    ownership lock the active-request root scope must not enter early
    (attempted-entry barrier), and after the detached scope exits the root
    body resolves the ROOT credential via TLS with the raw channel scrubbed —
    plus exact ambient restoration.
    """
    import threading

    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    (base / ".env").write_text("OPENROUTER_API_KEY=root-value\n", encoding="utf-8")
    beta_home = base / "profiles" / "beta"
    beta_home.mkdir(parents=True)
    (beta_home / ".env").write_text("OPENROUTER_API_KEY=beta-value\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    profiles._reload_dotenv(beta_home)
    assert os.environ.get("OPENROUTER_API_KEY") == "beta-value"

    proxy = _AttemptRecordingOwnershipLock(profiles._PROCESS_ENV_OWNERSHIP_LOCK)
    monkeypatch.setattr(profiles, "_PROCESS_ENV_OWNERSHIP_LOCK", proxy)

    detached_entered = threading.Event()
    release_detached = threading.Event()
    active_done = threading.Event()
    out: dict = {}
    worker_exc: list[BaseException] = []

    def detached_worker():
        try:
            with profiles.profile_scope_for_detached_worker("beta", "test"):
                detached_entered.set()
                out["detached_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["detached_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
                assert release_detached.wait(5)
        except BaseException as exc:
            worker_exc.append(exc)

    def active_worker():
        profiles.set_request_profile("default")
        try:
            with profiles.profile_env_for_active_request("test"):
                out["active_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["active_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
        except BaseException as exc:
            worker_exc.append(exc)
        finally:
            profiles.clear_request_profile()
            active_done.set()

    t_detached = threading.Thread(target=detached_worker)
    t_active = threading.Thread(target=active_worker)
    t_detached.start()
    assert detached_entered.wait(5)
    proxy.arm()
    t_active.start()
    assert proxy.attempted.wait(5), "active-request root never attempted the ownership lock"
    assert not active_done.is_set(), "active-request root entered before detached released"
    release_detached.set()
    t_detached.join(5)
    t_active.join(5)
    if worker_exc:
        raise worker_exc[0]

    # Detached worker saw the NAMED value on both channels throughout.
    assert out["detached_raw"] == "beta-value"
    assert out["detached_tls"] == "beta-value"
    # Active-request root scope, after the detached scope exited, resolved the
    # ROOT value via TLS; raw channel scrubbed of the foreign named value.
    assert out["active_tls"] == "root-value"
    assert out["active_raw"] is None
    # Exact restoration.
    assert os.environ.get("OPENROUTER_API_KEY") == "beta-value"
    profiles._loaded_profile_env_keys.discard("OPENROUTER_API_KEY")
    profiles._loaded_profile_env_owner.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("OPENROUTER_API_KEY", None)


def test_streaming_legacy_skill_lock_order_no_deadlock_with_direct_named(
    monkeypatch, tmp_path
):
    """Forced legacy/static-module deadlock regression (#6327).

    The streaming legacy/static fallback holds _SKILL_HOME_MODULE_PATCH_LOCK
    for the whole turn and, mid-turn, enters profile_scope_for_detached_worker()
    (a full-body _PROCESS_ENV_OWNERSHIP_LOCK holder) for model resolution.  A
    direct named background scope holds the ownership lock and can wait on the
    skill lock — if streaming took SKILL before PROCESS, the two threads would
    form a cross-thread AB/BA cycle and neither would ever terminate.

    This test composes the REAL locks in the fixed streaming order
    (PROCESS -> SKILL, then the nested detached/model-resolution scope) against
    a direct named background scope, and asserts BOTH threads terminate with
    owner-correct raw/TLS values.  A source-order assertion pins the streaming
    acquire order so reverting the fix fails the test even though the runtime
    composition itself is well-ordered.
    """
    import threading
    import time as _time

    # Source-order pin: api/streaming.py must acquire the shared process-env
    # ownership lock BEFORE the skill-module patch lock in the legacy/static
    # full-turn block (single global lock order PROCESS -> SKILL -> ENV).
    src = Path(__file__).parent.parent / "api" / "streaming.py"
    text = src.read_text(encoding="utf-8")
    proc_acquire = text.find("_PROCESS_ENV_OWNERSHIP_LOCK.acquire()")
    skill_acquire = text.find("_SKILL_HOME_MODULE_PATCH_LOCK.acquire()")
    assert proc_acquire != -1, "streaming must acquire _PROCESS_ENV_OWNERSHIP_LOCK"
    assert skill_acquire != -1, "streaming must acquire _SKILL_HOME_MODULE_PATCH_LOCK"
    assert 0 <= proc_acquire < skill_acquire, (
        "global lock order violated: streaming must acquire "
        "_PROCESS_ENV_OWNERSHIP_LOCK BEFORE _SKILL_HOME_MODULE_PATCH_LOCK"
    )

    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    (base / ".env").write_text("OPENROUTER_API_KEY=root-value\n", encoding="utf-8")
    for prof, val in (("alpha", "alpha-value"), ("beta", "beta-value")):
        home = base / "profiles" / prof
        home.mkdir(parents=True)
        (home / ".env").write_text(f"OPENROUTER_API_KEY={val}\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-value")

    streaming_entered = threading.Event()
    release_streaming = threading.Event()
    direct_done = threading.Event()
    out: dict = {}
    worker_exc: list[BaseException] = []

    def streaming_thread():
        # The FIXED streaming order: the legacy/static full-turn block acquires
        # the shared process-env ownership lock BEFORE the skill-module patch
        # lock, then re-enters the detached/model-resolution scope mid-turn.
        try:
            with profiles._PROCESS_ENV_OWNERSHIP_LOCK:
                with profiles._SKILL_HOME_MODULE_PATCH_LOCK:
                    with profiles.profile_scope_for_detached_worker("beta", "test"):
                        streaming_entered.set()
                        out["streaming_raw"] = os.environ.get("OPENROUTER_API_KEY")
                        out["streaming_tls"] = config._thread_local_env_value(
                            "OPENROUTER_API_KEY"
                        )
                    assert release_streaming.wait(10)
        except BaseException as exc:
            worker_exc.append(exc)

    def direct_thread():
        try:
            with profiles.profile_env_for_background_worker("alpha", "test"):
                out["direct_raw"] = os.environ.get("OPENROUTER_API_KEY")
                out["direct_tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
        except BaseException as exc:
            worker_exc.append(exc)
        finally:
            direct_done.set()

    t_streaming = threading.Thread(target=streaming_thread)
    t_direct = threading.Thread(target=direct_thread)
    t_streaming.start()
    assert streaming_entered.wait(5)
    t_direct.start()
    _time.sleep(0.1)
    assert not direct_done.is_set(), "direct scope entered while streaming held the turn"
    release_streaming.set()
    t_streaming.join(15)
    t_direct.join(15)
    if worker_exc:
        raise worker_exc[0]
    assert not t_streaming.is_alive(), "streaming thread deadlocked on SKILL/PROCESS"
    assert not t_direct.is_alive(), "direct background thread deadlocked on PROCESS/SKILL"

    # Streaming nested detached scope resolved the BETA value on both channels.
    assert out["streaming_raw"] == "beta-value"
    assert out["streaming_tls"] == "beta-value"
    # Direct named scope, after the streaming turn released, resolved ALPHA.
    assert out["direct_raw"] == "alpha-value"
    assert out["direct_tls"] == "alpha-value"
    # Exact ambient restoration.
    assert os.environ.get("OPENROUTER_API_KEY") == "ambient-value"


# ─────────────────────────────────────────────────────────────────────────────
# Facet F — production-composed streaming ownership (#6327, round 2)
#
# The Facet E lock-order tests compose the REAL locks by hand but never run
# _run_agent_streaming(), the checkpoint thread, the bounded joins, or the
# unwind paths — so they cannot see the two remaining concurrency schedules:
#
#   B1 — the streaming turn snapshots/mutates raw os.environ UNCONDITIONALLY
#        but, on the dynamic/nonlegacy branch, used to take the shared
#        _PROCESS_ENV_OWNERSHIP_LOCK only for the legacy skill-module block.
#        A direct named profile scope could then run concurrently while the
#        turn's env was installed (both observe the wrong profile; reversed
#        restoration leaves stale values).  Fix: the turn now acquires
#        PROCESS before ANY raw-env snapshot/mutation, in every mode.
#
#   B2 — the periodic checkpoint thread holds the per-session agent lock and
#        used to enter profile_env_for_background_worker(), which blocks on
#        the turn's full-body PROCESS ownership; the turn's bounded join then
#        blocks on the same agent lock → AGENT↔PROCESS deadlock.  Fix:
#        _save_streaming_checkpoint() uses a READ-ONLY explicit-profile scope
#        (thread/context-local only, never mirrors os.environ) so the
#        AGENT → PROCESS edge is gone.
#
# These tests drive the REAL _run_agent_streaming() with a fake session/agent
# and assert both schedules on real unlock paths (normal completion, cancel,
# exception).
# ─────────────────────────────────────────────────────────────────────────────


class _StreamingFakeHomeOverride:
    """Minimal hermes_constants stand-in for the context-local home override."""

    def set_hermes_home_override(self, home):
        return ("override-token",)

    def reset_hermes_home_override(self, token):
        return None


class _StreamingFakeSession:
    """Minimal Session stand-in (mirrors test_issue2965's shape)."""

    def __init__(self, session_id, profile, workspace, stream_id):
        self.session_id = session_id
        self.title = "Streaming ownership"
        self.workspace = str(workspace)
        self.model = "test-model"
        self.model_provider = None
        self.profile = profile
        self.personality = None
        self.messages = []
        self.context_messages = []
        self.tool_calls = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost = None
        self.context_length = 0
        self.threshold_tokens = 0
        self.last_prompt_tokens = 0
        self.active_stream_id = stream_id
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.llm_title_generated = True

    def save(self, *args, **kwargs):
        return None

    def compact(self):
        return {
            "session_id": self.session_id,
            "title": self.title,
            "workspace": self.workspace,
            "model": self.model,
            "created_at": 0,
            "updated_at": 0,
            "pinned": False,
            "archived": False,
            "project_id": None,
            "profile": self.profile,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost": self.estimated_cost,
            "personality": self.personality,
        }


def _install_streaming_profile_env(monkeypatch, tmp_path, profile_values):
    """Wire fake profile homes + streaming deps; return (session, stream_id)."""
    import queue

    import api.config as cfg
    import api.oauth as oauth
    import api.profiles as profiles
    import api.streaming as streaming

    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    (base / ".env").write_text("OPENROUTER_API_KEY=root-value\n", encoding="utf-8")
    homes = {}
    for prof, val in profile_values.items():
        home = base / "profiles" / prof
        home.mkdir(parents=True)
        (home / ".env").write_text(f"OPENROUTER_API_KEY={val}\n", encoding="utf-8")
        homes[prof] = home

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))

    def fake_get_home(profile_name):
        name = str(profile_name or "").strip() or "default"
        if name == "default":
            return base
        return homes.get(name, base / "profiles" / name)

    def fake_runtime_env(home):
        home_str = str(Path(home))
        for prof, val in profile_values.items():
            if home_str == str(homes[prof]):
                return {"OPENROUTER_API_KEY": val}
        if home_str == str(base):
            return {"OPENROUTER_API_KEY": "root-value"}
        return {}

    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", fake_get_home)
    monkeypatch.setattr(profiles, "get_profile_runtime_env", fake_runtime_env)

    session = _StreamingFakeSession(
        session_id="issue6327-stream-session",
        profile="alpha",
        workspace=tmp_path,
        stream_id="issue6327-stream",
    )
    stream_id = session.active_stream_id

    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(
        streaming, "_maybe_schedule_title_refresh", lambda *a, **k: None
    )
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda _model, **_kw: ("test-model", "test-provider", None),
    )
    monkeypatch.setattr("api.config.get_config", lambda: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda _cfg: [])
    monkeypatch.setattr("api.config.load_settings", lambda: {})
    monkeypatch.setattr(
        oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda _resolver, requested=None: {
            "provider": requested or "test-provider",
            "api_key": "synthetic-key",
            "base_url": None,
        },
    )

    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = lambda requested=None: {
        "provider": requested or "test-provider",
        "api_key": "synthetic-key",
        "base_url": None,
    }
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")

    class _FakeSessionDB:
        def __init__(self, db_path=None):
            self.db_path = db_path

        def close(self):
            return None

    fake_hermes_state.SessionDB = _FakeSessionDB
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)

    with cfg.SESSION_AGENT_CACHE_LOCK:
        cfg.SESSION_AGENT_CACHE.clear()
    streaming.STREAMS.clear()
    streaming.CANCEL_FLAGS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.STREAM_PARTIAL_TEXT.clear()
    streaming.STREAM_REASONING_TEXT.clear()
    streaming.STREAM_LIVE_TOOL_CALLS.clear()
    streaming.STREAMS[stream_id] = queue.Queue()

    return session, stream_id


class _StreamingFakeAgent:
    """Agent stand-in: subclasses implement run_conversation()."""

    def __init__(self, **kwargs):
        self.session_db = kwargs.get("session_db")
        self._session_db = self.session_db
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_estimated_cost_usd = None
        self.context_compressor = None
        self._last_error = None
        self.ephemeral_system_prompt = None

    def run_conversation(self, **kwargs):
        raise NotImplementedError

    def interrupt(self, _message):
        return None


def _streaming_env_keys():
    return (
        "OPENROUTER_API_KEY",
        "TERMINAL_CWD",
        "HERMES_EXEC_ASK",
        "HERMES_SESSION_KEY",
        "HERMES_SESSION_ID",
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_HOME",
    )


def _snapshot_streaming_env():
    return {k: os.environ.get(k) for k in _streaming_env_keys()}


def test_streaming_dynamic_env_ownership_blocks_direct_profile_overlap(
    monkeypatch, tmp_path
):
    """DYNAMIC/nonlegacy streaming holds PROCESS for the whole turn (#6327 B1).

    Drives the REAL ``_run_agent_streaming()`` with the context-local
    override installed and skill modules reporting profile-home support — the
    branch where the old code skipped ``_PROCESS_ENV_OWNERSHIP_LOCK`` while
    still installing raw ``os.environ`` for the entire turn.  A direct named
    background scope started mid-turn must attempt the ownership lock and be
    blocked until the turn releases it (recording-proxy barrier, no sleeps);
    raw + TLS resolve the STREAM's alpha profile during the turn; after both
    scopes complete, the ambient env is restored exactly.
    """
    import api.profiles as profiles
    import api.streaming as streaming

    session, stream_id = _install_streaming_profile_env(
        monkeypatch, tmp_path, {"alpha": "alpha-value", "beta": "beta-value"}
    )
    monkeypatch.setattr(
        streaming,
        "_set_streaming_hermes_home_override",
        lambda home: (_StreamingFakeHomeOverride(), "tok", True),
    )
    monkeypatch.setattr(profiles, "_skill_modules_support_profile_home", lambda home: True)
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-value")

    entered = threading.Event()
    release_agent = threading.Event()
    stream_done = threading.Event()
    stream_out: dict = {}
    stream_exc: list[BaseException] = []

    class _Agent(_StreamingFakeAgent):
        def run_conversation(self, **kwargs):
            try:
                stream_out["raw"] = os.environ.get("OPENROUTER_API_KEY")
                stream_out["tls"] = config._thread_local_env_value("OPENROUTER_API_KEY")
                entered.set()
                assert release_agent.wait(10)
                history = list(kwargs.get("conversation_history") or [])
                return {
                    "messages": history
                    + [
                        {"role": "user", "content": kwargs.get("persist_user_message", "")},
                        {"role": "assistant", "content": "ok"},
                    ]
                }
            except BaseException as exc:
                stream_exc.append(exc)
                raise

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _Agent)

    proxy = _AttemptRecordingOwnershipLock(profiles._PROCESS_ENV_OWNERSHIP_LOCK)
    monkeypatch.setattr(profiles, "_PROCESS_ENV_OWNERSHIP_LOCK", proxy)
    env_before = _snapshot_streaming_env()

    def stream_runner():
        try:
            streaming._run_agent_streaming(
                session_id=session.session_id,
                msg_text="hello",
                model="test-model",
                model_provider="test-provider",
                workspace=str(tmp_path),
                stream_id=stream_id,
            )
        except BaseException as exc:
            stream_exc.append(exc)
        finally:
            stream_done.set()

    t_stream = threading.Thread(target=stream_runner)
    t_stream.start()
    direct_done = threading.Event()
    direct_out: dict = {}
    direct_exc: list[BaseException] = []
    try:
        assert entered.wait(15), "streaming turn never entered run_conversation"
        proxy.arm()

        def direct_worker():
            try:
                with profiles.profile_env_for_background_worker("beta", "test"):
                    direct_out["raw"] = os.environ.get("OPENROUTER_API_KEY")
                    direct_out["tls"] = config._thread_local_env_value(
                        "OPENROUTER_API_KEY"
                    )
            except BaseException as exc:
                direct_exc.append(exc)
            finally:
                direct_done.set()

        t_direct = threading.Thread(target=direct_worker)
        t_direct.start()
        assert proxy.attempted.wait(10), (
            "direct scope never attempted the ownership lock while streaming ran"
        )
        assert not direct_done.is_set(), (
            "direct profile scope entered while the dynamic streaming turn held the env"
        )
        # During the turn both channels resolve the STREAM's alpha profile and
        # the direct scope could not corrupt the raw channel.
        assert stream_out["raw"] == "alpha-value"
        assert stream_out["tls"] == "alpha-value"
        assert os.environ.get("OPENROUTER_API_KEY") == "alpha-value"
    finally:
        release_agent.set()
    t_stream.join(30)
    assert not t_stream.is_alive(), "streaming thread deadlocked"
    t_direct.join(30)
    assert not t_direct.is_alive(), "direct background thread deadlocked"
    if stream_exc:
        raise stream_exc[0]
    if direct_exc:
        raise direct_exc[0]

    # After the turn released, the direct scope resolved BETA on both channels.
    assert direct_out["raw"] == "beta-value"
    assert direct_out["tls"] == "beta-value"
    # Ownership lock is free and the ambient env is restored exactly.
    assert proxy.acquire(blocking=False), "streaming turn leaked the ownership lock"
    proxy.release()
    assert _snapshot_streaming_env() == env_before, (
        "streaming turn left stale raw env behind after teardown"
    )


def test_streaming_checkpoint_no_agent_process_deadlock_normal(
    monkeypatch, tmp_path
):
    """LEGACY streaming + forced checkpoint: no AGENT↔PROCESS deadlock (#6327 B2).

    Real ``_run_agent_streaming()`` on the legacy/static branch (holds
    PROCESS + SKILL for the whole turn).  While the turn is live, a checkpoint
    action runs EXACTLY as ``_periodic_checkpoint()`` does — acquire the
    per-session agent lock, then call the real ``_save_streaming_checkpoint()``.
    The read-only explicit-profile scope must complete while the turn holds
    PROCESS (no AGENT → PROCESS edge), then the turn's bounded join and
    ``with _agent_lock`` finalize must not deadlock; ambient env is restored
    exactly and no lock is left held.
    """
    import api.profiles as profiles
    import api.streaming as streaming

    session, stream_id = _install_streaming_profile_env(
        monkeypatch, tmp_path, {"alpha": "alpha-value", "beta": "beta-value"}
    )
    # Force the LEGACY/static branch: override installed but skill modules do
    # not support profile-home resolution (real snapshot/patch run — they are
    # tolerant of missing modules).
    monkeypatch.setattr(
        streaming,
        "_set_streaming_hermes_home_override",
        lambda home: (_StreamingFakeHomeOverride(), "tok", True),
    )
    monkeypatch.setattr(profiles, "_skill_modules_support_profile_home", lambda home: False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-value")

    entered = threading.Event()
    release_agent = threading.Event()
    stream_done = threading.Event()
    ckpt_done = threading.Event()
    ckpt_out: dict = {}
    ckpt_exc: list[BaseException] = []
    stream_exc: list[BaseException] = []

    class _Agent(_StreamingFakeAgent):
        def run_conversation(self, **kwargs):
            entered.set()

            def checkpoint_action():
                try:
                    # Identical schedule to _periodic_checkpoint(): hold the
                    # per-session agent lock, then save the checkpoint.  The
                    # save must NOT wait on the turn's PROCESS ownership.
                    with streaming._get_session_agent_lock(session.session_id):
                        ckpt_out["raw_before"] = os.environ.get("OPENROUTER_API_KEY")
                        streaming._save_streaming_checkpoint(session)
                        # The read-only scope restored this fresh thread's
                        # thread-local env on exit (no TLS residue).
                        ckpt_out["thread_env_after"] = getattr(
                            config._thread_ctx, "env", None
                        )
                except BaseException as exc:
                    ckpt_exc.append(exc)
                finally:
                    ckpt_done.set()

            threading.Thread(target=checkpoint_action, daemon=True).start()
            assert ckpt_done.wait(10), (
                "checkpoint save deadlocked on the streaming turn's PROCESS lock"
            )
            assert ckpt_out["raw_before"] == "alpha-value"
            assert release_agent.wait(10)
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history
                + [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "ok"},
                ]
            }

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _Agent)
    env_before = _snapshot_streaming_env()

    def stream_runner():
        try:
            streaming._run_agent_streaming(
                session_id=session.session_id,
                msg_text="hello",
                model="test-model",
                model_provider="test-provider",
                workspace=str(tmp_path),
                stream_id=stream_id,
            )
        except BaseException as exc:
            stream_exc.append(exc)
        finally:
            stream_done.set()

    t_stream = threading.Thread(target=stream_runner)
    t_stream.start()
    assert entered.wait(15), "streaming turn never entered run_conversation"
    release_agent.set()
    t_stream.join(30)
    assert not t_stream.is_alive(), (
        "streaming thread deadlocked on the checkpoint join / session lock"
    )
    if stream_exc:
        raise stream_exc[0]
    if ckpt_exc:
        raise ckpt_exc[0]
    assert ckpt_done.is_set(), "checkpoint never completed"
    # The read-only scope restored the (empty) thread env on the ckpt thread.
    assert ckpt_out["thread_env_after"] == {}
    # No lock left held; ambient env restored exactly.
    assert profiles._PROCESS_ENV_OWNERSHIP_LOCK.acquire(blocking=False), (
        "streaming turn leaked the ownership lock"
    )
    profiles._PROCESS_ENV_OWNERSHIP_LOCK.release()
    assert _snapshot_streaming_env() == env_before, (
        "streaming turn left stale raw env behind after teardown"
    )


def test_streaming_checkpoint_no_agent_process_deadlock_cancel_exception(
    monkeypatch, tmp_path
):
    """LEGACY streaming + forced checkpoint on the cancel/exception path (#6327 B2).

    Same real-turn composition as the normal-path test, but the agent sets the
    stream's cancel event and RAISES mid-turn (with a checkpoint already
    completed under the session agent lock).  The exception handler's bounded
    join + ``with _agent_lock`` finalize must settle without deadlock, and the
    ambient env must be restored exactly.
    """
    import api.profiles as profiles
    import api.streaming as streaming

    session, stream_id = _install_streaming_profile_env(
        monkeypatch, tmp_path, {"alpha": "alpha-value", "beta": "beta-value"}
    )
    monkeypatch.setattr(
        streaming,
        "_set_streaming_hermes_home_override",
        lambda home: (_StreamingFakeHomeOverride(), "tok", True),
    )
    monkeypatch.setattr(profiles, "_skill_modules_support_profile_home", lambda home: False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-value")
    # The cancel/exception unwind appends journal events + persists the
    # cancelled turn; those are orthogonal to the lock schedule under test.
    monkeypatch.setattr(streaming, "append_turn_journal_event_for_stream", lambda *a, **k: None)
    monkeypatch.setattr(streaming, "_finalize_cancelled_turn", lambda *a, **k: None)

    entered = threading.Event()
    stream_done = threading.Event()
    ckpt_done = threading.Event()
    ckpt_exc: list[BaseException] = []
    stream_exc: list[BaseException] = []

    class _Agent(_StreamingFakeAgent):
        def run_conversation(self, **kwargs):
            entered.set()

            def checkpoint_action():
                try:
                    with streaming._get_session_agent_lock(session.session_id):
                        streaming._save_streaming_checkpoint(session)
                except BaseException as exc:
                    ckpt_exc.append(exc)
                finally:
                    ckpt_done.set()

            threading.Thread(target=checkpoint_action, daemon=True).start()
            assert ckpt_done.wait(10), (
                "checkpoint save deadlocked on the streaming turn's PROCESS lock"
            )
            # Cancel + raise mid-turn: the exception handler must join the
            # (already finished) checkpoint thread and take the session lock.
            cancel_event = streaming.CANCEL_FLAGS.get(stream_id)
            assert cancel_event is not None
            cancel_event.set()
            raise RuntimeError("simulated provider failure")

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _Agent)
    env_before = _snapshot_streaming_env()

    def stream_runner():
        try:
            streaming._run_agent_streaming(
                session_id=session.session_id,
                msg_text="hello",
                model="test-model",
                model_provider="test-provider",
                workspace=str(tmp_path),
                stream_id=stream_id,
            )
        except BaseException as exc:
            stream_exc.append(exc)
        finally:
            stream_done.set()

    t_stream = threading.Thread(target=stream_runner)
    t_stream.start()
    assert entered.wait(15), "streaming turn never entered run_conversation"
    t_stream.join(30)
    assert not t_stream.is_alive(), (
        "streaming thread deadlocked on the cancel/exception unwind"
    )
    if stream_exc:
        raise stream_exc[0]
    if ckpt_exc:
        raise ckpt_exc[0]
    assert ckpt_done.is_set(), "checkpoint never completed"
    # No lock left held; ambient env restored exactly.
    assert profiles._PROCESS_ENV_OWNERSHIP_LOCK.acquire(blocking=False), (
        "streaming turn leaked the ownership lock"
    )
    profiles._PROCESS_ENV_OWNERSHIP_LOCK.release()
    assert _snapshot_streaming_env() == env_before, (
        "streaming turn left stale raw env behind after teardown"
    )


# ─────────────────────────────────────────────────────────────────────────────
# #6327 — B3: process-wakeup credential revalidation OUTSIDE the AGENT lock
# ─────────────────────────────────────────────────────────────────────────────


def _run_process_wakeup_vs_streaming_deadlock(
    monkeypatch, tmp_path, *, settlement, credential_recovered
):
    """Production-composed two-thread regression for the #6327 AB/BA cycle.

    A REAL named-profile ``start_session_turn(source="process_wakeup")``
    contender races a REAL ``_run_agent_streaming()`` turn on the
    DYNAMIC/nonlegacy branch — the branch where full-turn
    ``_PROCESS_ENV_OWNERSHIP_LOCK`` (PROCESS) ownership is UNCONDITIONAL
    since #6327.  The streaming turn holds PROCESS for its whole lifetime and
    waits for the per-session agent lock (AGENT) at writeback.  The wakeup
    contender must therefore revalidate the paused credential pool OUTSIDE
    AGENT: while it is blocked on PROCESS, AGENT must remain free — otherwise
    the AB/BA cycle (streaming: PROCESS -> AGENT; wakeup: AGENT -> PROCESS)
    deadlocks both threads.

    ``settlement`` drives how the streaming turn ends: "normal" returns a
    result, "cancel" sets the stream cancel event, "exception" raises a
    simulated provider failure.  ``credential_recovered`` drives the (stubbed)
    credential-pool leaf outcome: True clears the pause and starts a run,
    False keeps the pause and returns the 409 suppressed-wakeup response.
    """
    import api.models as models
    import api.routes as routes
    import api.streaming as streaming

    session, stream_id = _install_streaming_profile_env(
        monkeypatch, tmp_path, {"alpha": "alpha-value", "beta": "beta-value"}
    )
    # DYNAMIC/nonlegacy branch: the unconditional full-turn PROCESS owner.
    monkeypatch.setattr(
        streaming,
        "_set_streaming_hermes_home_override",
        lambda home: (_StreamingFakeHomeOverride(), "tok", True),
    )
    monkeypatch.setattr(profiles, "_skill_modules_support_profile_home", lambda home: True)
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-value")

    # The paused credential-pool wakeup lane the contender will revalidate.
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="test-model",
        provider="test-provider",
    )
    assert pause and pause.get("paused")
    assert models.process_wakeup_pause_matches(
        session,
        model="test-model",
        provider="test-provider",
        classification="credential_pool_empty",
    )

    # Production routes stubs that are orthogonal to the lock schedule: the
    # REAL start_session_turn + REAL _process_wakeup_provider_has_recovery_credential
    # (which enters the named detached profile scope and takes PROCESS) are
    # exercised; only leaf resolution/run-dispatch are stubbed.
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(
        routes, "_resolve_chat_workspace_with_recovery", lambda s, w: str(tmp_path)
    )
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda s, p: (None, None, None))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *a, **k: ("test-model", "test-provider", False),
    )
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **k: None)
    monkeypatch.setattr(
        routes, "canonical_model_provider_lane", lambda m, p: ("test-model", "test-provider")
    )
    monkeypatch.setattr(
        routes,
        "provider_has_process_wakeup_recovery_credential",
        lambda provider_id, refresh=False: credential_recovered,
    )
    start_run_calls = []
    monkeypatch.setattr(
        routes,
        "_start_run",
        lambda *a, **k: start_run_calls.append(k)
        or {"_status": 200, "stream_id": "stub-stream", "session_id": session.session_id},
    )

    entered = threading.Event()
    release_agent = threading.Event()
    stream_done = threading.Event()
    stream_exc: list[BaseException] = []

    class _Agent(_StreamingFakeAgent):
        def run_conversation(self, **kwargs):
            entered.set()
            if settlement == "cancel":
                cancel_event = streaming.CANCEL_FLAGS.get(stream_id)
                assert cancel_event is not None
                cancel_event.set()
            assert release_agent.wait(10)
            if settlement == "exception":
                raise RuntimeError("simulated provider failure")
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history
                + [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "ok"},
                ]
            }

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _Agent)
    if settlement in ("cancel", "exception"):
        monkeypatch.setattr(
            streaming, "append_turn_journal_event_for_stream", lambda *a, **k: None
        )
        monkeypatch.setattr(streaming, "_finalize_cancelled_turn", lambda *a, **k: None)

    proxy = _AttemptRecordingOwnershipLock(profiles._PROCESS_ENV_OWNERSHIP_LOCK)
    monkeypatch.setattr(profiles, "_PROCESS_ENV_OWNERSHIP_LOCK", proxy)
    env_before = _snapshot_streaming_env()

    def stream_runner():
        try:
            streaming._run_agent_streaming(
                session_id=session.session_id,
                msg_text="hello",
                model="test-model",
                model_provider="test-provider",
                workspace=str(tmp_path),
                stream_id=stream_id,
            )
        except BaseException as exc:
            stream_exc.append(exc)
        finally:
            stream_done.set()

    t_stream = threading.Thread(target=stream_runner)
    t_stream.start()
    contender_done = threading.Event()
    contender_exc: list[BaseException] = []
    contender_out: dict = {}

    def contender():
        try:
            contender_out["resp"] = routes.start_session_turn(
                session.session_id, "wakeup", source="process_wakeup"
            )
        except BaseException as exc:
            contender_exc.append(exc)
        finally:
            contender_done.set()

    t_contender = threading.Thread(target=contender)
    try:
        assert entered.wait(15), "streaming turn never entered run_conversation"
        # Streaming now holds PROCESS for the whole turn.  Arm the recording
        # proxy and start the wakeup contender: its out-of-AGENT revalidation
        # must attempt PROCESS and block on the streaming turn.
        proxy.arm()
        t_contender.start()
        assert proxy.attempted.wait(10), (
            "wakeup contender never attempted the ownership lock while streaming ran"
        )
        assert not contender_done.is_set(), (
            "wakeup contender completed while streaming still held PROCESS"
        )
        # Deterministic barrier assertion: while the contender is BLOCKED on
        # PROCESS, the per-session AGENT lock must be free.  Holding AGENT here
        # would be the AB/BA half (streaming owns PROCESS and waits for AGENT).
        agent_lock = config._get_session_agent_lock(session.session_id)
        assert agent_lock.acquire(blocking=False), (
            "wakeup contender held AGENT while blocked on PROCESS (AB/BA cycle)"
        )
        agent_lock.release()
    finally:
        release_agent.set()
    t_stream.join(30)
    assert not t_stream.is_alive(), "streaming thread deadlocked"
    t_contender.join(30)
    assert not t_contender.is_alive(), "wakeup contender thread deadlocked"
    if stream_exc:
        raise stream_exc[0]
    if contender_exc:
        raise contender_exc[0]

    # Bounded settlement: no lock left held, ambient env restored exactly.
    assert contender_done.is_set(), "wakeup contender never settled"
    assert proxy.acquire(blocking=False), "a thread leaked the ownership lock"
    proxy.release()
    assert _snapshot_streaming_env() == env_before, (
        "threads left stale raw env behind after settlement"
    )

    if credential_recovered:
        assert start_run_calls, (
            "recovered wakeup did not start a run after clearing the pause"
        )
        assert contender_out["resp"]["_status"] == 200
        live_pause = getattr(session, "process_wakeup_pause", None)
        assert not (isinstance(live_pause, dict) and live_pause.get("paused")), (
            "recovered wakeup left the pause in place"
        )
    else:
        assert not start_run_calls, "unrecovered wakeup must not start a run"
        resp = contender_out["resp"]
        assert resp["_status"] == 409, resp
        live_pause = getattr(session, "process_wakeup_pause", None)
        assert isinstance(live_pause, dict) and live_pause.get("paused") is True
        assert int(live_pause.get("suppressed_count") or 0) >= 1, (
            "suppressed wakeup did not record suppression metadata"
        )
    return contender_out["resp"]


def test_process_wakeup_revalidation_outside_agent_lock_normal(
    monkeypatch, tmp_path
):
    """Wakeup revalidation vs streaming normal settlement: no AB/BA (#6327 B3)."""
    _run_process_wakeup_vs_streaming_deadlock(
        monkeypatch, tmp_path, settlement="normal", credential_recovered=True
    )


def test_process_wakeup_revalidation_outside_agent_lock_cancel(
    monkeypatch, tmp_path
):
    """Wakeup revalidation vs streaming cancel settlement: no AB/BA (#6327 B3)."""
    _run_process_wakeup_vs_streaming_deadlock(
        monkeypatch, tmp_path, settlement="cancel", credential_recovered=False
    )


def test_process_wakeup_revalidation_outside_agent_lock_exception(
    monkeypatch, tmp_path
):
    """Wakeup revalidation vs streaming provider-exception settlement (#6327 B3)."""
    _run_process_wakeup_vs_streaming_deadlock(
        monkeypatch, tmp_path, settlement="exception", credential_recovered=False
    )


# ─────────────────────────────────────────────────────────────────────────────
# #6327 — B4: deterministic barrier tests for the immutable owner token
#
# The out-of-AGENT revalidation answer is bound to an IMMUTABLE owner token
# (exact owner identity + SID + profile/home generation + pause state +
# credential fingerprint, snapshotted under the canonical owner's AGENT lock).
# These tests drive the REAL token pipeline
# (_build_immutable_session_owner_token -> _revalidate_... ->
# _apply_process_wakeup_revalidation_once / _validate_start_owner_fence)
# against a concurrent writer that mutates the canonical cache between the
# pipeline steps, synchronized with Events — never sleeps.  Every test asserts
# the stale answer is never applied/saved/suppressed/started, and that no
# PROCESS acquisition is attempted while AGENT is held (the #6327 AB/BA
# invariant).
# ─────────────────────────────────────────────────────────────────────────────


def _sessions_backed_get_session(session_id):
    """get_session stand-in resolving through the canonical config.SESSIONS."""
    with config.LOCK:
        return config.SESSIONS.get(session_id)


def _install_immutable_owner_token_fixture(monkeypatch, tmp_path, *, recovered=True):
    """Install a live paused wakeup session + the #6327 token-pipeline stubs.

    Returns ``(session, calls, proxy)``: ``calls`` collects start/save/suppress
    invocations so tests can assert nothing stale was ever mutated, and
    ``proxy`` is the armed-on-demand recording PROCESS-ownership lock wrapper.
    """
    import api.models as models
    import api.routes as routes
    import api.streaming as streaming

    session, stream_id = _install_streaming_profile_env(
        monkeypatch, tmp_path, {"alpha": "alpha-value", "beta": "beta-value"}
    )
    monkeypatch.setattr(
        streaming,
        "_set_streaming_hermes_home_override",
        lambda home: (_StreamingFakeHomeOverride(), "tok", True),
    )
    monkeypatch.setattr(profiles, "_skill_modules_support_profile_home", lambda home: True)
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-value")

    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="test-model",
        provider="test-provider",
    )
    assert pause and pause.get("paused")
    assert models.process_wakeup_pause_matches(
        session,
        model="test-model",
        provider="test-provider",
        classification="credential_pool_empty",
    )

    # The canonical cache: get_session resolves through config.SESSIONS (the
    # same dict the production helpers read under LOCK), so a writer thread can
    # atomically replace/rotate the owner between pipeline steps.
    monkeypatch.setattr(routes, "get_session", _sessions_backed_get_session)
    with config.LOCK:
        config.SESSIONS[session.session_id] = session

    # The canonical-model lane + credential-pool leaf the revalidation hits;
    # the remaining stubs mirror the B3 fixture so an accidental reach fails
    # loudly instead of touching real state.
    monkeypatch.setattr(
        routes, "canonical_model_provider_lane", lambda m, p: ("test-model", "test-provider")
    )
    monkeypatch.setattr(
        routes,
        "provider_has_process_wakeup_recovery_credential",
        lambda provider_id, refresh=False: recovered,
    )
    monkeypatch.setattr(
        routes, "_resolve_chat_workspace_with_recovery", lambda s, w: str(tmp_path)
    )
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda s, p: (None, None, None))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *a, **k: ("test-model", "test-provider", False),
    )
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **k: None)

    calls = {"start": [], "save": [], "suppress": []}
    monkeypatch.setattr(
        routes,
        "_start_run",
        lambda *a, **k: calls["start"].append(k) or {"_status": 200, "stream_id": "stub"},
    )
    monkeypatch.setattr(
        routes, "_save_session_quietly", lambda *a, **k: calls["save"].append((a, k))
    )
    monkeypatch.setattr(
        routes,
        "suppress_process_wakeup_for_provider_pause",
        lambda *a, **k: calls["suppress"].append((a, k)) or None,
    )

    proxy = _AttemptRecordingOwnershipLock(profiles._PROCESS_ENV_OWNERSHIP_LOCK)
    monkeypatch.setattr(profiles, "_PROCESS_ENV_OWNERSHIP_LOCK", proxy)
    return session, calls, proxy


def _build_token_and_revalidate(routes, session_id):
    """Run the #6327 token build + out-of-AGENT revalidation; return both."""
    token, owner = routes._build_immutable_session_owner_token(
        session_id, model="test-model", provider="test-provider"
    )
    assert token is not None and owner is not None
    revalidation = routes._revalidate_process_wakeup_credential_outside_agent_lock(
        token, model="test-model", provider="test-provider"
    )
    assert revalidation["token_session_id"] == token["session_id"]
    assert revalidation["recovered"] is True
    return token, owner, revalidation


def _run_concurrent_writer(go_event, done_event, mutate):
    """Run ``mutate()`` on a writer thread synchronized by Events (no sleeps)."""

    def writer():
        try:
            assert go_event.wait(10)
            mutate()
        finally:
            done_event.set()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    return thread


def test_revalidation_replacement_same_sid_different_profile_generation(
    monkeypatch, tmp_path
):
    """Same-SID owner replacement with an identical pause but a different
    profile generation must NOT apply the revalidation answer (#6327 B4).

    A concurrent writer replaces the canonical cache owner under the SAME sid
    (byte-identical pause dict, different profile => different home +
    credential generation) between token build and apply: the apply must
    refuse with ``owner_replaced``, never save/suppress/start on either
    object, and never attempt PROCESS while AGENT is held.
    """
    import api.models as models
    import api.routes as routes

    session, calls, proxy = _install_immutable_owner_token_fixture(
        monkeypatch, tmp_path, recovered=True
    )
    sid = session.session_id
    original_pause = dict(session.process_wakeup_pause)
    try:
        token, owner, revalidation = _build_token_and_revalidate(routes, sid)
        assert owner is session
        assert token["profile"] == "alpha"

        replacement = _StreamingFakeSession(sid, "beta", tmp_path, "issue6327-stream")
        replacement.process_wakeup_pause = dict(original_pause)
        go = threading.Event()
        done = threading.Event()

        def _replace_owner():
            with config.LOCK:
                config.SESSIONS[sid] = replacement

        writer = _run_concurrent_writer(go, done, _replace_owner)
        go.set()
        assert done.wait(10), "replacement writer never ran"
        writer.join(10)

        proxy.arm()
        result = routes._apply_process_wakeup_revalidation_once(
            token, revalidation, model="test-model", provider="test-provider"
        )
        assert not proxy.attempted.is_set(), (
            "replacement apply attempted PROCESS while AGENT was held (AB/BA)"
        )
        assert result["retry"] is True, result
        assert result["reason"] == "owner_replaced", result
        # The token also captures the profile generation: mutating the SAME
        # canonical object's profile after the token was built is flagged by
        # the mismatch detector even when identity is preserved.
        session.profile = "beta"
        try:
            assert (
                routes._process_wakeup_owner_token_mismatch(token, session)
                == "profile_changed"
            )
        finally:
            session.profile = "alpha"

        # Nothing stale was saved/suppressed/started; neither object mutated.
        assert calls["start"] == []
        assert calls["save"] == []
        assert calls["suppress"] == []
        assert dict(session.process_wakeup_pause) == original_pause
        assert dict(replacement.process_wakeup_pause) == original_pause
        assert models.process_wakeup_pause_matches(
            replacement,
            model="test-model",
            provider="test-provider",
            classification="credential_pool_empty",
        )
        assert proxy.acquire(blocking=False), "a thread leaked the ownership lock"
        proxy.release()
    finally:
        with config.LOCK:
            config.SESSIONS.pop(sid, None)


def test_revalidation_compression_migration_during_wait(monkeypatch, tmp_path):
    """Compression (old-SID archived snapshot -> continuation) while the
    revalidation waits must never touch the snapshot (#6327 B4).

    The writer archives the tokenized owner (``pre_compression_snapshot``,
    old SID preserved) and migrates the canonical cache to a continuation
    under a NEW sid with the per-session AGENT lock aliased — the exact #6327
    compression contract.  The stale apply must requeue with
    ``session_archived`` without mutating the snapshot; the atomic resolve
    (``_resolve_live_session_owner`` and a fresh token build) follows to the
    continuation; applying with the fresh token lands on the LIVE owner only.
    """
    import api.routes as routes

    session, calls, proxy = _install_immutable_owner_token_fixture(
        monkeypatch, tmp_path, recovered=True
    )
    old_sid = session.session_id
    new_sid = "issue6327-compressed-continuation"
    original_pause = dict(session.process_wakeup_pause)
    # Keep the continuation scan hermetic: no real session index/sidecars.
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", tmp_path / "sessions" / "_index.json")
    try:
        token, owner, revalidation = _build_token_and_revalidate(routes, old_sid)
        assert owner is session

        continuation = _StreamingFakeSession(
            new_sid, "alpha", tmp_path, "issue6327-stream"
        )
        continuation.process_wakeup_pause = dict(original_pause)
        continuation.parent_session_id = old_sid
        continuation.updated_at = 2.0
        session.updated_at = 1.0
        go = threading.Event()
        done = threading.Event()

        def _compress():
            session.pre_compression_snapshot = True
            with config.SESSION_AGENT_LOCKS_LOCK:
                # Production aliases the per-session AGENT lock to the new SID.
                config.SESSION_AGENT_LOCKS[new_sid] = config.SESSION_AGENT_LOCKS.get(
                    old_sid
                )
            with config.LOCK:
                config.SESSIONS[old_sid] = session
                config.SESSIONS[new_sid] = continuation

        writer = _run_concurrent_writer(go, done, _compress)
        go.set()
        assert done.wait(10), "compression writer never ran"
        writer.join(10)

        # Atomic resolve: following the archived snapshot lands on the
        # continuation; the lock entry is aliased to the new sid.
        resolved_owner, resolved_sid = routes._resolve_live_session_owner(
            old_sid, preferred=session
        )
        assert resolved_owner is continuation
        assert resolved_sid == new_sid
        with config.SESSION_AGENT_LOCKS_LOCK:
            assert (
                config.SESSION_AGENT_LOCKS.get(new_sid)
                is config.SESSION_AGENT_LOCKS.get(old_sid)
            )

        # The stale apply refuses and NEVER mutates the archived snapshot.
        proxy.arm()
        result = routes._apply_process_wakeup_revalidation_once(
            token, revalidation, model="test-model", provider="test-provider"
        )
        assert not proxy.attempted.is_set(), (
            "archived apply attempted PROCESS while AGENT was held (AB/BA)"
        )
        assert result["retry"] is True and result["reason"] == "session_archived", result
        assert session.pre_compression_snapshot is True
        assert dict(session.process_wakeup_pause) == original_pause
        assert dict(continuation.process_wakeup_pause) == original_pause
        assert calls["start"] == [] and calls["save"] == [] and calls["suppress"] == []
        # The next revalidation runs legitimately OUTSIDE AGENT (it takes
        # PROCESS) — stop recording before it, then re-arm for the apply.
        proxy.disarm()

        # Bounded rebuild follows the continuation: applying with the fresh
        # token lands on the LIVE owner (clears ITS pause), never the snapshot.
        token2, owner2, revalidation2 = _build_token_and_revalidate(routes, old_sid)
        assert owner2 is continuation
        assert token2["session_id"] == new_sid
        proxy.arm()
        result2 = routes._apply_process_wakeup_revalidation_once(
            token2, revalidation2, model="test-model", provider="test-provider"
        )
        assert not proxy.attempted.is_set(), (
            "continuation apply attempted PROCESS while AGENT was held (AB/BA)"
        )
        assert result2["retry"] is False and result2["error"] is None, result2
        live_pause = getattr(continuation, "process_wakeup_pause", None)
        assert not (isinstance(live_pause, dict) and live_pause.get("paused"))
        assert calls["start"] == []
        assert calls["save"], "applied revalidation must persist the live owner"
        # The archived snapshot stayed untouched end to end.
        assert session.pre_compression_snapshot is True
        assert dict(session.process_wakeup_pause) == original_pause
        assert proxy.acquire(blocking=False), "a thread leaked the ownership lock"
        proxy.release()
    finally:
        with config.LOCK:
            config.SESSIONS.pop(old_sid, None)
            config.SESSIONS.pop(new_sid, None)
        with config.SESSION_AGENT_LOCKS_LOCK:
            config.SESSION_AGENT_LOCKS.pop(new_sid, None)


def test_revalidation_credential_generation_changes_before_apply(
    monkeypatch, tmp_path
):
    """Credential-generation movement between token build and apply must retry
    outside AGENT (bounded) and never apply the stale answer (#6327 B4).

    The token snapshots the credential-state fingerprint at build time.  A
    concurrent writer bumps the generation while the revalidation waits; the
    apply must refuse with ``credential_state_changed`` (pause untouched,
    nothing saved/suppressed/started, no PROCESS while AGENT held), and the
    bounded rebuild on the CURRENT generation converges to a clean apply.
    """
    import api.routes as routes

    session, calls, proxy = _install_immutable_owner_token_fixture(
        monkeypatch, tmp_path, recovered=True
    )
    sid = session.session_id
    original_pause = dict(session.process_wakeup_pause)
    try:
        generation = {"value": "gen-1"}
        monkeypatch.setattr(
            routes,
            "process_wakeup_credential_state_fingerprint",
            lambda _s: generation["value"],
        )

        token, owner, revalidation = _build_token_and_revalidate(routes, sid)
        assert owner is session
        assert token["credential_state_fingerprint"] == "gen-1"

        go = threading.Event()
        done = threading.Event()

        def _bump_generation():
            generation["value"] = "gen-2"

        writer = _run_concurrent_writer(go, done, _bump_generation)
        go.set()
        assert done.wait(10), "generation writer never ran"
        writer.join(10)

        proxy.arm()
        result = routes._apply_process_wakeup_revalidation_once(
            token, revalidation, model="test-model", provider="test-provider"
        )
        assert not proxy.attempted.is_set(), (
            "stale-generation apply attempted PROCESS while AGENT was held (AB/BA)"
        )
        assert result["retry"] is True, result
        assert result["reason"] == "credential_state_changed", result
        assert dict(session.process_wakeup_pause) == original_pause
        assert calls["start"] == [] and calls["save"] == [] and calls["suppress"] == []
        # The next revalidation runs legitimately OUTSIDE AGENT (it takes
        # PROCESS) — stop recording before it, then re-arm for the apply.
        proxy.disarm()

        # Bounded retry: the rebuild captures the CURRENT generation and the
        # apply converges on the live owner (pause cleared on the new
        # generation) — the stale answer was never applied.
        token2, owner2, revalidation2 = _build_token_and_revalidate(routes, sid)
        assert owner2 is session
        assert token2["credential_state_fingerprint"] == "gen-2"
        proxy.arm()
        result2 = routes._apply_process_wakeup_revalidation_once(
            token2, revalidation2, model="test-model", provider="test-provider"
        )
        assert not proxy.attempted.is_set(), (
            "current-generation apply attempted PROCESS while AGENT was held (AB/BA)"
        )
        assert result2["retry"] is False and result2["error"] is None, result2
        live_pause = getattr(session, "process_wakeup_pause", None)
        assert not (isinstance(live_pause, dict) and live_pause.get("paused"))
        assert calls["start"] == []
        assert proxy.acquire(blocking=False), "a thread leaked the ownership lock"
        proxy.release()
    finally:
        with config.LOCK:
            config.SESSIONS.pop(sid, None)


def test_revalidation_owner_replaced_before_run_acceptance(monkeypatch, tmp_path):
    """Owner replacement between validation/apply and run acceptance must be
    caught by the validation-to-start fence (#6327 B4).

    After the revalidation answer was applied and the token rebuilt fresh
    (the production retry-loop shape), a concurrent writer replaces the
    canonical owner under the same SID.  ``_validate_start_owner_fence`` must
    fire (``owner_replaced``) while the caller still holds the canonical AGENT
    lock: no run is started on the stale owner, no PROCESS is attempted, and
    the replacement object is left untouched.  The control assertion proves
    the same token passes the fence while the canonical owner is unchanged.
    """
    import api.routes as routes

    session, calls, proxy = _install_immutable_owner_token_fixture(
        monkeypatch, tmp_path, recovered=True
    )
    sid = session.session_id
    try:
        token, owner, revalidation = _build_token_and_revalidate(routes, sid)
        assert owner is session
        proxy.arm()
        applied = routes._apply_process_wakeup_revalidation_once(
            token, revalidation, model="test-model", provider="test-provider"
        )
        assert not proxy.attempted.is_set(), (
            "apply attempted PROCESS while AGENT was held (AB/BA)"
        )
        assert applied["retry"] is False and applied["error"] is None, applied
        # Production rebuilds the token after the apply so the acceptance
        # token is fresh relative to the (cleared) pause state.
        fresh_token, fresh_owner = routes._build_immutable_session_owner_token(
            sid, model="test-model", provider="test-provider"
        )
        assert fresh_owner is session
        assert fresh_token["pause_state"] == {}

        session_lock = config._get_session_agent_lock(sid)
        # Control: with the canonical owner unchanged the fence passes.
        assert routes._validate_start_owner_fence(fresh_token, session_lock) is None

        # Concurrent writer replaces the owner (same SID) before acceptance.
        replacement = _StreamingFakeSession(sid, "alpha", tmp_path, "issue6327-stream")
        replacement.process_wakeup_pause = dict(session.process_wakeup_pause)
        go = threading.Event()
        done = threading.Event()

        def _replace_owner():
            with config.LOCK:
                config.SESSIONS[sid] = replacement

        writer = _run_concurrent_writer(go, done, _replace_owner)
        go.set()
        assert done.wait(10), "replacement writer never ran"
        writer.join(10)

        proxy.arm()
        fence_error = routes._validate_start_owner_fence(fresh_token, session_lock)
        assert not proxy.attempted.is_set(), (
            "fence attempted PROCESS while AGENT was held (AB/BA)"
        )
        assert fence_error == "owner_replaced", fence_error
        # The acceptance gate refuses: no run started, replacement untouched.
        assert calls["start"] == []
        assert getattr(replacement, "process_wakeup_pause", None) == dict(
            session.process_wakeup_pause
        )
        assert proxy.acquire(blocking=False), "a thread leaked the ownership lock"
        proxy.release()
    finally:
        with config.LOCK:
            config.SESSIONS.pop(sid, None)
