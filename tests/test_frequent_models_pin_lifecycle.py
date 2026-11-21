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
no real profile, state directory or service is touched.  Rejected or failed
deletes must leave the target store alone, and a cleanup failure must never be
reported as a successful delete.
"""

import shutil
import sys
import types

import pytest

from api import model_preferences as mp
from api import profiles as profiles


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
    pin(env, 'work', 'model-a')
    with pytest.raises(ValueError):
        profiles.delete_profile_api('work')
    assert store_dir(env, 'work').is_dir()
    assert pins(env, 'work') == ['model-a']


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


def test_pin_store_cleanup_failure_is_not_reported_as_success(env, monkeypatch):
    install_fake_cli(monkeypatch, delete_impl=_rmtree_profile(env))
    profile_home(env, 'work').mkdir(parents=True)
    pin(env, 'work', 'model-a')
    # External damage: the store path is now a plain file, so removing the
    # store fails. The delete must surface the error, not claim success.
    shutil.rmtree(store_dir(env, 'work'))
    store_dir(env, 'work').write_text('not a directory')
    with pytest.raises(OSError):
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
    with pytest.raises(mp.PreferencesError):
        profiles.delete_profile_api('work')
    assert sentinel.read_text() == 'unrelated data'


def test_renamed_root_alias_keeps_default_pins(env, monkeypatch):
    monkeypatch.setattr(profiles, '_root_profile_name_cache', {'default', 'home'})
    pin(env, 'default', 'root-model')
    with pytest.raises(ValueError):
        profiles.delete_profile_api('home')
    assert pins(env, 'default') == ['root-model']
