"""Keep an actual ACL refusal distinct from unavailable ACL/path metadata."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from api import workspace, workspace_access


@pytest.fixture
def acl(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir()
    target = root / "evidence.txt"
    target.write_text("unchanged")
    registry = tmp_path / "workspaces.json"
    rows = [{"path": str(root), "owner_email": "owner@example.test", "members": []}]
    registry.write_text(json.dumps(rows))
    monkeypatch.setattr(workspace, "_workspaces_file", lambda: registry)
    return SimpleNamespace(root=root, target=target, registry=registry, rows=rows)


@pytest.mark.parametrize("alias", [False, True])
def test_nonmember_gets_membership_required_for_real_and_symlink_paths(acl, alias):
    target = acl.target
    if alias:
        link = acl.root.parent / "alias"
        link.symlink_to(acl.root, target_is_directory=True)
        target = link / acl.target.name
    before = acl.registry.read_bytes()
    with pytest.raises(PermissionError, match="^Workspace membership required$"):
        workspace_access.ensure_scope_access("member@example.test", target)
    assert acl.target.read_text() == "unchanged"
    assert acl.registry.read_bytes() == before


@pytest.mark.parametrize("mode", ["owner", "member", "unowned"])
def test_authorized_and_legacy_unowned_access_is_unchanged(acl, mode):
    actor = "owner@example.test" if mode == "owner" else "member@example.test"
    if mode == "member":
        acl.rows[0]["members"] = [" MEMBER@example.test "]
    if mode == "unowned":
        acl.rows[0].pop("owner_email")
    acl.registry.write_text(json.dumps(acl.rows))
    workspace_access.ensure_scope_access(actor, acl.target)


@pytest.mark.parametrize("raw", ["{ invalid", "{}", '[{"path":"/workspace","members":3}]'])
def test_malformed_registry_stays_unavailable_and_closed(acl, raw):
    acl.registry.write_text(raw)
    with pytest.raises(PermissionError, match="^Workspace membership unavailable$"):
        workspace_access.ensure_scope_access("member@example.test", acl.target)
    assert acl.registry.read_text() == raw


@pytest.mark.parametrize("position", ["target", "ancestor"])
def test_symlink_resolution_loop_stays_unavailable(acl, position):
    first = acl.root.parent / "loop-a"
    second = acl.root.parent / "loop-b"
    first.symlink_to(second)
    second.symlink_to(first)
    target = first if position == "target" else acl.target
    if position == "ancestor":
        acl.rows[0]["path"] = str(first)
        acl.registry.write_text(json.dumps(acl.rows))
    with pytest.raises(PermissionError, match="^Workspace membership unavailable$"):
        workspace_access.ensure_scope_access("member@example.test", target)


@pytest.mark.parametrize("position", ["target", "ancestor"])
def test_filesystem_permission_error_is_not_exposed_as_membership_denial(acl, monkeypatch, position):
    original = Path.resolve
    blocked = acl.target if position == "target" else acl.root

    def resolve(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("sensitive filesystem diagnostic")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(PermissionError, match="^Workspace membership unavailable$"):
        workspace_access.ensure_scope_access("member@example.test", acl.target)


@pytest.mark.parametrize("malformed", [False, True])
def test_execution_guard_returns_403_with_the_correct_reason(acl, monkeypatch, malformed):
    from api import ownership, routes

    if malformed:
        acl.registry.write_text("{ invalid")
    monkeypatch.setattr(ownership, "request_owner_scope", lambda _: "member@example.test")
    session = SimpleNamespace(workspace=str(acl.root), worktree_repo_root=None)
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda _: session)
    handler = SimpleNamespace(wfile=io.BytesIO(), status=None)
    handler.send_response = lambda value: setattr(handler, "status", value)
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    assert workspace_access.guard_session_request(
        handler, urlsplit("/api/chat/start"), {"session_id": "synthetic"}) is False
    assert handler.status == 403
    reason = "unavailable" if malformed else "required"
    assert json.loads(handler.wfile.getvalue()) == {"error": "Workspace membership " + reason}


def test_membership_of_a_child_workspace_survives_a_broader_excluding_one(acl, monkeypatch):
    """14-09-2026: a stray workspace rooted at /home (owner only, everyone else
    excluded) sat above every client folder and blocked people from the
    specific workspaces they WERE members of. Membership in any containing
    workspace must grant access regardless of iteration order."""
    child = acl.root / "makro"
    child.mkdir()
    target = child / "notes.md"
    target.write_text("client work")
    broad = {"path": str(acl.root.parent), "owner_email": "someone@example.test", "members": []}
    specific = {"path": str(child), "owner_email": "owner@example.test",
                "members": ["member@example.test"]}
    for rows in ([broad, specific], [specific, broad]):
        acl.registry.write_text(json.dumps(rows))
        workspace_access.ensure_scope_access("member@example.test", target)  # no raise
    # A true non-member of every containing workspace is still refused.
    acl.registry.write_text(json.dumps([broad, specific]))
    with pytest.raises(PermissionError, match="^Workspace membership required$"):
        workspace_access.ensure_scope_access("stranger@example.test", target)


def test_retargeted_workspace_alias_is_checked_again_before_authorizing(acl):
    other = acl.root.parent / 'other-private'
    other.mkdir()
    evidence = other / 'evidence.txt'
    evidence.write_text('must stay private')
    alias = acl.root.parent / 'workspace-alias'
    alias.symlink_to(acl.root, target_is_directory=True)
    acl.rows[0]['path'] = str(alias)
    acl.registry.write_text(json.dumps(acl.rows))
    workspace_access.ensure_scope_access('owner@example.test', acl.target)
    alias.unlink()
    alias.symlink_to(other, target_is_directory=True)
    with pytest.raises(PermissionError, match='^Workspace membership required$'):
        workspace_access.ensure_scope_access('member@example.test', evidence)
    workspace_access.ensure_scope_access('owner@example.test', evidence)
    assert evidence.read_text() == 'must stay private'


def test_missing_child_of_an_owned_workspace_still_requires_membership(acl):
    target = acl.root / 'not-created-yet' / 'report.md'
    with pytest.raises(PermissionError, match='^Workspace membership required$'):
        workspace_access.ensure_scope_access('member@example.test', target)
    workspace_access.ensure_scope_access('owner@example.test', target)
    assert not target.exists()
