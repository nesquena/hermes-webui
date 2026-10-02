"""Concurrent custom-endpoint probing + bounded follower waits.

Review follow-up on the custom-endpoint timeout change (30 s per endpoint):
probes run inside the models-cache rebuild, and *follower* callers (the next
``/api/models``, the chat-start model resolution, the Settings picker) wait on
``_cache_build_cv`` for the whole rebuild. With a per-endpoint allowance of
30 s, a couple of dead or hanging custom endpoints could hold the picker past
the frontend's own request timeout.

Two behaviours are pinned here:

1. custom endpoints are probed CONCURRENTLY — both hanging endpoints are
   in flight at the same time, so a dead upstream only costs its own timeout
   instead of delaying every healthy provider behind it in the loop;
2. an ordinary follower returns within the foreground rebuild budget instead of
   blocking on the in-flight rebuild.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error

import pytest

import api.config as cfg
import api.profiles as profiles


@pytest.fixture(autouse=True)
def isolate_models_catalog_state(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: {}\n", encoding="utf-8")
    auth_store_path = tmp_path / "auth.json"
    auth_store_path.write_text("{}", encoding="utf-8")
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text("", encoding="utf-8")

    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(cfg, "_cfg_path", config_path, raising=False)
    monkeypatch.setattr(cfg, "_cfg_mtime", config_path.stat().st_mtime, raising=False)
    monkeypatch.setattr(cfg, "_cfg_has_in_memory_overrides", lambda: True)
    monkeypatch.setattr(cfg, "_get_auth_store_path", lambda: auth_store_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: tmp_path / "models_cache.json")
    monkeypatch.setattr(cfg, "_delete_models_cache_on_disk", lambda: None)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: "unit-test-fingerprint")
    monkeypatch.setattr(cfg, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_source_fingerprint", None, raising=False)
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)
    monkeypatch.setattr(cfg, "cfg", {}, raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(cfg.os, "getenv", lambda key, default=None: default or "")

    return {"auth_store_path": auth_store_path}


def _configure_two_hanging_custom_providers() -> None:
    cfg.cfg = {
        "model": {"provider": "openai-api", "default": "gpt-5.5"},
        "providers": {},
        "fallback_providers": [],
        "custom_providers": [
            {"name": "Hang A", "base_url": "https://hang-a.example/v1", "api_key": "k-a"},
            {"name": "Hang B", "base_url": "https://hang-b.example/v1", "api_key": "k-b"},
        ],
    }


def _install_fake_urlopen(monkeypatch, *, hold: threading.Event | None = None,
                          hold_seconds: float = 5.0, overlap_state: dict | None = None):
    """URL open that hangs on the two custom endpoints and refuses everything else."""

    def fake_urlopen(req, timeout=10):
        url = str(getattr(req, "full_url", req))
        if "hang-a.example" in url or "hang-b.example" in url:
            if overlap_state is not None:
                with overlap_state["lock"]:
                    overlap_state["in_flight"] += 1
                    overlap_state["peak"] = max(overlap_state["peak"], overlap_state["in_flight"])
            try:
                if hold is not None:
                    hold.wait(timeout=hold_seconds)
                else:
                    time.sleep(min(float(timeout or 0.2), 0.2))
            finally:
                if overlap_state is not None:
                    with overlap_state["lock"]:
                        overlap_state["in_flight"] -= 1
            raise urllib.error.URLError("timed out")
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def test_two_hanging_custom_endpoints_are_probed_concurrently(monkeypatch):
    _configure_two_hanging_custom_providers()
    overlap = {"lock": threading.Lock(), "in_flight": 0, "peak": 0}
    _install_fake_urlopen(monkeypatch, overlap_state=overlap)

    started = time.monotonic()
    cfg.get_available_models()
    elapsed = time.monotonic() - started

    # Both endpoints must have been in flight at the same moment: a serial loop
    # would peak at 1 and cost two full probes back to back.
    assert overlap["peak"] == 2, f"probes were not concurrent (peak={overlap['peak']})"
    # And the whole build must not pay each probe serially: the fake holds each
    # call ~0.2 s, so two serial probes would push this past ~0.4 s.
    assert elapsed < 0.35, f"rebuild took {elapsed:.3f}s — probes look serial"


def test_ordinary_follower_returns_within_budget_while_probes_hang(monkeypatch):
    _configure_two_hanging_custom_providers()
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.2, raising=False)

    hold = threading.Event()
    _install_fake_urlopen(monkeypatch, hold=hold, hold_seconds=5.0)

    # Start a rebuild and let it blow past the budget, leaving its worker stuck
    # inside the hanging probes.
    starter = threading.Thread(target=lambda: cfg.get_available_models(force_refresh=True))
    starter.start()
    starter.join(timeout=5.0)
    assert not starter.is_alive(), "budget fallback should have returned the starter"
    assert cfg._cache_build_in_progress is True, "worker should still be probing"

    started = time.monotonic()
    follower = cfg.get_available_models()
    elapsed = time.monotonic() - started

    assert follower.get("groups") is not None
    assert elapsed < 1.5, (
        f"follower blocked {elapsed:.2f}s on the hanging rebuild — it must return "
        "within the foreground rebuild budget"
    )

    hold.set()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and cfg._cache_build_in_progress:
        time.sleep(0.02)
    assert cfg._cache_build_in_progress is False


def test_follower_returns_validated_disk_catalog_while_probes_hang(monkeypatch):
    _configure_two_hanging_custom_providers()
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.2, raising=False)

    disk_catalog = {
        "active_provider": "openai-api",
        "default_model": "gpt-5.5",
        "configured_model_badges": {},
        "groups": [
            {
                "provider": "OpenAI",
                "provider_id": "openai-api",
                "models": [{"id": "gpt-5.5", "label": "GPT-5.5"}],
            }
        ],
        "aliases": {},
    }
    # A fingerprint-validated disk catalog exists (the ordinary warm-install
    # case): the follower must be served it inside the budget instead of
    # blocking on the hanging rebuild.
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: disk_catalog)

    hold = threading.Event()
    _install_fake_urlopen(monkeypatch, hold=hold, hold_seconds=5.0)

    starter = threading.Thread(target=lambda: cfg.get_available_models(force_refresh=True))
    starter.start()
    starter.join(timeout=5.0)
    assert not starter.is_alive()

    started = time.monotonic()
    follower = cfg.get_available_models()
    elapsed = time.monotonic() - started

    assert [g["provider_id"] for g in follower["groups"]] == ["openai-api"]
    assert elapsed < 1.0, f"follower blocked {elapsed:.2f}s despite an available disk catalog"

    hold.set()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and cfg._cache_build_in_progress:
        time.sleep(0.02)
