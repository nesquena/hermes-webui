"""Adversarial regression tests for #6516: slug collision, x-ai identity.

Covers three follow-up defects found by Codex re-gate:

  1. Lossy slugging collision: two ``custom_providers`` names that slugify
     to the same string (e.g. "foo:bar" and "foo-bar") must not silently
     route to the wrong endpoint. ``_get_provider_base_url`` must return
     None (fail closed) when a slug match is ambiguous.

  2. Provider-key cleanup for colon-named entries: ``_clean_provider_key_from_config``
     must correctly match a custom provider whose ``model.provider`` is
     ``custom:192.168.5.242:8000`` (colon form) against the custom_providers
     entry whose ``name`` is ``192.168.5.242:8000``.

  3. Active-provider identity for ``x-ai``: ``_resolve_configured_provider_id``
     must preserve ``x-ai`` verbatim (it is in ``_PROVIDER_DISPLAY``) rather
     than alias-resolving it to ``xai``, so the provider card and active
     badge stay matched.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

import api.config as config
import api.profiles as profiles
import api.streaming as streaming  # noqa: F401  (used by r5 composed tests)
from api.providers import get_providers


@pytest.fixture(autouse=True)
def _isolate_models_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    config.invalidate_models_cache()
    yield
    config.invalidate_models_cache()


# ── Issue 1: slug collision disambiguation ──────────────────────────────


def test_get_provider_base_url_unique_slug_succeeds():
    """A slug that matches exactly one custom_providers entry must return
    that entry's base_url."""
    cfg = {
        "model": {"default": "default-model", "provider": "custom:node-a-8000"},
        "custom_providers": [
            {"name": "node-a:8000", "base_url": "http://node-a:8000/v1"},
        ],
    }
    old_cfg = dict(config.cfg)
    config.cfg.clear()
    config.cfg.update(json.loads(json.dumps(cfg)))
    try:
        url = config._get_provider_base_url("custom:node-a-8000")
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
    assert url == "http://node-a:8000/v1", f"Expected unique slug URL, got {url!r}"


def test_get_provider_base_url_slug_collision_returns_none():
    """When two custom_providers names slugify to the same string,
    ``_get_provider_base_url`` must return None (fail closed) rather
    than silently returning the first match."""
    # "foo:bar" and "foo-bar" both slugify to "custom:foo-bar"
    cfg = {
        "model": {"default": "default-model", "provider": "custom:foo-bar"},
        "custom_providers": [
            {"name": "foo:bar", "base_url": "http://wrong:8000/v1"},
            {"name": "foo-bar", "base_url": "http://right:8000/v1"},
        ],
    }
    old_cfg = dict(config.cfg)
    config.cfg.clear()
    config.cfg.update(json.loads(json.dumps(cfg)))
    try:
        url = config._get_provider_base_url("custom:foo-bar")
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)

    assert url is None, (
        f"Expected None on ambiguous slug collision, got {url!r}"
    )


def test_resolve_model_provider_default_still_works_after_collision():
    """When the active provider matches the model section, its URL must
    still resolve correctly even when a slug collision exists in the
    custom_providers list.  The active model path (#1) runs before
    the list scan (#3) so collisions don't break the default."""
    cfg = {
        "model": {"default": "default-model", "provider": "custom:my-server",
                  "base_url": "http://my-server:8000/v1"},
        "custom_providers": [
            {"name": "my:server", "base_url": "http://collision:5000/v1"},
            {"name": "my-server", "base_url": "http://my-server:8000/v1"},
        ],
    }
    old_cfg = dict(config.cfg)
    config.cfg.clear()
    config.cfg.update(json.loads(json.dumps(cfg)))
    try:
        # This should return the model section URL, not the first
        # custom_providers entry's URL.
        url = config._get_provider_base_url("custom:my-server")
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)

    # The model section match (#2) runs before the list scan (#3),
    # so even though the list has an entry for "custom:my-server",
    # the model block is authoritative for the active provider.
    assert url == "http://my-server:8000/v1", (
        f"Expected the active model's base_url, got {url!r}"
    )


# ── Issue 2: provider-key cleanup with colon-named entries ──────────────


def test_custom_provider_name_matches_colon_against_hyphen():
    """``_custom_provider_name_matches`` must return True when
    provider_id uses colons (e.g. ``custom:192.168.5.242:8000``)
    and the entry name uses a colon that slugifies to a hyphen."""
    from api.providers import _custom_provider_name_matches

    # provider_id = "custom:192.168.5.242:8000" (with colon)
    # name = "192.168.5.242:8000" → slug = "custom:192.168.5.242-8000"
    assert _custom_provider_name_matches(
        "custom:192.168.5.242:8000", "192.168.5.242:8000"
    ), "Colon-bearing provider_id should match colon-bearing entry name"


def test_custom_provider_name_matches_hyphen_against_colon():
    """``_custom_provider_name_matches`` must return True when
    provider_id uses hyphens (e.g. ``custom:192.168.5.242-8000``)
    and the entry name uses colons."""
    from api.providers import _custom_provider_name_matches

    assert _custom_provider_name_matches(
        "custom:192.168.5.242-8000", "192.168.5.242:8000"
    ), "Hyphenated provider_id should match colon-bearing entry name"


def test_custom_provider_name_matches_no_match():
    """``_custom_provider_name_matches`` must return False for an
    unrelated provider_id."""
    from api.providers import _custom_provider_name_matches

    assert not _custom_provider_name_matches(
        "openai", "192.168.5.242:8000"
    ), "openai should not match a custom provider name"


# ── Issue 3: x-ai provider identity preserved ───────────────────────────


def test_resolve_configured_provider_id_preserves_x_ai():
    """``_resolve_configured_provider_id`` must return ``x-ai`` verbatim
    since it is in ``_PROVIDER_DISPLAY``, not alias-resolve it to ``xai``."""
    result = config._resolve_configured_provider_id("x-ai")
    assert result == "x-ai", (
        f"Expected 'x-ai' preserved verbatim, got {result!r}"
    )


def test_resolve_configured_provider_id_preserves_known_canonicals():
    """Other known canonical IDs must also be preserved verbatim."""
    for pid in ("anthropic", "openai", "deepseek", "google", "openai-codex"):
        result = config._resolve_configured_provider_id(pid)
        assert result == pid, (
            f"Expected {pid!r} preserved, got {result!r}"
        )


def test_resolve_configured_provider_id_still_aliases_unknown():
    """An unknown ID that does NOT exist in _PROVIDER_DISPLAY or
    _PROVIDER_MODELS must still be alias-resolved."""
    result = config._resolve_configured_provider_id("google-gemini")
    # google-gemini is not in _PROVIDER_DISPLAY/_PROVIDER_MODELS directly
    # (google is).  It should be alias-resolved.
    assert result != "google-gemini", (
        f"Expected alias resolution for 'google-gemini', got {result!r}"
    )


def test_get_providers_active_provider_x_ai(tmp_path, monkeypatch):
    """``get_providers()`` must report ``x-ai`` as the active provider
    when ``model.provider`` is ``x-ai``, not alias-resolved ``xai``."""
    import api.config as cfg_mod

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "model:\n"
        "  default: grok-4.20\n"
        "  provider: x-ai\n"
        "  base_url: https://api.x.ai/v1\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(cfg_mod, "_get_config_path", lambda: config_path)
    cfg_mod.reload_config()

    try:
        result = get_providers()
    finally:
        cfg_mod.reload_config()

    ap = result.get("active_provider")
    assert ap == "x-ai", (
        f"Expected active_provider 'x-ai', got {ap!r}. "
        "x-ai must not be alias-resolved to xai."
    )


# ── Issue 4: two-profile production-composition regression ──────────────────


def test_resolve_custom_provider_runtime_overrides_uses_config_data():
    """``_resolve_custom_provider_runtime_overrides`` must consult the
    ``config_data`` dict (the target profile's config) rather than the
    ambient ``get_config()`` when resolving a named ``custom:*`` provider.

    This is the production-composition regression: when the streaming worker
    runs under profile A but a session is routed to profile B, the initial
    send and both credential self-heal retries must pick up profile B's
    URL/key sentinels, not profile A's.
    """
    from api.streaming import _resolve_custom_provider_runtime_overrides

    profile_a_cfg = {
        "model": {"default": "model-a", "provider": "custom:worker",
                  "base_url": "http://profile-a:8000/v1"},
        "custom_providers": [
            {"name": "worker", "base_url": "http://profile-a:8000/v1",
             "api_key": "profile-a-key"},
        ],
    }
    profile_b_cfg = {
        "model": {"default": "model-b", "provider": "custom:worker",
                  "base_url": "http://profile-b:9000/v1"},
        "custom_providers": [
            {"name": "worker", "base_url": "http://profile-b:9000/v1",
             "api_key": "profile-b-key"},
        ],
    }

    # Initial send: must use profile B's config, not the ambient (A) config.
    provider, key, url = _resolve_custom_provider_runtime_overrides(
        "custom:worker", None, None,
        config_data=profile_b_cfg,
    )
    assert url == "http://profile-b:9000/v1", (
        f"Initial send: expected profile-b URL, got {url!r}"
    )
    assert key == "profile-b-key", (
        f"Initial send: expected profile-b key, got {key!r}"
    )
    assert provider == "custom", (
        f"Initial send: expected collapsed 'custom' provider, got {provider!r}"
    )

    # Retry path 1 (self-heal on 401): same config_data must still win.
    provider, key, url = _resolve_custom_provider_runtime_overrides(
        "custom:worker", None, None,
        config_data=profile_b_cfg,
    )
    assert url == "http://profile-b:9000/v1", (
        f"Retry 1: expected profile-b URL, got {url!r}"
    )
    assert key == "profile-b-key", (
        f"Retry 1: expected profile-b key, got {key!r}"
    )

    # Retry path 2 (except-path self-heal): same config_data must still win.
    provider, key, url = _resolve_custom_provider_runtime_overrides(
        "custom:worker", None, None,
        config_data=profile_b_cfg,
    )
    assert url == "http://profile-b:9000/v1", (
        f"Retry 2: expected profile-b URL, got {url!r}"
    )
    assert key == "profile-b-key", (
        f"Retry 2: expected profile-b key, got {key!r}"
    )


# ── Issue 6: URL-less collision fail-closed ───────────────────────────────────


def test_get_provider_base_url_url_less_collision_fails_closed():
    """When two custom_providers entries slugify to the same slug but one has
    a blank base_url, _get_provider_base_url must return None — NOT the
    sibling's URL.

    Before the fix, slug_matches only appended URL-bearing entries, so the
    URL-less colliding entry was invisible to the len > 1 check and the
    sibling's URL was returned.  After the fix, ALL slug-matching entries
    are counted, so the collision is detected and None is returned.
    """
    import api.config as config
    import json

    cfg = {
        "model": {"default": "test-model", "provider": "openai"},
        "custom_providers": [
            {"name": "foo:bar", "base_url": "http://sibling.example/v1",
             "api_key": "sibling-key"},
            {"name": "foo-bar", "base_url": "", "api_key": ""},
        ],
    }
    old_cfg = dict(config.cfg)
    config.cfg.clear()
    config.cfg.update(json.loads(json.dumps(cfg)))
    try:
        url = config._get_provider_base_url("custom:foo-bar")
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
    assert url is None, (
        f"URL-less collision must fail closed (return None), got {url!r}"
    )


def test_get_provider_base_url_single_match_with_url_returns_url():
    """When exactly one entry matches (even if a sibling with a blank URL
    exists but doesn't slug-match), the URL is returned.
    """
    import api.config as config
    import json

    cfg = {
        "model": {"default": "test-model", "provider": "openai"},
        "custom_providers": [
            {"name": "foo:bar", "base_url": "http://valid.example/v1",
             "api_key": "valid-key"},
            {"name": "other:provider", "base_url": "", "api_key": ""},
        ],
    }
    old_cfg = dict(config.cfg)
    config.cfg.clear()
    config.cfg.update(json.loads(json.dumps(cfg)))
    try:
        url = config._get_provider_base_url("custom:foo-bar")
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
    assert url == "http://valid.example/v1", (
        f"Single match should return URL, got {url!r}"
    )


# ── Issue 7: custom:slug identity preserved through retry ─────────────────────


def test_resolve_custom_provider_runtime_overrides_preserves_identity():
    """_resolve_custom_provider_runtime_overrides must collapse custom:slug
    to plain 'custom' on the initial resolution, but the caller (streaming
    worker) must retain the original custom:slug for retry paths.

    This test verifies that calling _resolve_custom_provider_runtime_overrides
    with a custom:slug provider returns 'custom' as the provider (collapsed),
    and that a second call with the collapsed 'custom' value does NOT re-enter
    the custom: resolution path (returns 'custom' unchanged).
    """
    from api.streaming import _resolve_custom_provider_runtime_overrides

    profile_cfg = {
        "model": {"default": "model-a", "provider": "custom:worker",
                  "base_url": "http://profile-a:8000/v1"},
        "custom_providers": [
            {"name": "worker", "base_url": "http://profile-a:8000/v1",
             "api_key": "profile-a-key"},
        ],
    }

    # Initial resolution: custom:worker → custom (collapsed)
    provider, key, url = _resolve_custom_provider_runtime_overrides(
        "custom:worker", None, None,
        config_data=profile_cfg,
    )
    assert provider == "custom", (
        f"Initial: expected collapsed 'custom', got {provider!r}"
    )
    assert url == "http://profile-a:8000/v1", (
        f"Initial: expected profile-a URL, got {url!r}"
    )
    assert key == "profile-a-key", (
        f"Initial: expected profile-a key, got {key!r}"
    )

    # Retry with collapsed 'custom': must NOT re-enter custom: path
    provider, key, url = _resolve_custom_provider_runtime_overrides(
        "custom", None, None,
        config_data=profile_cfg,
    )
    assert provider == "custom", (
        f"Retry: collapsed 'custom' must pass through, got {provider!r}"
    )
    assert url is None, (
        f"Retry: collapsed 'custom' must not resolve URL, got {url!r}"
    )

    # Retry with original custom:worker: must re-resolve from config_data
    provider, key, url = _resolve_custom_provider_runtime_overrides(
        "custom:worker", None, None,
        config_data=profile_cfg,
    )
    assert provider == "custom", (
        f"Retry: expected collapsed 'custom', got {provider!r}"
    )
    assert url == "http://profile-a:8000/v1", (
        f"Retry: expected profile-a URL, got {url!r}"
    )
    assert key == "profile-a-key", (
        f"Retry: expected profile-a key, got {key!r}"
    )


# ── Issue 8: target-profile authority over truthy ambient runtime values ──────


def test_runtime_overrides_target_profile_overrides_truthy_ambient():
    """When config_data is provided, the target profile's URL/key must
    override conflicting truthy runtime values that may have been resolved
    from the ambient process-global config.

    Before the fix, _resolve_custom_provider_runtime_overrides only replaced
    URL/key when incoming runtime values were falsey, so a truthy ambient
    URL/key survived over the explicit target-profile row.
    """
    from api.streaming import _resolve_custom_provider_runtime_overrides

    target_cfg = {
        "model": {"default": "model-a", "provider": "custom:worker",
                  "base_url": "http://target-profile:9000/v1"},
        "custom_providers": [
            {"name": "worker", "base_url": "http://target-profile:9000/v1",
             "api_key": "target-profile-key"},
        ],
    }

    # Ambient runtime values are truthy — they must be overridden by the
    # target profile's URL/key.
    provider, key, url = _resolve_custom_provider_runtime_overrides(
        "custom:worker", "ambient-key", "http://ambient:8000/v1",
        config_data=target_cfg,
    )
    assert provider == "custom", (
        f"Expected collapsed 'custom', got {provider!r}"
    )
    assert url == "http://target-profile:9000/v1", (
        f"Target profile URL must override truthy ambient URL, got {url!r}"
    )
    assert key == "target-profile-key", (
        f"Target profile key must override truthy ambient key, got {key!r}"
    )


def test_runtime_overrides_no_config_data_falls_back_to_ambient():
    """When config_data is None, the old behavior is preserved: only
    override URL/key when incoming runtime values are falsey.
    """
    from api.streaming import _resolve_custom_provider_runtime_overrides

    # Without config_data, truthy ambient values survive.
    provider, key, url = _resolve_custom_provider_runtime_overrides(
        "custom:worker", "ambient-key", "http://ambient:8000/v1",
        config_data=None,
    )
    assert provider == "custom", (
        f"Expected collapsed 'custom', got {provider!r}"
    )
    assert url == "http://ambient:8000/v1", (
        f"Without config_data, ambient URL must survive, got {url!r}"
    )
    assert key == "ambient-key", (
        f"Without config_data, ambient key must survive, got {key!r}"
    )


# ── Issue 9: env-guard blocks process-env fallback for target-owned ${ENV} ─────


def test_resolve_custom_provider_connection_blocks_process_env_for_target(monkeypatch):
    """When config_data is provided, ${ENV} and key_env lookups must NOT
    fall back to process-env variables from a different profile.

    The target profile's custom_providers entry has api_key='${TARGET_ENV}'
    and key_env='TARGET_KEY_ENV'. We set both a process-env variable and a
    thread-local env variable. The target-owned ${ENV} should resolve from
    the thread-local env, and the process-env variable must be ignored.
    """
    import api.config as config

    # Set a process-env variable that should NOT be used.
    monkeypatch.setenv("TARGET_ENV", "process-env-value")
    monkeypatch.setenv("TARGET_KEY_ENV", "process-env-key-env-value")

    target_cfg = {
        "custom_providers": [
            {"name": "worker", "base_url": "http://target:9000/v1",
             "api_key": "${TARGET_ENV}"},
        ],
    }

    # Without thread-local env, ${ENV} should resolve to empty (process-env
    # blocked) — the key is not found.
    api_key, base_url = config.resolve_custom_provider_connection(
        "custom:worker", config_data=target_cfg,
    )
    assert api_key is None or api_key == "", (
        f"Process-env must be blocked for target-owned ${{ENV}}, got {api_key!r}"
    )
    assert base_url == "http://target:9000/v1"

    # Now set the thread-local env — ${ENV} should resolve from it.
    # Preserve any pre-existing thread-local env exactly (presence + value) so
    # a combined test process or a prior profile scope is not erased (#6516 r5).
    _prior_env_exists = hasattr(config._thread_ctx, "env")
    _prior_env_value = getattr(config._thread_ctx, "env", None)
    config._thread_ctx.env = {"TARGET_ENV": "thread-local-value"}
    try:
        api_key, base_url = config.resolve_custom_provider_connection(
            "custom:worker", config_data=target_cfg,
        )
        assert api_key == "thread-local-value", (
            f"Thread-local env must be used for ${{ENV}}, got {api_key!r}"
        )
    finally:
        if _prior_env_exists:
            config._thread_ctx.env = _prior_env_value
        else:
            try:
                del config._thread_ctx.env
            except AttributeError:
                pass


def test_resolve_custom_provider_connection_key_env_blocks_process_env(monkeypatch):
    """key_env must also be blocked from process-env fallback when
    config_data is provided.
    """
    import api.config as config

    monkeypatch.setenv("TARGET_KEY_ENV", "process-env-key-env-value")

    target_cfg = {
        "custom_providers": [
            {"name": "worker", "base_url": "http://target:9000/v1",
             "key_env": "TARGET_KEY_ENV"},
        ],
    }

    # Without thread-local env, key_env should resolve to empty (process-env
    # blocked).
    api_key, base_url = config.resolve_custom_provider_connection(
        "custom:worker", config_data=target_cfg,
    )
    assert api_key is None or api_key == "", (
        f"Process-env must be blocked for target-owned key_env, got {api_key!r}"
    )
    assert base_url == "http://target:9000/v1"

    # With thread-local env, key_env should resolve from it.
    # Preserve any pre-existing thread-local env exactly (presence + value) so
    # a combined test process or a prior profile scope is not erased (#6516 r5).
    _prior_env_exists = hasattr(config._thread_ctx, "env")
    _prior_env_value = getattr(config._thread_ctx, "env", None)
    config._thread_ctx.env = {"TARGET_KEY_ENV": "thread-local-key-env-value"}
    try:
        api_key, base_url = config.resolve_custom_provider_connection(
            "custom:worker", config_data=target_cfg,
        )
        assert api_key == "thread-local-key-env-value", (
            f"Thread-local env must be used for key_env, got {api_key!r}"
        )
    finally:
        if _prior_env_exists:
            config._thread_ctx.env = _prior_env_value
        else:
            try:
                del config._thread_ctx.env
            except AttributeError:
                pass


# ── Issue 10: colon/hyphen active-model authority ─────────────────────────────


def test_get_provider_base_url_colon_hyphen_active_model_authority():
    """When the active model.provider is custom:foo:bar and the requested
    provider_id is custom:foo-bar (or vice versa), the model section's URL
    must be used as the trusted authority — they canonicalize to the same
    slug.

    Before the fix, _get_provider_base_url compared raw lowercase strings,
    so custom:foo:bar did not match custom:foo-bar and the trusted active-model
    URL was ignored.
    """
    import api.config as config
    import json

    cfg = {
        "model": {"default": "test-model", "provider": "custom:foo:bar",
                  "base_url": "http://active-model:8000/v1"},
        "custom_providers": [
            {"name": "foo-bar", "base_url": "http://colliding:9000/v1"},
            {"name": "foo:bar", "base_url": "http://colliding:9000/v1"},
        ],
    }
    old_cfg = dict(config.cfg)
    config.cfg.clear()
    config.cfg.update(json.loads(json.dumps(cfg)))
    try:
        # Requested as custom:foo-bar — should match active custom:foo:bar
        # via canonicalization and return the active-model URL.
        url = config._get_provider_base_url("custom:foo-bar")
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
    assert url == "http://active-model:8000/v1", (
        f"Canonicalized active-model authority must win, got {url!r}"
    )


def test_resolve_custom_provider_connection_colon_hyphen_authority():
    """resolve_custom_provider_connection must use the canonicalized
    model.provider to discriminate collisions.

    When model.provider is custom:foo:bar and the requested provider is
    custom:foo-bar, the expected URL from the model section must be used
    to select the unique colliding entry.
    """
    import api.config as config

    cfg = {
        "model": {"default": "test-model", "provider": "custom:foo:bar",
                  "base_url": "http://active-model:8000/v1"},
        "custom_providers": [
            {"name": "foo-bar", "base_url": "http://colliding:9000/v1",
             "api_key": "colliding-key"},
            {"name": "foo:bar", "base_url": "http://active-model:8000/v1",
             "api_key": "active-model-key"},
        ],
    }
    api_key, base_url = config.resolve_custom_provider_connection(
        "custom:foo-bar", config_data=cfg,
    )
    assert base_url == "http://active-model:8000/v1", (
        f"Canonicalized authority must select the right entry, got {base_url!r}"
    )
    assert api_key == "active-model-key", (
        f"Canonicalized authority must select the right key, got {api_key!r}"
    )


# ── Issue 11: detached-worker 401 self-heal stays scoped to owning profile ────


def test_self_heal_reads_owning_profile_auth_json(monkeypatch, tmp_path):
    """A 401 self-heal initiated from a profile-A ambient context must read
    profile-B's auth.json, not profile A's.

    This is the detached-worker retry test: the streaming worker thread is
    a detached thread that does NOT inherit the per-request profile TLS.
    The self-heal must explicitly scope to the owning profile so the cache
    owner stays profile B.
    """
    import threading
    import contextlib

    # Set up two profile homes with distinct auth.json files.
    profile_a_home = tmp_path / "profile_a"
    profile_b_home = tmp_path / "profile_b"
    profile_a_home.mkdir()
    profile_b_home.mkdir()

    # Profile A's auth.json has a stale key.
    (profile_a_home / "auth.json").write_text(
        '{"stale": true}', encoding="utf-8"
    )
    # Profile B's auth.json has the fresh key.
    (profile_b_home / "auth.json").write_text(
        '{"provider_credentials": {"custom:test": {"api_key": "fresh-b-key"}}}',
        encoding="utf-8",
    )

    # Track which auth.json was read.
    read_auth_paths = []

    # Patch read_auth_json at its source module (api.oauth) since
    # _attempt_credential_self_heal does a local import from api.oauth.
    import api.oauth as oauth_mod
    original_read_auth = oauth_mod.read_auth_json

    def _recording_read_auth(auth_path=None):
        read_auth_paths.append(str(auth_path) if auth_path else "DEFAULT_AUTH_JSON_PATH")
        return original_read_auth(auth_path)

    monkeypatch.setattr(oauth_mod, "read_auth_json", _recording_read_auth)

    # Patch resolve_runtime_provider_with_anthropic_env_lock at api.oauth.
    monkeypatch.setattr(
        oauth_mod,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda resolver, *args, **kwargs: {
            "provider": "custom",
            "api_key": "fresh-b-key",
            "base_url": "http://gpu.local:8000/v1",
        },
    )

    # Patch resolve_runtime_provider at hermes_cli.runtime_provider.
    import hermes_cli.runtime_provider as rtp_mod
    monkeypatch.setattr(
        rtp_mod,
        "resolve_runtime_provider",
        lambda requested=None, target_model=None: {
            "provider": "custom",
            "api_key": "fresh-b-key",
            "base_url": "http://gpu.local:8000/v1",
        },
    )

    # Patch invalidate_credential_pool_cache at api.config.
    monkeypatch.setattr(
        config,
        "invalidate_credential_pool_cache",
        lambda provider_id: None,
    )

    # Patch SESSION_AGENT_CACHE and its lock at api.config.
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", {})
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE_LOCK", threading.Lock())

    # Patch _close_cached_agent_entry_at_session_boundary at api.streaming.
    import api.streaming as streaming_mod
    monkeypatch.setattr(
        streaming_mod,
        "_close_cached_agent_entry_at_session_boundary",
        lambda session_id, entry: None,
    )

    # Patch profile_scope_for_detached_worker at api.profiles to be a no-op
    # context manager so the test doesn't depend on the filesystem profile
    # layout. The self-heal's _do_self_heal closure already uses _auth_path
    # (derived from profile_home) to read the correct auth.json.
    @contextlib.contextmanager
    def _noop_profile_scope(profile_name, purpose="test", logger_override=None):
        yield

    monkeypatch.setattr(
        profiles,
        "profile_scope_for_detached_worker",
        _noop_profile_scope,
    )

    # Call self-heal with profile B's context.
    result = streaming_mod._attempt_credential_self_heal(
        "custom:test",
        "test-session-id",
        _agent_lock_ref=None,
        target_model="test-model",
        profile_name="work",
        profile_home=str(profile_b_home),
        original_custom_provider="custom:test",
    )

    # The self-heal must have read profile B's auth.json, not profile A's.
    assert len(read_auth_paths) == 1
    assert str(profile_b_home / "auth.json") in read_auth_paths[0]
    assert str(profile_a_home / "auth.json") not in read_auth_paths[0]

    # The result should contain the fresh key from profile B.
    assert result is not None
    assert result.get("api_key") == "fresh-b-key"


def test_self_heal_without_profile_falls_back_to_default(monkeypatch):
    """When no profile_name is provided, self-heal uses the default auth.json path."""
    import threading

    import api.oauth as oauth_mod
    read_auth_paths = []
    original_read_auth = oauth_mod.read_auth_json

    def _recording_read_auth(auth_path=None):
        read_auth_paths.append(str(auth_path) if auth_path else "DEFAULT_AUTH_JSON_PATH")
        return original_read_auth(auth_path)

    monkeypatch.setattr(oauth_mod, "read_auth_json", _recording_read_auth)
    monkeypatch.setattr(
        oauth_mod,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda resolver, *args, **kwargs: {
            "provider": "openai",
            "api_key": "fresh-key",
            "base_url": "https://api.openai.com/v1",
        },
    )

    import hermes_cli.runtime_provider as rtp_mod
    monkeypatch.setattr(
        rtp_mod,
        "resolve_runtime_provider",
        lambda requested=None, target_model=None: {
            "provider": "openai",
            "api_key": "fresh-key",
            "base_url": "https://api.openai.com/v1",
        },
    )

    import api.streaming as streaming_mod
    monkeypatch.setattr(config, "invalidate_credential_pool_cache", lambda provider_id: None)
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", {})
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE_LOCK", threading.Lock())
    monkeypatch.setattr(
        streaming_mod,
        "_close_cached_agent_entry_at_session_boundary",
        lambda session_id, entry: None,
    )

    result = streaming_mod._attempt_credential_self_heal(
        "openai",
        "test-session-id",
        _agent_lock_ref=None,
        target_model="test-model",
        profile_name=None,
        profile_home=None,
        original_custom_provider=None,
    )

    # Must have used the default auth.json path (no profile_home).
    assert len(read_auth_paths) == 1
    assert read_auth_paths[0] == "DEFAULT_AUTH_JSON_PATH"


# ── Round-5: production-composed streaming 401 self-heal ownership ───────────
# These tests drive the real _run_agent_streaming() (not helper-level) through
# the returned-401 credential self-heal path and assert the actual AIAgent
# constructor values on both the initial send and the retry stay on the owning
# profile's custom:<slug> URL/key — never an ambient process-global sentinel —
# and that the self-heal invalidates the NAMED custom:<slug> identity (#6516 r5).


import queue
import sys
import types

import api.models as _api_models
from api.models import Session as _Session


def _r5_make_session(tmp_path, session_id, model, msg):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    session = _Session(
        session_id=session_id,
        title="r5 streaming",
        workspace=str(tmp_path),
        model=model,
        messages=[],
        context_messages=[],
    )
    session.pending_user_message = msg
    session.pending_started_at = 1.0
    session.save()
    return session, session_dir


def _msg_mentions_authority(payload) -> bool:
    """True if an SSE ``apperror`` payload's message mentions custom-provider
    authority failure (used to assert an abort surfaced a user-visible error
    rather than silently dropping the request)."""
    _text = str((payload or {}).get('message') or (payload or {}).get('details') or '')
    _lower = _text.lower()
    return ('authority' in _lower and 'custom' in _lower) or 'custom provider' in _lower


def _r5_drive_streaming_401(
    tmp_path,
    monkeypatch,
    *,
    target_cfg,
    ambient_key,
    ambient_url,
    mode="returned",
    credential_pool=None,
):
    """Drive the real ``_run_agent_streaming()`` through a 401 self-heal path
    for a named custom:<slug> provider.

    *mode* selects which self-heal branch ``run_conversation`` triggers:
      - ``"returned"``: the agent returns an auth-classified error result
        (the returned-401 self-heal path, ~line 9678).
      - ``"raised"``:  the agent raises an auth-classified exception (the
        except-path self-heal, ~line 10898/10969).

    Returns ``(constructions, invalidated_ids, apperrors)`` where
    *constructions* is the list of AIAgent constructor-kwargs dicts (initial
    send and any retries), *invalidated_ids* is the list of provider ids handed
    to ``invalidate_credential_pool_cache`` during self-heal, and *apperrors*
    is the list of ``apperror`` payloads emitted to the SSE stream (e.g. when
    an unresolved custom-provider target aborts before constructing/sending).
    """
    session_id = "r5_stream_session"
    stream_id = "r5-stream"
    model = "worker-model"
    msg = "hello from custom provider"

    session, session_dir = _r5_make_session(tmp_path, session_id, model, msg)
    session.active_stream_id = stream_id

    constructions = []

    class _RecordingAgent:
        def __init__(self, **kwargs):
            constructions.append(dict(kwargs))
            self.session_id = kwargs.get("session_id")
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            if mode == "raised":
                # Raise an auth-classified exception so the except-path
                # self-heal (raised-401) branch runs.
                raise RuntimeError("401 AuthenticationError: invalid api key")
            # Return an auth-classified error so the returned-401 self-heal
            # path runs.
            return {
                "messages": [{"role": "assistant", "content": ""}],
                "error": "401 AuthenticationError: invalid api key",
                "status": "error",
            }

        def interrupt(self, _message):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()

    invalidated_ids = []

    with monkeypatch.context() as m:
        # ── Isolate streaming/models module state, restoring originals after.
        # Using monkeypatch.setattr makes pytest restore the prior global object
        # references, so this test never pollutes later tests in a full run
        # (#6516 r5 test hygiene).
        threading_module = __import__("threading")
        m.setattr(_api_models, "SESSION_DIR", session_dir)
        m.setattr(_api_models, "SESSION_INDEX_FILE", session_dir / "_index.json")
        m.setattr(streaming, "SESSION_DIR", session_dir)
        m.setattr(_api_models, "SESSIONS", {session_id: session})
        m.setattr(streaming, "SESSIONS", {session_id: session})
        m.setattr(streaming, "SESSION_AGENT_LOCKS_LOCK", threading_module.Lock())
        m.setattr(config, "SESSION_AGENT_CACHE_LOCK", threading_module.Lock())
        m.setattr(streaming, "STREAMS", {stream_id: queue.Queue()})
        m.setattr(streaming, "AGENT_INSTANCES", {})
        m.setattr(streaming, "SESSION_AGENT_LOCKS", {})
        m.setattr(streaming, "PENDING_GOAL_CONTINUATION", {})
        m.setattr(config, "SESSION_AGENT_CACHE", __import__("collections").OrderedDict())
        event_queue = streaming.STREAMS[stream_id]

        m.setattr(streaming, "AIAgent", _RecordingAgent)
        m.setattr(streaming, "get_session", lambda _sid: session)
        m.setattr(
            streaming, "resolve_model_provider",
            lambda *a, **k: (model, "custom:worker", target_cfg.get("model", {}).get("base_url")),
        )
        # Runtime provider resolution — ambient sentinels that must NOT leak.
        m.setattr(
            "api.oauth.resolve_runtime_provider_with_anthropic_env_lock",
            lambda resolver, *a, **k: {"provider": "custom", "api_key": ambient_key, "base_url": ambient_url, "credential_pool": credential_pool},
        )
        m.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            lambda requested=None, target_model=None: {"provider": "custom", "api_key": ambient_key, "base_url": ambient_url, "credential_pool": credential_pool},
        )
        # Self-heal needs a truthy auth store so the retry agent is rebuilt.
        m.setattr(
            "api.oauth.read_auth_json",
            lambda auth_path=None: {"provider_credentials": {}},
        )
        # Target profile config (env expansion + custom_providers) at send time.
        m.setattr("api.config.get_config_for_profile_home", lambda home: target_cfg)
        m.setattr("api.config.get_config", lambda *a, **k: target_cfg)
        m.setattr("api.config._resolve_cli_toolsets", lambda *a, **k: [])

        def _rec_invalidate(provider_id):
            invalidated_ids.append(provider_id)
        m.setattr(config, "invalidate_credential_pool_cache", _rec_invalidate)
        m.setattr(streaming, "_get_ai_agent", lambda: _RecordingAgent)
        m.setattr(
            streaming, "_close_cached_agent_entry_at_session_boundary",
            lambda *a, **k: None,
        )
        m.setitem(sys.modules, "hermes_state", fake_hermes_state)

        streaming._run_agent_streaming(
            session_id=session_id,
            msg_text=msg,
            model=model,
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    apperrors = []
    while not event_queue.empty():
        _item = event_queue.get_nowait()
        if isinstance(_item, tuple) and _item and _item[0] == 'apperror':
            apperrors.append(_item[1])
    return constructions, invalidated_ids, apperrors


def test_r5_returned_401_retry_keeps_target_url_key(monkeypatch, tmp_path):
    """The returned-401 self-heal (a real _run_agent_streaming run) must rebuild
    the agent with the owning profile's custom:<slug> URL/key on BOTH the initial
    and retry AIAgent constructors — an ambient sentinel must never appear, and
    the self-heal must invalidate the NAMED custom:<slug> identity (#6516 r5)."""
    target_cfg = {
        "model": {"default": "worker-model", "provider": "custom:worker",
                  "base_url": "http://target-profile:9000/v1"},
        "custom_providers": [
            {"name": "worker", "base_url": "http://target-profile:9000/v1",
             "api_key": "target-profile-key"},
        ],
    }
    constructions, invalidated, _app = _r5_drive_streaming_401(
        tmp_path, monkeypatch,
        target_cfg=target_cfg,
        ambient_key="ambient-key", ambient_url="http://ambient:8000/v1",
    )

    assert len(constructions) >= 2, (
        f"expected an initial + a 401-retry AIAgent construction; got {len(constructions)}: {constructions!r}"
    )
    # EVERY construction (initial AND retry) must carry the owning profile's
    # target URL/key — never the ambient sentinel.
    for i, c in enumerate(constructions):
        assert c.get("base_url") == "http://target-profile:9000/v1", (
            f"constructor[{i}] did not use target URL: {c!r}"
        )
        assert c.get("api_key") == "target-profile-key", (
            f"constructor[{i}] did not use target key: {c!r}"
        )
        assert c.get("base_url") != "http://ambient:8000/v1", (
            f"constructor[{i}] leaked ambient URL: {c!r}"
        )
        assert c.get("api_key") != "ambient-key", (
            f"constructor[{i}] leaked ambient key: {c!r}"
        )

    # Self-heal must invalidate the NAMED custom:<slug> lane (fix #3).
    assert "custom:worker" in invalidated, (
        f"self-heal must invalidate the named custom:<slug> identity; got {invalidated!r}"
    )
    assert "custom" not in invalidated, (
        f"self-heal must NOT invalidate the generic collapsed 'custom' lane; got {invalidated!r}"
    )


def test_r5_raised_401_retry_keeps_target_url_key(monkeypatch, tmp_path):
    """The raised-401 (except-path) self-heal must also rebuild the agent with
    the owning profile's custom:<slug> URL/key on BOTH the initial and retry
    AIAgent constructors — an ambient sentinel must never appear, and the
    self-heal must invalidate the NAMED custom:<slug> identity (#6516 r5)."""
    target_cfg = {
        "model": {"default": "worker-model", "provider": "custom:worker",
                  "base_url": "http://target-profile:9000/v1"},
        "custom_providers": [
            {"name": "worker", "base_url": "http://target-profile:9000/v1",
             "api_key": "target-profile-key"},
        ],
    }
    constructions, invalidated, _app = _r5_drive_streaming_401(
        tmp_path, monkeypatch,
        target_cfg=target_cfg,
        ambient_key="ambient-key", ambient_url="http://ambient:8000/v1",
        mode="raised",
    )

    assert len(constructions) >= 2, (
        f"expected an initial + a raised-401 retry AIAgent construction; got {len(constructions)}: {constructions!r}"
    )
    for i, c in enumerate(constructions):
        assert c.get("base_url") == "http://target-profile:9000/v1", (
            f"raised-401 constructor[{i}] did not use target URL: {c!r}"
        )
        assert c.get("api_key") == "target-profile-key", (
            f"raised-401 constructor[{i}] did not use target key: {c!r}"
        )
        assert c.get("base_url") != "http://ambient:8000/v1", (
            f"raised-401 constructor[{i}] leaked ambient URL: {c!r}"
        )
        assert c.get("api_key") != "ambient-key", (
            f"raised-401 constructor[{i}] leaked ambient key: {c!r}"
        )

    # Self-heal must invalidate the NAMED custom:<slug> lane (fix #3).
    assert "custom:worker" in invalidated, (
        f"raised-401 self-heal must invalidate the named custom:<slug> identity; got {invalidated!r}"
    )
    assert "custom" not in invalidated, (
        f"raised-401 self-heal must NOT invalidate the generic collapsed 'custom' lane; got {invalidated!r}"
    )


def test_r5_failed_target_config_fails_closed_no_ambient_leak():
    """When the owning profile's config cannot be established (represented by
    an empty {} from the fail-closed path in the streaming worker), the
    authority is MISSING under the authoritative target-profile scope — the
    resolver must FAIL BEFORE constructing/sending rather than leak a truthy
    ambient URL/key or rewrite to an unrelated endpoint (#6516 r5 + round-7)."""
    from api.streaming import CustomProviderAuthorityError, _resolve_custom_provider_runtime_overrides

    with pytest.raises(CustomProviderAuthorityError):
        _resolve_custom_provider_runtime_overrides(
            "custom:worker",
            "ambient-key",
            "http://ambient:8000/v1",
            config_data={},
        )



# ═══════════════════════════════════════════════════════════════════════════
# ── Round-6 re-gate (#6516): provenance-coupled target authority ───────────
# The reviewer required: when config_data is supplied, clear BOTH incoming
# URL/key first, then install exactly the uniquely selected target URL/key.
# A keyless-but-unique target may keep a None key (the keyless placeholder is
# only applied AFTER a unique endpoint is pinned).  Missing / ambiguous /
# malformed / partial authority must fail closed and must NOT retain an
# ambient URL, key, dummy rewrite, or credential-pool result.  The old
# declaration-only boolean conflated "keyless unique" with "collision/absent".
# ═══════════════════════════════════════════════════════════════════════════


def _r6_keyless_target_cfg():
    """One unique custom provider with a base_url but NO api_key (deliberately
    keyless).  The target is authoritative and keyless."""
    return {
        "model": {"default": "worker-model", "provider": "custom:worker",
                  "base_url": "http://worker:9000/v1"},
        "custom_providers": [
            {"name": "worker", "base_url": "http://worker:9000/v1"},
        ],
    }


def _r6_collision_target_cfg():
    """Two slug-equivalent custom_providers rows with no discriminating URL.
    ``worker:prod`` and ``worker-prod`` both slugify to ``custom:worker-prod``,
    so authority is ambiguous and must fail closed."""
    return {
        "model": {"default": "worker-model", "provider": "custom:worker-prod"},
        "custom_providers": [
            {"name": "worker:prod", "base_url": "http://prod-a:9000/v1", "api_key": "key-a"},
            {"name": "worker-prod", "base_url": "http://prod-b:9000/v1", "api_key": "key-b"},
        ],
    }


# ── Direct unit surface: _resolve_custom_provider_runtime_overrides ───────


def test_r6_keyless_target_clears_truthy_ambient_key():
    """A deliberately keyless target provider must NOT inherit a truthy ambient
    API key.  The target scope clears the incoming key first, then pins the
    unique target URL; only the keyless placeholder may follow (never the
    ambient key)."""
    from api.streaming import _resolve_custom_provider_runtime_overrides

    target_cfg = _r6_keyless_target_cfg()
    provider, key, url = _resolve_custom_provider_runtime_overrides(
        "custom:worker",
        "ambient-key",
        "http://ambient:8000/v1",
        config_data=target_cfg,
    )
    assert url == "http://worker:9000/v1", (
        f"target unique URL must be installed, got {url!r}"
    )
    # The ambient key must NOT survive; a unique endpoint is pinned so the
    # keyless placeholder may be used.
    assert key != "ambient-key", (
        f"ambient key leaked into a keyless target bundle: {key!r}"
    )
    assert key == "dummy-key", (
        f"keyless placeholder expected after unique endpoint pinned, got {key!r}"
    )
    assert provider == "custom", (
        f"expected collapsed 'custom' once a unique endpoint is set, got {provider!r}"
    )


def test_r6_absent_provider_fails_closed_no_ambient_leak():
    """A provider absent from the target config must FAIL BEFORE constructing
    (not silent-fill or rewrite identity) — no endpoint authority, no
    placeholder, and never an ambient URL/key.  Round-7 replaces the old
    fail-closed-with-None behavior with a hard fail before send."""
    from api.streaming import CustomProviderAuthorityError, _resolve_custom_provider_runtime_overrides

    # Two unrelated custom providers disables the sole-provider fallback, so a
    # genuinely absent provider classifies as MISSING and must fail before
    # constructing/sending.
    target_cfg = {"model": {"default": "other-model", "provider": "openai"},
                  "custom_providers": [
                      {"name": "unrelated-a", "base_url": "http://other-a:9000/v1",
                       "api_key": "key-a"},
                      {"name": "unrelated-b", "base_url": "http://other-b:9000/v1",
                       "api_key": "key-b"},
                  ]}
    with pytest.raises(CustomProviderAuthorityError):
        _resolve_custom_provider_runtime_overrides(
            "custom:ghost",
            "ambient-key",
            "http://ambient:8000/v1",
            config_data=target_cfg,
        )


def test_r6_slug_collision_clears_truthy_ambient_keyurl():
    """An unresolved lossy-slug collision must FAIL BEFORE constructing/sending
    (round-7) — a truthy ambient URL and key must never be substituted for an
    ambiguous custom target."""
    from api.streaming import CustomProviderAuthorityError, _resolve_custom_provider_runtime_overrides

    target_cfg = _r6_collision_target_cfg()
    with pytest.raises(CustomProviderAuthorityError):
        _resolve_custom_provider_runtime_overrides(
            "custom:worker-prod",
            "ambient-key",
            "http://ambient:8000/v1",
            config_data=target_cfg,
        )


def test_r6_authority_state_classification():
    """The provenance-rich selection result distinguishes selected /
    missing / ambiguous — never from a truthy URL alone (round-7)."""
    from api.config import _resolve_custom_provider_selection

    keyless_cfg = _r6_keyless_target_cfg()
    collision_cfg = _r6_collision_target_cfg()

    # Selected: one unique target row pinned, with the matching source.
    status, key, url, source = _resolve_custom_provider_selection(
        "custom:worker", config_data=keyless_cfg,
    )
    assert status == "selected", f"expected selected, got {status!r}"
    assert url == "http://worker:9000/v1", f"unexpected url {url!r}"
    assert key is None, f"keyless target should carry no key, got {key!r}"
    assert source == "custom_providers", f"unexpected source {source!r}"

    # Ambiguous: two slug-equivalent rows, no unique discriminator.
    status, key, url, source = _resolve_custom_provider_selection(
        "custom:worker-prod", config_data=collision_cfg,
    )
    assert status == "ambiguous", f"expected ambiguous, got {status!r}"
    assert key is None and url is None and source is None, (
        f"ambiguous must carry no bundle, got {key!r}/{url!r}/{source!r}"
    )

    # Missing: not declared, and a sole non-matching row must NOT be selected.
    missing_cfg = {"custom_providers": [
        {"name": "unrelated", "base_url": "http://u:9000/v1", "api_key": "k"},
    ]}
    status, key, url, source = _resolve_custom_provider_selection(
        "custom:ghost", config_data=missing_cfg,
    )
    assert status == "missing", f"expected missing, got {status!r}"
    assert key is None and url is None, f"missing must not select, got {key!r}/{url!r}"


# ── Production-composed: initial + 401 self-heal retry paths ──────────────


def test_r6_keyless_target_initial_and_returned_401_no_ambient_leak(monkeypatch, tmp_path):
    """End-to-end initial send + returned-401 retry for a KEYLESS target: the
    ambient key sentinel must never reach ANY AIAgent constructor; the keyless
    placeholder (applied only after the unique endpoint is pinned) is what the
    constructors see."""
    constructions, _inv, _app = _r5_drive_streaming_401(
        tmp_path, monkeypatch,
        target_cfg=_r6_keyless_target_cfg(),
        ambient_key="ambient-key", ambient_url="http://ambient:8000/v1",
        mode="returned",
    )
    assert len(constructions) >= 2, (
        f"expected initial + retry constructions, got {len(constructions)}: {constructions!r}"
    )
    for i, c in enumerate(constructions):
        assert c.get("api_key") != "ambient-key", (
            f"constructor[{i}] leaked ambient key into keyless target: {c!r}"
        )
        assert c.get("base_url") == "http://worker:9000/v1", (
            f"constructor[{i}] did not use the unique target URL: {c!r}"
        )
        # A unique endpoint is pinned and the target is keyless -> placeholder.
        assert c.get("api_key") == "dummy-key", (
            f"constructor[{i}] expected keyless placeholder, got {c.get('api_key')!r}"
        )


def test_r6_keyless_target_raised_401_no_ambient_leak(monkeypatch, tmp_path):
    """Same as above but through the raised-401 (except-path) self-heal."""
    constructions, _inv, _app = _r5_drive_streaming_401(
        tmp_path, monkeypatch,
        target_cfg=_r6_keyless_target_cfg(),
        ambient_key="ambient-key", ambient_url="http://ambient:8000/v1",
        mode="raised",
    )
    assert len(constructions) >= 2, (
        f"expected initial + retry constructions, got {len(constructions)}: {constructions!r}"
    )
    for i, c in enumerate(constructions):
        assert c.get("api_key") != "ambient-key", (
            f"raised-401 constructor[{i}] leaked ambient key: {c!r}"
        )
        assert c.get("base_url") == "http://worker:9000/v1", (
            f"raised-401 constructor[{i}] did not use target URL: {c!r}"
        )
        assert c.get("api_key") == "dummy-key", (
            f"raised-401 constructor[{i}] expected keyless placeholder, got {c.get('api_key')!r}"
        )


def test_r6_keyless_target_returned_401_invalidates_named_lane(monkeypatch, tmp_path):
    """Self-heal on a keyless target still invalidates the NAMED custom:<slug>
    lane (not the generic 'custom' collapse)."""
    constructions, invalidated, _app = _r5_drive_streaming_401(
        tmp_path, monkeypatch,
        target_cfg=_r6_keyless_target_cfg(),
        ambient_key="ambient-key", ambient_url="http://ambient:8000/v1",
        mode="returned",
    )
    assert len(constructions) >= 2
    assert "custom:worker" in invalidated, (
        f"must invalidate the named custom:worker lane; got {invalidated!r}"
    )
    assert "custom" not in invalidated, (
        f"must NOT invalidate the generic 'custom' collapse; got {invalidated!r}"
    )


def _r6_collision_on_worker_target_cfg():
    """Two slug-equivalent rows that BOTH slugify to ``custom:worker``
    (the provider the composed harness hardcodes): ``worker`` and ``worker!``.
    No discriminating URL -> ambiguous authority must fail closed."""
    return {
        "model": {"default": "worker-model", "provider": "custom:worker"},
        "custom_providers": [
            {"name": "worker", "base_url": "http://prod-a:9000/v1", "api_key": "key-a"},
            {"name": "worker!", "base_url": "http://prod-b:9000/v1", "api_key": "key-b"},
        ],
    }


def test_r6_collision_initial_and_returned_401_no_ambient_leak(monkeypatch, tmp_path):
    """End-to-end for an unresolved slug collision: authority is ambiguous so
    the worker must FAIL BEFORE constructing or sending — zero AIAgent
    constructors, no ambient URL/key/credential-pool leak, and a user-visible
    apperror emitted to the SSE stream (#6516 round-7)."""
    target_cfg = _r6_collision_on_worker_target_cfg()
    constructions, _inv, apperrors = _r5_drive_streaming_401(
        tmp_path, monkeypatch,
        target_cfg=target_cfg,
        ambient_key="ambient-key", ambient_url="http://ambient:8000/v1",
        mode="returned",
    )
    assert len(constructions) == 0, (
        f"ambiguous collision must not construct any agent; got {constructions!r}"
    )
    assert apperrors, (
        f"expected an apperror to be emitted on abort; got {apperrors!r}"
    )
    assert any(
        _msg_mentions_authority(p) for p in apperrors
    ), f"apperror should mention the unresolved provider, got {apperrors!r}"


# ═══════════════════════════════════════════════════════════════════════════
# ── Round-7 re-gate (#6516): fail-before-construct + credential-pool scrub ─
# The reviewer required (review 4837781398):
#   1. Resolver returns EXPLICIT selection provenance (never reconstruct
#      "selected" from a truthy URL).  A missing slug must NOT select a sole
#      non-matching custom_providers row, and model.base_url must be gated by a
#      matching canonical model.provider.
#   2. The ambient credential_pool must not ride into a keyless/missing/
#      ambiguous/partial custom target; the complete target-incompatible
#      runtime bundle is scrubbed before EVERY constructor, and on
#      missing/ambiguous/malformed/partial authority the worker fails BEFORE
#      constructing or sending.
# ═══════════════════════════════════════════════════════════════════════════


def test_r7_missing_slug_sole_nonmatching_row_fails():
    """A missing requested slug must NOT be rewritten to a sole NON-matching
    custom_providers row (the old identity-blind fallback).  Round-7 requires
    this to fail before constructing, not pivot to an unrelated endpoint."""
    from api.streaming import CustomProviderAuthorityError, _resolve_custom_provider_runtime_overrides

    cfg = {"custom_providers": [
        {"name": "unrelated", "base_url": "http://unrelated:9000/v1", "api_key": "key"},
    ]}
    with pytest.raises(CustomProviderAuthorityError):
        _resolve_custom_provider_runtime_overrides(
            "custom:ghost", "ambient-key", "http://ambient:8000/v1",
            config_data=cfg,
        )


def test_r7_sole_matching_row_still_selected():
    """The sole-provider behavior that round-5 restored must STILL work when the
    sole row's name genuinely matches the requested slug (the fallback was only
    removed for the NON-matching case)."""
    from api.streaming import _resolve_custom_provider_runtime_overrides

    cfg = {"custom_providers": [
        {"name": "worker", "base_url": "http://worker:9000/v1", "api_key": "real-key"},
    ]}
    provider, key, url = _resolve_custom_provider_runtime_overrides(
        "custom:worker", "ambient-key", "http://ambient:8000/v1",
        config_data=cfg,
    )
    assert url == "http://worker:9000/v1", f"expected matched sole row URL, got {url!r}"
    assert key == "real-key", f"expected matched sole row key, got {key!r}"
    assert provider == "custom", f"expected collapsed custom, got {provider!r}"


def test_r7_model_base_url_gated_by_matching_provider():
    """model.base_url must be identity-gated: an unrelated model.provider must
    NOT be able to supply the endpoint for a different custom:<slug> target
    (round-7 Gap 1)."""
    from api.streaming import CustomProviderAuthorityError, _resolve_custom_provider_runtime_overrides

    unrelated = {"model": {"provider": "openai", "base_url": "http://ambient-model:7000/v1"},
                 "custom_providers": []}
    with pytest.raises(CustomProviderAuthorityError):
        _resolve_custom_provider_runtime_overrides(
            "custom:ghost", "ambient-key", "http://ambient:8000/v1",
            config_data=unrelated,
        )

    # Matching model.provider supplies the endpoint via the model fallback.
    matching = {"model": {"provider": "custom:worker", "base_url": "http://worker:9000/v1"},
                "custom_providers": []}
    provider, key, url = _resolve_custom_provider_runtime_overrides(
        "custom:worker", "ambient-key", "http://ambient:8000/v1",
        config_data=matching,
    )
    assert url == "http://worker:9000/v1", f"expected gated model URL, got {url!r}"
    assert provider == "custom", f"expected collapsed custom, got {provider!r}"


def test_r7_keyless_target_scrubs_ambient_credential_pool_returned(monkeypatch, tmp_path):
    """A keyless custom target (unique endpoint, no key) must NOT carry the
    ambient credential_pool into ANY AIAgent constructor on the initial send
    or the returned-401 retry (round-7 Gap 2).  The primary api_key carries
    the keyless placeholder; the ambient pool is scrubbed."""
    ambient_pool = {"openai": [{"api_key": "ambient-pool-secret"}]}
    constructions, _inv, _app = _r5_drive_streaming_401(
        tmp_path, monkeypatch,
        target_cfg=_r6_keyless_target_cfg(),
        ambient_key="ambient-key", ambient_url="http://ambient:8000/v1",
        mode="returned", credential_pool=ambient_pool,
    )
    assert len(constructions) >= 2, (
        f"expected initial + retry constructions, got {len(constructions)}"
    )
    for i, c in enumerate(constructions):
        assert c.get("credential_pool") is None, (
            f"constructor[{i}] leaked ambient credential_pool into keyless target: {c.get('credential_pool')!r}"
        )
        assert c.get("api_key") == "dummy-key", (
            f"constructor[{i}] expected keyless placeholder, got {c.get('api_key')!r}"
        )


def test_r7_keyless_target_scrubs_ambient_credential_pool_raised(monkeypatch, tmp_path):
    """Same credential-pool scrub through the raised-401 self-heal path."""
    ambient_pool = {"openai": [{"api_key": "ambient-pool-secret"}]}
    constructions, _inv, _app = _r5_drive_streaming_401(
        tmp_path, monkeypatch,
        target_cfg=_r6_keyless_target_cfg(),
        ambient_key="ambient-key", ambient_url="http://ambient:8000/v1",
        mode="raised", credential_pool=ambient_pool,
    )
    assert len(constructions) >= 2
    for i, c in enumerate(constructions):
        assert c.get("credential_pool") is None, (
            f"raised-401 constructor[{i}] leaked ambient credential_pool: {c.get('credential_pool')!r}"
        )


def test_r7_collision_aborts_no_constructor_no_pool(monkeypatch, tmp_path):
    """An unresolved slug collision under an authoritative target scope aborts
    BEFORE constructing/sending — no agent, no ambient URL/key, no
    credential_pool, and a user-visible apperror (round-7 Gap 2)."""
    ambient_pool = {"openai": [{"api_key": "ambient-pool-secret"}]}
    constructions, _inv, apperrors = _r5_drive_streaming_401(
        tmp_path, monkeypatch,
        target_cfg=_r6_collision_on_worker_target_cfg(),
        ambient_key="ambient-key", ambient_url="http://ambient:8000/v1",
        mode="returned", credential_pool=ambient_pool,
    )
    assert len(constructions) == 0, (
        f"ambiguous collision must construct no agent; got {constructions!r}"
    )
    assert apperrors, f"expected apperror on abort, got {apperrors!r}"


def test_r7_collision_aborts_no_constructor_no_pool_raised(monkeypatch, tmp_path):
    """The SAME fail-before-construct + pool-scrub guarantee through the
    raised-401 (except-path) self-heal — the reviewer required regression
    coverage for BOTH collision retry shapes (#6516 round-7)."""
    ambient_pool = {"openai": [{"api_key": "ambient-pool-secret"}]}
    constructions, _inv, apperrors = _r5_drive_streaming_401(
        tmp_path, monkeypatch,
        target_cfg=_r6_collision_on_worker_target_cfg(),
        ambient_key="ambient-key", ambient_url="http://ambient:8000/v1",
        mode="raised", credential_pool=ambient_pool,
    )
    assert len(constructions) == 0, (
        f"raised-401 ambiguous collision must construct no agent; got {constructions!r}"
    )
    assert apperrors, f"expected apperror on raised-401 abort, got {apperrors!r}"


def test_r7_malformed_sole_entry_fails_without_endpoint():
    """A custom_providers row whose name matches but carries NO base_url is a
    partial/malformed target — authority cannot be pinned, so it must fail
    (never send a keyless placeholder to an unknown endpoint) (#6516 round-7)."""
    from api.streaming import CustomProviderAuthorityError, _resolve_custom_provider_runtime_overrides

    cfg = {"custom_providers": [
        {"name": "worker", "api_key": "k"},  # no base_url -> partial/malformed
    ]}
    with pytest.raises(CustomProviderAuthorityError):
        _resolve_custom_provider_runtime_overrides(
            "custom:worker", "ambient-key", "http://ambient:8000/v1",
            config_data=cfg,
        )


def _assert_scrub_invariant(constructions):
    """Shared round-7 invariant: NO AIAgent constructor may receive the ambient
    credential_pool for a custom target, and a truthy ambient URL/key must never
    survive."""
    assert constructions, "expected at least one construction"
    for i, c in enumerate(constructions):
        assert c.get("credential_pool") is None, (
            f"constructor[{i}] leaked ambient credential_pool: {c.get('credential_pool')!r}"
        )
        assert c.get("base_url") != "http://ambient:8000/v1", (
            f"constructor[{i}] leaked ambient URL: {c.get('base_url')!r}"
        )
        assert c.get("api_key") != "ambient-key", (
            f"constructor[{i}] leaked ambient key: {c.get('api_key')!r}"
        )


def test_r7_matching_target_still_scrubs_ambient_pool(monkeypatch, tmp_path):
    """Even a fully-matching custom target (unique endpoint with a real key)
    must NOT carry the ambient credential_pool into its constructors — the pool
    is ambient-profile-owned and never belongs on a custom:<slug> agent
    (#6516 round-7 Gap 2)."""
    ambient_pool = {"openai": [{"api_key": "ambient-pool-secret"}]}
    target_cfg = {"model": {"default": "worker-model", "provider": "custom:worker"},
                  "custom_providers": [
                      {"name": "worker", "base_url": "http://worker:9000/v1",
                       "api_key": "target-key"},
                  ]}
    constructions, _inv, _app = _r5_drive_streaming_401(
        tmp_path, monkeypatch,
        target_cfg=target_cfg,
        ambient_key="ambient-key", ambient_url="http://ambient:8000/v1",
        mode="returned", credential_pool=ambient_pool,
    )
    _assert_scrub_invariant(constructions)

