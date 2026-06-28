"""Regression coverage for session sidebar discoverability invariants."""


def test_messageful_hidden_snapshot_is_preserved_when_no_visible_representative():
    from api.models import _preserve_messageful_sidebar_discoverability

    hidden_snapshot = {
        "session_id": "root_snapshot",
        "title": "Long conversation snapshot",
        "message_count": 42,
        "pre_compression_snapshot": True,
    }

    result = _preserve_messageful_sidebar_discoverability(
        candidates=[hidden_snapshot],
        visible=[],
    )

    assert [row["session_id"] for row in result] == ["root_snapshot"]
    assert result[0]["discoverability_warning"] == "rescued_messageful_hidden_session"


def test_messageful_hidden_snapshot_stays_hidden_when_continuation_is_visible():
    from api.models import _preserve_messageful_sidebar_discoverability

    hidden_snapshot = {
        "session_id": "root_snapshot",
        "title": "Archived snapshot",
        "message_count": 42,
        "pre_compression_snapshot": True,
    }
    visible_tip = {
        "session_id": "tip_session",
        "parent_session_id": "root_snapshot",
        "title": "Visible continuation",
        "message_count": 50,
    }

    result = _preserve_messageful_sidebar_discoverability(
        candidates=[hidden_snapshot, visible_tip],
        visible=[visible_tip],
    )

    assert [row["session_id"] for row in result] == ["tip_session"]


def test_intentional_background_sessions_are_not_rescued_into_sidebar():
    from api.models import _preserve_messageful_sidebar_discoverability

    cron_row = {
        "session_id": "cron_digest_001",
        "title": "Digest",
        "source_tag": "cron",
        "message_count": 12,
    }

    result = _preserve_messageful_sidebar_discoverability(
        candidates=[cron_row],
        visible=[],
    )

    assert result == []


def _lineage_row(session_id, *, root="lineage", count=1, ts=1.0, snapshot=False, source="cli"):
    return {
        "session_id": session_id,
        "title": session_id,
        "source_tag": source,
        "session_source": source,
        "_lineage_root_id": root,
        "message_count": count,
        "user_message_count": 1 if count else 0,
        "last_message_at": ts,
        "updated_at": ts,
        "pre_compression_snapshot": snapshot,
    }


def test_fuller_snapshot_does_not_hide_multirow_imported_cli_lineage():
    """Multiple messageful imported/CLI rows in one lineage are user-facing segments."""
    from api.models import _prefer_fuller_snapshots_for_sidebar

    rows = [
        _lineage_row("imported-a", count=3, ts=100.0),
        _lineage_row("imported-b", count=4, ts=200.0),
        _lineage_row("snapshot", count=20, ts=300.0, snapshot=True),
    ]

    result = _prefer_fuller_snapshots_for_sidebar(rows)

    assert [row["session_id"] for row in result] == [
        "imported-a",
        "imported-b",
        "snapshot",
    ]
    snapshot = next(row for row in result if row["session_id"] == "snapshot")
    assert snapshot["_show_pre_compression_snapshot"] is True


def test_fuller_snapshot_still_replaces_single_inactive_continuation():
    """The existing single-continuation cleanup stays intact."""
    from api.models import _prefer_fuller_snapshots_for_sidebar

    rows = [
        _lineage_row("continuation", count=3, ts=100.0, source="webui"),
        _lineage_row("snapshot", count=20, ts=300.0, snapshot=True, source="webui"),
    ]

    result = _prefer_fuller_snapshots_for_sidebar(rows)

    assert [row["session_id"] for row in result] == ["snapshot"]
    assert result[0]["_show_pre_compression_snapshot"] is True


def test_delete_conversation_targets_entire_compression_lineage():
    """Delete Conversation should remove every compression segment, not only the visible tip."""
    from api.models import _session_delete_target_ids_from_rows

    rows = [
        {"session_id": "root", "pre_compression_snapshot": True, "message_count": 10},
        {
            "session_id": "middle",
            "parent_session_id": "root",
            "pre_compression_snapshot": True,
            "message_count": 20,
        },
        {"session_id": "tip", "parent_session_id": "middle", "message_count": 5},
        {"session_id": "fork", "parent_session_id": "middle", "session_source": "fork", "message_count": 1},
        {"session_id": "unrelated", "message_count": 1},
    ]

    assert _session_delete_target_ids_from_rows("tip", rows) == ["root", "middle", "tip"]
    assert _session_delete_target_ids_from_rows("middle", rows) == ["root", "middle", "tip"]


def test_delete_segment_scope_keeps_legacy_single_session_delete():
    from api.models import _session_delete_target_ids_from_rows

    rows = [
        {"session_id": "root", "pre_compression_snapshot": True, "message_count": 10},
        {"session_id": "tip", "parent_session_id": "root", "message_count": 5},
    ]

    assert _session_delete_target_ids_from_rows("tip", rows, scope="segment") == ["tip"]
