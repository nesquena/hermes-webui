"""Perf regression tests — lineage-stitch memoization for GET /api/session.

The compression-continuation stitch (`_webui_sidecar_lineage_messages_for_display`)
re-merged multi-thousand-message parent+child transcripts on EVERY /api/session
request (seconds per call on real sessions). The merge result is now cached,
keyed by the exact stat signature of every sidecar involved, so an idle
historical lineage merges once. These tests pin the contract:

- warm calls return the same rows without re-merging;
- cache hits hand out copies (caller mutation cannot corrupt the cache);
- any write to child OR snapshot-parent sidecar invalidates the entry.
"""
from __future__ import annotations

import time

import pytest

import api.profiles as profiles


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    return home


@pytest.fixture
def lineage(hermes_home, monkeypatch):
    """A snapshot parent + continuation child pair persisted to disk."""
    import api.routes as routes
    from api.models import Session

    monkeypatch.setattr(routes, "SESSION_DIR", hermes_home / "sessions")
    import api.models as models

    monkeypatch.setattr(models, "SESSION_DIR", hermes_home / "sessions")
    # Fresh cache per test.
    routes._lineage_display_cache.clear()

    parent = Session(
        session_id="lineage_parent",
        title="parent",
        messages=[
            {"role": "user", "content": f"question {i}", "timestamp": 1000 + i * 10}
            if i % 2 == 0
            else {"role": "assistant", "content": f"answer {i}", "timestamp": 1000 + i * 10}
            for i in range(40)
        ],
    )
    parent.pre_compression_snapshot = True
    parent.save()

    child = Session(
        session_id="lineage_child",
        title="child",
        messages=[
            {"role": "user", "content": "suite", "timestamp": 5000},
            {"role": "assistant", "content": "reponse suite", "timestamp": 5010},
        ],
    )
    child.parent_session_id = "lineage_parent"
    child.save()
    return routes, Session, child


def test_lineage_stitch_is_cached_and_correct(lineage):
    routes, Session, child = lineage
    m1 = routes._webui_sidecar_lineage_messages_for_display(child)
    assert routes._lineage_display_cache, "merge result must be cached"
    m2 = routes._webui_sidecar_lineage_messages_for_display(child)
    assert [m.get("timestamp") for m in m1] == [m.get("timestamp") for m in m2]
    # Parent turns + child turns all present.
    assert len(m1) == 42


def test_cache_hits_return_copies_not_aliases(lineage):
    routes, Session, child = lineage
    routes._webui_sidecar_lineage_messages_for_display(child)
    m2 = routes._webui_sidecar_lineage_messages_for_display(child)
    m2[0]["__mutated"] = True
    m3 = routes._webui_sidecar_lineage_messages_for_display(child)
    assert "__mutated" not in m3[0], "caller mutation leaked into the cache"


def test_child_write_invalidates_cache(lineage, hermes_home):
    routes, Session, child = lineage
    routes._webui_sidecar_lineage_messages_for_display(child)
    child.messages.append({"role": "user", "content": "new turn", "timestamp": 6000})
    time.sleep(0.01)
    child.save()
    reloaded = Session.load("lineage_child")
    merged = routes._webui_sidecar_lineage_messages_for_display(reloaded)
    assert any(m.get("content") == "new turn" for m in merged), (
        "stale cache served after the child sidecar changed"
    )


def test_parent_write_invalidates_cache(lineage, hermes_home):
    routes, Session, child = lineage
    routes._webui_sidecar_lineage_messages_for_display(child)
    parent = Session.load("lineage_parent")
    parent.messages.append(
        {"role": "assistant", "content": "late parent row", "timestamp": 1999}
    )
    time.sleep(0.01)
    parent.save()
    merged = routes._webui_sidecar_lineage_messages_for_display(child)
    assert any(m.get("content") == "late parent row" for m in merged), (
        "stale cache served after the snapshot parent sidecar changed"
    )


def test_lru_eviction_during_parent_validation_cannot_crash(lineage, monkeypatch):
    """A cache entry may disappear while its parent signatures are checked."""
    routes, Session, child = lineage
    import api.models as models

    expected = routes._webui_sidecar_lineage_messages_for_display(child)
    real_signature = models._sidecar_stat_signature
    evicted = False

    def evict_on_parent(path):
        nonlocal evicted
        if not evicted and path.stem == "lineage_parent":
            evicted = True
            with routes._lineage_display_cache_lock:
                routes._lineage_display_cache.pop(child.session_id, None)
        return real_signature(path)

    monkeypatch.setattr(models, "_sidecar_stat_signature", evict_on_parent)
    got = routes._webui_sidecar_lineage_messages_for_display(child)

    assert evicted
    assert [m.get("content") for m in got] == [m.get("content") for m in expected]


def test_incomplete_multihop_parent_signatures_disable_cache(hermes_home, monkeypatch):
    import api.models as models
    import api.routes as routes
    from api.models import Session

    monkeypatch.setattr(routes, "SESSION_DIR", hermes_home / "sessions")
    monkeypatch.setattr(models, "SESSION_DIR", hermes_home / "sessions")
    routes._lineage_display_cache.clear()

    grandparent = Session(
        session_id="lineage_grandparent",
        messages=[{"role": "user", "content": "grand", "timestamp": 1}],
    )
    grandparent.pre_compression_snapshot = True
    grandparent.save()
    parent = Session(
        session_id="lineage_parent_two",
        messages=[{"role": "assistant", "content": "parent", "timestamp": 2}],
    )
    parent.pre_compression_snapshot = True
    parent.parent_session_id = grandparent.session_id
    parent.save()
    child = Session(
        session_id="lineage_child_two",
        messages=[{"role": "user", "content": "child", "timestamp": 3}],
    )
    child.parent_session_id = parent.session_id
    child.save()

    real_signature = models._sidecar_stat_signature

    def incomplete_signature(path):
        if path.stem == grandparent.session_id:
            return None
        return real_signature(path)

    monkeypatch.setattr(models, "_sidecar_stat_signature", incomplete_signature)
    got = routes._webui_sidecar_lineage_messages_for_display(child)

    assert [m.get("content") for m in got] == ["grand", "parent", "child"]
    with routes._lineage_display_cache_lock:
        assert child.session_id not in routes._lineage_display_cache


def test_sessions_without_lineage_do_not_pollute_cache(lineage):
    routes, Session, child = lineage
    routes._lineage_display_cache.clear()
    solo = Session(
        session_id="lineage_solo",
        title="solo",
        messages=[{"role": "user", "content": "hi", "timestamp": 1}],
    )
    solo.save()
    out = routes._webui_sidecar_lineage_messages_for_display(solo)
    assert len(out) == 1
    assert "lineage_solo" not in routes._lineage_display_cache
