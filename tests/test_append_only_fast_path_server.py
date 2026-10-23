"""Server-side append-only fast path unit checks (#7925 re-gate)."""
from __future__ import annotations

from api.helpers import _public_message_projection
from api.routes import _append_anchor_fingerprint, _append_fast_path_anchor, _message_window_for_display


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


def test_append_fast_path_anchor_validates_generation_fingerprint_and_range():
    rows = _make_rows(2001)
    projected = _public_message_projection(rows[1999], _enabled=True)
    fp = _append_anchor_fingerprint(projected)
    assert fp and len(fp) == 26
    s = _FakeSession(generation=0)
    assert _append_fast_path_anchor(s, rows, "1999", fp, "0") == 1999
    # generation changed → None (fall back to the authoritative full fetch)
    assert _append_fast_path_anchor(s, rows, "1999", fp, "1") is None
    # anchor fingerprint mismatch → None
    assert _append_fast_path_anchor(s, rows, "1999", "x" * 26, "0") is None
    # anchor out of range → None
    assert _append_fast_path_anchor(s, rows, "999999", fp, "0") is None
    # unparsable index → None
    assert _append_fast_path_anchor(s, rows, "abc", fp, "0") is None
    # missing fingerprint → None
    assert _append_fast_path_anchor(s, rows, "1999", None, "0") is None


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
    assert client_fp != fp
    assert _append_fast_path_anchor(s, rewritten, "1999", client_fp, "0") is None


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

    # The client rendered all 601 parent rows; its anchor is the last one.
    anchor_row = _public_message_projection(parent_rows[600], _enabled=False)
    anchor_fp = _append_anchor_fingerprint(anchor_row)
    query = (
        "session_id=cont-child&messages=1&resolve_model=0"
        "&msg_after=600"
        f"&anchor_fp={anchor_fp}"
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
    query = (
        "session_id=cont-child&messages=1&resolve_model=0"
        "&msg_after=600"
        f"&anchor_fp={anchor_fp}"
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
