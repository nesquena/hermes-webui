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
import threading
from pathlib import Path


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
    # Restoration prefers the exact (model, provider) pair.
    assert 'modelOptions.find(o=>o.value===proj.model&&String(o.sub||"")===wantProv)' in src
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
    seg = src[i:i + 5000]
    cancel_i = seg.index('_auto_assign_cancel_sweeps(body["project_id"])')
    save_i = seg.index("save_projects(projects)")
    assert cancel_i < save_i, "cancellation must precede project removal"
    assert '_auto_assign_finish_deleting(body["project_id"])' in seg


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
