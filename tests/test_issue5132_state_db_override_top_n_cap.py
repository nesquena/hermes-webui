"""Regression tests for #5132 — cap sidebar state.db count/title overrides to top-N.

On power users with thousands of sessions, GET /api/sessions blocked 5-18s in the
all_sessions.state_db_overrides stage: _apply_sidebar_state_db_overrides probed
state.db for EVERY row (2400+ ids) on every concurrent poll, piling up sqlite reads
against the gateway-watcher + agent writes and flapping the UI to "Connection lost".

The fix is a TWO-TIER split (the cap on the whole probe was the first cut and was a
regression — see test_source_classification_is_never_capped below):

  * SOURCE classification (source/title) is read for ALL rows. It feeds the CLI/WebUI
    sidebar filter that runs BEFORE the lazy lineage correction, so capping it would
    silently drop rows whose stale JSON source disagrees with state.db.
  * The expensive MESSAGES aggregation (COUNT(*)/MAX(timestamp) GROUP BY) — the actual
    bottleneck — is capped to the top-N (default 60) paint-priority rows.

The cap is env-configurable (HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N) and fails open.

These tests pin: (1) source ids are NEVER capped; (2) count ids ARE capped to top-N;
(3) the env override bounds the COUNT tier; (4) a non-positive / unparseable cap;
(5) lists at/under the cap; (6) a DB error never breaks /api/sessions; (7) a probed
row's webui source is applied; (8) the end-to-end route regression: a stale-CLI
JSON row whose state.db source is webui must stay visible in a WebUI-filtered sidebar
even when it falls beyond the cap; and (9) the cap bounds ONLY the two expensive
passes — the SQL aggregation and the per-row transcript reconciliation — while the
cheap raw/actual count, last-message timestamps and visible fallback still run for
EVERY row (a first-append cross-surface continuation is a named, messageful
conversation and must stay in the public payload even when fillers own the window).
"""
from __future__ import annotations

import pytest

import api.models as models


def _capture_probed_ids(monkeypatch):
    """Patch _read_state_db_sidebar_overrides to record both id tiers it is asked for."""
    seen = {}

    def _fake_read(db_path, id_set, count_session_ids=None):
        seen["source_ids"] = set(id_set)
        seen["count_ids"] = None if count_session_ids is None else set(count_session_ids)
        return {}

    monkeypatch.setattr(models, "_read_state_db_sidebar_overrides", _fake_read)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    return seen


def _sessions(n):
    # Caller passes an already pinned-first/newest-first sorted list; index order
    # therefore IS paint priority. id "s0" is the most-recent/visible-most.
    return [{"session_id": f"s{i}"} for i in range(n)]


def test_source_classification_is_never_capped(monkeypatch):
    """SOURCE tier must cover ALL rows (capping it dropped rows from the sidebar — #5132 regression)."""
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    models._apply_sidebar_state_db_overrides(_sessions(1000))
    assert seen["source_ids"] == {f"s{i}" for i in range(1000)}, (
        "Source/title classification must be read for EVERY row, never capped"
    )


def test_count_aggregation_capped_to_top_n_default_300(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    models._apply_sidebar_state_db_overrides(_sessions(1000))
    assert seen["count_ids"] == {f"s{i}" for i in range(300)}, (
        "The message-count/latest-timestamp aggregation keeps the documented "
        "top-300 default: shrinking it to the paint window froze the sort "
        "order of settled conversations past the window"
    )


def test_env_override_changes_count_cap(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "50")
    models._apply_sidebar_state_db_overrides(_sessions(1000))
    assert seen["count_ids"] == {f"s{i}" for i in range(50)}, (
        "HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N must bound the COUNT tier"
    )
    assert seen["source_ids"] == {f"s{i}" for i in range(1000)}, (
        "...but the source tier stays uncapped"
    )


def test_non_positive_cap_disables_capping(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "0")
    models._apply_sidebar_state_db_overrides(_sessions(500))
    assert seen["count_ids"] is None, "cap<=0 must disable the count cap (count every row)"
    assert len(seen["source_ids"]) == 500


def test_unparseable_cap_falls_back_to_default(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "not-a-number")
    models._apply_sidebar_state_db_overrides(_sessions(1000))
    assert seen["count_ids"] == {f"s{i}" for i in range(300)}, (
        "An unparseable cap must fall back to the default 300, not crash"
    )


def test_grown_rows_beyond_the_paint_window_still_walk(monkeypatch):
    """No second window: the walk runs for every grown row, the SQL tier keeps its cap.

    A raw delta cannot tell a display row from a tool row, so the visible count
    of a capped-out row has no honest cheap source. The growth guard bounds the
    work instead: only rows state.db advanced past the sidecar reach the walk.
    """
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    reconciled = _capture_reconciled_ids(monkeypatch, 1000)
    models._apply_sidebar_state_db_overrides(_grown_sessions(1000))
    assert sorted(reconciled) == sorted(f"s{i}" for i in range(1000)), (
        f"every grown row must walk; got {len(reconciled)}"
    )


def test_removed_reconcile_window_env_does_not_narrow_the_walk(monkeypatch):
    """The old RECONCILE_TOP_N knob is gone: setting it must not skip any row."""
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_RECONCILE_TOP_N", "5")
    reconciled = _capture_reconciled_ids(monkeypatch, 20)
    models._apply_sidebar_state_db_overrides(_grown_sessions(20))
    assert sorted(reconciled) == sorted(f"s{i}" for i in range(20))


def test_list_under_cap_counts_everything(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    models._apply_sidebar_state_db_overrides(_sessions(50))
    assert seen["count_ids"] is None, "A list at/under the cap counts every row (no cap needed)"
    assert seen["source_ids"] == {f"s{i}" for i in range(50)}


def test_override_failure_is_swallowed(monkeypatch):
    """Override lookup must fail open — a DB error never breaks /api/sessions."""
    def _boom(db_path, id_set, count_session_ids=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(models, "_read_state_db_sidebar_overrides", _boom)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    # Must not raise.
    models._apply_sidebar_state_db_overrides(_sessions(10))


def test_capped_rows_still_receive_source_overrides(monkeypatch):
    """A row beyond the COUNT cap must still get its SOURCE metadata applied."""
    def _fake_read(db_path, id_set, count_session_ids=None):
        # Return a webui source override for s400 (beyond the default cap).
        return {
            "s400": {
                "_state_db_source": "webui",
                "_state_db_source_tag": "webui",
                "_state_db_raw_source": "webui",
                "_state_db_session_source": "webui",
                "_state_db_source_label": "WebUI",
            }
        }

    monkeypatch.setattr(models, "_read_state_db_sidebar_overrides", _fake_read)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    sessions: list[dict] = _sessions(500)
    sessions[400]["is_cli_session"] = True
    models._apply_sidebar_state_db_overrides(sessions)
    assert sessions[400]["is_cli_session"] is False, (
        "A row beyond the count cap with a webui state.db source must STILL be corrected "
        "(source classification is uncapped)"
    )


def test_stale_cli_json_beyond_cap_stays_webui_via_real_db(monkeypatch, tmp_path):
    """End-to-end #5132 regression: a stale-CLI JSON row whose state.db source is webui,
    sitting BEYOND the top-N cap, must keep source='webui' so it isn't filtered out of a
    WebUI-only sidebar. This is the exact row that vanished under the first (cap-everything) cut.
    """
    import sqlite3

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, session_source TEXT, title TEXT, message_count INTEGER)")
    # The stale row lives at index 400 (beyond the default cap). state.db says it's webui.
    conn.execute("INSERT INTO sessions (id, source, title, message_count) VALUES (?,?,?,?)",
                 ("s400", "webui", "Stale CLI row", 3))
    conn.commit()
    conn.close()

    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)

    sessions = _sessions(500)
    # JSON metadata wrongly marks it CLI.
    sessions[400]["is_cli_session"] = True
    sessions[400]["session_source"] = "cli"
    models._apply_sidebar_state_db_overrides(sessions)

    assert sessions[400]["is_cli_session"] is False, (
        "state.db source=webui must override the stale CLI JSON flag even beyond the cap"
    )
    assert sessions[400].get("session_source") == "webui", (
        "session_source must be reclassified to webui from state.db"
    )


# --- transcript reconciliation is part of the capped tier -------------------------
#
# The COUNT cap bounds TWO things, and only two:
#   * the SQL messages aggregation (``_read_state_db_sidebar_overrides``), and
#   * the per-row sidecar+state.db transcript merge
#     (``_reconciled_visible_count_for_sidebar``) — 98 grown rows x 0.19s =
#     18.8s of a 20s /api/sessions build, 307k _cached_message_key calls, which
#     is why TOP_N=20 and TOP_N=300 used to measure the same wall clock.
#
# Everything else in the overlay is CHEAP (sessions-table columns the caller
# already fetched) and it decides whether a row is publicly visible at all, so
# it runs for EVERY row. Gating the count repair on the window froze a
# cross-surface continuation at message_count 0 and dropped it from
# /api/sessions entirely — the zero-to-messageful membership case below.
#
# The deterministic performance oracle is unchanged: at most N transcript
# merges per build (_test_transcript_reconciliation_capped_to_top_n pins it).


def _grown_sessions(n, *, archived=False):
    """n rows all reporting fewer messages than state.db (so every one would reconcile)."""
    return [
        {
            "session_id": f"s{i}",
            "message_count": 1,
            "visible_message_count": 1,
            "actual_message_count": 1,
            "last_message_at": 1000.0,
            "updated_at": 1000.0,
            "archived": archived,
        }
        for i in range(n)
    ]


def _grown_metadata(n):
    return {
        f"s{i}": {
            "_state_db_source": "webui",
            "_state_db_message_count": 5,
            # The SQL read supplies the active-domain count next to the raw one;
            # the revision check takes its expectation from this key only.
            "_state_db_active_message_count": 5,
            "_state_db_last_message_at": 2000.0,
        }
        for i in range(n)
    }


def _capture_reconciled_ids(monkeypatch, n):
    """Patch the expensive per-row merge and record which session ids it was asked for."""
    reconciled: list[str] = []

    def _fake_reconciled(session_id, **_bounds):
        reconciled.append(str(session_id))
        return None

    monkeypatch.setattr(
        models, "_reconciled_visible_count_for_sidebar", _fake_reconciled
    )
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: _grown_metadata(n),
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    return reconciled


def test_grown_rows_outside_the_count_window_walk_too(monkeypatch):
    """With cap N the SQL tier still bounds to top-N, but the walk is not windowed.

    Cheap repairs reach every row; the visible count is walked for every grown
    row because the raw delta overstates a settled tool-using session.
    """
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "3")
    reconciled = _capture_reconciled_ids(monkeypatch, 10)
    sessions = _grown_sessions(10)

    models._apply_sidebar_state_db_overrides(sessions)

    assert sorted(reconciled) == [f"s{i}" for i in range(10)], (
        f"every grown row walks, window or not; got {sorted(reconciled)}"
    )
    # Rows inside the window still get the real state.db count.
    assert sessions[0]["message_count"] == 5
    # Rows outside it still get the CHEAP authoritative count + timestamps:
    # state.db is the cross-surface writer and a stale zero here would hide a
    # real conversation.
    assert [s["message_count"] for s in sessions[3:]] == [5] * 7, (
        "capped rows must still receive the cheap raw/actual count repair"
    )
    assert [s["last_message_at"] for s in sessions[3:]] == [2000.0] * 7, (
        "capped rows must still receive the cheap last-message bump"
    )


def test_capped_out_grown_row_takes_the_walk_not_the_raw_delta(monkeypatch):
    """Review 5593765140 item 1: a settled tool-using row past the old 60-row window.

    state.db's count includes rows the sidebar never renders (tool rows), so the
    raw delta (4 -> 8) must never become the visible total. The walk answers 4
    and is now the only source, window or not.
    """
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    sessions = _grown_sessions(61)
    sessions.append(
        {
            "session_id": "TOOLUSER",
            "message_count": 4,
            "visible_message_count": 4,
            "actual_message_count": 4,
            "last_message_at": 1003.5,
            "updated_at": 1003.5,
        }
    )
    metadata = _grown_metadata(61)
    metadata["TOOLUSER"] = {
        "_state_db_source": "webui",
        "_state_db_message_count": 8,
        "_state_db_last_message_at": 1004.0,
    }
    walked = []

    def _fake_walk(session_id, **_bounds):
        walked.append(str(session_id))
        return 4

    monkeypatch.setattr(models, "_reconciled_visible_count_for_sidebar", _fake_walk)
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: dict(metadata),
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")

    models._apply_sidebar_state_db_overrides(sessions)

    row = sessions[-1]
    assert "TOOLUSER" in walked, "the grown row must be walked, not delta-arithmetic'd"
    assert row["visible_message_count"] == 4, (
        "4 display rows in the sidecar; the 4 tool rows in state.db must not inflate"
    )
    assert row["message_count"] == 8, "the raw count stays authoritative for the row"


def test_transcript_reconciliation_uncapped_when_count_ids_is_none(monkeypatch):
    """count_session_ids=None (cap disabled or list under the cap) reconciles every row."""
    reconciled = _capture_reconciled_ids(monkeypatch, 10)

    models._apply_sidebar_state_db_override_metadata(
        _grown_sessions(10), _grown_metadata(10)
    )

    assert sorted(reconciled) == [f"s{i}" for i in range(10)], (
        "without a count_session_ids bound every grown row reconciles as before"
    )


def test_cap_disabled_reconciles_every_row_through_the_entry_point(monkeypatch):
    """TOP_N=0 disables the cap end to end: count_ids is None, so no row is skipped."""
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "0")
    reconciled = _capture_reconciled_ids(monkeypatch, 10)

    models._apply_sidebar_state_db_overrides(_grown_sessions(10))

    assert sorted(reconciled) == [f"s{i}" for i in range(10)], (
        "cap<=0 must restore the uncapped (pre-fix) reconciliation behaviour"
    )


# --- first-append cross-surface matrix (public membership) ------------------------
#
# default (60) and explicit 60, ordinary and archived fillers, repeated polls, and a
# cap-disabled control. A named conversation whose first user message arrived via
# state.db (CLI/Telegram continuation) must be publicly visible in EVERY one: its
# state.db count is authoritative and cheap to read.


def _first_append_sessions(fillers, *, archived_fillers=False):
    """Newer fillers own the pre-filter top-N window; the continued row is last."""
    rows = _grown_sessions(fillers, archived=archived_fillers)
    for index, row in enumerate(rows):
        row["title"] = f"Filler {index}"
        row["message_count"] = 3
        row["visible_message_count"] = 3
        row["actual_message_count"] = 3
        row["last_message_at"] = 10_000.0 - index
        row["updated_at"] = 10_000.0 - index
    rows.append(
        {
            "session_id": "CONTINUED",
            "title": "Named conversation",
            "message_count": 0,
            "visible_message_count": 0,
            "actual_message_count": 0,
            "last_message_at": 1.0,
            "updated_at": 1.0,
            "archived": False,
        }
    )
    return rows


def _patch_first_append(monkeypatch, sessions, *, reconciled_visible=1):
    reconciled = []

    def _fake_reconciled(session_id, **_bounds):
        reconciled.append(str(session_id))
        return reconciled_visible

    metadata = {}
    for s in sessions:
        sid = s["session_id"]
        metadata[sid] = {
            "_state_db_source": "webui",
            "_state_db_message_count": s["message_count"],
            "_state_db_last_message_at": s["last_message_at"],
        }
    metadata["CONTINUED"]["_state_db_message_count"] = 1
    metadata["CONTINUED"]["_state_db_last_message_at"] = 20_000.0

    monkeypatch.setattr(
        models, "_reconciled_visible_count_for_sidebar", _fake_reconciled
    )
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: dict(metadata),
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    return reconciled


def _sidecar_row_is_public(row):
    """Mirror of the visibility predicate.

    ``static/sessions.js:_sidebarRowHasVisibleMessages`` and
    ``routes._session_has_server_visible_messages`` both key on message_count
    first. A named (titled) row is not dropped by the #1171 Untitled+0 filter,
    so a repaired count is what puts it in the public payload.
    """
    return bool(int(row.get("message_count") or 0) > 0)


@pytest.mark.parametrize("archived_fillers", [False, True])
@pytest.mark.parametrize("fillers", [61, 200])
def test_first_append_row_is_publicly_visible_at_default_cap(
    monkeypatch, fillers, archived_fillers
):
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    sessions = _first_append_sessions(fillers, archived_fillers=archived_fillers)
    reconciled = _patch_first_append(monkeypatch, sessions)

    models._apply_sidebar_state_db_overrides(sessions)

    row = sessions[-1]
    assert row["session_id"] == "CONTINUED"
    assert _sidecar_row_is_public(row), (
        "a cross-surface first append must make the row public even when "
        f"top-N fillers (archived={archived_fillers}) consumed the window"
    )
    assert row["visible_message_count"] == 1, (
        "the visible fallback must credit the cheap raw growth"
    )
    assert row["last_message_at"] == 20_000.0
    assert reconciled == ["CONTINUED"], (
        "the continued row is the only grown row here, so only it walks"
    )


def test_first_append_row_is_publicly_visible_at_explicit_cap_60(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "60")
    sessions = _first_append_sessions(61)
    reconciled = _patch_first_append(monkeypatch, sessions)

    models._apply_sidebar_state_db_overrides(sessions)

    row = sessions[-1]
    assert _sidecar_row_is_public(row)
    assert row["visible_message_count"] == 1
    assert reconciled == ["CONTINUED"]


def test_first_append_row_survives_repeated_polls(monkeypatch):
    """Poll twice: the second build sees the same resolved values (no reset, no drift)."""
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    sessions = _first_append_sessions(61)
    _patch_first_append(monkeypatch, sessions)

    models._apply_sidebar_state_db_overrides(sessions)
    first = dict(sessions[-1])
    models._apply_sidebar_state_db_overrides(sessions)
    second = dict(sessions[-1])

    assert _sidecar_row_is_public(second)
    assert second["message_count"] == first["message_count"] == 1
    assert second["visible_message_count"] == first["visible_message_count"] == 1


def test_first_append_row_matches_cap_disabled_control(monkeypatch):
    """The cap changes cost, not the public membership of a messageful row."""
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "60")
    capped_sessions = _first_append_sessions(61)
    _patch_first_append(monkeypatch, capped_sessions)
    models._apply_sidebar_state_db_overrides(capped_sessions)

    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "0")
    control_sessions = _first_append_sessions(61)
    _patch_first_append(monkeypatch, control_sessions)
    models._apply_sidebar_state_db_overrides(control_sessions)

    capped = capped_sessions[-1]
    control = control_sessions[-1]
    for key in ("message_count", "actual_message_count", "last_message_at"):
        assert capped[key] == control[key], (
            f"{key} differs between the capped build and the cap-disabled control"
        )
    assert _sidecar_row_is_public(capped) is True
    assert _sidecar_row_is_public(control) is True


# --- guards that must survive the split ------------------------------------------


def test_quiet_delegation_wakeup_still_credits_no_visible_row(monkeypatch):
    """A capped-out pending hidden wakeup must not be resurrected as a visible row."""
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "1")
    filler = _grown_sessions(1)[0]
    filler["message_count"] = 3
    filler["visible_message_count"] = 3
    filler["actual_message_count"] = 3
    wakeup = {
        "session_id": "WAKEUP",
        "message_count": 0,
        "visible_message_count": 0,
        "actual_message_count": 0,
        "last_message_at": 1.0,
        "updated_at": 1.0,
        "pending_user_source": "delegation_wakeup",
    }
    sessions = [filler, wakeup]
    metadata = {
        "s0": {
            "_state_db_source": "webui",
            "_state_db_message_count": 3,
            "_state_db_last_message_at": 10_000.0,
        },
        "WAKEUP": {
            "_state_db_source": "subagent",
            "_state_db_message_count": 1,
            "_state_db_last_message_at": 20_000.0,
        },
    }
    reconciled: list[str] = []

    def _fake_reconciled(session_id, **_bounds):
        reconciled.append(str(session_id))
        return None

    monkeypatch.setattr(
        models,
        "_reconciled_visible_count_for_sidebar",
        _fake_reconciled,
    )
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: dict(metadata),
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")

    models._apply_sidebar_state_db_overrides(sessions)

    row = sessions[1]
    assert "WAKEUP" in reconciled, "every grown row walks, hidden or not"
    assert row["visible_message_count"] == 0, (
        "a pending delegation_wakeup user row is hidden provenance: it must not "
        "raise the visible count, and the fallback keeps its discount"
    )
    assert row["message_count"] == 1, "the raw count is still authoritative"


def test_source_correction_still_applies_beyond_cap(monkeypatch):
    """Source classification is never capped: a capped row keeps its webui source flags."""
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "1")
    sessions = _grown_sessions(4)
    for row in sessions:
        row["is_cli_session"] = True
        row["session_source"] = "cli"
    metadata = _grown_metadata(4)
    for entry in metadata.values():
        entry["_state_db_source_tag"] = "webui"
        entry["_state_db_raw_source"] = "webui"
        entry["_state_db_session_source"] = "webui"
        entry["_state_db_source_label"] = "WebUI"
    monkeypatch.setattr(
        models, "_reconciled_visible_count_for_sidebar", lambda sid, **_bounds: None
    )
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: dict(metadata),
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")

    models._apply_sidebar_state_db_overrides(sessions)

    for row in sessions:
        assert row["is_cli_session"] is False, (
            "source classification is uncapped: a capped row must still be "
            "reclassified from state.db"
        )
        assert row["session_source"] == "webui"
        assert row["message_count"] == 5, (
            "a webui capped row still gets the cheap count repair"
        )


def test_foreign_source_rows_keep_their_own_flags(monkeypatch):
    """A foreign (cli) state.db source is never overlaid — count overlay is webui/subagent only."""
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "1")
    sessions = _grown_sessions(3)
    metadata = _grown_metadata(3)
    metadata["s2"]["_state_db_source"] = "cli"
    monkeypatch.setattr(
        models, "_reconciled_visible_count_for_sidebar", lambda sid, **_bounds: None
    )
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: dict(metadata),
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")

    models._apply_sidebar_state_db_overrides(sessions)

    assert "session_source" not in sessions[2], (
        "a foreign (cli) source row is left untouched by the overlay"
    )
    assert sessions[2]["message_count"] == 1, (
        "foreign CLI rows keep their existing count flags"
    )


def test_actual_count_never_regresses_through_the_fallback(monkeypatch):
    """actual_message_count is a max, so the fallback can only raise it."""
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "1")
    sessions = _grown_sessions(3)
    sessions[2]["actual_message_count"] = 9
    monkeypatch.setattr(
        models, "_reconciled_visible_count_for_sidebar", lambda sid, **_bounds: None
    )
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: _grown_metadata(3),
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")

    models._apply_sidebar_state_db_overrides(sessions)

    assert sessions[2]["actual_message_count"] == 9
    assert sessions[2]["message_count"] == 5


def test_visible_count_never_exceeds_raw_count(monkeypatch):
    """The fallback clamps to state_count, so visible <= raw always."""
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "1")
    sessions = _grown_sessions(3)
    for row in sessions:
        row["visible_message_count"] = 2
    metadata = _grown_metadata(3)
    for entry in metadata.values():
        entry["_state_db_message_count"] = 4
    monkeypatch.setattr(
        models, "_reconciled_visible_count_for_sidebar", lambda sid, **_bounds: None
    )
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: dict(metadata),
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")

    models._apply_sidebar_state_db_overrides(sessions)

    for row in sessions:
        assert row["visible_message_count"] <= row["message_count"]


def test_none_and_non_positive_cap_are_fully_compatible(monkeypatch):
    """No cap (None) and a non-positive cap both leave the overlay uncapped."""
    for cap in (None, "0", "-5"):
        if cap is None:
            monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
        else:
            monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", cap)
        reconciled = _capture_reconciled_ids(monkeypatch, 10)
        models._apply_sidebar_state_db_overrides(_grown_sessions(10))
        assert len(reconciled) == 10, f"cap={cap} must not bound a 10-row list"


# --- capped-out fallback invariants (#7882 cap follow-up) -------------------------
#
# The reconciled walk is the authority for a row inside the window. Outside it,
# two cases used to over-count or under-count because the fallback only did raw
# delta arithmetic:
#
#   * an eagerly checkpointed hidden wakeup is ALREADY excluded by the sidecar's
#     visible count, so discounting it again undercounts the visible reply;
#   * a truncated sidecar plus append-only state.db rows re-credits the removed
#     suffix, because state.db has no truncation metadata to refute the shrink.
#
# Both are pinned here at the production function boundary (real
# ``_apply_sidebar_state_db_override_metadata``, storage-shaped metadata).


def _capped_filler(rows, *, cap):
    """Fillers that own the pre-filter window, plus the row under test last."""
    sessions = _grown_sessions(rows)
    for index, row in enumerate(sessions):
        row["message_count"] = 3
        row["visible_message_count"] = 3
        row["actual_message_count"] = 3
        row["last_message_at"] = 10_000.0 - index
        row["updated_at"] = 10_000.0 - index
    return sessions


def _capped_metadata(sessions, target, *, count, last):
    metadata = {}
    for row in sessions:
        sid = row["session_id"]
        metadata[sid] = {
            "_state_db_source": "webui",
            "_state_db_message_count": row["message_count"],
            "_state_db_last_message_at": row["last_message_at"],
        }
    metadata[target]["_state_db_message_count"] = count
    metadata[target]["_state_db_last_message_at"] = last
    return metadata


def _apply_with_fallback(monkeypatch, sessions, metadata, *, cap):
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", str(cap))
    reconciled = []

    def _fake_reconciled(session_id, **_bounds):
        reconciled.append(str(session_id))
        return None

    monkeypatch.setattr(models, "_reconciled_visible_count_for_sidebar", _fake_reconciled)
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: dict(metadata),
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    models._apply_sidebar_state_db_overrides(sessions)
    return reconciled


def _wakeup_row(**extra):
    row = {
        "session_id": "WAKEUP",
        "message_count": 2,
        "visible_message_count": 2,
        "actual_message_count": 2,
        "last_message_at": 1.0,
        "updated_at": 1.0,
        "pending_user_message": "handoff",
        "pending_user_source": "delegation_wakeup",
        "pending_started_at": 1000.5,
    }
    row.update(extra)
    return row


def test_capped_out_hidden_wakeup_already_in_sidecar_is_not_double_discounted(monkeypatch):
    """Eager checkpoint: the sidecar already holds AND excludes the hidden row.

    The pending hidden row is materialised in the sidecar, so ``current_visible``
    already excludes it. Only the assistant reply since the last save shows up in
    the state.db delta; the hidden row must not be discounted a second time.

    The evidence is the write-side fact the checkpoint persists
    (``_pending_hidden_wakeup_at``), not a transcript: the sidebar overlay pops
    ``messages`` and ``_strip_sidebar_heavy_metadata`` drops it from the index
    snapshot, so a check that needs a transcript key can never fire on a real
    row. The writer -> index -> row ingress itself is covered end to end by
    ``test_issue7882_cap_checkpoint_ingress.py``; this pins the fallback's
    arithmetic.
    """
    sessions = _capped_filler(3, cap=2)
    sessions.append(_wakeup_row(_pending_hidden_wakeup_at=1000.5))
    metadata = _capped_metadata(sessions, "WAKEUP", count=3, last=20_000.0)
    reconciled = _apply_with_fallback(monkeypatch, sessions, metadata, cap=2)

    row = sessions[-1]
    assert reconciled == ["WAKEUP"], "the grown row walks; the fallback's arithmetic still decides"
    assert row["visible_message_count"] == 3, (
        "2 visible in the sidecar + 1 visible reply; the materialised hidden row "
        "must not be discounted a second time"
    )
    assert "messages" not in row, "the overlay must not leave the transcript on the row"


def test_capped_out_hidden_wakeup_absent_from_sidecar_still_discounted(monkeypatch):
    """Deferred case: the hidden row exists only in state.db, so it is still owed."""
    sessions = _capped_filler(3, cap=2)
    sessions.append(_wakeup_row())
    metadata = _capped_metadata(sessions, "WAKEUP", count=3, last=20_000.0)
    _apply_with_fallback(monkeypatch, sessions, metadata, cap=2)

    row = sessions[-1]
    assert row["visible_message_count"] == 2, (
        "an unmaterialised hidden wakeup keeps its visible-count discount"
    )


def test_materialised_fact_from_another_turn_does_not_vouch(monkeypatch):
    """Ownership is per turn: a fact naming a DIFFERENT pending start is not evidence.

    An older checkpointed wakeup that left its row behind must not excuse the
    discount for a NEW pending wakeup, even when the text matches.
    """
    sessions = _capped_filler(3, cap=2)
    sessions.append(
        _wakeup_row(
            pending_started_at=2000.0,
            _pending_hidden_wakeup_at=1000.5,
        )
    )
    metadata = _capped_metadata(sessions, "WAKEUP", count=3, last=20_000.0)
    _apply_with_fallback(monkeypatch, sessions, metadata, cap=2)

    assert sessions[-1]["visible_message_count"] == 2, (
        "a fact bound to another pending turn cannot vouch for this one"
    )


def test_materialised_fact_fails_closed_without_a_pending_turn():
    """Every unknown answers False: the discount is still owed."""
    assert models._pending_hidden_row_materialized(
        {"_pending_hidden_wakeup_at": 1000.5}
    ) is False
    assert models._pending_hidden_row_materialized(
        {"pending_user_source": "delegation_wakeup", "_pending_hidden_wakeup_at": 1000.5}
    ) is False
    assert models._pending_hidden_row_materialized(
        {"pending_user_source": "delegation_wakeup", "pending_started_at": 1000.5}
    ) is False
    assert models._pending_hidden_row_materialized(
        {
            "pending_user_source": "webui",
            "pending_started_at": 1000.5,
            "_pending_hidden_wakeup_at": 1000.5,
        }
    ) is False
    assert models._pending_hidden_row_materialized({}) is False
    assert models._pending_hidden_row_materialized(None) is False


def test_capped_out_truncated_sidecar_does_not_credit_the_removed_suffix(monkeypatch):
    """state.db is append-only; a truncated sidecar still refutes its own suffix."""
    sessions = _capped_filler(3, cap=2)
    sessions.append(
        {
            "session_id": "TRUNC",
            "message_count": 2,
            "visible_message_count": 2,
            "actual_message_count": 4,
            "last_message_at": 5.0,
            "updated_at": 5.0,
            "truncation_boundary": 3.0,
        }
    )
    metadata = _capped_metadata(sessions, "TRUNC", count=4, last=30_000.0)
    reconciled = _apply_with_fallback(monkeypatch, sessions, metadata, cap=2)

    row = sessions[-1]
    assert "TRUNC" in reconciled, (
        "a truncation-bearing row outside the paint window is still re-walked: "
        "its authoritative visible count only exists inside the walk"
    )
    assert row["visible_message_count"] == 2, (
        "the removed suffix must not be re-credited from append-only state.db rows"
    )
    assert row["message_count"] == 4, (
        "raw growth stays state.db-authoritative; only the visible count is refuted"
    )


def test_truncated_sidecar_without_growth_is_unchanged(monkeypatch):
    """A truncation boundary alone must not shrink an already-correct row."""
    sessions = _capped_filler(3, cap=2)
    sessions.append(
        {
            "session_id": "TRUNC2",
            "message_count": 4,
            "visible_message_count": 2,
            "actual_message_count": 4,
            "last_message_at": 5000.0,
            "updated_at": 5000.0,
            "truncation_boundary": 3.0,
        }
    )
    metadata = _capped_metadata(sessions, "TRUNC2", count=4, last=5000.0)
    _apply_with_fallback(monkeypatch, sessions, metadata, cap=2)

    assert sessions[-1]["visible_message_count"] == 2


def test_in_window_row_still_uses_the_reconciled_walk(monkeypatch):
    """Truncation metadata never overrides the reconciled walk inside the window."""
    sessions = _capped_filler(1, cap=2)
    sessions.append(
        {
            "session_id": "INSIDE",
            "message_count": 2,
            "visible_message_count": 2,
            "actual_message_count": 2,
            "last_message_at": 1.0,
            "updated_at": 1.0,
            "truncation_boundary": 3.0,
        }
    )
    metadata = _capped_metadata(sessions, "INSIDE", count=4, last=20_000.0)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "2")
    seen = []

    def _fake_reconciled(session_id, **_bounds):
        seen.append(str(session_id))
        return 3

    monkeypatch.setattr(models, "_reconciled_visible_count_for_sidebar", _fake_reconciled)
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: dict(metadata),
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    models._apply_sidebar_state_db_overrides(sessions)

    assert "INSIDE" in seen, "a row inside the window still pays the authoritative walk"
    assert sessions[-1]["visible_message_count"] == 3


# --- shrink authority rides the metadata prefix ----------------------------------


def test_metadata_prefix_carries_the_truncation_boundary():
    """The capped-out branch only sees the metadata prefix, so it must carry it."""
    import inspect

    src = inspect.getsource(models.Session.compact)
    assert "truncation_boundary" in src, (
        "compact() must emit truncation_boundary or the capped-out fallback "
        "cannot honour a persisted truncation"
    )

# --- the walk is paid once per revision, not once per poll ------------------------


def _memo_walk(monkeypatch, calls):
    """Patch the walk so each call is recorded and the real memo path is used."""

    def _walk(session_id, **bounds):
        calls.append((str(session_id), bounds))
        from api.models import _SIDEBAR_RECONCILE_MEMO

        memo = _SIDEBAR_RECONCILE_MEMO.get()
        if isinstance(memo, dict):
            memo[(str(session_id), int(bounds.get("expected_state_count") or 0))] = 4
        return 4

    monkeypatch.setattr(models, "_reconciled_visible_count_for_sidebar", _walk)


def test_memo_skips_the_walk_when_state_db_has_not_advanced(monkeypatch):
    """Second poll on a settled board: same sidecar, no new state.db row, no walk."""
    calls = []
    sessions = _grown_sessions(2)
    for row in sessions:
        row["visible_message_count"] = 4
    metadata = _grown_metadata(2)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "0")
    _memo_walk(monkeypatch, calls)
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: dict(metadata),
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    monkeypatch.setattr(models.Session, "load_metadata_only", _fake_stub_sidecar)
    monkeypatch.setattr(
        models, "get_state_db_session_messages", lambda sid, **kw: _fake_state_rows()
    )

    models._apply_sidebar_state_db_overrides(sessions)
    first_walks = len(calls)
    models._apply_sidebar_state_db_overrides(sessions)

    assert first_walks == 2, "the first poll reconciles every grown row"
    assert len(calls) == first_walks, (
        "a settled row whose state.db count did not move must not be re-walked"
    )
    assert [row["visible_message_count"] for row in sessions] == [4, 4]


def test_walk_takes_its_own_memo_path_end_to_end(monkeypatch):
    """Poll five times: one transcript walk, then the memo answers every poll."""
    sessions = _grown_sessions(1)
    # s0's sidecar is one row behind state.db, which is what the overlay's
    # growth guard needs before it reads the transcript at all.
    sessions[0]["message_count"] = 4
    sessions[0]["actual_message_count"] = 4
    sessions[0]["last_message_at"] = 1000.0
    sessions[0]["updated_at"] = 1000.0
    sessions[0]["visible_message_count"] = 4
    metadata = {
        "s0": {
            "_state_db_source": "webui",
            "_state_db_message_count": 5,
            "_state_db_active_message_count": 5,
            "_state_db_last_message_at": 2003.0,
        }
    }
    reads = []

    def _counting_state_read(sid, **kwargs):
        reads.append(str(sid))
        # Five rows: the sidecar's four plus the appended reply the aggregate
        # counted. Growth (5 == 5) is the shape this test pins the memo path on;
        # a transcript smaller than its aggregate is covered separately below.
        return _fake_state_rows() + [
            {"role": "assistant", "content": "m4", "timestamp": 1004.0}
        ]

    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "0")
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: dict(metadata),
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    # A real (well, stand-in) sidecar object: the walk reads it before it reads
    # state.db, so a metadata-only stub would answer every poll identically.
    monkeypatch.setattr(models.Session, "load_metadata_only", _fake_stub_sidecar)
    monkeypatch.setattr(models, "get_state_db_session_messages", _counting_state_read)

    for _ in range(5):
        models._apply_sidebar_state_db_overrides(sessions)

    assert sessions[0]["visible_message_count"] == 5, (
        "the merged transcript (4 rows) is reported, clamped by the raw count"
    )
    assert reads == ["s0"], (
        "one transcript read for five polls: after the first walk the memo "
        "answers every poll whose state.db count has not moved"
    )


def _pinned_stub_sidecar(messages):
    """A full-load sidecar stub carrying *messages*."""

    class _Stub:
        _loaded_metadata_only = False
        profile = None
        truncation_watermark = None
        truncation_boundary = None
        pending_user_source = None
        pending_started_at = None
        pending_user_message = None

    _Stub.messages = list(messages)
    return _Stub()


def _patch_archive_scene(monkeypatch, sessions, metadata, state_rows):
    """Patch the overlay for a scene whose aggregate and detailed read disagree."""
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "0")
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: dict(metadata),
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    monkeypatch.setattr(models, "get_state_db_session_messages", state_rows)


def test_archiving_between_the_aggregate_and_the_walk_keeps_the_shrunk_count(monkeypatch):
    """A compaction between the two reads is not a mismatch: report what was read.

    The SQL aggregate sees six active rows; the Agent archives four of them
    before the detailed read, which therefore returns two. Rejecting that
    transcript made this poll report the raw stored counter (six) while the full
    GET reported two — one wrong sidebar count per compaction.
    """
    kept = [
        {"role": "user", "content": "keep0", "timestamp": 1000.0},
        {"role": "assistant", "content": "keep1", "timestamp": 1001.0},
    ]
    sessions = _grown_sessions(1)
    sessions[0]["message_count"] = 2
    sessions[0]["actual_message_count"] = 2
    sessions[0]["visible_message_count"] = 2
    metadata = {
        "s0": {
            "_state_db_source": "webui",
            "_state_db_message_count": 6,
            "_state_db_active_message_count": 6,
            "_state_db_last_message_at": 3003.0,
        }
    }
    _patch_archive_scene(monkeypatch, sessions, metadata, lambda sid, **kw: list(kept))
    monkeypatch.setattr(
        models.Session, "load_metadata_only", lambda sid, **kw: _pinned_stub_sidecar(kept)
    )

    models._apply_sidebar_state_db_overrides(sessions)

    assert sessions[0]["visible_message_count"] == 2, (
        "the shrunken transcript is the newer answer; the raw pre-archive "
        "counter must not be reported for that poll"
    )


def test_compaction_that_archives_and_appends_reports_the_new_total(monkeypatch):
    """Compaction archives four rows and appends a reply inside the same window."""
    sidecar = [
        {"role": "user", "content": "keep0", "timestamp": 1000.0},
        {"role": "assistant", "content": "keep1", "timestamp": 1001.0},
    ]
    fresh = sidecar + [
        {"role": "user", "content": "after-compaction", "timestamp": 3003.0}
    ]
    sessions = _grown_sessions(1)
    sessions[0]["message_count"] = 2
    sessions[0]["actual_message_count"] = 2
    sessions[0]["visible_message_count"] = 2
    metadata = {
        "s0": {
            "_state_db_source": "webui",
            "_state_db_message_count": 6,
            "_state_db_active_message_count": 6,
            "_state_db_last_message_at": 3003.0,
        }
    }
    _patch_archive_scene(monkeypatch, sessions, metadata, lambda sid, **kw: list(fresh))
    monkeypatch.setattr(
        models.Session, "load_metadata_only", lambda sid, **kw: _pinned_stub_sidecar(sidecar)
    )

    models._apply_sidebar_state_db_overrides(sessions)

    assert sessions[0]["visible_message_count"] == 3, (
        "two surviving rows plus the reply appended during the compaction"
    )


def test_walk_refuses_growth_but_answers_a_shrink(monkeypatch):
    """Directly: growth between the reads refuses the transcript, a shrink answers."""
    sidecar = [
        {"role": "user", "content": "keep0", "timestamp": 1000.0},
        {"role": "assistant", "content": "keep1", "timestamp": 1001.0},
    ]
    state_rows = {"rows": list(sidecar)}
    monkeypatch.setattr(
        models.Session, "load_metadata_only", lambda sid, **kw: _pinned_stub_sidecar(sidecar)
    )
    monkeypatch.setattr(
        models,
        "get_state_db_session_messages",
        lambda sid, **kw: list(state_rows["rows"]),
    )

    # The aggregate saw two rows; three landed before the detailed read.
    state_rows["rows"] = sidecar + [
        {"role": "assistant", "content": f"landed-{i}", "timestamp": 4004.0 + i}
        for i in range(3)
    ]
    assert (
        models._reconciled_visible_count_for_sidebar(
            "s0", expected_state_count=2, expected_last_message_at=1001.0
        )
        is None
    )

    # A shrink (the aggregate counted six, four were archived) is answered.
    state_rows["rows"] = list(sidecar)
    answered = models._reconciled_visible_count_for_sidebar(
        "s0", expected_state_count=6, expected_last_message_at=3003.0
    )
    assert answered is not None
    assert int(getattr(answered, "visible", answered)) == 2


def _fake_stub_sidecar(sid, **kwargs):
    class _Stub:
        _loaded_metadata_only = False
        messages = [
            {"role": "user", "content": f"m{index}", "timestamp": 1000.0 + index}
            for index in range(4)
        ]
        profile = None
        truncation_watermark = None
        truncation_boundary = None
        pending_user_source = None
        pending_started_at = None
        pending_user_message = None

    return _Stub()


def _fake_state_rows():
    return [
        {"role": "user", "content": f"m{index}", "timestamp": 1000.0 + index}
        for index in range(4)
    ]


def test_walk_refuses_a_revision_the_row_did_not_overlay(monkeypatch):
    """A transcript that moved since the SQL read cannot vouch for the row."""
    calls = []
    sessions = _grown_sessions(1)
    sessions[0]["visible_message_count"] = 1
    metadata = {
        "s0": {
            "_state_db_source": "webui",
            "_state_db_message_count": 5,
            "_state_db_active_message_count": 5,
            "_state_db_last_message_at": 2000.0,
        }
    }
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "0")
    _memo_walk(monkeypatch, calls)
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: dict(metadata),
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    monkeypatch.setattr(models.Session, "load_metadata_only", _fake_stub_sidecar)
    # The detailed read now answers five rows, the sixth landed in between.
    monkeypatch.setattr(
        models,
        "get_state_db_session_messages",
        lambda sid, **kw: [
            {"role": "user", "content": f"m{index}", "timestamp": 1000.0 + index}
            for index in range(5)
        ],
    )

    models._apply_sidebar_state_db_overrides(sessions)

    assert len(calls) == 1, "the row is inside the window, so it is walked once"
    assert calls[0][1]["expected_state_count"] == 5, (
        "the walk receives the revision the overlay aggregated"
    )
    # And a transcript that answers a different revision is refused: no memo
    # entry is written for it, so the next poll walks again instead of trusting
    # a count derived from rows the row never had.
    from api.models import _SIDEBAR_RECONCILE_MEMO

    assert _SIDEBAR_RECONCILE_MEMO.get() in (None, {})


def test_reconcile_cap_leaves_the_latest_timestamp_tier_alone(monkeypatch):
    """A row past the reconcile window still sees a newer latest-message time.

    Only a row whose state.db count actually grew takes the newer timestamp:
    an equal-count replay re-stamp is not activity (#8368 re-gate, see the
    ``same two messages re-stamped`` finding), so ``s2`` keeps the sidecar's
    own time while the grown ``s1`` rises without paying the merge.
    """
    sessions = _grown_sessions(3)
    for index, row in enumerate(sessions):
        row["visible_message_count"] = 1
        row["last_message_at"] = 100.0
        row["updated_at"] = 100.0
    metadata = {
        row["session_id"]: {
            "_state_db_source": "webui",
            "_state_db_message_count": 1,
            "_state_db_last_message_at": 900.0 + index,
        }
        for index, row in enumerate(sessions)
    }
    metadata["s0"]["_state_db_message_count"] = 3
    metadata["s0"]["_state_db_last_message_at"] = 1100.0
    metadata["s1"]["_state_db_message_count"] = 2
    metadata["s1"]["_state_db_last_message_at"] = 1200.0
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_RECONCILE_TOP_N", "1")
    reconciled = _capture_reconciled_ids(monkeypatch, 3)
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: dict(metadata),
    )

    models._apply_sidebar_state_db_overrides(sessions)

    assert reconciled == ["s0", "s1"], (
        "every grown row walks; the equal-count replay does not"
    )
    assert sessions[1]["last_message_at"] == 1200.0, (
        "a grown row past the window keeps the newer state.db activity time"
    )
    assert sessions[1]["message_count"] == 2, "and the grown raw count"
    assert sessions[2]["last_message_at"] == 100.0, (
        "an equal-count re-stamp is a replay, not new activity: keep the sidecar time"
    )



# --- the revision check compares one row domain, not two ---------------------------
#
# #7882 re-gate: the SQL aggregate that feeds the overlay counted EVERY state.db row
# while the detailed reader the walk uses excludes ``active = 0`` archive rows. A
# static store with two active rows and four archives therefore answered 2 from the
# reader and 6 from the aggregate, the overlay read that as a concurrent append,
# refused the walk, and the raw-growth fallback reported six visible messages on
# every poll. Archived rows are pre-compaction history: they must never become
# visible growth.


def _archived_state_db(tmp_path):
    """Two active rows plus four ``active = 0`` archives, in one real state.db."""
    import sqlite3

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, session_source TEXT, "
        "title TEXT, message_count INTEGER)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
        "content TEXT, timestamp REAL, active INTEGER)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, message_count) "
        "VALUES ('s0', 'webui', 'webui', 'Archived', 6)"
    )
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp, active) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("s0", "user", "q1", 1000.0, 1),
            ("s0", "assistant", "a1", 1001.0, 1),
            ("s0", "user", "old1", 900.0, 0),
            ("s0", "assistant", "old2", 901.0, 0),
            ("s0", "user", "old3", 902.0, 0),
            ("s0", "assistant", "old4", 903.0, 0),
        ],
    )
    conn.commit()
    conn.close()
    return db


def _active_sidecar_rows():
    return [
        {"role": "user", "content": "q1", "timestamp": 1000.0},
        {"role": "assistant", "content": "a1", "timestamp": 1001.0},
    ]


def test_archive_rows_never_become_visible_growth(monkeypatch, tmp_path):
    """Three public payload builds on an unchanged store all report two, not six."""
    db = _archived_state_db(tmp_path)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_RECONCILE_TOP_N", raising=False)

    def _sidecar(sid, **_kwargs):
        class _Stub:
            _loaded_metadata_only = False
            messages = _active_sidecar_rows()
            profile = None
            truncation_watermark = None
            truncation_boundary = None
            pending_user_source = None
            pending_started_at = None
            pending_user_message = None

        return _Stub()

    monkeypatch.setattr(models.Session, "load_metadata_only", _sidecar)

    # The overlay's own read keeps both counts: the raw one (6, for the growth
    # guard and the displayed message_count) and the active-domain one (2, which
    # is the domain the detailed reader answers in).
    overrides = models._read_state_db_sidebar_overrides(db, {"s0"}, {"s0"})
    assert overrides["s0"]["_state_db_message_count"] == 6
    assert overrides["s0"]["_state_db_active_message_count"] == 2

    for build in range(3):
        sessions = [
            {
                "session_id": "s0",
                "message_count": 2,
                "visible_message_count": 2,
                "actual_message_count": 2,
                "last_message_at": 1000.0,
                "updated_at": 1000.0,
            }
        ]
        models._apply_sidebar_state_db_overrides(sessions)
        assert sessions[0]["visible_message_count"] == 2, (
            f"build {build}: archive rows must not be credited as visible growth"
        )
        assert sessions[0]["message_count"] == 6, (
            "the raw count still feeds the growth guard and the displayed total"
        )


def test_genuine_concurrent_append_still_refuses_the_walk(monkeypatch, tmp_path):
    """A row that landed between the SQL read and the walk still fails the check."""
    db = _archived_state_db(tmp_path)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_RECONCILE_TOP_N", raising=False)

    def _sidecar(sid, **_kwargs):
        class _Stub:
            _loaded_metadata_only = False
            messages = _active_sidecar_rows()
            profile = None
            truncation_watermark = None
            truncation_boundary = None
            pending_user_source = None
            pending_started_at = None
            pending_user_message = None

        return _Stub()

    monkeypatch.setattr(models.Session, "load_metadata_only", _sidecar)

    # The aggregate saw two active rows; the detailed reader answers three, i.e.
    # an active append landed in between. The walk must be refused.
    real_messages = models.get_state_db_session_messages

    def _grown_reader(sid, **kwargs):
        return list(real_messages(sid, **kwargs)) + [
            {"role": "user", "content": "late", "timestamp": 1002.0}
        ]

    monkeypatch.setattr(models, "get_state_db_session_messages", _grown_reader)

    sessions = [
        {
            "session_id": "s0",
            "message_count": 2,
            "visible_message_count": 2,
            "actual_message_count": 2,
            "last_message_at": 1000.0,
            "updated_at": 1000.0,
        }
    ]
    models._apply_sidebar_state_db_overrides(sessions)

    from api.models import _SIDEBAR_RECONCILE_MEMO

    assert _SIDEBAR_RECONCILE_MEMO.get() in (None, {}), (
        "a transcript that answers a different revision must not be cached as "
        "the row's visible count"
    )


def test_archive_rows_outside_the_count_cap_never_become_visible_growth(
    monkeypatch, tmp_path
):
    """Archived rows must not inflate the count when the row is outside the cap.

    #8368 re-gate (SILENT): the active-domain aggregate is only computed for the
    top-N rows, so a pinned row outside the cap has no
    ``_state_db_active_message_count``. Falling back to the raw stored counter
    (six, two active plus four archived) handed the revision check the wrong row
    domain, refused a settled transcript, and the raw-growth fallback reported
    six visible messages where master and the full GET report two.
    """
    db = _archived_state_db(tmp_path)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)

    def _sidecar(sid, **_kwargs):
        class _Stub:
            _loaded_metadata_only = False
            messages = _active_sidecar_rows()
            profile = None
            truncation_watermark = None
            truncation_boundary = None
            pending_user_source = None
            pending_started_at = None
            pending_user_message = None

        return _Stub()

    monkeypatch.setattr(models.Session, "load_metadata_only", _sidecar)

    # 300 pinned fillers own the whole count tier: ``s0`` is outside it, so its
    # active-domain aggregate (and its last-message timestamp) is never read.
    fillers = [
        {
            "session_id": f"f{index}",
            "title": f"Filler {index}",
            "message_count": 3,
            "visible_message_count": 3,
            "actual_message_count": 3,
            "last_message_at": 10_000.0 - index,
            "updated_at": 10_000.0 - index,
        }
        for index in range(300)
    ]
    target = {
        "session_id": "s0",
        "title": "Archived",
        "message_count": 2,
        "visible_message_count": 2,
        "actual_message_count": 2,
        "last_message_at": 1000.0,
        "updated_at": 1000.0,
    }
    sessions = fillers + [target]

    models._apply_sidebar_state_db_overrides(sessions)

    assert target["visible_message_count"] == 2, (
        "archived rows must not become visible growth outside the count cap"
    )
    assert target["message_count"] == 6, (
        "the raw count still feeds the growth guard and the displayed total"
    )
    assert sessions[0]["visible_message_count"] == 3


# --- the timestamp guard decides whether state.db rows are growth at all -----------
#
# #8368 re-gate must-fix: dropping the ``state_last > current_last`` condition
# credited state.db rows the sidecar had already answered. state.db is
# append-only, so a conversation truncated in the WebUI keeps its old rows
# there, and a replayed transcript can carry a restamped timestamp without a
# single new message. Neither is activity in the sidebar.


def test_truncated_conversation_does_not_resurrect_state_db_rows(monkeypatch):
    """A conversation truncated to zero must not be refilled from stale rows."""
    sessions = [
        {
            "session_id": "TRUNC",
            "title": "named chat",
            "message_count": 0,
            "visible_message_count": 0,
            "actual_message_count": 0,
            "last_message_at": 2000.0,
            "updated_at": 2000.0,
        }
    ]
    metadata = {
        "TRUNC": {
            "_state_db_source": "webui",
            "_state_db_message_count": 2,
            "_state_db_last_message_at": 1500.0,
        }
    }
    walked = []

    def _fake_walk(session_id, **_bounds):
        walked.append(str(session_id))
        return 2

    monkeypatch.setattr(models, "_reconciled_visible_count_for_sidebar", _fake_walk)
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: dict(metadata),
    )

    models._apply_sidebar_state_db_overrides(sessions)

    row = sessions[0]
    assert row["message_count"] == 0, (
        "an older state.db row set is not growth: the truncation stands"
    )
    assert row["visible_message_count"] == 0
    assert row["last_message_at"] == 2000.0, (
        "and the stale rows must not drag the row back down the list"
    )
    assert walked == [], "no walk for growth the sidecar already refuted"


def test_replayed_equal_count_restamp_is_not_sidebar_activity(monkeypatch):
    """Equal counts with a newer state.db time is a replay, not new activity."""
    sessions = [
        {
            "session_id": "REPLAY",
            "message_count": 2,
            "visible_message_count": 2,
            "actual_message_count": 2,
            "last_message_at": 1001.0,
            "updated_at": 1001.0,
        }
    ]
    metadata = {
        "REPLAY": {
            "_state_db_source": "webui",
            "_state_db_message_count": 2,
            "_state_db_last_message_at": 2001.0,
        }
    }
    walked = []

    def _fake_walk(session_id, **_bounds):
        walked.append(str(session_id))
        return 2

    monkeypatch.setattr(models, "_reconciled_visible_count_for_sidebar", _fake_walk)
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: dict(metadata),
    )

    models._apply_sidebar_state_db_overrides(sessions)

    row = sessions[0]
    assert row["last_message_at"] == 1001.0, (
        "a restamped replay must not jump an old chat above newer ones"
    )
    assert row["message_count"] == 2
    assert walked == [], "nothing grew, so nothing walks"
