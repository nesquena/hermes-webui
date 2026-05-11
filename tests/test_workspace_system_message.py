from pathlib import Path

from api.streaming import _workspace_system_message


def test_workspace_system_message_describes_session_pinned_workspace_not_global_web_ui_selection():
    message = _workspace_system_message(Path("/tmp/project-a"))

    assert "Active workspace for this session: /tmp/project-a" in message
    assert (
        "session-pinned workspace selected when this chat/session was created or switched"
        in message
    )
    assert "selected in the web UI" not in message
    assert "single authoritative source" not in message
    assert "most recent [Workspace::v1: ...] tag" in message
