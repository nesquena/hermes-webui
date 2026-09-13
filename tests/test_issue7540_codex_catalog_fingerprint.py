"""Regression tests for issue #7540 — Codex models_cache.json churn must not
bust a valid /api/models catalog cache and force a live rebuild on session open.

Bug shape
---------
``_models_cache_source_fingerprint()`` covers the small local catalogs the
catalog depends on, including Codex's ``~/.codex/models_cache.json``, and
fingerprinted that file by ``mtime_ns`` + ``size``
(``_models_cache_file_fingerprint``, #2443).  Codex rewrites that file on its
own refresh timer, and each rewrite bumps both stat fields even when the model
payload is byte-identical — only ``fetched_at`` / ``updated_at`` move.  So:

    Codex refresh -> fingerprint differs
    -> ``_get_fresh_memory_models_cache()`` rejects the warm 24h snapshot
    -> the session visit (#4756) then finds ``_is_loadable_disk_cache()``
       rejecting the disk snapshot for the same reason
    -> ``get_available_models(force_refresh=True)`` -> serial live provider
       probes on the session-open path (the multi-second hang in the report).

Fix (maintainer recommendation on #7540: semantic fingerprint)
--------------------------------------------------------------
``_codex_models_cache_fingerprint()`` hashes the file's *content* with the
refresh-timestamp keys deny-listed, mirroring the auth.json fix
(``_auth_store_semantic_fingerprint``, RCA t_16551f61).  Metadata churn is
invisible to the fingerprint, while anything that actually feeds the Codex
model list we merge still changes it.

These tests are INVARIANTS, not change-detectors:

  * churn-immune           — a ``fetched_at``-only rewrite keeps the
                             fingerprint identical, keeps a previously-valid
                             disk cache loadable, and keeps a session visit
                             served from memory (no live rebuild).
  * real-change-invalidates — client_version / etag / models[] / an unknown
                             future field each flip the fingerprint AND reject
                             the previously-valid disk cache, so the fix cannot
                             over-stabilise into serving a stale catalog.
"""

import copy
import json
import os
import time

import pytest

import api.config as config


_TS_1 = "2026-09-13T12:00:00+00:00"
_TS_2 = "2026-09-13T15:45:11+00:00"


class _LiveRebuildReached(BaseException):
    """Sentinel raised if a session visit escalates to a live provider rebuild.

    Deliberately NOT an ``Exception``: the session-visit path catches broad
    ``Exception`` around its ``force_refresh`` call and falls back to a stale
    on-disk snapshot, which would mask the regression.
    """


def _codex_cache(fetched_at=_TS_1, *, client_version="0.51.0",
                 etag='"codex-etag-1"', models=None, extra_top=None):
    """Build a realistic ~/.codex/models_cache.json payload."""
    if models is None:
        models = [
            {"slug": "gpt-5.3-codex", "visibility": "list", "priority": 1},
            {"slug": "gpt-5.1-codex-mini", "visibility": "list", "priority": 2},
        ]
    payload = {
        "fetched_at": fetched_at,
        "client_version": client_version,
        "etag": etag,
        "models": models,
    }
    if extra_top:
        payload.update(extra_top)
    return payload


def _write_codex(tmp_path, monkeypatch, payload) -> "os.PathLike":
    """Write the Codex cache into an isolated CODEX_HOME and point config at it."""
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir(parents=True, exist_ok=True)
    cache_path = codex_home / "models_cache.json"
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return cache_path


def _rewrite(path, payload):
    """Rewrite the Codex cache and deterministically bump its mtime.

    ``os.utime`` is used instead of ``time.sleep`` so the mtime_ns change is
    guaranteed on every filesystem/timestamp-resolution combination — the test
    must prove the *old* stat fingerprint churned, not rely on clock luck.
    """
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    bumped = path.stat().st_mtime + 7.0
    os.utime(path, (bumped, bumped))
    return path


def _catalog_payload():
    return {
        "active_provider": "codex",
        "default_model": "gpt-5.3-codex",
        "configured_model_badges": {"gpt-5.3-codex": "Codex"},
        "groups": [{"name": "Codex", "models": ["gpt-5.3-codex"]}],
    }


# ── churn-immunity invariant ────────────────────────────────────────────────


def test_codex_fetched_at_only_rewrite_keeps_fingerprint_identical(tmp_path, monkeypatch):
    """Codex's own refresh moves ``fetched_at`` only — same models, same size."""
    p = _write_codex(tmp_path, monkeypatch, _codex_cache(fetched_at=_TS_1))
    fp_before = config._codex_models_cache_fingerprint(p)
    st_before = p.stat()

    _rewrite(p, _codex_cache(fetched_at=_TS_2))
    fp_after = config._codex_models_cache_fingerprint(p)
    st_after = p.stat()

    # Pre-condition: this really is the mtime/size churn from the report — the
    # old stat-based fingerprint WOULD have changed.
    assert (st_before.st_mtime_ns, st_before.st_size) != (
        st_after.st_mtime_ns, st_after.st_size), "test setup: rewrite must churn the stat fields"
    # Invariant: the semantic fingerprint does not.
    assert fp_before == fp_after, (
        "A Codex refresh that only moves fetched_at must NOT change the catalog "
        "fingerprint (#7540) — otherwise the 24h cache is rejected and the next "
        "session visit pays a live rebuild"
    )
    assert "semantic_sha256" in fp_before


def test_unsorted_codex_payload_key_order_keeps_fingerprint_identical(tmp_path, monkeypatch):
    """Key order is not semantic: a reordered but otherwise identical file must
    not bust the cache."""
    p = _write_codex(tmp_path, monkeypatch, _codex_cache())
    fp_before = config._codex_models_cache_fingerprint(p)

    reordered = {
        "models": _codex_cache()["models"],
        "etag": _codex_cache()["etag"],
        "client_version": _codex_cache()["client_version"],
        "fetched_at": _codex_cache()["fetched_at"],
    }
    _rewrite(p, reordered)

    assert config._codex_models_cache_fingerprint(p) == fp_before


def test_codex_metadata_churn_does_not_reject_valid_disk_models_cache(tmp_path, monkeypatch):
    """End-to-end: the disk snapshot a warm server wrote must still load after
    Codex refreshed its own cache."""
    p = _write_codex(tmp_path, monkeypatch, _codex_cache(fetched_at=_TS_1))
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    config._save_models_cache_to_disk(_catalog_payload())
    assert config._load_models_cache_from_disk() is not None

    _rewrite(p, _codex_cache(fetched_at=_TS_2))

    assert config._load_models_cache_from_disk() is not None, (
        "Codex metadata churn must NOT reject a valid on-disk models cache "
        "(#7540) — that rejection is what triggers the live rebuild"
    )


def test_session_visit_after_codex_refresh_needs_no_live_rebuild(tmp_path, monkeypatch):
    """The user-visible symptom: open a conversation after Codex refreshed.

    The warm in-memory snapshot must still be served; a rebuild here is the
    multi-second session-open hang in the report.
    """
    _write_codex(tmp_path, monkeypatch, _codex_cache(fetched_at=_TS_1))
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    payload = _catalog_payload()
    config._save_models_cache_to_disk(payload)

    # Warm the memory cache the way a normal /api/models call leaves it.
    monkeypatch.setattr(config, "_available_models_cache", copy.deepcopy(payload))
    monkeypatch.setattr(config, "_available_models_cache_ts", time.monotonic())
    monkeypatch.setattr(
        config,
        "_available_models_cache_source_fingerprint",
        config._models_cache_source_fingerprint(),
    )

    # Codex's refresh timer fires between two session opens.
    _rewrite(tmp_path / "codex_home" / "models_cache.json",
             _codex_cache(fetched_at=_TS_2))

    def _boom(**_kwargs):
        # BaseException, not Exception: get_available_models_for_session_visit
        # swallows exceptions from its force_refresh path and falls back to the
        # on-disk stale snapshot, so an AssertionError sentinel would be eaten
        # and the test would pass while quietly taking the slow path.
        raise _LiveRebuildReached(
            "session visit fell through to the live rebuild: Codex fetched_at "
            "churn invalidated a still-valid catalog cache (#7540)"
        )

    monkeypatch.setattr(config, "get_available_models", _boom)

    result = config.get_available_models_for_session_visit()

    assert result is not None
    assert result["default_model"] == "gpt-5.3-codex"


def test_warm_memory_cache_survives_codex_churn(tmp_path, monkeypatch):
    """Isolates the invalidation point itself (the memory-cache rejection that
    precedes the disk load and the force_refresh)."""
    _write_codex(tmp_path, monkeypatch, _codex_cache(fetched_at=_TS_1))
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    payload = _catalog_payload()
    config._save_models_cache_to_disk(payload)
    monkeypatch.setattr(config, "_available_models_cache", copy.deepcopy(payload))
    monkeypatch.setattr(config, "_available_models_cache_ts", time.monotonic())
    monkeypatch.setattr(
        config,
        "_available_models_cache_source_fingerprint",
        config._models_cache_source_fingerprint(),
    )

    _rewrite(tmp_path / "codex_home" / "models_cache.json",
             _codex_cache(fetched_at=_TS_2))

    assert config._get_fresh_memory_models_cache(time.monotonic()) is not None, (
        "The warm in-memory catalog cache must survive a Codex fetched_at-only "
        "refresh (#7540)"
    )


# ── real-change-invalidates invariant ───────────────────────────────────────


def test_new_codex_model_changes_fingerprint(tmp_path, monkeypatch):
    p = _write_codex(tmp_path, monkeypatch, _codex_cache())
    fp_before = config._codex_models_cache_fingerprint(p)

    models = _codex_cache()["models"] + [
        {"slug": "gpt-5.4-codex", "visibility": "list", "priority": 3},
    ]
    _rewrite(p, _codex_cache(models=models))

    assert config._codex_models_cache_fingerprint(p) != fp_before, (
        "A new Codex model changes the merged catalog and MUST bust the cache"
    )


def test_model_visibility_change_changes_fingerprint(tmp_path, monkeypatch):
    """``visibility`` gates whether the slug is surfaced — a hide/show flip is a
    real catalog change at unchanged file size."""
    p = _write_codex(tmp_path, monkeypatch, _codex_cache())
    fp_before = config._codex_models_cache_fingerprint(p)

    models = copy.deepcopy(_codex_cache()["models"])
    models[0]["visibility"] = "hidden"
    _rewrite(p, _codex_cache(models=models))

    assert config._codex_models_cache_fingerprint(p) != fp_before


@pytest.mark.parametrize(
    "changed",
    [
        {"client_version": "0.52.0"},
        {"etag": '"codex-etag-2"'},
        {"models": [{"slug": "gpt-5.3-codex", "priority": 9}]},
    ],
)
def test_codex_catalog_fields_stay_in_fingerprint(tmp_path, monkeypatch, changed):
    """Everything that actually feeds the Codex model list stays covered."""
    p = _write_codex(tmp_path, monkeypatch, _codex_cache())
    fp_before = config._codex_models_cache_fingerprint(p)

    _rewrite(p, _codex_cache(**changed))

    assert config._codex_models_cache_fingerprint(p) != fp_before


def test_unknown_codex_field_stays_in_fingerprint(tmp_path, monkeypatch):
    """Deny-list (not allow-list) safety: an unknown field is NOT stripped, so a
    hypothetical future model-gating field still busts the cache."""
    p = _write_codex(tmp_path, monkeypatch,
                     _codex_cache(extra_top={"some_future_model_gate": "v1"}))
    fp_before = config._codex_models_cache_fingerprint(p)

    _rewrite(p, _codex_cache(extra_top={"some_future_model_gate": "v2"}))

    assert config._codex_models_cache_fingerprint(p) != fp_before, (
        "An unknown (non-deny-listed) field must remain in the fingerprint — the "
        "deny-list must fail safe toward over-invalidation"
    )


def test_real_codex_change_still_rejects_previously_valid_disk_cache(tmp_path, monkeypatch):
    """Anti-over-stabilisation mirror of the churn test: a disk snapshot that WAS
    valid must be rejected once a genuine Codex model change lands."""
    p = _write_codex(tmp_path, monkeypatch, _codex_cache())
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    config._save_models_cache_to_disk(_catalog_payload())
    assert config._load_models_cache_from_disk() is not None

    _rewrite(p, _codex_cache(client_version="0.52.0"))

    assert config._load_models_cache_from_disk() is None, (
        "A real Codex catalog change MUST reject the stale disk cache — the fix "
        "must not over-stabilise into serving wrong data"
    )


# ── helper unit guards ──────────────────────────────────────────────────────


def test_strip_volatile_codex_fields_is_pure_and_recursive():
    src = {
        "fetched_at": "ts",
        "client_version": "0.51.0",
        "models": [{"slug": "gpt-5.3-codex", "updated_at": "ts", "priority": 1}],
    }
    snapshot = copy.deepcopy(src)
    out = config._strip_volatile_codex_cache_fields(src)

    assert src == snapshot  # input untouched (pure)
    assert out == {
        "client_version": "0.51.0",
        "models": [{"slug": "gpt-5.3-codex", "priority": 1}],
    }


def test_unparseable_codex_cache_falls_back_to_stat_fingerprint(tmp_path, monkeypatch):
    """Corrupt / mid-write file: fall back to the old stat fingerprint instead of
    silently pinning a hash of garbage — behaviour is never less safe than the
    stat-based version, and a later good file still invalidates."""
    p = _write_codex(tmp_path, monkeypatch, _codex_cache())
    p.write_text('{"models": [', encoding="utf-8")

    fp = config._codex_models_cache_fingerprint(p)
    assert fp.get("semantic") == "unparsed-fallback"
    assert "mtime_ns" in fp and "size" in fp

    _rewrite(p, _codex_cache(fetched_at=_TS_2))
    assert config._codex_models_cache_fingerprint(p) != fp


def test_missing_codex_cache_fingerprint_is_stable_and_marked(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "absent"))
    fp = config._codex_models_cache_fingerprint(tmp_path / "absent" / "models_cache.json")

    assert fp.get("missing") is True
    assert config._codex_models_cache_fingerprint(
        tmp_path / "absent" / "models_cache.json") == fp
