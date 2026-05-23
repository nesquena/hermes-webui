"""Source-level guards for the Projects/Chats session sidebar index renderer."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = REPO_ROOT / "static" / "sessions.js"


def _js() -> str:
    return SESSIONS_JS.read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    start = source.find(f"function {name}(")
    assert start != -1, f"{name} not found"
    paren_start = source.find("(", start)
    assert paren_start != -1, f"{name} params not found"
    paren_depth = 1
    i = paren_start + 1
    while i < len(source) and paren_depth:
        if source[i] == "(":
            paren_depth += 1
        elif source[i] == ")":
            paren_depth -= 1
        i += 1
    assert paren_depth == 0, f"{name} params did not terminate"
    depth_start = source.find("{", i)
    assert depth_start != -1, f"{name} body not found"
    depth = 1
    i = depth_start + 1
    while i < len(source) and depth:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} body did not terminate"
    return source[start:i]


def test_render_session_list_uses_session_index_read_model():
    js = _js()
    body = _function_body(js, "renderSessionList")

    assert "/api/session-index" in body
    assert "/api/projects" not in body
    assert "all_profiles" not in body
    assert "current_session_id" in body
    assert "_applySessionIndexPayload(indexData)" in js


def test_sidebar_index_state_and_local_storage_keys_exist():
    js = _js()

    for snippet in (
        "let _sessionIndexGroups = [];",
        "let _sessionIndexArchiveRows = {};",
        "let _sessionIndexArchiveNextCursor = {};",
        "let _sessionIndexArchiveLoading = {};",
        "let _sessionIndexArchiveErrors = {};",
        "hermes-sidebar-projects-collapsed",
        "hermes-sidebar-archive-collapsed",
    ):
        assert snippet in js


def test_lazy_archive_loader_and_group_labels_are_present():
    js = _js()

    assert "function _loadSessionIndexArchive" in js
    assert "/api/session-index/archive" in js
    assert "workspace:" in js
    assert "Archive" in js


def test_archive_render_reuses_date_group_primitives():
    js = _js()
    body = _function_body(js, "renderSessionListFromCache")

    archive_idx = body.find("label.textContent='Archive'")
    assert archive_idx != -1
    archive_window = body[max(0, archive_idx - 1600): archive_idx + 3000]
    for class_name in (
        "session-date-group",
        "session-date-header",
        "session-date-caret",
    ):
        assert class_name in archive_window


def test_profile_badges_removed_but_avatar_hooks_remain():
    js = _js()

    assert "metaBits.push(s.profile)" not in js
    assert "session-agent-avatar" in js
    assert "_profileAvatar" in js


def test_virtual_scroll_flat_rows_contract_is_preserved():
    js = _js()
    body = _function_body(js, "renderSessionListFromCache")

    assert "const flatSessionRows=[]" in body
    assert "flatSessionRows.push({group,session:s})" in body
    assert "_sessionVirtualWindow" in body
    assert "_sessionVirtualSpacer" in body
