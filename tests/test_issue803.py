"""
Issue #803 (completes #798) — per-client profile isolation via cookie + thread-local.

PR #800 fixed POST /api/session/new (client sends profile in body).
PR #805 extends the fix to ALL endpoints: profile switches set a hermes_profile
cookie, server.py reads it per-request into a thread-local, and the existing
api/profiles.py helpers consult the thread-local before the process global.

Covers:
  1. build_profile_cookie() / get_profile_cookie() roundtrip + validation
  2. set_request_profile() / get_active_profile_name() / clear_request_profile()
  3. get_active_hermes_home() routes via thread-local
  4. switch_profile(process_wide=False) does NOT mutate process globals
  5. Concurrent requests on different threads see independent profiles
"""
import logging
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── 1. Cookie build/parse roundtrip ──────────────────────────────────────────

class TestProfileCookieHelpers:

    def test_build_profile_cookie_sets_value(self):
        from api.helpers import build_profile_cookie
        s = build_profile_cookie('alice')
        assert 'hermes_profile=alice' in s
        assert 'HttpOnly' in s
        assert 'SameSite=Lax' in s
        assert 'Path=/' in s

    def test_build_profile_cookie_default_persists(self):
        from api.helpers import build_profile_cookie
        s = build_profile_cookie('default')
        assert 'hermes_profile=default' in s
        assert 'Max-Age=0' not in s

    def test_get_profile_cookie_returns_none_when_absent(self):
        from api.helpers import get_profile_cookie
        handler = MagicMock()
        handler.headers.get = lambda k, d='': ''
        assert get_profile_cookie(handler) is None

    def test_get_profile_cookie_extracts_valid_name(self, monkeypatch):
        from api.helpers import get_profile_cookie
        monkeypatch.setattr('api.auth.is_auth_enabled', lambda: False)
        handler = MagicMock()
        handler.headers.get = lambda k, d='': 'hermes_profile=alice' if k == 'Cookie' else d
        assert get_profile_cookie(handler) == 'alice'

    def test_get_profile_cookie_requires_session_bound_signature_when_auth_enabled(self, monkeypatch):
        from api.auth import sign_profile_cookie_value
        from api.helpers import get_profile_cookie

        session_cookie = 'session-token.session-sig'
        monkeypatch.setattr('api.auth.is_auth_enabled', lambda: True)
        monkeypatch.setattr('api.auth.verify_session', lambda cookie: cookie == session_cookie)
        signed_profile = sign_profile_cookie_value('alice', session_cookie)

        handler = MagicMock()
        handler.headers.get = lambda k, d='': (
            f'hermes_session={session_cookie}; hermes_profile={signed_profile}' if k == 'Cookie' else d
        )
        assert get_profile_cookie(handler) == 'alice'

    def test_get_profile_cookie_rejects_unsigned_profile_when_auth_enabled(self, monkeypatch):
        from api.helpers import get_profile_cookie

        monkeypatch.setattr('api.auth.is_auth_enabled', lambda: True)
        handler = MagicMock()
        handler.headers.get = lambda k, d='': (
            'hermes_session=session-token.session-sig; hermes_profile=alice' if k == 'Cookie' else d
        )
        assert get_profile_cookie(handler) is None

    def test_get_profile_cookie_rejects_profile_signed_for_another_session(self, monkeypatch):
        from api.auth import sign_profile_cookie_value
        from api.helpers import get_profile_cookie

        other_session = 'other-token.other-sig'
        current_session = 'session-token.session-sig'
        monkeypatch.setattr('api.auth.verify_session', lambda cookie: cookie in {other_session, current_session})
        signed_profile = sign_profile_cookie_value('alice', other_session)
        monkeypatch.setattr('api.auth.is_auth_enabled', lambda: True)
        handler = MagicMock()
        handler.headers.get = lambda k, d='': (
            f'hermes_session={current_session}; hermes_profile={signed_profile}' if k == 'Cookie' else d
        )
        assert get_profile_cookie(handler) is None

    def test_build_profile_cookie_binds_to_auth_session_when_auth_enabled(self, monkeypatch):
        from api.auth import verify_profile_cookie_value
        from api.helpers import build_profile_cookie

        session_cookie = 'session-token.session-sig'
        monkeypatch.setattr('api.auth.is_auth_enabled', lambda: True)
        monkeypatch.setattr('api.auth.verify_session', lambda cookie: cookie == session_cookie)
        handler = MagicMock()
        handler.headers.get = lambda k, d='': f'hermes_session={session_cookie}' if k == 'Cookie' else d

        cookie = build_profile_cookie('alice', handler)
        value = cookie.split('hermes_profile=', 1)[1].split(';', 1)[0]
        assert value != 'alice'
        assert verify_profile_cookie_value(value, session_cookie) == 'alice'

    def test_sign_profile_cookie_requires_active_session(self, monkeypatch):
        from api.auth import sign_profile_cookie_value

        monkeypatch.setattr('api.auth.verify_session', lambda cookie: False)
        with pytest.raises(ValueError):
            sign_profile_cookie_value('alice', 'expired-token.session-sig')

    def test_verify_profile_cookie_rejects_expired_session(self, monkeypatch):
        from api.auth import sign_profile_cookie_value, verify_profile_cookie_value

        session_cookie = 'session-token.session-sig'
        monkeypatch.setattr('api.auth.verify_session', lambda cookie: True)
        signed_profile = sign_profile_cookie_value('alice', session_cookie)

        monkeypatch.setattr('api.auth.verify_session', lambda cookie: False)
        assert verify_profile_cookie_value(signed_profile, session_cookie) is None

    def test_build_profile_cookie_fails_closed_when_auth_session_missing(self, monkeypatch, caplog):
        from api.helpers import build_profile_cookie

        monkeypatch.setattr('api.auth.is_auth_enabled', lambda: True)
        monkeypatch.setattr('api.auth.verify_session', lambda cookie: False)
        handler = MagicMock()
        handler.headers.get = lambda k, d='': ''

        with pytest.raises(RuntimeError):
            build_profile_cookie('alice', handler)
        assert 'Failed to sign active profile cookie' in caplog.text

    def test_get_profile_cookie_accepts_default(self, monkeypatch):
        from api.helpers import get_profile_cookie
        monkeypatch.setattr('api.auth.is_auth_enabled', lambda: False)
        handler = MagicMock()
        handler.headers.get = lambda k, d='': 'hermes_profile=default' if k == 'Cookie' else d
        assert get_profile_cookie(handler) == 'default'

    def test_get_profile_cookie_rejects_injection(self, monkeypatch):
        """Cookie value must pass _PROFILE_ID_RE fullmatch — rejects traversal/injection."""
        from api.helpers import get_profile_cookie
        monkeypatch.setattr('api.auth.is_auth_enabled', lambda: False)
        for bad in ('../etc', 'a/b', 'name;DROP', 'WithCaps', 'has space', '.hidden'):
            handler = MagicMock()
            handler.headers.get = lambda k, d='', v=bad: f'hermes_profile={v}' if k == 'Cookie' else d
            assert get_profile_cookie(handler) is None, f"{bad!r} should be rejected"

    def test_get_profile_cookie_ignores_malformed_header(self):
        from api.helpers import get_profile_cookie
        handler = MagicMock()
        handler.headers.get = lambda k, d='': '\x00\x01not-a-cookie' if k == 'Cookie' else d
        # Must not raise; returns None
        result = get_profile_cookie(handler)
        assert result is None

    def test_profile_cookie_name_defaults_to_hermes_profile(self, monkeypatch):
        from api.helpers import build_profile_cookie

        monkeypatch.delenv('WEBUI_PROFILE_COOKIE_NAME', raising=False)

        s = build_profile_cookie('alice')
        assert 'hermes_profile=alice' in s

    def test_profile_cookie_name_can_be_isolated_per_webui_instance(self, monkeypatch):
        from api.helpers import build_profile_cookie, get_profile_cookie

        monkeypatch.setenv('WEBUI_PROFILE_COOKIE_NAME', 'hermes_profile_social')
        monkeypatch.setattr('api.auth.is_auth_enabled', lambda: False)

        s = build_profile_cookie('writer')
        assert 'hermes_profile_social=writer' in s
        assert 'hermes_profile=writer' not in s

        handler = MagicMock()
        handler.headers.get = lambda k, d='': (
            'hermes_profile=wrong; hermes_profile_social=writer' if k == 'Cookie' else d
        )
        assert get_profile_cookie(handler) == 'writer'

    def test_verify_profile_cookie_rejects_invalid_name_pattern(self, monkeypatch):
        """Defense-in-depth (#4023 Opus hardening): even a correctly-HMAC-signed
        cookie whose profile name fails _PROFILE_ID_RE must be rejected by the
        verifier itself, so a future caller can't skip the pattern gate."""
        from api.auth import sign_profile_cookie_value, verify_profile_cookie_value

        session_cookie = 'session-token.session-sig'
        monkeypatch.setattr('api.auth.verify_session', lambda cookie: cookie == session_cookie)
        # Sign a hostile name (would never come from a real switch, but proves the
        # verifier validates the name even when the signature is valid).
        signed = sign_profile_cookie_value('../etc', session_cookie)
        assert verify_profile_cookie_value(signed, session_cookie) is None
        # And a normal name still round-trips.
        ok = sign_profile_cookie_value('alice', session_cookie)
        assert verify_profile_cookie_value(ok, session_cookie) == 'alice'

    def test_build_profile_cookie_requires_handler_when_auth_enabled(self, monkeypatch):
        """Defense-in-depth (#4023 Opus hardening): a future call site that forgets
        to pass the handler while auth is enabled must NOT silently emit an
        unsigned profile cookie — it raises instead."""
        from api.helpers import build_profile_cookie

        monkeypatch.setattr('api.auth.is_auth_enabled', lambda: True)
        with pytest.raises(RuntimeError):
            build_profile_cookie('alice')  # no handler

    def test_build_profile_cookie_allows_no_handler_when_auth_disabled(self, monkeypatch):
        """No-auth mode keeps the legacy plain-name cookie with no handler."""
        from api.helpers import build_profile_cookie

        monkeypatch.delenv('WEBUI_PROFILE_COOKIE_NAME', raising=False)
        monkeypatch.setattr('api.auth.is_auth_enabled', lambda: False)
        s = build_profile_cookie('alice')
        assert 'hermes_profile=alice' in s

    def test_configured_profile_cookie_ignores_default_cookie_name(self, monkeypatch):
        from api.helpers import get_profile_cookie

        monkeypatch.setenv('WEBUI_PROFILE_COOKIE_NAME', 'hermes_profile_main')
        monkeypatch.setattr('api.auth.is_auth_enabled', lambda: False)

        handler = MagicMock()
        handler.headers.get = lambda k, d='': 'hermes_profile=social_profile' if k == 'Cookie' else d
        assert get_profile_cookie(handler) is None


# ── 1b. Profile cookie name resolution (env > legacy env > default) ───────────

class TestProfileCookieNameResolution:

    def test_default_when_unset(self, monkeypatch):
        from api.helpers import PROFILE_COOKIE_NAME, get_profile_cookie_name
        monkeypatch.delenv('HERMES_WEBUI_PROFILE_COOKIE_NAME', raising=False)
        monkeypatch.delenv('WEBUI_PROFILE_COOKIE_NAME', raising=False)
        assert get_profile_cookie_name() == PROFILE_COOKIE_NAME

    def test_canonical_env_overrides_default(self, monkeypatch):
        from api.helpers import get_profile_cookie_name
        monkeypatch.delenv('WEBUI_PROFILE_COOKIE_NAME', raising=False)
        monkeypatch.setenv('HERMES_WEBUI_PROFILE_COOKIE_NAME', 'hermes_profile_alt')
        assert get_profile_cookie_name() == 'hermes_profile_alt'

    def test_legacy_env_still_honoured(self, monkeypatch):
        from api.helpers import get_profile_cookie_name
        monkeypatch.delenv('HERMES_WEBUI_PROFILE_COOKIE_NAME', raising=False)
        monkeypatch.setenv('WEBUI_PROFILE_COOKIE_NAME', 'hermes_profile_legacy')
        assert get_profile_cookie_name() == 'hermes_profile_legacy'

    def test_canonical_takes_precedence_over_legacy(self, monkeypatch):
        from api.helpers import get_profile_cookie_name
        monkeypatch.setenv('HERMES_WEBUI_PROFILE_COOKIE_NAME', 'canonical')
        monkeypatch.setenv('WEBUI_PROFILE_COOKIE_NAME', 'legacy')
        assert get_profile_cookie_name() == 'canonical'

    def test_blank_canonical_falls_back_to_legacy(self, monkeypatch):
        from api.helpers import get_profile_cookie_name
        monkeypatch.setenv('HERMES_WEBUI_PROFILE_COOKIE_NAME', '   ')
        monkeypatch.setenv('WEBUI_PROFILE_COOKIE_NAME', 'hermes_profile_legacy')
        assert get_profile_cookie_name() == 'hermes_profile_legacy'

    def test_blank_envs_fall_back_to_default(self, monkeypatch):
        from api.helpers import PROFILE_COOKIE_NAME, get_profile_cookie_name
        monkeypatch.setenv('HERMES_WEBUI_PROFILE_COOKIE_NAME', '   ')
        monkeypatch.setenv('WEBUI_PROFILE_COOKIE_NAME', '')
        assert get_profile_cookie_name() == PROFILE_COOKIE_NAME

    def test_legacy_deprecation_warns_only_once(self, monkeypatch, caplog):
        # get_profile_cookie_name() runs on every request, so the deprecation
        # warning for the legacy env var must be emitted once per process.
        import api.helpers as helpers
        monkeypatch.delenv('HERMES_WEBUI_PROFILE_COOKIE_NAME', raising=False)
        monkeypatch.setenv('WEBUI_PROFILE_COOKIE_NAME', 'hermes_profile_legacy')
        monkeypatch.setattr(helpers, '_legacy_profile_cookie_warned', False)
        with caplog.at_level(logging.WARNING, logger='api.helpers'):
            for _ in range(3):
                assert helpers.get_profile_cookie_name() == 'hermes_profile_legacy'
        warned = [r for r in caplog.records if 'deprecated' in r.getMessage()]
        assert len(warned) == 1


# ── 2. Thread-local request context ──────────────────────────────────────────

class TestThreadLocalProfileContext:

    def test_tls_takes_priority_over_global(self):
        import api.profiles as p
        original = p._active_profile
        try:
            p._active_profile = 'global-default'
            p.set_request_profile('alice')
            assert p.get_active_profile_name() == 'alice'
        finally:
            p.clear_request_profile()
            p._active_profile = original

    def test_global_used_when_tls_cleared(self):
        import api.profiles as p
        original = p._active_profile
        try:
            p._active_profile = 'global-default'
            p.set_request_profile('alice')
            p.clear_request_profile()
            assert p.get_active_profile_name() == 'global-default'
        finally:
            p._active_profile = original

    def test_clear_is_idempotent(self):
        import api.profiles as p
        # Calling clear on a thread that never set anything must not raise
        p.clear_request_profile()
        p.clear_request_profile()


# ── 3. get_active_hermes_home routes through TLS ─────────────────────────────

def test_get_active_hermes_home_respects_tls(tmp_path, monkeypatch):
    import api.profiles as p
    monkeypatch.setattr(p, '_DEFAULT_HERMES_HOME', tmp_path)
    profile_dir = tmp_path / 'profiles' / 'alice'
    profile_dir.mkdir(parents=True)
    try:
        p.set_request_profile('alice')
        assert p.get_active_hermes_home() == profile_dir
        p.set_request_profile('default')
        assert p.get_active_hermes_home() == tmp_path
    finally:
        p.clear_request_profile()


# ── 4. switch_profile(process_wide=False) does not mutate globals ─────────────

def test_switch_profile_process_wide_false_does_not_mutate_global():
    """Per-client switches from the WebUI must leave _active_profile untouched."""
    import api.profiles as p

    # Monkey in a fake profile listing so switch_profile finds 'alice'
    original_global = p._active_profile
    original_env_home = os.environ.get('HERMES_HOME')

    # We need a profile that exists to get past the validation path.
    # Use 'default' — switch_profile accepts it without requiring hermes_cli.
    try:
        result = p.switch_profile('default', process_wide=False)
        # Global must not change
        assert p._active_profile == original_global, (
            f"process_wide=False must not mutate _active_profile "
            f"(was {original_global!r}, now {p._active_profile!r})"
        )
        # HERMES_HOME env must not change
        assert os.environ.get('HERMES_HOME') == original_env_home, (
            "process_wide=False must not mutate os.environ['HERMES_HOME']"
        )
        # Response still shape-compatible
        assert isinstance(result, dict)
    finally:
        p._active_profile = original_global


# ── 5. Concurrent threads see independent profile context ────────────────────

def test_concurrent_threads_see_independent_profiles():
    """The whole point of thread-local isolation: two threads, two cookies,
    two different get_active_profile_name() results, simultaneously."""
    import api.profiles as p

    results = {}
    errors = []
    barrier = threading.Barrier(2, timeout=5)

    def worker(name, key):
        try:
            p.set_request_profile(name)
            barrier.wait()  # both threads have set their TLS
            # Now each thread reads — must see its own value
            results[key] = p.get_active_profile_name()
            p.clear_request_profile()
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=('alice', 'alice'))
    t2 = threading.Thread(target=worker, args=('bob', 'bob'))
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)

    assert not errors, f"Workers raised: {errors}"
    assert results.get('alice') == 'alice', f"alice thread saw {results.get('alice')!r}"
    assert results.get('bob') == 'bob', f"bob thread saw {results.get('bob')!r}"
