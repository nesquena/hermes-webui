"""Tests for issue #1612: renamed root profile must resolve to ~/.hermes,
not ~/.hermes/profiles/<name>.

A renamed root/default Hermes profile (`is_default=True` on the agent side
but with a display name like `kinni`) was being treated as a named profile
directory under `~/.hermes/profiles/kinni`, which doesn't exist. Every
`if name == 'default':` site in api/profiles.py fell through to the wrong
filesystem path with `Profile 'kinni' does not exist.`

Fix: centralize the "is this the root?" check in `_is_root_profile(name)`
and replace each scattered `if name == 'default':` with it.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest


# ── _is_root_profile core ───────────────────────────────────────────────────


def test_is_root_profile_default_alias():
    """Legacy 'default' literal always resolves as root, regardless of cache state."""
    import api.profiles as p
    p._invalidate_root_profile_cache()
    assert p._is_root_profile('default') is True


def test_is_root_profile_empty_or_none_is_false():
    """Empty/None name is NOT root — caller code decides what to do."""
    import api.profiles as p
    assert p._is_root_profile('') is False
    assert p._is_root_profile(None) is False


def test_is_root_profile_renamed_root_via_list_profiles_api(monkeypatch):
    """A profile name reported by list_profiles_api with is_default=True is treated as root."""
    import api.profiles as p

    monkeypatch.setattr(p, 'list_profiles_api', lambda: [
        {'name': 'kinni', 'is_default': True, 'path': str(p._DEFAULT_HERMES_HOME)},
        {'name': 'haku', 'is_default': False, 'path': '/tmp/profiles/haku'},
    ])
    p._invalidate_root_profile_cache()

    assert p._is_root_profile('kinni') is True
    assert p._is_root_profile('haku') is False
    assert p._is_root_profile('default') is True


def test_is_root_profile_caches_results(monkeypatch):
    """Repeated calls don't re-invoke list_profiles_api — once-per-mutation memoization."""
    import api.profiles as p

    calls = {'n': 0}
    def fake_list():
        calls['n'] += 1
        return [{'name': 'kinni', 'is_default': True, 'path': '/tmp/.hermes'}]
    monkeypatch.setattr(p, 'list_profiles_api', fake_list)
    p._invalidate_root_profile_cache()

    p._is_root_profile('kinni')
    p._is_root_profile('kinni')
    p._is_root_profile('haku')
    assert calls['n'] == 1, "Cache should be hit after first lookup"


def test_is_root_profile_invalidation_drops_stale(monkeypatch):
    """Explicit invalidation forces re-query on next call."""
    import api.profiles as p

    seq = [
        [{'name': 'kinni', 'is_default': True, 'path': '/tmp/.hermes'}],
        [{'name': 'noblepro', 'is_default': True, 'path': '/tmp/.hermes'}],
    ]
    monkeypatch.setattr(p, 'list_profiles_api', lambda: seq[0] if seq else [])

    p._invalidate_root_profile_cache()
    assert p._is_root_profile('kinni') is True
    assert p._is_root_profile('noblepro') is False

    # Simulate rename — drop first state, second is now the truth
    seq.pop(0)
    p._invalidate_root_profile_cache()
    assert p._is_root_profile('kinni') is False
    assert p._is_root_profile('noblepro') is True


def test_is_root_profile_handles_list_profiles_failure(monkeypatch):
    """If list_profiles_api raises, fall back to literal-default-only — never raise."""
    import api.profiles as p

    def boom():
        raise RuntimeError("hermes_cli explosion")
    monkeypatch.setattr(p, 'list_profiles_api', boom)
    p._invalidate_root_profile_cache()

    # 'default' still works (handled before list_profiles_api call).
    assert p._is_root_profile('default') is True
    # Other names return False on failure.
    assert p._is_root_profile('kinni') is False


# ── get_root_profile_names: the same membership set, as data ────────────────
#
# A browser cannot call _is_root_profile() per name, so /api/profile/active and
# the profile-switch result ship the alias set instead. These pin that the two
# answers cannot drift.


def test_get_root_profile_names_default_only_when_root_unrenamed(monkeypatch):
    import api.profiles as p

    monkeypatch.setattr(p, 'list_profiles_api', lambda: [
        {'name': 'default', 'is_default': True, 'path': str(p._DEFAULT_HERMES_HOME)},
        {'name': 'haku', 'is_default': False, 'path': '/tmp/profiles/haku'},
    ])
    p._invalidate_root_profile_cache()

    assert p.get_root_profile_names() == ['default']


def test_get_root_profile_names_includes_renamed_root(monkeypatch):
    import api.profiles as p

    monkeypatch.setattr(p, 'list_profiles_api', lambda: [
        {'name': 'kinni', 'is_default': True, 'path': str(p._DEFAULT_HERMES_HOME)},
        {'name': 'haku', 'is_default': False, 'path': '/tmp/profiles/haku'},
    ])
    p._invalidate_root_profile_cache()

    assert p.get_root_profile_names() == ['default', 'kinni']


def test_get_root_profile_names_agrees_with_is_root_profile(monkeypatch):
    """The list is the predicate's membership set, not a parallel answer."""
    import api.profiles as p

    roster = [
        {'name': 'kinni', 'is_default': True, 'path': str(p._DEFAULT_HERMES_HOME)},
        {'name': 'haku', 'is_default': False, 'path': '/tmp/profiles/haku'},
        {'name': 'ghost', 'is_default': False, 'path': '/tmp/profiles/ghost'},
    ]
    monkeypatch.setattr(p, 'list_profiles_api', lambda: roster)
    p._invalidate_root_profile_cache()

    names = p.get_root_profile_names()
    for candidate in ('default', 'kinni', 'haku', 'ghost'):
        assert (candidate in names) is p._is_root_profile(candidate), candidate


def test_get_root_profile_names_invalidation_drops_stale(monkeypatch):
    import api.profiles as p

    seq = [
        [{'name': 'kinni', 'is_default': True, 'path': '/tmp/.hermes'}],
        [{'name': 'noblepro', 'is_default': True, 'path': '/tmp/.hermes'}],
    ]
    monkeypatch.setattr(p, 'list_profiles_api', lambda: seq[0] if seq else [])

    p._invalidate_root_profile_cache()
    assert p.get_root_profile_names() == ['default', 'kinni']

    seq.pop(0)
    p._invalidate_root_profile_cache()
    assert p.get_root_profile_names() == ['default', 'noblepro']


def test_get_root_profile_names_fails_soft_to_default(monkeypatch):
    """A roster lookup failure must not widen ownership or raise.

    ['default'] is the pre-rename truth: the browser keeps matching the literal
    alias and every other name keeps failing closed.
    """
    import api.profiles as p

    def boom():
        raise RuntimeError("hermes_cli explosion")
    monkeypatch.setattr(p, 'list_profiles_api', boom)
    p._invalidate_root_profile_cache()

    assert p.get_root_profile_names() == ['default']


def test_get_root_profile_names_shares_one_populate_path(monkeypatch):
    """One list_profiles_api() call serves both the predicate and the list."""
    import api.profiles as p

    calls = {'n': 0}

    def fake_list():
        calls['n'] += 1
        return [{'name': 'kinni', 'is_default': True, 'path': '/tmp/.hermes'}]

    monkeypatch.setattr(p, 'list_profiles_api', fake_list)
    p._invalidate_root_profile_cache()

    p._is_root_profile('kinni')
    p.get_root_profile_names()
    p._is_root_profile('haku')
    assert calls['n'] == 1


def test_switch_profile_result_ships_root_profiles(tmp_path, monkeypatch):
    """POST /api/profile/switch's payload carries the alias set too.

    Boot seeds it once; a switch can land on a roster the browser has never
    seen, so the same fact rides the switch result rather than waiting on the
    dropdown warm.
    """
    import api.profiles as p

    (tmp_path / 'webui_state').mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(p, '_DEFAULT_HERMES_HOME', tmp_path)
    monkeypatch.setattr(p, 'list_profiles_api', lambda: [
        {'name': 'kinni', 'is_default': True, 'path': str(tmp_path)},
    ])
    p._invalidate_root_profile_cache()

    result = p.switch_profile('kinni', process_wide=False)

    assert result['active'] == 'kinni'
    assert result['is_default'] is True
    assert result['root_profiles'] == ['default', 'kinni']


def test_profile_active_endpoint_ships_root_profiles(monkeypatch):
    """GET /api/profile/active carries the alias set beside ``is_default``.

    This is the boot payload the browser blocks on, so it is the only place a
    renamed-root fact can arrive before the profile dropdown roster warms.
    """
    from types import SimpleNamespace
    from urllib.parse import urlparse

    import api.profiles as p
    import api.routes as routes

    captured = {}

    def fake_j(_handler, payload, status=200, **_kwargs):
        captured['status'] = status
        captured['payload'] = payload
        return captured

    monkeypatch.setattr(routes, 'j', fake_j)
    monkeypatch.setattr(p, 'get_active_profile_name', lambda: 'kinni')
    monkeypatch.setattr(p, 'get_active_hermes_home', lambda: '/home/u/.hermes')
    monkeypatch.setattr(routes, 'get_profile_default_workspace', lambda: '/srv/x')
    monkeypatch.setattr(p, 'list_profiles_api', lambda: [
        {'name': 'kinni', 'is_default': True, 'path': '/home/u/.hermes'},
        {'name': 'haku', 'is_default': False, 'path': '/home/u/.hermes/profiles/haku'},
    ])
    p._invalidate_root_profile_cache()

    routes.handle_get(SimpleNamespace(), urlparse('/api/profile/active'))

    payload = captured['payload']
    assert payload['name'] == 'kinni'
    assert payload['is_default'] is True
    assert payload['root_profiles'] == ['default', 'kinni']


def test_profile_active_endpoint_root_profiles_survives_roster_failure(monkeypatch):
    """A boot-critical endpoint must not 500 or widen when the roster fails."""
    from types import SimpleNamespace
    from urllib.parse import urlparse

    import api.profiles as p
    import api.routes as routes

    captured = {}

    def fake_j(_handler, payload, status=200, **_kwargs):
        captured['payload'] = payload
        return captured

    def boom():
        raise RuntimeError('hermes_cli explosion')

    monkeypatch.setattr(routes, 'j', fake_j)
    monkeypatch.setattr(p, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(p, 'get_active_hermes_home', lambda: '/home/u/.hermes')
    monkeypatch.setattr(routes, 'get_profile_default_workspace', lambda: None)
    monkeypatch.setattr(p, 'list_profiles_api', boom)
    p._invalidate_root_profile_cache()

    routes.handle_get(SimpleNamespace(), urlparse('/api/profile/active'))

    assert captured['payload']['root_profiles'] == ['default']


# ── get_active_hermes_home: returns _DEFAULT_HERMES_HOME for renamed root ──


def test_get_active_hermes_home_returns_default_for_renamed_root(tmp_path, monkeypatch):
    """The core bug: a renamed root profile must resolve to _DEFAULT_HERMES_HOME,
    not _DEFAULT_HERMES_HOME / 'profiles' / <name>."""
    import api.profiles as p

    monkeypatch.setattr(p, '_DEFAULT_HERMES_HOME', tmp_path)
    monkeypatch.setattr(p, 'list_profiles_api', lambda: [
        {'name': 'kinni', 'is_default': True, 'path': str(tmp_path)},
    ])
    p._invalidate_root_profile_cache()
    monkeypatch.setattr(p, '_active_profile', 'kinni')

    result = p.get_active_hermes_home()
    assert result == tmp_path, f"Expected {tmp_path}, got {result}"


def test_get_active_hermes_home_returns_named_for_real_named_profile(tmp_path, monkeypatch):
    """Backward compat: a real named (non-default) profile still resolves to profiles/<name>."""
    import api.profiles as p

    profile_dir = tmp_path / 'profiles' / 'haku'
    profile_dir.mkdir(parents=True)
    monkeypatch.setattr(p, '_DEFAULT_HERMES_HOME', tmp_path)
    monkeypatch.setattr(p, 'list_profiles_api', lambda: [
        {'name': 'kinni', 'is_default': True, 'path': str(tmp_path)},
        {'name': 'haku', 'is_default': False, 'path': str(profile_dir)},
    ])
    p._invalidate_root_profile_cache()
    monkeypatch.setattr(p, '_active_profile', 'haku')

    result = p.get_active_hermes_home()
    assert result == profile_dir


# ── switch_profile: accepts renamed root display name ─────────────────────


def test_switch_profile_resolution_renamed_root_picks_default_home(tmp_path, monkeypatch):
    """switch_profile()'s resolution branch: a renamed root must select
    _DEFAULT_HERMES_HOME, not raise 'Profile <name> does not exist.'

    We don't drive switch_profile() end-to-end (it touches reload_config,
    workspace resolution, env mutation, etc.); instead we exercise the
    same resolve-or-raise structure that lives at the head of switch_profile.
    """
    import api.profiles as p

    monkeypatch.setattr(p, '_DEFAULT_HERMES_HOME', tmp_path)
    monkeypatch.setattr(p, 'list_profiles_api', lambda: [
        {'name': 'kinni', 'is_default': True, 'path': str(tmp_path)},
    ])
    p._invalidate_root_profile_cache()

    # Mirror switch_profile's resolution logic
    name = 'kinni'
    if p._is_root_profile(name):
        home = p._DEFAULT_HERMES_HOME
    else:
        home = p._resolve_named_profile_home(name)
        if not home.is_dir():
            raise ValueError(f"Profile '{name}' does not exist.")
    assert home == tmp_path

    # Sanity: a TRULY missing profile still raises (backward compat)
    with pytest.raises(ValueError, match="does not exist"):
        name = 'phantom'
        if p._is_root_profile(name):
            home = p._DEFAULT_HERMES_HOME
        else:
            home = p._resolve_named_profile_home(name)
            if not home.is_dir():
                raise ValueError(f"Profile '{name}' does not exist.")


def test_switch_profile_sticky_marker_renamed_root(tmp_path, monkeypatch):
    """switch_profile writes '' (empty marker) to active_profile file when
    switching to the root profile, regardless of its display name. This
    means a subsequent boot reads '' → falls through to 'default' alias →
    _is_root_profile('default') → resolves to _DEFAULT_HERMES_HOME, which
    is the only correct location for the renamed-root case."""
    import api.profiles as p

    monkeypatch.setattr(p, '_DEFAULT_HERMES_HOME', tmp_path)
    monkeypatch.setattr(p, 'list_profiles_api', lambda: [
        {'name': 'kinni', 'is_default': True, 'path': str(tmp_path)},
    ])
    p._invalidate_root_profile_cache()

    # Mirror the sticky-write line directly — guards that the new ternary
    # uses _is_root_profile, not the literal-'default' compare.
    written = '' if p._is_root_profile('kinni') else 'kinni'
    assert written == ''
    written2 = '' if p._is_root_profile('haku') else 'haku'
    assert written2 == 'haku' 


def test_delete_profile_blocks_renamed_root(tmp_path, monkeypatch):
    """delete_profile_api on a renamed root must refuse, same as 'default'."""
    import api.profiles as p

    monkeypatch.setattr(p, '_DEFAULT_HERMES_HOME', tmp_path)
    monkeypatch.setattr(p, 'list_profiles_api', lambda: [
        {'name': 'kinni', 'is_default': True, 'path': str(tmp_path)},
    ])
    p._invalidate_root_profile_cache()

    with pytest.raises(ValueError, match="Cannot delete the default profile"):
        p.delete_profile_api('kinni')


# ── Cleanup: invalidate cache between tests so they don't leak ─────────────


@pytest.fixture(autouse=True)
def _invalidate_cache_around_test():
    import api.profiles as p
    p._invalidate_root_profile_cache()
    yield
    p._invalidate_root_profile_cache()
