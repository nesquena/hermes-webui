from pathlib import Path

import pytest

from api.session_sidebar_index import (
    ARCHIVE_AFTER_DAY_CHOICES,
    DEFAULT_ARCHIVE_AFTER_DAYS,
    DEFAULT_ARCHIVE_LIMIT,
    SECONDS_PER_DAY,
    VALID_WORKSPACE_GROUPS,
    build_archive_page,
    build_session_archive_page,
    build_session_sidebar_index,
    normalize_archive_after_days,
    normalize_workspace_group,
    session_activity_ts,
    session_is_current,
    workspace_key_for,
)


def _row(session_id, *, title=None, workspace=None, workspace_group=None, age_days=0, **extra):
    now = 1_700_000_000.0
    row = {
        "session_id": session_id,
        "title": title or session_id,
        "created_at": now - (age_days * SECONDS_PER_DAY) - 10,
        "updated_at": now - (age_days * SECONDS_PER_DAY),
        "last_message_at": now - (age_days * SECONDS_PER_DAY),
        "workspace": workspace,
        "workspace_group": workspace_group,
        "profile": extra.pop("profile", "default"),
        "message_count": extra.pop("message_count", 1),
    }
    row.update(extra)
    return row


def _group(index, group_id):
    return next(group for group in index["groups"] if group["group_id"] == group_id)


def test_public_contract_exports_are_available():
    assert DEFAULT_ARCHIVE_AFTER_DAYS == 7
    assert DEFAULT_ARCHIVE_LIMIT == 30
    assert ARCHIVE_AFTER_DAY_CHOICES == (7, 14, 30, 90)
    assert VALID_WORKSPACE_GROUPS == {"workspace", "chats"}
    assert callable(workspace_key_for)
    assert callable(session_is_current)
    assert callable(build_session_archive_page)


def test_normalize_archive_after_days_defaults_supported_and_rejects_bad_values():
    assert normalize_archive_after_days(None) == 7
    assert normalize_archive_after_days("14") == 14
    assert normalize_archive_after_days(30) == 30

    for value in ("", "abc", 0, -7, 8, 14.5, object()):
        assert normalize_archive_after_days(value) == 7


def test_normalize_workspace_group_infers_workspace_or_chats():
    assert normalize_workspace_group("chats", workspace="/tmp/runtime") == "chats"
    assert normalize_workspace_group(" Chats ", workspace="/tmp/runtime") == "chats"
    assert normalize_workspace_group("WORKSPACE", workspace="/tmp/runtime") == "workspace"
    assert normalize_workspace_group("workspace", workspace="/tmp/runtime") == "workspace"
    assert normalize_workspace_group(None, workspace="/tmp/runtime") == "workspace"
    assert normalize_workspace_group(None, workspace=None) == "chats"


def test_session_activity_ts_prefers_last_message_then_updated_then_created():
    assert session_activity_ts({"last_message_at": 30, "updated_at": 20, "created_at": 10}) == 30
    assert session_activity_ts({"last_message_at": None, "updated_at": 20, "created_at": 10}) == 20
    assert session_activity_ts({"last_message_at": "", "updated_at": None, "created_at": 10}) == 10
    assert session_activity_ts({}) == 0


def test_session_activity_ts_rejects_malformed_and_non_finite_values():
    assert session_activity_ts({"last_message_at": "nan", "updated_at": "inf", "created_at": "-inf"}) == 0
    assert session_activity_ts({"last_message_at": "bad", "updated_at": 20, "created_at": 10}) == 20


def test_build_index_groups_workspaces_across_profiles_and_names_workspace(tmp_path):
    workspace = tmp_path / "runtime"
    workspace.mkdir()
    normalized = str(workspace.resolve())
    rows = [
        _row("work-a", workspace=str(workspace), profile="default"),
        _row("work-b", workspace=str(workspace), profile="other"),
        _row("chat-a", workspace=str(workspace), workspace_group="chats"),
    ]

    index = build_session_sidebar_index(
        rows,
        server_time=1_700_000_000.0,
        server_tz="UTC",
        workspace_names={normalized: "Runtime"},
        session_archive_after_days=7,
    )

    workspace_group = _group(index, f"workspace:{normalized}")
    chats_group = _group(index, "chats")

    assert workspace_key_for(None) is None
    assert workspace_key_for(str(workspace)) == normalized
    assert index["archive_after_days"] == 7
    assert index["manual_archived"] == {"count": 0}
    assert workspace_group["kind"] == "project"
    assert workspace_group["type"] == "workspace"
    assert workspace_group["key"] == f"workspace:{normalized}"
    assert workspace_group["name"] == "Runtime"
    assert workspace_group["label"] == "Runtime"
    assert workspace_group["workspace"] == normalized
    assert [row["session_id"] for row in workspace_group["sessions"]] == ["work-b", "work-a"]
    assert [row["id"] for row in workspace_group["sessions"]] == ["work-b", "work-a"]
    assert {row["profile"] for row in workspace_group["sessions"]} == {"default", "other"}
    assert {
        "id",
        "title",
        "profile",
        "avatar",
        "updated_at",
        "age_seconds",
        "pinned",
        "unread",
        "streaming",
        "pending",
    } <= set(workspace_group["sessions"][0])
    assert workspace_group["archive"] == {
        "count": 0,
        "has_more": False,
        "next_offset": None,
        "cursor": None,
    }
    assert [row["session_id"] for row in chats_group["sessions"]] == ["chat-a"]
    assert chats_group["kind"] == "chats"
    assert chats_group["type"] == "chats"
    assert chats_group["key"] == "chats"
    assert index["server_time"] == 1_700_000_000.0
    assert index["server_tz"] == "UTC"
    assert index["session_archive_after_days"] == 7


def test_old_rows_increment_archive_count_without_current_group_sessions(tmp_path):
    workspace = tmp_path / "runtime"
    workspace.mkdir()
    normalized = str(workspace.resolve())
    rows = [
        _row("current", workspace=str(workspace), age_days=1),
        _row("old", workspace=str(workspace), age_days=10),
    ]

    index = build_session_sidebar_index(
        rows,
        server_time=1_700_000_000.0,
        session_archive_after_days=7,
    )

    group = _group(index, f"workspace:{normalized}")
    assert [row["session_id"] for row in group["sessions"]] == ["current"]
    assert group["current_count"] == 1
    assert group["archive_count"] == 1
    assert group["archive"]["count"] == 1
    assert group["archive"]["has_more"] is True
    assert group["manual_archived_count"] == 0


@pytest.mark.parametrize(
    ("session_id", "extra"),
    [
        ("pinned", {"pinned": True}),
        ("unread", {"unread": True}),
        ("streaming", {"is_streaming": True}),
        ("active-stream-id", {"active_stream_id": "stream-1"}),
        ("streaming-alias", {"streaming": True}),
        ("pending-user-message", {"has_pending_user_message": True}),
        ("pending-user-text", {"pending_user_message": "still working"}),
        ("pending-alias", {"pending": True}),
    ],
)
def test_important_rows_stay_current_even_when_old_without_current_session(session_id, extra):
    rows = [_row(session_id, workspace_group="chats", age_days=30, **extra)]

    index = build_session_sidebar_index(
        rows,
        server_time=1_700_000_000.0,
        session_archive_after_days=7,
    )

    group = _group(index, "chats")
    assert [row["session_id"] for row in group["sessions"]] == [session_id]
    assert session_is_current(rows[0], server_time=1_700_000_000.0, session_archive_after_days=7)
    assert group["current_count"] == 1
    assert group["archive_count"] == 0


def test_current_session_stays_current_even_when_old():
    rows = [_row("current", workspace_group="chats", age_days=30)]

    index = build_session_sidebar_index(
        rows,
        server_time=1_700_000_000.0,
        session_archive_after_days=7,
        current_session_id="current",
    )

    group = _group(index, "chats")
    assert [row["session_id"] for row in group["sessions"]] == ["current"]
    assert group["current_count"] == 1
    assert group["archive_count"] == 0


def test_manual_archived_rows_are_excluded_and_counted_separately():
    rows = [
        _row("manual", workspace_group="chats", archived=True, age_days=30),
        _row("current", workspace_group="chats", age_days=1),
    ]

    index = build_session_sidebar_index(
        rows,
        server_time=1_700_000_000.0,
        session_archive_after_days=7,
    )

    group = _group(index, "chats")
    assert [row["session_id"] for row in group["sessions"]] == ["current"]
    assert group["current_count"] == 1
    assert group["archive_count"] == 0
    assert group["manual_archived_count"] == 1
    assert index["manual_archived"] == {"count": 1}


def test_build_archive_page_is_group_scoped_sorted_and_cursor_paginated(tmp_path):
    workspace = tmp_path / "runtime"
    workspace.mkdir()
    normalized = str(workspace.resolve())
    rows = [
        _row("work-older", workspace=str(workspace), age_days=12),
        _row("work-newer-b", workspace=str(workspace), age_days=10),
        _row("work-newer-a", workspace=str(workspace), age_days=10),
        _row("chat-old", workspace_group="chats", age_days=20),
        _row("manual", workspace=str(workspace), archived=True, age_days=40),
        _row("current", workspace=str(workspace), age_days=1),
    ]

    first = build_session_archive_page(
        rows,
        group_id=f"workspace:{normalized}",
        server_time=1_700_000_000.0,
        session_archive_after_days=7,
        limit=2,
    )
    second = build_archive_page(
        rows,
        group_id=f"workspace:{normalized}",
        server_time=1_700_000_000.0,
        session_archive_after_days=7,
        limit=2,
        cursor=first["next_cursor"],
    )

    assert [row["session_id"] for row in first["sessions"]] == ["work-newer-b", "work-newer-a"]
    assert first["remaining_count"] == 1
    assert first["next_cursor"]
    assert first["archive"]["count"] == 3
    assert first["archive"]["has_more"] is True
    assert first["archive"]["cursor"] == first["next_cursor"]
    assert [row["session_id"] for row in second["sessions"]] == ["work-older"]
    assert second["remaining_count"] == 0
    assert second["next_cursor"] is None
    assert all(row["group_id"] == f"workspace:{normalized}" for row in first["sessions"] + second["sessions"])
    assert all(row["age_archived"] for row in first["sessions"] + second["sessions"])


def test_archive_page_stale_cursor_uses_keyset_boundary(tmp_path):
    workspace = tmp_path / "runtime"
    workspace.mkdir()
    normalized = str(workspace.resolve())
    rows = [
        _row("newest", workspace=str(workspace), age_days=10),
        _row("middle", workspace=str(workspace), age_days=11),
        _row("oldest", workspace=str(workspace), age_days=12),
    ]
    first = build_session_archive_page(
        rows,
        group_id=f"workspace:{normalized}",
        server_time=1_700_000_000.0,
        session_archive_after_days=7,
        limit=1,
    )

    rows_without_cursor_row = [row for row in rows if row["session_id"] != "newest"]
    second = build_session_archive_page(
        rows_without_cursor_row,
        group_id=f"workspace:{normalized}",
        server_time=1_700_000_000.0,
        session_archive_after_days=7,
        limit=1,
        cursor=first["next_cursor"],
    )

    assert [row["session_id"] for row in first["sessions"]] == ["newest"]
    assert [row["session_id"] for row in second["sessions"]] == ["middle"]


@pytest.mark.parametrize("limit", ["bad", 0, -10, None])
def test_archive_page_bad_or_non_positive_limit_defaults(limit):
    rows = [
        _row(f"old-{idx}", workspace_group="chats", age_days=10 + idx)
        for idx in range(DEFAULT_ARCHIVE_LIMIT + 1)
    ]

    page = build_session_archive_page(
        rows,
        group_id="chats",
        server_time=1_700_000_000.0,
        session_archive_after_days=7,
        limit=limit,
    )

    assert len(page["sessions"]) == DEFAULT_ARCHIVE_LIMIT
    assert page["remaining_count"] == 1
    assert page["next_cursor"]
