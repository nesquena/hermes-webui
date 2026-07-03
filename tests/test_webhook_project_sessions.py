"""Webhook sessions should behave like cron sessions in the project chip layer."""


def _agent_row(session_id="webhook-route-1", *, source="webhook", title="Webhook Run"):
    return {
        "id": session_id,
        "title": title,
        "model": "test-model",
        "source": source,
        "raw_source": source,
        "message_count": 1,
        "actual_message_count": 1,
        "actual_user_message_count": 1,
        "last_activity": 10.0,
        "started_at": 9.0,
    }


def test_webhook_source_normalizes_to_webhook_category():
    from api.agent_sessions import is_cli_session_row, normalize_agent_session_source

    normalized = normalize_agent_session_source("webhook")

    assert normalized == {
        "raw_source": "webhook",
        "session_source": "webhook",
        "source_label": "Webhook",
    }
    assert is_cli_session_row({**normalized, "source": "webhook", "title": "Webhook Session"}) is False


def test_ensure_webhook_project_creates_dedicated_project():
    from api.models import WEBHOOK_PROJECT_NAME, ensure_webhook_project, load_projects, save_projects

    projects = load_projects()
    save_projects([p for p in projects if p.get("name") != WEBHOOK_PROJECT_NAME])

    pid1 = ensure_webhook_project()
    pid2 = ensure_webhook_project()

    assert pid1 == pid2
    projects = load_projects()
    webhook_projects = [p for p in projects if p.get("name") == WEBHOOK_PROJECT_NAME]
    assert len(webhook_projects) == 1
    assert webhook_projects[0]["project_id"] == pid1
    assert webhook_projects[0]["color"] == "#0ea5e9"
    assert len(pid1) == 12


def test_is_webhook_session_detects_source_metadata_not_session_id():
    from api.models import is_webhook_session

    assert is_webhook_session("any-id", source_tag="webhook") is True
    assert is_webhook_session("any-id", source_tag=" WebHook ") is True
    assert is_webhook_session("webhook:route:delivery") is False
    assert is_webhook_session("cron_abc", source_tag="cron") is False
    assert is_webhook_session("regular-session", source_tag="cli") is False
    assert is_webhook_session("") is False


def test_project_assigned_webhook_rows_are_returned_but_default_hidden():
    from api.models import (
        _hide_from_default_sidebar,
        _include_project_hidden_background_sidebar_sessions,
    )

    assert _hide_from_default_sidebar({"source_tag": "webhook", "session_id": "webhook:route:1"}) is True
    assert _hide_from_default_sidebar({"session_id": "webhook:route:1"}) is False

    visible = [
        {"session_id": "webui-1", "title": "Normal", "message_count": 1, "project_id": None},
    ]
    candidates = visible + [
        {
            "session_id": "webhook-visible",
            "source_tag": "webhook",
            "title": "Webhook output",
            "message_count": 2,
            "project_id": "webhook-project",
        },
        {
            "session_id": "webhook-unassigned",
            "source_tag": "webhook",
            "title": "Webhook output",
            "message_count": 2,
            "project_id": None,
        },
        {
            "session_id": "webhook-empty",
            "source_tag": "webhook",
            "title": "Webhook output",
            "message_count": 0,
            "project_id": "webhook-project",
        },
    ]

    rows = _include_project_hidden_background_sidebar_sessions(candidates, visible)

    by_id = {row["session_id"]: row for row in rows}
    assert set(by_id) == {"webui-1", "webhook-visible"}
    assert by_id["webhook-visible"]["default_hidden"] is True


def test_webhook_rows_get_webhook_project_id(monkeypatch, tmp_path):
    import api.models as models

    db = tmp_path / "state.db"
    db.write_text("", encoding="utf-8")

    monkeypatch.setattr(models, "read_importable_agent_session_rows", lambda *_a, **_kw: [_agent_row()])
    monkeypatch.setattr(models, "get_last_workspace", lambda: tmp_path)
    monkeypatch.setattr(models, "ensure_cron_project", lambda: "cron-project-id")
    monkeypatch.setattr(models, "ensure_webhook_project", lambda: "webhook-project-id", raising=False)
    monkeypatch.setattr(models.Session, "load_metadata_only", lambda _sid: None)

    rows = models._load_cli_sessions_uncached(tmp_path, db, _cli_profile=None, cron_project_limit=False)

    assert len(rows) == 1
    assert rows[0]["source_tag"] == "webhook"
    assert rows[0]["session_source"] == "webhook"
    assert rows[0]["source_label"] == "Webhook"
    assert rows[0]["project_id"] == "webhook-project-id"
    assert rows[0]["is_cli_session"] is False


def test_webhook_second_pass_keeps_older_project_rows_available(monkeypatch, tmp_path):
    import api.models as models

    db = tmp_path / "state.db"
    db.write_text("", encoding="utf-8")
    calls = []

    def fake_read_rows(_db_path, **kwargs):
        calls.append(kwargs)
        if kwargs.get("include_sources") == ("webhook",):
            return [_agent_row("webhook-older", title="Older webhook")]
        return []

    monkeypatch.setattr(models, "read_importable_agent_session_rows", fake_read_rows)
    monkeypatch.setattr(models, "get_last_workspace", lambda: tmp_path)
    monkeypatch.setattr(models, "ensure_cron_project", lambda: "cron-project-id")
    monkeypatch.setattr(models, "ensure_webhook_project", lambda: "webhook-project-id", raising=False)
    monkeypatch.setattr(models.Session, "load_metadata_only", lambda _sid: None)

    rows = models._load_cli_sessions_uncached(tmp_path, db, _cli_profile=None, cron_project_limit=False)

    assert [call.get("include_sources") for call in calls] == [None, ("webhook",)]
    assert len(rows) == 1
    assert rows[0]["session_id"] == "webhook-older"
    assert rows[0]["project_id"] == "webhook-project-id"
