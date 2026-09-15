"""The #4633 churn returns through a field #5854 did not move.

#5854 stopped `anchor_activity_scenes` from overflowing the 64 KB metadata
prefix, and cached the authoritative facts of a LEGACY sidecar so an unchanged
one is full-parsed at most once. Both halves are gated on the file being legacy
(`'anchor_scene_index' not in data`). A MODERN sidecar that overflows the prefix
for any OTHER reason therefore gets neither protection: the cheap read fails,
`load_metadata_only()` full-parses, and nothing is cached — so it re-parses on
every poll. That is exactly the #4633 allocation churn the earlier fix removed.

Measured in production on 2026-09-14 (Mac docker stack, 5.77 GiB colima VM):

  /data/hermes/webui/sessions/fa3bca34a0c6.json   111,857,574 bytes, 135,634 msgs
    "compression_anchor_summary"  offset      1,007   (73,192 bytes long)
    "message_count"               offset     75,446
    "anchor_scene_index"          offset     75,473   <- file IS modern
    "messages"                    offset     76,603   <- past the 65,536 budget

`_read_metadata_json_prefix` scanned to `messages`, exhausted its budget first
and returned None; `load_metadata_only` fell back to `json.loads` of all 112 MB;
the legacy-facts cache was skipped because `anchor_scene_index` was present.
`/api/session/status` averaged 1,172 ms over 6,441 calls, one `messages=0`
request took 48.1 s, the server reached 4.0 GB RSS and the guest OOM killer
killed it 7 times.

These tests lock the two properties that were missing:
  * the cheap prefix survives one oversized metadata field,
  * an unchanged modern sidecar is full-parsed at most once regardless.
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


def _make_modern_oversized(session_store, sid, n_msgs=5, summary_bytes=80000):
    """A MODERN sidecar (carries anchor_scene_index) whose metadata prefix
    overflows because of one oversized field, mirroring production.

    Scenes are deliberately tiny: the point is that the overflow comes from a
    field #5854 never moved, not from the scene bodies it did move.
    """
    s = M.Session(
        session_id=sid,
        title="Oversized",
        workspace=str(session_store.parent),
        model="glm",
        messages=[{"role": "user", "content": f"m{i}"} for i in range(n_msgs)],
    )
    s.anchor_activity_scenes = {"scene0": {"version": 1, "updated_at": 1000.0,
                                          "scene": {"activity_rows": []}}}
    s.compression_anchor_summary = "Z" * summary_bytes
    s.save()
    return s


def test_sidecar_is_modern_and_overflows_the_budget(session_store):
    """Guard the premise: the fixture really is the production shape — a modern
    file (anchor_scene_index present, scenes after messages) whose "messages"
    key sits beyond the 64 KB budget.

    If this ever fails, the two tests below stop covering what they claim to.
    """
    _make_modern_oversized(session_store, "mod1")
    raw = (session_store / "mod1.json").read_text(encoding="utf-8")
    assert '"anchor_scene_index"' in raw, "fixture must be MODERN, not legacy"
    ci = raw.find('"compression_anchor_summary"')
    xi = raw.find('"anchor_scene_index"')
    mi = raw.find('"messages"')
    si = raw.find('"anchor_activity_scenes"')
    assert -1 < ci < xi < mi < si, "modern layout: summary < scene_index < messages < scenes"
    assert mi > 65536, f'"messages" at {mi} must exceed the 64 KB budget to reproduce #4633'


def test_cheap_prefix_survives_an_oversized_metadata_field(session_store):
    """The cheap read must stop once it holds the metadata its callers need,
    instead of scanning all the way to "messages" and overflowing on whatever
    large field happens to precede it.

    Scanning-to-"messages" makes every future large metadata field a repeat of
    #4633; stopping at "enough" does not.
    """
    _make_modern_oversized(session_store, "mod2", n_msgs=7)
    prefix = M._read_metadata_json_prefix(session_store / "mod2.json")
    assert prefix is not None, "cheap prefix must succeed, not fall back to a full parse"
    parsed = json.loads(prefix)
    assert {"session_id", "title", "created_at", "updated_at"}.issubset(parsed.keys())
    assert parsed["message_count"] == 7
    assert "messages" not in parsed, "the messages array must not be in the cheap prefix"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP, not a flake: a sidecar whose metadata blob exceeds the prefix "
        "budget still re-parses per read. A failed prefix read yields no metadata at "
        "all, and the facts cache holds only counts (no title/created_at), so no stub "
        "can be built from it. The durable cure is write-side -- serialize the "
        "unbounded blobs AFTER `messages`, as #5854 did for scene bodies. strict=True "
        "so this fails loudly once that lands, instead of quietly passing and leaving "
        "a stale marker behind."
    ),
)
def test_modern_oversized_sidecar_not_reparsed_on_every_read(session_store, monkeypatch):
    """An unchanged sidecar must be full-parsed at most once, whether it is
    legacy or modern.

    This is the production failure: 6,441 polls of one session, each paying a
    full 112 MB `json.loads`, because the facts cache is reached only when
    `anchor_scene_index` is ABSENT. The counterpart legacy assertion already
    exists (test_issue5854_anchor_scene_split.py::
    test_legacy_large_scene_not_reparsed_on_every_read); the modern one does not.

    The field here deliberately exceeds the prefix budget so the fallback is
    genuinely taken. A budget bump alone must NOT be able to satisfy this test —
    otherwise it stops covering the caching hole and silently passes for the
    wrong reason.
    """
    M._LEGACY_SIDECAR_FACTS.clear()
    _make_modern_oversized(session_store, "mod3", n_msgs=5,
                           summary_bytes=M._METADATA_PREFIX_MAX_BYTES + 200_000)
    # Sanity: the cheap read genuinely fails here, so the slow path IS exercised.
    assert M._read_metadata_json_prefix(session_store / "mod3.json") is None, (
        "fixture must overflow the prefix budget; otherwise this test does not "
        "cover the fallback caching at all"
    )

    calls = {"n": 0}
    real_load = M.Session.load.__func__

    def _counting_load(cls, sid, *a, **k):
        if sid == "mod3":
            calls["n"] += 1
        return real_load(cls, sid, *a, **k)

    monkeypatch.setattr(M.Session, "load", classmethod(_counting_load))

    stubs = [M.Session.load_metadata_only("mod3") for _ in range(3)]
    for stub in stubs:
        assert stub is not None
        assert (stub._metadata_message_count or len(stub.messages)) == 5

    assert calls["n"] <= 1, (
        f"modern oversized sidecar full-loaded {calls['n']}x across 3 metadata reads; "
        "an unchanged file must be parsed at most once"
    )
