"""Regression test for issue #1937 — endless-scroll prefetch vs Start-jump race.

When both ``session_jump_buttons`` and ``session_endless_scroll`` opt-ins
are enabled, ``_loadOlderMessages`` (the endless-scroll prefetch) can be in
flight when the user clicks the Start jump pill, which calls
``_ensureAllMessagesLoaded``.  If the prefetch resolves AFTER the
ensure-all wholesale-replaces ``S.messages``, it would prepend a duplicate
page.

The fix uses two coordinated guards:

1. A ``_messagesGeneration`` token that gets bumped any time
   ``S.messages`` is wholesale-replaced.  ``_loadOlderMessages`` snapshots
   the token before its ``await`` and re-checks afterwards; if it changed,
   the prepend is aborted.

2. ``_ensureAllMessagesLoaded`` claims the existing ``_loadingOlder``
   mutex around its body so no NEW prefetch can start mid-replace, and so
   concurrent ensure-all invocations (e.g. rapid double-click on Start)
   serialize cleanly.  It also yields until any in-flight prefetch's
   ``finally`` clears the flag before claiming the mutex itself.

The old fix shape suggested in the issue (spin-wait on ``_loadingOlder``
before running ensure-all) does not actually solve the race the report
describes: by the time the prefetch passes its entry-gate check, it is
already past the only point where ``_loadingOlder`` is read, so a same-
flag check inside its post-await body would be a no-op.  The generation
token is the canonical pattern for invalidating async continuations and
is what this regression suite locks in.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """Slice the body of ``async function <name>`` (or ``function <name>``)."""
    needle_async = f"async function {name}"
    needle_sync = f"function {name}"
    if needle_async in src:
        start = src.index(needle_async)
    else:
        start = src.index(needle_sync)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function {name!r} body not found")


# ---------------------------------------------------------------------------
# Generation token: declared at module scope, bumped via the helper.
# ---------------------------------------------------------------------------

def test_generation_token_declared_at_module_scope():
    """``_messagesGeneration`` exists as a module-scoped mutable counter."""
    assert "let _messagesGeneration = 0;" in SESSIONS_JS, (
        "static/sessions.js must declare `let _messagesGeneration = 0;` so "
        "_loadOlderMessages can snapshot/re-check it across its `await`. "
        "See #1937."
    )


def test_generation_bump_helper_exists():
    """A single helper bumps the generation; both consumers route through it."""
    assert "function _bumpMessagesGeneration()" in SESSIONS_JS, (
        "static/sessions.js must define `_bumpMessagesGeneration()` so "
        "wholesale-replace sites have a single, named pivot to call. See #1937."
    )
    body = _function_body(SESSIONS_JS, "_bumpMessagesGeneration")
    assert "_messagesGeneration" in body, (
        "_bumpMessagesGeneration must mutate _messagesGeneration"
    )


# ---------------------------------------------------------------------------
# _loadOlderMessages: snapshot before await, re-check after.
# ---------------------------------------------------------------------------

def test_load_older_snapshots_generation_before_await():
    """Snapshot must be captured BEFORE the `await api(...)` call."""
    body = _function_body(SESSIONS_JS, "_loadOlderMessages")
    snapshot_idx = body.index("const startGeneration = _messagesGeneration;")
    await_idx = body.index("await api(")
    assert snapshot_idx < await_idx, (
        "_loadOlderMessages must snapshot _messagesGeneration before its "
        "`await`. Capturing it after the await defeats the race guard. "
        "See #1937."
    )


def test_load_older_aborts_when_generation_changed():
    """Post-await guard must compare against the snapshot and abort."""
    body = _function_body(SESSIONS_JS, "_loadOlderMessages")
    assert "if (_messagesGeneration !== startGeneration) return;" in body, (
        "_loadOlderMessages must bail out (without prepending) when the "
        "generation token changed during its await — that is the signal "
        "that S.messages was wholesale-replaced under it. See #1937."
    )


def test_load_older_generation_check_runs_before_prepend():
    """Generation check must come BEFORE the `S.messages = [...older, ...]` mutation."""
    body = _function_body(SESSIONS_JS, "_loadOlderMessages")
    guard_idx = body.index("if (_messagesGeneration !== startGeneration) return;")
    prepend_idx = body.index("S.messages = [...olderMsgs, ...S.messages];")
    assert guard_idx < prepend_idx, (
        "Generation guard must short-circuit BEFORE the prepend. "
        "Otherwise duplicate messages can still slip through. See #1937."
    )


# ---------------------------------------------------------------------------
# _ensureAllMessagesLoaded: claims the mutex, bumps the generation, yields.
# ---------------------------------------------------------------------------

def test_ensure_all_bumps_generation_before_replace():
    """Bump must happen BEFORE `S.messages = msgs` so racing prefetch sees it."""
    body = _function_body(SESSIONS_JS, "_ensureAllMessagesLoaded")
    bump_idx = body.rindex("_bumpMessagesGeneration()")
    replace_idx = body.index("S.messages = msgs;")
    assert bump_idx < replace_idx, (
        "_ensureAllMessagesLoaded must bump the generation token BEFORE the "
        "wholesale replace, otherwise an in-flight prefetch's post-await "
        "check could read the old value and prepend duplicates. See #1937."
    )


def test_ensure_all_claims_loading_older_mutex():
    """The body must hold `_loadingOlder = true` so no NEW prefetch starts mid-replace."""
    body = _function_body(SESSIONS_JS, "_ensureAllMessagesLoaded")
    assert "_loadingOlder = true;" in body, (
        "_ensureAllMessagesLoaded must claim the _loadingOlder mutex so "
        "the entry-gate in _loadOlderMessages short-circuits new prefetches "
        "while ensure-all is mid-replace. See #1937."
    )
    assert "_loadingOlder = false;" in body, (
        "_ensureAllMessagesLoaded must release the _loadingOlder mutex in "
        "its finally-block. Otherwise endless-scroll silently breaks after "
        "every Start-jump."
    )


def test_ensure_all_releases_mutex_in_finally():
    """Mutex release must live inside a `finally` so errors don't leak the lock."""
    body = _function_body(SESSIONS_JS, "_ensureAllMessagesLoaded")
    finally_idx = body.index("} finally {")
    release_idx = body.index("_loadingOlder = false;", finally_idx)
    assert release_idx > finally_idx, (
        "_loadingOlder release must be inside the finally-block to survive "
        "thrown errors during the wholesale replace. See #1937."
    )


def test_ensure_all_yields_when_prefetch_in_flight():
    """When a prefetch holds the mutex, ensure-all must wait, not wholesale-replace alongside it."""
    body = _function_body(SESSIONS_JS, "_ensureAllMessagesLoaded")
    # Look for the yield-loop on _loadingOlder before the mutex claim.
    yield_idx = body.index("while (_loadingOlder)")
    claim_idx = body.index("_loadingOlder = true;")
    assert yield_idx < claim_idx, (
        "_ensureAllMessagesLoaded must yield (poll _loadingOlder) BEFORE "
        "claiming the mutex itself, so an in-flight prefetch's finally-"
        "block fires and the generation guard inside that prefetch resolves "
        "the race cleanly. See #1937."
    )


def test_ensure_all_bumps_generation_during_wait_phase():
    """Bumping during the wait poisons any in-flight prefetch immediately, even before ensure-all gets the mutex."""
    body = _function_body(SESSIONS_JS, "_ensureAllMessagesLoaded")
    # Find the _loadingOlder branch that runs when a prefetch is in flight,
    # and verify it bumps the generation before the wait loop.
    branch_idx = body.index("if (_loadingOlder) {")
    wait_idx = body.index("while (_loadingOlder)", branch_idx)
    bump_in_branch = body.index("_bumpMessagesGeneration()", branch_idx)
    assert branch_idx < bump_in_branch < wait_idx, (
        "When a prefetch is in flight at entry, _ensureAllMessagesLoaded "
        "must bump the generation BEFORE the wait loop so the in-flight "
        "prefetch's post-await check fires the moment its api() resolves, "
        "not just for future calls. See #1937."
    )


def test_ensure_all_resets_oldest_idx():
    """After wholesale-replacing with the full history, _oldestIdx must reset to 0."""
    body = _function_body(SESSIONS_JS, "_ensureAllMessagesLoaded")
    assert "_oldestIdx = 0;" in body, (
        "_ensureAllMessagesLoaded must reset _oldestIdx to 0 — without it, "
        "a subsequent prefetch could send `msg_before=<stale-idx>` and "
        "request older messages that are already in the now-full transcript."
    )


def test_ensure_all_guards_against_session_switch_mid_await():
    """Same-session check must run after await — old version skipped this."""
    body = _function_body(SESSIONS_JS, "_ensureAllMessagesLoaded")
    await_idx = body.index("await api(")
    sid_check_idx = body.index("S.session.session_id !== sid", await_idx)
    replace_idx = body.index("S.messages = msgs;", await_idx)
    assert await_idx < sid_check_idx < replace_idx, (
        "_ensureAllMessagesLoaded must guard against session-switch races "
        "(re-check S.session.session_id after await) BEFORE wholesale-"
        "replacing S.messages. The pre-fix version had no such guard."
    )
