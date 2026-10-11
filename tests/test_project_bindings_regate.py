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


class _AnyWorkspace(set):
    """A set that claims to contain every workspace path."""

    def __contains__(self, item):  # noqa: D105
        return True




def _read_routes_py() -> str:
    return (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text(encoding="utf-8")


PB_I18N_KEYS = (
    "pb_bindings_title",
    "pb_bindings_menu",
    "pb_close",
    "pb_field_workspaces",
    "pb_field_model",
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
    "pb_cancel",
    "pb_save",
    "pb_updated",
    "pb_update_failed",
)

# Keys the UX work retired. The 2026-10-07T19:22:30Z re-gate dropped the chip
# menu's inline binding summary and its per-field "Unbind …" rows, and hid the
# per-project "Reasoning effort" row (it is profile-wide; #7881). The
# 2026-10-11T02:08:20Z re-gate moved the auto-assign sweep, its dialog toggle and
# the confirm it opened into a follow-up PR, so the five keys that only rendered
# that toggle/confirm are retired with it (nothing reads them).
PB_I18N_KEYS_RETIRED = (
    "pb_auto_assign_label",
    "pb_auto_assign_hint",
    "pb_auto_assign_confirm",
    "pb_auto_assign_confirm_unknown",
    "pb_auto_assign_confirm_btn",
    "pb_bindings_menu_bound",
    "pb_ws_summary",
    "pb_chip_model",
    "pb_chip_effort",
    "pb_chip_auto",
    "pb_unbind_workspace_named",
    "pb_unbind_model",
    "pb_unbind_effort",
    "pb_field_effort",
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
    # The option's own captured provider wins over the resolver's currently
    # selected route: the cloned entry has no DOM metadata, and
    # _modelStateForSelect answers with whichever route OWNS the value, so two
    # providers offering the same bare model id restored the wrong one
    # (re-gate 2026-10-07T14:33:01Z).
    assert "o._providerId=o.sub||(st&&st.model_provider)||'';" in src
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
    # Window covers the whole save block (it grew when the provider-scoped key
    # handling landed: _modelValueFor/_modelProvFor now run unconditionally).
    save_i = src.index("if(modelVal){")
    save_seg = src[save_i:save_i + 2400]
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
# Re-gate 2026-10-07T19:22:30Z — the chip menu is ONE row now (item 1).
# (Supersedes the earlier "unbind only the clicked workspace" SHOULD-FIX: the
# per-workspace unbind rows are gone, so the whole-menu-clear bug they fixed
# can no longer be reached from the menu.)
# ---------------------------------------------------------------------------


def test_chip_menu_unbinds_only_the_clicked_workspace():
    """The old multi-row payload can never come back through the menu."""
    src = _read_sessions_js()
    assert "const remaining=_boundWs.filter(p=>p!==wsPath);" not in src
    assert "_unbindItem" not in src
    assert "_saveProjectBindings(proj, remaining.length?{workspaces:remaining}:{workspaces:null});" not in src


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
    assert "t('pb_unbind_ws_title')" in src


def test_project_bindings_i18n_keys_present_in_all_locales():
    """Every locale block defines the full pb_* key set (i18n.js parity)."""
    src = _read_static("i18n.js")
    assert len(I18N_LOCALES) == 15
    for loc in I18N_LOCALES:
        chunk = _i18n_locale_chunk(src, loc)
        missing = [k for k in PB_I18N_KEYS if ("%s: '" % k) not in chunk]
        assert not missing, f"locale {loc!r} is missing pb_* keys: {missing}"


# ---------------------------------------------------------------------------
# SILENT — backfill rewrites historical activity dates
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

// Case 3 — a saved pair missing from the catalog is re-injected under a
// provider-SCOPED key (not the bare id: restoring the bare model would silently
// rebind the binding to whatever other provider happens to offer it).
const opts3 = [{value: 'gpt-4o-mini', name: 'gpt-4o-mini', sub: 'openai',
                _modelId: 'gpt-4o-mini', _providerId: 'openai'}];
const key3 = _bindingModelKeyFor({model: 'model-a:free', model_provider: 'custom:backup'}, opts3, {duplicates: false});
assert(key3 === 'custom:backup\\u001fmodel-a:free', 'the saved pair must keep its provider: ' + key3);
const injected = opts3.filter(o => o._saved)[0];
assert(injected && injected.value === key3 && injected.sub === 'custom:backup',
  'the saved pair must be re-injected with its provider');
assert(_modelValueFor(key3) === 'model-a:free' && _modelProvFor(key3) === 'custom:backup',
  'the provider-scoped key round-trips');

// ...and the provider survives the provider-scoped key form too.
const opts4 = [{value: '@openai:a', _key: 'openai\\u001fa', _modelId: 'a', _providerId: 'openai'}];
const key4 = _bindingModelKeyFor({model: 'm', model_provider: 'p'}, opts4, {duplicates: true});
assert(key4 === 'p\\u001fm', key4);
assert(_modelValueFor(key4) === 'm' && _modelProvFor(key4) === 'p', 'scoped key round-trips');

// Case 4 — the composer's own matcher is the fallback when identity is absent.
const opts5 = [{value: '@custom:backup:model-a:free', sub: 'custom:backup', _providerId: 'custom:backup'}];
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

// Case 6 — [re-gate 2026-10-07T14:33:01Z] only a DIFFERENT provider offers the
// saved model id. The old bare fallback reopened/saved custom:primary.
const opts6 = [{value: 'model-a:free', name: 'model-a:free', sub: 'custom:primary',
                _modelId: 'model-a:free', _providerId: 'custom:primary'}];
const key6 = _bindingModelKeyFor({model: 'model-a:free', model_provider: 'custom:backup'}, opts6, {duplicates: false});
assert(key6.indexOf('custom:primary') === -1, 'must not reopen as custom:primary: ' + key6);
assert(_modelValueFor(key6) === 'model-a:free' && _modelProvFor(key6) === 'custom:backup',
  'the saved (model, provider) pair must survive: ' + key6);
const inj6 = opts6.filter(o => o._saved)[0];
assert(inj6 && inj6.value === key6, 'the re-injected option must be selectable by that key');

// Case 7 — a resolver answer carrying a DIFFERENT provider is rejected too.
const opts7 = [{value: 'gpt-4o', name: 'gpt-4o', sub: 'provA', _modelId: 'gpt-4o', _providerId: 'provA'}];
const key7 = _bindingModelKeyFor(
  {model: 'gpt-4o', model_provider: 'provB'},
  opts7,
  {duplicates: false, select: {}, findModelInDropdown: () => 'gpt-4o'}
);
assert(_modelProvFor(key7) === 'provB', 'the saved provider must survive a resolver snap: ' + key7);

// Case 8 — no saved provider: the bare catalog row is still the identity.
const opts8 = [{value: 'model-a:free', name: 'model-a:free', sub: 'custom:primary',
                _modelId: 'model-a:free', _providerId: 'custom:primary'}];
assert(
  _bindingModelKeyFor({model: 'model-a:free'}, opts8, {duplicates: false}) === 'model-a:free',
  'a provider-less saved model keeps matching the bare catalog row'
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


# ---------------------------------------------------------------------------
# Re-gate 2026-10-07T14:33:01Z — the four remaining items on head bfa7b68c.
# ---------------------------------------------------------------------------


def test_binding_options_prefer_the_captured_provider():
    """[CORE] static/sessions.js:10788 — the cloned option loses its DOM
    metadata, and the resolver answers with the route that currently OWNS the
    value; two providers offering the same bare model id then restored (and
    re-saved) the wrong provider."""
    src = _read_sessions_js()
    assert "o._providerId=o.sub||(st&&st.model_provider)||'';" in src
    assert "o._providerId=_optProviderId(o)||(st&&st.model_provider)||o.sub||'';" not in src


def test_binding_save_keeps_a_provider_scoped_reinjection():
    """[CORE] static/sessions.js:10875 — the save path must recover the model id
    and the provider from a provider-scoped key in a NON-duplicate catalog."""
    src = _read_sessions_js()
    assert "const _bare=_modelValueFor(modelVal);" in src
    assert "let _prov=_modelProvFor(modelVal)||null;" in src
    assert "const _bare=_hasDuplicateModelValues?_modelValueFor(modelVal):modelVal;" not in src
    assert "if(_hasDuplicateModelValues){\n        _prov=_modelProvFor(modelVal)||null;\n      }" not in src




def test_delete_clears_a_cache_only_session_for_the_removed_project(tmp_path, monkeypatch):
    """[SILENT] api/routes.py:16855 — real create -> delete -> draft-save used to
    persist the removed project id.

    A chat created by "+ New Chat" is cache-only until its first save, so the
    index-only unlink never saw it. The delete now clears matching cached
    sessions — including those absent from the index — under the catalog lock.
    """
    import api.routes as routes

    pid = "proj_regate_cache_only"
    keep = "proj_regate_untouched"
    projects = [{
        "project_id": pid, "name": "Gone", "profile": "default", "workspaces": ["D:/ws-a"],
    }]
    _install_project_route_stubs(
        monkeypatch, projects, index_path=tmp_path / "missing_index.json"
    )

    class _CachedSession:
        def __init__(self, project_id):
            self.project_id = project_id
            self.saves = 0

        def save(self, *a, **k):
            self.saves += 1

    orphan = _CachedSession(pid)
    other = _CachedSession(keep)
    with routes.LOCK:
        routes.SESSIONS["s_regate_orphan"] = orphan
        routes.SESSIONS["s_regate_other"] = other
    try:
        responses = []
        _post_project_route(
            monkeypatch, "/api/projects/delete", {"project_id": pid}, responses
        )
        assert responses and responses[-1]["status"] == 200, responses
        assert orphan.project_id is None, (
            "the unsaved new chat kept the id of the deleted project"
        )
        assert orphan.saves == 0, "deletion must not write an unsaved chat"
        assert other.project_id == keep, "another project's session was touched"
        assert all(p["project_id"] != pid for p in projects)
    finally:
        with routes.LOCK:
            routes.SESSIONS.pop("s_regate_orphan", None)
            routes.SESSIONS.pop("s_regate_other", None)


class _DepthProbeLock:
    """Context-manager lock that records how deep the catalog section is.

    ``threading.RLock`` is a factory function, so wrap a real RLock instead of
    subclassing it.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.depth = 0

    def __enter__(self):
        self.depth += 1
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        self._lock.release()
        return False




def _write_index(tmp_path, rows):
    index_file = tmp_path / "_index.json"
    index_file.write_text(json.dumps(rows))
    return index_file










def _dialog_source() -> str:
    """Only the _showProjectBindingsDialog body (up to the next top-level fn)."""
    src = _read_sessions_js()
    start = src.index("function _showProjectBindingsDialog(proj){")
    end = src.index("function _startProjectRename(proj, chip){")
    return src[start:end]


def _ctx_menu_source() -> str:
    """Only the _showProjectContextMenu body (up to the next top-level fn)."""
    src = _read_sessions_js()
    start = src.index("function _showProjectContextMenu(e, proj, chip){")
    end = src.index("async function _confirmDeleteProject(proj){")
    return src[start:end]


def test_chip_menu_holds_a_single_project_settings_row():
    """[item 1] One row; no inline summary, no "Unbind …" rows, no widening."""
    seg = _ctx_menu_source()
    assert "t('pb_bindings_menu')" in seg
    assert "_unbindItem" not in seg
    assert "pb_bindings_menu_bound" not in seg
    assert "pb_ws_summary" not in seg
    assert "pb_chip_" not in seg
    assert "pb_unbind_model" not in seg
    assert "pb_unbind_effort" not in seg
    # It still opens the dialog on click.
    assert "_showProjectBindingsDialog(proj);" in seg
    # ...and the dialog title/menu label say "Project settings", not "Bindings"
    # (the maintainer's wording: "Bindings" is implementer vocabulary).
    i18n = _read_static("i18n.js")
    assert "pb_bindings_title: 'Project settings — {0}'" in i18n
    assert "pb_bindings_menu: 'Project settings…'" in i18n


def test_chip_menu_clamps_to_the_viewport():
    """[item 5] Clamped like the session row menu: 8px margins + a width cap."""
    seg = _ctx_menu_source()
    assert "window.innerWidth-8" in seg
    assert "window.innerHeight-8" in seg
    assert "window.innerWidth-16" in seg
    assert "menu.style.left=menuLeft+'px';" in seg
    assert "menu.style.top=menuTop+'px';" in seg


def test_bindings_dialog_uses_the_shared_app_dialog_classes():
    """[item 4] Built on .app-dialog* => skins, Escape, focus trap, contrast."""
    seg = _dialog_source()
    assert "app-dialog-overlay project-bindings-overlay" in seg
    assert "app-dialog project-bindings-dialog" in seg
    assert "app-dialog-header" in seg
    assert "app-dialog-title" in seg
    assert "app-dialog-close" in seg
    assert "app-dialog-btn confirm" in seg
    # Escape closes it; Tab stays trapped inside it.
    assert "if(e.key==='Escape')" in seg
    assert "e.key==='Tab'" in seg
    assert "document.removeEventListener('keydown',_onKey,true);" in seg
    # The private inline-styled overlay/dialog chrome is gone.
    assert "overlay.style.cssText" not in seg
    assert "dialog.style.cssText" not in seg
    # The CSS keeps the overlay BELOW the shared app dialog (z-index 1100) so a
    # prompt/confirm opened from inside still stacks on top.
    css = _read_static("style.css")
    assert ".app-dialog-overlay.project-bindings-overlay{display:flex;z-index:1050;}" in css


def test_bindings_dialog_hides_the_reasoning_effort_row():
    """[item 2] The profile-wide effort row is hidden; the API field stays."""
    seg = _dialog_source()
    assert "pb_field_effort" not in seg
    assert "pb_effort_" not in seg
    assert "effortCombo" not in seg
    # (the field never reaches the save payload or reads proj.reasoning_effort)
    assert "fields.reasoning_effort" not in seg
    assert "proj.reasoning_effort" not in seg
    # Backend keeps the field (it comes back with per-session effort, #7881).
    routes_src = _read_routes_py()
    assert "\"reasoning_effort\"" in routes_src
    assert "VALID_REASONING_EFFORTS" in routes_src










def test_retired_bindings_i18n_keys_are_gone_from_every_locale():
    """The keys the menu/effort cleanup retired must not linger anywhere."""
    src = _read_static("i18n.js")
    for loc in I18N_LOCALES:
        chunk = _i18n_locale_chunk(src, loc)
        leftover = [k for k in PB_I18N_KEYS_RETIRED if ("%s: '" % k) in chunk]
        assert not leftover, f"locale {loc!r} still defines retired keys: {leftover}"


# ---------------------------------------------------------------------------
# Re-gate 2026-10-07T22:04:16Z — keyboard ownership under a stacked dialog and
# stale docs. (The Save-side auto-assign confirmation and the canonical preview
# counts this re-gate also covered moved out with the auto-assign split,
# maintainer re-gate 2026-10-11T02:08:20Z.)
# ---------------------------------------------------------------------------


def test_stacked_app_dialog_owns_the_keyboard():
    """[CORE] static/sessions.js:10981 (senior-review MUST-FIX).

    The dialog's document-capture ``_onKey`` must yield to the shared app dialog
    (the "Type a path…" prompt / the counted confirm) opened from inside it.
    Both listeners run on one keydown — ``stopPropagation()`` does not stop
    same-node listeners — so Escape used to close the prompt AND the dialog
    (losing unsaved workspace/model edits) and Tab escaped the modal on top.
    """
    seg = _dialog_source()
    guard = "if(e.defaultPrevented||_isAppDialogOpen()) return;"
    assert guard in seg
    on_key = seg.index("function _onKey(e){")
    # The guard is the FIRST thing _onKey does: before its own Escape/Tab paths.
    assert on_key < seg.index(guard)
    assert seg.index(guard) < seg.index("if(e.key==='Escape')", on_key)
    assert seg.index(guard) < seg.index("e.key==='Tab'", on_key)
    # ...and it reads the shared dialog's own open state (ui.js), so it works in
    # both listener-registration orders.
    ui = _read_static("ui.js")
    assert "function _isAppDialogOpen(){" in ui
    assert "if(!_isAppDialogOpen()) return;" in ui










def test_docs_match_the_project_settings_ui():
    """[SHOULD-FIX] 4 — README + ARCHITECTURE still described the retired UI."""
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    arch = (root / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for stale in ("Bindings…", "effort:high", "ws×2", "bindings summary",
                  "pick **Bindings"):
        assert stale not in readme, stale
        assert stale not in arch, stale
    assert "Project settings…" in readme
    assert "Project settings…" in arch
    # The auto-assign sweep, its toggle, the counted confirmation it opened and
    # the preview endpoint behind that confirmation moved to a follow-up PR, so
    # no doc may still sell them as part of this build. `auto_assign` itself is
    # documented as a stored-but-dormant field (like `reasoning_effort`).
    for gone in ("Auto-assign sessions by workspace", "auto-assign-preview",
                 "File 23 existing chats under"):
        assert gone not in readme, gone
        assert gone not in arch, gone
    assert "stored but dormant" in arch
