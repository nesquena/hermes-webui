"""Followers must not sit 60s behind an in-flight models-catalog rebuild.

The browser aborts ``api()`` at 30s. A 60s server-side follower wait makes
``POST /api/session/new`` look hung ("New conversation") even though the server
would eventually return 200. ``prefer_cache`` callers must never wait, and a
follower whose wait runs out serves the best catalog it already knows instead of
re-entering the cold path.

A **plain** follower deliberately keeps the full wait for the slot owner:
``tests/test_issue7481_custom_probe_budget_fairness.py`` pins that a third caller
waits for the rebuild that owns the single-flight slot and is served *that*
catalog. Shortening the plain-follower wait is a separate product decision and
does not travel in this change.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent


def _catalog(label: str) -> dict:
    return {
        "active_provider": "openrouter",
        "default_model": label,
        "configured_model_badges": {},
        "groups": [
            {
                "provider": "OpenRouter",
                "provider_id": "openrouter",
                "models": [{"id": label, "label": label}],
            }
        ],
        "aliases": {},
    }


def _pin_config(monkeypatch, cfg, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: {}\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(cfg, "_cfg_path", config_path, raising=False)
    monkeypatch.setattr(cfg, "_cfg_mtime", config_path.stat().st_mtime, raising=False)


def _patch_catalog_cv_wait(monkeypatch, cfg, fn) -> None:
    """Install ``fn`` as the catalog condition's ``wait_for``.

    Patch the class, not the instance. ``monkeypatch`` undoes an instance
    assignment by writing the previous bound method back onto the instance,
    and that shadow hides every later ``threading.Condition.wait_for`` patch
    for the rest of the process. The force-refresh follower tests then call
    the real wait (a short budget looks like "never waited"; budget <= 0
    blocks with no timeout).
    """
    import threading

    original = threading.Condition.wait_for

    def _wait_for(self, predicate, timeout=None):
        if self is not cfg._cache_build_cv:
            return original(self, predicate, timeout)
        return fn(predicate, timeout=timeout)

    monkeypatch.setattr(threading.Condition, "wait_for", _wait_for)


def _seed_resident_cache(monkeypatch, cfg, catalog: dict) -> None:
    monkeypatch.setattr(cfg, "_available_models_cache", catalog, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_available_models_live_rebuild_ts", 0.0, raising=False)
    monkeypatch.setattr(
        cfg,
        "_available_models_cache_source_fingerprint",
        cfg._models_cache_source_fingerprint(),
        raising=False,
    )
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)


def test_prefer_cache_does_not_wait_on_inflight_rebuild(tmp_path, monkeypatch):
    import api.config as cfg

    catalog = _catalog("resident-model")
    _pin_config(monkeypatch, cfg, tmp_path)
    _seed_resident_cache(monkeypatch, cfg, catalog)

    def _must_not_wait(*_args, **_kwargs):
        raise AssertionError("prefer_cache must not wait on an in-flight rebuild")

    _patch_catalog_cv_wait(monkeypatch, cfg, _must_not_wait)

    started = time.monotonic()
    result = cfg.get_available_models(prefer_cache=True)
    assert time.monotonic() - started < 1.0
    assert result["default_model"] == "resident-model"
    # Serving a follower must not clear the rebuild flag for the real builder.
    assert cfg._cache_build_in_progress is True


def test_plain_follower_still_waits_for_the_slot_owner(tmp_path, monkeypatch):
    """A plain follower keeps waiting for the rebuild that owns the slot.

    Deliberately *not* bounded by the live-rebuild budget: upstream pins the
    waiting semantics in ``test_issue7481_custom_probe_budget_fairness.py``
    (a third caller waits for B and is served B's catalog, not a cached one).
    """
    import api.config as cfg

    catalog = _catalog("resident-model")
    _pin_config(monkeypatch, cfg, tmp_path)
    _seed_resident_cache(monkeypatch, cfg, catalog)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)
    timeouts: list[float | None] = []

    def _wait(_predicate, timeout=None):
        timeouts.append(timeout)
        return False

    _patch_catalog_cv_wait(monkeypatch, cfg, _wait)

    result = cfg.get_available_models()

    assert len(timeouts) == 1
    assert timeouts[0] is not None and timeouts[0] >= 1.0, (
        "a plain follower must wait for the slot owner; shortening that wait "
        "is a product decision upstream pins against (test_issue7481)"
    )
    assert result["default_model"] == "resident-model"
    assert cfg._cache_build_in_progress is True


def test_timed_out_follower_serves_the_resident_catalog_not_the_cold_path(
    tmp_path, monkeypatch
):
    """When the follower wait runs out mid-rebuild, it serves what it knows.

    It must not fall through into the cold path (live probes, a second build).
    """
    import api.config as cfg

    catalog = _catalog("resident-model")
    _pin_config(monkeypatch, cfg, tmp_path)
    _seed_resident_cache(monkeypatch, cfg, catalog)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)

    def _wait(_predicate, timeout=None):
        return False

    _patch_catalog_cv_wait(monkeypatch, cfg, _wait)

    def _must_not_rebuild(*_args, **_kwargs):
        raise AssertionError("a timed-out follower must not re-enter the cold path")

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _must_not_rebuild)

    result = cfg.get_available_models()
    assert result["default_model"] == "resident-model"
    assert cfg._cache_build_in_progress is True


def test_session_new_uses_cached_catalog():
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    start = src.find('if parsed.path == "/api/session/new":')
    assert start != -1
    end = src.find('if parsed.path == "/api/session/compression-recovery/start":', start)
    body = src[start:end]
    assert "prefer_cached_catalog=True" in body


def test_session_model_state_forwards_prefer_cached_catalog(monkeypatch):
    """The new-session flag must reach the resolver, not stop at the helper."""
    import api.routes as routes

    seen = {}

    def _resolve(model, provider, prefer_cached_catalog=False):
        seen["prefer_cached_catalog"] = prefer_cached_catalog
        return model, provider, False

    monkeypatch.setattr(routes, "_resolve_compatible_session_model_state", _resolve)
    model, provider = routes._session_model_state_from_request(
        "cached-model",
        None,
        prefer_cached_catalog=True,
    )
    assert (model, provider) == ("cached-model", None)
    assert seen.get("prefer_cached_catalog") is True


_CHOSEN_MODEL = "@anthropic:claude-sonnet-4.6"
_DEFAULT_MODEL = "gpt-5.4-mini"


def _one_model_trap() -> dict:
    """The emergency catalog: only the default model, so membership swaps."""
    return {
        "active_provider": "openai",
        "default_model": _DEFAULT_MODEL,
        "configured_model_badges": {},
        "groups": [
            {
                "provider": "OpenAI",
                "provider_id": "openai",
                "models": [{"id": _DEFAULT_MODEL, "label": "GPT-5.4 Mini"}],
            }
        ],
        "aliases": {},
    }


def _model_ids(catalog: dict) -> set[str]:
    found: set[str] = set()
    for group in catalog.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for bucket in ("models", "extra_models"):
            for entry in group.get(bucket) or []:
                if isinstance(entry, dict) and entry.get("id"):
                    found.add(str(entry["id"]))
    return found


def _arm_cold_fingerprint_miss(monkeypatch, tmp_path: Path):
    """Resident, disk, and stale all fail the source fingerprint.

    The planted snapshots contain only the default model. Serving any of
    them would make New Chat store that default. Anthropic is configured
    by env key only, not under ``providers:``, so the ``@anthropic:`` pick
    cannot take the providers-dict fast path and has to be found in the
    catalog.
    """
    import api.config as cfg
    import api.profiles as profiles

    home = tmp_path / "hermes-home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        "model:\n  provider: openai\n  default: gpt-5.4-mini\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")

    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(cfg, "_cfg_path", config_path, raising=False)
    monkeypatch.setattr(cfg, "_cfg_mtime", config_path.stat().st_mtime, raising=False)
    monkeypatch.setattr(cfg, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(
        cfg, "_models_cache_provenance", cfg._models_cache_provenance, raising=False
    )

    saved_cfg = dict(cfg.cfg)
    cfg.cfg.clear()
    cfg.cfg["model"] = {"provider": "openai", "default": _DEFAULT_MODEL}

    trap = _one_model_trap()
    disk = {
        **trap,
        "_schema_version": cfg._MODELS_CACHE_SCHEMA_VERSION,
        "_source_fingerprint": {"rejected": "fingerprint"},
    }
    runtime_version = cfg._current_webui_version()
    if runtime_version is not None:
        disk["_webui_version"] = runtime_version
    (tmp_path / "models_cache.json").write_text(json.dumps(disk), encoding="utf-8")

    # Fresh TTL, so the resident snapshot is dropped for the fingerprint and
    # not because it is older than the cache window.
    monkeypatch.setattr(cfg, "_available_models_cache", trap, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", time.monotonic(), raising=False)
    monkeypatch.setattr(cfg, "_available_models_live_rebuild_ts", 0.0, raising=False)
    monkeypatch.setattr(
        cfg,
        "_available_models_cache_source_fingerprint",
        {"rejected": "fingerprint"},
        raising=False,
    )
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)

    def _no_live(*_args, **_kwargs):
        raise AssertionError("cold prefer_cache must not run a live provider probe")

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _no_live)
    monkeypatch.setattr(cfg, "_read_live_provider_model_ids", _no_live)

    assert cfg._load_models_cache_from_disk() is None
    assert cfg._load_stale_models_cache_from_disk() is None
    assert (
        cfg._available_models_cache_source_fingerprint
        != cfg._models_cache_source_fingerprint()
    )

    def _restore() -> None:
        cfg.cfg.clear()
        cfg.cfg.update(saved_cfg)

    return cfg, _restore


def test_new_chat_keeps_configured_provider_model_when_catalog_cache_is_cold(
    tmp_path, monkeypatch
):
    """Fingerprint-cold caches must not replace a configured pick with the default.

    Resident, disk, and stale each hold only the default model and fail the
    source fingerprint. New Chat asks for a model of another configured
    provider. The static catalog (no live probes) still lists it, so the
    new session keeps it.
    """
    import api.routes as routes

    cfg, restore = _arm_cold_fingerprint_miss(monkeypatch, tmp_path)
    try:
        def _must_not_wait(*_args, **_kwargs):
            raise AssertionError("prefer_cache must not wait on an in-flight rebuild")

        _patch_catalog_cv_wait(monkeypatch, cfg, _must_not_wait)

        created: list[dict] = []

        class _Sess:
            session_id = "s_cold_catalog"
            messages: list = []
            profile = "default"

            def compact(self):
                return {"session_id": self.session_id}

        def _fake_new_session(**kw):
            created.append(dict(kw))
            return _Sess()

        monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
        monkeypatch.setattr(routes, "_resolve_new_session_workspace", lambda *a, **k: "/tmp/ws")
        monkeypatch.setattr(routes, "new_session", _fake_new_session)
        monkeypatch.setattr(routes, "public_session_projection", lambda row: row)
        monkeypatch.setattr(routes, "read_body", lambda handler: handler._body)
        responses: list[dict] = []
        monkeypatch.setattr(
            routes,
            "j",
            lambda handler, payload, status=200, extra_headers=None, **kw: responses.append(
                {"payload": payload, "status": status}
            )
            or True,
        )
        monkeypatch.setattr(
            routes,
            "bad",
            lambda handler, msg, status=400, **kw: responses.append(
                {"error": msg, "status": status}
            )
            or True,
        )

        handler = SimpleNamespace(
            command="POST",
            _body={
                "model": _CHOSEN_MODEL,
                "worktree": False,
                "workspace": "/tmp/ws",
            },
        )
        routes.handle_post(handler, SimpleNamespace(path="/api/session/new", query=""))

        assert responses and responses[-1].get("status") == 200, responses
        assert created, responses
        assert created[-1]["model"] == _CHOSEN_MODEL
        assert created[-1]["model_provider"] == "anthropic"
        assert cfg._cache_build_in_progress is False
    finally:
        restore()


def test_prefer_cache_inflight_miss_serves_static_catalog_not_one_model(
    tmp_path, monkeypatch
):
    """An in-flight prefer_cache miss still does not wait, and is not one model.

    Option A keeps the no-wait rule. With resident, disk, and stale rejected
    by fingerprint, the follower serves the static catalog without live probes,
    which still contains the configured non-default model.
    """
    cfg, restore = _arm_cold_fingerprint_miss(monkeypatch, tmp_path)
    try:
        monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)

        def _must_not_wait(*_args, **_kwargs):
            raise AssertionError("prefer_cache must not wait on an in-flight rebuild")

        _patch_catalog_cv_wait(monkeypatch, cfg, _must_not_wait)

        result = cfg.get_available_models(prefer_cache=True)
        ids = _model_ids(result)
        assert _CHOSEN_MODEL in ids
        assert _DEFAULT_MODEL in ids
        assert len(ids) > 1
        assert cfg._cache_build_in_progress is True
    finally:
        restore()
