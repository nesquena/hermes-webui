"""Profile switch must not discard the per-profile models disk cache.

``POST /api/profile/switch`` calls ``invalidate_models_cache()`` so the next
``/api/models`` re-resolves the new profile's catalog (#1200). That helper also
deleted the on-disk ``models_cache.<profile>.json`` — a test-isolation
behaviour — which forced a full cold rebuild (live provider ``fetch_models``
HTTPS calls, ~490k deepcopy calls) on every switch even though no source had
changed. The disk cache is already keyed per profile and rejected on read when
``_models_cache_source_fingerprint()`` differs, so deleting it buys no
correctness on the switch path and only costs seconds of latency.

The switch route now uses ``invalidate_models_cache(delete_disk=False)``.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse


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


def _prime_memory_cache(monkeypatch, catalog: dict):
    import api.config as cfg

    monkeypatch.setattr(cfg, "_available_models_cache", catalog, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 1.0, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_source_fingerprint", {"p": "x"}, raising=False)
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)


def test_invalidate_keeps_disk_cache_when_delete_disk_false(tmp_path, monkeypatch):
    import api.config as cfg

    cache_path = tmp_path / "models_cache.profile.json"
    cache_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    _prime_memory_cache(monkeypatch, _catalog("old-profile-model"))

    cfg.invalidate_models_cache(delete_disk=False)

    # In-memory snapshot dropped so the next request re-resolves the profile...
    assert cfg._available_models_cache is None
    assert cfg._available_models_cache_source_fingerprint is None
    assert cfg._models_cache_provenance is None
    # ...but the fingerprint-guarded disk cache survives.
    assert cache_path.exists(), "delete_disk=False must not unlink the per-profile disk cache"


def test_invalidate_default_still_deletes_disk_cache(tmp_path, monkeypatch):
    """Test-isolation contract is unchanged for every existing caller."""
    import api.config as cfg

    cache_path = tmp_path / "models_cache.profile.json"
    cache_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    _prime_memory_cache(monkeypatch, _catalog("old-profile-model"))

    cfg.invalidate_models_cache()

    assert cfg._available_models_cache is None
    assert not cache_path.exists()


def test_memory_only_invalidate_serves_next_request_from_disk_without_rebuild(tmp_path, monkeypatch):
    """After a memory-only drop, get_available_models() must reload the disk
    snapshot instead of running the live rebuild."""
    import api.config as cfg

    cache_path = tmp_path / "models_cache.profile.json"
    cache_path.write_text("{}", encoding="utf-8")
    disk_catalog = _catalog("disk-model")
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: disk_catalog if cache_path.exists() else None)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})
    # Pin config mtime tracking so an unrelated config change (or leaked
    # state from an earlier test) cannot force the reload branch.
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("model: {}\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "_get_config_path", lambda: cfg_file)
    monkeypatch.setattr(cfg, "_cfg_path", cfg_file, raising=False)
    monkeypatch.setattr(cfg, "_cfg_mtime", cfg_file.stat().st_mtime, raising=False)

    def _unexpected_rebuild(*_a, **_kw):
        raise AssertionError("profile switch must not trigger a live models rebuild")

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _unexpected_rebuild)
    _prime_memory_cache(monkeypatch, _catalog("stale-memory-model"))

    cfg.invalidate_models_cache(delete_disk=False)
    result = cfg.get_available_models()

    assert result["default_model"] == "disk-model"


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self
        self.headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8"))

    def get_json(self):
        return json.loads(self.body.decode("utf-8"))


def _drive_profile_switch(monkeypatch, name: str = "demo") -> _FakeHandler:
    """Execute POST /api/profile/switch through routes.handle_post with the
    auth / CSRF / profile-fs / watcher collaborators stubbed out, leaving
    ``config.invalidate_models_cache`` to whatever the caller installed."""
    from api import gateway_watcher, profiles, routes

    for mod in ("api.auth", "api.helpers", "api.routes"):
        monkeypatch.setattr(f"{mod}.is_auth_enabled", lambda: False, raising=False)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {"name": name})
    monkeypatch.setattr(profiles, "_validate_profile_name", lambda _name: None)
    monkeypatch.setattr(profiles, "switch_profile", lambda _name, process_wide=False: {"ok": True, "name": _name})
    monkeypatch.setattr(gateway_watcher, "restart_watcher_for_profile", lambda _name: None)

    handler = _FakeHandler()
    routes.handle_post(handler, urlparse("/api/profile/switch"))
    return handler


def test_profile_switch_route_calls_invalidate_with_delete_disk_false(monkeypatch):
    """Executed-route guard: the live switch handler must pass
    ``delete_disk=False`` (not rely on the default, which unlinks the disk
    cache and forces a cold rebuild)."""
    from api import config

    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(config, "invalidate_models_cache", lambda *a, **kw: calls.append((a, kw)))

    handler = _drive_profile_switch(monkeypatch)

    assert handler.status == 200, handler.body.decode("utf-8", "replace")
    assert handler.get_json() == {"ok": True, "name": "demo"}
    assert calls == [((), {"delete_disk": False})], (
        "/api/profile/switch must call invalidate_models_cache(delete_disk=False) exactly once; "
        f"got {calls!r}"
    )


def test_profile_switch_route_leaves_disk_cache_file_in_place(tmp_path, monkeypatch):
    """End-to-end through the real ``invalidate_models_cache``: after the
    executed switch route, the in-memory catalog is dropped but the
    per-profile disk snapshot still exists."""
    from api import config as cfg

    cache_path = tmp_path / "models_cache.profile.json"
    cache_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    _prime_memory_cache(monkeypatch, _catalog("old-profile-model"))

    handler = _drive_profile_switch(monkeypatch)

    assert handler.status == 200, handler.body.decode("utf-8", "replace")
    assert cfg._available_models_cache is None, "switch must still drop the in-memory catalog (#1200)"
    assert cache_path.exists(), "switch must not unlink the per-profile disk cache"
