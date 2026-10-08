"""Focused regressions for the PR #6836 re-gate review of 2026-10-08T02:11:02Z.

That review (id 5450591636, submitted against head ``129b127c``) is the one the
earlier watchdog rounds never saw: PR #6836 carries 32 reviews and the
un-paginated ``pulls/<n>/reviews`` call returns one page of 30. One test per
item, in the reviewer's order:

CORE 1  Escape with a combobox dropdown open must close ONLY the dropdown, not
        the whole Project settings dialog: the dialog's document-capture
        ``_onKey`` runs before the trigger's own keydown handler and used to
        close the dialog with unsaved edits.
SILENT 2 a preview from another profile must not be reusable as this profile's
        confirmation: the count is authorized against the project (404 for a
        project the active profile does not own, counted under that project's
        own profile) and the cached confirmation is keyed on
        profile + project + workspace snapshot.
SILENT 3 a dialog closed (Cancel / Escape) while the preview or the
        confirmation was in flight must neither prompt nor submit.
SILENT 4 the auto-assign sweep runs on a detached thread with no request
        profile context, so it must enter the project's own profile scope
        (``profile_scope_for_detached_worker``).
SHOULD 5 declining the confirmation must leave the STORED auto-assign flag
        untouched: the checkbox goes back to the saved value, never a hard
        false that a later Save would persist as "off".
"""

from pathlib import Path
from types import SimpleNamespace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_static(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / "static" / name).read_text(encoding="utf-8")


def _read_sessions_js() -> str:
    return _read_static("sessions.js")


def _read_routes_py() -> str:
    return (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text(encoding="utf-8")


def _dialog_source() -> str:
    """Only the _showProjectBindingsDialog body (up to the next top-level fn)."""
    src = _read_sessions_js()
    start = src.index("function _showProjectBindingsDialog(proj){")
    end = src.index("function _startProjectRename(proj, chip){")
    return src[start:end]


def _post_project_route(monkeypatch, path, body, responses):
    """Drive a project route in-process; each response lands in ``responses``."""
    import api.routes as routes

    def _record(payload, status):
        responses.append({"payload": payload, "status": status})
        return True

    monkeypatch.setattr(routes, "read_body", lambda handler: dict(body))
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None, **kw: _record(
            payload, status
        ),
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, msg, status=400: _record({"error": msg}, status),
    )
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    return routes.handle_post(
        SimpleNamespace(command="POST"), SimpleNamespace(path=path)
    )


def _strict_profile_match(a, b):
    """Real enough for the ownership gate: root/default alias, else equality."""
    return (a or "default") == (b or "default")


# ---------------------------------------------------------------------------
# [CORE] 1 — Escape with a combobox dropdown open must not close the dialog
# ---------------------------------------------------------------------------


def test_escape_with_an_open_combobox_closes_only_the_dropdown():
    """The dropdown owns Escape; the dialog (and unsaved edits) stay open.

    ``trigger.onkeydown`` handles Escape itself, but the dialog listener is
    installed on the document in the CAPTURE phase, so it runs FIRST — pressing
    Escape with the Model dropdown open closed the whole dialog. The dropdown is
    closed and the key consumed by the dialog's own handler instead.
    """
    seg = _dialog_source()
    on_key = seg.index("function _onKey(e){")
    guard = seg.index("if(e.defaultPrevented||_isAppDialogOpen()) return;", on_key)
    combo_branch = seg.index("if(e.key==='Escape'&&_closeOpenCombo()){", guard)
    dialog_close = seg.index("if(e.key==='Escape'){", guard)
    assert guard < combo_branch < dialog_close, (
        "the stack guard still runs first, then the dropdown claims Escape, "
        "then the dialog-close branch"
    )
    # The key is CONSUMED: the dialog-close path must not run as well.
    assert "e.preventDefault();e.stopPropagation();return;" in seg[combo_branch:dialog_close]

    # The helper closes exactly what the combo's own _close() closes, and only
    # reports True when a dropdown was actually open.
    helper = seg.index("function _closeOpenCombo(){")
    helper_seg = seg[helper:seg.index("closeBtn.onclick=", helper)]
    assert "overlay.querySelector('.project-bindings-combo-menu.open')" in helper_seg
    assert "if(!menu) return false;" in helper_seg
    assert "menu.classList.remove('open')" in helper_seg
    assert "trigger.classList.remove('open')" in helper_seg
    assert "trigger.setAttribute('aria-expanded','false')" in helper_seg
    assert "return true;" in helper_seg

    # ...the combo really does mark an open dropdown with those exact classes,
    # so the selector above matches a live dropdown (and nothing else).
    js = _read_sessions_js()
    assert "menu.classList.add('open');" in js
    assert "trigger.classList.add('open');" in js


# ---------------------------------------------------------------------------
# [SILENT] 2 — a foreign-profile preview cannot back this profile's confirmation
# ---------------------------------------------------------------------------


def test_auto_assign_preview_authorizes_the_project_and_counts_under_its_profile(monkeypatch):
    """The preview is a per-project read, not a caller-selectable one.

    It used to ignore ``project_id`` and count under the ACTIVE profile only, so
    profile B's dialog previewed (0 chats) for A's workspace list, cached
    "nothing to file", and A's chats were filed with no confirmation.
    """
    import api.routes as routes

    projects = [
        {"project_id": "proj_own", "name": "Own", "profile": "default", "workspaces": ["/ws/a"]},
        {"project_id": "proj_foreign", "name": "Foreign", "profile": "alpha", "workspaces": ["/ws/a"]},
    ]
    monkeypatch.setattr(routes, "load_projects", lambda *a, **k: [dict(p) for p in projects])
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_profiles_match", _strict_profile_match)
    calls = []
    monkeypatch.setattr(
        routes,
        "_auto_assign_candidate_count",
        lambda ws, profile=None: (calls.append((list(ws), profile)), 5)[1],
    )

    responses = []
    assert _post_project_route(
        monkeypatch,
        "/api/projects/auto-assign-preview",
        {"project_id": "proj_own", "workspaces": ["/ws/a"]},
        responses,
    ) is True
    assert [r["status"] for r in responses] == [200], responses
    assert responses[0]["payload"] == {"count": 5}, responses
    # The count runs under the PROJECT's own profile, never a caller-selected one.
    assert calls == [(["/ws/a"], "default")], calls

    # A project another profile owns is refused exactly like /api/projects/bind.
    responses = []
    _post_project_route(
        monkeypatch,
        "/api/projects/auto-assign-preview",
        {"project_id": "proj_foreign", "workspaces": ["/ws/a"]},
        responses,
    )
    assert [r["status"] for r in responses] == [404], responses
    assert calls == [(["/ws/a"], "default")], "a refused preview must not count anything"

    # An unknown project id is a 404 too — never a silent count.
    responses = []
    _post_project_route(
        monkeypatch,
        "/api/projects/auto-assign-preview",
        {"project_id": "proj_missing", "workspaces": ["/ws/a"]},
        responses,
    )
    assert [r["status"] for r in responses] == [404], responses
    assert calls == [(["/ws/a"], "default")], calls


def test_auto_assign_confirmation_is_bound_to_profile_project_and_snapshot():
    """The cached confirmation covers profile + project + the exact snapshot."""
    seg = _dialog_source()
    assert "const _wsKey=(paths)=>JSON.stringify([_dlgProfile(),proj.project_id,paths||[]]);" in seg
    # The active profile is read at call time (a switch re-keys the cache)...
    assert "? S.activeProfile.trim() : 'default');" in seg
    # ...the preview names the project it belongs to (the server authorizes it)...
    assert "body:JSON.stringify({project_id:proj.project_id, workspaces:wsPaths})," in seg
    # ...and the key is still compared on read, so nothing is silently reused.
    assert (
        "const _aaConfirmed=()=>_aaConfirmedKey!==null&&_aaConfirmedKey===_wsKey(_aaPathsNow());"
        in seg
    )
    assert "const _dlgProfile=()=>((typeof S!=='undefined'&&S&&typeof S.activeProfile==='string'&&S.activeProfile.trim())" in seg


# ---------------------------------------------------------------------------
# [SILENT] 3 — a closed dialog neither prompts nor submits
# ---------------------------------------------------------------------------


def test_closed_dialog_neither_prompts_nor_submits():
    """Save then Cancel while the (slow) preview is in flight must not bind."""
    seg = _dialog_source()
    gate = seg.index("const _ensureAutoAssignConfirmed=(wsPaths)=>{")
    save = seg.index("saveBtn.onclick=async()=>{")
    post = seg.index("await _saveProjectBindings(proj,fields);", save)

    preview = seg.index("const count=await _autoAssignCount(wsPaths);", gate)
    after_preview = seg.index("if(_closed) return false;", preview)
    confirm = seg.index("const ok=await showConfirmDialog({", after_preview)
    after_confirm = seg.index("if(_closed) return false;", confirm)
    assert preview < after_preview < confirm < after_confirm
    # A closed dialog must not cache an answer either.
    assert after_confirm < seg.index("if(ok) _aaConfirmedKey=key;", after_confirm)

    # Save: the check sits between the awaited gate and the only POST.
    awaited = seg.index("const ok=await _ensureAutoAssignConfirmed(wsPaths);", save)
    save_check = seg.index("if(_closed) return;", awaited)
    assert awaited < save_check < post
    assert "fields.auto_assign=autoAssign;" in seg[save_check:post]

    # The toggle path drops out the same way when the dialog is closed mid-prompt.
    toggle = seg.index("aaCb.onchange=async()=>{")
    assert "if(_closed) return;" in seg[toggle:save]


# ---------------------------------------------------------------------------
# [SILENT] 4 — the sweep worker runs inside the project's own profile scope
# ---------------------------------------------------------------------------


def _bind_route_stubs(monkeypatch, projects, *, active_profile, register_sink):
    """Stub the catalog/trust lookups and capture the spawned sweep thread."""
    import api.routes as routes
    import api.session_lifecycle as sl

    monkeypatch.setattr(routes, "load_projects", lambda *a, **k: [dict(p) for p in projects])
    monkeypatch.setattr(
        routes,
        "save_projects",
        lambda ps: projects.__setitem__(slice(None), [dict(p) for p in ps]),
    )
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: active_profile)
    monkeypatch.setattr(routes, "_profiles_match", _strict_profile_match)
    monkeypatch.setattr(routes, "load_workspaces", lambda: [])
    monkeypatch.setattr(routes, "save_workspaces", lambda wss: None)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda p, **_kw: Path(p))
    monkeypatch.setattr(
        sl,
        "_register_background_commit_thread",
        lambda t: (register_sink.append(t), True)[1],
    )
    monkeypatch.setattr(sl, "_unregister_background_commit_thread", lambda t: None)


def _run_bind_capture_sweep_profile(monkeypatch, tmp_path, *, project_profile, active_profile):
    """POST /api/projects/bind for an auto-assign project; report the sweep's profile."""
    import api.profiles as profiles
    import api.routes as routes

    ws = tmp_path / f"ws-{project_profile}"
    ws.mkdir(exist_ok=True)
    pid = f"proj_regate_scope_{project_profile}"
    projects = [
        {
            "project_id": pid,
            "name": "Scoped",
            "profile": project_profile,
            "workspaces": [str(ws)],
            "auto_assign": True,
        }
    ]
    seen = []
    threads = []
    _bind_route_stubs(
        monkeypatch, projects, active_profile=active_profile, register_sink=threads
    )
    # Stand in for the sweep body: what matters is the profile context it sees.
    monkeypatch.setattr(
        routes,
        "_apply_project_auto_assign",
        lambda proj: (seen.append(profiles.get_active_profile_name()), 0)[1],
    )

    responses = []
    assert _post_project_route(
        monkeypatch, "/api/projects/bind", {"project_id": pid, "auto_assign": True}, responses
    ) is True
    assert [r["status"] for r in responses] == [200], responses
    assert len(threads) == 1, "the bind must admit exactly one sweep worker"
    threads[0].join(5)
    assert not threads[0].is_alive(), "the sweep worker must finish"
    return seen


def test_auto_assign_sweep_worker_enters_the_projects_own_profile_scope(monkeypatch, tmp_path):
    """A detached worker inherits no request profile; it must set its own.

    Without the scope a NAMED-profile project's sweep resolved the DEFAULT
    profile — it read that profile's store and filed rows the profile boundary
    (and /api/session/move) refuses to touch.
    """
    src = _read_routes_py()
    i = src.index("def _file_existing_sessions(")
    worker_seg = src[i : src.index("t = threading.Thread(", i)]
    assert "profile_scope_for_detached_worker(" in worker_seg
    assert '"project auto-assign"' in worker_seg
    assert worker_seg.index("with profile_scope_for_detached_worker(") < worker_seg.index(
        "_apply_project_auto_assign(target)"
    )
    assert 'or "default"' in worker_seg

    assert _run_bind_capture_sweep_profile(
        monkeypatch, tmp_path, project_profile="alpha", active_profile="alpha"
    ) == ["alpha"]

    # A default-profile project keeps reporting default (the scope is a no-op there).
    assert _run_bind_capture_sweep_profile(
        monkeypatch, tmp_path, project_profile="default", active_profile="default"
    ) == ["default"]


# ---------------------------------------------------------------------------
# [SHOULD-FIX] 5 — declining leaves the STORED auto-assign value alone
# ---------------------------------------------------------------------------


def test_declining_the_confirmation_leaves_the_stored_flag_alone():
    """Decline puts the box back to the SAVED value, never a hard false.

    On a project whose auto-assign is already on, Save with nothing changed still
    prompts (a fresh dialog has no cached confirmation). Declining used to uncheck
    the box, and the next Save then persisted ``auto_assign: false`` — silently
    turning the stored flag off.
    """
    seg = _dialog_source()
    assert "aaCb.checked=false;" not in seg
    # The box is restored from the project's stored flag in BOTH decline paths.
    assert "if(!confirmed) aaCb.checked=!!proj.auto_assign;" in seg
    save = seg.index("saveBtn.onclick=async()=>{")
    post = seg.index("await _saveProjectBindings(proj,fields);", save)
    decline_open = seg.index("if(!ok){", save)
    decline = seg.index("aaCb.checked=!!proj.auto_assign;", decline_open)
    assert decline_open < decline < post
    assert "return;" in seg[decline:post]
