"""Regression tests for large-session sidebar resilience.

The sidebar must fail visibly when the sessions API times out, must not let
optional project metadata blank the conversations list, and must not return
bulky session-detail fields in /api/sessions rows.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent


def _sessions_js() -> str:
    return (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _workspace_js() -> str:
    return (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")


def test_session_list_refresh_has_visible_failure_state_instead_of_console_only():
    src = _sessions_js()
    block_start = src.find("async function _runRenderSessionListRefresh")
    assert block_start > 0
    block_end = src.find("async function _drainRenderSessionListQueue", block_start)
    assert block_end > block_start
    block = src[block_start:block_end]

    assert "console.warn('renderSessionList',e);" not in block
    assert "_showSessionListLoadError" in block
    assert "renderSessionListFromCache" in block
    assert "session-list-error" in src
    assert "Retry" in src


def test_sessions_and_projects_load_independently_so_projects_failure_cannot_blank_sidebar():
    src = _sessions_js()
    block_start = src.find("async function _runRenderSessionListRefresh")
    assert block_start > 0
    block_end = src.find("async function _drainRenderSessionListQueue", block_start)
    assert block_end > block_start
    block = src[block_start:block_end]

    assert "Promise.all" not in block
    assert "api('/api/sessions' + allProfilesQS" in block
    assert "try{\n      projData = await api('/api/projects'" in block
    assert "console.warn('renderProjectsList'," in block
    assert "_applySessionListPayload(sessData,projData)" in block


def test_sessions_api_uses_longer_timeout_and_timeout_retry_for_boot_refresh():
    sessions_src = _sessions_js()
    workspace_src = _workspace_js()

    assert "timeoutMs:_sessionListHasLoadedOnce?30000:_SESSION_LIST_BOOT_TIMEOUT_MS" in sessions_src
    assert "retryTimeouts:true" in sessions_src
    assert "retryStatuses:[502,503,504]" in sessions_src
    assert "retryTimeouts" in workspace_src
    assert "retryStatuses" in workspace_src


def test_sessions_sidebar_response_item_drops_bulky_detail_fields(monkeypatch):
    from api import routes

    monkeypatch.setattr(routes, "_session_attention_summary", lambda sid: {"kind": "none"})
    row = {
        "session_id": "sid-heavy",
        "title": "Visible title",
        "updated_at": 10,
        "last_message_at": 11,
        "message_count": 123,
        "user_message_count": 61,
        "compression_anchor_summary": "X" * 50000,
        "compression_anchor_details": {"huge": True},
        "context_engine_state": {"expensive": True},
        "gateway_routing_history": [{"hop": 1}],
        "composer_draft": "draft body",
        "pending_user_message": "private pending text",
        "tool_calls": [{"id": "call"}],
        "messages": [{"role": "user", "content": "not for sidebar"}],
    }

    item = routes._sidebar_session_response_item(row, redact_enabled=False)

    assert item["session_id"] == "sid-heavy"
    assert item["title"] == "Visible title"
    assert item["message_count"] == 123
    assert item["attention"] == {"kind": "none"}
    for key in (
        "compression_anchor_summary",
        "compression_anchor_details",
        "context_engine_state",
        "gateway_routing_history",
        "composer_draft",
        "pending_user_message",
        "tool_calls",
        "messages",
    ):
        assert key not in item


def test_json_helper_can_emit_compact_json_for_large_list_endpoints():
    from api.helpers import _json_response_body

    body = _json_response_body({"a": 1, "nested": {"b": 2}}, pretty=False).decode("utf-8")

    assert body == '{"a":1,"nested":{"b":2}}'
    assert "\n" not in body
