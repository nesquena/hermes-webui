"""Regression tests for #3019 cron sessions under the Cron Jobs project."""


def test_project_assigned_cron_rows_are_returned_but_default_hidden():
    from api.models import _include_project_hidden_background_sidebar_sessions

    visible = [
        {"session_id": "webui-1", "title": "Normal", "message_count": 1, "project_id": None},
    ]
    candidates = visible + [
        {
            "session_id": "cron_visible",
            "source_tag": "cron",
            "title": "Cron output",
            "message_count": 2,
            "project_id": "cron-project",
        },
        {
            "session_id": "cron_unassigned",
            "source_tag": "cron",
            "title": "Cron output",
            "message_count": 2,
            "project_id": None,
        },
        {
            "session_id": "cron_empty",
            "source_tag": "cron",
            "title": "Cron output",
            "message_count": 0,
            "project_id": "cron-project",
        },
    ]

    rows = _include_project_hidden_background_sidebar_sessions(candidates, visible)

    by_id = {row["session_id"]: row for row in rows}
    assert set(by_id) == {"webui-1", "cron_visible"}
    assert by_id["cron_visible"]["default_hidden"] is True


def test_agent_side_cron_rows_keep_project_chip_visibility():
    from api.routes import _dedupe_cli_sidebar_sessions_for_api

    represented_webui_ids = {"webui-1"}
    cli_rows = [
        {
            "session_id": "cli-normal",
            "source_tag": "cli",
            "title": "Manual deployment notes",
            "message_count": 1,
            "updated_at": 5,
        },
        {
            "session_id": "cron-agent-project",
            "source_tag": "cron",
            "title": "Cron agent run",
            "message_count": 3,
            "project_id": "cron-project",
            "updated_at": 4,
        },
        {
            "session_id": "cron-agent-unassigned",
            "source_tag": "cron",
            "title": "Cron hidden",
            "message_count": 3,
            "updated_at": 3,
        },
        {
            "session_id": "cron-agent-empty",
            "source_tag": "cron",
            "title": "Cron empty",
            "message_count": 0,
            "project_id": "cron-project",
            "updated_at": 2,
        },
        {
            "session_id": "webui-1",
            "source_tag": "cli",
            "title": "Duplicate imported copy",
            "message_count": 1,
        },
    ]

    rows = _dedupe_cli_sidebar_sessions_for_api(cli_rows, represented_webui_ids)

    by_id = {row["session_id"]: row for row in rows}
    assert set(by_id) == {"cli-normal", "cron-agent-project"}
    assert by_id["cron-agent-project"]["default_hidden"] is True


def test_session_list_project_filter_can_reveal_default_hidden_cron_rows():
    src = ( __import__("pathlib").Path(__file__).parent.parent / "static" / "sessions.js").read_text(encoding="utf-8")

    assert "!s.default_hidden||(_activeProject&&_activeProject!==NO_PROJECT_FILTER&&s.project_id===_activeProject)" in src
