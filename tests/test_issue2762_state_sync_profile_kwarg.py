"""
Regression test for #2762 — state_sync writes to wrong profile's state.db
when profile is switched via WebUI cookie.

Root cause: ``_get_state_db()`` relied on TLS-based
``get_active_hermes_home()`` to pick the DB path. TLS gets set on the HTTP
thread by the cookie middleware, but the agent streaming worker thread that
calls ``sync_session_usage`` does NOT inherit that TLS, so the lookup falls
through to the process-global active profile and writes to the wrong DB.

Fix: ``_get_state_db(profile=...)`` accepts an explicit profile name and
resolves *that* profile's home directly via
``_resolve_profile_home_for_name``. Callers that know the session's profile
(e.g. ``sync_session_usage`` after streaming completes) pass it explicitly,
avoiding the TLS race.

These tests pin:
  1. ``_get_state_db(profile='X')`` resolves X's home, not the active profile's.
  2. ``sync_session_usage(..., profile='X')`` writes to X's state.db only,
     even when the global active profile is set to Y.
  3. ``sync_session_usage`` with no profile kwarg falls back to the old
     TLS behavior (so existing callers don't regress).
"""
from __future__ import annotations

import sys
import sqlite3
from pathlib import Path

import pytest


# Make sure we can import the api package the same way the server does.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture()
def two_profile_homes(tmp_path, monkeypatch):
    """Stand up two minimal profile homes with state.db initialized via
    ``hermes_state.SessionDB`` itself (so the schema matches what the
    production code expects — `sync_session_usage` does a raw-SQL
    UPDATE of `message_count`, which hand-rolled schemas could miss).
    Per Copilot review on PR #2827.
    """
    # Skip the fixture cleanly if the production package isn't importable
    # in this env — same gate the tests below use.
    pytest.importorskip("hermes_state")
    from hermes_state import SessionDB

    hiyuki_home = tmp_path / 'hiyuki'
    maiko_home = tmp_path / 'maiko'
    for home in (hiyuki_home, maiko_home):
        home.mkdir(parents=True)
        # Touch state.db then open via SessionDB so its own constructor
        # runs whatever schema init / migration the production code
        # would see at runtime. Then close immediately — each test
        # opens its own handle through the production code path.
        (home / 'state.db').touch()
        SessionDB(home / 'state.db').close()

    # Stub api.profiles to return our temp paths
    import api.profiles as profiles_mod

    def fake_resolve(name):
        if name == 'hiyuki':
            return hiyuki_home
        if name == 'maiko':
            return maiko_home
        raise LookupError(name)

    monkeypatch.setattr(profiles_mod, '_resolve_profile_home_for_name', fake_resolve)
    # Active profile is hiyuki — the WRONG one for tests that pass profile='maiko'
    monkeypatch.setattr(profiles_mod, 'get_active_hermes_home', lambda: hiyuki_home)

    return {'hiyuki': hiyuki_home, 'maiko': maiko_home}


def _read_session(db_path: Path, session_id: str):
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        # Real state.db schema (see api/state_sync.py + hermes_cli StateDB):
        # `sessions` table has `id` as PRIMARY KEY (not session_id). Use real
        # column names so the test queries the actual schema.
        cur = conn.execute(
            "SELECT id AS session_id, title, input_tokens, output_tokens "
            "FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        return row
    finally:
        conn.close()


def test_get_state_db_honors_explicit_profile_kwarg(two_profile_homes):
    """_get_state_db(profile='maiko') resolves to maiko's home, NOT
    the active profile (hiyuki)."""
    from api.state_sync import _get_state_db

    # Some installs ship without the hermes_state package; the function
    # returns None gracefully and there's nothing to assert.
    try:
        import hermes_state  # noqa: F401
    except ImportError:
        pytest.skip("hermes_state package not available in this test env")

    db = _get_state_db(profile='maiko')
    if db is None:
        pytest.skip("SessionDB could not open the test db (env issue)")
    # We don't have a public accessor for the underlying path on SessionDB,
    # but writing through it and reading the raw file should work.
    db.ensure_session(session_id='probe-2762', source='webui', model='test')
    try:
        db.close()
    except Exception:
        pass

    # maiko's state.db should have the row; hiyuki's should not.
    assert _read_session(two_profile_homes['maiko'] / 'state.db', 'probe-2762') is not None, \
        "session was not written to maiko's state.db"
    assert _read_session(two_profile_homes['hiyuki'] / 'state.db', 'probe-2762') is None, \
        "session leaked into hiyuki's state.db — TLS-fallback regressed"


def test_sync_session_usage_writes_only_to_named_profile(two_profile_homes):
    """sync_session_usage(..., profile='maiko') is the actual scenario from
    the streaming worker thread post-#2762. The write must land in maiko's
    state.db only, regardless of what the global active profile is."""
    try:
        import hermes_state  # noqa: F401
    except ImportError:
        pytest.skip("hermes_state package not available in this test env")

    from api.state_sync import sync_session_usage

    sync_session_usage(
        session_id='2762-regression',
        input_tokens=42,
        output_tokens=17,
        estimated_cost=0.001,
        model='test-model',
        title='2762 regression test',
        message_count=3,
        profile='maiko',
    )

    maiko_row = _read_session(two_profile_homes['maiko'] / 'state.db', '2762-regression')
    hiyuki_row = _read_session(two_profile_homes['hiyuki'] / 'state.db', '2762-regression')

    assert maiko_row is not None, \
        "sync_session_usage(profile='maiko') did not write to maiko's state.db"
    assert hiyuki_row is None, \
        "sync_session_usage(profile='maiko') leaked into hiyuki's state.db — #2762 regression"


def test_sync_session_usage_without_profile_kwarg_uses_active(two_profile_homes):
    """Backward compatibility: when called without a profile kwarg (the
    pre-#2762 call shape), the function falls back to the active profile
    (here: hiyuki). Existing callers should not regress."""
    try:
        import hermes_state  # noqa: F401
    except ImportError:
        pytest.skip("hermes_state package not available in this test env")

    from api.state_sync import sync_session_usage

    sync_session_usage(
        session_id='legacy-call-shape',
        input_tokens=1,
        output_tokens=2,
        model='legacy',
        title='legacy',
        message_count=1,
    )

    hiyuki_row = _read_session(two_profile_homes['hiyuki'] / 'state.db', 'legacy-call-shape')
    assert hiyuki_row is not None, \
        "sync_session_usage() without profile= regressed: did not write to active profile's state.db"


def test_unknown_explicit_profile_returns_none_not_falls_back(two_profile_homes):
    """Copilot review of PR #2827: when ``profile`` is explicit and
    resolution fails (e.g. typoed profile name, IO error), the
    function MUST return None rather than silently fall back to
    HERMES_HOME and write to the wrong DB. That fallback would
    re-introduce the exact #2762 symptom (writes leaking into the
    active profile).

    The fixture's `fake_resolve` raises LookupError for any name
    that isn't 'hiyuki' or 'maiko', so passing 'does-not-exist'
    here exercises the failure path.
    """
    try:
        import hermes_state  # noqa: F401
    except ImportError:
        pytest.skip("hermes_state package not available in this test env")

    from api.state_sync import sync_session_usage

    # Passing an unknown profile name MUST NOT cause a write to land in
    # hiyuki (the active profile's home). If we leaked there, that's
    # the exact bug we're guarding against.
    sync_session_usage(
        session_id='unknown-profile-probe',
        input_tokens=99,
        output_tokens=99,
        model='probe',
        title='probe',
        message_count=1,
        profile='does-not-exist',
    )

    hiyuki_row = _read_session(two_profile_homes['hiyuki'] / 'state.db', 'unknown-profile-probe')
    maiko_row = _read_session(two_profile_homes['maiko'] / 'state.db', 'unknown-profile-probe')

    assert hiyuki_row is None, (
        "unknown explicit profile leaked write into hiyuki's state.db — #2762 regression"
    )
    assert maiko_row is None, (
        "unknown explicit profile somehow ended up in maiko's state.db"
    )


@pytest.mark.parametrize("bad_name", [
    "../etc",       # path traversal attempt
    "Foo Bar",      # space — invalid chars
    "FOO",          # uppercase — invalid per _PROFILE_ID_RE
    "-leading-dash",  # leading dash — invalid per regex (must start [a-z0-9])
    "_underscore",  # leading underscore — invalid per regex
    "a" * 100,      # too long (> 64 chars)
    "",             # empty string is handled by _is_root_profile, separate case
])
def test_invalid_profile_name_refused_not_falls_back(two_profile_homes, bad_name):
    """Per PR #2827 maintainer review: an invalid-but-non-malicious
    profile name on the explicit-profile path must be REFUSED, not
    quietly routed to the default state.db.

    Before this defense, ``_resolve_profile_home_for_name`` would return
    ``_DEFAULT_HERMES_HOME`` for any name failing ``_PROFILE_ID_RE``
    without raising — which is the exact #2762 leak symptom with a
    different trigger. The new regex check up-front turns that quiet
    leak into an explicit "refuse + log + return None" so the
    explicit-path contract is "write to the EXACT named profile, or
    write nowhere."

    The empty string is intentionally in the parametrize set because
    we want to confirm it's refused — ``_is_root_profile('')`` returns
    False (per ``api/profiles.py:216-217`` it short-circuits on falsy
    input), so an empty explicit profile fails both the
    ``_is_root_profile`` check and the regex, and the contract refuses
    the write. That's the expected behavior — an empty explicit name
    is itself a bug at the caller, not "I want the default."
    """
    try:
        import hermes_state  # noqa: F401
    except ImportError:
        pytest.skip("hermes_state package not available in this test env")

    from api.state_sync import sync_session_usage

    sync_session_usage(
        session_id=f'invalid-name-probe-{abs(hash(bad_name))}',
        input_tokens=99,
        output_tokens=99,
        model='probe',
        title='probe',
        message_count=1,
        profile=bad_name,
    )

    sid = f'invalid-name-probe-{abs(hash(bad_name))}'
    hiyuki_row = _read_session(two_profile_homes['hiyuki'] / 'state.db', sid)
    maiko_row = _read_session(two_profile_homes['maiko'] / 'state.db', sid)

    # All invalid names (including empty string) MUST be refused —
    # no row should appear in either profile's state.db.
    assert hiyuki_row is None, (
        f"invalid profile name {bad_name!r} leaked write into hiyuki's "
        "state.db — defense missed; #2762 regression"
    )
    assert maiko_row is None, (
        f"invalid profile name {bad_name!r} somehow ended up in maiko's state.db"
    )
