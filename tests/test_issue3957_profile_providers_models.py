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
  - ``api.profiles.profile_env_for_active_request()`` applies the active
    per-request profile's env around the read; no-op for the default profile.
"""

import os
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
    with profiles.profile_env_for_active_request("test"):
        # No env mutation, no HERMES_HOME change for the default profile.
        assert os.environ.get("ISSUE_3957_PROBE") is None
    assert os.environ.get("ISSUE_3957_PROBE") is None


def test_active_request_env_applies_named_profile_env(monkeypatch, tmp_path):
    """A named profile's .env is applied inside the block and restored after."""
    base = tmp_path / ".hermes"
    (base / "profiles" / "work").mkdir(parents=True)
    (base / "profiles" / "work" / ".env").write_text(
        "ISSUE_3957_PROBE=from-work-profile\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.delenv("ISSUE_3957_PROBE", raising=False)

    # Simulate the per-request cookie context (issue #798).
    profiles.set_request_profile("work")
    try:
        assert profiles.get_active_profile_name() == "work"
        assert os.environ.get("ISSUE_3957_PROBE") is None
        with profiles.profile_env_for_active_request("test"):
            assert os.environ.get("ISSUE_3957_PROBE") == "from-work-profile"
        # Restored after the block exits.
        assert os.environ.get("ISSUE_3957_PROBE") is None
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

    profiles.set_request_profile("work")
    try:
        with_raised = False
        try:
            with profiles.profile_env_for_active_request("test"):
                assert os.environ.get("ISSUE_3957_PROBE") == "from-work-profile"
                raise ValueError("boom")
        except ValueError:
            with_raised = True
        assert with_raised
        assert os.environ.get("ISSUE_3957_PROBE") is None
    finally:
        profiles.clear_request_profile()


def test_providers_and_models_routes_wrap_in_profile_env():
    """The two read routes are profile-scoped for non-default profiles (#3957).

    Structural guard: a future refactor that drops the wiring would silently
    reintroduce the bug, so pin it at the source level.
      - /api/providers wraps the synchronous read in profile_env_for_active_request.
      - /api/models relies on get_available_models() scoping its detached
        rebuild worker via profile_scope_for_detached_worker (the request-thread
        wrapper cannot reach the worker thread — Codex CORE finding).
    """
    routes_src = Path(profiles.__file__).resolve().parent.joinpath("routes.py").read_text(
        encoding="utf-8"
    )
    assert "profile_env_for_active_request" in routes_src
    config_src = Path(config.__file__).resolve().read_text(encoding="utf-8")
    assert "profile_scope_for_detached_worker" in config_src
    assert "_get_models_cache_path" in config_src


# ─────────────────────────────────────────────────────────────────────────────
# Facet A (worker thread) — the detached models-rebuild worker is profile-scoped
# (Codex CORE finding: the request-thread wrapper cannot reach the worker thread)
# ─────────────────────────────────────────────────────────────────────────────


def test_detached_worker_scope_noop_for_default_profile(monkeypatch):
    """profile_scope_for_detached_worker is a no-op for the default profile."""
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n in ("", "default"))
    monkeypatch.delenv("ISSUE_3957_WPROBE", raising=False)
    # Default/empty name → no TLS set, no env applied.
    with profiles.profile_scope_for_detached_worker("default", "test"):
        assert profiles.get_active_profile_name() in ("", "default")
        assert os.environ.get("ISSUE_3957_WPROBE") is None
    with profiles.profile_scope_for_detached_worker("", "test"):
        assert os.environ.get("ISSUE_3957_WPROBE") is None


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

