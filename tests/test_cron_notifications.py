"""Tests for WebUI/Hermex cron notification inbox endpoints."""

from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.request
import urllib.error

from tests._pytest_port import BASE


TEST_HOME = Path(os.environ["HERMES_WEBUI_TEST_STATE_DIR"])


def _notification_file(home: Path = TEST_HOME) -> Path:
    return home / "cron" / "notifications.jsonl"


def _reset(home: Path = TEST_HOME) -> Path:
    path = _notification_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _write_record(record: dict, home: Path = TEST_HOME) -> None:
    path = _notification_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def get(path):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
        ctype = r.headers.get("Content-Type", "")
        if ctype.startswith("text/event-stream"):
            return raw.decode("utf-8"), r.status, ctype
        return json.loads(raw), r.status, ctype


def post(path, body=None):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def test_notifications_endpoint_returns_unread_profile_scoped_records():
    _reset()
    _write_record({
        "id": "notif_old",
        "profile": "default",
        "job_id": "old-job",
        "title": "Old",
        "body": "old body",
        "created_at": "2026-01-01T00:00:00Z",
        "read_at": "2026-01-01T00:01:00Z",
    })
    _write_record({
        "id": "notif_new",
        "profile": "default",
        "job_id": "new-job",
        "title": "New",
        "body": "new body",
        "created_at": "2026-01-02T00:00:00Z",
        "read_at": None,
    })
    path = _notification_file()
    path.write_text(path.read_text(encoding="utf-8") + "malformed-json\n", encoding="utf-8")

    result, status, _ctype = get("/api/notifications?limit=10")

    assert status == 200
    assert [row["id"] for row in result["notifications"]] == ["notif_new", "notif_old"]
    assert result["unread_count"] == 1
    assert result["unread_by_profile"] == {"default": 1}


def test_notifications_equal_legacy_timestamps_use_append_order():
    _reset()
    for notification_id in ("older", "newer"):
        _write_record({
            "id": notification_id,
            "profile": "default",
            "created_at": "2026-01-01T00:00:00Z",
            "read_at": None,
        })

    result, status, _ctype = get("/api/notifications?limit=10")

    assert status == 200
    assert [row["id"] for row in result["notifications"]] == ["newer", "older"]


def test_notifications_unread_only_and_mark_read():
    _reset()
    _write_record({
        "id": "notif_mark",
        "profile": "default",
        "job_id": "mark-job",
        "title": "Mark me",
        "body": "body",
        "created_at": "2026-01-03T00:00:00Z",
        "read_at": None,
    })

    unread, status, _ctype = get("/api/notifications?unread_only=1")
    assert status == 200
    assert [row["id"] for row in unread["notifications"]] == ["notif_mark"]

    marked, status = post("/api/notifications/read", {"id": "notif_mark", "profile": "default"})
    assert status == 200
    assert marked["ok"] is True
    assert marked["notification"]["read_at"]

    unread_after, status, _ctype = get("/api/notifications?unread_only=1")
    assert status == 200
    assert unread_after["notifications"] == []
    assert unread_after["unread_count"] == 0
    path = _notification_file()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.with_suffix(path.suffix + ".lock").stat().st_mode & 0o777 == 0o600


def test_notifications_all_profiles_aggregates_visible_profile_dirs():
    _reset()
    other_home = TEST_HOME / "profiles" / "newsletteros"
    _reset(other_home)
    _write_record({
        "id": "notif_default",
        "profile": "default",
        "job_id": "default-job",
        "title": "Default",
        "body": "default body",
        "created_at": "2026-01-04T00:00:00Z",
        "read_at": None,
    })
    _write_record({
        "id": "notif_newsletter",
        "profile": "newsletteros",
        "job_id": "newsletter-job",
        "title": "Newsletter",
        "body": "newsletter body",
        "created_at": "2026-01-05T00:00:00Z",
        "read_at": None,
    }, home=other_home)

    scoped, status, _ctype = get("/api/notifications?limit=10")
    assert status == 200
    assert [row["id"] for row in scoped["notifications"]] == ["notif_default"]

    aggregate, status, _ctype = get("/api/notifications?limit=10&all_profiles=1")
    assert status == 200
    assert [row["id"] for row in aggregate["notifications"]] == ["notif_newsletter", "notif_default"]
    assert aggregate["unread_by_profile"]["newsletteros"] == 1


def test_notifications_events_once_returns_sse_snapshot():
    _reset()
    _write_record({
        "id": "notif_sse",
        "profile": "default",
        "job_id": "sse-job",
        "title": "SSE",
        "body": "sse body",
        "created_at": "2026-01-06T00:00:00Z",
        "read_at": None,
    })

    raw, status, ctype = get("/api/notifications/events?once=1")

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert "event: snapshot" in raw
    assert "notif_sse" in raw


def test_notifications_reader_bounds_legacy_unbounded_store():
    path = _reset()
    with open(path, "a", encoding="utf-8") as fh:
        for idx in range(2005):
            fh.write(json.dumps({
                "id": f"legacy_{idx}",
                "created_at": f"2026-01-01T00:00:{idx:04d}Z",
                "read_at": None,
            }) + "\n")

    result, status, _ctype = get("/api/notifications?limit=200")

    assert status == 200
    assert result["unread_count"] == 2000
    assert all(row["id"] != "legacy_0" for row in result["notifications"])


def test_mark_read_rejects_foreign_profile_in_isolated_mode(tmp_path, monkeypatch):
    from api import cron_notifications, profiles

    isolated_home = tmp_path / "profiles" / "newsletteros"
    foreign_home = tmp_path / "profiles" / "wolf-of-hermes"
    _reset(isolated_home)
    foreign_path = _reset(foreign_home)
    _write_record(
        {
            "id": "foreign-notification",
            "profile": "wolf-of-hermes",
            "created_at": "2026-01-01T00:00:00Z",
            "read_at": None,
        },
        home=foreign_home,
    )
    before = foreign_path.read_text(encoding="utf-8")

    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: True)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "newsletteros")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: isolated_home)
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda _name: foreign_home,
    )
    monkeypatch.setattr(
        profiles,
        "list_profiles_api",
        lambda: [{"name": "newsletteros"}, {"name": "wolf-of-hermes"}],
    )

    result = cron_notifications.mark_read(
        "foreign-notification", profile="wolf-of-hermes"
    )

    assert result is None
    assert foreign_path.read_text(encoding="utf-8") == before


def test_mark_read_allows_visible_profile_in_multi_profile_mode(tmp_path, monkeypatch):
    from api import cron_notifications, profiles

    default_home = tmp_path
    foreign_home = tmp_path / "profiles" / "wolf-of-hermes"
    _reset(default_home)
    _reset(foreign_home)
    _write_record(
        {
            "id": "visible-notification",
            "profile": "wolf-of-hermes",
            "created_at": "2026-01-01T00:00:00Z",
            "read_at": None,
        },
        home=foreign_home,
    )

    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: default_home)
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: foreign_home if name == "wolf-of-hermes" else default_home,
    )
    monkeypatch.setattr(
        profiles,
        "list_profiles_api",
        lambda: [{"name": "wolf-of-hermes"}],
    )

    result = cron_notifications.mark_read(
        "visible-notification", profile="wolf-of-hermes"
    )

    assert result is not None
    assert result["id"] == "visible-notification"
    assert result["read_at"]
