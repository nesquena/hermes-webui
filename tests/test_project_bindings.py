"""Tests for project bindings (/api/projects/bind).

A project can pin a workspace / model / reasoning effort so the quick-create
(+) button opens a new session pre-configured with that project's context.

- Bind fields persist to projects.json.
- workspace binding auto-registers the path in the saved workspace list.
- Invalid effort / nonexistent workspace are rejected.
- null/'' unbinds a field.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from tests._pytest_port import BASE


def _get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {}


def _post(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {}


HOME_WS = str(Path.home())


def _create_project():
    res = _post("/api/projects/create", {"name": "bind-test", "color": "#50c878"})
    assert res.get("ok"), f"create failed: {res}"
    return res["project"]["project_id"]


def test_bind_workspace_model_effort_roundtrip(tmp_path):
    pid = _create_project()
    ws_dir = tmp_path / "roundtrip-ws"
    ws_dir.mkdir()
    ws_str = str(ws_dir)
    res = _post("/api/projects/bind", {
        "project_id": pid,
        "workspace": ws_str,
        "model": "test-model-1",
        "model_provider": "custom:test",
        "reasoning_effort": "high",
    })
    assert res.get("ok"), f"bind failed: {res}"
    p = res["project"]
    assert p["workspace"] == str(ws_dir.resolve())
    assert p["model"] == "test-model-1"
    assert p["model_provider"] == "custom:test"
    assert p["reasoning_effort"] == "high"

    # Persisted on disk via GET /api/projects
    proj_res = _get("/api/projects")
    projects = proj_res.get("projects", [])
    saved = next((x for x in projects if x["project_id"] == pid), None)
    assert saved is not None, "bound project must appear in /api/projects"
    assert saved.get("reasoning_effort") == "high"


def test_bind_workspace_auto_registers_in_workspace_list(tmp_path):
    """A workspace outside the default saved list is auto-registered so an
    admin-style path (e.g. D:\\projects\\...) can be bound in one step."""
    pid = _create_project()
    import pathlib
    ws_dir = tmp_path / "bound-ws"
    ws_dir.mkdir()
    # Use the native path string (no manual slash translation — Windows and
    # POSIX CI both pass through; the server canonicalizes to the platform
    # form before storing).
    ws_str = str(ws_dir)
    res = _post("/api/projects/bind", {"project_id": pid, "workspace": ws_str})
    assert res.get("ok"), f"bind failed: {res}"
    assert pathlib.Path(res["project"]["workspace"]).resolve() == ws_dir.resolve()

    # The path must now be in the saved workspace list.
    ws_res = _get("/api/workspaces")
    assert any(
        pathlib.Path(w["path"]).resolve() == ws_dir.resolve()
        for w in ws_res.get("workspaces", [])
    ), "bound workspace must be auto-registered in the workspace list"


def test_bind_rejects_invalid_effort():
    pid = _create_project()
    res = _post("/api/projects/bind", {
        "project_id": pid,
        "reasoning_effort": "insane",
    })
    assert "error" in res, "invalid effort must be rejected"
    assert "reasoning_effort" in res["error"]


def test_bind_rejects_nonexistent_workspace():
    pid = _create_project()
    res = _post("/api/projects/bind", {
        "project_id": pid,
        "workspace": "Z:/definitely/not/here",
    })
    assert "error" in res, "nonexistent workspace must be rejected"


def test_bind_null_unbinds_field():
    pid = _create_project()
    _post("/api/projects/bind", {
        "project_id": pid,
        "workspace": HOME_WS,
        "reasoning_effort": "medium",
    })
    res = _post("/api/projects/bind", {
        "project_id": pid,
        "workspace": None,
        "reasoning_effort": None,
    })
    assert res.get("ok"), f"unbind failed: {res}"
    p = res["project"]
    assert "workspace" not in p
    assert "reasoning_effort" not in p


def test_bind_unknown_project_404():
    res = _post("/api/projects/bind", {
        "project_id": "deadbeef0000",
        "workspace": HOME_WS,
    })
    assert "error" in res, "unknown project must be rejected"


def test_bind_multiple_workspaces_with_default(tmp_path):
    """A project can bind several workspaces; the marked default drives new
    sessions, and default must always be a member of the workspaces list."""
    pid = _create_project()
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()
    a, b = str(ws_a), str(ws_b)

    res = _post("/api/projects/bind", {
        "project_id": pid,
        "workspaces": [a, b],
        "default_workspace": b,
        "auto_assign": True,
    })
    assert res.get("ok"), f"bind failed: {res}"
    p = res["project"]
    assert set(p["workspaces"]) == {a, b}, p["workspaces"]
    assert p["default_workspace"] == b
    assert p.get("auto_assign") is True

    # Default can also auto-add itself to the list (invariant holds).
    res2 = _post("/api/projects/bind", {
        "project_id": pid,
        "default_workspace": a,
    })
    p2 = res2["project"]
    assert p2["default_workspace"] == a
    assert a in p2["workspaces"]


def test_bind_workspaces_clear_with_null():
    pid = _create_project()
    _post("/api/projects/bind", {
        "project_id": pid,
        "workspaces": [HOME_WS],
        "default_workspace": HOME_WS,
    })
    res = _post("/api/projects/bind", {"project_id": pid, "workspaces": None})
    assert res.get("ok"), f"unbind failed: {res}"
    p = res["project"]
    assert "workspaces" not in p
    assert "default_workspace" not in p


def test_bind_auto_assign_off():
    pid = _create_project()
    _post("/api/projects/bind", {
        "project_id": pid,
        "workspaces": [HOME_WS],
        "auto_assign": True,
    })
    res = _post("/api/projects/bind", {
        "project_id": pid,
        "auto_assign": False,
    })
    assert res.get("ok")
    assert "auto_assign" not in res["project"]


def test_bind_workspaces_replace_keeps_legacy_alias_in_sync(tmp_path):
    """Replacing the workspace list must keep the legacy single `workspace`
    alias consistent (first entry, or removed when the list empties)."""
    pid = _create_project()
    ws_a = tmp_path / "alias-a"
    ws_b = tmp_path / "alias-b"
    ws_a.mkdir()
    ws_b.mkdir()
    a, b = str(ws_a), str(ws_b)

    r1 = _post("/api/projects/bind", {"project_id": pid, "workspaces": [a, b]})
    assert r1.get("ok")
    assert r1["project"]["workspace"] == a, "alias must mirror first entry"

    r2 = _post("/api/projects/bind", {"project_id": pid, "workspaces": [b]})
    assert r2.get("ok")
    assert r2["project"]["workspace"] == b, "alias must follow replacement"

    r3 = _post("/api/projects/bind", {"project_id": pid, "workspaces": None})
    assert r3.get("ok")
    assert "workspace" not in r3["project"], "alias must vanish when cleared"










def _drive_bind(monkeypatch, project, body):
    """Drive ``/api/projects/bind`` in-process (no live server) and return
    ``(handled, captured_response, mutated_project)``.

    Only the persistence + trust lookups are stubbed so the route's own
    binding logic (the code under test) runs for real.
    """
    import api.routes as routes

    projects = [project]
    monkeypatch.setattr(routes, "load_projects", lambda: projects)
    monkeypatch.setattr(
        routes, "save_projects", lambda ps: projects.__setitem__(slice(None), ps)
    )
    # Never touch the host's real workspace registry.
    monkeypatch.setattr(routes, "load_workspaces", lambda: [])
    monkeypatch.setattr(routes, "save_workspaces", lambda wss: None)
    monkeypatch.setattr(
        routes, "resolve_trusted_workspace", lambda p, **_kw: Path(p)
    )
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_profiles_match", lambda a, b: True)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: dict(body))
    captured = {}
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload, status=status
        )
        or True,
    )
    handled = routes.handle_post(
        SimpleNamespace(command="POST"),
        SimpleNamespace(path="/api/projects/bind"),
    )
    return handled, captured, projects[0]


def test_bind_legacy_workspace_replaces_stale_default(tmp_path, monkeypatch):
    """Greptile P1 (#6836): a legacy ``workspace=C`` bind REPLACES the whole
    workspace set, so a ``default_workspace`` left over from the previous set
    must not survive. Keeping it violated "default ∈ workspaces" and made
    quick-create open a workspace the project no longer owned.
    """
    import api.routes as routes

    ws_a = tmp_path / "legacy-a"
    ws_b = tmp_path / "legacy-b"
    ws_a.mkdir()
    ws_b.mkdir()
    a, b = str(ws_a), str(ws_b)

    proj = {
        "project_id": "proj_legacy", "name": "l", "profile": "default",
        "workspace": a, "workspaces": [a], "default_workspace": a,
    }
    handled, captured, out = _drive_bind(
        monkeypatch, proj, {"project_id": "proj_legacy", "workspace": b}
    )
    assert handled is True
    assert captured["status"] == 200, captured
    assert [Path(p) for p in out["workspaces"]] == [ws_b]
    assert Path(out["workspace"]) == ws_b
    assert Path(out["default_workspace"]) == ws_b, (
        "the stale default must not outlive the replaced workspace set"
    )
    assert out["default_workspace"] in out["workspaces"], "default ∈ workspaces"
    # The user-visible symptom: quick-create must open the BOUND workspace.
    assert Path(routes._project_default_workspace(out)) == ws_b

    # An explicit default_workspace in the same payload still wins (the
    # legacy branch must not shadow it) and is auto-added to the list.
    out2 = _drive_bind(
        monkeypatch,
        dict(proj),
        {"project_id": "proj_legacy", "workspace": b, "default_workspace": a},
    )[2]
    assert Path(out2["default_workspace"]) == ws_a
    assert out2["default_workspace"] in out2["workspaces"]
    assert {Path(p) for p in out2["workspaces"]} == {ws_a, ws_b}


def test_bind_model_update_without_provider_clears_stale_provider(
    tmp_path, monkeypatch
):
    """Greptile P1 (#6836): changing a bound model without sending
    ``model_provider`` must not keep the provider bound to the OLD model — the
    stale pair routes quick-create to an incompatible backend.
    """
    base = {
        "project_id": "proj_model", "name": "m", "profile": "default",
        "model": "old-model", "model_provider": "old-provider",
    }

    # Model-only update → the stale provider is dropped (the new model then
    # inherits the profile/default route instead of an incompatible pair).
    out = _drive_bind(
        monkeypatch, dict(base), {"project_id": "proj_model", "model": "new-model"}
    )[2]
    assert out["model"] == "new-model"
    assert "model_provider" not in out, "stale provider must not survive a model swap"

    # An explicit provider in the same payload still wins.
    out = _drive_bind(
        monkeypatch,
        dict(base),
        {"project_id": "proj_model", "model": "new-model", "model_provider": "custom:test"},
    )[2]
    assert out["model"] == "new-model"
    assert out["model_provider"] == "custom:test"

    # An @-qualified model carries its own provider → store the canonical pair
    # (the route comment promised this; it was previously a no-op).
    out = _drive_bind(
        monkeypatch, dict(base), {"project_id": "proj_model", "model": "@openai:gpt-4o"}
    )[2]
    assert out["model"] == "gpt-4o"
    assert out["model_provider"] == "openai"

    # null still unbinds both halves of the pair.
    out = _drive_bind(
        monkeypatch, dict(base), {"project_id": "proj_model", "model": None}
    )[2]
    assert "model" not in out
    assert "model_provider" not in out


def test_bind_default_workspace_keeps_legacy_workspace(tmp_path, monkeypatch):
    """Greptile P1 (2026-10-07T06:37:09Z): a default_workspace update must not drop
    a legacy project's only workspace.

    A project loaded from a pre-migration projects.json carries just
    ``workspace: A`` (no ``workspaces`` list). Receiving
    ``default_workspace: B`` used to seed the bound list from
    ``proj.get("workspaces") or []``, so it stored only B and overwrote the
    compatibility alias — A vanished from quick-create and auto-assignment.
    """
    ws_a = tmp_path / "legacy-keep-a"
    ws_b = tmp_path / "legacy-keep-b"
    ws_a.mkdir()
    ws_b.mkdir()

    legacy = {
        "project_id": "proj_legacy_default",
        "name": "legacy",
        "profile": "default",
        "workspace": str(ws_a),
    }

    out = _drive_bind(
        monkeypatch,
        dict(legacy),
        {"project_id": "proj_legacy_default", "default_workspace": str(ws_b)},
    )[2]

    bound = {Path(p) for p in out.get("workspaces") or []}
    assert ws_a in bound, f"legacy workspace A must stay bound, got {bound}"
    assert ws_b in bound, f"the new default must be auto-added, got {bound}"
    assert Path(out["default_workspace"]) == ws_b
    assert Path(out["workspace"]) in bound, "the compatibility alias must stay bound"
    assert {Path(p) for p in _project_workspaces_for(out)} == {ws_a, ws_b}

    # And the project still resolves both workspaces for the sweep/quick-create.
    import api.routes as routes

    assert {Path(p) for p in routes._project_workspaces(out)} == {ws_a, ws_b}
    assert Path(routes._project_default_workspace(out)) == ws_b


def _project_workspaces_for(proj):
    import api.routes as routes

    return routes._project_workspaces(proj)


