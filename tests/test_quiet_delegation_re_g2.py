"""Round-2 re-gate regression tests for #7882 (2026-10-07 review head ff0aa873).

Four findings, each reproduced by the gate on the rebase-with-no-code-change
head. The four fixes:

1. Fork regeneration after an async delegation was rejected at the row-source
   guard BEFORE the fork ownership proof was consulted (CORE, 403).
2. Id-less display rows missed the compression content-match exception, so
   retry/undo removed the exchange from display but left it in saved model
   context (CORE).
3. A local send promoted the raw count into the visible count — a hidden
   wakeup plus one send reported visible 5 for raw 4 / visible 3 (SILENT).
4. Sidebar state.db growth credited the hidden pending wakeup row as visible
   (SILENT): raw 4 / visible 3 with a pending wakeup reported 4.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from api.process_event_utils import is_hidden_transcript_row

_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

NODE_BIN = shutil.which("node")
_node_tests = pytest.mark.skipif(NODE_BIN is None, reason="node not on PATH")


def _run_node_vm(source: str) -> str:
    if NODE_BIN is None:
        pytest.skip("node not on PATH")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE_BIN, str(script_path)],
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


# ── Finding 1: fork regeneration after async delegation ───────────────────


def _fork_gate(row, *, parent="parent-123", sid="fork-child-7882"):
    from api.session_ops import _selected_regeneration_turn_owned

    session = type(
        "_ForkSession",
        (),
        {
            "read_only": False,
            "session_source": "fork",
            "is_cli_session": False,
            "raw_source": None,
            "source_tag": None,
            "parent_session_id": parent,
            "session_id": sid,
        },
    )()
    return _selected_regeneration_turn_owned(session, row)


def test_fork_gate_accepts_delegation_wakeup_with_matching_child_proof():
    """The re-gate's exact rejected shape: a settled wakeup row keeps
    ``_source: delegation_wakeup`` and carries ``_fork_child_turn`` pointing
    at this fork session. The ownership proof — parent session set and the
    child turn matching the session id — must be the authorization, not the
    row source. This calls the REAL gate (the round-1 test asserted only the
    stamp)."""
    row = {
        "role": "user",
        "content": "[ASYNC DELEGATION COMPLETE d1] internal handoff",
        "_source": "delegation_wakeup",
        "_fork_child_turn": "fork-child-7882",
    }
    assert _fork_gate(row) is True


def test_fork_gate_rejects_wakeup_without_ownership_proof():
    """The wakeup-source exception carries NO authorization by itself: a row
    claiming the wakeup source without a matching ``_fork_child_turn`` stays
    rejected (missing ownership)."""
    no_proof = {
        "role": "user",
        "content": "[ASYNC DELEGATION COMPLETE d1] internal handoff",
        "_source": "delegation_wakeup",
    }
    assert _fork_gate(no_proof) is False


def test_fork_gate_rejects_wakeup_with_foreign_child_turn():
    foreign = {
        "role": "user",
        "content": "[ASYNC DELEGATION COMPLETE d1] internal handoff",
        "_source": "delegation_wakeup",
        "_fork_child_turn": "some-other-session",
    }
    assert _fork_gate(foreign) is False


def test_fork_gate_rejects_wakeup_without_parent_session():
    orphan = {
        "role": "user",
        "content": "[ASYNC DELEGATION COMPLETE d1] internal handoff",
        "_source": "delegation_wakeup",
        "_fork_child_turn": "fork-child-7882",
    }
    assert _fork_gate(orphan, parent=None) is False


def test_fork_gate_still_rejects_unallowed_row_sources():
    """Non-wakeup, non-allowed row sources are unchanged: the exception is
    scoped to ``delegation_wakeup`` only."""
    cli = {"role": "user", "content": "hello", "_source": "cli"}
    assert _fork_gate(cli) is False


# ── Finding 2: id-less display rows match sanitized context copies ────────


@pytest.mark.parametrize("op", ["retry", "undo"])
def test_retry_undo_id_less_display_row_cuts_id_less_context_copy(
    monkeypatch, tmp_path, op
):
    """Round-2 must-fix: the selected display row has NO id (production
    settlement + compression sanitizing strips it), and the context copy is
    id-less too. The content match must still cut before it — the old
    ``row_id is None and target_id is not None`` condition required the
    TARGET to carry an id, so the removed question and answer survived in
    saved model context."""
    import contextlib

    import api.session_ops as session_ops
    from api.models import Session

    session = Session(
        session_id=f"{op}idless7882",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "old question", "timestamp": 1000.0},
            {"role": "assistant", "content": "old answer", "timestamp": 1001.0},
            # Selected display row: id-less, carries its ORIGINAL timestamp —
            # production settlement stamps wall-clock times on eager rows and
            # the compression writeback re-stamps only the CONTEXT copies.
            {"role": "user", "content": "real question", "timestamp": 1002.0},
            {
                "role": "user",
                "content": "[ASYNC DELEGATION COMPLETE d2] internal handoff",
                "timestamp": 1781024055.0,
                "_source": "delegation_wakeup",
            },
            {"role": "assistant", "content": "child result summary"},
        ],
        # Manual-compression context: sanitized id-less copies with FRESH
        # re-stamped timestamps that disagree with the display rows'.
        context_messages=[
            {"role": "user", "content": "old question", "timestamp": 1781024000.0},
            {"role": "assistant", "content": "old answer", "timestamp": 1781024001.0},
            {"role": "user", "content": "real question", "timestamp": 1781024002.0},
            {
                "role": "user",
                "content": "[ASYNC DELEGATION COMPLETE d2] internal handoff",
                "timestamp": 1781024003.0,
                "_source": "delegation_wakeup",
            },
            {"role": "assistant", "content": "child result summary", "timestamp": 1781024004.0},
        ],
    )
    saved = []
    session.save = lambda *args, **kwargs: saved.append(True)
    monkeypatch.setattr(session_ops, "get_session", lambda sid: session)
    monkeypatch.setattr(session_ops, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(
        session_ops, "_get_session_agent_lock", lambda sid: contextlib.nullcontext()
    )

    getattr(session_ops, f"{op}_last")(session.session_id)

    # The content match cuts BEFORE the selected id-less turn: the removed
    # question/answer (and the hidden handoff + reply after it) must NOT
    # survive in model context.
    assert [m["content"] for m in session.context_messages] == [
        "old question",
        "old answer",
    ]
    assert saved


@pytest.mark.parametrize("op", ["retry", "undo"])
def test_retry_undo_different_id_still_vetoes_same_text(monkeypatch, tmp_path, op):
    """The different-ID veto is preserved: when BOTH rows carry ids and they
    differ, the same-text later row must not capture the cut (greptile P1,
    unchanged by the round-2 fix)."""
    import contextlib

    import api.session_ops as session_ops
    from api.models import Session

    session = Session(
        session_id=f"{op}veto7882",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "same text", "id": "msg-display-1"},
            {
                "role": "user",
                "content": "[ASYNC DELEGATION COMPLETE d3] handoff",
                "timestamp": 1781024056.0,
                "_source": "delegation_wakeup",
            },
            {"role": "assistant", "content": "child result summary"},
        ],
        context_messages=[
            {"role": "user", "content": "same text", "id": "msg-display-1",
             "timestamp": 1781024000.0},
            {"role": "user", "content": "same text", "id": "msg-later-2",
             "timestamp": 1781024005.0},
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

    getattr(session_ops, f"{op}_last")(session.session_id)

    # The id mismatch vetoes the later same-text row, so the cut lands on the
    # TRUE selected turn (matched by id) — everything after it is removed.
    assert session.context_messages == []
    assert saved


# ── Finding 3: local-send visible promotion excludes the raw count ────────


@_node_tests
def test_local_send_visible_promotion_excludes_raw_count():
    """The re-gate's Chromium reproduction in the Node VM: a session with a
    hidden wakeup row — server says raw 4 / visible 3 — plus one local send.
    S.messages holds the 3 visible rows + 1 hidden wakeup + the optimistic
    send. The visible count must report 4 (3 + the send), never 5 (the raw
    count), across repeated updater calls (send() calls it twice)."""
    start = SESSIONS_SRC.index("function upsertActiveSessionForLocalTurn")
    end = SESSIONS_SRC.index("function _sessionRowsWithActiveEphemeralSession", start)
    body = SESSIONS_SRC[start:end]
    source = (
        "const SESSIONS_JS = " + repr(SESSIONS_SRC) + ";\n"
        + r"""
function extractFunc(name) {
  const start = SESSIONS_JS.indexOf('function ' + name + '(');
  if (start < 0) throw Error(name + ' missing');
  let i = SESSIONS_JS.indexOf('{', start) + 1, depth = 1;
  while (depth && i < SESSIONS_JS.length) {
    if (SESSIONS_JS[i] === '{') depth++;
    else if (SESSIONS_JS[i] === '}') depth--;
    i++;
  }
  return SESSIONS_JS.slice(start, i);
}

const S = {
  session: {
    session_id: 'sid-7882',
    title: 'Test chat',
    message_count: 4,             // server raw total: 3 visible + 1 hidden
    visible_message_count: 3,     // server visible total
  },
  messages: [
    {role: 'user', content: 'q1'},
    {role: 'assistant', content: 'a1'},
    {role: 'user', content: 'q2'},
    {role: 'user', content: '[ASYNC DELEGATION COMPLETE d1] handoff',
     _source: 'delegation_wakeup'},
    // The optimistic send row is added by send() BEFORE the updater runs.
    {role: 'user', content: 'my new question'},
  ],
  activeProfile: 'default',
};
const _allSessions = [];
const t = (k) => k;
function renderSessionListFromCache() {}
function closeSessionActionMenu() {}
const document = {createElement: () => ({style: {}, dataset: {}})};

"""
        + body
        + r"""

// Two updater calls in one send (optimistic + provisional-title pass):
// send() snapshots the visible tail BEFORE the optimistic push (3 visible
// rows; the hidden wakeup is excluded) and passes it as the baseline.
upsertActiveSessionForLocalTurn({messageCount: 5, localVisibleBeforePush: 3});
upsertActiveSessionForLocalTurn({messageCount: 5, localVisibleBeforePush: 3});

console.log(JSON.stringify({
  raw: S.session.message_count,
  visible: S.session.visible_message_count,
}));
"""
    )
    result = json.loads(_run_node_vm(source))
    assert result["raw"] == 5, f"Raw count follows the transcript, got {result}"
    assert result["visible"] == 4, (
        "Visible count must promote only the local-transcript authority "
        f"(3 visible + 1 send = 4), never the raw count (5), got {result}"
    )


@_node_tests
def test_local_send_paginated_tail_bumps_exactly_one_visible_row():
    """Greptile round-4 P2: in a paginated conversation (100 visible rows on
    the server, 30 loaded locally), one send makes localVisible 31 — a
    tail-only promotion would leave the server total 100 (stale), and the raw
    count would count hidden wakeups. The baseline snapshot (taken from the
    tail BEFORE the push) makes the bump exactly the rows the send added:
    100 + 1 = 101, idempotent across repeated updater calls."""
    start = SESSIONS_SRC.index("function upsertActiveSessionForLocalTurn")
    end = SESSIONS_SRC.index("function _sessionRowsWithActiveEphemeralSession", start)
    body = SESSIONS_SRC[start:end]
    source = (
        "const SESSIONS_JS = " + repr(SESSIONS_SRC) + ";\n"
        + r"""
function extractFunc(name) {
  const start = SESSIONS_JS.indexOf('function ' + name + '(');
  if (start < 0) throw Error(name + ' missing');
  let i = SESSIONS_JS.indexOf('{', start) + 1, depth = 1;
  while (depth && i < SESSIONS_JS.length) {
    if (SESSIONS_JS[i] === '{') depth++;
    else if (SESSIONS_JS[i] === '}') depth--;
    i++;
  }
  return SESSIONS_JS.slice(start, i);
}

const S = {
  session: {
    session_id: 'sid-paged-7882',
    title: 'Long chat',
    message_count: 100,          // server raw total
    visible_message_count: 100,  // server visible total
  },
  messages: [],                  // 30 loaded rows: 29 visible + 1 hidden wakeup
  activeProfile: 'default',
};
for (let i = 0; i < 29; i++) S.messages.push({role: i % 2 ? 'assistant' : 'user', content: 'row ' + i});
S.messages.push({role: 'user', content: '[ASYNC DELEGATION COMPLETE d1] handoff', _source: 'delegation_wakeup'});
// The optimistic send row is added by send() BEFORE the updater runs.
S.messages.push({role: 'user', content: 'my new question'});

const _allSessions = [];
const t = (k) => k;
function renderSessionListFromCache() {}
function closeSessionActionMenu() {}
const document = {createElement: () => ({style: {}, dataset: {}})};

"""
        + body
        + r"""

// send() snapshot: visible tail length BEFORE the push was 29.
upsertActiveSessionForLocalTurn({messageCount: 31, localVisibleBeforePush: 29});
upsertActiveSessionForLocalTurn({messageCount: 31, localVisibleBeforePush: 29});

console.log(JSON.stringify({
  raw: S.session.message_count,
  visible: S.session.visible_message_count,
}));
"""
    )
    result = json.loads(_run_node_vm(source))
    # Raw count keeps the server authority (100): a tail-only local count must
    # never DEMOTE the raw total in a paginated chat — master's Math.max
    # semantics — and the server recount on settle owns the final number.
    assert result["raw"] == 100, f"Raw count must keep the server total, got {result}"
    assert result["visible"] == 101, (
        "Paginated-chat send must bump the server visible total by exactly "
        f"the rows the send added (100 + 1 = 101), got {result}"
    )


# ── Finding 4: overlay credits the hidden pending wakeup ──────────────────


def test_overlay_pending_wakeup_excluded_from_visible_growth():
    """The re-gate's SQLite/HTTP reproduction: sidecar says raw 4 / visible 3
    is WRONG — the real shape is visible 3 provenance-stamped rows, then the
    pending wakeup turn appends one PLAIN user row to state.db (raw 4). The
    overlay must credit the delta minus the pending hidden user row:
    visible 3 + (4 - 2 - 1) = 4... no: sidecar raw 3 / visible 3, state.db
    raw 4 with a pending wakeup → visible 3 + (4-3-1) = 3."""
    from api.models import _apply_sidebar_state_db_override_metadata

    sessions = [
        {
            "session_id": "growth7882b",
            "message_count": 3,
            "visible_message_count": 3,
            # Provenance: the pending turn is a hidden wakeup.
            "pending_user_source": "delegation_wakeup",
            "has_pending_user_message": True,
            "last_message_at": 1781024000.0,
            "updated_at": 1781024000.0,
        }
    ]
    metadata = {
        "growth7882b": {
            "_state_db_source": "webui",
            "_state_db_message_count": 4,  # +1: the plain (unstamped) wakeup row
            "_state_db_last_message_at": 1781024010.0,
        }
    }
    _apply_sidebar_state_db_override_metadata(sessions, metadata)

    assert sessions[0]["message_count"] == 4
    # Raw 4 = 3 provenance-stamped + 1 pending hidden user row. The overlay
    # must NOT report 4 visible: the wakeup row is hidden.
    assert sessions[0]["visible_message_count"] == 3


def test_overlay_pending_visible_turn_still_bumps_visible_count():
    """A pending NON-hidden turn (webui/fork) keeps the monotone bump: its
    state.db row is a real visible user row."""
    from api.models import _apply_sidebar_state_db_override_metadata

    sessions = [
        {
            "session_id": "growth7882c",
            "message_count": 3,
            "visible_message_count": 3,
            "pending_user_source": "webui",
            "has_pending_user_message": True,
            "last_message_at": 1781024000.0,
            "updated_at": 1781024000.0,
        }
    ]
    metadata = {
        "growth7882c": {
            "_state_db_source": "webui",
            "_state_db_message_count": 4,
            "_state_db_last_message_at": 1781024010.0,
        }
    }
    _apply_sidebar_state_db_override_metadata(sessions, metadata)

    assert sessions[0]["message_count"] == 4
    # The webui pending row is visible: the whole delta is credited.
    assert sessions[0]["visible_message_count"] == 4


def test_reconciled_visible_count_returns_none_when_state_db_read_fails(monkeypatch):
    """Greptile round-4 P2: a failed detailed state.db read is NOT 'no state
    rows'. Converting the failure to [] (or to the sidecar's own count) makes
    the caller treat the stale sidecar total as a successful recount and skip
    its growth fallback — even though the growth guard already saw newer
    state.db rows. The helper must return None so the raw-delta fallback
    runs."""
    from api import models as models_mod

    sid = "reconcile-fail-7882"

    class _FakeSession:
        session_id = sid
        profile = None
        messages = [{"role": "user", "content": "old", "_source": "webui"}]
        truncation_watermark = None
        truncation_boundary = None

    monkeypatch.setattr(models_mod.Session, "load_metadata_only", staticmethod(lambda _sid: _FakeSession()))
    monkeypatch.setattr(
        models_mod,
        "get_state_db_session_messages",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("state.db unavailable")),
    )
    assert models_mod._reconciled_visible_count_for_sidebar(sid) is None


def test_reconciled_visible_count_returns_none_when_merge_fails(monkeypatch):
    """Greptile round-4 P2: a merge failure has no proved reconciled total —
    returning the sidecar's own count freezes a stale total instead of letting
    the caller's fallback run."""
    from api import models as models_mod

    sid = "reconcile-merge-fail-7882"

    class _FakeSession:
        session_id = sid
        profile = None
        messages = [{"role": "user", "content": "old", "_source": "webui"}]
        truncation_watermark = None
        truncation_boundary = None

    monkeypatch.setattr(models_mod.Session, "load_metadata_only", staticmethod(lambda _sid: _FakeSession()))
    monkeypatch.setattr(
        models_mod,
        "get_state_db_session_messages",
        lambda *_a, **_k: [{"role": "user", "content": "new"}],
    )
    monkeypatch.setattr(
        models_mod,
        "merge_session_messages_append_only",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("merge exploded")),
    )
    assert models_mod._reconciled_visible_count_for_sidebar(sid) is None


def test_overlay_no_pending_row_keeps_monotone_bump():
    """Without a pending turn, the growth is ordinary settled rows — the
    round-1 behaviour is unchanged (visible rides the raw delta)."""
    from api.models import _apply_sidebar_state_db_override_metadata

    sessions = [
        {
            "session_id": "growth7882",
            "message_count": 2,
            "visible_message_count": 2,
            "last_message_at": 1781024000.0,
            "updated_at": 1781024000.0,
        }
    ]
    metadata = {
        "growth7882": {
            "_state_db_source": "webui",
            "_state_db_message_count": 4,
            "_state_db_last_message_at": 1781024010.0,
        }
    }
    _apply_sidebar_state_db_override_metadata(sessions, metadata)

    assert sessions[0]["message_count"] == 4
    assert sessions[0]["visible_message_count"] == 4


def test_compact_emits_pending_user_source_only_when_set(tmp_path):
    """compact() carries the pending provenance the overlay reads — but only
    when set (ordinary webui/fork rows keep the old shape)."""
    from api.models import Session

    s = Session(
        session_id="compact7882a",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "hi"}],
        pending_user_message="[ASYNC DELEGATION COMPLETE d4] handoff",
        pending_user_source="delegation_wakeup",
    )
    compact = s.compact(include_runtime=True)
    assert compact.get("pending_user_source") == "delegation_wakeup"

    s2 = Session(
        session_id="compact7882b",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "hi"}],
        pending_user_message="hello",
        pending_user_source="webui",
    )
    compact2 = s2.compact(include_runtime=True)
    assert "pending_user_source" not in compact2


def test_hidden_predicate_unchanged():
    """Guard: the hidden-row predicate stays keyed on the typed stamp."""
    assert is_hidden_transcript_row({"_source": "delegation_wakeup"})
    assert not is_hidden_transcript_row({"_source": "webui"})
    assert not is_hidden_transcript_row({})


# ── Round-3 must-fix 1: carrier ambiguity rejects earlier id-less match ────


def test_retry_undo_merged_carrier_rejects_earlier_id_less_match():
    """After manual compression merges a summary into the latest user row
    (merged carrier), a repeated prompt ("continue") leaves several id-less
    same-text candidates. Selecting the newest turn must NOT cut at an
    earlier id-less occurrence: the text-only match is rejected (None) and
    the caller's last-user fallback cuts at the carrier — keeping the rows
    master keeps."""
    from api.session_ops import (
        _truncate_context_before_row,
        _context_prefix_before_last_user,
    )

    context = [
        {"role": "user", "content": "old question", "timestamp": 1781024000.0},
        {"role": "assistant", "content": "old answer", "timestamp": 1781024000.5},
        # Earlier id-less same-text occurrence (sanitized compression copy).
        {"role": "user", "content": "continue", "timestamp": 1781024001.0},
        {"role": "assistant", "content": "step 1 done", "timestamp": 1781024001.5},
        # Merged carrier: live 'continue' text + summary + delimiter suffix.
        {
            "role": "user",
            "content": (
                "[PRIOR CONTEXT — for reference only; not a new message] continue\n\n"
                "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\nsummary body…"
            ),
            "timestamp": 1791568650.9,
        },
        {"role": "assistant", "content": "step 3 done", "timestamp": 1781024003.5},
    ]
    target = {"role": "user", "content": "continue", "timestamp": 1781024006.0}

    result = _truncate_context_before_row(context, target)
    assert result is None, (
        "the earlier id-less occurrence must be rejected when a newer merged "
        f"carrier makes the identity ambiguous, got cut@{len(result) if result is not None else None}"
    )
    # Caller fallback: cut before the context's own last user row — master
    # parity (keeps the 4 rows before the carrier).
    fallback = _context_prefix_before_last_user(context)
    assert len(fallback) == 4
    assert [str(m.get("content", ""))[:30] for m in fallback] == [
        "old question",
        "old answer",
        "continue",
        "step 1 done",
    ]


def test_retry_undo_carrier_shape_still_matches_without_carrier():
    """Control for the ambiguity fix: the SAME id-less content match still
    cuts when NO merged carrier exists in the context (the round-2
    sanitized-compression fix must keep working)."""
    from api.session_ops import _truncate_context_before_row

    context = [
        {"role": "user", "content": "old question", "timestamp": 1781024000.0},
        {"role": "assistant", "content": "old answer", "timestamp": 1781024001.0},
        {"role": "user", "content": "real question", "timestamp": 1781024002.0},
        {
            "role": "user",
            "content": "[ASYNC DELEGATION COMPLETE d2] internal handoff",
            "timestamp": 1781024003.0,
            "_source": "delegation_wakeup",
        },
        {"role": "assistant", "content": "child result summary", "timestamp": 1781024004.0},
    ]
    target = {"role": "user", "content": "real question", "timestamp": 1002.0}
    result = _truncate_context_before_row(context, target)
    assert result is not None
    assert [m["content"] for m in result] == ["old question", "old answer"]


def test_retry_undo_carrier_rows_after_carrier_still_match():
    """A plain id-less copy AFTER the newest carrier is unambiguous: the
    carrier's summary only quotes compressed-away turns, so a later row that
    survived compression is provably the selected turn."""
    from api.session_ops import _truncate_context_before_row

    context = [
        {
            "role": "user",
            "content": (
                "[PRIOR CONTEXT — for reference only; not a new message] continue\n\n"
                "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\nsummary body…"
            ),
            "timestamp": 1791568650.9,
        },
        {"role": "assistant", "content": "step 3 done", "timestamp": 1781024003.5},
        # The selected turn survived compression AFTER the carrier.
        {"role": "user", "content": "continue", "timestamp": 1781024006.0},
        {"role": "assistant", "content": "step 4 done", "timestamp": 1781024006.5},
    ]
    target = {"role": "user", "content": "continue", "timestamp": 1781024006.0}
    result = _truncate_context_before_row(context, target)
    assert result is not None
    assert len(result) == 2


def test_real_compressor_round2_context_cuts_at_tail_copy():
    """Production-composed: run the REAL Agent ContextCompressor over a
    repeated-prompt transcript (the exact context shape after manual
    compression) and retry the newest prompt. The matcher must cut before
    the tail copy — master parity — never at the carrier or an earlier
    occurrence."""
    compressor = pytest.importorskip("agent.context_compressor")
    from api.session_ops import (
        _truncate_context_before_row,
        _context_prefix_before_last_user,
    )
    from api.streaming import _sanitize_messages_for_api, _stamp_missing_message_timestamps
    import copy as copymod

    ContextCompressor = compressor.ContextCompressor
    messages = [
        {"role": "user", "content": "old question", "timestamp": 1781024000.0},
        {"role": "assistant", "content": "old answer", "timestamp": 1781024000.5},
        {"role": "user", "content": "continue", "timestamp": 1781024001.0},
        {"role": "assistant", "content": "step 1 done", "timestamp": 1781024001.5},
        {"role": "user", "content": "continue", "timestamp": 1781024002.0},
        {"role": "assistant", "content": "step 2 done", "timestamp": 1781024002.5},
        {"role": "user", "content": "continue", "timestamp": 1781024003.0},
        {"role": "assistant", "content": "step 3 done", "timestamp": 1781024003.5},
        {"role": "user", "content": "continue", "timestamp": 1781024004.0},
        {"role": "assistant", "content": "step 4 done", "timestamp": 1781024004.5},
    ]
    original = _sanitize_messages_for_api(messages)
    cc = ContextCompressor(
        model="gpt-4o-mini", quiet_mode=True, protect_last_n=2, protect_first_n=2,
    )
    compressed = cc.compress(original, current_tokens=100000)
    context = copymod.deepcopy(compressed)
    _stamp_missing_message_timestamps(context)
    # The compression must have produced a merged/standalone summary carrier
    # for this fixture to exercise the ambiguity path.
    assert any(
        m.get("role") == "user" and m.get("_compressed_summary") for m in context
    ), "fixture expects a summary carrier; compressor output changed"

    target = {"role": "user", "content": "continue", "timestamp": 1781024004.0}
    result = _truncate_context_before_row(context, target)
    fallback = _context_prefix_before_last_user(context)
    assert result is None or len(result) == len(fallback), (
        "retry of the newest prompt must cut at (or after) the last user row — "
        f"matcher cut@{len(result) if result is not None else None}, "
        f"fallback cut@{len(fallback)}"
    )


# ── Round-3 must-fix 2: eager-save wakeup undercount ───────────────────────


def test_overlay_eager_wakeup_reply_counts_reconciled_rows(tmp_path, monkeypatch):
    """Eager-save mode: the sidecar already contains AND excludes the hidden
    wakeup user row; when the assistant reply lands in state.db while the
    turn is pending, the overlay must NOT subtract the wakeup again. The
    visible total must come from the reconciled, provenance-stamped rows."""
    import sqlite3
    from collections import OrderedDict

    import api.config as config
    import api.models as models
    import api.profiles as profiles

    sid = "eagerwake7882"
    # Isolated session store + state.db so the reconciled reader sees the
    # test's rows, not the host's.
    monkeypatch.setattr(config, "STATE_DIR", tmp_path, raising=False)
    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    state_db_path = tmp_path / "state.db"
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db_path, raising=False)
    session_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(state_db_path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, started_at REAL, message_count INTEGER)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, message_count) VALUES (?, 'webui', 'Eager wake', 'test-model', 1000.0, 4)",
        (sid,),
    )
    # state.db holds the sidecar's 3 rows PLUS the visible assistant reply
    # that landed while the wakeup turn is still pending (raw 4).
    for row in (
        ("user", "q1", 1781024000.0),
        ("assistant", "a1", 1781024000.5),
        # The wakeup row the Agent core appended as a PLAIN user row.
        ("user", "[ASYNC DELEGATION COMPLETE d5] handoff", 1781024001.0),
        ("assistant", "child reply", 1781024002.0),
    ):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, row[0], row[1], row[2]),
        )
    conn.commit()
    conn.close()

    sidecar = models.Session(
        session_id=sid,
        title="Eager wake",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "q1", "timestamp": 1781024000.0},
            {"role": "assistant", "content": "a1", "timestamp": 1781024000.5},
            # The hidden wakeup row the eager checkpoint persisted.
            {
                "role": "user",
                "content": "[ASYNC DELEGATION COMPLETE d5] handoff",
                "timestamp": 1781024001.0,
                "_source": "delegation_wakeup",
            },
        ],
        pending_user_message="[ASYNC DELEGATION COMPLETE d5] handoff",
        pending_user_source="delegation_wakeup",
        pending_started_at=1781024001.0,
    )
    # Persist + reload so the overlay reads the same metadata-prefix visible
    # count the sidebar poll sees: raw 3, visible 2 (wakeup excluded).
    sidecar.save()
    reloaded = models.Session.load(sid)
    compact = reloaded.compact(include_runtime=True)
    assert compact["message_count"] == 3
    assert compact["visible_message_count"] == 2
    sessions = [dict(compact)]
    # The bot's shape: state.db grows by ONE VISIBLE assistant reply while
    # the wakeup turn is still pending (raw 3 -> 4). The old arithmetic
    # subtracted hidden_pending_delta=1 from that delta and reported visible
    # 2 — treating the visible reply as the hidden row. last_message_at
    # must also advance (the overlay's anti-resurrection guard requires a
    # strictly newer state.db row), which is exactly what a fresh append
    # does in production.
    metadata = {
        sid: {
            "_state_db_source": "webui",
            "_state_db_message_count": 4,
            "_state_db_last_message_at": 1891569499.5,
        }
    }
    models._apply_sidebar_state_db_override_metadata(sessions, metadata)

    assert sessions[0]["message_count"] == 4
    # Reconciled rows: 3 visible (q1, a1, child reply) — the old delta
    # arithmetic reported 2.
    assert sessions[0]["visible_message_count"] == 3, (
        f"eager wakeup reply must count reconciled visible rows (3), "
        f"got {sessions[0]['visible_message_count']}"
    )


# ── Round-3 must-fix 3: localVisible counts tool rows ──────────────────────


@_node_tests
def test_local_send_visible_count_includes_tool_rows():
    """The server's visible count includes tool rows; localVisible must use
    the same definition or a send in a conversation with tool results
    reports 4 msgs where master shows 5 (Chromium repro at 1280/390px)."""
    start = SESSIONS_SRC.index("function upsertActiveSessionForLocalTurn")
    end = SESSIONS_SRC.index("function _sessionRowsWithActiveEphemeralSession", start)
    body = SESSIONS_SRC[start:end]
    source = (
        "const SESSIONS_JS = " + repr(SESSIONS_SRC) + ";\n"
        + r"""
function extractFunc(name) {
  const start = SESSIONS_JS.indexOf('function ' + name + '(');
  if (start < 0) throw Error(name + ' missing');
  let i = SESSIONS_JS.indexOf('{', start) + 1, depth = 1;
  while (depth && i < SESSIONS_JS.length) {
    if (SESSIONS_JS[i] === '{') depth++;
    else if (SESSIONS_JS[i] === '}') depth--;
    i++;
  }
  return SESSIONS_JS.slice(start, i);
}

const S = {
  session: {
    session_id: 'sid-7882-tools',
    title: 'Tool chat',
    message_count: 5,             // server raw total
    visible_message_count: 4,     // server visible total (includes tool rows)
  },
  messages: [
    {role: 'user', content: 'q1'},
    {role: 'assistant', content: 'thinking', tool_calls: [{id: 't1'}]},
    {role: 'tool', content: 'tool result', tool_call_id: 't1'},
    {role: 'assistant', content: 'a1'},
    // The optimistic send row is added by send() BEFORE the updater runs.
    {role: 'user', content: 'my new question'},
  ],
  activeProfile: 'default',
};
const _allSessions = [];
const t = (k) => k;
function renderSessionListFromCache() {}
function closeSessionActionMenu() {}
const document = {createElement: () => ({style: {}, dataset: {}})};

"""
        + body
        + r"""

upsertActiveSessionForLocalTurn({messageCount: 6});

console.log(JSON.stringify({
  raw: S.session.message_count,
  visible: S.session.visible_message_count,
}));
"""
    )
    result = json.loads(_run_node_vm(source))
    assert result["raw"] == 6
    # 4 server-visible rows + the send = 5. The old tool-row exclusion
    # reported 4 (server said 5).
    assert result["visible"] == 5, (
        "localVisible must count tool rows like the server does "
        f"(4 visible + 1 send = 5), got {result}"
    )
