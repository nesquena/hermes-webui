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


def test_auto_assign_project_for_workspace(tmp_path, monkeypatch):
    """_auto_assign_project_for_workspace picks the owning project in list order."""
    import api.routes as routes

    pid1 = _create_project()
    pid2 = _create_project()
    ws = tmp_path / "shared"
    ws.mkdir()
    ws_str = str(ws)
    _post("/api/projects/bind", {
        "project_id": pid1,
        "workspaces": [ws_str],
        "auto_assign": True,
    })
    _post("/api/projects/bind", {
        "project_id": pid2,
        "workspaces": [ws_str],
        "auto_assign": True,
    })

    # First match in on-disk list order wins; reload from disk to avoid cache.
    assert routes._auto_assign_project_for_workspace(ws_str) in (pid1, pid2)

    # Unclaimed workspace → None
    assert routes._auto_assign_project_for_workspace("Z:/not/claimed") is None
    # Disabled flag → None
    _post("/api/projects/bind", {"project_id": pid1, "auto_assign": False})
    _post("/api/projects/bind", {"project_id": pid2, "auto_assign": False})
    assert routes._auto_assign_project_for_workspace(ws_str) is None


def test_auto_assign_project_omitted_profile_resolves_to_active(monkeypatch):
    """Greptile P1 (#6836): an omitted ``profile`` must resolve to the ACTIVE
    profile (same rule as ``new_session``), never to None-as-default.

    Otherwise a named-profile caller that omits ``profile`` could have its
    session auto-assigned to a default-profile project (or vice versa) —
    the auto-assign matcher and the session creator disagree on the
    effective profile.
    """
    import api.routes as routes

    ws = "C:/Users/Admin/workspace"
    default_proj = {
        "project_id": "proj_default", "name": "d",
        "profile": "default", "workspaces": [ws], "auto_assign": True,
    }
    named_proj = {
        "project_id": "proj_work", "name": "w",
        "profile": "work", "workspaces": [ws], "auto_assign": True,
    }
    monkeypatch.setattr(routes, "load_projects", lambda: [default_proj, named_proj])

    # Active profile = default: omitted profile resolves to default → the
    # default project must win, NOT the named one.
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    assert routes._auto_assign_project_for_workspace(ws) == "proj_default"
    # Explicit named profile → the named project wins.
    assert routes._auto_assign_project_for_workspace(ws, profile="work") == "proj_work"

    # Active profile = named (e.g. caller is operating inside profile "work"):
    # an omitted profile must resolve to "work", so the named project wins and
    # the default project is NOT matched (cross-profile pollution).
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "work")
    assert routes._auto_assign_project_for_workspace(ws) == "proj_work"

    # The pre-fix bug: with profile=None (unpatched helper), the default
    # project used to be returned even when the ACTIVE profile is a named one
    # (condition `p.get("profile") and profile` short-circuited). Regression
    # guard: an explicit None must behave identically to the active profile.
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "work")
    assert routes._auto_assign_project_for_workspace(ws, profile=None) == "proj_work"


def test_apply_project_auto_assign_files_existing_sessions(tmp_path, monkeypatch):
    """_apply_project_auto_assign re-files existing sessions whose workspace
    is bound, skipping cross-profile rows and already-owned sessions."""
    import api.routes as routes

    # Point the index at a temp file with three sessions: two in the bound
    # workspace, one in another workspace.
    ws = tmp_path / "bound-ws"
    ws.mkdir()
    ws_str = str(ws)
    index_file = tmp_path / "_index.json"
    index_file.write_text(json.dumps([
        {"session_id": "sess_aaa", "workspace": ws_str, "profile": "default",
         "project_id": None, "message_count": 3},
        {"session_id": "sess_bbb", "workspace": ws_str, "profile": "default",
         "project_id": "already-owned", "message_count": 1},
        {"session_id": "sess_ccc", "workspace": HOME_WS,
         "profile": "default", "project_id": None, "message_count": 2},
        {"session_id": "sess_ddd", "workspace": ws_str, "profile": "other",
         "project_id": None, "message_count": 4},
    ]))
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    # Deterministic ownership probe: "" = state.db is readable and holds no
    # such row. Without this stub the sweep fails closed whenever the machine
    # has no state.db (Greptile P1 2026-10-10T01:04:32Z).
    monkeypatch.setattr(routes, "_state_db_session_source_strict", lambda sid: "")

    saved = {}
    class _FakeSession:
        def __init__(self, sid):
            self.session_id = sid
            self.project_id = None
            # Live row must carry the same profile/workspace the index entry
            # had, otherwise the new authoritative recheck under lock would
            # (correctly) reject it. Keep the fake minimal but realistic.
            self.profile = "other" if sid == "sess_ddd" else "default"
            self.workspace = HOME_WS if sid == "sess_ccc" else ws_str
        def save(self, touch_updated_at=True):
            saved[self.session_id] = self.project_id

    def _fake_get_session(sid, metadata_only=False):  # noqa: ARG001 — signature mirrors real get_session
        if sid not in ("sess_aaa", "sess_bbb", "sess_ccc", "sess_ddd"):
            return None
        return _FakeSession(sid)

    monkeypatch.setattr(routes, "get_session", _fake_get_session)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    proj = {"project_id": "proj_xyz", "profile": "default",
            "workspaces": [ws_str], "auto_assign": True}
    # The sweep re-reads the LIVE project row before filing anything (Greptile
    # P1 2026-10-10T02:22:51Z). This test drives it from an in-memory snapshot
    # with no projects catalog on disk, so pin the unchanged-bindings answer.
    monkeypatch.setattr(
        routes, "_auto_assign_live_binding", lambda pid: (True, {ws_str})
    )
    changed = routes._apply_project_auto_assign(proj)

    # sess_aaa: bound ws, unowned → re-filed.
    assert saved.get("sess_aaa") == "proj_xyz"
    # sess_bbb: already owned by another project → untouched.
    assert "sess_bbb" not in saved
    # sess_ccc: different workspace → untouched.
    assert "sess_ccc" not in saved
    # sess_ddd: other profile → untouched.
    assert "sess_ddd" not in saved
    assert changed == 1


def test_apply_project_auto_assign_named_profile_never_sweeps_default(tmp_path, monkeypatch):
    """A NAMED-profile project must not re-file default/unprofiled sessions —
    they would end up tagged with a foreign project_id."""
    import api.routes as routes

    ws = tmp_path / "named-ws"
    ws.mkdir()
    ws_str = str(ws)
    index_file = tmp_path / "_index.json"
    index_file.write_text(json.dumps([
        {"session_id": "sess_def", "workspace": ws_str, "profile": "default",
         "project_id": None, "message_count": 1},
        {"session_id": "sess_none", "workspace": ws_str, "profile": None,
         "project_id": None, "message_count": 1},
        {"session_id": "sess_haku", "workspace": ws_str, "profile": "haku",
         "project_id": None, "message_count": 1},
    ]))
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    # Deterministic ownership probe: "" = state.db is readable and holds no
    # such row. Without this stub the sweep fails closed whenever the machine
    # has no state.db (Greptile P1 2026-10-10T01:04:32Z).
    monkeypatch.setattr(routes, "_state_db_session_source_strict", lambda sid: "")

    saved = {}
    class _FakeSession:
        def __init__(self, sid):
            self.session_id = sid
            self.project_id = None
            # Mirror the index row's profile/workspace so the new
            # live-row check under lock does not (correctly) reject
            # the fake. None profile coalesces to "default" in prod.
            self.profile = {"sess_def": "default", "sess_none": None, "sess_haku": "haku"}[sid]
            self.workspace = ws_str
        def save(self, touch_updated_at=True):
            saved[self.session_id] = self.project_id

    def _fake_get_session_named(sid, metadata_only=False):  # noqa: ARG001
        if sid not in ("sess_def", "sess_none", "sess_haku"):
            return None
        return _FakeSession(sid)

    monkeypatch.setattr(routes, "get_session", _fake_get_session_named)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    proj = {"project_id": "proj_named", "profile": "haku",
            "workspaces": [ws_str], "auto_assign": True}
    # The sweep re-reads the LIVE project row before filing anything (Greptile
    # P1 2026-10-10T02:22:51Z). This test drives it from an in-memory snapshot
    # with no projects catalog on disk, so pin the unchanged-bindings answer.
    monkeypatch.setattr(
        routes, "_auto_assign_live_binding", lambda pid: (True, {ws_str})
    )
    changed = routes._apply_project_auto_assign(proj)

    assert saved.get("sess_haku") == "proj_named"
    assert "sess_def" not in saved, "named project must not sweep default sessions"
    assert "sess_none" not in saved, "named project must not sweep unprofiled sessions"
    assert changed == 1


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


def test_bind_auto_assign_sweeps_a_legacy_workspace_project(tmp_path, monkeypatch):
    """Greptile P1 (2026-10-07T08:10:26Z): turning auto_assign on for a LEGACY
    project (only ``workspace: A``) must still run the historical sweep.

    Enabling it with ``default_workspace: A`` leaves the ``workspaces`` field
    absent (the default is already the legacy workspace, so nothing is
    auto-added), and the launch guard read exactly that absent field — so
    existing unowned sessions in A were never filed, while future sessions were
    (that path uses ``_project_workspaces``).
    """
    import threading

    import api.session_lifecycle as lifecycle
    import api.routes as routes

    ws_a = tmp_path / "legacy-sweep-a"
    ws_a.mkdir()
    legacy = {
        "project_id": "proj_legacy_sweep",
        "name": "legacy-sweep",
        "profile": "default",
        "workspace": str(ws_a),
    }

    ran = threading.Event()
    seen = []

    def _record(proj):
        seen.append(dict(proj))
        ran.set()
        return 0

    monkeypatch.setattr(routes, "_apply_project_auto_assign", _record)
    # Never touch the process-wide drain registry from a test.
    monkeypatch.setattr(
        lifecycle, "_register_background_commit_thread", lambda _t: True
    )

    handled, captured, out = _drive_bind(
        monkeypatch,
        dict(legacy),
        {
            "project_id": "proj_legacy_sweep",
            "auto_assign": True,
            "default_workspace": str(ws_a),
        },
    )
    assert handled is True
    assert captured["status"] == 200, captured
    assert out.get("auto_assign") is True
    assert "workspaces" not in out, (
        "precondition: the default is the legacy workspace, so no multi-value "
        "field is materialised"
    )
    assert ran.wait(5), "the historical sweep must run for a legacy-workspace project"
    assert seen and seen[0]["project_id"] == "proj_legacy_sweep"
