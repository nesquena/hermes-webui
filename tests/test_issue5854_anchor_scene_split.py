"""Regression coverage for #5854: move anchor_activity_scenes out of the metadata prefix.

Root of #4633 (server RSS climb → OOM) and #5839 (browser freeze): reasoning-heavy
sessions store 250–480 KB of `anchor_activity_scenes`, and `save()` serialized them
BEFORE `messages`, so the cheap metadata-prefix read (`_read_metadata_json_prefix`,
64 KB cap) overflowed and fell back to a full multi-MB `json.loads` on every
sidebar poll — the allocation churn that drove the RSS high-water crash.

The fix persists a compact `anchor_scene_index` fingerprint ({scene_key: updated_at})
in the metadata prefix (after `message_count`, before `messages`) and moves the full
scene bodies AFTER `messages`. The sidebar-poll freshness check reads the fingerprint;
the full-session GET still reads full bodies. These tests lock:
  * the new serialization order (message_count < scene_index < messages < scenes),
  * the cheap prefix stays small for a large-scene session (the #4633 fix),
  * metadata-only stubs carry the fingerprint and a truthful message_count,
  * full load round-trips scene bodies unchanged,
  * the freshness comparison stays behavior-identical (keys + max updated_at),
  * legacy-layout back-compat (scenes-before-messages) + the message_count recovery.
"""
import json

import pytest

import api.models as M


@pytest.fixture
def session_store(tmp_path, monkeypatch):
    sdir = tmp_path / "sessions"
    monkeypatch.setattr(M, "SESSION_DIR", sdir)
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


def _big_scenes(n_scenes=3, n_rows=80, base_ts=1000.0):
    rows = [{"row_id": f"r{i}", "kind": "tool", "text": "X" * 3000,
             "thinking": {"text": ""}, "tool": {"name": "read_file"}} for i in range(n_rows)]
    return {f"scene{j}": {"version": 1, "updated_at": base_ts + j,
                          "scene": {"activity_rows": rows}} for j in range(n_scenes)}


def _make(session_store, sid="s1", scenes=None, msgs=None):
    s = M.Session(session_id=sid, title="T", workspace=str(session_store.parent),
                  model="glm", messages=msgs if msgs is not None else [
                      {"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}])
    if scenes is not None:
        s.anchor_activity_scenes = scenes
    s.save()
    return s


# ── Serialization order + cheap prefix (the #4633 fix) ──────────────────────

def test_scene_bodies_serialize_after_messages_with_fingerprint_before(session_store):
    _make(session_store, "s1", scenes=_big_scenes())
    raw = (session_store / "s1.json").read_text(encoding="utf-8")
    ci = raw.find('"message_count"')
    xi = raw.find('"anchor_scene_index"')
    mi = raw.find('"messages"')
    si = raw.find('"anchor_activity_scenes"')
    assert -1 < ci < xi < mi < si, "order must be message_count < scene_index < messages < scenes"


def test_large_scene_session_keeps_cheap_prefix_small(session_store):
    """The #4633 root: a 250KB+ scene session must NOT force a full parse."""
    _make(session_store, "s1", scenes=_big_scenes())
    prefix = M._read_metadata_json_prefix(session_store / "s1.json")
    assert prefix is not None, "cheap prefix must succeed (not fall back to full load)"
    assert len(prefix.encode("utf-8")) < 65536
    parsed = json.loads(prefix)
    assert parsed["message_count"] == 2
    assert set(parsed["anchor_scene_index"]) == {"scene0", "scene1", "scene2"}
    assert "anchor_activity_scenes" not in parsed, "full scenes must not be in the cheap prefix"


def test_metadata_only_stub_is_cheap_and_correct(session_store):
    _make(session_store, "s1", scenes=_big_scenes())
    stub = M.Session.load_metadata_only("s1")
    assert stub is not None
    assert stub.messages == []
    assert stub._metadata_message_count == 2
    assert stub._loaded_metadata_only is True
    # fingerprint available on the stub; full scenes not materialized
    assert M._session_scene_keys(stub) == {"scene0", "scene1", "scene2"}
    assert M._session_scene_updated_at(stub) == 1002.0


def test_full_load_round_trips_scene_bodies(session_store):
    scenes = _big_scenes(n_scenes=3, n_rows=80)
    _make(session_store, "s1", scenes=scenes)
    full = M.Session.load("s1")
    assert set(full.anchor_activity_scenes) == {"scene0", "scene1", "scene2"}
    assert len(full.anchor_activity_scenes["scene0"]["scene"]["activity_rows"]) == 80
    # bodies unchanged
    assert full.anchor_activity_scenes["scene0"]["scene"]["activity_rows"][0]["text"] == "X" * 3000


def test_save_reload_save_is_idempotent_on_scene_index(session_store):
    _make(session_store, "s1", scenes=_big_scenes())
    full = M.Session.load("s1")
    full.save(touch_updated_at=False)
    prefix = json.loads(M._read_metadata_json_prefix(session_store / "s1.json"))
    assert set(prefix["anchor_scene_index"]) == {"scene0", "scene1", "scene2"}
    assert prefix["message_count"] == 2


# ── Freshness comparison stays behavior-identical ───────────────────────────

def test_disk_scene_fingerprint_modern_and_legacy_equivalent():
    # modern: fingerprint present
    modern = {"anchor_scene_index": {"a": 5.0, "b": 9.0}}
    keys, latest = M._disk_scene_fingerprint(modern)
    assert keys == {"a", "b"} and latest == 9.0
    # legacy: full bodies inline (scenes-before-messages layout)
    legacy = {"anchor_activity_scenes": {"a": {"updated_at": 5.0}, "b": {"updated_at": 9.0}}}
    keys2, latest2 = M._disk_scene_fingerprint(legacy)
    assert keys2 == {"a", "b"} and latest2 == 9.0
    # neither present → None (must fall through, not assume "no scenes")
    assert M._disk_scene_fingerprint({"title": "x"}) is None
    # empty modern index present → ("no scenes", 0.0), NOT None
    assert M._disk_scene_fingerprint({"anchor_scene_index": {}}) == (set(), 0.0)


def test_cached_lags_disk_detects_new_scene_and_newer_timestamp(session_store):
    # persist a session with scene0@1000
    _make(session_store, "s1", scenes={"scene0": {"updated_at": 1000.0, "scene": {}}})
    # cached copy that is BEHIND (no scenes) → must lag
    cached_behind = M.Session(session_id="s1", title="T", workspace=str(session_store.parent),
                              model="glm", messages=[{"role": "user", "content": "hi"},
                                                     {"role": "assistant", "content": "yo"}])
    assert M._cached_session_lags_disk(cached_behind) is True
    # cached copy AT PARITY → must NOT lag
    cached_parity = M.Session(session_id="s1", title="T", workspace=str(session_store.parent),
                              model="glm", messages=[{"role": "user", "content": "hi"},
                                                     {"role": "assistant", "content": "yo"}])
    cached_parity.anchor_activity_scenes = {"scene0": {"updated_at": 1000.0, "scene": {}}}
    assert M._cached_session_lags_disk(cached_parity) is False
    # cached copy AHEAD of disk (extra un-persisted scene) → must NOT force reload
    cached_ahead = M.Session(session_id="s1", title="T", workspace=str(session_store.parent),
                             model="glm", messages=[{"role": "user", "content": "hi"},
                                                    {"role": "assistant", "content": "yo"}])
    cached_ahead.anchor_activity_scenes = {"scene0": {"updated_at": 1000.0, "scene": {}},
                                           "scene1": {"updated_at": 2000.0, "scene": {}}}
    assert M._cached_session_lags_disk(cached_ahead) is False


def test_cached_lags_disk_detects_newer_scene_updated_at(session_store):
    _make(session_store, "s1", scenes={"scene0": {"updated_at": 2000.0, "scene": {}}})
    cached_stale = M.Session(session_id="s1", title="T", workspace=str(session_store.parent),
                             model="glm", messages=[{"role": "user", "content": "hi"},
                                                    {"role": "assistant", "content": "yo"}])
    cached_stale.anchor_activity_scenes = {"scene0": {"updated_at": 1000.0, "scene": {}}}
    assert M._cached_session_lags_disk(cached_stale) is True


def test_parity_cache_not_forced_reload_when_prefix_read_fails(session_store):
    """MUST-FIX regression (Opus round-1): when the cheap prefix read fails
    (oversized metadata field > 64KB), the slow path must compare the CACHED
    side against its REAL records, not a stale load-time fingerprint. A cache at
    exact parity with disk must NOT be judged as lagging (which would force a
    spurious full reload of the heavy session this PR optimizes).

    Simulate an oversized metadata prefix by giving the session a large
    `compression_anchor_summary` so `_read_metadata_json_prefix` overflows and
    the slow path (Session.load_metadata_only) is taken.
    """
    scenes = {"scene0": {"updated_at": 1000.0, "scene": {}}}
    s = M.Session(session_id="p1", title="T", workspace=str(session_store.parent),
                  model="glm", messages=[{"role": "user", "content": "hi"},
                                         {"role": "assistant", "content": "yo"}])
    s.anchor_activity_scenes = scenes
    s.compression_anchor_summary = "Z" * 80000  # push metadata prefix > 64KB
    s.save()
    # Sanity: the cheap prefix genuinely fails for this file (slow path taken).
    assert M._read_metadata_json_prefix(session_store / "p1.json") is None
    # A cached full session at exact parity (same scene key + updated_at) whose
    # load-time fingerprint is stale/empty must NOT be judged as lagging.
    cached_parity = M.Session(session_id="p1", title="T", workspace=str(session_store.parent),
                              model="glm", messages=[{"role": "user", "content": "hi"},
                                                     {"role": "assistant", "content": "yo"}])
    cached_parity.anchor_activity_scenes = {"scene0": {"updated_at": 1000.0, "scene": {}}}
    cached_parity._anchor_scene_index = {}  # stale/empty load-time fingerprint
    cached_parity.compression_anchor_summary = "Z" * 80000
    assert M._cached_session_lags_disk(cached_parity) is False
    # But a genuinely-behind cache (disk has a newer scene) still reloads.
    cached_behind = M.Session(session_id="p1", title="T", workspace=str(session_store.parent),
                              model="glm", messages=[{"role": "user", "content": "hi"},
                                                     {"role": "assistant", "content": "yo"}])
    cached_behind.anchor_activity_scenes = {}
    assert M._cached_session_lags_disk(cached_behind) is True


# ── Legacy back-compat ──────────────────────────────────────────────────────

def _write_legacy(session_store, sid, scenes, message_count, with_msgs=True):
    """Emulate a pre-#5854 sidecar: scenes BEFORE message_count/messages, no fingerprint."""
    doc = {"session_id": sid, "title": "Legacy", "workspace": str(session_store.parent),
           "model": "glm", "created_at": 1.0, "updated_at": 2.0,
           "anchor_activity_scenes": scenes,
           "message_count": message_count,
           "messages": [{"role": "user", "content": "hi"}] if with_msgs else []}
    (session_store / f"{sid}.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def test_legacy_layout_metadata_only_reads_count_and_scene_keys(session_store):
    _write_legacy(session_store, "leg1", {"sc0": {"updated_at": 5.0}}, message_count=7)
    stub = M.Session.load_metadata_only("leg1")
    assert stub is not None
    # legacy message_count survives (it's before scenes in this legacy doc, but even
    # when after, the recovery path covers it — see next test)
    assert stub._metadata_message_count == 7


def test_legacy_large_scene_missing_count_recovers_via_full_load(session_store):
    """The Codex CORE hazard: legacy scenes-before-count + large scenes. The cheap
    prefix stops at scenes and misses message_count; with no index count, a naive
    stub would report 0 msgs and the session could vanish. Must recover via full load."""
    big = _big_scenes(n_scenes=3, n_rows=80)  # >64KB so prefix stops at scenes
    # legacy order: scenes FIRST, then message_count AFTER (pre-#5854 shape)
    doc = {"session_id": "leg2", "title": "Legacy", "workspace": str(session_store.parent),
           "model": "glm", "created_at": 1.0, "updated_at": 2.0,
           "anchor_activity_scenes": big,
           "message_count": 5,
           "messages": [{"role": "user", "content": f"m{i}"} for i in range(5)]}
    (session_store / "leg2.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    # No _index.json entry → must fall back to full load and get the real count.
    stub = M.Session.load_metadata_only("leg2")
    assert stub is not None
    assert (stub._metadata_message_count or len(stub.messages)) == 5, "must not report 0 messages"


def test_legacy_missing_count_uses_authoritative_not_stale_index(session_store):
    """Codex round-2/3 CORE: on a legacy sidecar whose prefix lacks message_count,
    _persisted_message_count must NOT trust a (possibly lagging) _index.json. It
    full-parses once to get the AUTHORITATIVE count (and caches it), rather than
    under-reporting from a stale index or returning None (which would pin the
    session in memory as non-evictable and can 404 the first send)."""
    big = _big_scenes(n_scenes=3, n_rows=80)
    doc = {"session_id": "leg3", "title": "Legacy", "workspace": str(session_store.parent),
           "model": "glm", "created_at": 1.0, "updated_at": 2.0,
           "anchor_activity_scenes": big, "message_count": 4,
           "messages": [{"role": "user", "content": f"m{i}"} for i in range(4)]}
    (session_store / "leg3.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    M._LEGACY_SIDECAR_FACTS.clear()
    # Prefix stops at scenes (no count, no fingerprint) → must return the real
    # authoritative count (4), never None and never a stale index value.
    assert M._persisted_message_count("leg3") == 4


def test_legacy_disk_ahead_scene_detected_via_full_load(session_store):
    """Codex round-2 SILENT: a legacy stub carries no scene signal (no fingerprint,
    scenes after the prefix). The slow path must full-load to detect a genuine
    disk-ahead scene change instead of serving a stale cached worklog."""
    # legacy file WITH a prefix-captured count (small enough) but scenes present;
    # emulate the stub-with-no-scene-signal by writing legacy layout.
    doc = {"session_id": "leg4", "title": "Legacy", "workspace": str(session_store.parent),
           "model": "glm", "created_at": 1.0, "updated_at": 2.0,
           "message_count": 2,
           "anchor_activity_scenes": {"sceneNEW": {"updated_at": 5000.0, "scene": {}}},
           "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]}
    (session_store / "leg4.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    # cached copy is BEHIND: it has an older/different scene set.
    cached_behind = M.Session(session_id="leg4", title="Legacy", workspace=str(session_store.parent),
                              model="glm", messages=[{"role": "user", "content": "hi"},
                                                     {"role": "assistant", "content": "yo"}])
    cached_behind.anchor_activity_scenes = {}
    assert M._cached_session_lags_disk(cached_behind) is True


def test_fully_loaded_session_never_uses_stale_fingerprint(session_store):
    """Codex round-2 SILENT: the scene helpers must read REAL records for a
    fully-loaded session, never its (possibly stale) load-time _anchor_scene_index."""
    full = M.Session(session_id="f1", title="T", workspace=str(session_store.parent),
                     model="glm", messages=[{"role": "user", "content": "hi"}])
    full.anchor_activity_scenes = {"sceneA": {"updated_at": 30.0}}
    full._anchor_scene_index = {"sceneOLD": 10.0}  # stale load-time fingerprint
    # _loaded_metadata_only is falsy → must read the REAL records, not the index.
    assert M._session_scene_keys(full) == {"sceneA"}
    assert M._session_scene_updated_at(full) == 30.0
    # A metadata-only stub, by contrast, DOES use its fingerprint.
    stub = M.Session(session_id="f2", title="T", workspace=str(session_store.parent),
                     model="glm", messages=[])
    stub._loaded_metadata_only = True
    stub.anchor_activity_scenes = {}
    stub._anchor_scene_index = {"sceneB": 42.0}
    assert M._session_scene_keys(stub) == {"sceneB"}
    assert M._session_scene_updated_at(stub) == 42.0


def _write_legacy_large(session_store, sid, n_msgs=5):
    """Write a legacy large-scene sidecar (scenes before count, >64KB prefix)."""
    big = _big_scenes(n_scenes=3, n_rows=80)
    doc = {"session_id": sid, "title": "Legacy", "workspace": str(session_store.parent),
           "model": "glm", "created_at": 1.0, "updated_at": 2.0,
           "anchor_activity_scenes": big,
           "message_count": n_msgs,
           "messages": [{"role": "user", "content": f"m{i}"} for i in range(n_msgs)]}
    (session_store / f"{sid}.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def test_legacy_large_scene_not_reparsed_on_every_read(session_store, monkeypatch):
    """Codex round-3 SILENT: an unchanged legacy large-scene sidecar must be
    full-parsed at most once (cached by stat signature), not on every poll —
    otherwise the #4633 churn returns for legacy files that are never re-saved."""
    M._LEGACY_SIDECAR_FACTS.clear()
    _write_legacy_large(session_store, "legcache", n_msgs=5)
    # Count real full-loads via a spy on the raw file read inside load().
    calls = {"n": 0}
    real_load = M.Session.load.__func__

    def _counting_load(cls, sid, *a, **k):
        if sid == "legcache":
            calls["n"] += 1
        return real_load(cls, sid, *a, **k)

    monkeypatch.setattr(M.Session, "load", classmethod(_counting_load))
    s1 = M.Session.load_metadata_only("legcache")
    s2 = M.Session.load_metadata_only("legcache")
    s3 = M.Session.load_metadata_only("legcache")
    assert s1._metadata_message_count == 5
    assert s2._metadata_message_count == 5
    assert s3._metadata_message_count == 5
    assert calls["n"] == 1, f"legacy file full-loaded {calls['n']}x; must be cached after the first"


def test_legacy_session_stays_evictable(session_store):
    """Codex round-3 SILENT: a clean legacy session must remain LRU-evictable —
    _persisted_message_count must yield its authoritative count (via the facts
    cache) rather than None (which would pin it in memory forever)."""
    M._LEGACY_SIDECAR_FACTS.clear()
    _write_legacy_large(session_store, "legevict", n_msgs=3)
    # Prime the facts cache via a full load (mirrors get_session materializing it).
    M.Session.load("legevict")
    assert M._persisted_message_count("legevict") == 3


def test_legacy_count_recovers_on_facts_cache_miss(session_store):
    """Codex round-3 CORE: even with the legacy-facts cache EMPTY (never loaded,
    or LRU-evicted), _persisted_message_count must not return None — it full-parses
    once and returns the authoritative count, so the session stays evictable and a
    fresh new_session() can't evict its own unsaved session and 404 the first send."""
    _write_legacy_large(session_store, "legmiss", n_msgs=6)
    M._LEGACY_SIDECAR_FACTS.clear()  # simulate cold / evicted facts cache
    assert M._persisted_message_count("legmiss") == 6
    # and the miss re-populated the cache for the next call
    assert M._legacy_sidecar_facts_get("legmiss") is not None
