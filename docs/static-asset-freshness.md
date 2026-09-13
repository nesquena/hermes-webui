# Static asset identity: bounded freshness

The shell and service-worker routes share one `AssetIdentityCache` instance at
`api.asset_identity_cache.ASSET_IDENTITY_CACHE`, through the existing
`_assets_cache_bust_token()` helper. The strict recursive inventory and actual
content digests remain authoritative. Semantic bundle identity is unchanged.

## Contract change

Previously each token request rescanned the entire static tree. The new policy
shares a successful completed scan for **1 second**, measured with a monotonic
clock from completion, and shares unavailable identity for **0.25 seconds**.
After expiry, same-size/same-nanosecond-mtime replacements, nested assets, added
or removed files, worker resources and icons are detected by content hashing.
A metadata touch alone does not change the identity.

This is bounded staleness, not an atomic filesystem snapshot. An edit during
freshness can retain the preceding asset token until the next refresh. This is
why served static responses must continue reading exact bytes, deriving their
ETags from that same buffer, and returning `public, max-age=0, must-revalidate`
for every query token. No `immutable` permission is granted by this cache.
The existing semantic-version bridge is unchanged; it still distinguishes the
release version from the fingerprinted asset token.

There is **at most one active scan per process**, including across root changes,
and at most **four retained root entries**, including failures. Root identity
is server-selected, never taken from a URL query or request header. Warm hits
perform no static-tree traversal, resolution, stat or byte reads. The original
scanner resolves symlinks inside the single-flight callback; lexical `..` is
not collapsed before symlink resolution.

At most **32 followers** wait, with a **2-second** monotonic waiting budget.
The original absolute deadline is checked again after a notified follower
reacquires the lock. A completed generation is shared only while its own
completion-based expiry remains valid. A delayed follower fails closed and
never starts a replacement scan just because its generation has expired.
Extra or timed-out callers receive unavailable identity, rather than stale
success or permission to launch another scan. Existing route behavior then
keeps shell output out of its cache and returns a no-store 503 for the worker.
Other healthy cached roots can still be reused while a scan is in progress.
The scanning leader can remain blocked in filesystem I/O: this mechanism bounds
scan fan-out, not OS call duration or all HTTP server request threads. It does
not attempt to kill threads or spawn replacement scans. Bounds are per process;
multiple server processes each own their own cache.

Filesystem work never holds the coordination lock. Ordinary exceptions become
short-lived unavailable results. A leader's `BaseException` still propagates,
but followers are released with unavailable identity. Interrupted follower waits
release their slot. Coordination cleanup also runs if publication fails.

## Diagnostics

`ASSET_IDENTITY_CACHE.snapshot()` returns an independent, fixed-key in-process
snapshot. It includes requests, warm/negative hits, refresh attempts/successes/
failures, shared results, expired shared results, waiter rejections/timeouts,
current and peak waiters,
entry/eviction counts, in-flight state, and last/total/maximum refresh seconds.

There are no request identifiers, paths, roots, tokens, digests, exception
strings, per-key labels, exporters, log floods, new dependencies, new HTTP
endpoints, or background threads. An exporter would be a separate scoped task.

For a same-root burst within freshness, `refreshes` should increase once;
`in_flight` is 0 or 1; `entries <= 4`; `current_waiters <= 32`. Repeated inventory
errors should increase negative hits rather than refreshes until failure expiry.
Timeouts and rejections are fail-closed availability signals, not proof of a
healthy cache. Timing values describe this process, not production-wide SLOs.

## Verification

Run the new unit, byte-identity, and full-router tests along with the existing
static cache, static resolver, PWA, semantic bridge and index-template tests
through `./scripts/test.sh`. Legacy mutation tests must explicitly advance a
fake monotonic clock past freshness rather than disabling the cache. Retain the
non-root POSIX directory-permission regression and exact nanosecond assertions.

Then run the complete repository suite and credential-free browser smoke on the
rebased head. A source-helper replay does not validate HTTP middleware, browser
service-worker lifecycle, subpath installation, authentication, gzip/304 routes,
or the maintainer's private gate. These remain separate certification steps.
