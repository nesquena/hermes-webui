"""Focused regressions for the PR #6836 re-gate review.

Review submitted 2026-10-07T06:02:27Z against head ``da43ec25``. This module pins
each finding from that review, one test per item:

CORE 1  provider bindings lost: the dialog must read an option's provider from
        the authoritative helper (dataset OR inherited ``<optgroup>``), so a
        canonicalized binding such as ``@custom:backup:model-a:free`` re-opens
        with the right ``model_provider`` instead of null.
CORE 2  an explicit workspace switch must beat a project's bound default.
CORE 3  bindings must never be merged/forwarded across a profile switch.
CORE 4  deleting a project must refuse new backfill sweeps and cancel + join the
        in-flight ones BEFORE unlinking (no sessions orphaned under a dead id).
SILENT  the backfill must not rewrite historical ``updated_at``.
SHOULD  the bind worker must self-unregister from the drain registry; the chip
        menu must unbind only the clicked workspace; the added UI strings need
        i18n keys in every one of the 15 locales.
"""

import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_static(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / "static" / name).read_text(encoding="utf-8")


def _read_sessions_js() -> str:
    return _read_static("sessions.js")


def _read_routes_py() -> str:
    return (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text(encoding="utf-8")


PB_I18N_KEYS = (
    "pb_bindings_title",
    "pb_bindings_menu",
    "pb_bindings_menu_bound",
    "pb_ws_summary",
    "pb_chip_model",
    "pb_chip_effort",
    "pb_chip_auto",
    "pb_field_workspaces",
    "pb_field_model",
    "pb_field_effort",
    "pb_none_inherit",
    "pb_no_workspaces",
    "pb_no_options",
    "pb_mark_default",
    "pb_set_default",
    "pb_set_default_title",
    "pb_unbind_ws_title",
    "pb_add_workspace_placeholder",
    "pb_type_path",
    "pb_enter_ws_path",
    "pb_add",
    "pb_add_workspace_title",
    "pb_add_workspace_message",
    "pb_workspace_already_bound",
    "pb_auto_assign_label",
    "pb_auto_assign_hint",
    "pb_cancel",
    "pb_save",
    "pb_updated",
    "pb_update_failed",
    "pb_unbind_workspace_named",
    "pb_unbind_model",
    "pb_unbind_effort",
    "pb_effort_minimal",
    "pb_effort_low",
    "pb_effort_medium",
    "pb_effort_high",
    "pb_effort_xhigh",
    "pb_effort_max",
)

# LOCALES block order in static/i18n.js (15 blocks).
I18N_LOCALES = (
    "en", "it", "ja", "ru", "es", "de", "zh", "zh-Hant",
    "pt", "ko", "fr", "cs", "tr", "pl", "vi",
)


def _i18n_locale_chunk(src: str, loc: str) -> str:
    """Slice of i18n.js from ``loc``'s opening line up to the next locale block."""
    head = ("\n  '%s': {\n" % loc) if "-" in loc else ("\n  %s: {\n" % loc)
    i = src.index(head) + len(head)
    idx = I18N_LOCALES.index(loc)
    if idx + 1 < len(I18N_LOCALES):
        nxt = I18N_LOCALES[idx + 1]
        nhead = ("\n  '%s': {\n" % nxt) if "-" in nxt else ("\n  %s: {\n" % nxt)
        j = src.index(nhead, i)
    else:
        j = src.index("const _I18N_TOOL_ACTION_TEXT_EN", i)
    return src[i:j]


# ---------------------------------------------------------------------------
# CORE 1 — provider bindings are lost on re-open
# ---------------------------------------------------------------------------


def test_bindings_dialog_resolves_provider_via_optgroup_helper():
    """Catalog options inherit their provider from <optgroup>; read it properly."""
    src = _read_sessions_js()
    # Helper exists and delegates to the ui.js authority.
    assert "const _optProviderId=(o)=>{" in src
    assert "if(typeof _getOptionProviderId==='function') return _getOptionProviderId(o)||'';" in src
    # Option collection uses it, never a bare dataset read.
    assert "const provider=_optProviderId(o);" in src
    assert "const provider=(o.dataset&&o.dataset.provider)||'';" not in src
    # Restoration matches on the canonical (model, provider) IDENTITY, not on
    # the raw option VALUE: the server canonicalizes
    # '@custom:backup:model-a:free' to model 'model-a:free' + provider
    # 'custom:backup', so value equality never hit and the dialog reopened on
    # inherit-default with the provider dropped on save (re-gate 2026-10-07).
    assert "o._modelId=(st&&st.model)||o.value;" in src
    assert "o._providerId=_optProviderId(o)||(st&&st.model_provider)||o.sub||'';" in src
    assert "x.value&&x._modelId===wantModel&&String(x._providerId||'')===wantProv" in src
    assert "const _initialModelKey=_bindingModelKeyFor(proj, modelOptions, {" in src
    # A saved pair that is missing from the current catalog is re-injected so
    # it stays selectable AND re-savable instead of being silently cleared.
    assert "_saved:true," in src
    # Saving never persists null for a provider-qualified model id.
    assert "_prov=_getOptionProviderId({value:_bare})||null;" in src


def test_bindings_dialog_provider_falls_back_to_qualified_model_id():
    """A '@custom:<slug>:<model>' id must still yield its provider when unsaved."""
    src = _read_sessions_js()
    # The fallback runs only after both option-based lookups came up empty.
    save_i = src.index("if(modelVal){")
    save_seg = src[save_i:save_i + 1600]
    assert "_prov=_getOptionProviderId({value:_bare})||null;" in save_seg
    assert "fields.model_provider=_prov;" in save_seg


# ---------------------------------------------------------------------------
# CORE 2 — an explicit workspace switch is overridden by the binding
# ---------------------------------------------------------------------------


def test_workspace_switch_new_chat_passes_explicit_workspace():
    """The switch-to-different-workspace new-chat must pass {workspace: path}."""
    src = _read_static("panels.js")
    assert "await newSession(false,{workspace:path});" in src
    # The old flag-only call (which let a project binding win) must be gone.
    assert "S._profileSwitchWorkspace=path;\n    if(typeof newSession==='function') await newSession(false);" not in src


# ---------------------------------------------------------------------------
# CORE 3 — a profile switch uses the previous profile's bindings
# ---------------------------------------------------------------------------


def test_project_bindings_require_active_profile_at_both_sites():
    """Both cited sites gate on _profileMatchesActiveProfile."""
    src = _read_sessions_js()
    assert src.count("_profileMatchesActiveProfile(_proj.profile,S.activeProfile)") == 1
    assert "!_profileMatchesActiveProfile(project.profile, S.activeProfile)" in src
    i = src.index("function _projectBindingsForNewSession(project){")
    seg = src[i:i + 1600]
    assert "return o;" in seg, "mismatched profile must yield no bindings"


# ---------------------------------------------------------------------------
# SHOULD-FIX — chip menu unbinds everything
# ---------------------------------------------------------------------------


def test_chip_menu_unbinds_only_the_clicked_workspace():
    """One item per bound workspace; posts the remaining list, not workspace:null."""
    src = _read_sessions_js()
    assert "const remaining=_boundWs.filter(p=>p!==wsPath);" in src
    assert "_saveProjectBindings(proj, remaining.length?{workspaces:remaining}:{workspaces:null});" in src
    assert "_unbindItem('workspace','workspace')" not in src


# ---------------------------------------------------------------------------
# SHOULD-FIX — localization of the added 589 lines
# ---------------------------------------------------------------------------


def test_project_bindings_ui_strings_are_localized():
    """No hard-coded English literals left in the new dialog/menu code."""
    src = _read_sessions_js()
    for literal in (
        "'Bindings — '",
        "'No workspaces bound'",
        "'Auto-assign sessions by workspace'",
        "'Set default'",
        "'Add workspace'",
        "'(none) — inherit default'",
        "'Project bindings updated'",
        "'Binding update failed: '",
        "'Add workspace…'",
        "'Unbind '",
    ):
        assert literal not in src, f"hard-coded string still present: {literal}"
    assert "t('pb_bindings_title',proj.name)" in src
    assert "t('pb_add_workspace_message')" in src
    assert "t('pb_auto_assign_hint')" in src


def test_project_bindings_i18n_keys_present_in_all_locales():
    """Every locale block defines the full pb_* key set (i18n.js parity)."""
    src = _read_static("i18n.js")
    assert len(I18N_LOCALES) == 15
    for loc in I18N_LOCALES:
        chunk = _i18n_locale_chunk(src, loc)
        missing = [k for k in PB_I18N_KEYS if ("%s: '" % k) not in chunk]
        assert not missing, f"locale {loc!r} is missing pb_* keys: {missing}"

    # Non-English sanity: the long auto-assign sentence must be translated.
    en_hint = "'All existing and future sessions in the bound workspaces are filed under this project.'"
    for loc in I18N_LOCALES:
        if loc == "en":
            continue
        assert en_hint not in _i18n_locale_chunk(src, loc), (
            f"locale {loc!r} leaks the English auto-assign hint"
        )


# ---------------------------------------------------------------------------
# SILENT — backfill rewrites historical activity dates
# ---------------------------------------------------------------------------


def test_apply_project_auto_assign_preserves_updated_at(tmp_path, monkeypatch):
    """Filing a session must not bump updated_at (imported rows jump to Today)."""
    import api.routes as routes

    ws = tmp_path / "ws-touch"
    ws.mkdir()
    ws_str = str(ws)
    index_file = tmp_path / "_index.json"
    index_file.write_text(json.dumps([
        {"session_id": "s_touch", "workspace": ws_str, "profile": "default", "project_id": None},
    ]))
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    calls = []

    class _Row:
        def __init__(self):
            self.session_id = "s_touch"
            self.project_id = None
            self.profile = "default"
            self.workspace = ws_str

        def save(self, touch_updated_at=True):
            calls.append(touch_updated_at)

    row = _Row()

    def _fake_get_session(sid, metadata_only=False):
        if sid != "s_touch":
            return None
        return None if metadata_only else row

    monkeypatch.setattr(routes, "get_session", _fake_get_session)

    marker = "proj_touch"
    try:
        assert routes._apply_project_auto_assign(
            {"project_id": marker, "profile": "default", "workspaces": [ws_str]}
        ) == 1
    finally:
        routes._auto_assign_finish_deleting(marker)
    assert row.project_id == marker
    assert calls == [False], f"save must not touch updated_at, got {calls}"


# ---------------------------------------------------------------------------
# CORE 4 — deleting a project during backfill orphans sessions
# ---------------------------------------------------------------------------


def test_auto_assign_sweep_admission_refused_while_deleting():
    """A project being deleted refuses new sweeps; the marker is released after."""
    import api.routes as routes

    pid = "proj_delete_serial_1"
    assert routes._auto_assign_sweep_begin(pid) is True
    routes._auto_assign_sweep_end(pid)
    assert pid not in routes._AUTO_ASSIGN_DELETING

    routes._AUTO_ASSIGN_DELETING.add(pid)
    try:
        assert routes._auto_assign_sweep_begin(pid) is False
        assert routes._auto_assign_sweep_cancelled(pid) is True
    finally:
        routes._auto_assign_finish_deleting(pid)

    assert routes._auto_assign_sweep_cancelled(pid) is False
    assert routes._auto_assign_sweep_begin(pid) is True
    routes._auto_assign_sweep_end(pid)
    assert pid not in routes._AUTO_ASSIGN_SWEEPS


def test_apply_project_auto_assign_refuses_while_project_deleting(tmp_path, monkeypatch):
    """A sweep admitted after deletion started must not touch any session."""
    import api.routes as routes

    ws = tmp_path / "ws-del-refuse"
    ws.mkdir()
    ws_str = str(ws)
    index_file = tmp_path / "_index.json"
    index_file.write_text(json.dumps([
        {"session_id": "s_refuse", "workspace": ws_str, "profile": "default", "project_id": None},
    ]))
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    def _boom(*a, **k):
        raise AssertionError("sweep must not read/write sessions while deleting")

    monkeypatch.setattr(routes, "get_session", _boom)

    pid = "proj_delete_serial_2"
    routes._AUTO_ASSIGN_DELETING.add(pid)
    try:
        assert routes._apply_project_auto_assign({"project_id": pid, "workspaces": [ws_str]}) == 0
    finally:
        routes._auto_assign_finish_deleting(pid)


def test_auto_assign_sweep_stops_when_cancelled_midway(tmp_path, monkeypatch):
    """A delete landing mid-sweep stops it: no further project_id is written."""
    import api.routes as routes

    ws = tmp_path / "ws-del-stop"
    ws.mkdir()
    ws_str = str(ws)
    index_file = tmp_path / "_index.json"
    index_file.write_text(json.dumps([
        {"session_id": f"s{i}", "workspace": ws_str, "profile": "default", "project_id": None}
        for i in range(5)
    ]))
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    pid = "proj_delete_serial_3"
    saved = []

    class _Row:
        def __init__(self, sid):
            self.session_id = sid
            self.project_id = None
            self.profile = "default"
            self.workspace = ws_str

        def save(self, touch_updated_at=True):
            saved.append(self.session_id)
            # The concurrent /api/projects/delete lands right here.
            routes._AUTO_ASSIGN_DELETING.add(pid)

    rows = {f"s{i}": _Row(f"s{i}") for i in range(5)}

    def _fake_get_session(sid, metadata_only=False):
        return None if metadata_only else rows.get(sid)

    monkeypatch.setattr(routes, "get_session", _fake_get_session)

    try:
        changed = routes._apply_project_auto_assign(
            {"project_id": pid, "workspaces": [ws_str], "profile": "default"}
        )
    finally:
        routes._auto_assign_finish_deleting(pid)
    assert changed == 1, f"sweep must stop at the cancellation point, filed {changed}"
    assert saved == ["s0"]
    assert pid not in routes._AUTO_ASSIGN_SWEEPS


def test_auto_assign_cancel_sweeps_joins_an_inflight_sweep():
    """_auto_assign_cancel_sweeps marks deleting and joins the registered thread."""
    import api.routes as routes

    pid = "proj_delete_serial_4"
    started = threading.Event()

    def _sweep():
        assert routes._auto_assign_sweep_begin(pid) is True
        started.set()
        try:
            # Mimics the sweep loop: spin until cancellation, then return.
            for _ in range(1000):
                if routes._auto_assign_sweep_cancelled(pid):
                    break
                threading.Event().wait(0.01)
        finally:
            routes._auto_assign_sweep_end(pid)

    t = threading.Thread(target=_sweep, daemon=True)
    t.start()
    assert started.wait(5)
    try:
        assert pid in routes._AUTO_ASSIGN_SWEEPS
        routes._auto_assign_cancel_sweeps(pid)
        t.join(5)
        assert not t.is_alive(), "cancel_sweeps must join the in-flight sweep"
    finally:
        routes._auto_assign_finish_deleting(pid)
    assert pid not in routes._AUTO_ASSIGN_SWEEPS


def test_delete_endpoint_cancels_sweeps_before_removing_project():
    """The delete handler cancels + joins sweeps before it unlinks sessions."""
    src = _read_routes_py()
    i = src.index('parsed.path == "/api/projects/delete"')
    seg = src[i:i + 9000]
    cancel_i = seg.index('_auto_assign_cancel_sweeps(body["project_id"])')
    save_i = seg.index("save_projects(projects)")
    assert cancel_i < save_i, "cancellation must precede project removal"
    assert '_auto_assign_finish_deleting(body["project_id"])' in seg
    # A drain that fails must refuse the deletion entirely (503) instead of
    # removing a project whose sweep can still write project_id (re-gate
    # 2026-10-07, finding 3).
    assert 'if not _auto_assign_cancel_sweeps(body["project_id"]):' in seg
    assert '_auto_assign_abort_deleting(body["project_id"])' in seg
    assert "503" in seg
    # The catalog is RELOADED after the drain, inside the shared mutation lock:
    # saving the list read before the join erased a project created meanwhile
    # (re-gate 2026-10-07, finding 4).
    lock_i = seg.index("with _PROJECTS_CATALOG_LOCK:")
    assert cancel_i < lock_i < save_i, "reload must follow the drain"
    assert "p for p in load_projects()" in seg


# ---------------------------------------------------------------------------
# SHOULD-FIX — drain-registry leak
# ---------------------------------------------------------------------------


def test_bind_auto_assign_worker_self_unregisters():
    """The auto-assign bind worker must not leak a dead Thread in the registry."""
    src = _read_routes_py()
    i = src.index("def _file_existing_sessions(")
    seg = src[i:i + 1600]
    assert "finally:" in seg
    assert "_unregister_background_commit_thread(threading.current_thread())" in seg

    # The pattern is effective: a registered thread drops out once it finishes.
    import api.session_lifecycle as sl

    done = threading.Event()

    def _worker():
        try:
            pass
        finally:
            sl._unregister_background_commit_thread(threading.current_thread())
            done.set()

    t = threading.Thread(target=_worker, daemon=True)
    assert sl._register_background_commit_thread(t) is True
    assert t in sl._background_commit_threads
    t.start()
    assert done.wait(5)
    t.join(5)
    assert t not in sl._background_commit_threads


def test_backfill_uses_touch_updated_at_false_call_site():
    """The sweep body must call save(touch_updated_at=False)."""
    src = _read_routes_py()
    assert "s.save(touch_updated_at=False)" in src


# ---------------------------------------------------------------------------
# greptile re-review P1 (2026-10-07T06:37:09Z) — default_workspace drops the
# legacy workspace on a project that only carries `workspace: A`.
# ---------------------------------------------------------------------------


def test_default_workspace_update_seeds_from_canonical_workspace_accessor():
    """The default_workspace branch must seed the bound list canonically."""
    src = _read_routes_py()
    i = src.index('if "default_workspace" in body:')
    seg = src[i:i + 2000]
    assert "ws_list = _project_workspaces(proj)" in seg
    assert 'ws_list = proj.get("workspaces") or []' not in seg


def test_project_workspaces_falls_back_to_legacy_single_workspace():
    """_project_workspaces must surface a legacy `workspace: A` as the bound set."""
    import api.routes as routes

    assert routes._project_workspaces({"workspace": "/ws/A"}) == ["/ws/A"]
    assert routes._project_workspaces({"workspaces": ["/ws/A", "/ws/B"]}) == ["/ws/A", "/ws/B"]
    # A list wins over the stale alias; empty/absent yields nothing.
    assert routes._project_workspaces({"workspaces": ["/ws/B"], "workspace": "/ws/A"}) == ["/ws/B"]
    assert routes._project_workspaces({}) == []
    assert routes._project_workspaces(None) == []


def test_auto_assign_launch_guard_uses_canonical_workspace_accessor():
    """The bind handler must launch the sweep for a legacy `workspace: A` project."""
    src = _read_routes_py()
    assert 'if proj.get("auto_assign") and _project_workspaces(proj):' in src
    assert 'if proj.get("auto_assign") and proj.get("workspaces"):' not in src


# ---------------------------------------------------------------------------
# Re-gate 2026-10-07T10:29:19Z — the three backend findings are one race
# between deletion and the sweep lifecycle, seen from three orderings:
#
#   (a) "worker admitted late"      — a delete that lands between bind starting
#                                     its worker and the worker's own admission;
#   (b) "worker stuck past the join"— a sweep that outlives the join timeout;
#   (c) "create during the drain"   — the catalog saved after the join is the
#                                     stale list read before it.
#
# One ordering per test, each deterministic.
# ---------------------------------------------------------------------------


def _install_project_route_stubs(monkeypatch, projects, index_path=None):
    """Stub only persistence/trust lookups so the route logic runs for real.

    ``load_projects`` returns a FRESH copy per call, exactly like the
    disk-backed reader, so a handler's early read really is a stale snapshot;
    ``save_projects`` writes back into ``projects`` in place.
    """
    import api.routes as routes

    monkeypatch.setattr(
        routes, "load_projects", lambda *a, **k: [dict(p) for p in projects]
    )
    monkeypatch.setattr(
        routes,
        "save_projects",
        lambda ps: projects.__setitem__(slice(None), [dict(p) for p in ps]),
    )
    if index_path is not None:
        monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_path)
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_profiles_match", lambda a, b: True)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "load_workspaces", lambda: [])
    monkeypatch.setattr(routes, "save_workspaces", lambda wss: None)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda p, **_kw: Path(p))


def _post_project_route(monkeypatch, path, body, responses):
    """Drive a project route in-process; each response lands in ``responses``.

    The recorder appends to a caller-owned list (rather than capturing into a
    dict) because concurrent drivers replace ``routes.j`` / ``routes.bad``
    between calls; responses are identified by payload shape, not by handler.
    """
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
    return routes.handle_post(
        SimpleNamespace(command="POST"), SimpleNamespace(path=path)
    )


def test_bind_refuses_to_start_a_sweep_for_a_project_being_deleted():
    """Ordering (a): admission is atomic with the start, so nothing is launched.

    The old flow started the worker unconditionally and admitted it from inside
    the worker: a delete landing in that window found nothing to join, cleared
    its deleting marker in the `finally`, and the late sweep then filed
    sessions under the removed project.
    """
    import api.routes as routes

    pid = "proj_regate_admit_late"
    started = threading.Event()
    t = threading.Thread(target=lambda: started.set(), daemon=True)
    routes._AUTO_ASSIGN_DELETING.add(pid)
    try:
        assert routes._auto_assign_start_sweep(pid, t) is False
        assert not started.wait(0.2), "a refused admission must not start the worker"
        assert pid not in routes._AUTO_ASSIGN_SWEEPS
    finally:
        routes._auto_assign_finish_deleting(pid)


def test_admitted_sweep_is_registered_and_always_joinable():
    """Every registered sweep is already running, so a delete can always join it."""
    import api.routes as routes

    pid = "proj_regate_joinable"
    ran = threading.Event()
    t = threading.Thread(target=lambda: ran.set(), daemon=True)
    try:
        assert routes._auto_assign_start_sweep(pid, t) is True
        assert pid in routes._AUTO_ASSIGN_SWEEPS
        assert ran.wait(5)
        assert routes._auto_assign_cancel_sweeps(pid, timeout=5.0) is True
    finally:
        routes._auto_assign_finish_deleting(pid)
        routes._auto_assign_sweep_end(pid, t)
    assert pid not in routes._AUTO_ASSIGN_SWEEPS


def test_cancel_sweeps_reports_failure_when_a_worker_outlives_the_join():
    """Ordering (b): a stuck sweep makes the drain report failure, not success."""
    import api.routes as routes

    pid = "proj_regate_stuck"
    release = threading.Event()

    def _sweep():
        routes._auto_assign_sweep_begin(pid)
        try:
            release.wait(10)
        finally:
            routes._auto_assign_sweep_end(pid)

    t = threading.Thread(target=_sweep, daemon=True)
    assert routes._auto_assign_start_sweep(pid, t) is True
    try:
        assert routes._auto_assign_cancel_sweeps(pid, timeout=0.2) is False
        # ...and the claim can be released so the intact project stays usable.
        routes._auto_assign_abort_deleting(pid)
        assert pid not in routes._AUTO_ASSIGN_DELETING
    finally:
        release.set()
        t.join(5)
        routes._auto_assign_finish_deleting(pid)
    assert not t.is_alive()


def test_delete_returns_503_and_keeps_the_project_when_a_sweep_cannot_drain(
    tmp_path, monkeypatch
):
    """Ordering (b) end-to-end: the handler refuses instead of orphaning rows."""
    import api.routes as routes

    pid = "proj_regate_503"
    projects = [{"project_id": pid, "name": "Busy", "profile": "default"}]
    _install_project_route_stubs(monkeypatch, projects, tmp_path / "no-index.json")
    release = threading.Event()

    def _sweep():
        routes._auto_assign_sweep_begin(pid)
        try:
            release.wait(10)
        finally:
            routes._auto_assign_sweep_end(pid)

    t = threading.Thread(target=_sweep, daemon=True)
    assert routes._auto_assign_start_sweep(pid, t) is True
    real_cancel = routes._auto_assign_cancel_sweeps
    # The handler joins with its 10 s default; shrink it for the test.
    monkeypatch.setattr(
        routes,
        "_auto_assign_cancel_sweeps",
        lambda project_id, timeout=10.0: real_cancel(project_id, timeout=0.2),
    )
    captured = []
    try:
        assert (
            _post_project_route(
                monkeypatch, "/api/projects/delete", {"project_id": pid}, captured
            )
            is True
        )
        assert [r["status"] for r in captured] == [503], captured
        assert [p["project_id"] for p in projects] == [pid], "project must stay intact"
        assert pid not in routes._AUTO_ASSIGN_DELETING, "claim released for a retry"
    finally:
        release.set()
        t.join(5)
        routes._auto_assign_finish_deleting(pid)


def test_create_during_the_delete_drain_survives_the_save(tmp_path, monkeypatch):
    """Ordering (c): the delete must not erase a project created while it waits.

    The handler read the catalog BEFORE joining its sweeps and saved that stale
    list afterwards; a project created during the (up to 10 s) join vanished.
    """
    import api.routes as routes

    pid = "proj_regate_drain"
    other = "proj_regate_other"
    projects = [
        {"project_id": pid, "name": "Doomed", "profile": "default"},
        {"project_id": other, "name": "Keeper", "profile": "default"},
    ]
    _install_project_route_stubs(monkeypatch, projects, tmp_path / "no-index.json")

    hold = threading.Event()
    entered = threading.Event()

    def _sweep():
        routes._auto_assign_sweep_begin(pid)
        try:
            hold.wait(10)
        finally:
            routes._auto_assign_sweep_end(pid)

    t = threading.Thread(target=_sweep, daemon=True)
    assert routes._auto_assign_start_sweep(pid, t) is True

    real_cancel = routes._auto_assign_cancel_sweeps

    def _hooked_cancel(project_id, timeout=10.0):
        entered.set()
        return real_cancel(project_id, timeout)

    monkeypatch.setattr(routes, "_auto_assign_cancel_sweeps", _hooked_cancel)

    responses = []

    def _delete():
        _post_project_route(
            monkeypatch, "/api/projects/delete", {"project_id": pid}, responses
        )

    del_thread = threading.Thread(target=_delete)
    del_thread.start()
    try:
        assert entered.wait(5), "delete never reached the sweep drain"
        _post_project_route(
            monkeypatch, "/api/projects/create", {"name": "BornDuringDrain"}, responses
        )
        # Only the create's response is in yet (the delete is still draining).
        assert [r["status"] for r in responses if r["payload"].get("project")] == [200], responses
    finally:
        hold.set()
        del_thread.join(10)
        routes._auto_assign_finish_deleting(pid)

    names = [p["name"] for p in projects]
    assert "BornDuringDrain" in names, f"the drain save erased a new project: {names}"
    assert "Doomed" not in names, names
    assert "Keeper" in names, names
    assert [r["status"] for r in responses if r["payload"] == {"ok": True}] == [200], responses


# ---------------------------------------------------------------------------
# CORE 1 (re-gate follow-up) — provider binding restoration, run for real
# ---------------------------------------------------------------------------


def _run_node(tmp_path: Path, name: str, script: str) -> str:
    if shutil.which("node") is None:
        pytest.skip("node is required for the dialog resolution probe")
    script_path = tmp_path / name
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


_BINDING_KEY_PROBE = """
__HELPERS__
function assert(cond, msg) { if (!cond) throw new Error(msg); }

// Case 1 — server-canonicalized pair matches the qualified catalog option.
const opts1 = [
  {value: '', name: '(none)'},
  {value: '@custom:backup:model-a:free', name: 'model-a:free', sub: 'custom:backup',
   _modelId: 'model-a:free', _providerId: 'custom:backup'},
  {value: 'gpt-4o-mini', name: 'gpt-4o-mini', sub: 'openai',
   _modelId: 'gpt-4o-mini', _providerId: 'openai'},
];
assert(
  _bindingModelKeyFor({model: 'model-a:free', model_provider: 'custom:backup'}, opts1, {duplicates: false})
    === '@custom:backup:model-a:free',
  'canonicalized pair must restore onto its qualified catalog option'
);

// Case 2 — duplicate bare ids stay provider-scoped.
const opts2 = [
  {value: '@openai:gpt-4o', name: 'gpt-4o', sub: 'openai',
   _key: 'openai\\u001fgpt-4o', _modelId: 'gpt-4o', _providerId: 'openai'},
  {value: '@azure:gpt-4o', name: 'gpt-4o', sub: 'azure',
   _key: 'azure\\u001fgpt-4o', _modelId: 'gpt-4o', _providerId: 'azure'},
];
assert(
  _bindingModelKeyFor({model: 'gpt-4o', model_provider: 'azure'}, opts2, {duplicates: true})
    === 'azure\\u001fgpt-4o',
  'the saved provider route must win over the first catalog entry'
);

// Case 3 — a saved pair missing from the catalog is re-injected, not dropped.
const opts3 = [{value: 'gpt-4o-mini', name: 'gpt-4o-mini', sub: 'openai',
                _modelId: 'gpt-4o-mini', _providerId: 'openai'}];
const key3 = _bindingModelKeyFor({model: 'model-a:free', model_provider: 'custom:backup'}, opts3, {duplicates: false});
assert(key3 === 'model-a:free', 'the saved pair must stay selectable: ' + key3);
const injected = opts3.filter(o => o._saved)[0];
assert(injected && injected.value === 'model-a:free' && injected.sub === 'custom:backup',
  'the saved pair must be re-injected with its provider');
assert(_modelValueFor(key3) === 'model-a:free', 'wire model id round-trips');

// ...and the provider survives the provider-scoped key form too.
const opts4 = [{value: '@openai:a', _key: 'openai\\u001fa', _modelId: 'a', _providerId: 'openai'}];
const key4 = _bindingModelKeyFor({model: 'm', model_provider: 'p'}, opts4, {duplicates: true});
assert(key4 === 'p\\u001fm', key4);
assert(_modelValueFor(key4) === 'm' && _modelProvFor(key4) === 'p', 'scoped key round-trips');

// Case 4 — the composer's own matcher is the fallback when identity is absent.
const opts5 = [{value: '@custom:backup:model-a:free'}];
assert(
  _bindingModelKeyFor(
    {model: 'model-a:free', model_provider: 'custom:backup'},
    opts5,
    {duplicates: false, select: {}, findModelInDropdown: () => '@custom:backup:model-a:free'}
  ) === '@custom:backup:model-a:free',
  'findModelInDropdown fallback'
);

// Case 5 — no saved model, and a bare (provider-less) binding.
assert(_bindingModelKeyFor({}, opts1, {duplicates: false}) === '', 'no saved model => (none)');
assert(
  _bindingModelKeyFor({model: 'gpt-4o-mini'}, opts3, {duplicates: false}) === 'gpt-4o-mini',
  'bare saved model matches by identity'
);
console.log('ok');
"""


def test_binding_model_key_resolution_matches_by_identity(tmp_path):
    """Run the shipped restoration logic under node (not a text assertion)."""
    src = _read_sessions_js()
    helpers = src[src.index("const _modelValueKeyFor=") : src.index("function _showProjectBindingsDialog(")]
    assert "_bindingModelKeyFor" in helpers
    script = _BINDING_KEY_PROBE.replace("__HELPERS__", helpers)
    assert _run_node(tmp_path, "binding_model_key_probe.js", script).strip() == "ok"
