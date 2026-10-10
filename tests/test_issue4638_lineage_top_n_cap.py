"""Regression tests for #4638 — cap sidebar lineage enrichment to the top-N sessions.

On power users with thousands of sessions, GET /api/sessions spent ~5s in
_enrich_sidebar_lineage_metadata probing state.db for EVERY row's compression
lineage. The sidebar paints pinned-first then newest-first, and the caller passes
an already-sorted list, so enriching only the top-N (default 300) most-recent rows
covers the visible window while bounding wall-clock. The cap is env-configurable
(HERMES_WEBUI_LINEAGE_TOP_N) and fails open.

These tests pin: (1) only the top-N ids are probed when the list exceeds the cap,
(2) the env override is honored, (3) a non-positive / unparseable cap disables the
cap (enrich all), (4) lists at/under the cap probe everything, and (5) fork-source
rows do not expand the capped lineage lookup.
"""
from __future__ import annotations

import api.models as models


def _capture_probed_ids(monkeypatch):
    """Patch read_session_lineage_metadata to record the id set it is asked for."""
    seen = {}

    def _fake_read(db_path, id_set):
        seen["ids"] = set(id_set)
        return {}

    monkeypatch.setattr(models, "read_session_lineage_metadata", _fake_read)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    return seen


def _sessions(n):
    # Caller passes an already pinned-first/newest-first sorted list; index order
    # therefore IS paint priority. id "s0" is the most-recent/visible-most.
    return [{"session_id": f"s{i}"} for i in range(n)]


def test_caps_enrichment_to_top_n_default_300(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.delenv("HERMES_WEBUI_LINEAGE_TOP_N", raising=False)
    models._enrich_sidebar_lineage_metadata(_sessions(1000))
    assert seen["ids"] == {f"s{i}" for i in range(300)}, (
        "Default cap must probe exactly the top-300 (paint-priority) sessions"
    )


def test_env_override_changes_cap(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_LINEAGE_TOP_N", "50")
    models._enrich_sidebar_lineage_metadata(_sessions(1000))
    assert seen["ids"] == {f"s{i}" for i in range(50)}, (
        "HERMES_WEBUI_LINEAGE_TOP_N must bound the probed set"
    )


def test_non_positive_cap_disables_capping(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_LINEAGE_TOP_N", "0")
    models._enrich_sidebar_lineage_metadata(_sessions(500))
    assert len(seen["ids"]) == 500, "cap<=0 must enrich all sessions (cap disabled)"


def test_unparseable_cap_falls_back_to_default(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_LINEAGE_TOP_N", "not-a-number")
    models._enrich_sidebar_lineage_metadata(_sessions(1000))
    assert seen["ids"] == {f"s{i}" for i in range(300)}, (
        "An unparseable cap must fall back to the default 300, not crash"
    )


def test_list_under_cap_probes_everything(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.delenv("HERMES_WEBUI_LINEAGE_TOP_N", raising=False)
    models._enrich_sidebar_lineage_metadata(_sessions(120))
    assert seen["ids"] == {f"s{i}" for i in range(120)}, (
        "A list at/under the cap must enrich every session"
    )


def test_enrichment_failure_is_swallowed(monkeypatch):
    """Lineage enrichment must fail open — a DB error never breaks /api/sessions."""
    def _boom(db_path, id_set):
        raise RuntimeError("db down")

    monkeypatch.setattr(models, "read_session_lineage_metadata", _boom)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    # Must not raise.
    models._enrich_sidebar_lineage_metadata(_sessions(10))


def test_parent_linked_forks_do_not_expand_lineage_cap(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.delenv("HERMES_WEBUI_LINEAGE_TOP_N", raising=False)
    sessions = _sessions(1000)
    sessions[450].update(session_source="fork", parent_session_id="snapshot")
    sessions[600].update(session_source=" Fork ", parent_session_id="other-parent")
    sessions[800].update(session_source="webui", parent_session_id="ordinary-parent")
    sessions[900].update(session_source="fork")
    models._enrich_sidebar_lineage_metadata(sessions)
    assert seen["ids"] == {f"s{i}" for i in range(300)}


def test_old_fork_source_overlay_does_not_expand_lineage_or_message_counts(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_LINEAGE_TOP_N", "1")
    rows = [
        {"session_id": "recent"},
        {"session_id": "old-fork", "session_source": "fork", "parent_session_id": "original",
         "message_count": 2},
        {"session_id": "old-webui", "session_source": "webui"},
    ]
    calls = []

    def source_only(_db_path, ids, *, count_session_ids):
        calls.append((ids, count_session_ids))
        return {"old-fork": {
            "_state_db_source": "webui", "_state_db_session_source": "webui",
            "_state_db_message_count": 1000,
        }}

    monkeypatch.setattr(models, "_read_state_db_sidebar_overrides", source_only)
    models._enrich_sidebar_lineage_metadata(rows)
    assert seen["ids"] == {"recent"}
    assert calls == [({"old-fork"}, set())]
    assert rows[1]["session_source"] == "webui"
    assert rows[1]["parent_session_id"] == "original"
    assert rows[1]["message_count"] == 2
    assert "actual_message_count" not in rows[1]
    assert "_lineage_root_id" not in rows[1]
