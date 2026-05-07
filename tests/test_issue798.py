"""
Issue #798 — Profile isolation: switching profile in one browser client must not
affect sessions created by other concurrent clients.

Root cause: _active_profile was a process-level global in api/profiles.py.
Fix: new_session() now accepts an explicit `profile` param passed from the client
request body (S.activeProfile), which bypasses the shared global entirely.
get_hermes_home_for_profile() resolves a HERMES_HOME path from a name without
touching os.environ or module-level state.
"""

import json
import os
import subprocess
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


def test_get_hermes_home_for_profile_returns_profile_subdir(tmp_path, monkeypatch):
    """R19b: Named profile that exists returns its subdirectory."""
    import api.profiles as p

    profile_dir = tmp_path / 'profiles' / 'alice'
    profile_dir.mkdir(parents=True)
    monkeypatch.setattr(p, '_DEFAULT_HERMES_HOME', tmp_path)
    result = p.get_hermes_home_for_profile('alice')
    assert result == profile_dir


def test_get_hermes_home_for_profile_returns_profile_path_for_missing_profile(tmp_path, monkeypatch):
    """R19c: Named profile that does not exist on disk now returns the
    profile-scoped path (created on first use by the agent layer), NOT the
    base home. Tightened in v0.50.251 / PR #1373 to fix #1195: the previous
    is_dir() fallback caused new profiles to silently route every session
    back to the default profile until the directory existed on disk.
    Path traversal is still blocked by the _PROFILE_ID_RE regex (R19j)."""
    import api.profiles as p

    monkeypatch.setattr(p, '_DEFAULT_HERMES_HOME', tmp_path)
    result = p.get_hermes_home_for_profile('ghost')
    assert result == tmp_path / 'profiles' / 'ghost'


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


def _run_profile_resolution_probe(env):
    script = r'''
import json
from pathlib import Path
import api.profiles as p
import api.models as m

p.set_request_profile('foo')
foo_home = p.get_active_hermes_home()
explicit_foo_home = p.get_hermes_home_for_profile('foo')
foo_runtime = p.get_profile_runtime_env(explicit_foo_home)
model_home = m._get_profile_home('foo')
explicit_bar_home = p.get_hermes_home_for_profile('bar')
p.set_request_profile('bar')
active_bar_home = p.get_active_hermes_home()
print(json.dumps({
    'default_home': str(p._DEFAULT_HERMES_HOME),
    'foo_home': str(foo_home),
    'explicit_foo_home': str(explicit_foo_home),
    'foo_terminal_cwd': foo_runtime.get('TERMINAL_CWD'),
    'model_home': str(model_home),
    'explicit_bar_home': str(explicit_bar_home),
    'active_bar_home': str(active_bar_home),
}))
'''
    result = subprocess.run(
        [sys.executable, '-c', script],
        cwd=Path(__file__).parent.parent,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_hermes_base_home_named_profile_matches_cookie_without_doubling(tmp_path):
    """R19k / #749: HERMES_BASE_HOME may point directly at a named profile home.

    A single-profile WebUI deployment can start with both HERMES_BASE_HOME and
    HERMES_HOME set to /base/profiles/foo while the browser still sends the
    logical cookie hermes_profile=foo.  Both active-profile and explicit
    per-request helpers must use /base/profiles/foo, not the doubled
    /base/profiles/foo/profiles/foo path — even if that nested path already
    exists from a prior bad write.
    """
    profile_home = tmp_path / 'profiles' / 'foo'
    doubled_home = profile_home / 'profiles' / 'foo'
    doubled_home.mkdir(parents=True)
    profile_home.joinpath('config.yaml').write_text(
        'terminal:\n  cwd: /expected/profile-home\n', encoding='utf-8'
    )
    doubled_home.joinpath('config.yaml').write_text(
        'terminal:\n  cwd: /wrong/doubled-home\n', encoding='utf-8'
    )

    env = os.environ.copy()
    env.update({
        'HERMES_BASE_HOME': str(profile_home),
        'HERMES_HOME': str(profile_home),
    })
    data = _run_profile_resolution_probe(env)

    assert data['default_home'] == str(tmp_path)
    assert data['foo_home'] == str(profile_home)
    assert data['explicit_foo_home'] == str(profile_home)
    assert data['foo_terminal_cwd'] == '/expected/profile-home'
    assert data['model_home'] == str(profile_home)


def test_hermes_base_home_named_profile_nonmatching_cookie_uses_sibling_profile_path(tmp_path):
    """R19l / #749: non-matching cookies must not silently route to the pinned home.

    When HERMES_BASE_HOME is supplied as /base/profiles/foo but the request asks
    for logical profile bar, preserving base semantics means bar resolves to the
    sibling /base/profiles/bar.  It must not fall back to foo, and it must not
    append bar under foo/profiles/bar.
    """
    profile_home = tmp_path / 'profiles' / 'foo'
    profile_home.mkdir(parents=True)

    env = os.environ.copy()
    env.update({'HERMES_BASE_HOME': str(profile_home)})
    data = _run_profile_resolution_probe(env)

    expected_bar_home = tmp_path / 'profiles' / 'bar'
    assert data['explicit_bar_home'] == str(expected_bar_home)
    assert data['active_bar_home'] == str(expected_bar_home)


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

    # Patch Session.save ONCE around both threads — not once per thread.
    # Per-thread `with patch.object(...)` nested across threads has a known
    # concurrency bug in unittest.mock where one thread's __exit__ can capture
    # the other thread's mock as the "original" and leave the class attribute
    # permanently pointing at a MagicMock, breaking every later test that
    # calls Session.save (any test writing a real session file).
    def make_session(profile_name, key):
        try:
            s = m.new_session(profile=profile_name)
            results[key] = s.profile
        except Exception as exc:
            errors.append(exc)

    with patch.object(m.Session, 'save', return_value=None):
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


def test_get_hermes_home_for_profile_rejects_path_traversal():
    """R19j: get_hermes_home_for_profile() must reject names that don't match
    _PROFILE_ID_RE (e.g. path traversal like '../../etc') and return the base
    home. After v0.50.251 / PR #1373 removed the is_dir() fallback, the regex
    is the SOLE guard against path traversal — verify each known-bad shape
    still returns the base home, not a traversed path."""
    import api.profiles as p
    base = p._DEFAULT_HERMES_HOME
    assert p.get_hermes_home_for_profile('../../etc') == base
    assert p.get_hermes_home_for_profile('../escape') == base
    assert p.get_hermes_home_for_profile('/absolute/path') == base
    assert p.get_hermes_home_for_profile('has spaces') == base
    assert p.get_hermes_home_for_profile('UPPERCASE') == base
    # Valid names now route to the profile-scoped path (created on first use).
    # Previously these returned `base` because no profile dir existed on disk.
    assert p.get_hermes_home_for_profile('alice') == base / 'profiles' / 'alice'
    assert p.get_hermes_home_for_profile('my-profile') == base / 'profiles' / 'my-profile'
    assert p.get_hermes_home_for_profile('profile_1') == base / 'profiles' / 'profile_1'
    # R19j coverage gaps closed in v0.50.251 per Opus pre-release review:
    # - Trailing-newline names must be rejected (re.match would let them through;
    #   re.fullmatch correctly anchors $). Catches the match-vs-fullmatch footgun.
    assert p.get_hermes_home_for_profile('valid\n') == base
    assert p.get_hermes_home_for_profile('a\n') == base
    # - Length boundaries: 64 chars (max valid: 1 + 63 suffix) routes to profile path,
    #   65 chars rejected.
    assert p.get_hermes_home_for_profile('a' * 64) == base / 'profiles' / ('a' * 64)
    assert p.get_hermes_home_for_profile('a' * 65) == base
    # - Single-char name is the minimum valid form.
    assert p.get_hermes_home_for_profile('a') == base / 'profiles' / 'a'
    # - Non-ASCII / Unicode-trick names are rejected by the ASCII-only charset.
    assert p.get_hermes_home_for_profile('voilà') == base
    assert p.get_hermes_home_for_profile('名前') == base
