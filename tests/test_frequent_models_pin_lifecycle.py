"""Pin-store lifecycle across named-profile deletion (#8622).

Greptile P2 (discussion_r4596348885): pins for a named profile live under
``STATE_DIR/model_preferences/<owner>`` — OUTSIDE the profile home
(``~/.hermes/profiles/<name>``).  ``delete_profile_api()`` only removed the
profile directory, so deleting and recreating a profile with the same name
resurrected the deleted profile's pins; six unavailable models consumed the
whole pin allowance and new pins were refused.

These tests drive the REAL ``delete_profile_api()`` — the ``hermes_cli``
delegate AND the ImportError rmtree fallback — against a throwaway HERMES_HOME
and a throwaway pin store.  The external CLI boundary is an isolated stand-in;
no real profile, state directory or service is touched.  Rejected deletes must
leave the target store alone.  A pin write for a home-less named profile (a tab
still on a deleted profile) must be refused without recreating the store, and a
pin-store cleanup failure after the delete already happened is reported as a
partial success with a warning — never as a bare 403/400/500 — with the damaged
store preserved and the profile-list cache refreshed regardless.
"""

import json
import shutil
import sys
import types
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from api import model_preferences as mp
from api import profiles as profiles
from api import routes


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + pin store; the only mutable boundaries."""
    home = tmp_path / 'hermes-home'
    (home / 'profiles').mkdir(parents=True)
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setattr(profiles, '_DEFAULT_HERMES_HOME', home)
    monkeypatch.setattr(profiles, '_active_profile', 'default')
    # Root-name membership in this disposable process: only the legacy alias.
    monkeypatch.setattr(profiles, '_root_profile_name_cache', {'default'})
    monkeypatch.setattr(profiles, '_root_profile_name_cache_loaded', True)
    monkeypatch.setattr(mp, '_state_dir', lambda: state)
    active = {'name': 'default'}
    monkeypatch.setattr(mp, '_request_profile', lambda: active['name'])
    mp._scan_cache.clear()
    return {'home': home, 'state': state, 'active': active}


def install_fake_cli(monkeypatch, *, delete_impl=None, create_impl=None):
    """Stand in for hermes_cli.profiles at the import boundary.

    ``delete_profile_api``/``create_profile_api`` import from this module at
    call time; the stand-ins record the call and delegate the filesystem effect
    to the caller-supplied impl (default: the real rmtree/mkdir behaviour).
    """
    calls = {'delete': [], 'create': []}
    pkg = types.ModuleType('hermes_cli')
    pkg.__path__ = []
    mod = types.ModuleType('hermes_cli.profiles')

    def delete_profile(name, yes=False, **kw):
        calls['delete'].append(name)
        if delete_impl is not None:
            delete_impl(name)

    def create_profile(name, clone_from=None, clone_config=False,
                       clone_all=False, no_alias=False, **kw):
        calls['create'].append(name)
        if create_impl is not None:
            create_impl(name)

    mod.delete_profile = delete_profile
    mod.create_profile = create_profile
    monkeypatch.setitem(sys.modules, 'hermes_cli', pkg)
    monkeypatch.setitem(sys.modules, 'hermes_cli.profiles', mod)
    return calls


def _rmtree_profile(env):
    return lambda name: shutil.rmtree(env['home'] / 'profiles' / name,
                                      ignore_errors=True)


def _mkdir_profile(env):
    def create(name):
        (env['home'] / 'profiles' / name).mkdir(parents=True, exist_ok=True)
    return create


def _block_cli_import(monkeypatch):
    """Force the ImportError fallback: sys.modules entry is None."""
    pkg = types.ModuleType('hermes_cli')
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, 'hermes_cli', pkg)
    monkeypatch.setitem(sys.modules, 'hermes_cli.profiles', None)


def profile_home(env, name):
    return env['home'] / 'profiles' / name


def store_dir(env, name):
    return env['state'] / 'model_preferences' / name


def store_file(env, name):
    return store_dir(env, name) / 'model_preferences.json'


def pin(env, name, model, provider='prov'):
    env['active']['name'] = name
    return mp.set_pin(env['state'], model, provider, True)


def pins(env, name):
    env['active']['name'] = name
    return [p['model'] for p in mp.read_pins(env['state'])]


# ── The regression: a same-name recreate must not resurrect old pins ────────

def test_delete_profile_removes_its_pin_store_and_a_recreate_starts_empty(
        env, monkeypatch):
    calls = install_fake_cli(monkeypatch, delete_impl=_rmtree_profile(env),
                             create_impl=_mkdir_profile(env))
    profiles.create_profile_api('work')
    assert profile_home(env, 'work').is_dir()
    for i in range(mp.MAX_PINS):
        pin(env, 'work', f'model-{i}')
    assert pins(env, 'work') == [f'model-{i}' for i in range(mp.MAX_PINS)]
    assert store_file(env, 'work').is_file()

    result = profiles.delete_profile_api('work')
    assert result == {'ok': True, 'name': 'work'}
    assert calls['delete'] == ['work']
    assert not profile_home(env, 'work').exists()
    assert not store_dir(env, 'work').exists()

    # Same-name recreate: no resurrected pins, and new pins are accepted.
    profiles.create_profile_api('work')
    assert pins(env, 'work') == []
    pin(env, 'work', 'fresh-model')
    assert pins(env, 'work') == ['fresh-model']


def test_delete_without_hermes_cli_fallback_also_cleans_pins(env, monkeypatch):
    _block_cli_import(monkeypatch)
    profile_home(env, 'work').mkdir(parents=True)
    for i in range(mp.MAX_PINS):
        pin(env, 'work', f'model-{i}')
    assert store_file(env, 'work').is_file()

    result = profiles.delete_profile_api('work')
    assert result == {'ok': True, 'name': 'work'}
    assert not profile_home(env, 'work').exists()  # the fallback rmtree ran
    assert not store_dir(env, 'work').exists()
    assert pins(env, 'work') == []


# ── Rejections and failures must never touch the target store ───────────────

def test_rejected_deletes_leave_the_pin_store_untouched(env, monkeypatch):
    install_fake_cli(monkeypatch, delete_impl=_rmtree_profile(env))
    profile_home(env, 'work').mkdir(parents=True)
    for i in range(mp.MAX_PINS):
        pin(env, 'work', f'model-{i}')
    before = store_file(env, 'work').read_bytes()

    # The default (root) profile cannot be deleted at all.
    with pytest.raises(ValueError):
        profiles.delete_profile_api('default')
    assert store_file(env, 'work').read_bytes() == before

    # Isolated-profile mode rejects every deletion.
    monkeypatch.setattr(profiles, '_is_isolated_profile_mode', lambda: True)
    with pytest.raises(PermissionError):
        profiles.delete_profile_api('work')
    assert store_file(env, 'work').read_bytes() == before


def test_a_failed_cli_delete_keeps_the_pin_store(env, monkeypatch):
    def boom(name):
        raise RuntimeError('cli refused')

    install_fake_cli(monkeypatch, delete_impl=boom)
    profile_home(env, 'work').mkdir(parents=True)
    pin(env, 'work', 'model-a')
    with pytest.raises(RuntimeError):
        profiles.delete_profile_api('work')
    assert store_dir(env, 'work').is_dir()
    assert pins(env, 'work') == ['model-a']


def test_missing_profile_in_fallback_delete_keeps_the_pin_store(
        env, monkeypatch):
    _block_cli_import(monkeypatch)
    # Store present, profile home absent: the fallback refuses before cleanup.
    # The store is seeded directly because a pin write for a home-less named
    # profile is itself refused (#8622) — that is the regression below.
    store_dir(env, 'work').mkdir(parents=True)
    store_file(env, 'work').write_text(
        json.dumps({'pins': [{'model': 'model-a', 'provider': 'prov'}]}),
        encoding='utf-8')
    with pytest.raises(ValueError):
        profiles.delete_profile_api('work')
    assert store_dir(env, 'work').is_dir()
    assert pins(env, 'work') == ['model-a']


def test_a_pin_from_a_tab_on_a_deleted_profile_cannot_recreate_its_store(
        env, monkeypatch):
    """#8622 maintainer finding 2.1: the cookie is a valid NAME, not a live
    profile.  A tab still on ``work`` after the delete must not resurrect
    ``model_preferences/work/`` — pin AND unpin alike — while the default
    profile, live named profiles and legal names keep working."""
    install_fake_cli(monkeypatch, delete_impl=_rmtree_profile(env),
                     create_impl=_mkdir_profile(env))
    profiles.create_profile_api('work')
    pin(env, 'work', 'model-a')
    assert store_file(env, 'work').is_file()

    assert profiles.delete_profile_api('work')['ok'] is True
    assert not store_dir(env, 'work').exists()

    # A pin from the stale tab is refused and recreates nothing.
    with pytest.raises(mp.PreferencesError):
        pin(env, 'work', 'model-b')
    assert not store_dir(env, 'work').exists()
    # An unpin from the same tab is refused the same way.
    with pytest.raises(mp.PreferencesError):
        mp.set_pin(env['state'], 'model-a', 'prov', False)
    assert not store_dir(env, 'work').exists()
    # A stale read sees nothing rather than the deleted profile's pins.
    assert pins(env, 'work') == []

    # A same-name recreate starts empty, and the ordinary/default paths still
    # accept pins (no over-broad refusal).
    profiles.create_profile_api('work')
    assert pins(env, 'work') == []
    pin(env, 'work', 'fresh-model')
    assert pins(env, 'work') == ['fresh-model']
    pin(env, 'default', 'default-model')
    assert pins(env, 'default') == ['default-model']


# ── Blast radius ─────────────────────────────────────────────────────────────

def test_other_profiles_and_the_default_store_survive_a_delete(
        env, monkeypatch):
    install_fake_cli(monkeypatch, delete_impl=_rmtree_profile(env),
                     create_impl=_mkdir_profile(env))
    profiles.create_profile_api('work')
    profiles.create_profile_api('other')
    pin(env, 'work', 'work-model')
    pin(env, 'other', 'other-model')
    pin(env, 'default', 'default-model')

    assert profiles.delete_profile_api('work')['ok'] is True
    assert not store_dir(env, 'work').exists()
    assert pins(env, 'other') == ['other-model']
    assert pins(env, 'default') == ['default-model']
    assert (env['state'] / 'model_preferences.json').is_file()


def test_cleanup_failure_reports_partial_success_and_keeps_the_store(
        env, monkeypatch):
    """#8622 maintainer finding 2.2: the profile is already gone, so a pin-store
    cleanup failure is a PARTIAL success.  The route must answer 200 with a
    warning (not 400/403/500), the damaged store must survive untouched, and
    the profile-list cache must refresh so the deleted profile does not stay
    listed until the cache expires."""
    install_fake_cli(monkeypatch, delete_impl=_rmtree_profile(env))
    profile_home(env, 'work').mkdir(parents=True)
    pin(env, 'work', 'model-a')
    # External damage: the store path is now a plain file, so removing the
    # store fails with an OSError after the delete already happened.
    shutil.rmtree(store_dir(env, 'work'))
    store_dir(env, 'work').write_text('not a directory')
    # A stale list cache must not survive the failed cleanup.
    profiles._LIST_PROFILES_CACHE = ([{'name': 'work'}], 9e18)

    result = profiles.delete_profile_api('work')

    assert result['ok'] is True, result
    assert result['name'] == 'work'
    assert result.get('warning'), result
    assert not profile_home(env, 'work').exists()  # the delete DID happen
    # No new data was deleted and the damaged store was preserved.
    assert store_dir(env, 'work').read_text() == 'not a directory'
    # The list cache was refreshed despite the cleanup failure.
    assert profiles._LIST_PROFILES_CACHE is None


def test_cleanup_permission_error_reports_partial_success_not_403(
        env, monkeypatch):
    """A real PermissionError during cleanup is a filesystem failure, not a
    forbidden action: the delete already happened, so it must not surface as
    the route's 403."""
    install_fake_cli(monkeypatch, delete_impl=_rmtree_profile(env))
    profile_home(env, 'work').mkdir(parents=True)
    pin(env, 'work', 'model-a')

    def denied(name):
        raise PermissionError('read-only file system')

    monkeypatch.setattr(mp, 'delete_profile_store', denied)
    result = profiles.delete_profile_api('work')
    assert result['ok'] is True and result.get('warning'), result
    assert not profile_home(env, 'work').exists()


def test_unexpected_cleanup_error_is_not_swallowed(env, monkeypatch):
    install_fake_cli(monkeypatch, delete_impl=_rmtree_profile(env))
    profile_home(env, 'work').mkdir(parents=True)
    pin(env, 'work', 'model-a')

    def boom(name):
        raise RuntimeError('unexpected')

    monkeypatch.setattr(mp, 'delete_profile_store', boom)
    with pytest.raises(RuntimeError):
        profiles.delete_profile_api('work')


@pytest.mark.parametrize('linked_component', ['root', 'store'])
def test_pin_cleanup_refuses_symlinks_and_preserves_their_targets(
        env, monkeypatch, tmp_path, linked_component):
    install_fake_cli(monkeypatch, delete_impl=_rmtree_profile(env))
    profile_home(env, 'work').mkdir(parents=True)
    external = tmp_path / 'unrelated'
    external.mkdir()
    if linked_component == 'root':
        target = external / 'work'
        target.mkdir()
        (env['state'] / 'model_preferences').symlink_to(external, target_is_directory=True)
    else:
        target = external
        store_dir(env, 'work').parent.mkdir(parents=True)
        store_dir(env, 'work').symlink_to(external, target_is_directory=True)
    sentinel = target / 'model_preferences.json'
    sentinel.write_text('unrelated data')
    # The linked target is never traversed or deleted — the refusal is reported
    # as a cleanup warning on an already-completed delete, not as success and
    # not as a bare 403/400 failure.
    result = profiles.delete_profile_api('work')
    assert result['ok'] is True and result.get('warning'), result
    assert sentinel.read_text() == 'unrelated data'


def test_renamed_root_alias_keeps_default_pins(env, monkeypatch):
    monkeypatch.setattr(profiles, '_root_profile_name_cache', {'default', 'home'})
    pin(env, 'default', 'root-model')
    with pytest.raises(ValueError):
        profiles.delete_profile_api('home')
    assert pins(env, 'default') == ['root-model']


# ── Route contract: a cleanup failure is not a failed delete ────────────────

def _post_profile_delete(monkeypatch, name):
    """Drive the REAL POST /api/profile/delete route with stubbed transport."""
    cap = {}

    def _j(_handler, payload, status=200):
        cap['j'] = (payload, status)
        return True

    def _bad(_handler, msg, code=400):
        cap['bad'] = (msg, code)
        return True

    handler = SimpleNamespace(command='POST', headers={})
    monkeypatch.setattr(routes, 'read_body', lambda _h: {'name': name})
    monkeypatch.setattr(routes, '_check_csrf', lambda _h: True)
    monkeypatch.setattr(routes, '_csrf_exempt_path', lambda _p: False)
    monkeypatch.setattr(routes, '_guard_request_session_visibility',
                        lambda *_a, **_k: True)
    monkeypatch.setattr(routes, 'j', _j)
    monkeypatch.setattr(routes, 'bad', _bad)
    routes.handle_post(handler, urlparse('/api/profile/delete'))
    return cap


def test_delete_route_reports_a_cleanup_failure_as_partial_success(
        env, monkeypatch):
    install_fake_cli(monkeypatch, delete_impl=_rmtree_profile(env))
    profile_home(env, 'work').mkdir(parents=True)
    pin(env, 'work', 'model-a')
    shutil.rmtree(store_dir(env, 'work'))
    store_dir(env, 'work').write_text('not a directory')

    cap = _post_profile_delete(monkeypatch, 'work')

    # 200 + warning: the delete already happened, so the route must not answer
    # 400 (PreferencesError), 500 (escaped OSError) or 403 (PermissionError) —
    # any of those would claim the profile still exists when it does not.
    assert 'bad' not in cap, cap
    payload, status = cap['j']
    assert status == 200
    assert payload['ok'] is True and payload['name'] == 'work'
    assert payload.get('warning'), payload
    assert not profile_home(env, 'work').exists()


def test_delete_route_keeps_the_403_for_a_delete_that_did_not_happen(
        env, monkeypatch):
    install_fake_cli(monkeypatch, delete_impl=_rmtree_profile(env))
    profile_home(env, 'work').mkdir(parents=True)
    pin(env, 'work', 'model-a')
    monkeypatch.setattr(profiles, '_is_isolated_profile_mode', lambda: True)

    cap = _post_profile_delete(monkeypatch, 'work')

    # A refused delete (nothing was removed) stays a 403, and the pin store
    # survives untouched.
    assert 'j' not in cap
    assert cap['bad'][1] == 403, cap
    assert pins(env, 'work') == ['model-a']
