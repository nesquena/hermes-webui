"""#7925 by-construction: ``prefix_fp`` hashes the DELIVERED projection.

The 2026-10-31 re-gate made the prefix digest cover the whole delivered
projection of a covered row by *approximating* it from outside: four
out-of-row "seeds" (``redact=``, ``active_turn=``, ``owned_tool_calls=`` and a
scene-hydration pass over the pre-transform rows) were mixed in before the
rows, and the rows themselves were hashed raw. Each seed is a second,
independent implementation of a transform the delivery already performs, so
the proof and the delivered rows could drift apart — and one of them (the
scene hydration) could not even see a scene-free session's tool cards.

The digest now hashes the projection itself: scene hydration, internal-field
stripping, credential redaction and the active-turn marking — the very
function ``redact_session_data`` uses to build the response body — plus the
covered prefix's own session-level tool cards in their delivered form. There
is no seed left to drift.

This file pins the reviewer's acceptance criteria end to end. Every test is
RED-before on the two shapes that could still pass them:

* the pre-#7925-re-gate digest (raw rows, no out-of-row input at all) — the
  ``/tmp`` baseline built from ``HEAD``;
* the seeded digest (this branch's parent), where the proof moved without any
  delivered row moving.

Transport-layer clipping (``_messages_for_limited_payload``) is deliberately
outside the projection: it bounds the payload, it does not change what a row
IS, and whether a row is clipped depends on which window it lands in — the
same row must hash identically whether it arrives clipped in one window or
whole in another. The client keeps a clipped row's range out of the fast path.
"""
from __future__ import annotations

import json

from api.helpers import _public_message_projection  # noqa: F401  (re-exported for the suite)
from api.routes import (
    _PREFIX_FP_DIGEST_TAG,
    _display_prefix_fingerprint,
    _display_prefix_projection,
)

from tests.test_append_only_fast_path_server import (  # noqa: E402
    _append_get,
    _credential_row,
    _full_transcript_get,
    _make_rows,
    _mid_request_save_seam,
    _scene_record,
    _state_db_only_reply,
    _warm_cache_append_harness,
    _warm_display_cache,
)


def _independent_digest(projected_rows) -> str:
    """Recompute the digest from DELIVERED rows alone, from the documented
    format, without going through ``api.routes``.

    This is the constructive half of "by construction": if the digest's input
    is the projection of the rows the response serializes, then the digest can
    be re-derived from the response body with nothing else. A seeded digest
    cannot be re-derived this way at all (``redact=`` / ``active_turn=`` /
    ``owned_tool_calls=`` are not visible in the delivered bytes), and a digest
    over pre-transform rows re-derives to a different value.
    """
    import hashlib

    digest = hashlib.blake2b(digest_size=20)
    digest.update(_PREFIX_FP_DIGEST_TAG)
    digest.update(b"coverage=" + str(len(projected_rows)).encode("ascii") + b"\x1f")
    for row in projected_rows:
        digest.update(
            json.dumps(
                row,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8", "surrogatepass")
        )
        digest.update(b"\x1f")
    return digest.hexdigest()


def _sidecar_with_credential(n=601):
    rows = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(n)
    ]
    rows[500]["content"] = _credential_row()
    return rows


# ── (a) the controlled redaction schedule: True → False → True ─────────────


def test_redaction_policy_schedule_true_false_true_refuses_the_append(monkeypatch, tmp_path):
    """#7925 acceptance (a): the reviewer's controlled schedule.

    The client's view is established with the redaction policy ON, so the proof
    it holds covers the MASKED projection of its prefix. The policy then flips
    OFF: the response must not deliver unmasked text under a True-policy proof,
    so the append is REFUSED and the authoritative full fetch arrives unmasked
    under an OFF-policy proof. Flipping back to ON proves the proof tracks the
    policy in both directions — the same echo is accepted again and the rows
    arrive masked.

    RED-before: with the rows hashed raw and no seed, a policy flip moves
    nothing, the append is ACCEPTED and the open tab keeps unmasked rows under
    a proof minted under the ON policy.
    """
    import api.config

    sidecar = _sidecar_with_credential()
    state_db = sidecar + [_state_db_only_reply()]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": True})
    _warm_display_cache(routes)  # the load that establishes the client's view
    held_fp = captured["payload"]["session"]["prefix_fp"]
    assert held_fp
    # Premise: the establishing load really delivered the masked row.
    warm_rows = captured["payload"]["session"]["messages"] or []
    assert _credential_row() not in json.dumps(warm_rows), (
        "the establishing load delivered an unmasked credential with the "
        "policy ON — the fixture cannot exercise the schedule"
    )

    # ── the policy flips OFF ──
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": False})
    off = _append_get(
        routes, msg_after=601, anchor_row=_state_db_only_reply(), prefix_fp=held_fp
    )
    assert off.get("_append_only") is not True, (
        "the policy flipped OFF and the append was still accepted — the open "
        "tab would keep unmasked rows under a proof minted under the ON "
        "policy (the reviewer's controlled schedule)"
    )
    off_rows = off.get("messages") or []
    assert len(off_rows) == 602, off_rows[:2]
    assert _credential_row() in json.dumps(off_rows), (
        "the OFF-policy authoritative full fetch must deliver the unmasked row"
    )
    assert off["prefix_fp"] != held_fp, (
        "the OFF-policy proof must differ from the ON-policy one the client holds"
    )

    # ── the policy flips back ON ──
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": True})
    on = _append_get(
        routes, msg_after=601, anchor_row=_state_db_only_reply(), prefix_fp=held_fp
    )
    assert on.get("_append_only") is True, (
        "the policy is back to the one the proof was minted under, so the "
        "append must be accepted again — the proof has to track the policy in "
        "both directions, not just refuse once"
    )
    on_rows = on.get("messages") or []
    assert _credential_row() not in json.dumps(on_rows), (
        "the ON-policy append delivered an unmasked credential"
    )


# ── (b) the digest's hash input IS the response body's projection ───────────


def test_prefix_fingerprint_hashes_exactly_the_delivered_projection(monkeypatch, tmp_path):
    """#7925 acceptance (b): constructive assertion.

    A full load's coverage is its whole window, so the digest can be recomputed
    from the response's own ``messages`` field using nothing but the documented
    format — and it must equal ``prefix_fp`` byte for byte. The fixture carries
    a credential row (so the redaction transform is inside the hash input) and
    no session-level cards (so the row serialization is the whole input).

    RED-before: the seeded digest mixes ``redact=`` / ``active_turn=`` /
    ``owned_tool_calls=`` into the hash, none of which is derivable from the
    delivered bytes, so the recomputation cannot match; a digest over
    pre-transform rows re-derives to a different value again.
    """
    import api.config

    sidecar = _sidecar_with_credential()
    state_db = sidecar + [_state_db_only_reply()]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": True})
    session = _full_transcript_get(routes)
    delivered = session.get("messages") or []
    assert len(delivered) == 602, delivered[:2]
    # Premise: the credential really is masked in the delivered bytes, so the
    # redaction transform is part of what the digest hashed.
    assert _credential_row() not in json.dumps(delivered)
    assert _independent_digest(delivered) == session["prefix_fp"], (
        "the digest is not the hash of the delivered projection — it must be "
        "re-derivable from the response body's own bytes"
    )


# ── (c) an anchor-scene save that lands mid-GET ─────────────────────────────


def test_mid_request_scene_save_publishes_a_proof_of_the_delivered_rows(monkeypatch, tmp_path):
    """#7925 acceptance (c): a scene save inside the GET handler.

    The save lands after the response rows have been projected (the
    ``_active_stream_ids`` seam). The published proof must describe the rows
    THIS response delivers, so the follow-up append is refused and the
    authoritative full document — worklog included — arrives.

    The by-construction half: the paused response's ``prefix_fp`` is exactly
    the digest of the rows it delivered, recomputed from the response's own
    ``messages``. A proof of the post-save inputs over pre-save rows is not
    expressible, because the projection is a pure function of the rows and the
    single input snapshot.

    RED-before: the mint re-read ``s.anchor_activity_scenes`` after the
    hydration and published a digest of the NEW inputs over the OLD rows; the
    recomputation from the delivered (pre-save) rows then cannot match, and the
    follow-up append is accepted with an empty window.
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
    # By construction: the proof describes exactly those delivered rows.
    assert _independent_digest(held["messages"]) == held_fp, (
        "the paused GET published a proof that is not the hash of the rows it "
        "delivered — the mid-request save moved the proof, not the rows"
    )

    session = _append_get(
        routes, msg_after=601, anchor_row=_state_db_only_reply(), prefix_fp=held_fp
    )
    assert session.get("_append_only") is not True, (
        "the mid-request save published a digest of the NEW inputs over the "
        "OLD rows, so the empty append was accepted and the saved scene never "
        "reaches the open tab"
    )
    assert session.get("message_count") == 602
    delivered = session.get("messages") or []
    assert len(delivered) == 602
    assert any(m.get("_anchor_activity_scene") for m in delivered), (
        "the authoritative full fetch must carry the saved worklog — the save "
        "really landed, so the reload has to correct the held rows"
    )


# ── (d) a tool-card edit on a scene-free session ────────────────────────────


def test_scene_free_tool_card_edit_refuses_the_append(monkeypatch, tmp_path):
    """#7925 acceptance (d): no saved scene, an older card's summary rewritten.

    The digest covers the covered prefix's own session-level tool cards in
    their DELIVERED form (the response's ``tool_calls`` field for that prefix),
    so a card summary edit on a row the client already holds moves the proof
    and the append is refused — the authoritative full fetch re-delivers the
    rewritten card.

    RED-before: without the cards in the hash input the edit moves nothing, the
    append is accepted, the window carries no cards and the open tab keeps the
    stale snippet forever.
    """
    sidecar = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    state_db = sidecar + [_state_db_only_reply()]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)
    _warm_display_cache(routes)
    held_fp = captured["payload"]["session"]["prefix_fp"]
    assert held_fp

    import api.models as models

    s = models.get_session("cont-child")
    assert not getattr(s, "anchor_activity_scenes", None)  # scene-free session
    # An in-process edit only: no sidecar write, so every stat-derived key is
    # unchanged and only the digest can see it.
    s.tool_calls = [
        {"assistant_msg_idx": 151, "id": "t1", "tool_name": "Bash", "result": "rewritten"}
    ]

    session = _append_get(
        routes, msg_after=601, anchor_row=_state_db_only_reply(), prefix_fp=held_fp
    )
    assert session.get("_append_only") is not True, (
        "an older card's summary changed on a scene-free session and the "
        "append was still accepted — the window carries no cards, so the "
        "client keeps the stale snippet with no way to refresh it"
    )
    assert session.get("message_count") == 602
    cards = session.get("tool_calls") or []
    assert [c.get("result") for c in cards] == ["rewritten"], cards

    # The cards are in the hash input as their DELIVERED form: the projection's
    # card output moves with the edit, so the digest has to move with it.
    rows = _make_rows(10)
    before = _display_prefix_projection(
        rows, 10, tool_calls=[{"assistant_msg_idx": 3, "id": "t1", "result": "one"}]
    )
    after = _display_prefix_projection(
        rows, 10, tool_calls=[{"assistant_msg_idx": 3, "id": "t1", "result": "two"}]
    )
    assert before[1] and after[1] and before[1] != after[1], (
        "the covered prefix's own tool cards must be part of the delivered "
        "projection — a card edit has to be visible in what the digest hashes"
    )
    # A card owned by an appended row stays out, or every tool-using turn would
    # refuse its own append.
    appended = _display_prefix_projection(
        rows, 10, tool_calls=[{"assistant_msg_idx": 10, "id": "t9", "result": "new"}]
    )
    assert appended[1] == [], appended[1]


# ── (e) the active-turn token changes ───────────────────────────────────────


def test_active_turn_boundary_refuses_the_append(monkeypatch, tmp_path):
    """#7925 acceptance (e): the active-turn token moves.

    The client's view is established while a turn is active, so the turn's own
    user row is delivered marked ``_active_turn_user: true``. The turn boundary
    then moves the token: the row is no longer marked, the held proof no longer
    describes it, and the append is refused so the authoritative full fetch
    re-delivers the row in its new form.

    RED-before: without the marking in the hash input the token move is
    invisible, the append is accepted and the open tab keeps a row the server
    now renders differently.
    """
    import api.config
    from api.process_event_utils import build_active_turn_token

    token = build_active_turn_token("stream-1", 1700000000.0)
    assert token
    sidecar = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"p{i}",
         "timestamp": 1700000000 + i}
        for i in range(601)
    ]
    # The turn's own user row carries the token the production settlement
    # helper stamps, so the session's active-turn token decides its marking.
    sidecar[600]["_active_turn_token"] = token
    state_db = sidecar + [_state_db_only_reply()]
    routes, captured = _warm_cache_append_harness(monkeypatch, tmp_path, sidecar, state_db)

    import api.models as models

    s = models.get_session("cont-child")
    s.active_stream_id = "stream-1"
    s.pending_started_at = 1700000000.0
    # Keep the stream "alive" so the GET path does not clear the pending turn
    # before it can be projected, and so the active stream is reported.
    monkeypatch.setitem(api.config.STREAMS, "stream-1", object())
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {"stream-1"})

    held = _full_transcript_get(routes)
    held_fp = held.get("prefix_fp")
    assert held_fp
    delivered = held.get("messages") or []
    assert len(delivered) == 602
    # Premise: the turn's user row really is delivered marked.
    assert delivered[600].get("_active_turn_user") is True, delivered[600]
    assert _independent_digest(delivered) == held_fp, (
        "the proof must be the hash of the delivered rows — the marked row "
        "included"
    )

    # The turn boundary moves the token: the row is no longer the active one.
    s.pending_started_at = 1700000900.0
    session = _append_get(
        routes, msg_after=601, anchor_row=_state_db_only_reply(), prefix_fp=held_fp
    )
    assert session.get("_append_only") is not True, (
        "the active-turn token moved and the append was still accepted — the "
        "open tab keeps a user row the server no longer renders as the active "
        "turn's"
    )
    redelivered = session.get("messages") or []
    assert redelivered[600].get("_active_turn_user") is None, redelivered[600]


# ── (f) the format tag is domain-separated from the legacy format ───────────


def test_prefix_fingerprint_format_tag_is_domain_separated(monkeypatch):
    """#7925 acceptance (f): an older format's digest can never compare equal.

    The domain-separation tag rides inside the hash, so bumping it (``/2`` →
    ``/3``, the format change that replaced the seeds with the delivered
    projection) makes every digest minted by an older build compare unequal:
    a format change fails closed instead of silently reusing a stale proof.

    RED-before: the seeded digest carries no such constant (the tag was an
    inline literal), so this test cannot even import it.
    """
    rows = _make_rows(50)
    current = _display_prefix_fingerprint(rows, 50)
    assert current
    assert _PREFIX_FP_DIGEST_TAG == b"prefix_fp/3\x1f", (
        "the digest format changed — the domain-separation tag must be bumped "
        "so an older build's proof can never compare equal to a new one"
    )
    # Re-mint under the legacy tag: it must never collide with the current one.
    # monkeypatch (Greptile 2026-11-08, P2): the legacy tag used to be restored
    # in a ``finally`` and then set AGAIN for the closing assertion with no
    # restore at all, so every later test in the process minted under the
    # legacy format and results depended on test order. monkeypatch undoes the
    # swap however this test exits.
    import api.routes as routes

    monkeypatch.setattr(routes, "_PREFIX_FP_DIGEST_TAG", b"prefix_fp/2\x1f")
    legacy = _display_prefix_fingerprint(rows, 50)
    assert legacy != current, (
        "a digest minted under the legacy format tag compared equal to a "
        "new-format one — the tag is not inside the hash input"
    )
    # Same rows, same everything but the tag: the inequality above is evidence
    # the tag is bound into the hash rather than a coincidence of the row bytes.
    assert _display_prefix_fingerprint(rows, 50) == legacy


# ── bounded work (#7899): the proof never scans past its coverage ───────────


class _CountingRows:
    """A row source that records how many rows were actually walked."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.walked = 0

    def __iter__(self):
        for row in self._rows:
            self.walked += 1
            yield row


def test_prefix_projection_walks_only_the_covered_prefix():
    """#7899 bounded work: the proof's hashing work is bounded by its coverage.

    The digest's input is the delivered projection of ``[0, coverage_end)`` —
    the client's held prefix, the widest range an append can vouch for — so the
    projection must not walk, materialize or hash rows beyond it. A windowed
    GET's proof cost is therefore proportional to the prefix it covers, never
    to the transcript: a 2001-row document asked for ``[0, 2000)`` walks
    exactly 2000 rows, and rows at/after the coverage end cannot move it.

    RED-before: the seeded digest materialized ``list(messages)`` before
    slicing, so it walked the whole document on every request.
    """
    rows = _make_rows(2001)
    source = _CountingRows(rows)
    fp = _display_prefix_fingerprint(source, 2000)
    assert fp
    assert source.walked == 2000, (
        f"the projection walked {source.walked} rows for a [0, 2000) coverage — "
        "a hidden full-document scan (the #7899 rejection)"
    )
    # Rows beyond the coverage end are not the proof's business.
    mutated = [dict(r) for r in rows]
    mutated[2000] = {"role": "user", "content": "x" * 4096, "timestamp": 1}
    assert _display_prefix_fingerprint(mutated, 2000) == fp
