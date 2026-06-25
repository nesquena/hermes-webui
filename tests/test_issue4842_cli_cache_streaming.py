from __future__ import annotations

import threading
import time

import api.models as models
import api.profiles as profiles


def _set_active_streams(monkeypatch, ids):
    monkeypatch.setattr(models, "_active_stream_ids", lambda: set(ids))


def test_cli_cache_key_stays_frozen_during_streaming(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    db_path.write_text("", encoding="utf-8")

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        models,
        "_default_claude_code_projects_dir",
        lambda: tmp_path / "projects",
    )

    fp = {"value": 0}
    monkeypatch.setattr(
        models,
        "_sqlite_file_stat_cache_key",
        lambda _p: ("fp", fp["value"]),
    )

    _set_active_streams(monkeypatch, {"live-1"})
    _, _, _, key_streaming_a = models._resolve_cli_sessions_context(None)
    fp["value"] = 1
    _, _, _, key_streaming_b = models._resolve_cli_sessions_context(None)
    fp["value"] = 2
    _, _, _, key_streaming_c = models._resolve_cli_sessions_context(None)

    assert key_streaming_a == key_streaming_b == key_streaming_c

    _set_active_streams(monkeypatch, set())
    fp["value"] = 10
    _, _, _, key_idle_a = models._resolve_cli_sessions_context(None)
    fp["value"] = 11
    _, _, _, key_idle_b = models._resolve_cli_sessions_context(None)

    assert key_idle_a != key_idle_b


def test_get_cli_sessions_follower_reuses_stale_rows_during_slow_rebuild(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    models.clear_cli_sessions_cache()
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)
    (_, _, _, cache_key) = models._resolve_cli_sessions_context(None)
    cache_stamp = models._cli_sessions_cache_invalidation_stamp()
    with models._CLI_SESSIONS_CACHE_LOCK:
        models._CLI_SESSIONS_CACHE[cache_key] = (
            time.monotonic() - 1.0,
            cache_stamp,
            [{"session_id": "stale", "title": "stale-row"}],
        )

    started = threading.Event()
    owner_block = threading.Event()
    results = {}

    def _blocking_loader(*_args, **_kwargs):
        started.set()
        owner_block.wait()
        return [{"session_id": "fresh", "title": "fresh-row"}]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", _blocking_loader)

    def _owner():
        results["owner"] = models.get_cli_sessions()

    def _follower():
        results["follower"] = models.get_cli_sessions()

    owner = threading.Thread(target=_owner, daemon=True)
    follower = threading.Thread(target=_follower, daemon=True)

    owner.start()
    assert started.wait(1.0), "owner did not start"
    follower.start()
    follower.join(1.0)

    assert results.get("follower") == [{"session_id": "stale", "title": "stale-row"}]
    assert not follower.is_alive()
    owner_block.set()
    owner.join(2.0)
    assert not owner.is_alive()

    assert results.get("owner") == [{"session_id": "fresh", "title": "fresh-row"}]


def test_get_cli_sessions_clear_during_rebuild_does_not_restore_stale_rows(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    models.clear_cli_sessions_cache()
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)

    owner_started = threading.Event()
    owner_block = threading.Event()
    results = {}

    def _blocking_loader(*_args, **_kwargs):
        owner_started.set()
        owner_block.wait()
        return [{"session_id": "fresh", "title": "fresh-row"}]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", _blocking_loader)

    _, _, _, cache_key = models._resolve_cli_sessions_context(None)

    owner = threading.Thread(
        target=lambda: results.setdefault("owner", models.get_cli_sessions()),
        daemon=True,
    )
    owner.start()

    assert owner_started.wait(1.0), "owner did not enter rebuild"
    models.clear_cli_sessions_cache()

    owner_block.set()
    owner.join(1.0)

    assert results.get("owner") == [{"session_id": "fresh", "title": "fresh-row"}]
    with models._CLI_SESSIONS_CACHE_LOCK:
        assert cache_key not in models._CLI_SESSIONS_CACHE

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", lambda *_args, **_kwargs: [{"session_id": "recovered", "title": "recovered-row"}])
    recovered = models.get_cli_sessions()
    assert recovered == [{"session_id": "recovered", "title": "recovered-row"}]
    with models._CLI_SESSIONS_CACHE_LOCK:
        assert cache_key in models._CLI_SESSIONS_CACHE
