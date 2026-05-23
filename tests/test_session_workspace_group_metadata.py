"""Regression tests for persisted session workspace grouping metadata."""


def test_session_compact_preserves_chats_group_with_runtime_workspace():
    from api.models import Session

    session = Session(workspace="/tmp/hermes-runtime", workspace_group="chats")

    compact = session.compact()
    assert compact["workspace"] == "/tmp/hermes-runtime"
    assert compact["workspace_group"] == "chats"


def test_legacy_session_with_workspace_infers_workspace_group():
    from api.models import Session

    session = Session(workspace="/tmp/hermes-project")

    compact = session.compact()
    assert compact["workspace"] == "/tmp/hermes-project"
    assert compact["workspace_group"] == "workspace"


def test_new_session_keeps_runtime_workspace_but_groups_under_chats(monkeypatch):
    import api.models as models

    monkeypatch.setattr(models, "get_last_workspace", lambda: "/tmp/hermes-runtime")

    session = models.new_session(
        workspace=None,
        workspace_group="chats",
        profile="default",
    )
    try:
        compact = session.compact()
        assert session.workspace == "/tmp/hermes-runtime"
        assert compact["workspace"] == "/tmp/hermes-runtime"
        assert compact["workspace_group"] == "chats"
    finally:
        with models.LOCK:
            models.SESSIONS.pop(session.session_id, None)
