"""Server-side append-only fast path unit checks (#7925 re-gate)."""
from __future__ import annotations

import json

from api.helpers import _public_message_projection
from api.routes import (
    _append_anchor_fingerprint,
    _append_fast_path_anchor,
    _display_prefix_fingerprint,
    _message_window_for_display,
)


def _make_rows(n: int) -> list:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}", "timestamp": 1700000000 + i}
        for i in range(n)
    ]


class _FakeSession:
    def __init__(
        self,
        generation: int = 0,
        *,
        tool_calls=None,
        anchor_activity_scenes=None,
        active_stream_id=None,
        pending_started_at=None,
    ):
        self.transcript_generation = generation
        self.tool_calls = tool_calls
        self.anchor_activity_scenes = anchor_activity_scenes
        self.active_stream_id = active_stream_id
        self.pending_started_at = pending_started_at


def test_msg_after_window_returns_rows_strictly_after_anchor():
    rows = _make_rows(2001)
    window, offset = _message_window_for_display(rows, msg_limit=500, msg_after=1999)
    assert len(window) == 1
    assert offset == 2000
    assert window[0]["content"] == "m2000"


def test_msg_after_window_empty_at_end_of_transcript():
    rows = _make_rows(601)
    window, offset = _message_window_for_display(rows, msg_limit=500, msg_after=600)
    assert window == []
    # offset = len(messages) means "nothing after the anchor"; the handler's
    # append-mode truncation check (offset + len < total) is therefore False.
    assert offset == len(rows)


def test_msg_after_window_caps_at_limit_and_reports_more():
    rows = _make_rows(2003)
    window, offset = _message_window_for_display(rows, msg_limit=500, msg_after=500)
    assert len(window) == 500
    assert offset == 501
    assert offset + len(window) < len(rows)  # more rows exist after the cap


def test_msg_after_window_keeps_anchor_trailing_tool_rows():
    """#7925 re-gate (2026-10-19): the window is the CONTIGUOUS suffix after the
    anchor, so a tool-result row that landed after the anchor was captured is
    delivered instead of skipped.

    The previous shape scanned forward for the first renderable row and started
    there, assuming everything between the anchor and that row was the anchor's
    own already-rendered output. That assumption breaks when the client captured
    the anchor before its results were saved (a tab loading mid-turn, an external
    or CLI run, a hidden tab): the tool row was never sent, the client's
    lastRenderedIdx went off by one, and the next reload's anchor check failed
    into a full fetch. The tool row now travels with the window.
    """
    rows = _make_rows(40)
    # Anchor is an assistant row whose tool result row follows it.
    rows[10] = {"role": "assistant", "content": "call", "timestamp": 1, "tool_calls": [{"id": "t1"}]}
    rows[11] = {"role": "tool", "tool_call_id": "t1", "content": "result", "timestamp": 2}
    window, offset = _message_window_for_display(rows, msg_limit=500, msg_after=10)
    # The window starts strictly after the anchor and is contiguous from there.
    assert offset == 11
    assert window[0]["content"] == "result", window[:2]
    assert window[1]["content"] == "m12", window[:2]


def test_prefix_fingerprint_refuses_a_nondestructive_earlier_row_edit():
    """2026-10-27 review, finding 4 (design directive): the append contract
    only transported rows after the anchor and every DESTRUCTIVE rewrite was
    already covered by the generation bump. The remaining hole was a
    NON-destructive server-side edit of an earlier row (settled-reasoning
    backfill, saved attachments) that bumps no generation: the anchor row is
    untouched, both existing checks pass, and the client stitches a stale
    prefix onto the fresh tail. The server-computed prefix fingerprint closes
    it — a digest over the render-relevant fields of rows [0, msg_after+1),
    minted on every load and re-checked on every append.
    """
    rows = _make_rows(2001)
    # The client's view: rows [0, 2000) — its anchor is row 1999, so the
    # digest it holds covers exactly [0, msg_after + 1) = [0, 2000).
    held_fp = _display_prefix_fingerprint(rows, 2000)
    assert held_fp and held_fp != _display_prefix_fingerprint(rows, 1999)

    # A non-destructive edit lands on row 500: NO generation bump, and the
    # anchor row (1999) is byte-identical, so both pre-existing checks pass.
    edited = [dict(r) for r in rows]
    edited[500]["reasoning"] = "backfilled reasoning text"
    s = _FakeSession(generation=0)
    anchor_fp = _append_anchor_fingerprint(
        _public_message_projection(edited[1999], _enabled=True)
    )
    assert _append_fast_path_anchor(s, edited, "1999", anchor_fp, "0", held_fp) is None

    # Control: the same digest over the unchanged document is accepted.
    assert _append_fast_path_anchor(s, rows, "1999", anchor_fp, "0", held_fp) == 1999
    # A missing digest fails closed: an older client loses only the fast path
    # and takes the authoritative full fetch — master's behaviour.
    assert _append_fast_path_anchor(s, rows, "1999", anchor_fp, "0", None) is None
    # The digest covers every field the projection delivers (whole row minus
    # the explicit exclusion set — 2026-10-29 finding 2): an excluded field
    # (stripped before the browser sees the row) is not a rendered difference.
    internal = [dict(r) for r in rows]
    internal[500]["_row_id"] = "state-db-row-123"
    assert _append_fast_path_anchor(s, internal, "1999", anchor_fp, "0", held_fp) == 1999
    # A field the projection DELIVERS — even an unknown one — is covered by
    # default: a new rendered field cannot be missed again.
    delivered = [dict(r) for r in rows]
    delivered[500]["_internal_note"] = "not rendered"
    assert (
        _append_fast_path_anchor(s, delivered, "1999", anchor_fp, "0", held_fp)
        is None
    )
    # An attachment edit is render-relevant and must refuse.
    attachments = [dict(r) for r in rows]
    attachments[500]["attachments"] = [{"id": "a1", "name": "saved.png"}]
    assert (
        _append_fast_path_anchor(s, attachments, "1999", anchor_fp, "0", held_fp)
        is None
    )


def test_prefix_fingerprint_digests_lone_surrogate_rows():
    """2026-10-28 CI re-gate: a crashed stream can persist a lone surrogate
    (half a pair split mid-token — see tests/test_recovered_surrogate_http.py).
    The strict utf-8 encode in the digest raised UnicodeEncodeError, so every
    GET /api/session on such a session answered 500. The digest must stay total
    over any persisted content: it encodes with surrogatepass, stays
    deterministic, and still discriminates content.
    """
    rows = _make_rows(50)
    rows[10]["content"] = "prefix\ud83d"  # lone high surrogate
    rows[11]["content"] = "prefix\udc00"  # lone low surrogate
    fp = _display_prefix_fingerprint(rows, 50)
    assert fp and fp == _display_prefix_fingerprint(rows, 50)  # deterministic
    assert fp != _display_prefix_fingerprint(rows, 49)  # still discriminates
    edited = [dict(r) for r in rows]
    edited[10]["content"] = "prefix\ude42"
    assert _display_prefix_fingerprint(edited, 50) != fp


def test_append_fast_path_anchor_validates_generation_fingerprint_and_range():
    rows = _make_rows(2001)
    projected = _public_message_projection(rows[1999], _enabled=True)
    fp = _append_anchor_fingerprint(projected)
    assert fp and len(fp) == 26
    # The client holds rows [0, 2000): its prefix digest covers [0, 2000).
    pfp = _display_prefix_fingerprint(rows, 2000)
    s = _FakeSession(generation=0)
    assert _append_fast_path_anchor(s, rows, "1999", fp, "0", pfp) == 1999
    # generation changed → None (fall back to the authoritative full fetch)
    assert _append_fast_path_anchor(s, rows, "1999", fp, "1", pfp) is None
    # anchor fingerprint mismatch → None
    assert _append_fast_path_anchor(s, rows, "1999", "x" * 26, "0", pfp) is None
    # anchor out of range → None
    assert _append_fast_path_anchor(s, rows, "999999", fp, "0", pfp) is None
    # unparsable index → None
    assert _append_fast_path_anchor(s, rows, "abc", fp, "0", pfp) is None
    # missing fingerprint → None
    assert _append_fast_path_anchor(s, rows, "1999", None, "0", pfp) is None
    # missing prefix digest → None (fail closed; an older client loses only
    # the fast path and takes master's authoritative full fetch)
    assert _append_fast_path_anchor(s, rows, "1999", fp, "0", None) is None
    # prefix digest over the wrong coverage → None
    assert _append_fast_path_anchor(s, rows, "1999", fp, "0", _display_prefix_fingerprint(rows, 1999)) is None


def test_append_fast_path_anchor_detects_rewritten_seam_row():
    rows = _make_rows(2001)
    rewritten = [dict(r) for r in rows]
    rewritten[1999]["content"] = "m1999-edited"
    projected = _public_message_projection(rewritten[1999], _enabled=True)
    fp = _append_anchor_fingerprint(projected)
    s = _FakeSession(generation=0)
    # The client would fingerprint its OLD row; the server's row now differs.
    client_fp = _append_anchor_fingerprint(
        _public_message_projection(rows[1999], _enabled=True)
    )
    client_pfp = _display_prefix_fingerprint(rows, 2000)
    assert client_fp != fp
    assert _append_fast_path_anchor(s, rewritten, "1999", client_fp, "0", client_pfp) is None


def test_msg_after_window_delivers_a_lone_tool_result_after_the_anchor():
    """#7925 re-gate (2026-10-19), the permanent-loss case.

    The client holds 601 rows whose last one is an assistant row carrying
    ``tool_calls``; the server then saves ONLY the tool result row for that
    call. Master's window scanned forward for the first renderable row, found
    none in the suffix, and returned nothing — so the client kept 601 rows
    while the server had 602, the counts matched, and the tool output never
    appeared even after repeated reloads.

    The contiguous-suffix window now returns that lone tool row (it matches a
    call the anchor made), so the client's count moves to 602 and the output
    shows up.
    """
    rows = _make_rows(601)
    # The anchor is the last row the client rendered: an assistant tool call.
    rows[600] = {
        "role": "assistant",
        "content": "calling",
        "timestamp": 1,
        "tool_calls": [{"id": "t1"}],
    }
    # The server then persists only the tool result for that call.
    rows.append({"role": "tool", "tool_call_id": "t1", "content": "the output", "timestamp": 2})

    window, offset = _message_window_for_display(rows, msg_limit=500, msg_after=600)

    assert offset == 601, offset
    assert [r["content"] for r in window] == ["the output"], window
    # The client can now advance its lastRenderedIdx to the tool row, so the
    # next reload's anchor check succeeds instead of failing into a full fetch.
    assert offset + len(window) == len(rows)


# ── Handler-level: continuation collapse (2026-10-21 re-gate, finding 1) ──
# A WebUI continuation session renders as ONE conversation: the parent's rows
# followed by the child's. The client holds that merged transcript and, on a
# refresh wider than the window ceiling, asks for only the rows after its last
# rendered index. When the append is refused the response MUST be the
# authoritative full transcript — for a continuation that is the merged
# parent+child document — never the child's rows alone, or the client replaces
# its 601 rendered parent rows with the child's single row.

import types


def _continuation_handler_harness(monkeypatch, tmp_path, parent_rows, child_rows):
    import api.config
    import api.models as models
    import api.session_ops
    from api import routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    parent = models.Session(
        session_id="cont-parent",
        workspace="/fixture",
        model="fixture-model",
        context_length=128_000,
        messages=[dict(r) for r in parent_rows],
        source_tag="webui",
        session_source="webui",
    )
    parent.save(skip_index=True)
    child = models.Session(
        session_id="cont-child",
        workspace="/fixture",
        model="fixture-model",
        context_length=128_000,
        messages=[dict(r) for r in child_rows],
        parent_session_id="cont-parent",
        source_tag="webui",
        session_source="webui",
    )
    child.save(skip_index=True)
    with models.LOCK:
        models.SESSIONS.pop("cont-parent", None)
        models.SESSIONS.pop("cont-child", None)

    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *a, **k: True)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *a, **k: {})
    monkeypatch.setattr(routes, "find_run_summary", lambda *a, **k: None)
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": False})
    monkeypatch.setattr(api.session_ops, "regeneration_state", lambda _s: ([], []))
    monkeypatch.setattr(api.session_ops, "regeneration_authority", lambda *_a, **_k: None)
    captured = {}
    monkeypatch.setattr(
        routes,
        "j",
        lambda _h, payload, status=200, **_k: (
            captured.__setitem__("payload", payload),
            captured.__setitem__("status", status),
            payload,
        )[-1],
    )
    return routes, captured


def test_continuation_append_refusal_returns_the_merged_transcript(monkeypatch, tmp_path):
    """2026-10-21 re-gate finding 1: a refused append on a continuation must
    not collapse the client to the child's rows."""
    from api.helpers import _public_message_projection
    from api.routes import _append_anchor_fingerprint

    parent_rows = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}", "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    child_rows = [{"role": "user", "content": "child question", "timestamp": 1700000601}]

    routes, captured = _continuation_handler_harness(
        monkeypatch, tmp_path, parent_rows, child_rows
    )

    # The client rendered all 601 parent rows; its anchor is the last one and
    # its prefix digest covers [0, 601) of the merged document.
    from api.routes import _display_prefix_fingerprint

    anchor_row = _public_message_projection(parent_rows[600], _enabled=False)
    anchor_fp = _append_anchor_fingerprint(anchor_row)
    prefix_fp = _display_prefix_fingerprint(parent_rows, 601)
    query = (
        "session_id=cont-child&messages=1&resolve_model=0"
        "&msg_after=600"
        f"&anchor_fp={anchor_fp}"
        f"&prefix_fp={prefix_fp}"
        "&transcript_generation=0"
        "&msg_limit=500&expand_renderable=1"
    )
    routes._handle_session_get(
        None, types.SimpleNamespace(path="/api/session", query=query)
    )

    assert captured["status"] == 200
    session = captured["payload"]["session"]
    messages = session.get("messages") or []
    append_only = session.get("_append_only")
    # The full display document (lineage-merged parent+child) makes the anchor
    # valid, so the fast path is ACCEPTED: exactly the child's new row travels,
    # flagged _append_only, and the client appends it onto its 601 rows.
    assert append_only is True, (
        "the append was refused — with the merged document available the "
        "anchor must validate; a refusal here means the response would be "
        "answered from a lineage-less document again"
    )
    assert [m.get("content") for m in messages] == ["child question"], messages
    assert session.get("_messages_offset") == 601, session.get("_messages_offset")


# ── Handler-level: warm display cache must not starve the append merge ──
# 2026-10-27 review (CORE): the finding-1 fix routes append requests down
# the full display merge branch, but the state.db loader above it still
# keyed on `msg_limit is not None` while the append request carries
# `msg_limit=500` (it bounds the RETURNED window only). On a display-cache
# hit the loader set `state_db_messages = []`, so the full branch merged
# the sidecar with nothing: a 601-sidecar + 602-state.db session answered
# a focus reload with 601 rows and a reply already on screen disappeared
# (master keeps 602; cold cache gave 602 on both). The append must see the
# full merged document exactly like master's bare GET.


def _warm_cache_append_harness(monkeypatch, tmp_path, sidecar_rows, state_db_rows):
    """601-ish sidecar rows + one state.db-only reply, cache pre-warmed."""
    from tests.test_webui_state_db_reconciliation import _make_state_db

    routes, captured = _continuation_handler_harness(
        monkeypatch, tmp_path, [], sidecar_rows
    )
    _make_state_db(tmp_path / "state.db", "cont-child", state_db_rows)
    routes._display_merge_cache.clear()
    return routes, captured


def _warm_display_cache(routes):
    """The real initial `msg_limit=30` load: populates the merge cache."""
    import types

    routes._handle_session_get(
        None,
        types.SimpleNamespace(
            path="/api/session",
            query="session_id=cont-child&messages=1&resolve_model=0&msg_limit=30&expand_renderable=1",
        ),
    )


def _append_get(routes, *, msg_after, anchor_row, prefix_fp=None, generation=0):
    import types

    from api.helpers import _public_message_projection
    from api.routes import _append_anchor_fingerprint

    anchor_fp = _append_anchor_fingerprint(
        _public_message_projection(anchor_row, _enabled=False)
    )
    captured = {}
    routes.j = lambda _h, payload, status=200, **_k: (
        captured.__setitem__("payload", payload),
        captured.__setitem__("status", status),
        payload,
    )[-1]
    query = (
        "session_id=cont-child&messages=1&resolve_model=0"
        f"&msg_after={msg_after}"
        f"&anchor_fp={anchor_fp}"
        f"&transcript_generation={generation}"
        "&msg_limit=500&expand_renderable=1"
    )
    if prefix_fp is not None:
        query += f"&prefix_fp={prefix_fp}"
    routes._handle_session_get(
        None, types.SimpleNamespace(path="/api/session", query=query)
    )
    return captured["payload"]["session"]


def _state_db_only_reply():
    return {
        "role": "assistant",
        "content": "state-db-only reply",
        "timestamp": 1700000900,
    }


def test_warm_cache_append_with_no_new_rows_keeps_every_row(monkeypatch, tmp_path):
    """Maintainer's exact repro: the client holds all 602 rows (its anchor is
    the state.db-only reply); a warm-cache focus reload must not answer with
    the 601-row sidecar-only document and drop the reply on screen."""
    sidecar = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    state_db = sidecar + [_state_db_only_reply()]

    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)
    _warm_display_cache(routes)
    # Premise: the cache really is warm for this session (the probe would hit).
    import api.models as models

    probe = routes._display_merge_cached_messages(models.get_session("cont-child"), None)
    assert probe is not None and len(probe) == 602, (
        "cache did not warm — the repro needs a live cache entry"
    )

    # The client's held digest comes from the load that established its view
    # (the warm msg_limit=30 response covers [0, 602)). It cannot be
    # recomputed from the raw fixture rows: the full-row digest (2026-10-29
    # finding 2) also covers timestamps, and state.db-origin rows carry REAL
    # timestamps (floats) where the fixture has ints.
    held_fp = captured["payload"]["session"]["prefix_fp"]
    session = _append_get(
        routes,
        msg_after=601,
        anchor_row=_state_db_only_reply(),
        prefix_fp=held_fp,
    )
    assert session.get("message_count") == 602, (
        "warm-cache append answered with the sidecar-only document — a reply "
        "already on screen would be dropped (maintainer's CORE finding)"
    )
    assert session.get("_append_only") is True, session.get("_append_only")
    assert session.get("messages") == [], session.get("messages")
    assert session.get("_messages_offset") == 602, session.get("_messages_offset")


def test_warm_cache_append_with_one_new_row_fetches_it(monkeypatch, tmp_path):
    """The second warm-cache shape: the client holds the 601 sidecar rows and
    the state.db-only reply must travel with the append window instead of
    being silently starved by the cache-hit empty load."""
    sidecar = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    state_db = sidecar + [_state_db_only_reply()]

    routes, _ = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)
    _warm_display_cache(routes)

    session = _append_get(
        routes,
        msg_after=600,
        anchor_row=sidecar[600],
        prefix_fp=_display_prefix_fingerprint(sidecar, 601),
    )
    assert session.get("_append_only") is True, session.get("_append_only")
    messages = session.get("messages") or []
    assert [m.get("content") for m in messages] == ["state-db-only reply"], messages
    assert session.get("message_count") == 602, session.get("message_count")


def test_append_window_clipped_tool_diff_signals_truncation(monkeypatch, tmp_path):
    """#7925 re-gate finding 5 (2026-10-21): a large tool diff inside the
    append window is clipped to a preview by the bounded-payload limiter.
    The row then arrives with ``_content_truncated`` but the append-mode
    truncation flag stayed False, so the client kept the clipped diff with no
    signal that content was missing. The flag now folds the clip in, which
    sends the client to the authoritative full fetch (msg_limit=all skips the
    clipper) — master's shape, with the complete diff."""
    from api.routes import _append_anchor_fingerprint
    from api.helpers import _public_message_projection

    rows = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}", "timestamp": 1700000000 + i}
        for i in range(600)
    ]
    anchor = {"role": "assistant", "content": "calling", "timestamp": 1700000600,
              "tool_calls": [{"id": "t1"}]}
    big_tool = {"role": "tool", "tool_call_id": "t1", "content": "x" * 5000,
                "timestamp": 1700000601}
    rows.append(anchor)
    rows.append(big_tool)

    routes, captured = _continuation_handler_harness(monkeypatch, tmp_path, rows, [])
    # No continuation here: the child session IS the full transcript.
    import api.models as models
    with models.LOCK:
        models.SESSIONS.pop("cont-child", None)
    child = models.Session(
        session_id="cont-child",
        workspace="/fixture",
        model="fixture-model",
        context_length=128_000,
        messages=[dict(r) for r in rows],
        parent_session_id="",
        source_tag="webui",
        session_source="webui",
    )
    child.save(skip_index=True)
    with models.LOCK:
        models.SESSIONS.pop("cont-child", None)

    anchor_fp = _append_anchor_fingerprint(
        _public_message_projection(anchor, _enabled=False)
    )
    from api.routes import _display_prefix_fingerprint

    # The client holds rows [0, 601): its digest covers [0, msg_after + 1).
    prefix_fp = _display_prefix_fingerprint(rows, 601)
    query = (
        "session_id=cont-child&messages=1&resolve_model=0"
        "&msg_after=600"
        f"&anchor_fp={anchor_fp}"
        f"&prefix_fp={prefix_fp}"
        "&transcript_generation=0"
        "&msg_limit=500&expand_renderable=1"
    )
    routes._handle_session_get(
        None, types.SimpleNamespace(path="/api/session", query=query)
    )

    session = captured["payload"]["session"]
    messages = session.get("messages") or []
    assert session.get("_append_only") is True
    assert messages, "the tool row should travel with the window"
    tool_row = messages[-1]
    assert tool_row.get("_content_truncated") is True, (
        "the over-long tool diff was not clipped — fixture too small"
    )
    assert len(tool_row.get("content") or "") < 5000
    # The clip must surface as truncation so the client re-fetches the full
    # document instead of keeping the preview forever.
    assert session.get("_messages_truncated") is True, (
        "a clipped append-window tool diff left _messages_truncated False — "
        "the client would keep the preview with no missing-content signal"
    )


# ── 2026-10-29 re-gate, finding 2: the fingerprint must cover EVERYTHING the
# browser renders ──────────────────────────────────────────────────────────────
# The original whitelist (_PREFIX_FP_FIELDS: role/content/reasoning/
# attachments/tool_calls) missed rows' hydrated anchor-activity scenes and the
# _source stamp: a scene save changed only s.anchor_activity_scenes (records +
# sidecar, never a row) and a _source change altered what the browser renders
# (delegation_wakeup rows are hidden), but neither moved the digest — so the
# next focus reload accepted an empty append and kept the stale view. The
# digest is now INVERTED: each row is hashed as the browser receives it (the
# projected display row), minus an explicit exclusion set (only the fields
# stripped before delivery). These tests are red-before: each one fails on the
# unmodified whitelist digest and passes on the inverted one.

def _scene_record(message_index, final_answer):
    """One persisted anchor-activity-scene record for an assistant row.

    Mirrors the write shape of /api/session/anchor-scene (routes.py
    _handle_session_anchor_scene): the record is stored under
    ``s.anchor_activity_scenes`` and never touches the message rows.
    """
    return {
        "version": "anchor_activity_scene_record_v1",
        "message_index": message_index,
        "message_ref": "",
        "stream_id": "",
        "scene": {
            "mode": "compact_worklog",
            "final_answer": final_answer,
            "activity_rows": [
                {"role": "thinking", "text": "saved reasoning", "order": 0},
            ],
        },
        "updated_at": 1700000001,
    }


def test_prefix_fingerprint_mint_tracks_anchor_scene_saves():
    """2026-10-29 review finding 2: a saved anchor scene renders a worklog on
    the assistant row, but the whitelist digest ignored it (the scene record
    never modifies a row). The mint must change once the record exists.

    RED-before: on the unmodified whitelist digest this errors with
    TypeError (no ``scenes`` parameter exists), because the digest had no
    input channel for scene records at all.
    """
    rows = _make_rows(2001)
    held_fp = _display_prefix_fingerprint(rows, 2000)
    scenes = {"index:1501": _scene_record(1501, "m1501")}
    fresh_fp = _display_prefix_fingerprint(rows, 2000, scenes=scenes, tool_calls=None)
    assert fresh_fp != held_fp, (
        "a saved anchor-activity scene changed no row, so the whitelist digest "
        "was blind to it — the next focus reload accepted an empty append and "
        "kept the worklog absent (maintainer's exact repro)"
    )


def test_append_verify_refuses_after_anchor_scene_save():
    """2026-10-29 review finding 2, verify side: the client's view was
    established BEFORE the scene save, so its echoed prefix_fp predates the
    rendered worklog. The append must be refused (authoritative full fetch
    carries the worklog), not accepted as an empty append.

    RED-before: the unmodified verifier ignores s.anchor_activity_scenes and
    accepts the append (returns the anchor index).
    """
    rows = _make_rows(2001)
    held_fp = _display_prefix_fingerprint(rows, 2000)
    s = _FakeSession(generation=0)
    s.anchor_activity_scenes = {"index:1501": _scene_record(1501, "m1501")}
    anchor_fp = _append_anchor_fingerprint(
        _public_message_projection(rows[1999], _enabled=True)
    )
    assert _append_fast_path_anchor(s, rows, "1999", anchor_fp, "0", held_fp) is None, (
        "the client's pre-save digest must refuse the append after a scene "
        "save — the saved scene renders a worklog the client does not have"
    )


def test_prefix_fingerprint_tracks_source_field_changes():
    """2026-10-29 review finding 2: the browser renders rows differently based
    on ``_source`` (ui.js filters delegation_wakeup rows out of the visible
    transcript), but ``_source`` was outside the five-field whitelist. A
    _source change must move the digest.

    RED-before: the whitelist digest is identical before/after, so the first
    assertion fails on unmodified code.
    """
    rows = _make_rows(2001)
    held_fp = _display_prefix_fingerprint(rows, 2000)
    edited = [dict(r) for r in rows]
    edited[500]["_source"] = "delegation_wakeup"
    assert _display_prefix_fingerprint(edited, 2000) != held_fp, (
        "a _source change alters what the browser renders (delegation_wakeup "
        "rows are hidden) but the whitelist digest ignored it"
    )
    s = _FakeSession(generation=0)
    anchor_fp = _append_anchor_fingerprint(
        _public_message_projection(edited[1999], _enabled=True)
    )
    assert _append_fast_path_anchor(s, edited, "1999", anchor_fp, "0", held_fp) is None


def test_prefix_fingerprint_exclusion_set_is_explicit_and_rendered_fields_covered():
    """2026-10-29 review finding 2 (design directive): the digest is inverted —
    whole row minus an EXPLICIT exclusion set of fields known not to render.
    This test enumerates that set (so a future rendered field is covered by
    default rather than missed) and pins the default-coverage property.

    2026-10-31 finding 3 tightened the set: ``_active_turn_token`` is stripped
    before delivery like the rest of the internal fields, but it is an INPUT to
    the delivery (it decides the row's ``_active_turn_user`` marking), so it is
    the one field that must stay INSIDE the digest.

    RED-before: _PREFIX_FP_EXCLUDED_FIELDS does not exist on the unmodified
    whitelist implementation.
    """
    from api.helpers import _PUBLIC_MESSAGE_INTERNAL_FIELDS
    from api.routes import _PREFIX_FP_EXCLUDED_FIELDS

    # The exclusion set is the fields stripped before delivery by
    # _public_message_projection / scrub_internal_replay_fields, MINUS the one
    # field that is an input to the delivery rather than dead weight: anything
    # else on a row reaches the browser and must be covered by default.
    assert frozenset(_PREFIX_FP_EXCLUDED_FIELDS) == frozenset(
        _PUBLIC_MESSAGE_INTERNAL_FIELDS - {"_active_turn_token"}
    )
    for rendered in (
        "role",
        "content",
        "reasoning",
        "attachments",
        "tool_calls",
        "_source",  # rendered: delegation_wakeup rows are hidden/filtered
        "_anchor_activity_scene",  # rendered: the saved worklog
        "_anchor_stream_id",  # travels with the rendered scene
        "timestamp",
        "ts",
        "_ts",
        "tool_call_id",
        # finding 3: stripped before delivery, but it decides the delivered
        # row's active-turn marking, so the digest must cover it.
        "_active_turn_token",
    ):
        assert rendered not in _PREFIX_FP_EXCLUDED_FIELDS, rendered

    rows = _make_rows(50)
    fp = _display_prefix_fingerprint(rows, 50)
    # Default coverage: a NEW unlisted field (as a future rendered field would
    # be) changes the digest without touching the exclusion set.
    extra = [dict(r) for r in rows]
    extra[10]["_future_rendered_field"] = "x"
    assert _display_prefix_fingerprint(extra, 50) != fp
    # An excluded field never churns the digest: it is stripped before the
    # browser sees the row, so it is not a rendered difference.
    internal = [dict(r) for r in rows]
    internal[10]["_row_id"] = "state-db-row-123"
    internal[10]["api_content"] = {"role": "assistant", "content": "raw"}
    assert _display_prefix_fingerprint(internal, 50) == fp


# ── 2026-10-29 should-fix 1: memoise the mint on display-cache hits ──────────
# The mint hashes the WHOLE document on every windowed GET, even when the
# display-merge cache just supplied the identical document (550 ms on the
# 32 MiB shape). The digest now rides on the display-merge cache entry and is
# reused on hits while the scene records / tool calls are unchanged.

def _warm_get(routes):
    import types

    routes._handle_session_get(
        None,
        types.SimpleNamespace(
            path="/api/session",
            query="session_id=cont-child&messages=1&resolve_model=0&msg_limit=30&expand_renderable=1",
        ),
    )


def test_display_cache_hit_reuses_the_minted_prefix_fingerprint(monkeypatch, tmp_path):
    """should-fix 1: a warm display-merge cache must not pay for re-hashing
    the whole document — the response prefix_fp comes from the entry.

    RED-before: on the unmodified code every GET re-mints, so the second
    (warm) GET still calls _display_prefix_fingerprint and this fails.
    """
    sidecar = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    state_db = sidecar + [_state_db_only_reply()]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)

    _warm_get(routes)  # cold: builds the entry and mints the digest
    first_session = captured["payload"]["session"]
    assert first_session.get("prefix_fp")

    calls = []
    orig = routes._display_prefix_fingerprint
    monkeypatch.setattr(
        routes,
        "_display_prefix_fingerprint",
        lambda *a, **k: (calls.append(1), orig(*a, **k))[-1],
    )
    _warm_get(routes)  # warm: the entry's digest must be reused
    second_session = captured["payload"]["session"]
    assert calls == [], (
        "a display-cache hit re-minted the whole-document digest — ordinary "
        "windowed opens must not pay for it (should-fix 1)"
    )
    assert second_session.get("prefix_fp") == first_session.get("prefix_fp")


def test_cold_display_load_hashes_the_document_exactly_once(monkeypatch, tmp_path):
    """2026-10-30 re-gate (cold double-hash): a cold windowed GET must hash
    the whole document ONCE, and the warm hit not at all.

    The maintainer's measurement on the previous head: 2 full-document hashes
    on the cold GET, 0 on the warm one. The cold path built the digest inside
    the display-merge cache entry AND then computed the identical digest again
    in the response mint — the entry was only reachable through the cache-hit
    probe, so the cold load never saw it.

    The counter is installed BEFORE the first GET (the existing memo test
    installs it after the cold load, so it cannot catch this) and counts only
    the calls that cover the whole document.

    RED-before: the cold GET hashes twice (2 != 1).
    """
    sidecar = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    state_db = sidecar + [_state_db_only_reply()]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)

    calls = []
    orig = routes._display_prefix_fingerprint

    def _counting(messages, count, **kwargs):
        # A full-document hash covers every row of the merged document.
        if count == len(messages or []):
            calls.append(count)
        return orig(messages, count, **kwargs)

    monkeypatch.setattr(routes, "_display_prefix_fingerprint", _counting)

    _warm_get(routes)  # cold: builds the entry AND mints — exactly one hash
    first_session = captured["payload"]["session"]
    assert first_session.get("prefix_fp")
    assert calls == [602], (
        f"the cold load must hash the document exactly once, got {calls}"
    )

    calls.clear()
    _warm_get(routes)  # warm: the entry's digest is reused, nothing is hashed
    second_session = captured["payload"]["session"]
    assert calls == [], (
        f"the warm hit must not hash the document at all, got {calls}"
    )
    assert second_session.get("prefix_fp") == first_session.get("prefix_fp")


def test_display_cache_memo_tracks_anchor_scene_saves(monkeypatch, tmp_path):
    """should-fix 1 + finding 2: a scene save (which writes the sidecar and so
    invalidates the merge-cache key) must yield a fresh digest on the next
    load — the memo can never hand out a pre-save digest.

    RED-before: the unmodified mint is scene-blind, so the post-save digest
    equals the pre-save one and this fails.
    """
    sidecar = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    state_db = sidecar + [_state_db_only_reply()]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)

    _warm_get(routes)
    before = captured["payload"]["session"]["prefix_fp"]
    assert before

    import api.models as models

    s = models.get_session("cont-child")
    # Row 151 is an assistant row inside this 602-row document.
    s.anchor_activity_scenes = {"index:151": _scene_record(151, "p151")}
    s.save(touch_updated_at=False, skip_index=True)

    _warm_get(routes)
    after = captured["payload"]["session"]["prefix_fp"]
    assert after != before, (
        "a scene save rendered a new worklog but the next load minted the "
        "same digest — the memo (or the digest) ignored the scene records"
    )


# ── 2026-10-31 re-gate: close the CLASS of out-of-row inputs ───────────────
# The docstring claims coverage is "complete by construction" because every
# row is hashed as delivered. Two inputs disproved that: they live OUTSIDE the
# rows (on the session, or in a setting applied after the mint) yet change what
# the browser receives for a row it already holds.
#
# Finding 1 (SILENT): `_display_prefix_fingerprint` read session-level
# ``tool_calls`` only through `_hydrate_anchor_activity_scenes`, which it
# called ONLY when the session had a saved anchor scene. A scene-free
# session's cards therefore never reached the digest — the maintainer's repro
# (602 scene-free rows, edit an older card's summary, append one row, focus
# refresh) was ACCEPTED and the open tab kept the stale snippet where master's
# focus refresh shows the new one.
#
# Finding 2 (SILENT, privacy): `redact_session_data` runs AFTER the mint, and
# neither the digest nor the display-merge memo's ``meta`` tuple carried
# ``api_redact_enabled`` — the maintainer's repro (turn redaction on, append a
# row, trigger the real focus-refresh handler) left earlier rows UNMASKED in
# the open tab where master masks them.
#
# Both tests below are RED-before: each fails on the unmodified digest, which
# had no input channel for the input at all.

def _credential_row():
    """A row whose delivered projection depends on the redaction setting."""
    return "Authorization: Bearer sk-ant-api03-AAAABBBBCCCCDDDD1234567890"


def test_prefix_fingerprint_covers_scene_free_session_tool_cards():
    """2026-10-31 re-gate, finding 1 (mint side): the digest must hash the
    calls the covered prefix owns, independently of anchor scenes.

    RED-before: the unmodified digest has no ``tool_calls`` channel unless
    ``scenes`` is truthy, so the scene-free digest is identical with and
    without the card and the first assertion fails.
    """
    rows = _make_rows(2001)
    held_fp = _display_prefix_fingerprint(rows, 2000)
    cards = [
        {
            "assistant_msg_idx": 1501,
            "id": "t1",
            "tool_name": "Bash",
            "result": "old summary",
        }
    ]
    fresh_fp = _display_prefix_fingerprint(rows, 2000, scenes=None, tool_calls=cards)
    assert fresh_fp != held_fp, (
        "a session-level tool card on a scene-free session never reached the "
        "digest — editing an older card's summary and appending a row was "
        "accepted, so the open tab kept the stale snippet (maintainer's "
        "finding 1 repro)"
    )
    # A later edit of the same card must move it again.
    edited = [
        {
            "assistant_msg_idx": 1501,
            "id": "t1",
            "tool_name": "Bash",
            "result": "new summary",
        }
    ]
    assert (
        _display_prefix_fingerprint(rows, 2000, scenes=None, tool_calls=edited)
        != fresh_fp
    )


def test_append_verify_refuses_a_scene_free_tool_card_edit():
    """2026-10-31 re-gate, finding 1 (verify side): the client's view was
    established scene-free; an older card's summary then changed with no
    generation bump and no row edit. The append must be refused.

    RED-before: the unmodified verifier passes ``tool_calls`` only into the
    scene hydration, which never runs without ``scenes`` — so the append is
    accepted (returns the anchor index).
    """
    rows = _make_rows(2001)
    # The client's held digest comes from the load that established its view:
    # a scene-free session with no session-level cards at all.
    held_fp = _display_prefix_fingerprint(rows, 2000)
    s = _FakeSession(generation=0)
    s.tool_calls = [
        {
            "assistant_msg_idx": 1501,
            "id": "t1",
            "tool_name": "Bash",
            "result": "rewritten summary",
        }
    ]
    anchor_fp = _append_anchor_fingerprint(
        _public_message_projection(rows[1999], _enabled=True)
    )
    assert _append_fast_path_anchor(s, rows, "1999", anchor_fp, "0", held_fp) is None, (
        "an older card's summary changed on a scene-free session and the "
        "append was still accepted — the client keeps a tool snippet the "
        "server no longer has (maintainer's finding 1 repro)"
    )
    # Control: the card set the digest was actually minted under still accepts.
    unchanged = _FakeSession(generation=0)
    assert _append_fast_path_anchor(unchanged, rows, "1999", anchor_fp, "0", held_fp) == 1999


def test_append_verify_refuses_a_card_edit_with_no_new_rows():
    """2026-10-31 re-gate, the tail of the "Low, not blocking" note: a
    session-level card change on an owner row the client ALREADY holds was not
    delivered even on an accepted append, because
    ``_tool_calls_for_message_window`` limits cards to the returned rows. The
    finding-1 binding refuses the append whatever the tail looks like — here
    there are no new rows at all, so the old behaviour would have answered with
    an empty append window and kept the stale snippet forever.

    RED-before: the unmodified verifier accepts (returns the anchor index).
    """
    rows = _make_rows(602)
    # The client holds the whole 602-row document; its digest covers [0, 602).
    held_fp = _display_prefix_fingerprint(rows, 602)
    s = _FakeSession(generation=0)
    s.tool_calls = [
        {
            "assistant_msg_idx": 151,
            "id": "t1",
            "tool_name": "Bash",
            "result": "rewritten summary",
        }
    ]
    anchor_fp = _append_anchor_fingerprint(
        _public_message_projection(rows[601], _enabled=True)
    )
    assert _append_fast_path_anchor(s, rows, "601", anchor_fp, "0", held_fp) is None, (
        "an owned row's card summary changed with no new rows and the append "
        "was accepted — the window carries no cards, so the client keeps the "
        "stale snippet with no way to ever refresh it"
    )
    # Control: the card set the digest was minted under still accepts (and the
    # window is legitimately empty — the server has nothing new).
    unchanged = _FakeSession(generation=0)
    assert _append_fast_path_anchor(unchanged, rows, "601", anchor_fp, "0", held_fp) == 601


def test_prefix_fingerprint_excludes_cards_owned_by_appended_rows():
    """The other half of finding 1: calls owned by the APPENDED rows must stay
    out of the covered prefix's digest, or every turn that runs a tool would
    refuse its own append.

    This one is green before and after: it pins the boundary so the fix cannot
    over-refuse.
    """
    rows = _make_rows(2001)
    held_fp = _display_prefix_fingerprint(rows, 2000)
    appended_cards = [
        {
            "assistant_msg_idx": 2000,
            "id": "t9",
            "tool_name": "Bash",
            "result": "the new turn's card",
        }
    ]
    assert (
        _display_prefix_fingerprint(rows, 2000, tool_calls=appended_cards) == held_fp
    ), (
        "a card owned by a row at/after the covered end moved the digest — "
        "every turn that runs a tool would refuse its own append"
    )
    s = _FakeSession(generation=0)
    s.tool_calls = appended_cards
    anchor_fp = _append_anchor_fingerprint(
        _public_message_projection(rows[1999], _enabled=True)
    )
    assert _append_fast_path_anchor(s, rows, "1999", anchor_fp, "0", held_fp) == 1999


def test_prefix_fingerprint_tracks_the_redaction_setting(monkeypatch):
    """2026-10-31 re-gate, finding 2 (mint side): the digest must be seeded
    with the effective ``api_redact_enabled`` value, because
    ``redact_session_data`` applies it AFTER the mint.

    RED-before: the unmodified digest never reads the setting, so the two
    digests are identical and the assertion fails.
    """
    import api.config

    rows = _make_rows(2001)
    rows[500]["content"] = _credential_row()
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": True})
    on_fp = _display_prefix_fingerprint(rows, 2000)
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": False})
    off_fp = _display_prefix_fingerprint(rows, 2000)
    assert on_fp != off_fp, (
        "the credential-redaction setting is applied after the mint and was "
        "not seeded into the digest — flipping it changed what the browser "
        "receives for every covered row without moving the fingerprint "
        "(maintainer's finding 2 repro)"
    )
    # Same setting, same digest: the seed must not churn.
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": True})
    assert _display_prefix_fingerprint(rows, 2000) == on_fp


def test_append_verify_refuses_after_the_redaction_setting_flips(monkeypatch):
    """2026-10-31 re-gate, finding 2 (verify side): the client's view was
    established with redaction ON; the setting is then turned OFF (or on) and
    the append must be refused so the authoritative full fetch re-delivers
    every covered row in its new projection.

    RED-before: the unmodified verifier ignores the setting and accepts.
    """
    import api.config

    rows = _make_rows(2001)
    rows[500]["content"] = _credential_row()
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": True})
    held_fp = _display_prefix_fingerprint(rows, 2000)
    # The anchor row carries no credential-shaped text, so its own digest is
    # stable across the flip: the prefix digest is the only check that can
    # refuse here.
    anchor_fp = _append_anchor_fingerprint(
        _public_message_projection(rows[1999], _enabled=True)
    )
    s = _FakeSession(generation=0)
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": False})
    assert _append_fast_path_anchor(s, rows, "1999", anchor_fp, "0", held_fp) is None, (
        "the redaction setting flipped and the append was still accepted — "
        "earlier rows stay unmasked in the open tab where master masks them "
        "(maintainer's finding 2 repro)"
    )
    # Control: the setting the digest was minted under still accepts.
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": True})
    assert _append_fast_path_anchor(s, rows, "1999", anchor_fp, "0", held_fp) == 1999


def test_prefix_fingerprint_tracks_the_active_turn_token():
    """2026-10-31 re-gate, the third member of the class: the active-turn
    token (``active_stream_id`` + ``pending_started_at``) is session-level
    state that ``redact_session_data`` applies after the mint — it decides
    whether a covered user row is delivered with ``_active_turn_user: true``
    (ui.js:15016 / sessions.js:4969 read that flag).

    RED-before: the unmodified digest has no token channel, so the first
    assertion fails.
    """
    from api.process_event_utils import build_active_turn_token

    rows = _make_rows(2001)
    token = build_active_turn_token("stream-1", 1700000000.0)
    assert token
    held_fp = _display_prefix_fingerprint(rows, 2000, active_turn_token=token)
    assert _display_prefix_fingerprint(rows, 2000) != held_fp, (
        "the active-turn token is session state applied after the mint and "
        "was not seeded into the digest — a turn ending (or a new one "
        "starting) changes the delivered user row without moving the digest"
    )
    # A different token must move it too.
    other = build_active_turn_token("stream-2", 1700000900.0)
    assert _display_prefix_fingerprint(rows, 2000, active_turn_token=other) != held_fp
    # The same token must not churn it.
    assert _display_prefix_fingerprint(rows, 2000, active_turn_token=token) == held_fp


def test_append_verify_refuses_when_the_active_turn_token_changes():
    """2026-10-31 re-gate, the active-turn token on the verify side."""
    from api.process_event_utils import build_active_turn_token

    rows = _make_rows(2001)
    token = build_active_turn_token("stream-1", 1700000000.0)
    held_fp = _display_prefix_fingerprint(rows, 2000, active_turn_token=token)
    s = _FakeSession(
        generation=0,
        active_stream_id="stream-1",
        pending_started_at=1700000000.0,
    )
    anchor_fp = _append_anchor_fingerprint(
        _public_message_projection(rows[1999], _enabled=True)
    )
    assert _append_fast_path_anchor(s, rows, "1999", anchor_fp, "0", held_fp) == 1999
    # The turn ended: the covered user row is no longer delivered with
    # ``_active_turn_user``, so the held digest must refuse.
    s.active_stream_id = None
    assert _append_fast_path_anchor(s, rows, "1999", anchor_fp, "0", held_fp) is None


def test_prefix_fingerprint_entry_meta_carries_every_out_of_row_input():
    """The memo's reuse guard must carry the same out-of-row inputs the digest
    is seeded with, or a display-cache hit can hand out a digest minted under
    different session state / settings.
    """
    from api.process_event_utils import build_active_turn_token
    from api.routes import _build_prefix_fingerprint_entry

    rows = _make_rows(10)
    s = _FakeSession(generation=0)
    s.tool_calls = [{"assistant_msg_idx": 3, "id": "t1", "result": "one"}]
    s.anchor_activity_scenes = {"index:3": _scene_record(3, "m3")}
    s.active_stream_id = "stream-1"
    s.pending_started_at = 1700000000.0
    entry = _build_prefix_fingerprint_entry(s, rows)
    assert entry and entry.get("digest")
    scenes_fp, cards_fp, redact_flag, token = entry["meta"]
    assert scenes_fp and cards_fp
    assert isinstance(redact_flag, bool)
    assert token == build_active_turn_token("stream-1", 1700000000.0)

    # A card edit must change the meta (and therefore the digest reuse guard).
    s.tool_calls = [{"assistant_msg_idx": 3, "id": "t1", "result": "two"}]
    edited = _build_prefix_fingerprint_entry(s, rows)
    assert edited["meta"] != entry["meta"]
    assert edited["digest"] != entry["digest"]

    # A different active-turn token must change the meta as well.
    s.tool_calls = [{"assistant_msg_idx": 3, "id": "t1", "result": "one"}]
    s.active_stream_id = "stream-2"
    s.pending_started_at = 1700000900.0
    retokened = _build_prefix_fingerprint_entry(s, rows)
    assert retokened["meta"] != entry["meta"]
    assert retokened["digest"] != entry["digest"]


# ── 2026-10-31 re-gate: sidecar-only sessions ──────────────────────────────
# A sidecar-only session returns from
# `_limited_webui_messages_for_display_with_sidecar` BEFORE the display-merge
# cache, so it never owns a merge entry: every windowed GET mints the
# whole-document digest from the rows it actually returns. The maintainer
# measured 1/1/1 hashes over three `msg_limit=30` GETs where the
# state.db-backed shape gives 1/0/0 — that gap is RETAINED on purpose
# (round-2 review: removing the sidecar-only digest memo is the accepted
# bounded alternative, see the retained-read regression below).


def test_sidecar_only_windowed_get_rehashes_the_document_on_every_get(monkeypatch, tmp_path):
    """The retained performance gap on the sidecar-only shape: with no memo on
    this path, every windowed GET hashes the document exactly once.

    Correctness first — a digest borrowed under a LATER sidecar stat let an
    append be accepted against a document the response never delivered
    (round-2 P2, regression below). Until a read-generation of the actual
    sidecar/lineage snapshot exists upstream, this path stays unmemoized and
    pays one hash per GET; the state.db-backed shape still gives 1/0/0 (see
    `test_cold_display_load_hashes_the_document_exactly_once`).

    This test pins the gap so it can neither silently widen nor silently
    close without the soundness property that would justify it.
    """
    sidecar = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    # No state.db rows: the display document IS the sidecar.
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, [])
    assert routes._display_merge_cache == {}

    calls = []
    orig = routes._display_prefix_fingerprint

    def _counting(messages, count, **kwargs):
        if count == len(messages or []):
            calls.append(count)
        return orig(messages, count, **kwargs)

    monkeypatch.setattr(routes, "_display_prefix_fingerprint", _counting)

    digests = []
    for _ in range(3):
        _warm_get(routes)
        digests.append(captured["payload"]["session"]["prefix_fp"])
    assert calls == [601, 601, 601], (
        "a sidecar-only windowed GET must mint its digest from the rows it "
        f"returns on every request, got {calls}"
    )
    assert digests[0] and digests[1] == digests[0] == digests[2], digests


def test_sidecar_only_prefix_digest_tracks_a_tool_card_edit(monkeypatch, tmp_path):
    """should-fix + finding 1 on the sidecar-only shape: this path carries no
    digest memo, so an in-process card edit (no sidecar write, so the stat
    signature is unchanged) must still move the next GET's digest.
    """
    sidecar = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, [])

    _warm_get(routes)
    before = captured["payload"]["session"]["prefix_fp"]
    assert before

    import api.models as models

    s = models.get_session("cont-child")
    # In-process edit only: NO save(), so the sidecar stat signature — and with
    # it every cache key — is unchanged.
    s.tool_calls = [
        {"assistant_msg_idx": 151, "id": "t1", "tool_name": "Bash", "result": "edited"}
    ]

    _warm_get(routes)
    after = captured["payload"]["session"]["prefix_fp"]
    assert after != before, (
        "a session-level card changed on a sidecar-only session but the next "
        "GET delivered the pre-edit digest"
    )


def test_sidecar_only_prefix_digest_tracks_the_redaction_setting(monkeypatch, tmp_path):
    """should-fix + finding 2 on the sidecar-only shape: flipping the redaction
    setting in process (no sidecar write) must move the next GET's digest."""
    import api.config

    sidecar = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, [])

    _warm_get(routes)
    before = captured["payload"]["session"]["prefix_fp"]
    assert before

    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": True})
    _warm_get(routes)
    after = captured["payload"]["session"]["prefix_fp"]
    assert after != before, (
        "the redaction setting flipped but the sidecar-only memo handed out "
        "the digest minted under the old setting"
    )


# ── 2026-10-31 re-gate round 2 (P2): a retained read must not borrow a
# digest minted from a LATER document ──
# The sidecar-only memo added for the should-fix above sampled the CURRENT
# sidecar stat AFTER the caller had already materialized `sidecar_messages`,
# and reused a matching current-stat entry without proving the passed rows
# belonged to that generation. The maintainer's replay: a windowed GET
# snapshots the 601-row sidecar-only document and pauses before the memo
# lookup; a writer rewrites row 590 and persists it (count, last timestamp,
# generation, anchor row and meta all unchanged, so every key still matches);
# a fresh GET reads the edited document and publishes its digest under the new
# stat; the paused GET resumes, samples that stat, borrows the fresh digest
# and still returns its own pre-edit rows; the append that echoes the proof is
# then ACCEPTED with zero returned rows and the stale row stays on screen.
#
# The memo is gone (accepted bounded alternative): the digest is minted from
# the rows the response actually delivers, so the same schedule ends in a
# REFUSAL that re-delivers the full document. Sound reuse would need the
# read-generation of the actual sidecar/lineage snapshot, which upstream does
# not provide yet.


def _replay_retained_read_schedule(monkeypatch, tmp_path, writer):
    """Run the round-2 interleaving; return (routes, captured, sidecar, paused).

    ``writer(session)`` performs the durable change while the first windowed
    GET is paused between snapshotting the sidecar document and minting its
    digest. The hook only controls ordering: the snapshot, the save, the
    response mint and the append verifier are production code.
    """
    import api.models as models

    sidecar = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    # No state.db rows: the display document IS the sidecar.
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, [])
    session = models.get_session("cont-child")

    orig_lineage = routes._webui_sidecar_lineage_messages_for_display
    paused = {"done": False, "fresh": None}

    def _retained_read(sess, **kwargs):
        # The snapshot this GET will deliver, taken BEFORE the write.
        rows = orig_lineage(sess, **kwargs)
        if paused["done"]:
            return rows
        paused["done"] = True
        writer(session)
        # A fresh GET reads the edited document and publishes its digest under
        # the post-save stat (while the memo existed).
        _warm_get(routes)
        paused["fresh"] = captured["payload"]["session"]
        # The paused GET resumes with its retained pre-write rows.
        return rows

    monkeypatch.setattr(
        routes, "_webui_sidecar_lineage_messages_for_display", _retained_read
    )
    # msg_limit=500 puts row 590 inside the returned 500-row tail, exactly
    # like the maintainer's replay.
    routes._handle_session_get(
        None,
        types.SimpleNamespace(
            path="/api/session",
            query="session_id=cont-child&messages=1&resolve_model=0&msg_limit=500&expand_renderable=1",
        ),
    )
    return routes, captured, sidecar, paused


def _writer_rewrites_row_590(session):
    """Replace row 590's content and persist it — nothing else moves."""
    row = dict(session.messages[590])
    row["content"] = "p590-rewritten-on-disk"
    session.messages[590] = row
    session.save(touch_updated_at=False, skip_index=True)


def test_sidecar_only_retained_read_cannot_borrow_a_later_digest(monkeypatch, tmp_path):
    """Round-2 P2 end to end: retained-read → durable earlier-row save → fresh
    memo publication → resumed old response → append-refusal.

    RED-before (sidecar-only memo present): the paused response carries the
    fresh GET's digest, the append is ACCEPTED with zero returned rows, and
    the open tab keeps the stale row 590. The digest must be minted from the
    rows the response delivers, so the append is refused and the authoritative
    full document — carrying the rewrite — arrives instead.
    """
    routes, captured, sidecar, paused = _replay_retained_read_schedule(
        monkeypatch, tmp_path, _writer_rewrites_row_590
    )

    fresh = paused["fresh"]
    # Premise: the fresh GET really read the rewritten document.
    assert any(
        m.get("content") == "p590-rewritten-on-disk"
        for m in (fresh.get("messages") or [])
    ), fresh.get("messages")

    # The paused GET's own 500-row tail still carries the OLD row 590 ...
    held = captured["payload"]["session"]
    held_rows = held.get("messages") or []
    assert len(held_rows) == 500, held_rows[:2]
    assert any(m.get("content") == "p590" for m in held_rows), held_rows[485:495]

    # ... so the proof it hands the client must describe THAT document. The
    # client's held view ends at row 600, which the rewrite left untouched.
    session_after = _append_get(
        routes,
        msg_after=600,
        anchor_row=sidecar[600],
        prefix_fp=held["prefix_fp"],
    )
    assert session_after.get("_append_only") is not True, (
        "a windowed GET that snapshotted the document BEFORE a durable "
        "earlier-row rewrite published a digest minted from the LATER "
        f"document: the append was accepted with "
        f"{len(session_after.get('messages') or [])} returned rows and the "
        "open tab keeps the stale row 590 (maintainer's round-2 P2 replay)"
    )
    # The refusal answers with the authoritative full document, rewrite
    # included — exactly master's focus refresh.
    assert session_after.get("message_count") == 601
    delivered = session_after.get("messages") or []
    assert len(delivered) == 601, delivered[:3]
    assert delivered[590]["content"] == "p590-rewritten-on-disk", delivered[590]


def _writer_appends_a_row(session):
    """Append one row and persist it — the covered prefix is unchanged, but
    the row count and the last timestamp move, so any stat-derived key must
    miss."""
    session.messages.append(
        {"role": "assistant", "content": "p601-appended", "timestamp": 1700000601}
    )
    session.save(touch_updated_at=False, skip_index=True)


def test_sidecar_only_memo_miss_mints_from_the_returned_rows(monkeypatch, tmp_path):
    """Miss/publication interleaving: the same retained read, but the durable
    change moves the key (an appended row), so the resumed GET must MISS and
    mint its digest from the rows it returns.

    Borrowing the fresh GET's digest here would cover 602 rows and refuse a
    legitimate append (the covered prefix is unchanged), so this pins the miss
    path: the append is accepted and exactly the new row travels.
    """
    routes, captured, sidecar, paused = _replay_retained_read_schedule(
        monkeypatch, tmp_path, _writer_appends_a_row
    )

    fresh = paused["fresh"]
    assert (fresh.get("message_count") or 0) == 602, fresh.get("message_count")

    held = captured["payload"]["session"]
    held_rows = held.get("messages") or []
    assert len(held_rows) == 500, held_rows[:2]

    session_after = _append_get(
        routes,
        msg_after=600,
        anchor_row=sidecar[600],
        prefix_fp=held["prefix_fp"],
    )
    assert session_after.get("_append_only") is True, (
        "a memo MISS after a retained read refused a legitimate append — the "
        "digest was not minted from the rows the response returned"
    )
    messages = session_after.get("messages") or []
    assert [m.get("content") for m in messages] == ["p601-appended"], messages
    assert session_after.get("message_count") == 602
    assert session_after.get("_messages_offset") == 601


# ── 2026-10-31 re-gate: the maintainer's two repros through the real handler ─
# "602 scene-free rows, update an older card's summary, append one row, focus
# refresh" and "turn redaction on, append a row, then trigger the real
# focus-refresh handler". Both must answer with the authoritative full
# transcript (no ``_append_only``), which is what master's focus refresh does.


def test_handler_focus_refresh_refuses_after_a_scene_free_card_edit(monkeypatch, tmp_path):
    """Finding 1 end to end: the append is refused and the full document
    (carrying the rewritten card) is delivered instead of the stale snippet.

    RED-before: the append is accepted (``_append_only`` true) and the client
    keeps the old summary.
    """
    sidecar = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    state_db = sidecar + [_state_db_only_reply()]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)
    _warm_display_cache(routes)
    # The client's held digest comes from the load that established its view.
    held_fp = captured["payload"]["session"]["prefix_fp"]
    assert held_fp

    import api.models as models

    s = models.get_session("cont-child")
    assert not getattr(s, "anchor_activity_scenes", None)  # scene-free session
    # An older card's summary is rewritten in process: no row changes, no
    # generation bump, no sidecar write.
    s.tool_calls = [
        {"assistant_msg_idx": 151, "id": "t1", "tool_name": "Bash", "result": "rewritten"}
    ]

    session = _append_get(
        routes,
        msg_after=601,
        anchor_row=_state_db_only_reply(),
        prefix_fp=held_fp,
    )
    assert session.get("_append_only") is not True, (
        "the focus-refresh append was accepted after an older card's summary "
        "changed — the open tab keeps the stale snippet (maintainer's "
        "finding 1 repro)"
    )
    # The refusal answers with the authoritative full document, which carries
    # the rewritten card — exactly master's focus refresh.
    assert session.get("message_count") == 602
    assert len(session.get("messages") or []) == 602
    cards = session.get("tool_calls") or []
    assert [c.get("result") for c in cards] == ["rewritten"], cards


def test_handler_focus_refresh_refuses_after_the_redaction_setting_flips(monkeypatch, tmp_path):
    """Finding 2 end to end: with redaction turned on after the client's view
    was established, the append is refused and the full document arrives
    masked — instead of leaving earlier rows unmasked in the open tab.

    RED-before: the append is accepted and the delivered rows are unmasked.
    """
    import api.config

    sidecar = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    sidecar[500]["content"] = _credential_row()
    state_db = sidecar + [_state_db_only_reply()]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)
    # The harness establishes the view with redaction OFF.
    _warm_display_cache(routes)
    held_fp = captured["payload"]["session"]["prefix_fp"]
    assert held_fp

    # The setting is turned ON (through the settings API's own code path:
    # redact_session_data reads it per request).
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": True})
    session = _append_get(
        routes,
        msg_after=601,
        anchor_row=_state_db_only_reply(),
        prefix_fp=held_fp,
    )
    assert session.get("_append_only") is not True, (
        "the focus-refresh append was accepted after redaction was turned on "
        "— earlier rows stay unmasked in the open tab (maintainer's finding 2 "
        "repro)"
    )
    assert session.get("message_count") == 602
    delivered = session.get("messages") or []
    assert len(delivered) == 602
    assert _credential_row() not in json.dumps(delivered), (
        "the authoritative full fetch delivered an unmasked credential with "
        "api_redact_enabled=True"
    )


# ── 2026-10-31 re-gate, finding 1: ONE snapshot for projection + digest ─────
# The mint re-read the out-of-row inputs (scene records, session tool cards,
# the redaction setting, the active-turn token) AFTER the response rows had
# already been projected from an earlier read. A save that lands in between —
# the maintainer's repro: pause the GET after the response rows are projected,
# save an anchor scene or edit a tool card, then let the mint run — published a
# digest of the NEW inputs over the OLD rows, so every later reload accepted an
# empty append and the stale content stayed indefinitely. The inputs are now
# read ONCE per request and that single snapshot feeds the projection, the
# digest and the cache-entry validation.


def _full_transcript_get(routes):
    """Master's bare full-transcript GET (no msg_limit, no expand_renderable)."""
    import types

    captured = {}
    routes.j = lambda _h, payload, status=200, **_k: (
        captured.__setitem__("payload", payload),
        payload,
    )[-1]
    routes._handle_session_get(
        None,
        types.SimpleNamespace(
            path="/api/session",
            query="session_id=cont-child&messages=1&resolve_model=0",
        ),
    )
    return captured["payload"]["session"]


def _mid_request_save_seam(monkeypatch, routes, save):
    """Install a save that lands between the projection and the mint.

    ``routes._active_stream_ids`` is the one module-level call the GET makes
    after it has hydrated the response rows and before it mints the digest
    (routes.py: the hydration, then ``compact_session``, then ``prefix_fp``).
    Wrapping it stands in for another thread committing a save while this GET
    is in flight; the snapshot, the projection, the mint and the append
    verifier are all production code.
    """
    real_active = routes._active_stream_ids

    def _save_lands_mid_request():
        save()
        return real_active()

    monkeypatch.setattr(routes, "_active_stream_ids", _save_lands_mid_request)


def test_handler_mid_request_scene_save_cannot_publish_a_proof_of_new_inputs(
    monkeypatch, tmp_path
):
    """Finding 1 end to end: a save that lands BETWEEN the projection of the
    response rows and the digest mint must not publish a proof of the NEW
    inputs over the OLD rows.

    RED-before: the mint re-reads ``s.anchor_activity_scenes`` after the
    hydration, so the published digest covers the saved scene while the
    delivered rows do not carry it — the follow-up append is ACCEPTED with an
    empty window and the worklog never reaches the open tab.
    """
    import api.models as models

    sidecar = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    state_db = sidecar + [_state_db_only_reply()]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)
    s = models.get_session("cont-child")
    assert not getattr(s, "anchor_activity_scenes", None)  # scene-free session

    _mid_request_save_seam(
        monkeypatch,
        routes,
        lambda: setattr(s, "anchor_activity_scenes", {"index:151": _scene_record(151, "p151")}),
    )

    held = _full_transcript_get(routes)
    held_fp = held.get("prefix_fp")
    assert held_fp
    # Premise: the response really carries the OLD content — the save landed
    # after the rows were projected, so no worklog is on any delivered row.
    assert not any(m.get("_anchor_activity_scene") for m in held.get("messages") or []), (
        "the save landed before the projection — the repro needs the rows "
        "projected from the pre-save inputs"
    )

    # The later reload proves the prefix it holds with that digest. Master's
    # behaviour: the save happened, so the proof no longer describes the held
    # rows and the authoritative full document (carrying the worklog) arrives.
    session = _append_get(
        routes,
        msg_after=601,
        anchor_row=_state_db_only_reply(),
        prefix_fp=held_fp,
    )
    assert session.get("_append_only") is not True, (
        "the mid-request save published a digest of the NEW inputs over the "
        "OLD rows, so the empty append was accepted and the saved scene never "
        "reaches the open tab (maintainer's finding 1 repro)"
    )
    assert session.get("message_count") == 602
    delivered = session.get("messages") or []
    assert len(delivered) == 602
    assert any(m.get("_anchor_activity_scene") for m in delivered), (
        "the authoritative full fetch must carry the saved worklog — the save "
        "really landed, so the reload has to correct the held rows"
    )


def test_handler_mid_request_card_edit_cannot_publish_a_proof_of_new_inputs(
    monkeypatch, tmp_path
):
    """Finding 1 end to end, tool-card half: a session-level card edited while
    the GET is in flight must not be covered by the digest published with the
    OLD card still on the delivered rows.

    RED-before: the response carries the pre-edit card while the mint seeds
    the edited one, so the follow-up append is ACCEPTED and the open tab keeps
    the stale snippet forever.
    """
    sidecar = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    state_db = sidecar + [_state_db_only_reply()]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)
    import api.models as models

    s = models.get_session("cont-child")
    # Row 590 is an assistant row inside the 30-row tail window this GET
    # returns, so the delivered card list is observable on the response.
    old_card = {"assistant_msg_idx": 590, "id": "t1", "tool_name": "Bash", "result": "old summary"}
    new_card = {"assistant_msg_idx": 590, "id": "t1", "tool_name": "Bash", "result": "rewritten summary"}
    s.tool_calls = [old_card]

    _mid_request_save_seam(
        monkeypatch, routes, lambda: setattr(s, "tool_calls", [new_card])
    )

    held = _full_transcript_get(routes)
    held_fp = held.get("prefix_fp")
    assert held_fp
    # Premise: the response carries the OLD card — the edit landed after the
    # card list was projected.
    held_cards = held.get("tool_calls") or []
    assert [c.get("result") for c in held_cards] == ["old summary"], held_cards

    session = _append_get(
        routes,
        msg_after=601,
        anchor_row=_state_db_only_reply(),
        prefix_fp=held_fp,
    )
    assert session.get("_append_only") is not True, (
        "the mid-request card edit published a digest of the NEW card over "
        "the OLD one, so the empty append was accepted and the open tab keeps "
        "the stale snippet (maintainer's finding 1 repro)"
    )
    cards = session.get("tool_calls") or []
    assert [c.get("result") for c in cards] == ["rewritten summary"], cards


# ── 2026-10-31 re-gate, finding 3: the per-row _active_turn_token ───────────
# ``_PREFIX_FP_EXCLUDED_FIELDS`` was exactly ``_PUBLIC_MESSAGE_INTERNAL_FIELDS``
# — the set ``_public_message_projection`` strips before delivery — so the
# per-row ``_active_turn_token`` rode along in the exclusion. That field is an
# INPUT to the delivery: ``redact_session_data`` marks a covered user row
# ``_active_turn_user: true`` when its own token equals the session's active
# token. Stamping only the row's token therefore changed the delivered row
# while the proof stood still, and an empty append was accepted. The turn
# lifecycle rarely produces that state (the token only lands on the tail user
# row of the current turn), so this is a narrow case — but the field is now
# covered by the digest instead of excluded.


def test_prefix_fingerprint_covers_the_row_active_turn_token():
    """Finding 3: the row's own ``_active_turn_token`` moves the digest, so a
    stamp that changes the delivered row can no longer keep its proof.

    RED-before: the field is inside the exclusion set, so the digest is
    byte-identical before and after the stamp and the append is accepted.
    """
    from api.process_event_utils import build_active_turn_token, stamp_message_source

    rows = _make_rows(602)
    # The session's current turn is active, so its tail user row is delivered
    # with ``_active_turn_user: true`` once it carries the token.
    token = build_active_turn_token("stream-1", 123.0)
    assert token
    s = _FakeSession(generation=0, active_stream_id="stream-1", pending_started_at=123.0)
    # The client's view was established while this turn was already active,
    # so the session-level seed is unchanged by the stamp below.
    held_fp = _display_prefix_fingerprint(rows, 602, active_turn_token=token)

    stamped = [dict(r) for r in rows]
    # The production settlement helper stamps ONLY the row's token (a ``webui``
    # source leaves ``_source`` alone).
    stamp_message_source(stamped[600], "webui", active_turn_token=token)
    assert stamped[600].get("_active_turn_token") == token

    # Premise: the stamp really changes what the browser receives for row 600.
    marked = _public_message_projection(
        stamped[600], _enabled=True, _active_turn_token=token
    )
    assert marked.get("_active_turn_user") is True, marked
    assert (
        _public_message_projection(rows[600], _enabled=True, _active_turn_token=token).get(
            "_active_turn_user"
        )
        is None
    )

    assert _display_prefix_fingerprint(stamped, 602, active_turn_token=token) != held_fp, (
        "stamping only the row's _active_turn_token changed the delivered row "
        "but kept its proof — the digest excluded the very field that decides "
        "the active-turn marking (maintainer's finding 3)"
    )

    anchor_fp = _append_anchor_fingerprint(
        _public_message_projection(stamped[601], _enabled=True)
    )
    assert _append_fast_path_anchor(s, stamped, "601", anchor_fp, "0", held_fp) is None, (
        "an empty append was accepted after the row's active-turn token was "
        "stamped — the open tab keeps a row the server now renders differently"
    )
    # Control: the un-stamped document under the same active turn still proves
    # itself, so the fix cannot over-refuse.
    plain_anchor_fp = _append_anchor_fingerprint(
        _public_message_projection(rows[601], _enabled=True)
    )
    assert _append_fast_path_anchor(s, rows, "601", plain_anchor_fp, "0", held_fp) == 601
