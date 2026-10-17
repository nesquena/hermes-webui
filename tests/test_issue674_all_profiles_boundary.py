"""Real-boundary regression tests for the all-profiles external-agent bridge.

The maintainer re-gate at #674 replayed these two paths with the REAL
``get_last_workspace()`` and disposable config/filesystem inputs and got
2 failed / 1 passed. Both suites below drive the production boundaries rather
than the helper functions:

1. ``get_cli_sessions(all_profiles=True)`` -> ``get_cli_session_messages()``
   -- the all-profiles LOADER feeding the detail DISPATCHER.
2. ``_handle_session_import_cli()`` -> the persisted sidecar on disk
   -- the IMPORT HANDLER crossing the persisted-session boundary.

Why the boundaries matter: a named profile WITHOUT its own
``last_workspace.txt`` resolves to the legacy GLOBAL workspace on an ambient
call, but to the profile's own configured default on an explicit
``get_last_workspace(profile=...)`` call. Any code path that mixes the two
resolves lists an Aider id from one git root and then opens it against the
other, so the row silently comes back empty. Helper-level tests cannot see that
split because they hand-pick one resolver.
"""
from __future__ import annotations

import json
import os

import pytest


AIDER_HISTORY_BODY = "#### hello from aider\n\n> run the thing\n\nassistant reply line\n"


def _git_root(base, name):
    """Create a git-root-shaped directory the bounded aider scan will accept."""
    root = base / name
    (root / '.git').mkdir(parents=True)
    return root


def _write_history(repo, body=AIDER_HISTORY_BODY, name='.aider.chat.history.md'):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8')
    return path


@pytest.fixture
def two_roots(tmp_path):
    """Two distinct git roots: an 'ambient' one and a 'profile default' one.

    Mirrors the maintainer's replay: a named profile named ``work`` has NO
    ``last_workspace.txt`` of its own, while the legacy GLOBAL file still points
    at root_global and ``work``'s own configured default points at root_profile.
    So ``get_last_workspace()`` (ambient) -> root_global but
    ``get_last_workspace(profile='work')`` (explicit) -> root_profile: the two
    resolves genuinely disagree, which is what makes the list/open split real.
    """
    root_global = _git_root(tmp_path, 'root-global')
    root_profile = _git_root(tmp_path, 'root-profile')
    # The legacy GLOBAL binding, read only by the ambient resolve.
    (root_global / 'last_workspace.txt').write_text(str(root_global), encoding='utf-8')
    return tmp_path, root_global, root_profile


def _wire_real_workspace_resolver(monkeypatch, root_global, root_profile):
    """Point the REAL get_last_workspace at disposable inputs.

    No production resolver is replaced -- the genuine function runs, with its
    file lookups redirected at tmp_path -- so the ambient/explicit divergence
    comes from the real #7168 global-fallback rules rather than a stub.
    """
    import api.workspace as workspace

    monkeypatch.delenv('HERMES_WEBUI_AIDER_SCAN_DIR', raising=False)
    monkeypatch.delenv('HERMES_WEBUI_TEST_STATE_DIR', raising=False)
    monkeypatch.setattr(
        workspace, '_GLOBAL_LW_FILE', root_global / 'last_workspace.txt'
    )
    # No profile-scoped last_workspace.txt exists for the named profile.
    monkeypatch.setattr(workspace, '_last_workspace_file', lambda profile=None: None)
    monkeypatch.setattr(
        workspace, '_last_workspace_file_for_profile', lambda profile=None: None
    )
    monkeypatch.setattr(workspace, '_remote_terminal_cwd', lambda *a, **k: None)
    # #7168: a named profile must never inherit the global binding, so an
    # explicit named-profile resolve falls through to its own configured default.
    monkeypatch.setattr(
        workspace, '_is_default_profile_home', lambda home, *a, **k: False
    )
    monkeypatch.setattr(
        workspace, '_profile_default_workspace',
        lambda profile=None: str(root_profile),
    )
    monkeypatch.setattr('api.profiles.get_active_profile_name', lambda: 'work')
    return workspace


# ── Finding 1: one consistent aider scan root across list / detail / import ──


def test_all_profiles_loader_and_detail_dispatcher_agree_on_scan_root(
    two_roots, monkeypatch
):
    """A row listed by the all-profiles loader must open through the dispatcher.

    The loader used a BARE ``get_last_workspace()`` for the shared external
    workspace while the detail path and the cache key both go through
    ``_aider_profile_workspace()``. With a named profile that has no
    ``last_workspace.txt`` those two resolve to different git roots, so the
    listed Aider id was looked up against a root the list never scanned and
    ``get_cli_session_messages`` returned ``[]``.
    """
    import api.models as models
    import api.workspace as workspace

    _tmp, root_global, root_profile = two_roots
    _write_history(root_profile)

    # Real resolver, disposable inputs.
    _wire_real_workspace_resolver(monkeypatch, root_global, root_profile)

    # Prove the two resolves genuinely differ -- otherwise this test would pass
    # for the wrong reason and pin nothing.
    assert str(workspace.get_last_workspace()) == str(root_global)
    assert str(workspace.get_last_workspace(profile='work')) == str(root_profile)

    # One named profile named 'work', and the aider bridge active.
    monkeypatch.setattr(models, '_all_profiles_cli_contexts', lambda: ([], ()))
    monkeypatch.setattr(models, '_load_cli_sessions_uncached', lambda *a, **k: [])
    monkeypatch.setattr(models, '_default_claude_code_projects_dir', lambda: _tmp)
    monkeypatch.setattr(models, '_cli_sessions_streaming_freeze_marker', lambda: None)
    models.clear_cli_sessions_cache()

    listed = models.get_cli_sessions(
        all_profiles=True, include_claude_code=False, include_aider=True
    )
    aider_rows = [r for r in listed if r.get('source_tag') == 'aider']
    assert len(aider_rows) == 1, (
        'the all-profiles loader must list the aider transcript from the '
        f"profile's own scan root; got {listed!r}"
    )
    sid = aider_rows[0]['session_id']

    # Now open it through the REAL detail dispatcher with the same profile the
    # row was listed under. Before the fix this scanned root_global and
    # returned [] -- the "listed row opens empty" symptom.
    messages = models.get_cli_session_messages(sid, profile='work')
    contents = [m.get('content') for m in messages]
    assert contents, (
        'the detail dispatcher must resolve the SAME git root the all-profiles '
        f'loader listed from; it returned no messages at all ({messages!r})'
    )
    assert contents[0] == 'hello from aider'
    assert contents[-1] == 'assistant reply line'


def test_all_profiles_loader_does_not_regress_claude_code_binding(
    two_roots, monkeypatch
):
    """Claude Code stays on the ambient global resolve; only aider is scoped.

    Claude Code history is global (``~/.claude/projects``), not profile-owned,
    so its row workspace must keep following the ambient resolve. Scoping the
    shared lookup to a profile would regress that bridge -- this pins it.
    """
    import api.models as models

    _tmp, root_global, root_profile = two_roots

    _wire_real_workspace_resolver(monkeypatch, root_global, root_profile)

    projects = _tmp / 'claude-projects'
    (projects / 'sess').mkdir(parents=True)
    (projects / 'sess' / 'conv.jsonl').write_text(
        json.dumps({'type': 'user', 'message': {'role': 'user', 'content': 'hi cc'}})
        + '\n'
        + json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'content': 'yo'}})
        + '\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(models, '_default_claude_code_projects_dir', lambda: projects)

    rows = models.get_claude_code_sessions()

    assert len(rows) == 1, f'expected the Claude Code transcript, got {rows!r}'
    # Global bridge -> the ambient (global) workspace, NOT the profile default.
    assert rows[0]['workspace'] == str(root_global)


# ── Finding 2: a metadata miss must not mint a writable import ──


def _aider_row(tmp_path, root):
    """The exact list metadata the aider bridge emits: ``profile`` is None."""
    from api.models import _aider_session_id

    path = _write_history(root)
    return {'session_id': _aider_session_id(path), 'profile': None}, path


@pytest.fixture
def import_boundary(monkeypatch, tmp_path):
    """Wire the import handler to a real session store and a real Aider root.

    Only the foreign-store plumbing is stubbed (the ``j``/``bad`` response
    helpers and the unrelated side effects). ``import_cli_session``, the
    ``Session`` store and the disk are all real, so the assertions below are
    read back off the persisted sidecar rather than off a return value.
    """
    import api.config as cfg
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    root = _git_root(tmp_path, 'import-root')
    row, _path = _aider_row(tmp_path, root)

    # Use the session store the test suite already isolates for us (conftest
    # points HERMES_WEBUI_TEST_STATE_DIR at a per-run state dir) instead of
    # re-pointing the shared SESSION_DIR module global. Patching that global
    # from two modules is what the repo's own import tests avoid, and it made
    # the refresh path depend on which module happened to hold the binding.
    import api.models as _models_mod

    session_dir = _models_mod.SESSION_DIR

    # The shared module-global caches must not leak a view of the store that
    # predates this test's fixtures.
    _models_mod.clear_cli_sessions_cache()
    _models_mod._PERSISTED_SESSION_IDS_CACHE = (None, None, frozenset())

    monkeypatch.setattr(models, 'PROJECTS_FILE', tmp_path / 'projects.json')
    monkeypatch.setattr(cfg, 'PROJECTS_FILE', tmp_path / 'projects.json')
    monkeypatch.setattr(models, '_projects_migrated', True)

    # A named profile that owns no last_workspace.txt -> its own default root.
    monkeypatch.delenv('HERMES_WEBUI_AIDER_SCAN_DIR', raising=False)
    monkeypatch.delenv('HERMES_WEBUI_TEST_STATE_DIR', raising=False)
    monkeypatch.setattr(models, '_default_aider_scan_dir', lambda *a, **k: root)
    monkeypatch.setattr(
        'api.profiles.get_active_profile_name', lambda: 'work'
    )
    monkeypatch.setattr(profiles, '_active_profile', 'work')

    # The list row carries ``profile: None``, so the profile-scoped metadata
    # lookup MISSES and _resolve_cli_import_metadata returns {} even though the
    # message lookup still succeeds. That miss is the whole bug.
    monkeypatch.setattr(
        routes, '_resolve_cli_import_metadata',
        lambda sid, **kw: {},
    )
    monkeypatch.setattr(routes, 'require', lambda body, *keys: None)
    monkeypatch.setattr(
        routes, 'j',
        lambda _handler, payload, status=200, extra_headers=None: payload,
    )
    monkeypatch.setattr(
        routes, 'bad',
        lambda _handler, msg, code=400: {'error': msg, 'code': code},
    )
    monkeypatch.setattr(routes, 'publish_session_list_changed', lambda *a, **k: None)
    monkeypatch.setattr(routes, '_queue_generated_title_for_imported_session', lambda *a, **k: None)
    monkeypatch.setattr(routes, 'get_last_workspace', lambda *a, **k: str(root))

    boundary = {
        'routes': routes,
        'models': models,
        'root': root,
        'row': row,
        'dir': session_dir,
    }
    try:
        yield boundary
    finally:
        # Leave the shared isolated store as we found it.
        for _sid in (row['session_id'], 'cli_other_profile_0001', 'cli_ordinary_session_0001'):
            try:
                (session_dir / f'{_sid}.json').unlink(missing_ok=True)
            except OSError:
                pass
        models.clear_cli_sessions_cache()
        models._PERSISTED_SESSION_IDS_CACHE = (None, None, frozenset())


def test_all_profiles_import_of_aider_session_is_read_only_on_metadata_miss(
    import_boundary,
):
    """An Aider id must never become a WRITABLE WebUI session, even on a miss.

    The handler derived ``_read_only_view`` only from the resolved metadata and
    the subagent id check. Aider list metadata has ``profile: None``, so the
    all-profiles metadata resolve returns ``{}`` while the profile-aware message
    lookup still succeeds -- and the handler then called the real
    ``import_cli_session``, producing ``imported: true, read_only: false`` and a
    writable sidecar. The id prefix itself is the external read-only identity.
    """
    routes = import_boundary['routes']
    sid = import_boundary['row']['session_id']

    response = routes._handle_session_import_cli(
        object(),
        {'session_id': sid, 'profile': 'work', 'all_profiles': True},
    )

    assert response.get('imported') is False, (
        'a metadata-missed Aider import must stay a read-only view, not a '
        f'materialized writable session; got {response.get("imported")!r}'
    )
    assert response['session']['read_only'] is True, (
        f'the response projection must be read_only; got {response["session"]!r}'
    )

    # The persisted-session boundary: no writable sidecar on disk, and if a
    # sidecar exists at all it must be marked read_only after a reload.
    sidecar = import_boundary['dir'] / f'{sid}.json'
    if sidecar.exists():
        persisted = json.loads(sidecar.read_text(encoding='utf-8'))
        assert persisted.get('read_only') is True, (
            'reloading the saved sidecar must still show read_only; got '
            f'{persisted.get("read_only")!r}'
        )


def test_metadata_miss_refresh_never_mints_a_writable_sidecar(import_boundary):
    """The existing-sidecar REFRESH path must not launder read-only either.

    On refresh the handler re-resolves metadata and only coerced read_only for
    a subagent child. With a metadata miss it fell through with no coercion, so
    a previously-imported external session could be refreshed back into a
    writable state. A lookup miss must never mint a writable copy here either.
    """
    routes = import_boundary['routes']
    models = import_boundary['models']
    sid = import_boundary['row']['session_id']

    # A stale writable sidecar from before the fix -- exactly the laundering
    # risk the refresh path has to close.
    stale = models.Session(
        session_id=sid,
        title='stale',
        workspace=str(import_boundary['root']),
        model='aider',
        messages=[{'role': 'user', 'content': 'hello from aider'}],
        profile='work',
    )
    stale.save(touch_updated_at=False)

    # Precondition: the refresh path is only exercised if the handler can
    # actually LOAD the sidecar back. Without this assertion a store-binding
    # regression silently routes through the fresh-import path instead, and the
    # test would report a confusing read_only failure rather than the wiring
    # bug that caused it.
    assert models.Session.load(sid) is not None, (
        'the stale sidecar must be loadable from the isolated session store '
        f'({import_boundary["dir"]}) or this test is not exercising refresh'
    )

    response = routes._handle_session_import_cli(
        object(),
        {'session_id': sid, 'profile': 'work', 'all_profiles': True},
    )

    assert response.get('imported') is False, (
        'refreshing an already-imported session must not re-import it; got '
        f'{response.get("imported")!r}'
    )

    sidecar = import_boundary['dir'] / f'{sid}.json'
    assert sidecar.exists()
    persisted = json.loads(sidecar.read_text(encoding='utf-8'))
    assert persisted.get('read_only') is True, (
        'a refresh whose metadata lookup MISSED must coerce the persisted '
        f'sidecar to read_only; got {persisted.get("read_only")!r}'
    )
    assert response['session']['read_only'] is True


def test_ordinary_cli_import_is_not_downgraded_by_the_read_only_rule(import_boundary):
    """The read-only rule must key on the EXTERNAL id prefix only.

    Guards the fix against over-reach: a plain CLI session id (no external
    prefix) with no read-only metadata must still import as a normal writable
    CLI session. Weakening ordinary profile-ownership/import behaviour would
    be a regression, not a fix.
    """
    routes = import_boundary['routes']
    sid = 'cli_ordinary_session_0001'
    messages = [
        {'role': 'user', 'content': 'do the thing'},
        {'role': 'assistant', 'content': 'done'},
    ]

    monkey = import_boundary['models'].__dict__  # noqa: F841 - readability only

    def _messages(_sid, profile=None):
        return messages

    import api.routes as routes_mod

    original = routes_mod.get_cli_session_messages
    routes_mod.get_cli_session_messages = _messages
    try:
        response = routes._handle_session_import_cli(
            object(), {'session_id': sid, 'profile': 'work'}
        )
    finally:
        routes_mod.get_cli_session_messages = original

    assert response.get('imported') is True, (
        'a non-external CLI session must still take the normal import path; got '
        f'{response!r}'
    )


def test_profile_ownership_checks_are_not_weakened_by_the_read_only_rule(
    import_boundary,
):
    """Cross-profile 404/409 gating still runs before the read-only view.

    The fix must short-circuit only the READ-ONLY DECISION, never the
    profile-ownership gates. An all-profiles import naming a profile the stored
    session does not belong to must still be refused.
    """
    routes = import_boundary['routes']
    models = import_boundary['models']
    sid = 'cli_other_profile_0001'

    owned = models.Session(
        session_id=sid,
        title='owned by work',
        workspace=str(import_boundary['root']),
        model='cli',
        messages=[{'role': 'user', 'content': 'x'}],
        profile='work',
    )
    owned.save(touch_updated_at=False)

    response = routes._handle_session_import_cli(
        object(),
        {'session_id': sid, 'profile': 'other', 'all_profiles': True},
    )

    assert response.get('imported') is not True, (
        'an all_profiles import for a non-matching profile must stay refused; '
        f'got {response!r}'
    )


def test_env_isolation_sanity():
    """Guard the fixtures: no test may leave a real scan-dir override behind."""
    assert os.environ.get('HERMES_WEBUI_AIDER_SCAN_DIR') in (None, '')