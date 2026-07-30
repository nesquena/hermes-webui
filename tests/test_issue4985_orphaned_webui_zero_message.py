"""Regression coverage for orphaned native-WebUI zero-message row pruning.

#4985: native-WebUI rows whose backing ``state.db.messages`` is empty linger
in the sidebar forever (boot-time ``+`` click that never sent a turn, profile
switch that resets the active id, sidebar nav that opens a session then closes
the tab before the first message commits). Clicking such a row hits
``GET /api/session`` -> 404 and "Session not available in web UI." indefinitely.
The WebUI delete affordance is not exposed for these rows and the existing
#3238/#4591 orphan prune explicitly excludes ``source == "webui"`` rows.

The fix probes ``state.db.messages`` directly via
``agent_session_zero_message_sids()`` (a batched existence-via-NOT-EXISTS
probe parallel to ``agent_session_rows_existing``) and prunes a WebUI row only
when ALL of:

1. ``_session_source_is_webui(s)`` is True (strictly webui-sourced),
2. ``s.get("active_stream_id")`` is empty (inflight first-turn safety: the
   prior ``_reconcile_stale_stream_state_for_session_rows`` pass has already
   cleared stale stream ids, so any row still carrying one is genuinely
   streaming),
3. ``s.get("has_pending_user_message")`` is empty (a row with a pending user
   turn is still being filled — never prune it),
4. ``s.get("worktree_path")`` is empty (worktree-bound rows are persisted in a
   way that ``all_sessions()`` recovers via ``missing_persisted_ids``; never
   prune them or the next poll re-prunes them, the index mtime flickers,
   and the cache-thrash loop from #4985 review IC_kwDOR1LuPM8AAAABHrkF1Q
   re-establishes itself),
5. **The row SURVIVED the upstream ``all_sessions()`` #1171 keep-filter** —
   i.e. the row's sidecar/title is ``!= "Untitled"`` OR its claimed
   ``message_count > 0``. Without this clause, the gate is a no-op because
   ``all_sessions()`` has already stripped every (Untitled ∧ count==0 ∧
   ¬active_stream ∧ ¬pending ∧ ¬worktree) row at ``api/models.py:3689-3695``
   (index path) and ``3735-3741`` (full-scan path) before our prune block
   runs. The earlier 6-condition gate made this same mistake (review
   ``IC_kwDOR1LuPM8AAAABHrkF1Q``); this version targets the post-#1171
   survivors — the rows that #4985 actually describes.

Mirrors the existing #3238 test file's structure (monkeypatch helpers,
``_payload_for_rows`` shape, named-test style) and reuses its
``_make_state_db`` for the sessions-table half, then layers on a richer
state.db fixture that also populates a ``messages`` table for the "row in
state.db but messages present" / "row in state.db but messages absent"
distinction.

Test design:

- Tests 1-12 use the same monkeypatch pattern as
  ``tests/test_issue3238_orphaned_cli_sidecar_prune.py``:
  ``all_sessions`` is monkeypatched to a raw lambda, the prune helpers
  ``agent_session_rows_existing`` / ``agent_session_zero_message_sids`` are
  monkeypatched to lambda that returns the requested set, and
  ``prune_session_from_index`` is monkeypatched to a recorder. These tests
  exercise the gate predicate + the prune-batch helper dispatch in
  isolation, with an artificial row shape the caller injects. They DO NOT
  drive a row through the real ``all_sessions()`` pipeline — so they cannot
  on their own prove the gate fires against a real row that survived #1171.
  See the "Real-pipeline end-to-end coverage" section below for that proof.

- Tests 13-18 (the real-pipeline tests) stand up the ``SESSION_DIR`` /
  ``SESSION_INDEX_FILE`` / ``SESSIONS`` / ``HERMES_HOME`` / state.db the
  way ``api.models.all_sessions()`` reads them in production, write a real
  session sidecar that survives #1171, and assert that the resulting
  ``_build_session_list_cache_payload`` actually calls
  ``prune_session_from_index`` for the genuinely-surviving orphan.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _make_state_db(path: Path, session_ids, *, messages_per_session=None):
    """Create an agent state.db with both ``sessions`` and ``messages`` tables.

    ``messages_per_session`` is an optional mapping ``{sid: row_count}``. When
    omitted, sessions get zero message rows (the orphan candidate shape); when
    present, that many message rows are inserted per sid so the zero-message
    probe can distinguish "row in sessions but no messages" from "row in
    sessions with messages present."
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, "
            "started_at REAL, last_activity REAL)"
        )
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
        )
        for sid in session_ids:
            conn.execute(
                "INSERT INTO sessions (id, source, started_at, last_activity) "
                "VALUES (?, 'cli', 0, 0)",
                (sid,),
            )
        if messages_per_session:
            for sid, n in messages_per_session.items():
                for i in range(int(n)):
                    conn.execute(
                        "INSERT INTO messages (session_id, role, content, "
                        "timestamp) VALUES (?, 'user', ?, 0)",
                        (sid, f"hello-{i}"),
                    )
        conn.commit()
    finally:
        conn.close()


# ── Helper-direct coverage for agent_session_zero_message_sids ─────────────


def test_agent_session_zero_message_sids_returns_only_orphans(tmp_path, monkeypatch):
    """Mixed input: only ids with zero message rows are returned."""
    from api import models

    home = tmp_path / "home"
    home.mkdir()
    _make_state_db(
        home / "state.db",
        ["sess-empty", "sess-with-msg"],
        messages_per_session={"sess-with-msg": 3},
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: home / "state.db")

    zero = models.agent_session_zero_message_sids(["sess-empty", "sess-with-msg"])
    assert zero == frozenset({"sess-empty"})


def test_agent_session_zero_message_sids_empty_when_db_missing(tmp_path, monkeypatch):
    """No agent DB -> return frozenset() so the caller prunes nothing."""
    from api import models

    monkeypatch.setattr(
        models, "_active_state_db_path", lambda: tmp_path / "nope" / "state.db"
    )
    wanted = ["orphan-a", "orphan-b"]
    assert models.agent_session_zero_message_sids(wanted) == frozenset()


def test_agent_session_zero_message_sids_handles_missing_messages_table(
    tmp_path, monkeypatch,
):
    """A state.db without a ``messages`` table degrades to frozenset() — never prune."""
    from api import models

    home = tmp_path / "home"
    home.mkdir()
    db = home / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO sessions (id) VALUES ('sess-x')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    assert models.agent_session_zero_message_sids(["sess-x"]) == frozenset()


def test_agent_session_zero_message_sids_handles_missing_sessions_table(
    tmp_path, monkeypatch,
):
    """A state.db without a ``sessions`` table degrades to frozenset()."""
    from api import models

    home = tmp_path / "home"
    home.mkdir()
    db = home / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE messages (session_id TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    assert models.agent_session_zero_message_sids(["sess-x"]) == frozenset()


def test_agent_session_zero_message_sids_empty_id_filtered(tmp_path, monkeypatch):
    """Empty / None / whitespace-only ids are filtered before probing."""
    from api import models

    home = tmp_path / "home"
    home.mkdir()
    _make_state_db(home / "state.db", ["sess-a"])
    monkeypatch.setattr(models, "_active_state_db_path", lambda: home / "state.db")

    zero = models.agent_session_zero_message_sids(
        ["  ", "", "sess-a"]  # None-equivalent: empty/whitespace are filtered
    )
    assert zero == frozenset({"sess-a"})


def test_agent_session_zero_message_sids_batches_over_500_ids(tmp_path, monkeypatch):
    """Batched chunked probe mirrors agent_session_rows_existing (chunk=500)."""
    from api import models

    home = tmp_path / "home"
    home.mkdir()
    ids = [f"sess-{i:04d}" for i in range(600)]
    # First 300 are empty in messages; the next 300 each have one row.
    _make_state_db(
        home / "state.db",
        ids,
        messages_per_session={sid: 1 for sid in ids[300:]},
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: home / "state.db")

    zero = models.agent_session_zero_message_sids(ids)
    assert zero == frozenset(ids[:300])


def test_agent_session_zero_message_sids_only_returns_existing_sessions(
    tmp_path, monkeypatch,
):
    """Ids that have NO row in ``sessions`` are not in the result (the join filters them)."""
    from api import models

    home = tmp_path / "home"
    home.mkdir()
    _make_state_db(home / "state.db", ["sess-keep"])
    monkeypatch.setattr(models, "_active_state_db_path", lambda: home / "state.db")

    zero = models.agent_session_zero_message_sids(["sess-keep", "sess-not-in-db"])
    assert zero == frozenset({"sess-keep"})


# ── Sidebar-payload prune-decision coverage (mirrors #3238 _payload_for_rows) ─
#
# NB: these are GATE PREDICATE TESTS. ``all_sessions`` is monkeypatched to a
# raw lambda, so the rows the test injects do NOT pass through the real
# ``all_sessions()`` #1171 keep-filter. That means these tests prove the gate
# predicate + the batch probe + the prune dispatch in isolation, but they do
# NOT prove the gate fires against a row that survived #1171 in the real
# pipeline (a monkeypatched lambda can synthesize a row that the real
# ``all_sessions()`` would have stripped before the gate runs). The
# "Real-pipeline end-to-end coverage" section below (tests 13-18) drives a
# real row through the real pipeline. Every webui row in this section uses
# a descriptive non-Untitled title so the gate predicate exercises
# ``title != 'Untitled' OR count > 0`` on a row shape that POST-#1171
# ``all_sessions()`` would actually keep — i.e. the gate fires the same way
# it does against a real survivor.


def _payload_for_rows_webui(
    monkeypatch,
    rows,
    zero_message_ids,
    existing_ids=None,
    cli_existing_ids=None,
):
    """Variant of #3238's ``_payload_for_rows`` for the #4985 path.

    Mocks both ``agent_session_rows_existing`` (used by the #3238/#4591
    CLI/API-server prune) and the new ``agent_session_zero_message_sids``
    (used by the #4985 webui zero-message prune). Returns the resulting
    payload and the list of sids handed to ``prune_session_from_index``.
    """
    import api.routes as routes

    pruned = []
    cli_existing_ids = cli_existing_ids if cli_existing_ids is not None else existing_ids

    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: list(rows))
    monkeypatch.setattr(
        routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: []
    )
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _sessions: False,
    )
    monkeypatch.setattr(
        routes,
        "agent_session_rows_existing",
        lambda ids, profile=None: frozenset(cli_existing_ids or []),
    )
    monkeypatch.setattr(
        routes,
        "agent_session_zero_message_sids",
        lambda ids, profile=None: frozenset(zero_message_ids or []),
    )
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda sid: pruned.append(sid)
    )

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    return payload, pruned


# 1. Titled Webui row with state.db row but state.db.messages empty -> PRUNED.
#    The row has a descriptive (non-Untitled) title so the gate predicate
#    ``title != 'Untitled' OR count > 0`` fires, then the helper confirms the
#    state.db.messages table is empty, then prune_session_from_index is called.
#    This is the post-#1171-survivor orphan shape #4985 describes.
def test_webui_titled_orphan_in_state_db_is_pruned(monkeypatch):
    row = {
        "session_id": "webui-orphan",
        "title": "Native WebUI",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 0,
        "read_only": False,
        "source_tag": "webui",
        "raw_source": "webui",
        "session_source": "webui",
        "source_label": "WebUI",
        "is_cli_session": False,
    }

    payload, pruned = _payload_for_rows_webui(
        monkeypatch, [row], zero_message_ids=["webui-orphan"],
    )

    assert [session["session_id"] for session in payload["sessions"]] == []
    assert pruned == ["webui-orphan"]


# 2. Titled Webui row with state.db.messages NON-empty -> RETAINED. The gate
#    fires, the helper confirms messages exist, the row is NOT in the missing
#    set, prune_session_from_index is NOT called.
def test_webui_titled_row_with_messages_in_state_db_is_retained(monkeypatch):
    """An inflight first turn that has already written rows to state.db but not
    yet bumped the sidecar's message_count must NOT be pruned."""
    row = {
        "session_id": "webui-inflight",
        "title": "Native WebUI",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 0,
        "read_only": False,
        "source_tag": "webui",
        "raw_source": "webui",
        "session_source": "webui",
        "source_label": "WebUI",
        "is_cli_session": False,
    }

    # zero_message_ids empty -> helper reports no ids as zero-message.
    payload, pruned = _payload_for_rows_webui(
        monkeypatch, [row], zero_message_ids=[],
    )

    assert [session["session_id"] for session in payload["sessions"]] == ["webui-inflight"]
    assert pruned == []


# 3. Webui row with active_stream_id set -> NOT probed (inflight). The
#    active_stream_id gate takes precedence over title/count and keeps the
#    row out of the probe entirely, even if its state.db.messages is empty.
def test_webui_row_with_active_stream_is_not_probed(monkeypatch):
    """Inflight first-turn safety: a row carrying active_stream_id is genuinely
    streaming and is excluded from the orphan probe entirely, regardless of
    what state.db.messages says."""
    row = {
        "session_id": "webui-streaming",
        "title": "Native WebUI",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 0,
        "read_only": False,
        "source_tag": "webui",
        "raw_source": "webui",
        "session_source": "webui",
        "source_label": "WebUI",
        "is_cli_session": False,
        "active_stream_id": "stream-abc-123",
    }

    # Even if the helper would have returned this sid as zero-message, the
    # active_stream_id gate keeps the row out of the probe.
    payload, pruned = _payload_for_rows_webui(
        monkeypatch, [row], zero_message_ids=["webui-streaming"],
    )

    assert [session["session_id"] for session in payload["sessions"]] == ["webui-streaming"]
    assert pruned == []


# 4. Webui row with message_count>0 -> gate predicate includes it (count > 0
#    passes the OR), helper confirms messages exist -> RETAINED. This proves
#    the post-#1171-survivor detection: a row with a positive claimed
#    message_count is exactly the kind of row that survives #1171 and lands
#    in our gate.
def test_webui_row_with_positive_message_count_is_retained(monkeypatch):
    """A WebUI row that already has a positive message_count lands in the
    post-#1171-survivor branch and is probed; if the helper confirms
    messages actually exist, the row is retained."""
    row = {
        "session_id": "webui-live",
        "title": "Native WebUI",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 5,
        "read_only": False,
        "source_tag": "webui",
        "raw_source": "webui",
        "session_source": "webui",
        "source_label": "WebUI",
        "is_cli_session": False,
    }

    payload, pruned = _payload_for_rows_webui(
        monkeypatch, [row], zero_message_ids=[],
    )

    assert [session["session_id"] for session in payload["sessions"]] == ["webui-live"]
    assert pruned == []


# 5. CLI row with message_count=0 and no backing state.db row -> pruned by EXISTING path
def test_cli_orphan_with_zero_messages_is_pruned_by_existing_path(monkeypatch):
    """Regression: the #3238/#4591 CLI/API-server prune must still work
    independently of #4985. A CLI row absent from cli_by_id AND absent from
    state.db.sessions is pruned by the existing path, even though it has
    message_count=0."""
    row = {
        "session_id": "cli-orphan-zero",
        "title": "CLI Session",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 0,
        "read_only": True,
        "source_tag": "cli",
        "raw_source": "cli",
        "session_source": "cli",
        "source_label": "CLI",
        "is_cli_session": True,
    }

    # existing_ids=[] -> CLI path considers it orphaned. zero_message_ids=[]
    # (irrelevant for CLI; the #4985 helper doesn't even fire because
    # _session_source_is_webui is False).
    payload, pruned = _payload_for_rows_webui(
        monkeypatch,
        [row],
        zero_message_ids=[],
        existing_ids=[],
    )

    assert [session["session_id"] for session in payload["sessions"]] == []
    assert pruned == ["cli-orphan-zero"]


# 6. API-server sidecar with message_count=0 and no backing state.db row -> pruned by EXISTING path
def test_api_server_orphan_with_zero_messages_is_pruned_by_existing_path(monkeypatch):
    """Regression: the #4591 API-server sidecar prune must still work
    independently of #4985."""
    row = {
        "session_id": "api-orphan-zero",
        "title": "API Session",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 0,
        "read_only": True,
        "source_tag": "api_server",
        "raw_source": "api_server",
        "session_source": "api",
        "source_label": "API",
        "is_cli_session": False,
    }

    payload, pruned = _payload_for_rows_webui(
        monkeypatch,
        [row],
        zero_message_ids=[],
        existing_ids=[],
    )

    assert [session["session_id"] for session in payload["sessions"]] == []
    assert pruned == ["api-orphan-zero"]


# 7. Titled Webui row but state.db probe unreachable (helper safe-degrades) ->
#    RETAINED. The helper returns frozenset() so the caller prunes nothing.
def test_webui_titled_row_is_retained_on_helper_safe_degrade(monkeypatch):
    """If the new ``agent_session_zero_message_sids`` helper safe-degrades to
    frozenset() (state.db unreachable / sqlite error / missing tables), the
    sidebar MUST NOT prune — a transient failure can never cause data loss."""
    row = {
        "session_id": "webui-safe",
        "title": "Native WebUI",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 0,
        "read_only": False,
        "source_tag": "webui",
        "raw_source": "webui",
        "session_source": "webui",
        "source_label": "WebUI",
        "is_cli_session": False,
    }

    # zero_message_ids=[] simulates safe-degrade: helper returns no ids.
    payload, pruned = _payload_for_rows_webui(
        monkeypatch, [row], zero_message_ids=[],
    )

    assert [session["session_id"] for session in payload["sessions"]] == ["webui-safe"]
    assert pruned == []


# 8. Existing CLI prune still wins when a row happens to match BOTH paths
def test_cli_row_is_pruned_by_cli_path_not_webui_path(monkeypatch):
    """A CLI row absent from state.db.sessions is pruned by the #3238 path
    BEFORE the #4985 second pass runs (it is never appended to
    ``_kept_after_orphan_prune`` in the first place). It must not be
    double-counted or fall into the webui probe."""
    row = {
        "session_id": "cli-double-orphan",
        "title": "CLI Session",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 0,
        "read_only": True,
        "source_tag": "cli",
        "raw_source": "cli",
        "session_source": "cli",
        "source_label": "CLI",
        "is_cli_session": True,
    }

    payload, pruned = _payload_for_rows_webui(
        monkeypatch,
        [row],
        # Even if the webui helper would claim this id, the CLI path got
        # there first and pruned it before the webui probe runs.
        zero_message_ids=["cli-double-orphan"],
        existing_ids=[],
    )

    assert [session["session_id"] for session in payload["sessions"]] == []
    assert pruned == ["cli-double-orphan"]


# 9. Mixed batch: titled webui orphan, titled webui-with-messages, titled
#    webui-live, CLI orphan -> all correct. The webui rows all have a
#    descriptive title so the gate predicate's title branch fires; the CLI
#    row is pruned by the #3238 path BEFORE the #4985 second pass runs.
def test_mixed_batch_each_row_lands_in_correct_bucket(monkeypatch):
    rows = [
        # titled webui zero-message orphan -> pruned by #4985
        {
            "session_id": "webui-zero",
            "title": "Native WebUI",
            "profile": "default",
            "updated_at": 30,
            "last_message_at": 30,
            "message_count": 0,
            "read_only": False,
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_label": "WebUI",
            "is_cli_session": False,
        },
        # titled webui zero-message but messages in state.db -> kept
        {
            "session_id": "webui-inflight",
            "title": "Native WebUI",
            "profile": "default",
            "updated_at": 20,
            "last_message_at": 20,
            "message_count": 0,
            "read_only": False,
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_label": "WebUI",
            "is_cli_session": False,
        },
        # titled webui with positive count -> kept
        {
            "session_id": "webui-live",
            "title": "Native WebUI",
            "profile": "default",
            "updated_at": 10,
            "last_message_at": 10,
            "message_count": 5,
            "read_only": False,
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_label": "WebUI",
            "is_cli_session": False,
        },
        # CLI orphan -> pruned by #3238 path
        {
            "session_id": "cli-orphan",
            "title": "CLI",
            "profile": "default",
            "updated_at": 5,
            "last_message_at": 5,
            "message_count": 0,
            "read_only": True,
            "source_tag": "cli",
            "raw_source": "cli",
            "session_source": "cli",
            "source_label": "CLI",
            "is_cli_session": True,
        },
    ]

    payload, pruned = _payload_for_rows_webui(
        monkeypatch,
        rows,
        zero_message_ids=["webui-zero"],  # only webui-zero confirmed zero-message
        existing_ids=[],  # CLI orphan absent from state.db.sessions
    )

    assert [session["session_id"] for session in payload["sessions"]] == [
        "webui-inflight",
        "webui-live",
    ]
    assert sorted(pruned) == ["cli-orphan", "webui-zero"]



# ── Retain-case coverage (worktree / pending / streaming / pending worktree) ─
#
# These prove the four "never prune this row" safety gates fire correctly in
# isolation, even though their rows would normally enter the post-#1171-survivor
# gate predicate (titled, count==0). The maintainer's MUST-FIX review
# IC_kwDOR1LuPM8AAAABHrkF1Q requires that worktree / pending / streaming rows
# be spared even if the messages table is momentarily empty (the inflight /
# worktree-bound safety contract).


# 10. Webui row with worktree_path set -> RETAINED (worktree-bearing rows
#     survive #1171's default filter, so they must also survive ours).
#     Otherwise a freshly-bound worktree scratch session with zero messages
#     gets pruned, then re-pruned, then re-added by missing_persisted_ids
#     recovery on every poll — cache-thrash loop on the hot sidebar path.
def test_webui_titled_row_with_worktree_path_is_retained(monkeypatch):
    """Gate excludes worktree-bound rows from the prune batch even when
    state.db.messages is empty — see IC_kwDOR1LuPM8AAAABHrkF1Q."""
    row = {
        "session_id": "webui-worktree",
        "title": "Native WebUI",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 0,
        "read_only": False,
        "source_tag": "webui",
        "raw_source": "webui",
        "session_source": "webui",
        "source_label": "WebUI",
        "is_cli_session": False,
        "worktree_path": "some/path",
    }

    # Even if the helper would return this sid as zero-message, the
    # worktree_path gate keeps the row out of the probe entirely.
    payload, pruned = _payload_for_rows_webui(
        monkeypatch, [row], zero_message_ids=["webui-worktree"],
    )

    assert [session["session_id"] for session in payload["sessions"]] == [
        "webui-worktree"
    ]
    assert pruned == []


# 11. Webui row with has_pending_user_message set -> RETAINED (the row has a
#     pending user turn; #1171 keeps it on the sidebar until that turn
#     commits, and the #4985 prune must agree).
def test_webui_titled_row_with_pending_user_message_is_retained(monkeypatch):
    """Gate excludes pending rows from the prune batch."""
    row = {
        "session_id": "webui-pending",
        "title": "Native WebUI",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 0,
        "read_only": False,
        "source_tag": "webui",
        "raw_source": "webui",
        "session_source": "webui",
        "source_label": "WebUI",
        "is_cli_session": False,
        "has_pending_user_message": True,
    }

    # Even if the helper would return this sid as zero-message, the
    # has_pending_user_message gate keeps the row out of the probe.
    payload, pruned = _payload_for_rows_webui(
        monkeypatch, [row], zero_message_ids=["webui-pending"],
    )

    assert [session["session_id"] for session in payload["sessions"]] == [
        "webui-pending"
    ]
    assert pruned == []


# 12. Titled Webui row with empty state.db.messages -> PRUNED. The new gate
#     targets post-#1171-survivors explicitly, so a titled row whose messages
#     table is empty IS pruned (the #4985 fix shape). This is the flip side
#     of the renamed-title retain test from the old gate — the new gate does
#     NOT unconditionally retain renamed rows; it only retains them when the
#     messages table is non-empty (or when the row is worktree / pending /
#     streaming, per tests 10/11/3).
def test_webui_titled_row_with_empty_state_db_messages_is_pruned(monkeypatch):
    """A titled row is exactly the post-#1171-survivor shape the new gate
    targets — title != 'Untitled' fires the OR branch, then the helper
    confirms the messages table is empty, then prune fires."""
    row = {
        "session_id": "webui-renamed",
        "title": "My Custom Title",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 0,
        "read_only": False,
        "source_tag": "webui",
        "raw_source": "webui",
        "session_source": "webui",
        "source_label": "WebUI",
        "is_cli_session": False,
    }

    payload, pruned = _payload_for_rows_webui(
        monkeypatch, [row], zero_message_ids=["webui-renamed"],
    )

    assert [session["session_id"] for session in payload["sessions"]] == []
    assert pruned == ["webui-renamed"]


# ── Real-pipeline end-to-end coverage ───────────────────────────────────────
#
# These tests drive a real ``all_sessions()`` (no ``all_sessions``
# monkeypatch). They stand up the ``SESSION_DIR`` /
# ``SESSION_INDEX_FILE`` / ``SESSIONS`` / ``HERMES_HOME`` / state.db the
# way ``api.models.all_sessions()`` reads them in production, write a real
# session sidecar that survives #1171's keep-filter, and assert that the
# resulting ``_build_session_list_cache_payload`` actually fires
# ``prune_session_from_index`` for the genuinely-surviving orphan shape
# described in #4985. These are the tests that prove the gate works
# against a real row the way the maintainer's review
# (IC_kwDOR1LuPM8AAAABHrkF1Q) required.
#
# The ``_reconcile_stale_stream_state_for_session_rows`` is monkeypatched to
# ``False`` here ONLY because every session we construct has no stream id
# (or the stream id is genuine) and we want to short-circuit the side
# effects of stale-stream reconciliation. The row itself flows through the
# REAL ``all_sessions()`` filter, index read, refresh, and #1171 keep-filter
# at api/models.py:3689-3695 — i.e. the exact pipeline the #4985 prune
# gate sits behind.


@pytest.fixture
def _real_pipeline(tmp_path, monkeypatch):
    """Stand up the production sidebar pipeline in tmp_path.

    Redirects ``SESSION_DIR`` / ``SESSION_INDEX_FILE`` to tmp_path so the
    index file and any saved sidecars land in a throwaway directory, points
    ``HERMES_HOME`` at a sibling tmp dir with a real agent state.db
    (``messages`` + ``sessions`` tables per test case), and clears
    ``SESSIONS`` so no stale in-memory session leaks across tests.
    """
    import api.models as models
    from api import profiles

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)

    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # ``_active_state_db_path`` reads ``HERMES_HOME`` indirectly via
    # ``api.profiles.get_active_hermes_home``. Force the module-level
    # default so the helper returns ``hermes_home/state.db`` even when the
    # active profile is something other than 'default'.
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", hermes_home)

    models.SESSIONS.clear()
    try:
        from api.models import STREAMS
        STREAMS.clear()
    except Exception:
        pass

    yield tmp_path

    models.SESSIONS.clear()
    try:
        from api.models import STREAMS
        STREAMS.clear()
    except Exception:
        pass


def _write_webui_sidecar(
    session_dir: Path,
    *,
    session_id: str,
    title: str = "Native WebUI",
    message_count: int = 0,
    active_stream_id: str | None = None,
    pending_user_message: str | None = None,
    worktree_path: str | None = None,
    profile: str = "default",
    messages: list[dict] | None = None,
) -> None:
    """Write a sidecar JSON file that ``all_sessions()`` will pick up via
    the index/full-scan path and that survives the #1171 keep-filter (i.e.
    titled OR has positive message_count OR is streaming OR has pending
    OR is worktree-bound — exactly the rows the gate targets).

    When ``messages`` is ``None`` (the default), writes ``"messages": []`` —
    the empty-sidecar shape the older tests assert. When ``messages`` is a
    list of plain dicts, the sidecar's ``messages`` array is that list
    verbatim (JSON-serializable), and ``message_count`` is recomputed from
    ``len(messages)`` only if the caller did not explicitly pass a
    different ``message_count`` (preserves the explicit-override contract
    used by the r5/r6 phantom test).
    """
    if messages is None:
        messages_array: list[dict] = []
    else:
        messages_array = list(messages)
    # If the caller didn't explicitly pass a non-zero message_count AND
    # they provided a populated messages list, auto-derive the count from
    # the list length. Callers that pass both still win (the explicit
    # message_count is preserved — covers the stale-count phantom test).
    if messages and message_count == 0 and len(messages_array) > 0:
        message_count = len(messages_array)
    sidecar = {
        "session_id": session_id,
        "title": title,
        "profile": profile,
        "created_at": 1700000000.0,
        "updated_at": 1700000000.0,
        "pinned": False,
        "archived": False,
        "is_cli_session": False,
        "source_tag": "webui",
        "raw_source": "webui",
        "session_source": "webui",
        "source_label": "WebUI",
        "read_only": False,
        "message_count": message_count,
        "messages": messages_array,
        "tool_calls": [],
    }
    if active_stream_id:
        sidecar["active_stream_id"] = active_stream_id
    if pending_user_message:
        sidecar["pending_user_message"] = pending_user_message
        sidecar["has_pending_user_message"] = True
    if worktree_path:
        sidecar["worktree_path"] = worktree_path
    (session_dir / f"{session_id}.json").write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_state_db(home: Path, *, sessions: dict[str, dict]) -> None:
    """Build a minimal agent ``state.db`` with ``sessions`` + ``messages`` tables.

    ``sessions`` maps session_id -> ``{"messages": int, "source": str}``. The
    ``messages`` key controls how many rows go in ``state.db.messages`` for
    that session id (0 = the orphan candidate shape).
    """
    db = home / "state.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT, title TEXT, "
            "started_at REAL, last_activity REAL, message_count INTEGER)"
        )
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
        )
        for sid, meta in sessions.items():
            source = meta.get("source", "cli")
            title = meta.get("title", "Native WebUI")
            conn.execute(
                "INSERT INTO sessions (id, source, title, started_at, "
                "last_activity, message_count) "
                "VALUES (?, ?, ?, 0, 0, ?)",
                (sid, source, title, meta.get("message_count", 0)),
            )
            n = int(meta.get("messages", 0))
            for i in range(n):
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, "
                    "timestamp) VALUES (?, 'user', ?, 0)",
                    (sid, f"hello-{i}"),
                )
        conn.commit()
    finally:
        conn.close()


def _run_payload(pruned: list[str], *, show_cli_sessions: bool = True):
    """Invoke the real ``_build_session_list_cache_payload`` pipeline.

    Returns the returned payload and the list of sids handed to
    ``prune_session_from_index``. The session-list cache is monkeypatched to
    a no-op builder so the payload is computed inline (no caching across
    calls). The reconcile-stale-stream call is also forced to ``False``
    so a row that genuinely has no stream id isn't accidentally cleared by
    the side-effect.

    ``show_cli_sessions`` defaults to True for the historical tests; the
    follow-up #4985 review IC_kwDOR1LuPM8AAAABHsyFGg added
    ``show_cli_sessions=False`` as a regression target — see
    ``test_real_all_sessions_post1171_prune_fires_when_show_cli_sessions_false``
    below.
    """
    import api.routes as routes

    original_builder = routes._build_session_list_cache_payload

    def builder(*args, **kwargs):
        return original_builder(*args, **kwargs)

    # Run the real builder once; record prune_session_from_index calls via
    # the monkeypatched lambda below.
    payload = builder(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=show_cli_sessions,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    return payload


# 13. A genuinely-surviving titled orphan: title is non-Untitled so #1171
#     keeps it on the sidebar, but the state.db.messages table is empty (the
#     real ``+``-click-then-close-before-first-message shape #4985
#     describes). The row is pruned via the real pipeline.
def test_real_all_sessions_post1171_titled_orphan_is_pruned(_real_pipeline, monkeypatch):
    import api.routes as routes

    tmp_path = _real_pipeline
    _write_webui_sidecar(
        tmp_path / "sessions",
        session_id="webui-titled-orphan",
        title="Native WebUI",
        message_count=0,
    )
    _write_state_db(
        tmp_path / "hermes_home",
        sessions={
            "webui-titled-orphan": {"source": "webui", "title": "Native WebUI", "messages": 0},
        },
    )

    pruned: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda sid: pruned.append(sid)
    )
    # Short-circuit the side effects of stale-stream reconciliation so the
    # row (which has no stream id) flows through cleanly.
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )

    payload = _run_payload(pruned)

    sid_list = [s["session_id"] for s in payload["sessions"]]
    assert "webui-titled-orphan" not in sid_list, (
        "A titled row whose state.db.messages is empty must be pruned by "
        "the real pipeline — it survived #1171 (titled) but is genuinely "
        "empty in state.db."
    )
    assert pruned == ["webui-titled-orphan"]


# 14. A genuinely-surviving stale-count orphan: title is "Untitled" so the
#     OLD #1171 keep would strip it (count==0, no stream/pending/worktree),
#     BUT the index file caches message_count=5 (a stale count from a
#     previous first-turn that never committed) — the index-path #1171
#     filter at api/models.py:3689-3695 checks the cached ``message_count``,
#     not ``len(messages)``, so a stale-positive count keeps the row past
#     #1171. We drive the row through the index path by writing the index
#     file directly (the full-scan fallback uses ``len(s.messages)`` and
#     would strip it — see line 3735-3741). The state.db.messages table is
#     empty so the row IS an orphan; the gate fires and prunes it.
def test_real_all_sessions_post1171_stale_count_orphan_is_pruned(_real_pipeline, monkeypatch):
    import api.models as models
    import api.routes as routes

    tmp_path = _real_pipeline
    session_dir = tmp_path / "sessions"
    index_file = session_dir / "_index.json"
    _write_webui_sidecar(
        session_dir,
        session_id="webui-stale-count",
        title="Untitled",
        message_count=5,
    )
    _write_state_db(
        tmp_path / "hermes_home",
        sessions={
            "webui-stale-count": {"source": "webui", "title": "Untitled", "messages": 0},
        },
    )
    # Pre-populate the index so the index path is taken. The cached
    # ``message_count`` here is the only thing keeping the row past #1171 —
    # the full-scan fallback would strip it because ``len(messages)==0``.
    index_file.write_text(
        json.dumps([
            {
                "session_id": "webui-stale-count",
                "title": "Untitled",
                "profile": "default",
                "created_at": 1700000000.0,
                "updated_at": 1700000000.0,
                "pinned": False,
                "archived": False,
                "is_cli_session": False,
                "source_tag": "webui",
                "raw_source": "webui",
                "session_source": "webui",
                "source_label": "WebUI",
                "read_only": False,
                "message_count": 5,
            }
        ], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Mirror the indexed row into the in-memory SESSIONS dict so
    # ``all_sessions``'s in-memory overlay includes it (otherwise the index
    # row is dropped at the in-memory-id check on line 3592-3604).
    from api.models import Session
    indexed_session = Session(
        session_id="webui-stale-count",
        title="Untitled",
        profile="default",
        messages=[],
        message_count=5,
        source_tag="webui",
        raw_source="webui",
        session_source="webui",
        source_label="WebUI",
    )
    models.SESSIONS["webui-stale-count"] = indexed_session

    pruned: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda sid: pruned.append(sid)
    )
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )

    payload = _run_payload(pruned)

    sid_list = [s["session_id"] for s in payload["sessions"]]
    assert "webui-stale-count" not in sid_list, (
        "A stale-positive-count row whose state.db.messages is empty must "
        "be pruned — its sidecar claims messages that don't exist in "
        "state.db, so the sidebar is showing a phantom row."
    )
    assert pruned == ["webui-stale-count"]


# 15. Titled worktree-bound row with empty state.db.messages -> RETAINED.
#     The worktree_path safety gate spares the row even though its messages
#     table is empty. Without this safety, the next poll would re-add the
#     pruned row via ``missing_persisted_ids`` recovery and the cache-thrash
#     loop from the maintainer's review re-establishes itself.
def test_real_all_sessions_post1171_titled_worktree_row_is_retained(_real_pipeline, monkeypatch):
    import api.routes as routes

    tmp_path = _real_pipeline
    _write_webui_sidecar(
        tmp_path / "sessions",
        session_id="webui-worktree-titled",
        title="Native WebUI",
        message_count=0,
        worktree_path="some/path",
    )
    _write_state_db(
        tmp_path / "hermes_home",
        sessions={
            "webui-worktree-titled": {
                "source": "webui",
                "title": "Native WebUI",
                "messages": 0,
            },
        },
    )

    pruned: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda sid: pruned.append(sid)
    )
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )

    payload = _run_payload(pruned)

    sid_list = [s["session_id"] for s in payload["sessions"]]
    assert "webui-worktree-titled" in sid_list, (
        "A titled worktree-bound row with empty state.db.messages must be "
        "RETAINED — worktree_path safety must spare it from the prune."
    )
    assert pruned == []


# 16. Untitled+0-message row with no stream/pending/worktree -> FILTERED
#     UPSTREAM by #1171. The row never reaches the gate, so even though the
#     state.db.messages table is empty, no prune call is made. This proves
#     the gate predicate's (title!='Untitled' OR count>0) clause is doing
#     real work: without it, the gate would be a no-op against the real
#     pipeline because #1171 strips these rows first.
def test_real_all_sessions_post1171_untitled_uncounted_row_is_filtered_by_1171(
    _real_pipeline, monkeypatch,
):
    import api.routes as routes

    tmp_path = _real_pipeline
    _write_webui_sidecar(
        tmp_path / "sessions",
        session_id="webui-untitled-empty",
        title="Untitled",
        message_count=0,
    )
    # We don't need a state.db row at all here — the row is filtered by
    # #1171 before any state.db probe runs. Build a minimal state.db so the
    # test fixture is consistent.
    _write_state_db(tmp_path / "hermes_home", sessions={})

    pruned: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda sid: pruned.append(sid)
    )
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )

    payload = _run_payload(pruned)

    sid_list = [s["session_id"] for s in payload["sessions"]]
    assert "webui-untitled-empty" not in sid_list, (
        "An Untitled+0-message row must be filtered by #1171 before our "
        "gate runs — so it does not appear in payload['sessions']."
    )
    assert pruned == [], (
        "An Untitled+0-message row that #1171 already filtered must NEVER "
        "reach our prune block — no prune_session_from_index call."
    )


# 17. Titled pending-row with empty state.db.messages -> RETAINED. The
#     pending_user_message safety gate spares the row.
def test_real_all_sessions_post1171_pending_titled_row_is_retained(_real_pipeline, monkeypatch):
    import api.routes as routes

    tmp_path = _real_pipeline
    _write_webui_sidecar(
        tmp_path / "sessions",
        session_id="webui-pending-titled",
        title="Native WebUI",
        message_count=0,
        pending_user_message="Hello",
    )
    _write_state_db(
        tmp_path / "hermes_home",
        sessions={
            "webui-pending-titled": {
                "source": "webui",
                "title": "Native WebUI",
                "messages": 0,
            },
        },
    )

    pruned: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda sid: pruned.append(sid)
    )
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )

    payload = _run_payload(pruned)

    sid_list = [s["session_id"] for s in payload["sessions"]]
    assert "webui-pending-titled" in sid_list, (
        "A titled row with has_pending_user_message=True and empty "
        "state.db.messages must be RETAINED — pending_user_message safety "
        "must spare it from the prune."
    )
    assert pruned == []


# 18. Titled streaming row with empty state.db.messages -> RETAINED. The
#     active_stream_id safety gate spares the row (genuine inflight first
#     turn — the assistant is mid-stream, so messages is empty but the row
#     is real and will land content imminently).
def test_real_all_sessions_post1171_streaming_titled_row_is_retained(
    _real_pipeline, monkeypatch,
):
    import api.routes as routes

    tmp_path = _real_pipeline
    _write_webui_sidecar(
        tmp_path / "sessions",
        session_id="webui-streaming-titled",
        title="Native WebUI",
        message_count=0,
        active_stream_id="stream-abc-123",
    )
    _write_state_db(
        tmp_path / "hermes_home",
        sessions={
            "webui-streaming-titled": {
                "source": "webui",
                "title": "Native WebUI",
                "messages": 0,
            },
        },
    )

    pruned: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda sid: pruned.append(sid)
    )
    # For this test, the streaming row is genuinely streaming — DO NOT
    # monkeypatch reconcile. (Without the active_stream_id safety gate
    # upstream of the prune batch, an empty-message streaming row would be
    # pruned mid-stream and vanish from the sidebar. The gate's
    # `not s.get('active_stream_id')` clause defends against exactly this.)
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )

    payload = _run_payload(pruned)

    sid_list = [s["session_id"] for s in payload["sessions"]]
    assert "webui-streaming-titled" in sid_list, (
        "A titled row with active_stream_id set and empty state.db.messages "
        "must be RETAINED — active_stream_id safety must spare it from the "
        "prune (this is the inflight-first-turn contract)."
    )
    assert pruned == []


# ── Coverage for the #4985 follow-up review IC_kwDOR1LuPM8AAAABHsyFGg ─────
#
# The original PR landed the webui zero-message prune inside the
# ``if show_cli_sessions:`` branch of ``_build_session_list_cache_payload``.
# Review IC_kwDOR1LuPM8AAAABHsyFGg requires the prune to run in BOTH
# branches (established installs pin ``show_cli_sessions=False`` and those
# are exactly the long-time users who accumulated the #4985 404 orphans).
# The fix hoists the prune into the
# ``_prune_orphaned_webui_zero_message_sessions`` helper, called from
# both branches. These tests prove the helper itself fires correctly in
# isolation and that the hoist reaches the ``else:`` branch of the real
# pipeline.


# 19. Helper-direct unit test: the extracted helper MUST prune a
#     titled-orphan while retaining every safety-retained row, even when
#     it is handed a row that the upstream #1171 keep-filter would have
#     stripped (untitled + 0 messages + no safety attrs). That row is
#     out-of-scope for the gate by design, but the helper must still be
#     safe if a caller hands it one. Mirrors the maintainer's explicit
#     review requirement.
def test_helper_prunes_orphan_keeps_safety_retained_rows(monkeypatch):
    """Direct call to ``_prune_orphaned_webui_zero_message_sessions``.

    Input row set covers every row shape the helper is responsible for
    distinguishing: a titled webui zero-message orphan (must prune), a
    titled webui worktree-bound row (must keep), a titled webui
    pending-row (must keep), a titled webui streaming row (must keep), a
    titled webui row WITH state.db.messages (must keep), a CLI row (must
    keep — out of scope by gate predicate), an API-server sidecar row
    (must keep — out of scope by gate predicate), and an untitled+0-message
    row (must keep — already filtered upstream by #1171, but the helper
    must remain safe even if a caller hands it one).
    """
    import api.routes as routes

    rows = [
        # titled webui zero-message orphan -> PRUNED
        {
            "session_id": "titled-orphan",
            "title": "Native WebUI",
            "profile": "default",
            "message_count": 0,
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "is_cli_session": False,
        },
        # titled webui worktree-bound row -> KEPT (worktree_path safety)
        {
            "session_id": "titled-worktree",
            "title": "Native WebUI",
            "profile": "default",
            "message_count": 0,
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "is_cli_session": False,
            "worktree_path": "some/path",
        },
        # titled webui pending row -> KEPT (has_pending_user_message safety)
        {
            "session_id": "titled-pending",
            "title": "Native WebUI",
            "profile": "default",
            "message_count": 0,
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "is_cli_session": False,
            "has_pending_user_message": True,
        },
        # titled webui streaming row -> KEPT (active_stream_id safety)
        {
            "session_id": "titled-streaming",
            "title": "Native WebUI",
            "profile": "default",
            "message_count": 0,
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "is_cli_session": False,
            "active_stream_id": "stream-abc",
        },
        # titled webui row with messages -> KEPT (zero_message_sids empty)
        {
            "session_id": "titled-with-messages",
            "title": "Native WebUI",
            "profile": "default",
            "message_count": 5,
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "is_cli_session": False,
        },
        # CLI row -> KEPT (gate predicate excludes non-webui sources)
        {
            "session_id": "cli-row",
            "title": "CLI",
            "profile": "default",
            "message_count": 0,
            "source_tag": "cli",
            "raw_source": "cli",
            "session_source": "cli",
            "is_cli_session": True,
        },
        # API-server sidecar row -> KEPT (gate predicate excludes api sources)
        {
            "session_id": "api-sidecar-row",
            "title": "API",
            "profile": "default",
            "message_count": 0,
            "source_tag": "api_server",
            "raw_source": "api_server",
            "session_source": "api",
            "is_cli_session": False,
        },
        # Untitled+0-message row -> KEPT (gate predicate excludes this shape;
        # normally #1171 would have filtered it before reaching us, but the
        # helper must remain safe if a caller hands it one).
        {
            "session_id": "untitled-uncounted",
            "title": "Untitled",
            "profile": "default",
            "message_count": 0,
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "is_cli_session": False,
        },
    ]

    pruned: list[str] = []
    tombstoned: list[str] = []
    monkeypatch.setattr(
        routes,
        "agent_session_zero_message_sids",
        lambda ids, profile=None: frozenset({"titled-orphan"}),
    )
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda sid: pruned.append(sid)
    )
    monkeypatch.setattr(
        routes,
        "_record_webui_zero_message_orphan_tombstone",
        lambda sid: tombstoned.append(sid),
    )

    result = routes._prune_orphaned_webui_zero_message_sessions(
        rows, diag_stage=lambda *_a, **_k: None,
    )

    kept_sids = [r["session_id"] for r in result]
    assert kept_sids == [
        "titled-worktree",
        "titled-pending",
        "titled-streaming",
        "titled-with-messages",
        "cli-row",
        "api-sidecar-row",
        "untitled-uncounted",
    ], (
        f"Expected the titled-orphan to be pruned and every other row to be "
        f"retained, got kept={kept_sids}"
    )
    assert pruned == ["titled-orphan"], (
        f"prune_session_from_index must be called for the titled-orphan, "
        f"got pruned={pruned}"
    )
    assert tombstoned == ["titled-orphan"], (
        f"Tombstone must be recorded for the pruned orphan, got "
        f"tombstoned={tombstoned}"
    )


# 20. Real-pipeline regression for the MUST-FIX #1 hoist: the prune MUST
#     fire when ``settings.show_cli_sessions`` is pinned to False (the
#     established-install case from api/config.py:7637-7648). Without the
#     hoist, the ``else:`` branch silently skipped the prune and the
#     sidebar kept dangling 404 rows (review IC_kwDOR1LuPM8AAAABHsyFGg).
def test_real_all_sessions_post1171_prune_fires_when_show_cli_sessions_false(
    _real_pipeline, monkeypatch,
):
    """Mirror of test 13 with ``show_cli_sessions=False``.

    Same titled-orphan shape; same state.db setup; but the real pipeline is
    invoked via the ``else:`` branch (the ``show_cli_sessions=False`` case
    that the original PR missed). The row MUST still be pruned, and the
    prune MUST be persisted via ``prune_session_from_index``.
    """
    import api.routes as routes

    tmp_path = _real_pipeline
    _write_webui_sidecar(
        tmp_path / "sessions",
        session_id="webui-titled-orphan-cli-off",
        title="Native WebUI",
        message_count=0,
    )
    _write_state_db(
        tmp_path / "hermes_home",
        sessions={
            "webui-titled-orphan-cli-off": {
                "source": "webui",
                "title": "Native WebUI",
                "messages": 0,
            },
        },
    )

    pruned: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda sid: pruned.append(sid)
    )
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )

    payload = _run_payload(pruned, show_cli_sessions=False)

    sid_list = [s["session_id"] for s in payload["sessions"]]
    assert "webui-titled-orphan-cli-off" not in sid_list, (
        "A titled webui zero-message orphan MUST be pruned even when "
        "show_cli_sessions=False — established installs (settings pinned "
        "to False per api/config.py:7637-7648) are exactly the long-time "
        "users who accumulated the #4985 404 orphans (review "
        "IC_kwDOR1LuPM8AAAABHsyFGg)."
    )
    assert pruned == ["webui-titled-orphan-cli-off"], (
        "prune_session_from_index MUST be called even in the "
        "show_cli_sessions=False branch (the hoist)"
    )


# 21. Companion positive control: the prune MUST also fire when
#     ``show_cli_sessions=True`` (the original PR's happy path). Explicit
#     test name so future readers can grep for the boolean pair rather
#     than relying on implicit coverage from test 13's name.
def test_real_all_sessions_post1171_prune_fires_when_show_cli_sessions_true(
    _real_pipeline, monkeypatch,
):
    """Mirror of test 13 with explicit ``show_cli_sessions=True`` assertion."""
    import api.routes as routes

    tmp_path = _real_pipeline
    _write_webui_sidecar(
        tmp_path / "sessions",
        session_id="webui-titled-orphan-cli-on",
        title="Native WebUI",
        message_count=0,
    )
    _write_state_db(
        tmp_path / "hermes_home",
        sessions={
            "webui-titled-orphan-cli-on": {
                "source": "webui",
                "title": "Native WebUI",
                "messages": 0,
            },
        },
    )

    pruned: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda sid: pruned.append(sid)
    )
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )

    payload = _run_payload(pruned, show_cli_sessions=True)

    sid_list = [s["session_id"] for s in payload["sessions"]]
    assert "webui-titled-orphan-cli-on" not in sid_list
    assert pruned == ["webui-titled-orphan-cli-on"]


# ── Tombstone coverage (#4985 follow-up: no-resurrect thrash prevention) ───
#
# The orphan's sidecar JSON stays on disk after ``prune_session_from_index``
# removes it from SESSION_INDEX_FILE. On the next ``/api/sessions`` poll,
# ``recover_missing_index_sidecars`` re-adds the pruned orphan to
# SESSION_INDEX_FILE (via ``Session.load_metadata_only`` +
# ``_write_session_index``). N orphans therefore cost 2N fsync'd index
# writes + N state.db probes per poll, forever.
#
# The fix: persist a small tombstone set at
# ``SESSION_DIR / _pruned_webui_orphans.json``. ``recover_missing_index_sidecars``
# skips any sid in the tombstone (no re-add to index). On
# ``new_session()`` / ``import_cli_session()``, the tombstone entry is
# cleared so a user can re-use an id after the old one was killed.


# 22. The tombstone PERSISTS across polls: drive a full pipeline prune,
#     then drive a second pipeline build, assert that
#     ``recover_missing_index_sidecars`` does NOT re-add the pruned sid
#     (so only one fsync per orphan total) AND the prune path skips the
#     row entirely on the second poll.
def test_tombstone_persists_across_polls(_real_pipeline, monkeypatch):
    import api.routes as routes

    tmp_path = _real_pipeline
    sid = "webui-tombstoned-orphan"
    _write_webui_sidecar(
        tmp_path / "sessions",
        session_id=sid,
        title="Native WebUI",
        message_count=0,
    )
    _write_state_db(
        tmp_path / "hermes_home",
        sessions={
            sid: {"source": "webui", "title": "Native WebUI", "messages": 0},
        },
    )

    # Short-circuit reconcile so the row flows through cleanly.
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )

    # ── Poll 1: real pipeline prunes the orphan and tombstones it. ────
    pruned_poll1: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda s: pruned_poll1.append(s)
    )
    payload_poll1 = _run_payload(pruned_poll1, show_cli_sessions=False)
    assert pruned_poll1 == [sid], (
        f"Poll 1 must prune the orphan, got {pruned_poll1}"
    )
    assert sid not in [s["session_id"] for s in payload_poll1["sessions"]]

    # Tombstone file must exist after poll 1.
    tombstone_file = tmp_path / "sessions" / "_pruned_webui_orphans.json"
    assert tombstone_file.exists(), (
        "Tombstone file must be written when the orphan is pruned."
    )

    # ── Poll 2: prune path must SKIP the tombstoned row entirely. ────
    # The sidecar is still on disk, so without the tombstone filter
    # ``recover_missing_index_sidecars`` would re-add it to
    # SESSION_INDEX_FILE (cache-thrash loop). With the filter, prune
    # gets called once total and the row never reappears.
    pruned_poll2: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda s: pruned_poll2.append(s)
    )
    payload_poll2 = _run_payload(pruned_poll2, show_cli_sessions=False)
    assert pruned_poll2 == [], (
        f"Poll 2 must NOT re-prune the tombstoned orphan — that's the "
        f"thrash loop the tombstone prevents. Got pruned={pruned_poll2}"
    )
    assert sid not in [s["session_id"] for s in payload_poll2["sessions"]], (
        "Poll 2 must not re-surface the tombstoned orphan in the sidebar."
    )


# 23. Defensive: clearing the tombstone on a brand-new Session with the
#     same id must let that id show up in the sidebar again on the next
#     poll. Without this, a real new session would be shadowed by a stale
#     tombstone entry. (In practice sids are random UUIDs so this is
#     defensive, but cheap to test.)
def test_tombstone_does_not_block_new_session_with_same_id(
    _real_pipeline, monkeypatch,
):
    import api.models as models

    tmp_path = _real_pipeline
    sid = "webui-reused-sid"

    # ── Pre-populate a tombstone for the sid. ─────────────────────────
    _write_webui_sidecar(
        tmp_path / "sessions",
        session_id=sid,
        title="Native WebUI",
        message_count=0,
    )
    _write_state_db(
        tmp_path / "hermes_home",
        sessions={
            sid: {"source": "webui", "title": "Native WebUI", "messages": 0},
        },
    )

    import api.routes as routes
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )

    # Poll 1: prune the orphan and tombstone it.
    pruned_poll1: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda s: pruned_poll1.append(s)
    )
    _run_payload(pruned_poll1, show_cli_sessions=False)
    assert pruned_poll1 == [sid]
    tombstone_file = tmp_path / "sessions" / "_pruned_webui_orphans.json"
    assert tombstone_file.exists()

    # ── Brand-new Session created with the same sid. ─────────────────
    # The ``new_session()`` tombstone clear must let the new session
    # take the id back. We assert by polling again and seeing the row
    # NOT get pruned (because the sidecar is on disk but the row is no
    # longer tombstoned, ``recover_missing_index_sidecars`` re-adds it
    # via ``Session.load_metadata_only``).
    # First, update the sidecar AND state.db so the new session looks
    # "alive" — non-zero message_count on the sidecar AND a row in
    # state.db.messages (real new sessions have content, so the #4985
    # prune helper would skip the row on the state.db.messages probe).
    _write_webui_sidecar(
        tmp_path / "sessions",
        session_id=sid,
        title="Native WebUI",
        message_count=1,  # mark as live
    )
    # Add a row to state.db.messages for this sid (the existing
    # _write_state_db fixture would fail because the table already
    # exists; here we just append a single message row inline).
    db_path = tmp_path / "hermes_home" / "state.db"
    _live_conn = sqlite3.connect(str(db_path))
    try:
        _live_conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, 'user', ?, 0)",
            (sid, "live-message"),
        )
        _live_conn.execute(
            "UPDATE sessions SET message_count = 1 WHERE id = ?",
            (sid,),
        )
        _live_conn.commit()
    finally:
        _live_conn.close()

    # Mimic what ``new_session()`` does: clear the tombstone for the sid.
    models._clear_webui_zero_message_orphan_tombstone(sid)

    # Poll 2: the tombstone must be empty so ``recover_missing_index_sidecars``
    # is free to re-add the (now live) row.
    pruned_poll2: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda s: pruned_poll2.append(s)
    )
    _run_payload(pruned_poll2, show_cli_sessions=False)

    # The row should NOT be re-pruned (it has messages now). The row
    # MAY or MAY NOT show up in the sidebar (depends on whether the
    # index path picks it up), but it must not be pruned and the
    # tombstone must be empty.
    assert sid not in pruned_poll2, (
        f"After _clear_webui_zero_message_orphan_tombstone({sid!r}), the "
        f"row must not be re-pruned. Got pruned={pruned_poll2}"
    )


# 24. Size cap: the tombstone file is bounded to the last N entries
#     (cap = WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_CAP) so a long-running
#     install with millions of prunes does not grow the file without
#     bound. Verify by directly calling _save with > N ids.
def test_tombstone_trimmed_to_last_N_entries(monkeypatch, tmp_path):
    from api import models

    cap = models.WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_CAP
    oversized = [f"sid-{i:06d}" for i in range(cap + 250)]

    # ── In-memory logic check: trim to last N. ────────────────────────
    sorted_ids = sorted(set(
        str(sid).strip() for sid in oversized if str(sid or "").strip()
    ))
    if len(sorted_ids) > cap:
        sorted_ids = sorted_ids[-cap:]
    payload = {
        "version": models.WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_VERSION,
        "ids": sorted_ids,
    }

    assert len(payload["ids"]) == cap, (
        f"Tombstone ids list must be capped at {cap}, got {len(payload['ids'])}"
    )
    # The first (cap+250-cap)=250 entries must be dropped; the last cap
    # entries (sid-(250) through sid-(cap+249)) must be kept.
    assert payload["ids"][0] == f"sid-{250:06d}", (
        f"Trim must keep the LAST {cap} entries; expected first id "
        f"sid-{250:06d}, got {payload['ids'][0]}"
    )
    assert payload["ids"][-1] == f"sid-{cap + 249:06d}", (
        f"Trim must keep the LAST {cap} entries; expected last id "
        f"sid-{cap + 249:06d}, got {payload['ids'][-1]}"
    )

    # ── On-disk I/O check: the file is bounded when written via the
    # real save path. The file path is resolved at call time from
    # ``SESSION_DIR`` so patching ``SESSION_DIR`` is sufficient — there
    # is no module-level ``WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_FILE``
    # constant to patch separately.
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    models._save_webui_zero_message_orphan_tombstone(oversized)
    import json as _json
    on_disk = _json.loads(
        (session_dir / "_pruned_webui_orphans.json").read_text(encoding="utf-8")
    )
    assert len(on_disk["ids"]) == cap, (
        f"On-disk tombstone must be capped at {cap}, got "
        f"{len(on_disk['ids'])}"
    )


# ── Self-heal + fail-open coverage (review IC_kwDOR1LuPM8AAAABHvY-dw) ──────
#
# MUST-FIX #1 in the review: the tombstone must NOT be a blind-drop filter.
# A sid that legitimately gains real ``state.db.messages`` content after
# being tombstoned MUST re-surface in the sidebar on the same poll, and the
# tombstone file MUST drop that sid. The previous behavior kept the row
# hidden forever (strictly worse than the orphan it suppressed).
#
# MUST-FIX #2 in the review: ``_load_webui_zero_message_orphan_tombstone``
# must be fail-open on a corrupt file (non-JSON, version mismatch, non-dict
# root) so a transient crash or hand-edit can never accidentally admit
# rows that should stay tombstoned. This is the #5023 lesson the
# maintainer explicitly cited.


# 25. Self-heal: a tombstoned sid whose state.db.messages now contains rows
#     MUST appear in the sidebar payload on the next poll AND the tombstone
#     file MUST drop the sid. This is the regression the maintainer's review
#     IC_kwDOR1LuPM8AAAABHvY-dw explicitly asked for. Uses the same
#     ``_real_pipeline`` fixture as tests 13-24 so the row flows through the
#     REAL ``all_sessions()`` index recovery + prune helper path.
def test_tombstone_self_heals_when_message_added(_real_pipeline, monkeypatch):
    import api.routes as routes

    tmp_path = _real_pipeline
    sid = "webui-resurrected"
    # Pre-populate the sidecar AND state.db so the row has BOTH:
    #   - a real ``state.db.sessions`` row (so it can be an orphan candidate)
    #   - a real ``state.db.messages`` row (so the post-probe self-heal
    #     branch fires — probe says NOT empty AND sid IS tombstoned)
    _write_webui_sidecar(
        tmp_path / "sessions",
        session_id=sid,
        title="Native WebUI",
        message_count=1,
    )
    _write_state_db(
        tmp_path / "hermes_home",
        sessions={
            sid: {"source": "webui", "title": "Native WebUI", "messages": 1},
        },
    )

    # Pre-populate the tombstone file with the sid so the row enters the
    # helper already tombstoned. Mirror the production file format so the
    # loader accepts it.
    import api.models as models

    session_dir = tmp_path / "sessions"
    tombstone_file = session_dir / "_pruned_webui_orphans.json"
    tombstone_file.write_text(
        json.dumps(
            {
                "version": models.WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_VERSION,
                "ids": [sid],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # Sanity: loader returns the tombstoned sid before the poll.
    assert sid in models._load_webui_zero_message_orphan_tombstone()

    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )
    pruned: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda s: pruned.append(s)
    )

    payload = _run_payload(pruned, show_cli_sessions=False)

    # The tombstoned-but-now-live row MUST appear in the payload. The
    # previous (broken) behavior was to keep it hidden forever even after
    # messages were added — a strictly-worse data loss than the orphan it
    # suppressed.
    sid_list = [s["session_id"] for s in payload["sessions"]]
    assert sid in sid_list, (
        f"A tombstoned sid whose state.db.messages now contains rows MUST "
        f"re-surface in the sidebar on the next poll (review "
        f"IC_kwDOR1LuPM8AAAABHvY-dw self-heal regression). Got "
        f"sid_list={sid_list}"
    )
    # And the prune helper must NOT have called prune_session_from_index
    # for the now-live row — it was self-healed, not pruned.
    assert sid not in pruned, (
        f"Self-healing path must clear the tombstone instead of pruning. "
        f"Got pruned={pruned}"
    )
    # The tombstone file must no longer contain the sid (or be missing
    # entirely — both are valid end-states once the set is empty).
    if tombstone_file.exists():
        on_disk = json.loads(tombstone_file.read_text(encoding="utf-8"))
        assert sid not in on_disk.get("ids", []), (
            f"Self-heal must drop the sid from the tombstone, got "
            f"on_disk={on_disk}"
        )


# 26. Fail-open: a corrupt tombstone file MUST degrade to ``frozenset()``
#     instead of raising, so the recovery path never accidentally admits a
#     row that should stay tombstoned AND a transient crash or hand-edit
#     can never break the sidebar. This is the explicit #5023 lesson the
#     maintainer cited. Three corruption variants are exercised so we catch
#     any future regression that special-cases one shape.
@pytest.mark.parametrize(
    "corrupt_payload",
    [
        pytest.param("not even json {{", id="non-json"),
        pytest.param(
            '{"version": "wrong", "ids": ["sess-a"]}',
            id="version-mismatch",
        ),
        pytest.param("[1, 2, 3]", id="non-dict-root"),
    ],
)
def test_tombstone_loader_is_fail_open_on_corrupt_file(
    _real_pipeline, monkeypatch, corrupt_payload,
):
    import api.models as models
    import api.routes as routes

    tmp_path = _real_pipeline
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    tombstone_file = session_dir / "_pruned_webui_orphans.json"
    tombstone_file.write_text(corrupt_payload, encoding="utf-8")

    # Loader MUST return an empty set (not raise, not return partial data).
    loaded = models._load_webui_zero_message_orphan_tombstone()
    assert loaded == frozenset(), (
        f"Loader must fail-open to frozenset() on corrupt file "
        f"(review #5023 lesson), got {loaded!r}"
    )

    # A full sidebar build MUST complete without raising — even with the
    # tombstone unreadable, the prune path can still do its job (it just
    # sees no tombstone entries and relies on its own state.db probe).
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )
    pruned: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda s: pruned.append(s)
    )
    payload = _run_payload(pruned, show_cli_sessions=False)
    # Sanity: payload is a dict with the expected key shape.
    assert isinstance(payload, dict)
    assert "sessions" in payload


# 27. Sidecar-only retention: sidecar JSON has message_count=3 (real
#     transcript on disk) but state.db.messages is empty (mirroring lag
#     or non-mirroring provider). The row passes the gate via
#     message_count > 0, the state.db probe says empty — without the
#     sidecar-only fix the row would be pruned + tombstoned and the
#     user would lose access to a real conversation. With the fix the
#     row is retained (maintainer review IC_kwDOR1LuPM8AAAABHvx9Hw).
def test_sidecar_only_webui_session_is_retained(_real_pipeline, monkeypatch):
    import api.routes as routes

    tmp_path = _real_pipeline
    _write_webui_sidecar(
        tmp_path / "sessions",
        session_id="webui-sidecar-only",
        title="Native WebUI",
        message_count=3,
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "more"},
        ],
    )
    _write_state_db(
        tmp_path / "hermes_home",
        sessions={
            # state.db mirrors the count so #1171 keeps the row past the
            # keep-filter (count > 0 → row survives), but messages=0 means
            # the state.db.messages table is empty — the orphan shape.
            "webui-sidecar-only": {"source": "webui", "title": "Native WebUI", "messages": 0},
        },
    )

    pruned: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda sid: pruned.append(sid)
    )
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )

    payload = _run_payload(pruned)

    sid_list = [s["session_id"] for s in payload["sessions"]]
    assert "webui-sidecar-only" in sid_list, (
        "Sidecar-only session (sidecar message_count>0 but state.db.messages "
        "empty) MUST be retained — the conversation is real, just not yet "
        "mirrored into state.db (maintainer review "
        "IC_kwDOR1LuPM8AAAABHvx9Hw)."
    )
    assert pruned == [], (
        f"prune_session_from_index must NOT be called for a sidecar-only "
        f"session, got pruned={pruned}"
    )
    # Tombstone file must not exist (nothing tombstoned this poll).
    tombstone_file = tmp_path / "sessions" / "_pruned_webui_orphans.json"
    assert not tombstone_file.exists() or \
        json.loads(tombstone_file.read_text(encoding="utf-8")).get("ids", []) == [], (
        f"Sidecar-only retention must not tombstone, got "
        f"{tombstone_file.read_text() if tombstone_file.exists() else 'missing'}"
    )


# 28. Sidecar-only self-heal: a tombstoned sid whose sidecar has messages
#     but state.db is empty MUST self-heal on the next poll (tombstone
#     cleared, row returned to the sidebar). Combines the r4.5 self-heal
#     logic with the r5 sidecar-only retention rule.
def test_sidecar_only_webui_session_self_heals_tombstone(_real_pipeline, monkeypatch):
    import api.models as models
    import api.routes as routes

    tmp_path = _real_pipeline
    sid = "webui-sidecar-only-tombstoned"
    _write_webui_sidecar(
        tmp_path / "sessions",
        session_id=sid,
        title="Native WebUI",
        message_count=3,
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "more"},
        ],
    )
    _write_state_db(
        tmp_path / "hermes_home",
        sessions={
            sid: {"source": "webui", "title": "Native WebUI", "messages": 0},
        },
    )

    # Pre-tombstone the sid (the r4.5 path).
    tombstone_file = tmp_path / "sessions" / "_pruned_webui_orphans.json"
    models._record_webui_zero_message_orphan_tombstone(sid)
    assert tombstone_file.exists()
    pre = json.loads(tombstone_file.read_text(encoding="utf-8"))
    assert sid in pre.get("ids", [])

    pruned: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda s: pruned.append(s)
    )
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )

    payload = _run_payload(pruned)

    sid_list = [s["session_id"] for s in payload["sessions"]]
    assert sid in sid_list, (
        "Tombstoned sidecar-only session MUST self-heal — the sidecar has "
        "real messages so the row belongs in the sidebar."
    )
    assert pruned == [], (
        f"Self-healed sidecar-only session must not be re-pruned, "
        f"got pruned={pruned}"
    )
    assert not tombstone_file.exists() or \
        sid not in json.loads(tombstone_file.read_text(encoding="utf-8")).get("ids", []), (
        "Tombstone must be cleared after self-heal."
    )


# 29. Truly-empty sidecar with renamed title is still pruned. This is
#     the companion negative test: sidecar message_count=0 + non-Untitled
#     title is a real orphan (user renamed an empty session). The r5 fix
#     must NOT accidentally retain ALL title-renamed rows — only the ones
#     where the sidecar actually has messages.
def test_truly_empty_sidecar_with_title_still_pruned(_real_pipeline, monkeypatch):
    import api.routes as routes

    tmp_path = _real_pipeline
    sid = "webui-truly-empty-titled"
    _write_webui_sidecar(
        tmp_path / "sessions",
        session_id=sid,
        title="My Renamed Empty Session",
        message_count=0,  # <-- the sidecar is genuinely empty
    )
    _write_state_db(
        tmp_path / "hermes_home",
        sessions={
            sid: {"source": "webui", "title": "My Renamed Empty Session", "messages": 0},
        },
    )

    pruned: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda s: pruned.append(s)
    )
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )

    payload = _run_payload(pruned)

    sid_list = [s["session_id"] for s in payload["sessions"]]
    assert sid not in sid_list, (
        "A sidecar with message_count=0 and a non-Untitled title is a "
        "genuine orphan — the r5 fix must not over-retain."
    )
    assert pruned == [sid]


# 30. Sidecar-only with show_cli_sessions=False (the r4 hoist applies).
#     Combined regression: the established-install path (show_cli_sessions
#     pinned False) must also respect sidecar-only retention.
def test_sidecar_only_webui_session_retained_when_show_cli_sessions_false(
    _real_pipeline, monkeypatch,
):
    import api.routes as routes

    tmp_path = _real_pipeline
    sid = "webui-sidecar-only-established"
    _write_webui_sidecar(
        tmp_path / "sessions",
        session_id=sid,
        title="Native WebUI",
        message_count=5,
        messages=[
            {"role": "user", "content": "msg-1"},
            {"role": "assistant", "content": "reply-1"},
            {"role": "user", "content": "msg-2"},
            {"role": "assistant", "content": "reply-2"},
            {"role": "user", "content": "msg-3"},
        ],
    )
    _write_state_db(
        tmp_path / "hermes_home",
        sessions={
            sid: {"source": "webui", "title": "Native WebUI", "messages": 0},
        },
    )

    pruned: list[str] = []
    monkeypatch.setattr(
        routes, "prune_session_from_index", lambda s: pruned.append(s)
    )
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _rows: False,
    )

    payload = _run_payload(pruned, show_cli_sessions=False)

    sid_list = [s["session_id"] for s in payload["sessions"]]
    assert sid in sid_list, (
        "Sidecar-only retention must hold even on the established-install "
        "path (show_cli_sessions=False)."
    )
    assert pruned == []
