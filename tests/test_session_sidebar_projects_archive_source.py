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


def test_session_rows_use_existing_profile_avatar_helpers():
    js = _js()

    assert "session-agent-avatar" in js
    assert "_profileAvatarForUi" in js or "_profileAvatarMarkup" in js
    assert "profile-avatar--session-row" in js


def test_virtual_scroll_flat_rows_contract_is_preserved():
    js = _js()
    body = _function_body(js, "renderSessionListFromCache")

    assert "const flatSessionRows=[]" in body
    assert "flatSessionRows.push({group,session:s})" in body
    assert "_sessionVirtualWindow" in body
    assert "_sessionVirtualSpacer" in body


def test_archive_rows_are_not_synced_into_current_groups():
    js = _js()
    sync_body = _function_body(js, "_syncSessionIndexGroupsWithRows")
    apply_body = _function_body(js, "_applySessionIndexPayload")

    assert "function _syncSessionIndexGroupsWithRows(rows,currentRows=null)" in sync_body
    assert "const currentRows=_sessionIndexCurrentRows()" in apply_body
    assert "_syncSessionIndexGroupsWithRows(_allSessions,currentRows)" in apply_body
    assert "_sessionIndexLoadedArchiveRows()" in sync_body
    assert "archiveIds.has(row.session_id)" in sync_body
    assert "row.archived||row.age_archived" in sync_body
    assert "_isOptimisticFirstTurnSessionRow(row)" in sync_body


def test_collapsed_projects_do_not_add_archive_rows_to_virtual_flat_list():
    js = _js()
    body = _function_body(js, "renderSessionListFromCache")

    assert "if(group.kind==='project'&&projectCollapsed[groupId]) return;" in body
    collapsed_guard = body.index("if(group.kind==='project'&&projectCollapsed[groupId]) return;")
    archive_guard = body.index("if(archive&&archiveCollapsed[groupId]!==false) return;")
    archive_push = body.index("flatSessionRows.push({group,session:s,archive:true})")
    assert collapsed_guard < archive_guard < archive_push


def test_fresh_current_rows_prune_cached_archive_rows_on_index_apply():
    js = _js()
    body = _function_body(js, "_applySessionIndexPayload")

    assert "const currentIds=new Set(currentRows.map(s=>s&&s.session_id).filter(Boolean))" in body
    assert "_sessionIndexArchiveRows[key]=rows.filter(s=>!(s&&s.session_id&&currentIds.has(s.session_id)))" in body
    assert body.index("const currentRows=_sessionIndexCurrentRows()") < body.index("const archiveRows=_sessionIndexLoadedArchiveRows()")


def test_archive_load_rechecks_live_current_rows_before_assignment():
    js = _js()
    body = _function_body(js, "_loadSessionIndexArchive")

    assert "const liveGroupIds=new Set(_sessionIndexGroups.map(g=>_sessionIndexGroupId(g)).filter(Boolean))" in body
    assert "if(!liveGroupIds.has(groupId))" in body
    assert "const currentIds=new Set(_sessionIndexCurrentRows().map(s=>s&&s.session_id).filter(Boolean))" in body
    assert "const visibleMerged=merged.filter(s=>!(s&&s.session_id&&currentIds.has(s.session_id)))" in body
    assert "_sessionIndexArchiveRows[groupId]=visibleMerged" in body
    assert body.index("const data=await api('/api/session-index/archive?'") < body.index("const liveGroupIds=new Set")
    assert body.index("const currentIds=new Set(_sessionIndexCurrentRows()") < body.index("_sessionIndexArchiveRows[groupId]=visibleMerged")


def test_project_and_archive_headers_are_keyboard_accessible():
    js = _js()
    handler = _function_body(js, "_handleSidebarDisclosureKeydown")
    body = _function_body(js, "renderSessionListFromCache")

    assert "if(e.target!==e.currentTarget) return;" in handler
    assert "e.key==='Enter'||e.key===' '||e.key==='Spacebar'" in handler
    assert "e.preventDefault()" in handler
    assert "e.currentTarget.click()" in handler
    assert "hdr.setAttribute('role','button')" in body
    assert "hdr.tabIndex=0" in body
    assert "hdr.setAttribute('aria-expanded',collapsed?'false':'true')" in body
    assert "hdr.onkeydown=_handleSidebarDisclosureKeydown" in body
    assert "projectToggle.setAttribute('role','button')" in body
    assert "projectToggle.tabIndex=0" in body
    assert "projectToggle.setAttribute('aria-expanded',collapsed?'false':'true')" in body
    assert "projectToggle.onkeydown=_handleSidebarDisclosureKeydown" in body


def test_project_new_button_is_not_nested_inside_disclosure_control():
    js = _js()
    body = _function_body(js, "renderSessionListFromCache")

    assert "const projectToggle=document.createElement('div')" in body
    assert "projectToggle.className='session-index-project-disclosure'" in body
    assert "projectToggle.appendChild(caret);projectToggle.appendChild(folder);projectToggle.appendChild(name);projectToggle.appendChild(count)" in body
    assert "hdr.appendChild(projectToggle);hdr.appendChild(add)" in body
    assert "hdr.appendChild(caret);hdr.appendChild(folder);hdr.appendChild(name);hdr.appendChild(count);hdr.appendChild(add)" not in body


def test_project_new_button_has_accessible_name():
    js = _js()
    body = _function_body(js, "renderSessionListFromCache")

    assert "add.setAttribute('aria-label','New chat in this project')" in body
