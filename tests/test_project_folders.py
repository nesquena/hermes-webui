"""Projects + Spaces merge step 2: folders inside a project, behind a flag."""
from pathlib import Path

import pytest

from api import project_folders as pf

ROOT = Path(__file__).resolve().parents[1]


def test_flag_is_off_by_default_and_env_or_setting_turns_it_on():
    assert pf.enabled({}, environ={}) is False
    assert pf.enabled({"project_folders_enabled": True}, environ={}) is True
    assert pf.enabled({}, environ={"HERMES_WEBUI_PROJECT_FOLDERS": "1"}) is True
    assert pf.enabled({"project_folders_enabled": "yes"}, environ={}) is False


def _projects(tmp_path):
    team = tmp_path / "team"; team.mkdir()
    return [
        {"project_id": "p1", "name": "Acme", "owner_email": "owner@x.nl", "collaboration": True,
         "members": ["member@x.nl"], "workspace": str(team)},
        {"project_id": "p2", "name": "Personal", "owner_email": "solo@x.nl"},
        {"project_id": "p3", "name": "Gone", "owner_email": "owner@x.nl", "deleted": True},
    ]


def test_team_folder_is_listed_first_and_hidden_without_access(tmp_path):
    projects = _projects(tmp_path)
    assert [f["role"] for f in pf.folder_entries(projects[0])] == ["project"]
    assert pf.visible_folders(projects[0], lambda p: False) == []


def test_owner_can_add_a_visible_space_member_cannot(tmp_path):
    projects = _projects(tmp_path)
    space = tmp_path / "space"; space.mkdir()
    with pytest.raises(PermissionError):
        pf.add_folder(projects, "p1", str(space), "", email="member@x.nl", is_admin=False,
                      may_write_files=True, visible_space_paths={str(space)})
    p = pf.add_folder(projects, "p1", str(space), "Docs", email="owner@x.nl", is_admin=False,
                      may_write_files=True, visible_space_paths={str(space)}, now=1)
    assert p["folders"] == [{"path": str(space), "name": "Docs", "role": "linked", "added_by": "owner@x.nl", "added_at": 1.0}]
    # Adding again is idempotent; the team folder itself is never duplicated.
    pf.add_folder(projects, "p1", str(space), "Docs", email="owner@x.nl", is_admin=False,
                  may_write_files=True, visible_space_paths={str(space)})
    pf.add_folder(projects, "p1", projects[0]["workspace"], "", email="owner@x.nl", is_admin=False,
                  may_write_files=True, visible_space_paths={projects[0]["workspace"]})
    assert len(projects[0]["folders"]) == 1


def test_no_access_gain_through_adding(tmp_path):
    projects = _projects(tmp_path)
    other = tmp_path / "not-mine"; other.mkdir()
    with pytest.raises(PermissionError):  # not one of the caller's spaces
        pf.add_folder(projects, "p2", str(other), "", email="solo@x.nl", is_admin=False,
                      may_write_files=True, visible_space_paths=set())
    with pytest.raises(PermissionError):  # needs files:write
        pf.add_folder(projects, "p2", str(other), "", email="solo@x.nl", is_admin=False,
                      may_write_files=False, visible_space_paths={str(other)})
    with pytest.raises(ValueError):
        pf.add_folder(projects, "p2", str(tmp_path / "missing"), "", email="solo@x.nl", is_admin=True,
                      may_write_files=True, visible_space_paths=set())
    with pytest.raises(LookupError):
        pf.add_folder(projects, "p3", str(other), "", email="owner@x.nl", is_admin=True,
                      may_write_files=True, visible_space_paths=set())
    # Admins may link any existing folder.
    assert pf.add_folder(projects, "p2", str(other), "", email="boss@x.nl", is_admin=True,
                         may_write_files=True, visible_space_paths=set())["folders"][0]["path"] == str(other)


def test_remove_and_limits(tmp_path):
    projects = _projects(tmp_path)
    paths = []
    for i in range(pf.MAX_FOLDERS):
        d = tmp_path / f"d{i}"; d.mkdir(); paths.append(str(d))
        pf.add_folder(projects, "p2", str(d), "", email="solo@x.nl", is_admin=False,
                      may_write_files=True, visible_space_paths=set(paths))
    extra = tmp_path / "extra"; extra.mkdir()
    with pytest.raises(ValueError):
        pf.add_folder(projects, "p2", str(extra), "", email="solo@x.nl", is_admin=False,
                      may_write_files=True, visible_space_paths={str(extra)})
    pf.remove_folder(projects, "p2", paths[0], email="solo@x.nl", is_admin=False)
    assert len(projects[1]["folders"]) == pf.MAX_FOLDERS - 1
    with pytest.raises(PermissionError):
        pf.remove_folder(projects, "p2", paths[1], email="someone@x.nl", is_admin=False)


def test_my_work_is_virtual_and_only_own_chats_without_project():
    sessions = [
        {"session_id": "a", "owner_email": "me@x.nl", "title": "Mine", "updated_at": 2},
        {"session_id": "b", "owner_email": "me@x.nl", "project_id": "p1"},
        {"session_id": "c", "owner_email": "other@x.nl"},
        {"session_id": "d", "owner_email": "me@x.nl", "archived": True},
        {"session_id": "e", "owner_email": "me@x.nl", "project_shared": True},
    ]
    data = pf.my_work(sessions, "me@x.nl", [{"path": "/w/home", "name": "Home"}])
    assert data["project"]["virtual"] is True and data["project"]["project_id"] == "my-work"
    assert [c["session_id"] for c in data["conversations"]] == ["a"]
    assert data["folders"] == [{"path": "/w/home", "name": "Home", "role": "space"}]


def test_routes_are_flag_gated_and_reuse_existing_checks():
    routes = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    for path in ("/api/projects/folders", "/api/projects/my-work", "/api/projects/folders/add",
                 "/api/projects/folders/remove", "/api/projects/folders/config"):
        assert f'"{path}"' in routes
    get_fn = routes[routes.index("def _handle_project_folders_get"):routes.index("def _handle_project_folders_post")]
    assert 'if not _project_folders_flag():\n        return bad(handler, "Not found", 404)' in get_fn
    assert "projects_hub.visible_projects" in get_fn and "ensure_scope_access" in routes
    post_fn = routes[routes.index("def _handle_project_folders_post"):]
    assert "PROJECTS_LOCK" in post_fn and 'has_permission("files:write")' in post_fn
    assert "visible_ids" in post_fn


def test_project_entry_replaces_spaces_menu_and_folder_apis_keep_the_existing_gate():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'data-panel="projects"' in html
    assert 'data-panel="workspaces"' not in html
    assert 'id="mainWorkspaces"' in html  # retained for contextual/internal callers
    js = (ROOT / "static" / "projects-folders.js").read_text(encoding="utf-8")
    assert "/api/projects/folders/config" in js and "if (!(await enabled())) return;" in js
