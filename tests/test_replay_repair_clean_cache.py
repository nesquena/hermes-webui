"""Regression tests for the known-clean replay-repair cache.

Replay repair runs on every session LOAD and dominates cold-load cost (measured
485ms of repair for 171ms of JSON reading on a real deployment). It is also
almost always a no-op: on 60 consecutive real sidecars it changed nothing 60
times. `_repair_session_message_projections_cached` memoizes only that negative
verdict, keyed by the exact file bytes.

These tests pin the three properties that make the cache safe:

1. a cache hit returns data identical to running the full pipeline;
2. a session that genuinely needs repair is ALWAYS repaired, never cached away;
3. the collapse helpers do not mutate their input, which is what makes skipping
   them equivalent to running them.
"""

import copy
import hashlib
import json

import pytest

from api import models


@pytest.fixture(autouse=True)
def _clear_cache():
    models._repair_clean_digests.clear()
    yield
    models._repair_clean_digests.clear()


def _clean_session(session_id="20260101_000000_clean"):
    return {
        "session_id": session_id,
        "messages": [
            {"role": "user", "content": "bonjour"},
            {"role": "assistant", "content": "salut", "id": "a1"},
            {"role": "user", "content": "merci"},
            {"role": "assistant", "content": "de rien", "id": "a2"},
        ],
    }


def _dirty_session(session_id="20260101_000000_dirty"):
    """Two adjacent assistant rows with identical payloads: a real replay."""
    duplicated = {"role": "assistant", "content": "reponse", "id": "dup"}
    return {
        "session_id": session_id,
        "messages": [
            {"role": "user", "content": "question"},
            dict(duplicated),
            dict(duplicated),
        ],
    }


def test_cache_hit_matches_uncached_result():
    """A cached load must return exactly what the full pipeline returns."""
    reference, ref_msg, ref_ctx = models._repair_session_message_projections(
        _clean_session()
    )

    digest = "d" * 64
    first, msg1, ctx1 = models._repair_session_message_projections_cached(
        _clean_session(), digest
    )
    assert (msg1, ctx1) == (ref_msg, ref_ctx)
    assert first == reference

    # Second call takes the cache path.
    assert digest in models._repair_clean_digests
    second, msg2, ctx2 = models._repair_session_message_projections_cached(
        _clean_session(), digest
    )
    assert (msg2, ctx2) == (False, False)
    assert second == reference


def test_session_needing_repair_is_never_cached():
    """A positive verdict must not be memoized, and must repair every time."""
    digest = "e" * 64

    _, msg1, _ = models._repair_session_message_projections_cached(
        _dirty_session(), digest
    )
    assert msg1 is True, "the duplicated assistant row should have been collapsed"
    assert digest not in models._repair_clean_digests

    # Same bytes again: repair must run again, not be skipped.
    repaired, msg2, _ = models._repair_session_message_projections_cached(
        _dirty_session(), digest
    )
    assert msg2 is True
    assert len(repaired["messages"]) == 2


def test_distinct_digests_do_not_share_verdicts():
    """A clean file must never authorize skipping repair on a different file."""
    models._repair_session_message_projections_cached(_clean_session(), "a" * 64)

    repaired, changed, _ = models._repair_session_message_projections_cached(
        _dirty_session(), "b" * 64
    )
    assert changed is True
    assert len(repaired["messages"]) == 2


def test_missing_digest_forces_full_pipeline():
    """Without a trustworthy key the cache must not engage."""
    _, changed, _ = models._repair_session_message_projections_cached(
        _clean_session(), None
    )
    assert changed is False
    assert not models._repair_clean_digests

    repaired, changed, _ = models._repair_session_message_projections_cached(
        _dirty_session(), None
    )
    assert changed is True
    assert len(repaired["messages"]) == 2


def test_collapse_helpers_do_not_mutate_input():
    """The safety hypothesis behind the cache.

    Skipping repair is only equivalent to running it because the collapse
    helpers build new lists instead of editing messages in place. If that ever
    changes, this test fails instead of the cache silently serving stale data.
    """
    for factory in (_clean_session, _dirty_session):
        messages = factory()["messages"]
        snapshot = copy.deepcopy(messages)
        models._collapse_replayed_assistant_rows(messages)
        assert messages == snapshot, f"{factory.__name__}: input was mutated in place"


def test_cache_is_bounded():
    """The cache must not grow without limit on a long-lived server."""
    for i in range(models._REPAIR_CLEAN_CACHE_SIZE + 50):
        models._repair_session_message_projections_cached(
            _clean_session(), f"{i:064x}"
        )
    assert len(models._repair_clean_digests) <= models._REPAIR_CLEAN_CACHE_SIZE


def test_load_path_repairs_dirty_session_then_caches_clean(tmp_path, monkeypatch):
    """End-to-end through the real file loader."""
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)

    sid = "20260101_000000_dirty"
    path = tmp_path / f"{sid}.json"
    path.write_text(json.dumps(_dirty_session(sid)), encoding="utf-8")

    session = models._load_session_from_path(path)
    assert session is not None
    assert len(session.messages) == 2, "repair must run on a dirty sidecar"

    # A clean sidecar gets its digest memoized by the same loader.
    clean_sid = "20260101_000000_clean"
    clean_path = tmp_path / f"{clean_sid}.json"
    raw = json.dumps(_clean_session(clean_sid)).encode("utf-8")
    clean_path.write_bytes(raw)

    loaded = models._load_session_from_path(clean_path)
    assert loaded is not None
    assert len(loaded.messages) == 4
    assert hashlib.sha256(raw).hexdigest() in models._repair_clean_digests


def test_rewritten_file_invalidates_the_verdict(tmp_path, monkeypatch):
    """New bytes must never inherit the previous verdict."""
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)

    sid = "20260101_000000_evolving"
    path = tmp_path / f"{sid}.json"
    path.write_bytes(json.dumps(_clean_session(sid)).encode("utf-8"))
    assert models._load_session_from_path(path) is not None

    # The session is rewritten and now contains a replay.
    path.write_bytes(json.dumps(_dirty_session(sid)).encode("utf-8"))
    session = models._load_session_from_path(path)
    assert session is not None
    assert len(session.messages) == 2, "changed bytes must miss the cache"
