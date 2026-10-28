"""Server-side append-only fast path unit checks (#7925 re-gate)."""
from __future__ import annotations

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
    def __init__(self, generation: int = 0):
        self.transcript_generation = generation


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
    # The digest covers the RENDER-RELEVANT fields, not the whole row: an
    # internal-field-only change is not a rendered difference.
    internal = [dict(r) for r in rows]
    internal[500]["_internal_note"] = "not rendered"
    assert _append_fast_path_anchor(s, internal, "1999", anchor_fp, "0", held_fp) == 1999
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

    routes, _ = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)
    _warm_display_cache(routes)
    # Premise: the cache really is warm for this session (the probe would hit).
    import api.models as models

    probe = routes._display_merge_cached_messages(models.get_session("cont-child"), None)
    assert probe is not None and len(probe) == 602, (
        "cache did not warm — the repro needs a live cache entry"
    )

    session = _append_get(
        routes,
        msg_after=601,
        anchor_row=_state_db_only_reply(),
        prefix_fp=_display_prefix_fingerprint(state_db, 602),
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
