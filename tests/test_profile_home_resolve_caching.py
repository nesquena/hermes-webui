"""Regression test: `_resolve_profile_home_param` memoizes its filesystem
`.resolve()` call, but ONLY for the duration of a single
`profile_home_resolve_cache_scope()` (currently wrapping
`_load_cli_sessions_uncached`, the sidebar's session-list build).

Building the CLI session list calls `_resolve_profile_home_param(profile)`
once per session row (`Session.__init__`), so with hundreds of sessions on
the same profile the unscoped code redundantly re-resolved the identical
path via a filesystem `.resolve()` on every single row.

An earlier revision of this fix cached the result in a plain process-lifetime
module dict. That was flagged in PR #7636 review: `Path.resolve()` is a
filesystem call, not a pure function of process-startup constants -- a
profile-home symlink can be retargeted while the webui process keeps
running, and `_safe_resolve()` deliberately falls back to the UNRESOLVED
input after a transient error, so a process-lifetime cache could serve a
stale or wrong path for the rest of the process's life.

This file tests the corrected, call-scoped design:
  (a) within one scope, repeated resolves of the same argument hit the cache
      (single underlying `_safe_resolve()` call) -- same benefit as before.
  (b) across two SEPARATE scopes (simulating two different requests/list
      builds), the cache does NOT persist -- `_safe_resolve()` runs again
      fresh on the second scope, proving no cross-request staleness.
  (c) outside any scope, `_resolve_profile_home_param` resolves fresh every
      time with no caching at all, matching pre-PR behavior exactly.
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.workspace as ws  # noqa: E402
import api.models as models  # noqa: E402


def test_resolve_profile_home_param_caches_within_one_scope(tmp_path):
    """(a) Same as the original test, but the cache now requires an active scope."""
    home_dir = tmp_path / "profile_home"
    home_dir.mkdir()

    with patch.object(ws, "_safe_resolve", wraps=ws._safe_resolve) as spy:
        with ws.profile_home_resolve_cache_scope():
            first = ws._resolve_profile_home_param(home_dir)
            second = ws._resolve_profile_home_param(home_dir)
            third = ws._resolve_profile_home_param(home_dir)

    assert first == second == third == home_dir.resolve()
    assert spy.call_count == 1, (
        "expected the filesystem resolve() to run once within one scope and "
        f"be served from cache thereafter, but it ran {spy.call_count} times"
    )


def test_resolve_profile_home_param_caches_within_one_scope_for_profile_name(tmp_path):
    """(a), second branch: a logical profile-NAME STRING (not a Path) resolves

    through `get_hermes_home_for_profile()` before hitting the same
    `_cached_safe_resolve_profile_home()` memoization. Confirms the
    within-scope cache is genuinely hit for named profiles too, not just
    explicit Path callers.
    """
    home_dir = tmp_path / "named_profile_home"
    home_dir.mkdir()

    with patch("api.profiles.get_hermes_home_for_profile", return_value=home_dir), \
            patch.object(ws, "_safe_resolve", wraps=ws._safe_resolve) as spy:
        with ws.profile_home_resolve_cache_scope():
            first = ws._resolve_profile_home_param("myprofile")
            second = ws._resolve_profile_home_param("myprofile")
            third = ws._resolve_profile_home_param("myprofile")

    assert first == second == third == home_dir.resolve()
    assert spy.call_count == 1, (
        "expected the filesystem resolve() to run once within one scope and "
        f"be served from cache thereafter, but it ran {spy.call_count} times"
    )


def test_cache_does_not_persist_across_separate_scopes(tmp_path):
    """(b) Two separate scoped calls (simulating two different requests) must

    NOT share a cache -- the whole point of the PR #7636 review fix. If a
    symlink were retargeted between the two "requests", the second scope
    must be free to observe the new target; that's only possible if
    `_safe_resolve()` is invoked again fresh on scope #2.
    """
    home_dir = tmp_path / "profile_home"
    home_dir.mkdir()

    with patch.object(ws, "_safe_resolve", wraps=ws._safe_resolve) as spy:
        with ws.profile_home_resolve_cache_scope():
            ws._resolve_profile_home_param(home_dir)
            ws._resolve_profile_home_param(home_dir)
        assert spy.call_count == 1

        # Second, separate scope -- simulates a later, independent request.
        with ws.profile_home_resolve_cache_scope():
            ws._resolve_profile_home_param(home_dir)
            ws._resolve_profile_home_param(home_dir)

    assert spy.call_count == 2, (
        "expected the second scope to re-resolve fresh (no cross-scope/"
        f"cross-request caching), but _safe_resolve ran {spy.call_count} times total"
    )


def test_no_caching_outside_any_scope(tmp_path):
    """(c) Outside a scope, every call resolves fresh -- matching pre-PR

    behavior exactly, since most call sites of `_resolve_profile_home_param`
    are NOT the sidebar list-build hot loop and must never see a cached
    (possibly stale) result.
    """
    home_dir = tmp_path / "profile_home"
    home_dir.mkdir()

    with patch.object(ws, "_safe_resolve", wraps=ws._safe_resolve) as spy:
        first = ws._resolve_profile_home_param(home_dir)
        second = ws._resolve_profile_home_param(home_dir)
        third = ws._resolve_profile_home_param(home_dir)

    assert first == second == third == home_dir.resolve()
    assert spy.call_count == 3, (
        "expected every call outside an active scope to resolve fresh with "
        f"no caching, but _safe_resolve ran {spy.call_count} times for 3 calls"
    )


def test_scope_decorator_use_matches_load_cli_sessions_uncached_wiring():
    """Sanity check that `profile_home_resolve_cache_scope()` is usable as a

    decorator (the way it wraps `_load_cli_sessions_uncached` in
    `api/models.py`), and that the ContextVar is active only during the
    decorated call.
    """
    assert ws._PROFILE_HOME_RESOLVE_SCOPE.get() is None

    @ws.profile_home_resolve_cache_scope()
    def _inside():
        return ws._PROFILE_HOME_RESOLVE_SCOPE.get()

    cache_during_call = _inside()
    assert cache_during_call is not None
    assert ws._PROFILE_HOME_RESOLVE_SCOPE.get() is None


def _make_cli_sessions_uncached_fixture(tmp_path, n, *, message_count=3):
    """state.db + matching MODERN WebUI sidecars (message_count present in the
    prefix, literal 'default' profile string -- exactly what real production
    sidecars carry, confirmed against a live deployment's webui/sessions/*.json)
    for `n` cron rows.

    A "modern" sidecar (one that carries `message_count` in its JSON-prefix)
    is required to reach the fast branch of `Session.load_metadata_only()`
    (a single `Session(**parsed)` construction) instead of falling back to the
    legacy `cls.load()` re-parse path -- see #5854. That keeps this fixture
    isolated to the exact N+1 call shape under test: one `Session.__init__`
    (and therefore one `_resolve_profile_home_param` call) per sidecar row.
    """
    import sqlite3
    import time

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE INDEX idx_sessions_started ON sessions(started_at);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    now = time.time()
    sids = []
    for i in range(n):
        sid = f"cron_job{i:04d}_{int(now) + i}"
        sids.append(sid)
        conn.execute(
            "INSERT INTO sessions (id, source, session_source, title, model,"
            " started_at, message_count, parent_session_id, ended_at, end_reason)"
            " VALUES (?, 'cron', 'cron', NULL, 'deepseek/deepseek-chat', ?, 1,"
            " NULL, NULL, NULL)",
            (sid, now + i),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp)"
            " VALUES (?, ?, 'user', 'cron task', ?)",
            (f"cron_msg_{sid}", sid, now + i),
        )
    conn.commit()
    conn.close()

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    for sid in sids:
        (session_dir / f"{sid}.json").write_text(
            '{"session_id": "%s", "title": "Renamed %s", "created_at": 1.0,'
            ' "updated_at": 2.0, "archived": false, "message_count": %d,'
            ' "profile": "default", "messages": []}'
            % (sid, sid, message_count),
            encoding="utf-8",
        )
    return db_path, sids


def _count_profile_home_resolve_calls(call_args_list, expected_profile_home):
    """Filter a `_safe_resolve` spy's call list down to just the profile-home

    branch (`pre_resolve == expected_profile_home`), excluding the unrelated
    per-row `_safe_resolve()` call that resolves the session's `workspace`
    path (same call count either way -- not part of this PR, not what
    greptile's comment or the production stack trace was about).
    """
    return sum(
        1 for call in call_args_list
        if call.args and call.args[0] == expected_profile_home
    )


def test_load_cli_sessions_uncached_dedupes_profile_home_resolve_on_sidecar_cache_miss(tmp_path):
    """Real N+1 reproduction for PR #7636 review comment (greptile,
    api/models.py:7711): on a COLD `_SIDECAR_METADATA_CACHE` (fresh process,
    or the first time each sid's sidecar is seen), `_state_projection_sidecar_metadata`
    falls through to `Session.load_metadata_only(sid)` for EVERY row that has a
    sidecar -- and that constructs a real `Session` via `Session.__init__`,
    which resolves the session's `profile` param through
    `_resolve_path -> _remote_terminal_workspace_candidate ->
    _remote_terminal_cwd -> _resolve_profile_home_param -> _safe_resolve()`,
    matching the exact stack trace captured in production
    ("Slow WebUI request still running", 5-17s, pinned to this call site).
    Real production sidecars carry the literal string "default" for `profile`
    (confirmed against a live deployment's webui/sessions/*.json), which is
    what this fixture uses too.

    This is a DIFFERENT code path than the direct dict-projection rows
    greptile's comment focused on -- it fires from the sidecar-metadata
    overlay, not from the row-to-dict projection itself. It IS covered by
    `@profile_home_resolve_cache_scope()` on `_load_cli_sessions_uncached`,
    because contextvars propagate through ordinary synchronous nested calls
    (no thread pool / executor / asyncio task is involved anywhere in this
    call chain) -- proven here by counting only the profile-home-resolving
    `_safe_resolve()` invocations (filtered by argument), not the session's
    unrelated per-row workspace-path resolve, which this PR never touched and
    still runs once per row regardless.
    """
    from unittest import mock

    import api.profiles as profiles
    import api.workspace as ws

    models.clear_sidecar_metadata_cache()
    n = 60
    db_path, sids = _make_cli_sessions_uncached_fixture(tmp_path, n)

    with (
        mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        mock.patch("api.models.get_last_workspace", return_value=str(tmp_path)),
        mock.patch("api.models.ensure_cron_project", return_value="cron-pid"),
        mock.patch("api.models.SESSION_DIR", tmp_path / "sessions"),
        # Isolate the measurement: `_remote_terminal_cwd()` also calls
        # `get_config_for_profile_home()` right after `_resolve_profile_home_param()`,
        # and THAT helper does its own two direct, uncached `_safe_resolve()` calls
        # on the same profile-home path (api/config.py, a pre-existing, separate
        # hot path this PR does not touch and greptile's comment did not raise).
        # Left unmocked those calls alias onto the same argument value and would
        # make an unrelated inefficiency look like a failure of THIS cache.
        mock.patch("api.config.get_config_for_profile_home", return_value={}),
        mock.patch.object(ws, "_safe_resolve", wraps=ws._safe_resolve) as safe_resolve_spy,
    ):
        result = models._load_cli_sessions_uncached(tmp_path, db_path, None)

    assert len(result) == n
    profile_home_resolves = _count_profile_home_resolve_calls(
        safe_resolve_spy.call_args_list, profiles._DEFAULT_HERMES_HOME
    )
    # Every row's sidecar was a genuine cache miss (fresh clear above), so
    # Session.load_metadata_only() ran -- and therefore Session.__init__() ran
    # -- once per row: len(sids) separate `_resolve_profile_home_param("default")`
    # calls. Without the fix each of those pays its own `_safe_resolve()`
    # filesystem call; with the fix active only the FIRST one does, and the
    # remaining len(sids) - 1 are served from the call-scoped cache.
    assert profile_home_resolves == 1, (
        f"the profile-home _safe_resolve() ran {profile_home_resolves} times across "
        f"{n} sidecar-miss rows sharing one profile -- expected exactly 1 (the rest "
        "should be served from the call-scoped cache)"
    )


def test_load_cli_sessions_uncached_profile_home_resolve_scales_with_fix_disabled(tmp_path):
    """Companion to the test above: calling the UNDECORATED function directly

    (bypassing `profile_home_resolve_cache_scope()` via `__wrapped__`, i.e.
    reproducing pre-PR behavior) shows the real filesystem resolve DOES scale
    linearly with row count -- confirming the dedup measured above is the
    scope's doing, not an artifact of some other cache.
    """
    from unittest import mock

    import api.workspace as ws

    models.clear_sidecar_metadata_cache()
    n = 60
    db_path, sids = _make_cli_sessions_uncached_fixture(tmp_path, n)

    with (
        mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        mock.patch("api.models.get_last_workspace", return_value=str(tmp_path)),
        mock.patch("api.models.ensure_cron_project", return_value="cron-pid"),
        mock.patch("api.models.SESSION_DIR", tmp_path / "sessions"),
        # Same isolation as the test above -- see its comment.
        mock.patch("api.config.get_config_for_profile_home", return_value={}),
        mock.patch.object(ws, "_cached_safe_resolve_profile_home", wraps=ws._cached_safe_resolve_profile_home) as memo_spy,
        mock.patch.object(ws, "_safe_resolve", wraps=ws._safe_resolve) as safe_resolve_spy,
    ):
        undecorated = models._load_cli_sessions_uncached.__wrapped__
        result = undecorated(tmp_path, db_path, None)

    assert len(result) == n
    # The memoizing function is still called once per row either way...
    assert memo_spy.call_count == n
    # ...but with no active scope it never hits its cache branch, so every
    # single call pays its own real filesystem resolve.
    import api.profiles as profiles
    profile_home_resolves = _count_profile_home_resolve_calls(
        safe_resolve_spy.call_args_list, profiles._DEFAULT_HERMES_HOME
    )
    assert profile_home_resolves == n, (
        f"expected the profile-home _safe_resolve() to run once per row ({n}) with no "
        f"active cache scope, but it ran {profile_home_resolves} times"
    )
