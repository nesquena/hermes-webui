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
