"""Regression coverage for orphaned imported sidecar pruning.

#3238: WebUI does not sync when CLI sessions are deleted outside WebUI.
#4591: API-server sidecars are read-only and accumulated indefinitely after
their backing state.db row was deleted.

When a CLI/agent session is clicked in the WebUI sidebar it gets a WebUI-owned
sidecar (`webui/sessions/<id>.json` + an `_index.json` row) so it can render and
be reopened. From then on `all_sessions()` returns it independently of the agent
`state.db`. If the user later deletes that session from the CLI / local Hermes
storage, nothing prunes the orphaned sidecar, so the stale row lingers in the
sidebar forever — there is no WebUI delete affordance for CLI rows.

The fix probes `state.db` directly via `agent_session_rows_existing()` (an
exact, uncapped existence check) and prunes the sidecar only when the backing
row is genuinely gone. It must NOT rely on the session's presence in
`get_cli_sessions()`, which caps at `CLI_VISIBLE_SESSION_LIMIT` (20) — an
existing session can fall out of that window and look deleted.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _make_state_db(path: Path, session_ids):
    """Create a minimal agent state.db with a `sessions` table + given ids."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, "
            "started_at REAL, last_activity REAL)"
        )
        for sid in session_ids:
            conn.execute(
                "INSERT INTO sessions (id, source, started_at, last_activity) "
                "VALUES (?, 'cli', 0, 0)",
                (sid,),
            )
        conn.commit()
    finally:
        conn.close()


def test_agent_session_row_exists_true_for_present_row(tmp_path, monkeypatch):
    from api import models

    home = tmp_path / "home"
    home.mkdir()
    _make_state_db(home / "state.db", ["sess-present"])
    monkeypatch.setattr(models, "_active_state_db_path", lambda: home / "state.db")

    assert models.agent_session_row_exists("sess-present") is True


def test_agent_session_row_exists_false_for_deleted_row(tmp_path, monkeypatch):
    """The core fix: a session id that is NOT in state.db is reported gone."""
    from api import models

    home = tmp_path / "home"
    home.mkdir()
    _make_state_db(home / "state.db", ["other-session"])
    monkeypatch.setattr(models, "_active_state_db_path", lambda: home / "state.db")

    assert models.agent_session_row_exists("sess-deleted") is False


def test_agent_session_row_exists_safe_true_when_db_missing(tmp_path, monkeypatch):
    """No agent DB on this instance -> never claim a row is gone (no data loss)."""
    from api import models

    monkeypatch.setattr(
        models, "_active_state_db_path", lambda: tmp_path / "nope" / "state.db"
    )
    assert models.agent_session_row_exists("anything") is True


def test_agent_session_row_exists_empty_id_is_false(tmp_path, monkeypatch):
    from api import models

    home = tmp_path / "home"
    home.mkdir()
    _make_state_db(home / "state.db", ["x"])
    monkeypatch.setattr(models, "_active_state_db_path", lambda: home / "state.db")
    assert models.agent_session_row_exists("") is False
    assert models.agent_session_row_exists(None) is False


def test_agent_session_row_exists_handles_missing_sessions_table(tmp_path, monkeypatch):
    """A state.db without a `sessions` table degrades to safe-True."""
    from api import models

    home = tmp_path / "home"
    home.mkdir()
    db = home / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE unrelated (x TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    assert models.agent_session_row_exists("whatever") is True


def test_agent_session_rows_existing_returns_present_subset(tmp_path, monkeypatch):
    from api import models

    home = tmp_path / "home"
    home.mkdir()
    _make_state_db(home / "state.db", ["sess-a", "sess-b"])
    monkeypatch.setattr(models, "_active_state_db_path", lambda: home / "state.db")

    existing = models.agent_session_rows_existing(
        ["sess-a", "sess-b", "sess-missing", "", None]
    )
    assert existing == frozenset({"sess-a", "sess-b"})


def test_agent_session_rows_existing_safe_when_db_missing(tmp_path, monkeypatch):
    from api import models

    monkeypatch.setattr(
        models, "_active_state_db_path", lambda: tmp_path / "nope" / "state.db"
    )
    wanted = ["orphan-a", "orphan-b"]
    assert models.agent_session_rows_existing(wanted) == frozenset(wanted)


def test_agent_session_rows_existing_batches_over_500_ids(tmp_path, monkeypatch):
    from api import models

    home = tmp_path / "home"
    home.mkdir()
    ids = [f"sess-{i:04d}" for i in range(600)]
    _make_state_db(home / "state.db", ids[:300])
    monkeypatch.setattr(models, "_active_state_db_path", lambda: home / "state.db")

    existing = models.agent_session_rows_existing(ids)
    assert existing == frozenset(ids[:300])


def test_agent_session_rows_existing_normalizes_whitespace_in_probe_ids(tmp_path, monkeypatch):
    from api import models

    home = tmp_path / "home"
    home.mkdir()
    _make_state_db(home / "state.db", ["cli-padded"])
    monkeypatch.setattr(models, "_active_state_db_path", lambda: home / "state.db")

    existing = models.agent_session_rows_existing(["  cli-padded  "])
    assert existing == frozenset({"cli-padded"})


# ── Orphan-prune decision predicate (mirrors the sidebar merge-loop guard) ──


def _is_orphaned_cli_sidecar(row, cli_by_id, exists_fn):
    """Replicates the routes.py merge-loop predicate so the decision logic is
    covered without standing up the full HTTP sidebar endpoint."""
    from api.agent_sessions import is_cli_session_row
    from api.routes import _session_source_is_webui

    sid = row.get("session_id")
    return bool(
        sid
        and is_cli_session_row(row)
        and not _session_source_is_webui(row)
        and sid not in cli_by_id
        and not exists_fn(sid)
    )


def test_orphaned_imported_cli_sidecar_is_pruned():
    """import-sourced CLI row, not in cli_by_id, backing state.db row gone."""
    row = {"session_id": "cli-orphan", "source_tag": "cli", "is_cli_session": True}
    assert _is_orphaned_cli_sidecar(row, {}, exists_fn=lambda sid: False) is True


def test_native_webui_session_with_cli_ancestor_is_never_pruned():
    """Regression guard: a WebUI-native row must survive even if absent from
    cli_by_id and its (ancestor) id isn't in state.db."""
    row = {
        "session_id": "webui-native",
        "source_tag": "webui",
        "session_source": "webui",
        "is_cli_session": False,
    }
    assert _is_orphaned_cli_sidecar(row, {}, exists_fn=lambda sid: False) is False


def test_cli_row_still_backed_in_state_db_is_not_pruned():
    """Falls out of the recent-20 window (absent from cli_by_id) BUT still exists
    in state.db -> must NOT be pruned. This is the cap-window false-positive the
    direct state.db probe defends against."""
    row = {"session_id": "cli-old", "source_tag": "cli", "is_cli_session": True}
    assert _is_orphaned_cli_sidecar(row, {}, exists_fn=lambda sid: True) is False


def test_cli_row_present_in_cli_by_id_is_not_pruned():
    """Still in the live CLI list -> obviously not orphaned."""
    row = {"session_id": "cli-live", "source_tag": "cli", "is_cli_session": True}
    cli_by_id = {"cli-live": {"session_id": "cli-live"}}
    assert _is_orphaned_cli_sidecar(row, cli_by_id, exists_fn=lambda sid: False) is False


def _payload_for_rows(monkeypatch, rows, existing_ids):
    import api.routes as routes

    pruned = []

    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: list(rows))
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: [])
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda _sessions: False,
    )
    monkeypatch.setattr(
        routes,
        "agent_session_rows_existing",
        lambda ids, profile=None: frozenset(existing_ids),
    )
    monkeypatch.setattr(routes, "prune_session_from_index", lambda sid: pruned.append(sid))

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    return payload, pruned


def test_orphaned_api_server_sidecar_is_pruned_from_sidebar_payload(monkeypatch):
    """API-server sidecars take the same exact state.db orphan prune path."""
    row = {
        "session_id": "api-orphan",
        "title": "API Session",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 2,
        "read_only": True,
        "source_tag": "api_server",
        "raw_source": "api_server",
        "session_source": "api",
        "source_label": "API",
        "is_cli_session": False,
    }

    payload, pruned = _payload_for_rows(monkeypatch, [row], existing_ids=[])

    assert [session["session_id"] for session in payload["sessions"]] == []
    assert pruned == ["api-orphan"]


def test_api_server_sidecar_with_backing_state_row_is_retained(monkeypatch):
    """Absence from get_cli_sessions() alone is not enough to prune API rows."""
    row = {
        "session_id": "api-live",
        "title": "API Session",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 2,
        "read_only": True,
        "source_tag": "api_server",
        "raw_source": "api_server",
        "session_source": "api",
        "source_label": "API",
        "is_cli_session": False,
    }

    payload, pruned = _payload_for_rows(monkeypatch, [row], existing_ids=["api-live"])

    assert [session["session_id"] for session in payload["sessions"]] == ["api-live"]
    assert pruned == []


def test_webui_owned_session_with_api_metadata_is_not_pruned(monkeypatch):
    """A native WebUI row must stay safe even if stale metadata mentions API."""
    row = {
        "session_id": "webui-native",
        "title": "Native WebUI",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 2,
        "read_only": False,
        "source_tag": "webui",
        "raw_source": "api_server",
        "session_source": "webui",
        "source_label": "API",
        "is_cli_session": False,
    }

    payload, pruned = _payload_for_rows(monkeypatch, [row], existing_ids=[])

    assert [session["session_id"] for session in payload["sessions"]] == ["webui-native"]
    assert pruned == []
