"""Source guards for retiring the old Unassigned chip sidebar filter.

The Projects/Chats sidebar read model groups sessions by workspace and chats via
`/api/session-index`. The old project-chip filter bar, including the
"Unassigned" chip, should no longer be part of `renderSessionListFromCache`.
"""

from __future__ import annotations

import pathlib


JS = pathlib.Path(__file__).parent.parent / "static" / "sessions.js"


def _js() -> str:
    return JS.read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    start = source.find(f"function {name}(")
    assert start != -1, f"{name} not found"
    paren_start = source.find("(", start)
    paren_depth = 1
    i = paren_start + 1
    while i < len(source) and paren_depth:
        if source[i] == "(":
            paren_depth += 1
        elif source[i] == ")":
            paren_depth -= 1
        i += 1
    depth_start = source.find("{", i)
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


def test_render_no_longer_builds_old_project_chip_filter_bar():
    body = _function_body(_js(), "renderSessionListFromCache")

    assert "project-bar" not in body
    assert "noneChip.textContent='Unassigned';" not in body
    assert "profileFiltered.filter(s=>!s.project_id)" not in body
    assert "_activeProject===NO_PROJECT_FILTER" not in body


def test_render_uses_projects_and_chats_sections_instead():
    body = _function_body(_js(), "renderSessionListFromCache")

    assert "appendSectionLabel('Projects')" in body
    assert "appendSectionLabel('Chats')" in body
    assert "workspace_group:'workspace'" in body
    assert "newSession(true,{workspace:group.workspace,workspace_group:'workspace'})" in body


def test_legacy_project_picker_still_allows_removing_a_project_assignment():
    """The row action menu still needs the server-side move-to-null path."""
    js = _js()

    assert "await api('/api/session/move',{method:'POST',body:JSON.stringify({session_id:session.session_id,project_id:null})});" in js
    assert "_allSessions[idx].project_id=null" in js
