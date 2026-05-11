from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
ROUTES = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def test_workspace_dropdown_splits_workspace_row_into_explicit_actions():
    assert "newChatBtn.textContent=t('workspace_action_new_chat')" in PANELS
    assert "moveBtn.textContent=t('workspace_action_move_chat')" in PANELS
    assert "defaultBtn.textContent=t('workspace_action_set_default')" in PANELS
    assert "sessionsBtn.textContent=t('workspace_action_show_sessions')" in PANELS
    assert "newChatBtn.onclick=async(e)=>{e.stopPropagation();await newChatInWorkspace(w.path,w.name);};" in PANELS
    assert "moveBtn.onclick=async(e)=>{e.stopPropagation();await moveCurrentChatToWorkspace(w.path,w.name);};" in PANELS
    assert "defaultBtn.onclick=async(e)=>{e.stopPropagation();await setDefaultWorkspaceForNewChats(w.path,w.name);};" in PANELS
    assert "sessionsBtn.onclick=(e)=>{e.stopPropagation();filterSessionsByWorkspace(w.path,w.name);" in PANELS


def test_workspace_new_chat_action_does_not_move_existing_session_or_block_on_global_busy():
    start = PANELS.index("async function newChatInWorkspace")
    end = PANELS.index("async function moveCurrentChatToWorkspace")
    body = PANELS[start:end]
    assert "newSession(true,{workspace:path})" in body
    assert "/api/session/update" not in body
    assert "S.busy" not in body
    assert "const inheritWs=(options.workspace||switchWs||(S.session?S.session.workspace:null)||(S._profileDefaultWorkspace||null));" in SESSIONS


def test_workspace_move_current_chat_action_is_the_only_dropdown_path_that_updates_session_workspace():
    start = PANELS.index("async function moveCurrentChatToWorkspace")
    end = PANELS.index("async function setDefaultWorkspaceForNewChats")
    body = PANELS[start:end]
    assert "_isCurrentSessionBusyForWorkspaceMove()" in body
    assert "api('/api/session/update',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,workspace:path})})" in body
    assert "if(S.session.workspace!==path)" in body
    wrapper_start = PANELS.index("async function switchToWorkspace")
    wrapper_end = PANELS.index("async function setDefaultWorkspaceForNewChats")
    wrapper = PANELS[wrapper_start:wrapper_end]
    assert "return moveCurrentChatToWorkspace(path,name);" in wrapper
    assert "/api/session/update" not in wrapper


def test_workspace_default_action_uses_dedicated_last_workspace_endpoint():
    assert "if parsed.path == \"/api/workspaces/set_last\":" in ROUTES
    assert "return _handle_workspace_set_last(handler, body)" in ROUTES
    assert "api('/api/workspaces/set_last',{method:'POST',body:JSON.stringify({path})})" in PANELS
    assert "def _handle_workspace_set_last(handler, body):" in ROUTES
    assert "set_last_workspace(str(p))" in ROUTES


def test_session_update_rejects_workspace_move_while_session_is_active_but_keeps_model_updates_allowed():
    assert 'workspace_requested = "workspace" in body' in ROUTES
    assert 'workspace_changing = workspace_requested and str(old_ws or "") != str(new_ws or "")' in ROUTES
    assert 'getattr(s, "active_stream_id", None) or getattr(s, "pending_user_message", None)' in ROUTES
    assert 'status=409' in ROUTES
    session_update = ROUTES[ROUTES.index('if parsed.path == "/api/session/update":'):ROUTES.index('if parsed.path == "/api/session/delete":')]
    guard_pos = session_update.index('if workspace_changing and (')
    save_pos = session_update.index('s.save()')
    model_pos = session_update.index('if "model" in body or "model_provider" in body:')
    assert guard_pos < model_pos < save_pos


def test_workspace_session_filter_is_separate_from_project_filter_and_clearable():
    assert "let _activeWorkspaceFilter = null;" in SESSIONS
    assert "function filterSessionsByWorkspace(path, name){" in SESSIONS
    assert "const workspaceFiltered=_activeWorkspaceFilter?projectFiltered.filter(s=>s.workspace===_activeWorkspaceFilter.path):projectFiltered;" in SESSIONS
    assert "clearWorkspaceSessionFilter" in SESSIONS
    assert "workspace-filter" in SESSIONS


def test_workspace_actions_have_translatable_labels():
    for key in [
        "workspace_action_new_chat",
        "workspace_action_move_chat",
        "workspace_action_set_default",
        "workspace_action_show_sessions",
        "workspace_filter_clear",
    ]:
        assert f"{key}:" in I18N
