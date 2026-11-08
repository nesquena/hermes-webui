"""#7925 single projection: one delivered projection per GET /api/session.

The 2026-10-31 re-gate replaced the prefix proof's input — pre-transform rows
plus out-of-row "seeds" — with the DELIVERED projection itself, so the proof
hashes the bytes the response serializes. It left the projection being
computed twice per append request: once inside the fast-path verification
(over the client's held range) and once for the response body and mint (over
the response's coverage). Redaction dominates the projection's cost, so that
second pass over the held rows is what took a 32 MiB append from ~1.7s to
~2.9s — and it also meant the proof was derived from a different object than
the delivered rows.

This file pins the shape that removes the second pass. A message-loading GET
computes the delivered projection exactly once (A), and three views read that
one object (B):

* the response body is the projection's window slice;
* the response's ``prefix_fp`` is its digest over the response's coverage;
* the append verification compares the client's echo against its digest
  re-scoped to the client's HELD range (D) — never the rows the response is
  about to deliver.

A mismatch still fails closed to the authoritative full transcript (C), and
the projection still stops at the response's coverage end, never scanning the
rest of the document (E, #7899).

The documented consequence of re-scoping instead of re-projecting: scene
hydration resolves an assistant ref that occurs more than once in the COVERED
range by index instead of by ref, so a row that arrives inside the coverage
and duplicates a held row's ref moves the held row's scene — the digest stops
matching and the append is refused. That refusal is fail-closed AND correct
(the server's current rendering of the held row really did change, the same
shape as the active-turn boundary), and
``test_duplicate_ref_arrival_refuses_and_redelivers_the_held_row`` pins it,
together with the evidence that the held prefix is byte-identical under the
narrow scope — i.e. the previous shape would have accepted that append and
left the client rendering a row the server no longer renders that way.
"""
from __future__ import annotations

import api.routes as routes
from api.routes import (
    _assistant_anchor_scene_message_ref,
    _display_prefix_projection,
    _display_prefix_projection_scoped,
    _prefix_fingerprint_over_projection,
)

from tests.test_append_only_fast_path_server import (  # noqa: E402
    _append_get,
    _full_transcript_get,
    _state_db_only_reply,
    _warm_cache_append_harness,
    _warm_display_cache,
)
from tests.test_7925_prefix_fp_by_construction import _independent_digest  # noqa: E402
from tests.test_webui_state_db_reconciliation import _make_state_db  # noqa: E402


# ── helpers ─────────────────────────────────────────────────────────────────


def _sidecar_rows(n, scene_row=None):
    """The standard alternating fixture, optionally overriding one row."""
    rows = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"p{i}",
            "timestamp": 1700000000 + i,
        }
        for i in range(n)
    ]
    if scene_row is not None:
        idx, row = scene_row
        rows[idx] = row
    return rows


def _counting_projections(monkeypatch):
    """Count ``_display_prefix_projection`` calls and record each coverage."""
    calls = []
    orig = getattr(routes, "_display_prefix_projection", None)
    if orig is None:  # pragma: no cover - the projection always exists
        return calls

    def _spy(messages, count, **kwargs):
        calls.append(count)
        return orig(messages, count, **kwargs)

    monkeypatch.setattr(routes, "_display_prefix_projection", _spy)
    return calls


def _captured_window_projections(monkeypatch):
    """Record every ``_delivered_window_projection`` result of the request.

    A no-op recorder when the module has no such seam (the pre-single-projection
    shape), so a test still exercises the request and fails on its behavioural
    assertion rather than on a missing attribute.
    """
    seen = []
    orig = getattr(routes, "_delivered_window_projection", None)
    if orig is None:
        return seen

    def _spy(
        session,
        messages,
        *,
        msg_limit,
        msg_before,
        msg_after,
        expand_renderable,
        cached,
        inputs=None,
    ):
        result = orig(
            session,
            messages,
            msg_limit=msg_limit,
            msg_before=msg_before,
            msg_after=msg_after,
            expand_renderable=expand_renderable,
            cached=cached,
            inputs=inputs,
        )
        seen.append(
            {
                "messages": messages,
                "result": result,
                "inputs": inputs,
                "msg_limit": msg_limit,
                "msg_after": msg_after,
            }
        )
        return result

    monkeypatch.setattr(routes, "_delivered_window_projection", _spy)
    return seen


def _captured_proofs(monkeypatch):
    """Record every append-proof comparison: projection, coverage, inputs."""
    seen = []
    orig = getattr(routes, "_append_prefix_proof_matches", None)
    if orig is None:
        return seen

    def _spy(projection, count, prefix_fp_raw, *, inputs):
        seen.append(
            {"projection": projection, "count": count, "inputs": inputs, "echo": prefix_fp_raw}
        )
        return orig(projection, count, prefix_fp_raw, inputs=inputs)

    monkeypatch.setattr(routes, "_append_prefix_proof_matches", _spy)
    return seen


def _append_get_limited(*, msg_after, anchor_row, prefix_fp, msg_limit):
    """An append GET with an explicit window bound (``_append_get`` hardcodes 500)."""
    import types

    from api.helpers import _public_message_projection

    anchor_fp = routes._append_anchor_fingerprint(
        _public_message_projection(anchor_row, _enabled=False)
    )
    captured = {}
    routes.j = lambda _h, payload, status=200, **_k: (
        captured.__setitem__("payload", payload),
        payload,
    )[-1]
    routes._handle_session_get(
        None,
        types.SimpleNamespace(
            path="/api/session",
            query=(
                "session_id=cont-child&messages=1&resolve_model=0"
                f"&msg_after={msg_after}"
                f"&anchor_fp={anchor_fp}"
                "&transcript_generation=0"
                f"&prefix_fp={prefix_fp}"
                f"&msg_limit={msg_limit}&expand_renderable=1"
            ),
        ),
    )
    return captured["payload"]["session"]


def _rewrite_state_db(tmp_path, rows):
    """Replace the session's state.db rows (the source of "newly arrived" rows)."""
    path = tmp_path / "state.db"
    path.unlink(missing_ok=True)
    _make_state_db(path, "cont-child", rows)


# ── A. one projection per request ───────────────────────────────────────────


def test_append_request_projects_the_document_exactly_once(monkeypatch, tmp_path):
    """A: an accepted append computes the delivered projection ONCE.

    The maintainer's focus-refresh shape: the client holds all 602 rows and
    nothing new arrived, so the response is an empty append window whose
    coverage is the whole document. The previous shape computed the projection
    twice — once for the verification's held range and once for the body/mint
    coverage — which is the measured 2.9s (vs ~1.7s) append on a 32 MiB
    session.

    RED-before: the verification's own projection makes this 2.
    """
    sidecar = _sidecar_rows(601)
    state_db = sidecar + [_state_db_only_reply()]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)
    # The load that establishes the client's view: it holds [0, 602).
    held = _full_transcript_get(routes)
    held_fp = held["prefix_fp"]
    assert held_fp and len(held["messages"]) == 602

    calls = _counting_projections(monkeypatch)
    session = _append_get(
        routes, msg_after=601, anchor_row=_state_db_only_reply(), prefix_fp=held_fp
    )
    assert session.get("_append_only") is True, session.get("_append_only")
    assert session.get("messages") == []
    assert calls == [602], (
        f"one accepted append must project the document exactly once, got {calls}"
    )


def test_full_load_projects_the_document_exactly_once(monkeypatch, tmp_path):
    """A: master's bare full-transcript GET projects once. No verification
    projection exists on this path, so the guard pins that it stays that way."""
    sidecar = _sidecar_rows(601)
    state_db = sidecar + [_state_db_only_reply()]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)
    calls = _counting_projections(monkeypatch)
    session = _full_transcript_get(routes)
    assert session.get("prefix_fp")
    assert calls == [602], f"a full load must project once, got {calls}"


def test_warm_windowed_load_projects_nothing(monkeypatch, tmp_path):
    """A: a warm display-merge hit slices the entry's projection instead of
    recomputing it. The append's single projection is an upper bound; the warm
    windowed open is strictly below it."""
    sidecar = _sidecar_rows(601)
    state_db = sidecar + [_state_db_only_reply()]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)
    _warm_display_cache(routes)  # cold: builds the entry (one projection)
    calls = _counting_projections(monkeypatch)
    _warm_display_cache(routes)  # warm: the entry's rows are reused
    assert calls == [], f"a warm windowed open must project nothing, got {calls}"


def test_a_failed_precheck_answers_full_without_projecting_the_held_prefix(
    monkeypatch, tmp_path
):
    """A: the projection-free half of the validation runs first, so a request
    that fails it (here: a stale transcript_generation) never pays for a
    projection of the client's held prefix — the authoritative full response
    is projected once, for its own coverage."""
    sidecar = _sidecar_rows(601)
    state_db = sidecar + [_state_db_only_reply()]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)
    held = _full_transcript_get(routes)
    calls = _counting_projections(monkeypatch)
    session = _append_get(
        routes,
        msg_after=601,
        anchor_row=_state_db_only_reply(),
        prefix_fp=held["prefix_fp"],
        generation=1,  # stale: a destructive rewrite landed
    )
    assert session.get("_append_only") is not True
    assert len(session.get("messages") or []) == 602
    assert calls == [602], f"a precheck refusal must project once, got {calls}"


# ── B. three views, one projection object ───────────────────────────────────


def test_proof_body_and_verification_read_one_projection(monkeypatch, tmp_path):
    """B + D: the body, the published proof and the append verification are
    three views of ONE projection object.

    The client holds [0, 600); three new rows arrive; the append (msg_limit=2)
    delivers two of them, so the body is a STRICT slice of the projection and
    the three views can be compared for real: the body is the projection's
    window slice, the published proof is the projection's digest at the
    response coverage, and the verification compared the echo against the SAME
    object's digest re-scoped to the client's held range.
    """
    sidecar = _sidecar_rows(600)
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, list(sidecar))
    held = _full_transcript_get(routes)
    held_fp = held["prefix_fp"]
    assert len(held["messages"]) == 600

    new_rows = [
        {"role": "assistant", "content": f"new-{i}", "timestamp": 1700000600 + i}
        for i in range(3)
    ]
    _rewrite_state_db(tmp_path, list(sidecar) + new_rows)

    seen = _captured_window_projections(monkeypatch)
    proofs = _captured_proofs(monkeypatch)
    session = _append_get_limited(
        msg_after=599, anchor_row=sidecar[599], prefix_fp=held_fp, msg_limit=2
    )
    assert session.get("_append_only") is True, session.get("_append_only")
    messages = session.get("messages") or []
    assert [m["content"] for m in messages] == ["new-0", "new-1"], messages

    # One projection object for the whole request.
    assert len(seen) == 1
    window, offset, coverage_end, rows, cards = seen[-1]["result"]
    assert offset == 600 and coverage_end == 602 and len(rows) == 602
    # B1: the body is that projection's window slice — not a second derivation.
    assert messages == rows[coverage_end - len(messages) :]
    # B2: the published proof is that projection's digest at the coverage.
    assert session["prefix_fp"] == _prefix_fingerprint_over_projection(rows, cards)
    # B3: the verification read the SAME object, scoped to the held range (D).
    assert len(proofs) == 1
    proof = proofs[-1]
    assert proof["projection"][0] is rows and proof["projection"][1] is cards, (
        "the append verification must read the very projection the body and "
        "the mint read — not a second, differently-scoped one"
    )
    assert proof["count"] == 600, (
        f"the proof must cover the client's held range [0, 600), got {proof['count']}"
    )
    scoped_rows, scoped_cards = _display_prefix_projection_scoped(
        (rows, cards),
        proof["count"],
        tool_calls=proof["inputs"].tool_calls,
        redact_enabled=proof["inputs"].redact_enabled,
    )
    assert _prefix_fingerprint_over_projection(scoped_rows, scoped_cards) == held_fp, (
        "the verification must compare the echo against the digest of the "
        "SAME projection re-scoped to the client's held range"
    )


# ── the public form of the response IS the delivered projection ─────────────


def test_the_public_form_of_the_response_keeps_the_active_user_marker(monkeypatch, tmp_path):
    """#7925 Greptile P1 (2026-11-08): the public projection adds no second pass.

    ``_handle_session_get`` hands ``public_session_projection`` the payload
    whose rows ``prefix_fp`` hashed. The public form used to call
    ``redact_session_data`` again WITHOUT ``_messages_projected``: that second
    pass re-projected every row from scratch and stripped ``_active_turn_user``
    — a field the projection ADDS from the session's active-turn token, never
    one it stores on the row, and the first pass had already removed
    ``_active_turn_token`` so it could not be rebuilt. The response therefore
    disagreed with its own proof, and the browser lost the marker it needs to
    recognize the saved pending prompt, so it could show that prompt twice.

    RED-before (head 174754d90): the turn's user row arrives without
    ``_active_turn_user`` — the same silent drop
    ``test_active_turn_boundary_refuses_the_append`` pins from the digest side.
    """
    import api.config
    from api.process_event_utils import build_active_turn_token

    token = build_active_turn_token("stream-1", 1700000000.0)
    assert token
    sidecar = _sidecar_rows(4)
    # Row 2 is the turn's own user row; the settlement helper stamps the very
    # token the session's active-turn token is derived from.
    sidecar[2]["_active_turn_token"] = token
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, list(sidecar))

    import api.models as models

    s = models.get_session("cont-child")
    s.active_stream_id = "stream-1"
    s.pending_started_at = 1700000000.0
    # Keep the stream "alive" so the GET path does not clear the pending turn
    # before it can be projected, and so the active stream is reported.
    monkeypatch.setitem(api.config.STREAMS, "stream-1", object())
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {"stream-1"})

    held = _full_transcript_get(routes)
    delivered = held.get("messages") or []
    assert len(delivered) == 4
    # The public form kept the marker the delivered projection added …
    assert delivered[2].get("_active_turn_user") is True, delivered[2]
    # … and the rows the browser sees are the rows the proof hashed.
    assert _independent_digest(delivered) == held["prefix_fp"], (
        "the response must deliver exactly the rows its proof hashed"
    )


# ── D + E. the proof covers the held range; the work stays bounded ──────────


def test_the_append_proof_covers_the_held_prefix_not_the_new_rows(monkeypatch, tmp_path):
    """D + E: the verification digest covers the client's HELD range, and the
    projection stops at the response's coverage.

    The client holds [0, 600) of a 603-row document; the append (msg_limit=2)
    delivers rows 600-601, so the coverage ends at 602 — one row short of the
    document. The proof must be re-derived over [0, 600) (the held range), NOT
    over [0, 602) (which would refuse every append that has new rows), and the
    projection must not have walked row 602 at all (#7899): mutating a row
    beyond the coverage moves neither the verification nor the response.
    """
    sidecar = _sidecar_rows(600)
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, list(sidecar))
    held = _full_transcript_get(routes)
    held_fp = held["prefix_fp"]

    def _arrive(last_row_content):
        new_rows = [
            {"role": "assistant", "content": f"new-{i}", "timestamp": 1700000600 + i}
            for i in range(2)
        ]
        new_rows.append(
            {"role": "assistant", "content": last_row_content, "timestamp": 1700000602}
        )
        _rewrite_state_db(tmp_path, list(sidecar) + new_rows)

    _arrive("new-2")
    calls = _counting_projections(monkeypatch)
    proofs = _captured_proofs(monkeypatch)
    session = _append_get_limited(
        msg_after=599, anchor_row=sidecar[599], prefix_fp=held_fp, msg_limit=2
    )
    assert session.get("_append_only") is True, session.get("_append_only")
    # D: the proof covered [0, 600) — the held range — not [0, 602).
    assert [p["count"] for p in proofs] == [600], (
        "the append proof must be re-derived over the client's held range, "
        f"got {[p['count'] for p in proofs]}"
    )
    # E: the projection stopped at the coverage end (602 of the 603 rows).
    assert calls == [602], (
        f"the projection must cover [0, 602) — the response's coverage — got {calls}"
    )
    accepted_fp = session["prefix_fp"]
    assert accepted_fp

    # A row beyond the coverage is not the proof's business: mutating it
    # changes neither the verification nor the published proof.
    _arrive("new-2-rewritten-beyond-the-coverage")
    calls = _counting_projections(monkeypatch)
    session_again = _append_get_limited(
        msg_after=599, anchor_row=sidecar[599], prefix_fp=held_fp, msg_limit=2
    )
    assert session_again.get("_append_only") is True
    assert session_again["prefix_fp"] == accepted_fp
    assert session_again.get("messages") == session.get("messages")
    assert calls == [602]


# ── C. a stale proof still fails closed ─────────────────────────────────────


def test_a_stale_proof_refuses_and_answers_with_the_full_transcript(monkeypatch, tmp_path):
    """C: a non-destructive edit of a held row (no generation bump) still
    refuses the append, and the refusal answers with the authoritative full
    transcript carrying the edit.

    The refusal path also stays at one projection: the discarded append
    projection already covered the whole document (nothing new arrived), so it
    IS the full response's projection and is re-used instead of recomputed.
    """
    import api.models as models

    sidecar = _sidecar_rows(601)
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, [])
    held = _full_transcript_get(routes)
    held_fp = held["prefix_fp"]
    assert len(held["messages"]) == 601

    # A durable rewrite of row 500 with no generation bump: the anchor row and
    # the generation both still match, so only the proof can see it.
    s = models.get_session("cont-child")
    rewritten = dict(s.messages[500])
    rewritten["content"] = "p500-rewritten"
    s.messages[500] = rewritten
    s.save(touch_updated_at=False, skip_index=True)

    calls = _counting_projections(monkeypatch)
    seen = _captured_window_projections(monkeypatch)
    session = _append_get(
        routes, msg_after=600, anchor_row=sidecar[600], prefix_fp=held_fp
    )
    assert session.get("_append_only") is not True, (
        "a non-destructive edit of a held row must refuse the append — the "
        "open tab would keep the stale row"
    )
    assert session.get("message_count") == 601
    delivered = session.get("messages") or []
    assert len(delivered) == 601
    assert delivered[500]["content"] == "p500-rewritten", delivered[500]
    # ... and it cost exactly one projection (the discarded one was re-used).
    assert calls == [601], f"the refusal must project once, got {calls}"
    # The refusal's own proof describes the full response's coverage.
    _window, _offset, _coverage_end, rows, cards = seen[-1]["result"]
    assert session["prefix_fp"] == _prefix_fingerprint_over_projection(rows, cards)


# ── the documented boundary: a duplicate-ref arrival ────────────────────────


def test_duplicate_ref_arrival_refuses_and_redelivers_the_held_row(monkeypatch, tmp_path):
    """The documented consequence of re-scoping: fail-closed, and correctly so.

    Scene hydration resolves an assistant ref that occurs more than once in
    the COVERED range by index instead of by ref. Row 598's ref is unique in
    the client's held range [0, 600) — its proof was minted with the saved
    scene attached — but a row that arrives inside the response's coverage
    duplicates that ref, so the coverage-scoped projection no longer attaches
    the scene to row 598. The digest stops matching the echo and the append is
    REFUSED, and the authoritative full fetch re-delivers row 598 in the
    server's current form — without the scene.

    This is not a false refusal: the server's current rendering of the held
    row really did change (the same shape as the active-turn boundary). The
    evidence at the end shows the held prefix is byte-identical under the
    narrow scope, i.e. the previous shape — a second, held-range projection —
    would have ACCEPTED this append and left the client rendering a row the
    server no longer renders that way.
    """
    import api.models as models

    sidecar = _sidecar_rows(
        600,
        scene_row=(598, {"role": "assistant", "content": "scene answer", "timestamp": 1700000598}),
    )
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, [])

    s = models.get_session("cont-child")
    # A scene bound to row 598 BY REF whose final answer does not match the
    # row's own text: the by-ref match attaches it, the by-index fallback (the
    # ambiguity resolution) refuses it.
    s.anchor_activity_scenes = {
        "ref:598": {
            "version": "anchor_activity_scene_record_v1",
            "message_index": 598,
            "message_ref": _assistant_anchor_scene_message_ref(sidecar[598]),
            "stream_id": "",
            "scene": {
                "mode": "compact_worklog",
                "final_answer": "unrelated final answer",
                "activity_rows": [
                    {"role": "thinking", "text": "saved reasoning", "order": 0}
                ],
            },
            "updated_at": 1700000001,
        }
    }

    # The establishing load: row 598's ref is unique in [0, 600), so the scene
    # is attached and the proof covers it.
    held = _full_transcript_get(routes)
    held_fp = held["prefix_fp"]
    assert held_fp and len(held["messages"]) == 600
    assert held["messages"][598].get("_anchor_activity_scene"), (
        "premise: the establishing load must deliver row 598 with the scene"
    )

    # The duplicate arrives: an assistant row with the same ref payload
    # (whitespace-normalised content + timestamp) as row 598, appended to the
    # sidecar so both rows carry the same raw timestamp shape.
    s.messages.append(
        {"role": "assistant", "content": "scene answer", "timestamp": 1700000598}
    )
    s.save(touch_updated_at=False, skip_index=True)

    calls = _counting_projections(monkeypatch)
    seen = _captured_window_projections(monkeypatch)
    session = _append_get(
        routes, msg_after=599, anchor_row=sidecar[599], prefix_fp=held_fp
    )
    # (a) fail-closed: the append is refused ...
    assert session.get("_append_only") is not True, (
        "a duplicate-ref row arrived inside the coverage and moved the held "
        "row's scene — the append must be refused, not accepted against a "
        "prefix the server now renders differently"
    )
    assert session.get("message_count") == 601
    delivered = session.get("messages") or []
    assert len(delivered) == 601
    # ... and correctly so: the server's current rendering of row 598 carries
    # no scene (the duplicate made the ref ambiguous and the index fallback
    # does not match), while the client's screen still shows the old one.
    assert not delivered[598].get("_anchor_activity_scene"), delivered[598]
    assert delivered[600]["content"] == "scene answer"
    # One projection even on this refusal path (the discarded one was re-used).
    assert calls == [601], f"the refusal must project once, got {calls}"

    # Evidence: under the NARROW scope the held prefix is byte-identical — the
    # held-range projection the previous shape verified with still matches the
    # echo. The refusal comes from the coverage-scoped hydration alone.
    merged = seen[-1]["messages"]
    inputs = seen[-1]["inputs"]
    narrow_rows, narrow_cards = _display_prefix_projection(merged, 600, inputs=inputs)
    assert narrow_rows[598].get("_anchor_activity_scene"), (
        "premise: the narrow scope must still attach the scene to row 598"
    )
    assert _prefix_fingerprint_over_projection(narrow_rows, narrow_cards) == held_fp, (
        "the held prefix must be byte-identical under the narrow scope — the "
        "refusal is the coverage-scoped scene resolution, nothing else"
    )
