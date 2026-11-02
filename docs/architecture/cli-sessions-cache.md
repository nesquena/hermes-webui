# CLI sessions cache and singleflight coordination contract

This document describes the caching, singleflight coordination, and invalidation semantics for CLI session listings in `api/models.py`.

## Overview

When the WebUI sidebar or API queries CLI sessions (`_get_cached_cli_sessions`):
1. **Cache hit:** Returns the cached in-memory list if it has not expired (`ttl`) and the global invalidation stamp has not changed.
2. **In-flight singleflight claim:** If another request is currently rebuilding the cache for the same cache key (`_cli_sessions_cache_claim_rebuild`):
   - The first request becomes the owner and performs the rebuild.
   - Subsequent requests become waiters and block on a `threading.Event` (for up to `_CLI_SESSIONS_CACHE_WAIT_SECONDS` or `_CLI_SESSIONS_CACHE_STALE_WAIT_SECONDS`).
3. **Re-claim and bounded retries:**
   - When the event fires or times out, the waiter checks if a fresh entry is now available or if a stale snapshot can be returned.
   - If an adversarial clear-storm occurs (e.g. rapid writes or cache invalidations where the cache was emptied right as the owner finished), the waiter loops back to claim or wait again.
   - To prevent unbounded loop iterations and thread starvation, the loop is hard-capped at `_CLI_SESSIONS_CACHE_MAX_RECLAIMS` (default: 5 iterations, `#4966`). Once reached, the waiter falls back to loading and caching the sessions directly.
4. **Choose-and-publish ordering:** Publication is atomic under `_CLI_SESSIONS_CACHE_LOCK`. If, while this load was running, another rebuilder published a same-stamp and still-unexpired entry whose scan *started* after this one, that entry is returned instead of being overwritten. Ordering is by read start (`_CLI_SESSIONS_CACHE_READ_STARTED`, a sidecar keyed like the cache entry), never by publication or expiry time, so a scan that started earlier cannot displace a later one.
5. **Entry shape:** a cache entry remains `(expires_at, invalidation_stamp, sessions)`. The read-start clock lives in the sidecar rather than in the entry because `get_cli_sessions()` and the cache tests unpack entries positionally.
