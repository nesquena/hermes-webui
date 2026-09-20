"""Cold-load regression tests — skip ancestor loads that cannot contribute.

Root cause, proven against live data on 2026-08-24:

``_webui_sidecar_lineage_messages_for_display`` walks up to 20 snapshot
ancestors and calls ``Session.load()`` on each — tens of MB of JSON — *before*
it knows how big a window the caller wants. On this host every one of those
snapshots carries ``truncation_watermark == 0.0``, the truncate-to-empty
sentinel. Inside ``merge_session_messages_append_only`` that sentinel takes the
``watermark_timestamp == 0`` branch and returns ``[]``, so the loop accumulator
— seeded ``merged = []`` — is reset to empty on *every* hop. The stitched
ancestor prefix is therefore always empty.

Net effect: seconds of CPU spent producing zero additional visible rows.
Measured on a 9-hop / 167 MB lineage: 9.87s cold vs 0.04s warm, of which ~85%
was ancestor loading that contributed nothing.

The existing early-return guard cannot catch this: it calls
``_messages_start_with_visible_prefix(child, parent)``, which requires
``len(messages) >= len(prefix)`` and so returns False whenever the child is
SHORTER than the parent — exactly what compression produces.

These tests pin the fix:

- a neutralised ancestor is not loaded at all;
- the visible output is byte-for-byte what the full walk produced;
- an ACTIVE session (never cached, so it re-pays on every poll) benefits too;
- a genuine snapshot WITHOUT the sentinel still stitches its history.
"""
from __future__ import annotations

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


def _turns(n, base_ts, tag):
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"{tag}-{i}",
            "timestamp": base_ts + i * 10,
        }
        for i in range(n)
    ]


@pytest.fixture
def deep_lineage(hermes_home, monkeypatch):
    """Build a configurable chain of snapshot ancestors + a continuation child."""
    import api.models as models
    import api.routes as routes
    from api.models import Session

    monkeypatch.setattr(routes, "SESSION_DIR", hermes_home / "sessions")
    monkeypatch.setattr(models, "SESSION_DIR", hermes_home / "sessions")
    routes._lineage_display_cache.clear()

    def build(hops, sentinel, *, ancestor_rows=40, child_rows=6):
        """``sentinel`` is either a bool applied to every ancestor, or a set of
        depths (0 = oldest root) that carry the truncate-to-empty sentinel."""
        previous = None
        for depth in range(hops):
            sid = f"anc_{depth}"
            ancestor = Session(
                session_id=sid,
                title=f"ancestor {depth}",
                messages=_turns(ancestor_rows, 1000 + depth * 1000, f"anc{depth}"),
            )
            ancestor.pre_compression_snapshot = True
            has_sentinel = (
                depth in sentinel if isinstance(sentinel, set) else bool(sentinel)
            )
            if has_sentinel:
                # The truncate-to-empty sentinel: falsy, so a naive `if wm:`
                # check misses it, yet it blocks all replay in the merge.
                ancestor.truncation_watermark = 0.0
                ancestor.truncation_boundary = 0.0
            if previous is not None:
                ancestor.parent_session_id = previous
            ancestor.save()
            previous = sid

        child = Session(
            session_id="continuation",
            title="child",
            messages=_turns(child_rows, 90000, "child"),
        )
        child.parent_session_id = previous
        child.save()
        return routes, Session, child

    return build


def test_sentinel_ancestors_are_never_loaded(deep_lineage, monkeypatch):
    """RED: ancestors neutralised by the sentinel must not be read from disk."""
    routes, Session, child = deep_lineage(hops=9, sentinel=True)

    loaded: list[str] = []
    real_load = Session.load

    def counting_load(sid, *args, **kwargs):
        loaded.append(str(sid))
        return real_load(sid, *args, **kwargs)

    monkeypatch.setattr(routes.Session, "load", staticmethod(counting_load))

    out = routes._webui_sidecar_lineage_messages_for_display(child)

    ancestors_loaded = [s for s in loaded if s.startswith("anc_")]
    assert ancestors_loaded == [], (
        "ancestors whose watermark is the truncate-to-empty sentinel contribute "
        f"zero visible rows, so loading them is pure cost; loaded={ancestors_loaded}"
    )
    assert len(out) == len(child.messages)


def test_output_is_identical_to_the_full_walk(deep_lineage):
    """The skip must be output-identical, not merely faster."""
    from api.models import _session_message_visible_key

    routes, Session, child = deep_lineage(hops=6, sentinel=True)

    fast = routes._webui_sidecar_lineage_messages_for_display(child)

    # Reference: what the unoptimised walk produced — the child rows alone,
    # because every ancestor merge resets the accumulator to [].
    reference = list(child.messages)

    assert [_session_message_visible_key(m) for m in fast] == [
        _session_message_visible_key(m) for m in reference
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("active_stream_id", "stream-live"),
        ("pending_user_message", {"content": "queued"}),
    ],
)
def test_active_session_skips_the_walk_too(deep_lineage, monkeypatch, field, value):
    """Trou A: active sessions are never cached, so they re-pay on every poll."""
    routes, Session, child = deep_lineage(hops=9, sentinel=True)
    setattr(child, field, value)

    loaded: list[str] = []
    real_load = Session.load

    def counting_load(sid, *args, **kwargs):
        loaded.append(str(sid))
        return real_load(sid, *args, **kwargs)

    monkeypatch.setattr(routes.Session, "load", staticmethod(counting_load))

    routes._webui_sidecar_lineage_messages_for_display(child)

    ancestors_loaded = [s for s in loaded if s.startswith("anc_")]
    assert ancestors_loaded == [], (
        "an ACTIVE session is never cached, so the useless walk is re-paid on "
        f"every poll — this is the only recurring cost; loaded={ancestors_loaded}"
    )


def test_genuine_snapshot_history_is_still_stitched(deep_lineage):
    """Guard against over-optimisation: real ancestor history must survive."""
    routes, Session, child = deep_lineage(hops=2, sentinel=False)

    out = routes._webui_sidecar_lineage_messages_for_display(child)

    assert len(out) > len(child.messages), (
        "ancestors without the sentinel genuinely contribute visible history "
        "and must still be stitched"
    )


def test_shortcut_still_dedupes_the_child_rows(deep_lineage, monkeypatch):
    """Regression: the shortcut must not hand back raw, undeduped sidecar rows.

    The unoptimised path always ran the child through the final append-only
    merge, which drops rows already covered by the (empty) stitched prefix.
    A first version of this fix returned ``session.messages`` verbatim and
    inflated a real lineage from 6564 to 6747 visible rows. The shortcut must
    return exactly what the full walk returns — no more, no less.
    """
    routes, Session, child = deep_lineage(hops=9, sentinel=True)

    # A duplicated row is what the final merge is there to collapse.
    child.messages = list(child.messages) + [dict(child.messages[-1])]

    shortcut = routes._webui_sidecar_lineage_messages_for_display(child)

    # Reference: what the unoptimised implementation would have produced.
    reference = routes.merge_session_messages_append_only(
        [],
        list(child.messages),
        truncation_watermark=None,
    )

    assert shortcut == reference, (
        "the shortcut must reproduce the final merge exactly; returning raw "
        f"rows changes the visible output ({len(shortcut)} vs {len(reference)})"
    )


def test_shortcut_matches_full_walk_at_explicit_fork_boundary(
    deep_lineage, monkeypatch
):
    """A fork from a non-fork snapshot keeps its raw, isolated child rows."""
    import json

    routes, Session, child = deep_lineage(hops=1, sentinel=True)
    child.session_source = "fork"
    child.messages = list(child.messages) + [dict(child.messages[-1])]

    shortcut = routes._webui_sidecar_lineage_messages_for_display(child)
    routes._lineage_display_cache.clear()
    monkeypatch.setattr(routes, "_snapshot_parent_replays_nothing", lambda meta: False)
    reference = routes._webui_sidecar_lineage_messages_for_display(child)

    assert json.dumps(shortcut, sort_keys=True) == json.dumps(reference, sort_keys=True)
    assert reference == child.messages
    assert len(reference) == 7
    assert _contents(reference) == [f"child-{i}" for i in range(6)] + ["child-5"]
    assert not any(content.startswith("anc") for content in _contents(reference))


def test_shortcut_matches_full_walk_for_cumulative_parent_prefix(
    deep_lineage, monkeypatch
):
    """A cumulative child keeps its raw rows when it contains the parent prefix."""
    import json

    routes, Session, child = deep_lineage(
        hops=1, sentinel=True, ancestor_rows=2, child_rows=1
    )
    parent = Session.load("anc_0")
    own_row = dict(child.messages[0])
    child.messages = list(parent.messages) + [own_row, dict(own_row)]

    shortcut = routes._webui_sidecar_lineage_messages_for_display(child)
    routes._lineage_display_cache.clear()
    monkeypatch.setattr(routes, "_snapshot_parent_replays_nothing", lambda meta: False)
    reference = routes._webui_sidecar_lineage_messages_for_display(child)

    assert json.dumps(shortcut, sort_keys=True) == json.dumps(reference, sort_keys=True)
    assert reference == child.messages
    assert len(reference) == 4
    assert _contents(reference) == ["anc0-0", "anc0-1", "child-0", "child-0"]


def test_mixed_chain_with_contributing_segments_before_the_sentinel(
    deep_lineage, monkeypatch
):
    """Mixed lineage: child -> contributing parents -> neutralising snapshot.

    Shape: ``continuation -> anc_3 -> anc_2 -> anc_1(sentinel) -> anc_0(sentinel)``.
    ``anc_3`` and ``anc_2`` are genuine snapshots that replay history, so
    ``segments`` is already non-empty when the walk meets the sentinel on
    ``anc_1``. This pins the non-empty ``segments`` path:

    - output byte-identical to the unoptimised walk (shortcut disabled, and an
      explicit replay of the pre-fix merge loop);
    - ``anc_3``/``anc_2`` are the ONLY full loads — the sentinel and everything
      above it are never read;
    - nothing is inserted in the lineage display cache.
    """
    import json

    routes, Session, child = deep_lineage(hops=4, sentinel={0, 1})
    sid = child.session_id

    loaded: list[str] = []
    metadata_loaded: list[str] = []
    real_load = Session.load
    real_load_metadata_only = Session.load_metadata_only

    def counting_load(s, *args, **kwargs):
        loaded.append(str(s))
        return real_load(s, *args, **kwargs)

    def counting_load_metadata_only(s, *args, **kwargs):
        metadata_loaded.append(str(s))
        return real_load_metadata_only(s, *args, **kwargs)

    monkeypatch.setattr(routes.Session, "load", staticmethod(counting_load))
    monkeypatch.setattr(
        routes.Session, "load_metadata_only", staticmethod(counting_load_metadata_only)
    )

    fast = routes._webui_sidecar_lineage_messages_for_display(child)

    # The contributing parents are loaded, in walk order; the sentinel and its
    # own ancestry are not. The metadata-only proof walk DOES peek at anc_0's
    # cheap stub (to prove the fold above the sentinel stays empty) but never
    # parses its messages array.
    assert [s for s in loaded if s.startswith("anc_")] == ["anc_3", "anc_2"], loaded
    assert "anc_0" in metadata_loaded, metadata_loaded
    # Ancestry was deliberately left unverified above the sentinel, so the
    # result must not have entered the cache.
    assert sid not in routes._lineage_display_cache
    # Sanity: the contributing history is actually present in the output.
    assert len(fast) > len(child.messages)
    assert any(str(m.get("content", "")).startswith("anc2-") for m in fast)
    assert any(str(m.get("content", "")).startswith("anc3-") for m in fast)

    # Reference 1: the same walk with the shortcut disabled — every ancestor
    # is loaded and merged, exactly as before this change.
    routes._lineage_display_cache.clear()
    loaded.clear()
    monkeypatch.setattr(routes, "_snapshot_parent_replays_nothing", lambda meta: False)
    reference = routes._webui_sidecar_lineage_messages_for_display(child)
    assert [s for s in loaded if s.startswith("anc_")] == [
        "anc_3",
        "anc_2",
        "anc_1",
        "anc_0",
    ], loaded
    routes._lineage_display_cache.clear()

    # Reference 2: explicit replay of the pre-fix merge loop over the full chain
    # (oldest first, then the child), independent of the walk implementation.
    chain = [real_load(f"anc_{depth}") for depth in (3, 2, 1, 0)]
    replay: list = []
    for segment in reversed(chain):
        replay = routes.merge_session_messages_append_only(
            replay,
            list(segment.messages),
            truncation_watermark=segment.truncation_watermark,
            truncation_boundary=segment.truncation_boundary,
        )
    replay = routes.merge_session_messages_append_only(
        replay, list(child.messages), truncation_watermark=None
    )

    def _bytes(rows):
        return json.dumps(rows, sort_keys=True, ensure_ascii=False).encode("utf-8")

    assert _bytes(fast) == _bytes(reference), (
        f"shortcut output diverges from the unoptimised walk "
        f"({len(fast)} vs {len(reference)} rows)"
    )
    assert _bytes(fast) == _bytes(replay), (
        f"shortcut output diverges from the explicit pre-fix merge replay "
        f"({len(fast)} vs {len(replay)} rows)"
    )


# ── Gate regression: contributing ancestor ABOVE the zero-watermark snapshot ──
#
# ``merge_session_messages_append_only`` returns ``[]`` on the zero watermark
# ONLY when the accumulated prefix is still empty. The full walk folds
# oldest-first, so an older CONTRIBUTING snapshot (watermark=None) sitting
# above a zero-watermark snapshot fills the accumulator before the sentinel is
# reached; the sentinel then takes the general merge path and the archived
# history survives. A shortcut that breaks on the first sentinel stub without
# proving the older fold is empty drops that history (6 rows instead of 86).


def _full_walk_reference(routes, real_load, child, chain_ids):
    """Explicit replay of the pre-optimisation merge loop, oldest first."""
    replay: list = []
    for sid in chain_ids:
        segment = real_load(sid)
        replay = routes.merge_session_messages_append_only(
            replay,
            list(segment.messages),
            truncation_watermark=segment.truncation_watermark,
            truncation_boundary=segment.truncation_boundary,
        )
    return routes.merge_session_messages_append_only(
        replay, list(child.messages), truncation_watermark=None
    )


def _contents(rows):
    return [str(m.get("content", "")) for m in rows]


def test_contributing_ancestor_above_zero_watermark_snapshot_is_kept(
    deep_lineage, monkeypatch
):
    """Inverse ordering: ``continuation -> anc_1(sentinel) -> anc_0(contributing)``.

    Exact row identity and order must equal the full walk; the sentinel and
    the contributing root are BOTH fully loaded (the shortcut must decline).
    """
    routes, Session, child = deep_lineage(hops=2, sentinel={1})

    loaded: list[str] = []
    real_load = Session.load

    def counting_load(s, *args, **kwargs):
        loaded.append(str(s))
        return real_load(s, *args, **kwargs)

    monkeypatch.setattr(routes.Session, "load", staticmethod(counting_load))

    out = routes._webui_sidecar_lineage_messages_for_display(child)

    expected = _full_walk_reference(routes, real_load, child, ["anc_0", "anc_1"])
    # Absolute expectation, independent of both implementations: 40 oldest
    # rows, then the 40 sentinel rows, then the 6 child rows.
    assert _contents(expected) == (
        [f"anc0-{i}" for i in range(40)]
        + [f"anc1-{i}" for i in range(40)]
        + [f"child-{i}" for i in range(6)]
    )
    assert _contents(out) == _contents(expected), (
        f"older contributing history dropped: {len(out)} vs {len(expected)} rows"
    )
    assert [s for s in loaded if s.startswith("anc_")] == ["anc_1", "anc_0"], loaded


def test_zero_watermark_snapshot_with_contributing_root_is_not_skipped_deeper(
    deep_lineage, monkeypatch
):
    """``continuation -> anc_2(sentinel) -> anc_1(sentinel) -> anc_0(contributing)``.

    The proof walk must climb PAST intermediate sentinel stubs and still refuse
    the shortcut when the root contributes. Row identity/order pinned to the
    explicit pre-fix replay.
    """
    routes, Session, child = deep_lineage(hops=3, sentinel={1, 2})

    loaded: list[str] = []
    real_load = Session.load

    def counting_load(s, *args, **kwargs):
        loaded.append(str(s))
        return real_load(s, *args, **kwargs)

    monkeypatch.setattr(routes.Session, "load", staticmethod(counting_load))

    out = routes._webui_sidecar_lineage_messages_for_display(child)

    expected = _full_walk_reference(
        routes, real_load, child, ["anc_0", "anc_1", "anc_2"]
    )
    assert _contents(out) == _contents(expected)
    assert _contents(out)[:1] == ["anc0-0"]
    assert _contents(out)[-1] == "child-5"
    assert [s for s in loaded if s.startswith("anc_")] == [
        "anc_2",
        "anc_1",
        "anc_0",
    ], loaded


def test_all_sentinel_chain_still_skips_every_ancestor_load(deep_lineage, monkeypatch):
    """Performance control for the corrected fast path.

    With the fix, the shortcut is only taken after the metadata-only proof
    that every older ancestor is itself a sentinel. On the all-sentinel chain
    (the production shape that motivated the change) that proof succeeds, and
    the walk must still perform ZERO full ancestor loads — only cheap metadata
    stubs, each read at most once.
    """
    routes, Session, child = deep_lineage(hops=9, sentinel=True)

    loaded: list[str] = []
    metadata_loaded: list[str] = []
    real_load = Session.load
    real_load_metadata_only = Session.load_metadata_only

    def counting_load(s, *args, **kwargs):
        loaded.append(str(s))
        return real_load(s, *args, **kwargs)

    def counting_load_metadata_only(s, *args, **kwargs):
        metadata_loaded.append(str(s))
        return real_load_metadata_only(s, *args, **kwargs)

    monkeypatch.setattr(routes.Session, "load", staticmethod(counting_load))
    monkeypatch.setattr(
        routes.Session, "load_metadata_only", staticmethod(counting_load_metadata_only)
    )

    out = routes._webui_sidecar_lineage_messages_for_display(child)

    assert [s for s in loaded if s.startswith("anc_")] == [], loaded
    stubs = [s for s in metadata_loaded if s.startswith("anc_")]
    assert sorted(stubs) == [f"anc_{d}" for d in range(9)], stubs
    assert len(stubs) == len(set(stubs)), f"stub re-read within one walk: {stubs}"
    assert _contents(out) == [f"child-{i}" for i in range(6)]


def test_paginated_session_route_keeps_history_above_zero_watermark_snapshot(
    deep_lineage, monkeypatch
):
    """Drive the real ``GET /api/session?...&msg_limit=30`` handler.

    Lineage: ``continuation -> anc_1(sentinel, watermark=0) -> anc_0(contributing)``
    with 2 rows per ancestor and 2 child rows, so the whole stitched transcript
    (6 visible rows) fits inside the 30-row window and the absolute payload is
    fully determined: every row, in order, offset 0, not truncated.
    """
    import json
    from io import BytesIO
    from urllib.parse import urlparse

    import api.config as config
    import api.models as models

    routes, Session, child = deep_lineage(
        hops=2, sentinel={1}, ancestor_rows=2, child_rows=2
    )
    session_dir = routes.SESSION_DIR
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(
        config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False
    )
    monkeypatch.setattr(
        models, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False
    )
    # Cold cache: the route must resolve the child from disk in the isolated
    # session dir, then stitch the lineage itself.
    monkeypatch.setattr(models, "SESSIONS", type(models.SESSIONS)(), raising=False)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS, raising=False)
    routes._lineage_display_cache.clear()
    routes._display_merge_cache.clear()

    class _Handler:
        def __init__(self, path):
            self.path = path
            self.headers = {}
            self.client_address = ("127.0.0.1", 12345)
            self.status = None
            self.wfile = BytesIO()

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            pass

        def end_headers(self):
            pass

        def log_message(self, *args, **kwargs):
            pass

    handler = _Handler(
        f"/api/session?session_id={child.session_id}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200, handler.wfile.getvalue()[:300]
    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))["session"]

    # Absolute window: oldest contributing rows, then the sentinel snapshot's
    # own rows, then the continuation — nothing dropped, nothing reordered.
    assert _contents(payload["messages"]) == [
        "anc0-0",
        "anc0-1",
        "anc1-0",
        "anc1-1",
        "child-0",
        "child-1",
    ]
    assert [m["role"] for m in payload["messages"]] == [
        "user",
        "assistant",
    ] * 3
    assert payload["_messages_offset"] == 0
    assert payload["_messages_truncated"] is False
    assert payload["message_count"] == 6
