"""Tests for #1633: /api/models disk cache must be invalidated on WebUI version change.

Bug shape: STATE_DIR/models_cache.json was persisted across server restarts
without any version stamp. A Docker container update from version A to B
read the cache file written by version A — users saw stale picker contents
(missing models, phantom provider groups, etc.) for up to 24h until either
(a) the TTL expired, (b) a provider edit triggered invalidate_models_cache,
or (c) they manually deleted the file.

Fix: stamp the disk cache with the current WEBUI_VERSION + a schema version,
and reject loads where either field mismatches. A new release auto-rebuilds
the cache on the very next /api/models call instead of lingering for 24h.
"""

import json
import sys

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the disk cache to a tmp file and reset api.updates between tests."""
    from api import config

    cache_path = tmp_path / "models_cache.json"
    monkeypatch.setattr(config, "_models_cache_path", cache_path)
    yield cache_path


@pytest.fixture
def with_runtime_version():
    """Return a setter that forces a particular runtime WEBUI_VERSION."""
    # api.updates must be loaded for the lazy resolver to find it
    import api.updates as upd

    original = upd.WEBUI_VERSION

    def _set(version: str):
        upd.WEBUI_VERSION = version

    yield _set
    upd.WEBUI_VERSION = original


def _shape_cache():
    """Minimal valid cache shape (no version stamps — those are added on save)."""
    return {
        "active_provider": "anthropic",
        "default_model": "claude-sonnet-4.6",
        "configured_model_badges": {"foo": "bar"},
        "groups": [{"name": "Anthropic", "models": ["claude-sonnet-4.6"]}],
    }


# ── _current_webui_version lazy resolver ──────────────────────────────────


def test_current_webui_version_returns_runtime_version(with_runtime_version):
    """When api.updates is loaded, the lazy resolver returns its WEBUI_VERSION."""
    from api.config import _current_webui_version

    with_runtime_version("v0.50.999-test")
    assert _current_webui_version() == "v0.50.999-test"


def test_current_webui_version_returns_none_when_module_missing(monkeypatch):
    """Early-init path: if api.updates isn't in sys.modules, return None.

    Required so cache reads/writes during very early server boot don't wedge
    the startup sequence on AttributeError.
    """
    monkeypatch.delitem(sys.modules, "api.updates", raising=False)
    from api.config import _current_webui_version

    assert _current_webui_version() is None


# ── Disk cache version stamping ──────────────────────────────────────────


def test_save_stamps_webui_version_on_disk(isolated_cache, with_runtime_version):
    """Saving a cache writes both _webui_version and _schema_version stamps."""
    from api import config

    with_runtime_version("v0.50.293")
    config._save_models_cache_to_disk(_shape_cache())

    on_disk = json.load(open(isolated_cache))
    assert on_disk["_webui_version"] == "v0.50.293"
    assert on_disk["_schema_version"] == config._MODELS_CACHE_SCHEMA_VERSION


def test_save_omits_webui_version_when_runtime_unknown(isolated_cache, monkeypatch):
    """If api.updates isn't loaded (very early boot), save still works but
    skips the version stamp. The next load with a known runtime version will
    treat the file as invalid (fail-safe rebuild on first real call)."""
    monkeypatch.delitem(sys.modules, "api.updates", raising=False)
    from api import config

    config._save_models_cache_to_disk(_shape_cache())
    on_disk = json.load(open(isolated_cache))
    assert "_webui_version" not in on_disk
    # Schema version is always written — it doesn't depend on api.updates
    assert on_disk["_schema_version"] == config._MODELS_CACHE_SCHEMA_VERSION


def test_save_only_writes_known_keys(isolated_cache, with_runtime_version):
    """Defensive — extra junk in the cache dict shouldn't leak to disk."""
    from api import config

    with_runtime_version("v0.50.999")

    cache = _shape_cache()
    cache["secret_credentials"] = "definitely should not be on disk"
    cache["__internal_hint"] = "also nope"
    config._save_models_cache_to_disk(cache)

    on_disk = json.load(open(isolated_cache))
    assert "secret_credentials" not in on_disk
    assert "__internal_hint" not in on_disk


# ── Load: version validation ──────────────────────────────────────────────


def test_load_round_trip_matching_version(isolated_cache, with_runtime_version):
    """Save then load with the same runtime version returns the original shape."""
    from api import config

    with_runtime_version("v0.50.293")
    original = _shape_cache()
    config._save_models_cache_to_disk(original)

    loaded = config._load_models_cache_from_disk()
    assert loaded is not None
    # Shape preserved
    assert loaded["active_provider"] == original["active_provider"]
    assert loaded["default_model"] == original["default_model"]
    assert loaded["configured_model_badges"] == original["configured_model_badges"]
    assert loaded["groups"] == original["groups"]
    # Disk-only metadata stripped before return
    assert "_webui_version" not in loaded
    assert "_schema_version" not in loaded


def test_load_rejects_mismatched_webui_version(isolated_cache, with_runtime_version):
    """The core #1633 fix: a cache stamped v0.50.281 is invalid at runtime v0.50.293."""
    from api import config

    # Save under v0.50.281
    with_runtime_version("v0.50.281")
    config._save_models_cache_to_disk(_shape_cache())

    # Try to load under v0.50.293
    with_runtime_version("v0.50.293")
    loaded = config._load_models_cache_from_disk()
    assert loaded is None, (
        "Cache stamped with a different WebUI version must be rejected so the "
        "next call rebuilds with the current release's picker shape (#1633)"
    )


def test_load_rejects_legacy_cache_without_version_stamp(
    isolated_cache, with_runtime_version
):
    """Pre-#1633 cache files have no _webui_version field at all. They must
    be treated as invalid on the very first load post-update so users get
    a fresh rebuild instead of stale picker contents."""
    from api import config

    # Hand-write a pre-#1633 cache file (no version fields)
    legacy = _shape_cache()
    json.dump(legacy, open(isolated_cache, "w"))

    with_runtime_version("v0.50.293")
    loaded = config._load_models_cache_from_disk()
    assert loaded is None, (
        "Legacy (pre-#1633) cache files must be rejected so the first call "
        "after updating to a release with #1633 rebuilds from live data"
    )


def test_load_rejects_mismatched_schema_version(isolated_cache, with_runtime_version):
    """Schema version mismatch invalidates the cache regardless of WebUI version.
    Forward-compat for future cache-shape changes."""
    from api import config

    # Manually write a cache with a stale schema version but matching webui version
    stale = {
        "_schema_version": 0,  # old
        "_webui_version": "v0.50.293",
        **_shape_cache(),
    }
    json.dump(stale, open(isolated_cache, "w"))

    with_runtime_version("v0.50.293")
    loaded = config._load_models_cache_from_disk()
    assert loaded is None, (
        "Cache with a different schema version must be rejected even when "
        "WebUI version matches"
    )


def test_load_skips_version_check_when_runtime_unknown(isolated_cache, monkeypatch):
    """Early-init: if api.updates isn't loaded, _current_webui_version returns
    None. The version check should NOT run (because we have nothing to compare
    against), but other validity checks still apply.

    This is the fail-safe path that prevents a boot-time wedge if the very
    first /api/models call fires before api.updates is imported.
    """
    from api import config

    # Write a cache that's correct except has no _webui_version
    cache = {
        "_schema_version": config._MODELS_CACHE_SCHEMA_VERSION,
        "_source_fingerprint": config._models_cache_source_fingerprint(),
        # no _webui_version
        **_shape_cache(),
    }
    json.dump(cache, open(isolated_cache, "w"))

    monkeypatch.delitem(sys.modules, "api.updates", raising=False)
    loaded = config._load_models_cache_from_disk()
    # Loadable because runtime version was unknown — once api.updates loads,
    # the next call would re-validate.
    assert loaded is not None


# ── Validity helpers ─────────────────────────────────────────────────────


def test_is_valid_models_cache_remains_shape_only():
    """_is_valid_models_cache must NOT enforce version stamps — keep it loose
    so in-memory cache validations don't fail on missing _webui_version. The
    strict version check lives in _is_loadable_disk_cache only."""
    from api.config import _is_valid_models_cache

    cache = _shape_cache()
    # No _webui_version field
    assert _is_valid_models_cache(cache) is True


def test_is_loadable_disk_cache_checks_versions(with_runtime_version):
    """_is_loadable_disk_cache must check both schema + webui_version stamps."""
    from api import config

    with_runtime_version("v0.50.293")

    # Missing _webui_version
    bad1 = {"_schema_version": config._MODELS_CACHE_SCHEMA_VERSION, **_shape_cache()}
    assert config._is_loadable_disk_cache(bad1) is False

    # Wrong _webui_version
    bad2 = {
        "_schema_version": config._MODELS_CACHE_SCHEMA_VERSION,
        "_webui_version": "v0.50.281",
        **_shape_cache(),
    }
    assert config._is_loadable_disk_cache(bad2) is False

    # Wrong _schema_version
    bad3 = {
        "_schema_version": 0,
        "_webui_version": "v0.50.293",
        **_shape_cache(),
    }
    assert config._is_loadable_disk_cache(bad3) is False

    # Right
    good = {
        "_schema_version": config._MODELS_CACHE_SCHEMA_VERSION,
        "_webui_version": "v0.50.293",
        "_source_fingerprint": config._models_cache_source_fingerprint(),
        **_shape_cache(),
    }
    assert config._is_loadable_disk_cache(good) is True


def test_is_loadable_disk_cache_rejects_non_dict():
    """Non-dict input is invalid even when version checks are skipped."""
    from api.config import _is_loadable_disk_cache

    assert _is_loadable_disk_cache(None) is False
    assert _is_loadable_disk_cache([]) is False
    assert _is_loadable_disk_cache("string") is False
    assert _is_loadable_disk_cache(42) is False


# ── Edge cases ───────────────────────────────────────────────────────────


def test_load_handles_corrupt_json(isolated_cache, with_runtime_version):
    """A corrupt cache file (truncated JSON, non-UTF8 bytes) must return None
    silently, not raise — the cache layer is best-effort."""
    from api import config

    with open(isolated_cache, "wb") as f:
        f.write(b"{not valid json at all")

    with_runtime_version("v0.50.293")
    loaded = config._load_models_cache_from_disk()
    assert loaded is None


def test_load_handles_missing_file(isolated_cache, with_runtime_version):
    """Cache file simply doesn't exist (cold boot) → None, not error."""
    from api import config

    # isolated_cache fixture creates the path but not the file
    assert not isolated_cache.exists()

    with_runtime_version("v0.50.293")
    loaded = config._load_models_cache_from_disk()
    assert loaded is None


def test_save_overwrite_atomic(isolated_cache, with_runtime_version):
    """Saving twice with different versions overwrites cleanly via tmp+rename."""
    from api import config

    with_runtime_version("v0.50.281")
    config._save_models_cache_to_disk(_shape_cache())
    assert json.load(open(isolated_cache))["_webui_version"] == "v0.50.281"

    with_runtime_version("v0.50.293")
    config._save_models_cache_to_disk(_shape_cache())
    assert json.load(open(isolated_cache))["_webui_version"] == "v0.50.293"


def test_save_skips_invalid_shape(isolated_cache, with_runtime_version):
    """Pre-#1633 contract: invalid shape never lands on disk. Preserved."""
    from api import config

    with_runtime_version("v0.50.293")

    # Missing required keys
    config._save_models_cache_to_disk({"active_provider": "anthropic"})
    assert not isolated_cache.exists()


# ── End-to-end: simulate a Docker container update ───────────────────────


def test_docker_update_scenario_invalidates_old_cache(
    isolated_cache, with_runtime_version
):
    """Reproduce Deor's exact scenario from the bug report:

    1. Server v0.50.281 builds a cache and writes it to STATE_DIR.
    2. Container is updated to v0.50.292 (new image, same mounted state volume).
    3. New server boots and tries to load the cache file.
    4. Expected: load returns None, forcing a rebuild that picks up the
       picker fixes shipped between v0.50.281 and v0.50.292.
    """
    from api import config

    # Step 1: v0.50.281 writes cache
    with_runtime_version("v0.50.281")
    old_cache = {
        "active_provider": "nous",
        "default_model": "anthropic/claude-sonnet-4.6",
        "configured_model_badges": {"anthropic/claude-sonnet-4.6": "Anthropic"},
        # The pre-fix Nous group with only 4 models (the v0.50.281 bug)
        "groups": [{"name": "Nous Portal", "models": ["a", "b", "c", "d"]}],
    }
    config._save_models_cache_to_disk(old_cache)
    on_disk = json.load(open(isolated_cache))
    assert on_disk["_webui_version"] == "v0.50.281"
    assert len(on_disk["groups"][0]["models"]) == 4

    # Step 2-3: Container updates to v0.50.292; new server tries to load
    with_runtime_version("v0.50.292")
    loaded = config._load_models_cache_from_disk()

    # Step 4: cache rejected → caller will rebuild from live provider data
    assert loaded is None, (
        "After a WebUI version bump, the disk cache must be rejected so users "
        "see picker fixes immediately instead of waiting up to 24h for the TTL "
        "(#1633: Deor reported v0.50.292 looking identical to v0.50.281 because "
        "the v0.50.281 cache file was being reused unchanged)"
    )


# ── invalidate_models_cache still cleans the disk file ───────────────────


def test_invalidate_models_cache_still_deletes_disk_file(
    isolated_cache, with_runtime_version
):
    """Pre-existing contract preserved: invalidate_models_cache() drops the
    in-memory cache AND deletes the disk file. The version stamping must not
    interfere with this teardown path."""
    from api import config

    with_runtime_version("v0.50.293")
    config._save_models_cache_to_disk(_shape_cache())
    assert isolated_cache.exists()

    config.invalidate_models_cache()
    assert not isolated_cache.exists()
