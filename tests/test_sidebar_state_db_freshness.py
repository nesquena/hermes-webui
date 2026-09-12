"""Regression tests: sidebar rows for desktop/CLI-owned sessions must track
live agent state.db activity, not the frozen WebUI sidecar/index copy.

Desktop and CLI sessions write to the agent ``state.db`` (via hermes-agent),
NOT to the WebUI sidecar files. The WebUI sidebar list is served from its own
session registry (``webui/sessions/_index.json`` + sidecars), so rows for
external sessions freeze at import time: stale timestamps, message counts, and
sort position. The gateway watcher already reads fresh state.db every poll —
the overlay must apply that freshness to every sidebar response.

The freshness overlay in ``_session_list_cache_overlay_runtime_rows``:
- bumps ``updated_at`` / ``last_message_at`` when state.db last_activity is newer
- bumps ``message_count`` when state.db has more messages
- refreshes ``title`` from state.db
- is monotonic (never downgrades a newer sidecar value)
- fails open (any DB error leaves rows untouched)
"""

import pytest


def _fresh_row(sid="20260823_092817_7259ce", **overrides):
    row = {
        "session_id": sid,
        "title": "Fresh title from state.db",
        "message_count": 999,
        "updated_at": 2000.0,  # newer than any stale sidecar value
        # NOTE: the watcher projection carries updated_at = state.db
        # last_activity but deliberately NO last_message_at.
    }
    row.update(overrides)
    return row


def _stale_row(sid="20260823_092817_7259ce", **overrides):
    row = {
        "session_id": sid,
        "title": "Stale imported title",
        "message_count": 444,
        "updated_at": 1000.0,
        "last_message_at": 900.0,
        "source_tag": "desktop",
        "is_cli_session": False,
    }
    row.update(overrides)
    return row


def _patch_fresh_db(monkeypatch, rows):
    import api.gateway_watcher as gw
    import api.route_session_list_cache as slc

    monkeypatch.setattr(gw, "_get_agent_sessions_from_db", lambda: rows)
    monkeypatch.setattr(slc, "_session_list_cache_active_stream_ids", lambda: set())
    return slc


def test_overlay_freshens_stale_index_row_from_state_db(monkeypatch):
    import api.route_session_list_cache as slc

    _patch_fresh_db(monkeypatch, [_fresh_row()])
    out = slc._session_list_cache_overlay_runtime_rows([_stale_row()])
    assert len(out) == 1
    row = out[0]
    # updated_at follows state.db last_activity.
    assert row["updated_at"] == 2000.0
    # last_message_at propagates state.db activity when the fresh projection
    # has no real last_message_at, so the row both displays AND sorts live.
    assert row["last_message_at"] == 2000.0
    # message_count follows state.db.
    assert row["message_count"] == 999
    # title follows state.db.
    assert row["title"] == "Fresh title from state.db"


def test_overlay_uses_real_last_message_at_when_present(monkeypatch):
    import api.route_session_list_cache as slc

    # If the projection DOES carry a last_message_at, it wins over the
    # updated_at propagation (it is the more precise signal).
    fresh = _fresh_row(last_message_at=2100.0)
    _patch_fresh_db(monkeypatch, [fresh])
    out = slc._session_list_cache_overlay_runtime_rows([_stale_row()])
    assert out[0]["last_message_at"] == 2100.0
    assert out[0]["updated_at"] == 2000.0


def test_overlay_never_downgrades_newer_sidecar_values(monkeypatch):
    import api.route_session_list_cache as slc

    # Sidecar is NEWER than state.db (e.g. WebUI-native session written
    # through the WebUI). The overlay must be monotonic: no downgrade.
    fresh = _fresh_row(updated_at=500.0, message_count=10, title="Older db title")
    _patch_fresh_db(monkeypatch, [fresh])
    stale = _stale_row(
        updated_at=1000.0,
        last_message_at=1200.0,
        message_count=444,
        title="Stale imported title",
    )
    out = slc._session_list_cache_overlay_runtime_rows([stale])
    row = out[0]
    assert row["updated_at"] == 1000.0
    assert row["last_message_at"] == 1200.0
    assert row["message_count"] == 444
    assert row["title"] == "Stale imported title"


def test_overlay_ignores_sessions_not_in_state_db(monkeypatch):
    import api.route_session_list_cache as slc

    _patch_fresh_db(monkeypatch, [_fresh_row(sid="other-session")])
    row = _stale_row(sid="unknown-session", message_count=3, updated_at=100.0)
    out = slc._session_list_cache_overlay_runtime_rows([row])
    assert out[0]["message_count"] == 3
    assert out[0]["updated_at"] == 100.0


def test_overlay_fails_open_when_state_db_unavailable(monkeypatch):
    import api.gateway_watcher as gw
    import api.route_session_list_cache as slc

    def _boom():
        raise RuntimeError("state.db locked")

    monkeypatch.setattr(gw, "_get_agent_sessions_from_db", _boom)
    monkeypatch.setattr(slc, "_session_list_cache_active_stream_ids", lambda: set())
    row = _stale_row()
    out = slc._session_list_cache_overlay_runtime_rows([row])
    # Fail-open: the row passes through untouched.
    assert out[0]["message_count"] == 444
    assert out[0]["updated_at"] == 1000.0
    assert out[0]["last_message_at"] == 900.0
