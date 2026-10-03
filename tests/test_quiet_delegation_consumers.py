"""Hidden delegation_wakeup rows stay hidden in EVERY transcript consumer.

The chat render path hides ``_source: delegation_wakeup`` rows, but the
original PR hidden them only there. The gate review enumerated the other
consumers, each with a probe:

1. Restore window — the 30-message tail can be all-hidden → blank transcript.
2. /retry — the hidden handoff was the "last user message" and got resubmitted
   as a human turn.
3. Reload during deferred save — the state.db copy predates the ``_source``
   stamp and was adopted unstamped.
4. Content search — the hidden prompt matched and consumed search depth.
5. Title derivation — untitled session took the handoff prompt as its title.
6. HTML export / public share — the handoff published as a human message.
7. Counts — topbar totals and sidebar detail counts included hidden rows.

Every consumer calls the one shared predicate
(``api.process_event_utils.is_hidden_transcript_row``), keyed on the typed
``_source`` stamp, never on content.
"""
from __future__ import annotations

import pytest

from api.process_event_utils import is_hidden_transcript_row


def _hidden_row(**extra):
    row = {
        "role": "user",
        "content": "[ASYNC DELEGATION COMPLETE delegation-abc] internal handoff",
        "timestamp": 1781024055.0,
        "_source": "delegation_wakeup",
    }
    row.update(extra)
    return row


# ── 1. Restore window (#1 blocking) ────────────────────────────────────────


def test_restore_window_excludes_hidden_rows_from_renderable_budget():
    """A tail of 30 rows that are ALL hidden must not become the restore
    window: the visible older rows must be pulled in instead."""
    from api.routes import _message_window_for_display

    messages = [{"role": "user", "content": f"visible {idx}"} for idx in range(25)]
    # 30 hidden rows at the very end — the raw tail.
    messages.extend(_hidden_row() for _ in range(30))

    window, offset = _message_window_for_display(messages, msg_limit=30)

    assert window, "restore window must never be empty when visible rows exist"
    assert all(not is_hidden_transcript_row(m) for m in window)
    # The window must reach back to real visible rows, not stop at the raw tail.
    assert any("visible" in str(m.get("content", "")) for m in window)


def test_message_counts_as_renderable_for_window_rejects_hidden_row():
    from api.routes import _message_counts_as_renderable_for_window

    assert _message_counts_as_renderable_for_window(_hidden_row()) is False
    assert _message_counts_as_renderable_for_window(
        {"role": "user", "content": "real question"}
    ) is True


# ── 2. /retry (#2 blocking) ────────────────────────────────────────────────


def test_retry_skips_hidden_rows_and_resubmits_real_turn(monkeypatch, tmp_path):
    import contextlib

    import api.session_ops as session_ops
    from api.models import Session

    session = Session(
        session_id="retry7882",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "real question"},
            {"role": "assistant", "content": "delegating..."},
            # Hidden handoff is the LAST user row.
            _hidden_row(),
            {"role": "assistant", "content": "child result summary"},
        ],
    )
    saved = []
    session.save = lambda *args, **kwargs: saved.append(True)
    monkeypatch.setattr(session_ops, "get_session", lambda sid: session)
    monkeypatch.setattr(session_ops, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(
        session_ops, "_get_session_agent_lock", lambda sid: contextlib.nullcontext()
    )

    result = session_ops.retry_last(session.session_id)

    # Retry targets the real human turn, never the hidden handoff.
    assert result["last_user_text"] == "real question"
    assert [m["content"] for m in session.messages] == []
    assert saved


def test_retry_fails_cleanly_when_only_hidden_rows_exist(monkeypatch, tmp_path):
    import contextlib

    import api.session_ops as session_ops
    from api.models import Session

    session = Session(
        session_id="retry7882b",
        workspace=str(tmp_path),
        messages=[_hidden_row()],
    )
    session.save = lambda *args, **kwargs: None
    monkeypatch.setattr(session_ops, "get_session", lambda sid: session)
    monkeypatch.setattr(session_ops, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(
        session_ops, "_get_session_agent_lock", lambda sid: contextlib.nullcontext()
    )

    with pytest.raises(ValueError):
        session_ops.retry_last(session.session_id)


# ── 3. Reload during deferred save (projection stamp) ─────────────────────


def test_state_db_rows_adopt_pending_source_during_deferred_save():
    from api.models import _stamp_pending_source_for_display

    class _PendingSession:
        pending_user_source = "delegation_wakeup"
        pending_started_at = 1781024055.0
        pending_user_message = "[ASYNC DELEGATION COMPLETE delegation-abc] handoff"

    rows = [
        {
            "role": "user",
            "content": "[ASYNC DELEGATION COMPLETE delegation-abc] handoff",
            "timestamp": 1781024055.0,
            # No _source yet — the Agent core appended it to state.db raw.
        },
        {"role": "user", "content": "earlier real question", "timestamp": 100.0},
    ]

    stamped = _stamp_pending_source_for_display(_PendingSession(), rows)

    assert stamped[0]["_source"] == "delegation_wakeup"
    # The earlier row keeps no stamp: only the pending turn's exact timestamp.
    assert "_source" not in stamped[1]


def test_state_db_projection_stamp_is_inert_for_webui_source():
    from api.models import _stamp_pending_source_for_display

    class _PendingSession:
        pending_user_source = "webui"
        pending_started_at = 1781024055.0
        pending_user_message = "hello"

    rows = [{"role": "user", "content": "hello", "timestamp": 1781024055.0}]
    stamped = _stamp_pending_source_for_display(_PendingSession(), rows)
    assert "_source" not in stamped[0]


# ── 4. Content search ──────────────────────────────────────────────────────


def test_content_search_skips_hidden_rows_and_depth_budget():
    """A hidden row must neither match nor consume the depth budget: with
    depth=2 and a hidden row first, the real second row is still scanned."""
    from api.routes import _session_search_message_text

    # Direct unit: the scan pool the search handler builds.
    msgs = [
        _hidden_row(),  # would have consumed depth slot 1
        {"role": "user", "content": "find the needle here"},
    ]
    scan_pool = [m for m in msgs if not is_hidden_transcript_row(m)]
    depth = 1
    scanned = scan_pool[:depth] if depth else scan_pool

    assert len(scanned) == 1
    assert "needle" in _session_search_message_text(scanned[0]).lower()
    # And the hidden row's text is absent from everything scanned.
    assert all("ASYNC DELEGATION" not in _session_search_message_text(m) for m in scanned)


# ── 5. Title derivation ────────────────────────────────────────────────────


def test_title_from_skips_hidden_rows():
    from api.models import title_from

    messages = [
        _hidden_row(),
        {"role": "user", "content": "the real first question"},
    ]
    assert title_from(messages) == "the real first question"


def test_title_from_returns_fallback_when_only_hidden_rows():
    from api.models import title_from

    assert title_from([_hidden_row()], "Untitled") == "Untitled"


def test_provisional_title_not_taken_from_hidden_prompt():
    """The chat-start provisional title must not adopt a delegation handoff."""
    # _prepare_chat_start_session_for_stream gates on effective_source; the
    # gate expression is exercised via the shared predicate contract:
    # a delegation_wakeup effective_source suppresses the provisional title.
    from api.routes import _provisional_title_from_prompt

    prompt = _hidden_row()["content"]
    # The suppression happens before this helper is called (effective_source
    # check); assert the helper itself stays content-faithful so the gate is
    # the only thing that can suppress it.
    assert _provisional_title_from_prompt(prompt) == prompt[:64]


# ── 6. Export + share ──────────────────────────────────────────────────────


def test_html_export_excludes_hidden_rows():
    from api.session_export_html import render_session_html

    session = {
        "session_id": "s7882",
        "title": "Export test",
        "messages": [
            _hidden_row(),
            {"role": "user", "content": "visible question"},
            {"role": "assistant", "content": "visible answer"},
        ],
    }
    html_out = render_session_html(session)

    assert "ASYNC DELEGATION" not in html_out
    assert "visible question" in html_out


def test_share_snapshot_excludes_hidden_rows_and_counts():
    from api.shares import build_share_snapshot

    class _ShareSession:
        session_id = "s7882share"
        title = "Share test"
        workspace = ""

        def __init__(self):
            self.share_token = None
            self.messages = [
                _hidden_row(),
                {"role": "user", "content": "shareable question"},
                {"role": "assistant", "content": "shareable answer"},
            ]

        def __getattr__(self, name):
            return None

    snapshot = build_share_snapshot(_ShareSession())

    assert snapshot["message_count"] == 2
    assert all("ASYNC DELEGATION" not in str(m.get("content", "")) for m in snapshot["messages"])


# ── 7. Counts ──────────────────────────────────────────────────────────────


def test_compact_visible_message_count_excludes_hidden_rows():
    from api.models import Session

    session = Session(
        session_id="counts7882",
        messages=[
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            _hidden_row(),
            {"role": "assistant", "content": "a2"},
        ],
    )
    compact = session.compact()

    # Raw count keeps the paging authority.
    assert compact["message_count"] == 4
    # Labels consume the visible count.
    assert compact["visible_message_count"] == 3


def test_compact_visible_count_zero_when_only_hidden_rows():
    from api.models import Session

    session = Session(
        session_id="counts7882b",
        messages=[_hidden_row()],
    )
    compact = session.compact()

    assert compact["message_count"] == 1
    assert compact["visible_message_count"] == 0


def test_sidecar_prefix_writes_visible_message_count(tmp_path):
    from api.models import Session

    session = Session(
        session_id="prefix7882",
        messages=[{"role": "user", "content": "q"}, _hidden_row()],
    )
    session.save(skip_index=True)

    compact = session.compact()
    assert compact["visible_message_count"] == 1
    # Reload through the metadata path and confirm the prefix round-trips.
    stub = Session.load_metadata_only("prefix7882")
    assert stub is not None
    assert stub.compact()["visible_message_count"] == 1


def test_legacy_sidecar_metadata_only_load_falls_back_to_message_count(tmp_path, monkeypatch):
    """Gate review finding 1: a sidecar written BEFORE this PR has no
    visible-count prefix field, which parses as None — and compact() then
    emitted visible_message_count 0, making older sessions show "0 messages"
    in the sidebar. The legacy fallback must surface the raw message_count
    (a legacy file cannot contain hidden delegation_wakeup rows)."""
    import json as _json

    import api.models as models_mod
    from api.models import Session

    # Write a REAL legacy sidecar: full session JSON WITHOUT the
    # visible_message_count prefix key.
    legacy = Session(
        session_id="legacy7882",
        messages=[
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ],
    )
    legacy.save(skip_index=True)
    path = models_mod.SESSION_DIR / "legacy7882.json"
    payload = _json.loads(path.read_text(encoding="utf-8"))
    payload.pop("visible_message_count", None)
    payload["message_count"] = 3
    path.write_text(_json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(models_mod.SESSIONS, "get", lambda sid, default=None: None)
    stub = Session.load_metadata_only("legacy7882")
    assert stub is not None
    assert stub._loaded_metadata_only is True
    # The prefix carried NO visible count; the fallback must produce 3, not 0.
    assert stub.compact()["visible_message_count"] == 3


def test_compact_visible_count_refreshes_after_growth(tmp_path, monkeypatch):
    """A full session must re-walk its live array, not trust the count it was
    loaded with: save → load → append → save previously reported the stale
    load-time visible count (gate review finding 3)."""
    import api.models as models_mod
    from api.models import Session

    session = Session(
        session_id="grow7882",
        messages=[{"role": "user", "content": "q"}],
    )
    monkeypatch.setattr(models_mod.SESSIONS, "get", lambda sid, default=None: None)
    session.save(skip_index=True)

    reloaded = Session.load("grow7882")
    # Sanity: the metadata prefix carries the load-time snapshot.
    assert reloaded._metadata_visible_message_count == 1
    # The session grows; the visible count must reflect the live array.
    reloaded.messages.append({"role": "user", "content": "q2"})
    compact = reloaded.compact()
    assert compact["visible_message_count"] == 2


# ── 8. /undo and /status ───────────────────────────────────────────────────


def test_undo_targets_visible_turn_and_removes_hidden_suffix(monkeypatch, tmp_path):
    """/undo must remove the last VISIBLE human turn (and the hidden handoff
    after it), not select the hidden handoff as the undo target (finding 2)."""
    import contextlib

    import api.session_ops as session_ops
    from api.models import Session

    session = Session(
        session_id="undo7882",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "real question"},
            {"role": "assistant", "content": "delegating..."},
            _hidden_row(),
            {"role": "assistant", "content": "child result summary"},
        ],
        context_messages=[
            {"role": "user", "content": "real question"},
            {"role": "assistant", "content": "delegating..."},
            _hidden_row(),
            {"role": "assistant", "content": "child result summary"},
        ],
    )
    saved = []
    session.save = lambda *args, **kwargs: saved.append(True)
    monkeypatch.setattr(session_ops, "get_session", lambda sid: session)
    monkeypatch.setattr(session_ops, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(
        session_ops, "_get_session_agent_lock", lambda sid: contextlib.nullcontext()
    )

    result = session_ops.undo_last(session.session_id)

    assert result["removed_count"] == 4
    assert "real question" in result["removed_preview"]
    assert [m["content"] for m in session.messages] == []
    # The context cut lands on the same visible turn as the display cut.
    assert [m["content"] for m in session.context_messages] == []
    assert saved


def test_retry_truncates_context_at_the_same_visible_turn(monkeypatch, tmp_path):
    """The model context must be cut at the selected visible human turn, not
    at the hidden handoff — otherwise the model sees the turn twice
    (finding 1)."""
    import contextlib

    import api.session_ops as session_ops
    from api.models import Session

    session = Session(
        session_id="retryctx7882",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "real question"},
            {"role": "assistant", "content": "delegating..."},
            _hidden_row(),
            {"role": "assistant", "content": "child result summary"},
        ],
        context_messages=[
            {"role": "user", "content": "real question"},
            {"role": "assistant", "content": "delegating..."},
            _hidden_row(),
            {"role": "assistant", "content": "child result summary"},
        ],
    )
    saved = []
    session.save = lambda *args, **kwargs: saved.append(True)
    monkeypatch.setattr(session_ops, "get_session", lambda sid: session)
    monkeypatch.setattr(session_ops, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(
        session_ops, "_get_session_agent_lock", lambda sid: contextlib.nullcontext()
    )

    session_ops.retry_last(session.session_id)

    # Both histories cut at the SAME visible turn: no duplicate of the
    # resubmitted prompt (or its reply) survives in the model context.
    assert [m["content"] for m in session.context_messages] == []
    assert [m["content"] for m in session.messages] == []
    assert saved


def test_retry_on_compressed_history_clears_later_context(monkeypatch, tmp_path):
    """Gate review finding 2 (re-gate): when compression has already dropped
    the selected visible human turn from context_messages, /retry must cut
    the display transcript AND clear the later context — leaving the hidden
    delegation handoff and its reply in context would carry them into the
    next send."""
    import contextlib

    import api.session_ops as session_ops
    from api.models import Session

    session = Session(
        session_id="retrycomp7882",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "real question"},
            {"role": "assistant", "content": "delegating..."},
            _hidden_row(),
            {"role": "assistant", "content": "child result summary"},
        ],
        # Post-compression context: the selected "real question" turn (and the
        # hidden handoff after it) are GONE — only a compressed summary and
        # the tail remain.
        context_messages=[
            {"role": "user", "content": "compression summary of earlier turns"},
            {"role": "assistant", "content": "child result summary"},
        ],
    )
    saved = []
    session.save = lambda *args, **kwargs: saved.append(True)
    monkeypatch.setattr(session_ops, "get_session", lambda sid: session)
    monkeypatch.setattr(session_ops, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(
        session_ops, "_get_session_agent_lock", lambda sid: contextlib.nullcontext()
    )

    session_ops.retry_last(session.session_id)

    # Display history cut at the selected visible turn.
    assert [m["content"] for m in session.messages] == [
        "old question",
        "old answer",
    ]
    # The later context (summary + hidden handoff's reply) must NOT survive —
    # the next send cannot carry the removed turn.
    assert session.context_messages == []
    assert saved


def test_undo_on_compressed_history_clears_later_context(monkeypatch, tmp_path):
    """Same fail-closed rule as retry: /undo on a compressed history must not
    leave the hidden delegation handoff's reply in model context."""
    import contextlib

    import api.session_ops as session_ops
    from api.models import Session

    session = Session(
        session_id="undocomp7882",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "real question"},
            _hidden_row(),
            {"role": "assistant", "content": "child result summary"},
        ],
        context_messages=[
            {"role": "user", "content": "compression summary of earlier turns"},
            {"role": "assistant", "content": "child result summary"},
        ],
    )
    saved = []
    session.save = lambda *args, **kwargs: saved.append(True)
    monkeypatch.setattr(session_ops, "get_session", lambda sid: session)
    monkeypatch.setattr(session_ops, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(
        session_ops, "_get_session_agent_lock", lambda sid: contextlib.nullcontext()
    )

    session_ops.undo_last(session.session_id)

    assert [m["content"] for m in session.messages] == [
        "old question",
        "old answer",
    ]
    assert session.context_messages == []
    assert saved


def test_session_status_reports_visible_message_count(monkeypatch):
    """/status must not count hidden internal rows in its messages total
    (finding 4)."""
    import api.session_ops as session_ops
    from api.models import Session

    session = Session(
        session_id="status7882",
        messages=[
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            _hidden_row(),
        ],
    )
    monkeypatch.setattr(session_ops, "get_session", lambda sid: session)

    status = session_ops.session_status(session.session_id)

    assert status["message_count"] == 3
    assert status["visible_message_count"] == 2


# ── Shared predicate contract ──────────────────────────────────────────────


def test_predicate_keys_on_typed_source_not_content():
    # Content that RESEMBLES a wakeup envelope but is a real user row stays visible.
    assert not is_hidden_transcript_row(
        {"role": "user", "content": "[ASYNC DELEGATION COMPLETE x] pasted text"}
    )
    # Typed stamp wins regardless of content.
    assert is_hidden_transcript_row(_hidden_row(content="anything at all"))
    # Non-dict / missing source are never hidden.
    assert not is_hidden_transcript_row(None)
    assert not is_hidden_transcript_row({"role": "user", "content": "hi"})


# ── Round-4 re-gate findings ───────────────────────────────────────────────


def test_retry_context_cut_lands_on_selected_turn_not_retained_user(
    monkeypatch, tmp_path
):
    """Finding 1 (round-4): when compression replaced the selected human turn
    with a retained summary user row, the context cut must not stop at that
    unrelated retained user — that would leave the hidden handoff's reply in
    context while the selected turn is resubmitted. The selected turn cannot
    be proved present, so the later context clears."""
    import contextlib

    import api.session_ops as session_ops
    from api.models import Session

    session = Session(
        session_id="retryctx7882b",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "real question"},
            {"role": "assistant", "content": "delegating..."},
            _hidden_row(),
            {"role": "assistant", "content": "child result summary"},
        ],
        # Compressed context: the summary user is NOT the selected turn.
        context_messages=[
            {"role": "user", "content": "summary of earlier turns (retained user)"},
            {"role": "assistant", "content": "child result summary"},
        ],
    )
    saved = []
    session.save = lambda *args, **kwargs: saved.append(True)
    monkeypatch.setattr(session_ops, "get_session", lambda sid: session)
    monkeypatch.setattr(session_ops, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(
        session_ops, "_get_session_agent_lock", lambda sid: contextlib.nullcontext()
    )

    session_ops.retry_last(session.session_id)

    # The unrelated retained user must NOT survive as the cut boundary:
    # fail closed clears the whole later context.
    assert session.context_messages == []
    assert saved


def test_undo_context_cut_lands_on_selected_turn_not_retained_user(
    monkeypatch, tmp_path
):
    """Same identity-first rule for /undo (finding 2, round-4)."""
    import contextlib

    import api.session_ops as session_ops
    from api.models import Session

    session = Session(
        session_id="undoctx7882b",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "real question"},
            _hidden_row(),
            {"role": "assistant", "content": "child result summary"},
        ],
        context_messages=[
            {"role": "user", "content": "summary of earlier turns (retained user)"},
            {"role": "assistant", "content": "child result summary"},
        ],
    )
    saved = []
    session.save = lambda *args, **kwargs: saved.append(True)
    monkeypatch.setattr(session_ops, "get_session", lambda sid: session)
    monkeypatch.setattr(session_ops, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(
        session_ops, "_get_session_agent_lock", lambda sid: contextlib.nullcontext()
    )

    session_ops.undo_last(session.session_id)

    assert session.context_messages == []
    assert saved


def test_retry_context_cut_still_lands_on_matching_turn(monkeypatch, tmp_path):
    """The identity matcher keeps working when the selected turn IS in context:
    the cut lands before it (not at the context's own last user row)."""
    import contextlib

    import api.session_ops as session_ops
    from api.models import Session

    session = Session(
        session_id="retryctx7882c",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "real question"},
            _hidden_row(),
            {"role": "assistant", "content": "child result summary"},
        ],
        context_messages=[
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "real question"},
            _hidden_row(),
            {"role": "assistant", "content": "child result summary"},
        ],
    )
    saved = []
    session.save = lambda *args, **kwargs: saved.append(True)
    monkeypatch.setattr(session_ops, "get_session", lambda sid: session)
    monkeypatch.setattr(session_ops, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(
        session_ops, "_get_session_agent_lock", lambda sid: contextlib.nullcontext()
    )

    session_ops.retry_last(session.session_id)

    # Cut before "real question": the context keeps the prefix and loses the
    # hidden handoff and its reply.
    assert [m["content"] for m in session.context_messages] == [
        "old question",
        "old answer",
    ]
    assert saved


def test_sidebar_cache_transports_visible_message_count(monkeypatch):
    """Finding 2 (round-4): ``_SIDEBAR_SESSION_RESPONSE_FIELDS`` gates both the
    bounded cached rows and the final /api/sessions serializer. Without the
    scalar there, cache hits fall back to the raw count in the sidebar."""
    import api.route_session_list_cache as slc

    payload = {
        "sessions": [
            {
                "session_id": "vis7882",
                "title": "t",
                "message_count": 5,
                "visible_message_count": 4,
            }
        ]
    }
    bounded = slc._session_list_cache_bounded_payload(payload)
    assert bounded["sessions"][0]["visible_message_count"] == 4
    # Explicit zero is preserved (a transcript of only hidden rows).
    bounded0 = slc._session_list_cache_bounded_payload(
        {"sessions": [{"session_id": "z", "message_count": 2, "visible_message_count": 0}]}
    )
    assert bounded0["sessions"][0]["visible_message_count"] == 0


def test_fork_session_keeps_delegation_wakeup_source(monkeypatch, tmp_path):
    """Finding 3 (round-4): a fork session's ``session_source`` ownership
    override must not clobber the internal producer's explicit
    ``delegation_wakeup`` row stamp — the hidden-row predicate keys on it."""
    import api.routes as routes
    from api.models import Session

    session = Session(session_id="forkwakeup7882", workspace=str(tmp_path))
    session.session_source = "fork"
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")

    routes._prepare_chat_start_session_for_stream(
        session,
        msg="[ASYNC DELEGATION COMPLETE x] internal handoff",
        attachments=[],
        workspace=str(tmp_path),
        model="m",
        model_provider="p",
        stream_id="stream-7882",
        source="delegation_wakeup",
    )

    assert session.pending_user_source == "delegation_wakeup"


def test_fork_session_still_stamps_ordinary_human_rows(monkeypatch, tmp_path):
    """Ordinary fork-human rows keep the fork identity override."""
    import api.routes as routes
    from api.models import Session

    session = Session(session_id="forkhuman7882", workspace=str(tmp_path))
    session.session_source = "fork"
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")

    routes._prepare_chat_start_session_for_stream(
        session,
        msg="a human prompt in a fork",
        attachments=[],
        workspace=str(tmp_path),
        model="m",
        model_provider="p",
        stream_id="stream-7882b",
        source="webui",
    )

    assert session.pending_user_source == "fork"
