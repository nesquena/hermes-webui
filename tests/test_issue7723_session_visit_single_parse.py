"""Regression tests for issue #7723 follow-up — the session-visit freshness
precheck must not re-parse the whole models cache file.

Background
----------
#7723 ("`models_cache.json` mtime is never refreshed on a cache hit") was fixed
upstream by #7932, which replaced the mtime clock with an explicit ``_built_at``
stamp written by ``_save_models_cache_to_disk``. That fixed the *decision*
(freshness is now judged on build time, not file mtime).

It introduced a cost on the very hot path the issue is about.
``get_available_models_for_session_visit()`` calls
``_models_cache_file_age_seconds()`` on EVERY invocation, and since #7932 that
helper calls ``_models_disk_cache_built_at()``, which does a full
``open()`` + ``json.load()`` of the ENTIRE models cache file — a 200 KB,
5000-model catalog is ~675 KB — purely to read one float. On the fresh
session-visit path the same file is then parsed AGAIN immediately afterwards by
``_load_models_cache_from_disk()``. So every session visit pays 2 full JSON
parses of the durable cache where the pre-#7932 mtime check paid a single
``stat()``.

Measured cost of the redundant precheck (median, warm FS):

    models   file KB   precheck ms   disk load ms
         50       7.3        0.089          0.089
        200      27.2        0.298          0.303
        800     107.2        1.366          1.313
       5000     674.8       10.250          9.763

The precheck is ~1.4x the cost of the load it precedes, and it scales linearly
with catalog size — on a large catalog it roughly doubles session-open disk I/O
for a decision that only needs a single timestamp.

These tests are INVARIANTS, not change-detectors:
  * single-parse   — one session visit reads the durable cache at most once.
  * decision-equal — the fix must not change WHICH branch is taken; the
    ``_built_at`` decision semantics from #7932 are preserved exactly.

A file-lstat-free, byte-bounded read is not acceptable as the fix: correctness
of the freshness decision depends on the real ``_built_at``, so the parse result
must be threaded through to the loader rather than re-derived.
"""

from __future__ import annotations

import builtins  # noqa: F401
import json
import time

import pytest

import api.config as config


def _catalog(label: str, n_models: int = 8) -> dict:
    return {
        "active_provider": "openai",
        "default_model": f"{label}-0",
        "configured_model_badges": {},
        "groups": [
            {
                "provider": "OpenAI",
                "provider_id": "openai",
                "models": [
                    {
                        "id": f"{label}-{i}",
                        "label": f"{label} {i}",
                        "supports_fast_tier": bool(i % 2),
                        "description": "x" * 512,
                    }
                    for i in range(n_models)
                ],
            }
        ],
        "aliases": {},
    }


@pytest.fixture
def durable_cache(tmp_path, monkeypatch):
    """A loadable on-disk models cache with a CURRENT ``_built_at`` stamp."""
    monkeypatch.setattr(config, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(
        config, "_models_cache_source_fingerprint", lambda: {"profile": "demo"}, raising=False
    )
    monkeypatch.setattr(config, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(config, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(config, "_available_models_live_rebuild_ts", 0.0, raising=False)
    monkeypatch.setattr(
        config, "_available_models_cache_source_fingerprint", None, raising=False
    )
    monkeypatch.setattr(config, "_cache_build_in_progress", False, raising=False)
    monkeypatch.setattr(config, "_models_rebuild_seq", 0, raising=False)

    path = tmp_path / "models_cache.profile.json"
    payload = {
        "_schema_version": config._MODELS_CACHE_SCHEMA_VERSION,
        "_source_fingerprint": {"profile": "demo"},
        "_built_at": time.time() - 10,  # fresh: built 10s ago
        "active_provider": "openai",
        "default_model": "cached-0",
        "configured_model_badges": {},
        "groups": _catalog("cached")["groups"],
    }
    # _is_loadable_disk_cache validates the #1633 version stamp whenever the
    # runtime version resolves. api.updates IS imported under pytest (unlike a
    # bare interpreter), so an unstamped payload is rejected here and the visit
    # silently falls through to the live-rebuild path.
    runtime_version = config._current_webui_version()
    if runtime_version is not None:
        payload["_webui_version"] = runtime_version
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: path, raising=False)
    return path


def _count_cache_reads(monkeypatch, path):
    """Count full JSON parses of the durable cache file during one call.

    Only ``json.load`` is wrapped. Patching ``builtins.open`` instead would be
    a false-negative trap: ``_is_loadable_disk_cache`` logs on every rejection
    branch, ``_load_models_cache_from_disk`` wraps its whole body in
    ``except Exception: return None``, so breaking ``open`` anywhere below
    silently turns the disk hit into a miss and pushes the visit onto the
    multi-second live-rebuild path — the visit still "passes" while measuring
    nothing but the rebuild.
    """
    target = str(path)
    real_load = json.load
    counts = {"parse": 0}

    def counting_load(fp, *a, **kw):
        if getattr(fp, "name", None) == target:
            counts["parse"] += 1
        return real_load(fp, *a, **kw)

    monkeypatch.setattr(json, "load", counting_load)
    return counts


def test_one_session_visit_parses_durable_cache_at_most_once(durable_cache, monkeypatch):
    """Req: a fresh session visit must not full-parse the durable cache twice.

    The freshness precheck and the loader read the SAME file. Before the fix the
    precheck re-read+re-parsed all of it to extract one float, doubling session
    -open disk I/O on catalogs of any real size.
    """
    counts = _count_cache_reads(monkeypatch, durable_cache)

    result = config.get_available_models_for_session_visit()

    assert result["default_model"] == "cached-0"
    assert counts["parse"] == 1, (
        f"session visit parsed the durable models cache {counts['parse']}x; "
        "the freshness precheck must reuse the loader's parse, not redo it"
    )


def test_memoised_payload_is_handed_off_once_and_never_leaks_mutations(
    durable_cache, monkeypatch
):
    """Req: a memoised payload must be handed to exactly ONE reader.

    Before the fix every disk read was an independent ``json.load``, so a
    caller mutating the catalog it got back could never corrupt a LATER read.
    A memo that outlives its reader silently breaks that invariant:

    ``warm_models_catalog_provenance_if_cold`` assigns the loader's result
    straight into the process-wide ``_available_models_cache`` with no
    deepcopy, and ``get_available_models_for_session_visit`` returns the
    catalog to WebUI callers that mutate it in place. If the memo still held
    that same payload, a later read would be served catalog objects that a
    previous caller had already edited — and would hand them back as if they
    had just been read from disk.

    The memo exists to collapse the precheck+loader double-parse within ONE
    session visit. It must not survive past the reader it was built for.
    """
    # Populate the memo exactly as the session-visit freshness precheck does.
    assert config._models_disk_cache_built_at(durable_cache) is not None

    first = config._load_models_cache_from_disk()
    assert first is not None
    # Mutate in place, the way the real consumers do
    # (`_annotate_fast_tier_model_groups` writes `supports_fast_tier` into
    # these very dicts on every call).
    first["groups"].append({"injected": True})
    first["groups"][0]["models"][0]["POISON"] = "yes"

    second = config._load_models_cache_from_disk()
    assert second is not None, "second disk read must still succeed"
    assert all("injected" not in g for g in second["groups"]), (
        "a later disk read was served the SAME groups list that a previous "
        "caller mutated in place — the memo outlived its reader"
    )
    assert (
        "POISON" not in second["groups"][0]["models"][0]
    ), "a later disk read was served model dicts a previous caller had edited"


def test_freshness_decision_semantics_preserved(durable_cache, monkeypatch):
    """Req: the parse-reduction must not change WHICH branch the visit takes.

    Guards against a 'fix' that skips the read and silently reverts to the
    pre-#7932 mtime decision — the exact bug #7723 reported.
    """
    # Built 10s ago -> inside the 300s window -> served from disk, no rebuild.
    monkeypatch.setattr(
        config, "get_available_models", lambda **k: pytest.fail("must not rebuild"), raising=False
    )
    assert config.get_available_models_for_session_visit()["default_model"] == "cached-0"

    # Re-stamp the durable cache as 600s old -> outside the window -> must rebuild.
    payload = json.loads(durable_cache.read_text(encoding="utf-8"))
    payload["_built_at"] = time.time() - 600
    durable_cache.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(config, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(config, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(config, "_available_models_live_rebuild_ts", 0.0, raising=False)
    monkeypatch.setattr(
        config, "_available_models_cache_source_fingerprint", None, raising=False
    )

    rebuilt = {"active_provider": "openai", "default_model": "rebuilt-0"}
    calls = []

    def _live_rebuild(**kwargs):
        calls.append(kwargs)
        return rebuilt

    monkeypatch.setattr(config, "get_available_models", _live_rebuild, raising=False)
    monkeypatch.setattr(config, "_load_stale_models_cache_from_disk", lambda: None, raising=False)

    assert config.get_available_models_for_session_visit() == rebuilt
    assert calls == [{"force_refresh": True}]
