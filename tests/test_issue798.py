"""
Issue #798 — Profile isolation: switching profile in one browser client must not
affect sessions created by other concurrent clients.

Root cause: _active_profile was a process-level global in api/profiles.py.
Fix: new_session() now accepts an explicit `profile` param passed from the client
request body (S.activeProfile), which bypasses the shared global entirely.
get_hermes_home_for_profile() resolves a HERMES_HOME path from a name without
touching os.environ or module-level state.
"""

import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest


# ── R19: get_hermes_home_for_profile ─────────────────────────────────────────

def test_get_hermes_home_for_profile_returns_default_for_none():
    """R19a: None / empty string / 'default' all return the base home."""
    import api.profiles as p
    base = p._DEFAULT_HERMES_HOME
    assert p.get_hermes_home_for_profile(None) == base
    assert p.get_hermes_home_for_profile('') == base
    assert p.get_hermes_home_for_profile('default') == base


def test_get_hermes_home_for_profile_returns_profile_subdir(tmp_path):
    """R19b: Named profile that exists returns its subdirectory."""
    os.environ['HERMES_BASE_HOME'] = str(tmp_path)
    for mod in list(sys.modules):
        if mod in ('api.config', 'api.profiles'):
            del sys.modules[mod]
    import api.profiles as p

    try:
        profile_dir = tmp_path / 'profiles' / 'alice'
        profile_dir.mkdir(parents=True)
        result = p.get_hermes_home_for_profile('alice')
        assert result == profile_dir
    finally:
        os.environ.pop('HERMES_BASE_HOME', None)
        for mod in ('api.config', 'api.profiles'):
            sys.modules.pop(mod, None)
        import api.profiles  # noqa: F401  — re-import with real home


def test_get_hermes_home_for_profile_falls_back_for_missing_profile(tmp_path):
    """R19c: Named profile that does not exist falls back to base home."""
    os.environ['HERMES_BASE_HOME'] = str(tmp_path)
    for mod in list(sys.modules):
        if mod in ('api.config', 'api.profiles'):
            del sys.modules[mod]
    import api.profiles as p

    try:
        result = p.get_hermes_home_for_profile('ghost')
        assert result == tmp_path
    finally:
        os.environ.pop('HERMES_BASE_HOME', None)
        for mod in ('api.config', 'api.profiles'):
            sys.modules.pop(mod, None)
        import api.profiles  # noqa: F401


def test_get_hermes_home_for_profile_does_not_mutate_globals():
    """R19d: get_hermes_home_for_profile() must never change _active_profile or os.environ."""
    import api.profiles as p

    before_active = p._active_profile
    before_hermes_home = os.environ.get('HERMES_HOME')

    p.get_hermes_home_for_profile('some-other-profile')

    assert p._active_profile == before_active, (
        "get_hermes_home_for_profile() must not mutate _active_profile"
    )
    assert os.environ.get('HERMES_HOME') == before_hermes_home, (
        "get_hermes_home_for_profile() must not mutate os.environ['HERMES_HOME']"
    )


# ── R19e-h: new_session() profile isolation ───────────────────────────────────
# These tests call new_session() directly in-process.  Session.save() would write
# to SESSION_DIR which is set from HERMES_WEBUI_STATE_DIR at import time and may
# point to a test-scoped tmp dir that has already been torn down.  We patch save()
# to a no-op — the tests only care about s.profile, not persistence.

def test_new_session_uses_explicit_profile_not_global():
    """R19e: new_session(profile='alice') stamps session.profile='alice' even when
    the process-level _active_profile is 'default'.
    Core fix for #798: client B's session is tagged to B's profile, not the global.
    """
    import api.profiles as p
    import api.models as m

    original = p._active_profile
    try:
        p._active_profile = 'default'
        with patch.object(m.Session, 'save', return_value=None):
            s = m.new_session(profile='alice')
        assert s.profile == 'alice', (
            f"Expected s.profile='alice', got {s.profile!r}. "
            "new_session() should use the explicit profile param, not the global."
        )
    finally:
        p._active_profile = original


def test_new_session_falls_back_to_global_when_profile_not_supplied():
    """R19f: new_session() without explicit profile still reads _active_profile (backward compat)."""
    import api.profiles as p
    import api.models as m

    original = p._active_profile
    try:
        p._active_profile = 'default'
        with patch.object(m.Session, 'save', return_value=None):
            s = m.new_session()
        assert s.profile == 'default'
    finally:
        p._active_profile = original


def test_new_session_none_profile_falls_back_to_global():
    """R19g: profile=None explicitly also falls back to the global (same as omitting it)."""
    import api.profiles as p
    import api.models as m

    original = p._active_profile
    try:
        p._active_profile = 'default'
        with patch.object(m.Session, 'save', return_value=None):
            s = m.new_session(profile=None)
        assert s.profile == 'default'
    finally:
        p._active_profile = original


def test_concurrent_new_sessions_get_correct_profiles():
    """R19h: Two threads call new_session() with different explicit profiles simultaneously.
    Each session must be stamped with its own profile, never the other's.
    Direct reproduction of the #798 race (minus the actual switch_profile() call).
    """
    import api.models as m

    results = {}
    errors = []

    def make_session(profile_name, key):
        try:
            with patch.object(m.Session, 'save', return_value=None):
                s = m.new_session(profile=profile_name)
            results[key] = s.profile
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=make_session, args=('alice', 'alice'))
    t2 = threading.Thread(target=make_session, args=('bob', 'bob'))
    t1.start(); t2.start()
    t1.join(timeout=5); t2.join(timeout=5)

    assert not errors, f"Threads raised: {errors}"
    assert results.get('alice') == 'alice', f"alice session had profile {results.get('alice')!r}"
    assert results.get('bob') == 'bob', f"bob session had profile {results.get('bob')!r}"


# ── R19i: sessions.js sends profile in the POST body ─────────────────────────

def test_sessions_js_sends_profile_in_new_session_post():
    """R19i: sessions.js newSession() must include profile:S.activeProfile in the
    JSON body sent to /api/session/new — the client-side half of the #798 fix."""
    js = (Path(__file__).parent.parent / 'static' / 'sessions.js').read_text()
    assert 'profile:S.activeProfile' in js or 'profile: S.activeProfile' in js, (
        "sessions.js newSession() must send profile: S.activeProfile in the POST body "
        "so the server uses the tab's active profile, not the process global."
    )
