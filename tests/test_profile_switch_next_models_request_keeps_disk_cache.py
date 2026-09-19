"""Greptile review thread on #7632 (docs/architecture/models-cache-invalidation.md):

A per-client ``POST /api/profile/switch`` (``process_wide=False``) skips
``reload_config()``, so the process-global ``_cfg_mtime`` still holds the
*previous* profile's nonzero mtime. The next ``/api/models`` for the new
profile sees a different config mtime, takes the config-reload branch, and
``_refresh_config_cache()`` used to treat ``_old_cfg_mtime != 0.0`` as "config
edited" and unlink the disk snapshot — the *target* profile's
``models_cache.<name>.json`` — defeating ``delete_disk=False`` on the very next
request.

``_refresh_config_cache()`` now only deletes the disk snapshot when the same
config.yaml path was already loaded (a real edit of the active profile's
config). These tests drive real profile homes, real config files and real disk
cache files through ``switch_profile()`` + ``get_available_models()``.
"""

from __future__ import annotations

import os
from pathlib import Path


def _catalog(label: str) -> dict:
    return {
        "active_provider": "openai",
        "default_model": label,
        "configured_model_badges": {},
        "groups": [
            {
                "provider": "OpenAI",
                "provider_id": "openai",
                "models": [{"id": label, "label": label, "supports_fast_tier": False}],
            }
        ],
        "aliases": {},
    }


def _setup_two_profiles(tmp_path: Path, monkeypatch):
    """Real default + named profile homes, distinct config mtimes, one disk
    cache file per profile. Returns (default_cache, demo_cache, demo_config)."""
    import api.config as cfg
    import api.profiles as profiles

    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    default_home = tmp_path / ".hermes"
    default_home.mkdir()
    (default_home / "config.yaml").write_text("model:\n  default: default-model\n", encoding="utf-8")
    demo_home = default_home / "profiles" / "demo"
    demo_home.mkdir(parents=True)
    demo_config = demo_home / "config.yaml"
    demo_config.write_text("model:\n  default: demo-model\n", encoding="utf-8")
    os.utime(demo_config, (1_000_000, 1_000_000))  # guarantee a different mtime

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", default_home)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles._tls, "profile", None, raising=False)

    default_cache = tmp_path / "models_cache.json"
    demo_cache = tmp_path / "models_cache.demo.json"
    default_cache.write_text("{}", encoding="utf-8")
    demo_cache.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfg, "_models_cache_path", default_cache)

    # Serve the disk snapshot; never allow a live rebuild.
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: _catalog("disk-model"))
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)

    def _unexpected_rebuild(*_a, **_kw):
        raise AssertionError("must not trigger a live models rebuild")

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _unexpected_rebuild)

    # Snapshot/restore the process-global config state touched by reload_config().
    for attr in ("_cfg_mtime", "_cfg_path", "_cfg_fingerprint"):
        monkeypatch.setattr(cfg, attr, getattr(cfg, attr), raising=False)
    saved_cache = dict(cfg._cfg_cache)

    def _restore():
        cfg._cfg_cache.clear()
        cfg._cfg_cache.update(saved_cache)

    monkeypatch.setattr(cfg, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_source_fingerprint", None, raising=False)
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)

    # Load the default profile's config as the server would have at boot.
    cfg.reload_config()
    assert cfg._cfg_path == default_home / "config.yaml"
    assert cfg._cfg_mtime != 0.0

    return default_cache, demo_cache, demo_config, _restore


def test_first_models_request_after_per_client_switch_keeps_target_disk_cache(tmp_path, monkeypatch):
    import api.config as cfg
    import api.profiles as profiles

    default_cache, demo_cache, _demo_config, restore = _setup_two_profiles(tmp_path, monkeypatch)
    try:
        # Warm the default profile: /api/models with the default cookie.
        cfg.get_available_models()
        assert default_cache.exists()

        # Per-client switch, exactly as the route does it.
        profiles.switch_profile("demo", process_wide=False)
        cfg.invalidate_models_cache(delete_disk=False)
        assert demo_cache.exists()
        prev_mtime = cfg._cfg_mtime
        assert prev_mtime != 0.0, "per-client switch leaves the previous profile's mtime in place"

        # First /api/models from that client (cookie → thread-local profile).
        profiles.set_request_profile("demo")
        try:
            result = cfg.get_available_models()
        finally:
            profiles.clear_request_profile()

        assert result["default_model"] == "disk-model"
        assert cfg._cfg_path == _demo_config, "config-reload branch must have re-pointed to the demo config"
        assert cfg._cfg_mtime == _demo_config.stat().st_mtime
        assert demo_cache.exists(), (
            "first /api/models after a per-client profile switch must not unlink the "
            "target profile's models_cache.demo.json"
        )
        assert default_cache.exists()
    finally:
        restore()


def test_same_profile_config_edit_still_deletes_disk_cache(tmp_path, monkeypatch):
    """The path guard must not weaken the real-edit contract: an mtime change
    on the *same* config.yaml still drops the disk snapshot."""
    import api.config as cfg
    import api.profiles as profiles

    default_cache, demo_cache, demo_config, restore = _setup_two_profiles(tmp_path, monkeypatch)
    try:
        profiles.switch_profile("demo", process_wide=False)
        cfg.invalidate_models_cache(delete_disk=False)
        profiles.set_request_profile("demo")
        try:
            cfg.get_available_models()
            assert demo_cache.exists()
            # Edit demo's config.yaml → mtime moves on the same path.
            demo_config.write_text("model:\n  default: demo-model-2\n", encoding="utf-8")
            os.utime(demo_config, (2_000_000, 2_000_000))
            cfg.invalidate_models_cache(delete_disk=False)
            cfg.get_available_models()
        finally:
            profiles.clear_request_profile()
        assert not demo_cache.exists(), "editing the active profile's config.yaml must still delete its disk cache"
        assert default_cache.exists()
    finally:
        restore()
