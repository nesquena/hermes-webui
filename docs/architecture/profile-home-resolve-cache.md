# Profile-home resolve call-scoped cache (`api/workspace.py`)

This document records the current scope, ownership, and freshness contract for
the call-scoped memoization added around `_resolve_profile_home_param()` in
`api/workspace.py`. It describes shipped behavior and changes no runtime
behavior. It was added after PR #7636 review flagged this cache lifecycle as
an undocumented runtime contract.

## What is cached

- **What:** the resolved `Path` returned by `_safe_resolve()` for a given
  pre-resolve profile-home `Path`, inside `_cached_safe_resolve_profile_home()`.
- **Key:** the unresolved profile-home `Path` passed in.
- **Store:** a plain `dict[Path, Path]` held by the
  `_PROFILE_HOME_RESOLVE_SCOPE` `contextvars.ContextVar`, which defaults to
  `None` (no active scope).

## Scope: one call, not the process lifetime

The cache is deliberately **call-scoped**, not process-lifetime. It exists only
for the duration of one wrapped call to `_load_cli_sessions_uncached` — the
CLI/cron sidebar session-list build that resolves the same profile argument
once per `Session` row (hundreds of times per request in the common case,
which is the N+1 this cache fixes). `profile_home_resolve_cache_scope()` sets
the `ContextVar` to a fresh `{}` on entry and resets it to the previous value
in a `finally` block on exit, so no cache entry survives past the single call
it was created for. An earlier revision of this fix cached for the life of the
process; that was reverted in review (see #7636) because `Path.resolve()` is a
filesystem call, not a pure function of process-startup constants.

## Ownership: which call sites get caching

- `profile_home_resolve_cache_scope()` is applied, as a decorator, to exactly
  one call site today: `_load_cli_sessions_uncached` in `api/models.py`.
- Every other caller of `_resolve_profile_home_param()` /
  `_cached_safe_resolve_profile_home()` — and every call to those functions
  from outside an active scope, including calls that happen before or after
  the one wrapped call — gets **no caching**: `_PROFILE_HOME_RESOLVE_SCOPE.get()`
  returns `None`, and `_cached_safe_resolve_profile_home()` falls straight
  through to a fresh `_safe_resolve()` call, exactly matching pre-PR behavior.
- Nested/reentrant use of `profile_home_resolve_cache_scope()` restores the
  previous (outer) value on exit rather than clobbering it, so nesting is safe
  but still never leaks a cache entry past the scope that created it.

## Freshness guarantee

Because the cache is call-scoped and discarded in `finally` after each use, a
profile-home symlink retarget or a transient `_safe_resolve()` fallback
(`OSError`/`RuntimeError`/`ValueError`, which deliberately returns the
unresolved input) is always observed fresh on the **next** call — the cache
never outlives the call that populated it. The only staleness window is
*within* a single call, i.e. across the hundreds of `Session` rows built by one
`_load_cli_sessions_uncached` invocation — which is correct and intended, since
nothing about a profile's home path should change mid-call.

## Change protocol

1. If a new hot loop needs this memoization, wrap it with
   `profile_home_resolve_cache_scope()` (as a decorator or a `with` block) at
   its own call boundary — do not widen the existing scope on
   `_load_cli_sessions_uncached` to also cover unrelated code.
2. Do not turn this into a process-lifetime or otherwise longer-lived cache.
   The scoping is the fix for the correctness bugs described above, not an
   incidental detail.
3. This is a runtime contract: changes here update this document and are
   described in the PR body.

## Tests

`tests/test_profile_home_resolve_caching.py` covers: repeated resolves within
one scope hit the cache once
(`test_resolve_profile_home_param_caches_within_one_scope`,
`test_resolve_profile_home_param_caches_within_one_scope_for_profile_name`);
two separate scopes never share state, so a symlink change between them is
observed on the second scope
(`test_cache_does_not_persist_across_separate_scopes`); calls outside any
scope never cache at all (`test_no_caching_outside_any_scope`); the decorator
is actually wired onto `_load_cli_sessions_uncached`
(`test_scope_decorator_use_matches_load_cli_sessions_uncached_wiring`); and the
N+1 fix itself, both that it dedupes resolves on a cold sidecar-cache miss
(`test_load_cli_sessions_uncached_dedupes_profile_home_resolve_on_sidecar_cache_miss`)
and that resolve calls scale linearly with the fix disabled
(`test_load_cli_sessions_uncached_profile_home_resolve_scales_with_fix_disabled`).

## References

PR: #7636.
