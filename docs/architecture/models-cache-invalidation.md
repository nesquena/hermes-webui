# Models catalog cache invalidation contract (`/api/models`)

This document records the current identity and invalidation contract for the
`/api/models` catalog cache in `api/config.py`. It describes shipped behavior and
changes no runtime behavior. It was added after #7556 shipped in `exp-v0.52.303`,
which the #7556 review flagged as an undocumented runtime contract.

## What is cached

- **In memory:** `_available_models_cache` plus `_available_models_cache_ts`,
  with `_AVAILABLE_MODELS_CACHE_TTL` set to 24 hours.
- **On disk:** one `models_cache.json` per profile under the WebUI state
  directory (`_get_models_cache_path()`), stamped with `_schema_version` and
  `_webui_version`.
- **Cold path:** `get_available_models(prefer_cache=...)`; the `prefer_cache`
  branch never starts a live provider rebuild.
- **Hot path:** `_endpoint_advertised_model_ids()` reads only the published
  in-memory snapshot through the lock-free `_models_cache_provenance` tuple and
  validates it against the current source fingerprint before trusting it.
  `_sync_models_cache_provenance()` must run at every site that publishes or
  invalidates the snapshot, so the tuple can never tear.

## The five source axes

`_models_cache_source_fingerprint()` is the single chokepoint. A cache is served
only when every axis matches the value recorded in the cache — the disk reader
compares it in `_is_loadable_disk_cache()`, and the hot path compares it against
the fingerprint captured at publish time.

| Axis | Identity | Why it is fingerprinted this way |
| --- | --- | --- |
| `config_yaml` | stat identity: `mtime_ns` + size (`_models_cache_file_fingerprint`) | The file is rewritten only on deliberate user edits, and any edit can change the provider/model set, so the cheap conservative identity wins. |
| `auth_json` | content hash with a volatile-key deny-list (`_auth_store_semantic_fingerprint`, `_AUTH_FINGERPRINT_VOLATILE_KEYS`) | The credential store is rewritten roughly every 14 minutes by credential-pool / OAuth refresh; none of those rotating fields feed `detected_providers` or the returned catalog, and stat identity made the 24h cache churn on every refresh (RCA `t_d127953d` / `t_16551f61`). |
| `env` | `[key, HMAC-SHA256(signing key, value)]` per non-empty `.env` entry, parsed by `providers._load_env_file` (`_models_cache_env_fingerprint`); plaintext values are never recorded | Env keys decide `detected_providers` and values such as `LM_BASE_URL` decide which endpoint is probed, so both key and value changes invalidate. |
| `plugins` | `[relpath, mtime_ns, size]` of every non-bytecode file of each model-provider plugin, discovered like `providers._scan_home_layer`, flat manifests parsed with PyYAML like the agent (`_models_cache_plugin_fingerprint`) | The loader execs plugin code that builds `fallback_models`, so any file edit must invalidate, even without a `version` bump. |
| `catalog` | baked-in provider catalog sha256 (`_PROVIDER_MODELS` + `_PROVIDER_DISPLAY`) plus the Codex local catalog (`_codex_models_cache_fingerprint`, `_CODEX_CACHE_FINGERPRINT_VOLATILE_KEYS`) | A restart after a catalog change must not keep serving a persisted payload for up to 24h (#2443). Codex rewrites `~/.codex/models_cache.json` on its own timer, bumping `mtime_ns` and size while models, `etag`, and `client_version` stay identical, so the Codex axis hashes **content** with only the refresh timestamps (`fetched_at`, `updated_at`) removed (#7540, #7556). |

The over-budget stale fallback (`_load_stale_models_cache_from_disk`) tolerates a stale
`_webui_version` but never a source-fingerprint mismatch: such a snapshot is a wrong catalog.

## Codex catalog and routing

The configured default is checked against the active provider's own group
(including its overflow entries), not against matching bare IDs in other
providers' groups. An OpenAI API entry therefore cannot suppress insertion of a
configured Codex default. The normal provider-qualified deduplication still
keeps the resulting picker options distinct.

The static `openai-codex` model list is a degraded fallback, not an account
entitlement list. Account-aware live discovery and visible entries in the local
Codex catalog can add models absent from that fallback. Generic Agent-core
seeding skips `openai-codex` so it cannot reintroduce retired or account-specific
IDs into the static list; it still enriches other providers.

Codex selections are qualified as `@openai-codex:<model>` before the
same-provider bare-ID shortcut. This keeps a live-discovered Codex model routed
to Codex even if another configured provider advertises the same ID. The
separate OpenAI API catalog does not determine Codex subscription availability.

## Probe diagnostics have their own, shorter lifetime

`models_endpoint_error` is a *reachability diagnostic*, not catalog data: it
describes one custom provider's `GET <base_url>/v1/models` at the instant it was
taken. No source axis can observe that an endpoint recovered, so a diagnostic
must not inherit the 24h catalog TTL — one probe failure (a DNS blip, an attempt
past `CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS`, a throttled retry) would otherwise
report "verify base_url" for a full day against a correct `base_url` (#8158).

The diagnostic's `checked_at` and the catalog's `_built_at` are independent
stamps with different owners and lifetimes: `_built_at` ages the *catalog*
(24h), while `checked_at` ages one provider's *reachability claim* (60s). A
diagnostic is therefore never refreshed by, nor does it refresh, the catalog's
freshness clock.

- **Stamped**: `_custom_endpoint_error()` writes a wall-clock `checked_at`
  (wall clock, not `time.monotonic()`, because the diagnostic is persisted to and
  restored from `models_cache.json` and must stay comparable across processes).
- **Bounded**: `_MODELS_ENDPOINT_ERROR_RETRY_SECONDS` (60s) is the diagnostic's
  own lifetime. `_models_endpoint_error_retry_expired()` fails **closed** toward
  re-probing: a missing, non-numeric, non-finite, or future stamp counts as
  expired, so a cache written before this field existed re-probes on its first
  read rather than trusting an unverifiable stamp.
- **Retried off the read path**: a warm read that serves an expired diagnostic
  still returns the cached payload immediately — it never probes synchronously —
  and schedules one re-probe. The scheduling call sits at each site that
  publishes or serves a catalog (`_get_fresh_memory_models_cache` and the two
  cold-path disk hits), including the boot path that serves straight from
  `models_cache.json`.
- **Coalesced per (profile, provider)**: `_models_endpoint_reprobe_inflight` is
  keyed by profile *and* provider, because the catalog globals are process-wide
  and a same-slug provider in another profile has different credentials and a
  different endpoint. Concurrent readers join the in-flight probe.
- **Cache-only reads never re-probe.** `_get_fresh_memory_models_cache()` takes
  `allow_reprobe`, and `get_available_models(prefer_cache=True)` passes `False` on
  both its memory and its disk path. The `prefer_cache` contract is to answer
  without starting provider I/O (the wakeup-turn path depends on it), so an
  expired diagnostic is left for a normal read to refresh.
- **Resolved on the owning thread, never re-read by the worker.** A re-probe's
  request — endpoint URL, credential, profile — is resolved in
  `_resolve_endpoint_probe_request()` on the calling request thread, inside that
  profile's environment scope, and the worker replays exactly those values
  (`_EndpointProbeRequest`). The worker must not consult the process-shared `cfg`:
  the catalog globals are process-wide, so another profile's request can load its
  config after scheduling, and a late lookup would pair one profile's secret with
  another profile's URL. Resolution consumes `${VAR}` indirection and `key_env`
  the same way the rebuild does.
- **Fenced**: a re-probe result is published only when all three still hold —
  its generation is still the newest allocated one (`_models_rebuild_seq`), the
  *published catalog's* fingerprint is still the one the probe was scheduled for,
  and the runtime sources/profile are unchanged
  (`_models_build_identity_current`). The published-fingerprint check is what
  catches a catalog whose owner changed without a new generation
  (`warm_models_catalog_provenance_if_cold()` republishing another profile's
  snapshot); a worker-identity check alone cannot see that. The same predicate
  (`_endpoint_reprobe_target_is_current`) fences the live-probe clear, so neither
  writer can drift into a weaker check than the other, and the check runs both
  before the request is spent and again before publishing.
- **Copy-on-write**: merging a result republishes a new snapshot instead of
  editing the cached dict in place. The snapshot doubles as the
  `_models_cache_provenance` identity and as `_endpoint_advertised_model_ids()`'s
  memo key, so an in-place edit would leave that memo describing a payload it no
  longer matches.
- **Cleared by a healthy live probe**: `/api/models/live` captures an identity
  (`capture_live_probe_identity()`) *before* issuing its request and passes it to
  `clear_models_endpoint_error_for_provider(provider_id, identity)`; the identity
  argument is required, and a clear that cannot confirm the generation, the
  published owner and the profile is refused. A response that observed a
  superseded config must not erase a newer, still-valid diagnostic. Success means
  the request completed and the payload was well formed — a valid
  `{"data": []}` is an answer from a reachable endpoint, while a transport error,
  a malformed payload, or a cache hit is not. Clearing republishes the group
  without the diagnostic; the group itself is never dropped, so an empty group
  carrying `models_endpoint_error` stays for the auxiliary pickers (#7521).
- **Diagnostics are not a fingerprint axis.** They are per-probe observations, so
  expiring them must not be done by adding a source axis; the generation fence
  above is what keeps a stale observation from being applied.

## Invariant: deny-lists are one-directional

- Both volatile-key sets are deny-lists, never allow-lists. They may remove only
  fields that provably do not gate the provider/model set.
- Every other field — including fields that Codex or the auth store may add in
  the future — stays in the fingerprint
  (`test_unknown_codex_field_stays_in_fingerprint`).
- Consequence: excluding a volatile key can only make the fingerprint **more
  stable**; it can never hide a genuine catalog or provider change. When in
  doubt, keep the key in.

## Invariant: fallbacks are never less safe than stat

- Missing file → recorded as missing, and the fingerprint stays stable
  (`test_missing_codex_cache_fingerprint_is_stable_and_marked`).
- Unreadable, corrupt, or mid-write JSON → stat identity, marked
  `unparsed-fallback`.
- Transform failure — including `RecursionError` on a pathologically deep tree →
  stat identity, marked `encode-fallback`
  (`test_deeply_nested_codex_cache_degrades_to_stat_fallback_without_crashing`).
  The fingerprint must never raise into `/api/models`; a real rewrite still
  changes the stat identity, so the fallback is strictly no less safe than the
  pre-#7556 behavior.

## Version stamps (independent of the fingerprint)

- `_schema_version` must equal `_MODELS_CACHE_SCHEMA_VERSION` (currently `3`).
  Bump it when the cached payload shape changes incompatibly. The `checked_at`
  field added to probe diagnostics (#8158) is deliberately **not** a bump: it is
  an additive key inside an existing payload that older and newer readers both
  read as a dict, and the read path treats a missing stamp as expired, so a cache
  written before the field existed re-probes instead of needing to be discarded.
  Bumping would additionally throw away every warm catalog on upgrade, which the
  healthy-catalog cache is meant to survive.
- `_webui_version` must equal the running version, which forces a rebuild after
  every release so picker-shape fixes appear immediately instead of after the
  TTL expires. When the runtime version cannot be resolved (early boot), that
  check is skipped rather than wedging the boot.
- `_built_at` records the POSIX timestamp (in seconds) of when the disk cache
  was built by `_save_models_cache_to_disk`. Freshness checks evaluate age
  against `now - _built_at` rather than filesystem `st_mtime` to prevent read/access
  touches from resetting the cache age while preserving fallback to `st_mtime`
  if the stamp is missing or invalid.

## Invalidation paths: memory vs. disk

| Path | In-memory snapshot | Disk snapshot |
| --- | --- | --- |
| `invalidate_models_cache()` (default `delete_disk=True`) | dropped | **deleted** |
| `invalidate_models_cache(delete_disk=False)` (`POST /api/profile/switch`) | dropped | kept |
| `invalidate_provider_models_cache(provider_id)` | dropped | **deleted** |
| `_get_fresh_memory_models_cache()` on fingerprint mismatch / invalid shape | dropped | untouched |
| config-reload branch in `get_available_models()` | dropped | deleted by `_refresh_config_cache()` only when the *same* `config.yaml` path was already loaded and changed; a first load or path change (per-client switch) keeps it |

The switch keeps the disk snapshot because it is keyed per profile and
`_is_loadable_disk_cache()` rejects it unless every axis above matches, so any
new catalog input must become an axis first. `delete_profile_api()` and
`create_profile_api()` unlink `models_cache.<name>.json`, so a recreated profile
never inherits the old catalog.

Credential eviction is part of the same epoch-reset critical section as memory
invalidation, before rebuild admission reopens. Full invalidation clears all
profile pools; provider invalidation removes the original and canonical provider
keys for the active profile. Both retain disk-commit → catalog lock order.

Custom endpoint scheduling is work-conserving: a fair-share slice bounds the
serial wait, while its HTTP attempt can continue against the same caller deadline
(with publication headroom and the endpoint cap). Later endpoints still get
in-band attempts; unused time after them is lent back to pending probes. Completed
outcomes are consumed by the catalog worker and merged through its rebuild-local
memo before returning, so provider order does not hide a healthy slow endpoint.
HTTP threads receive already-resolved requests, own only private outcome boxes,
and never touch profile state, catalog/durable caches or publication locks.

A custom-endpoint timeout below the full endpoint cap is a truncated attempt,
not evidence of unreachability. A partial catalog with groups can be returned but
is not published to either cache; an empty partial uses the existing stale-disk
or static fallback instead. The existing worker checks generation revocation
under the catalog condition after the foreground handoff, then retries truncated
targets at the full cap and publishes only through the generation/source/ownership
fences. Unrelated live provider lookups (including empty results and exceptions)
are memoized only for that rebuild, not shared with a successor or another profile.
An abandoned partial is removed from the publication box before revocation is
checked. Empty partials that finish in time do not log a budget-overrun warning.
Malformed LM Studio responses whose `data` is not a list are ignored, preserving
the degraded catalog behavior for both models and onboarding callers.

## Change protocol

1. Add or change a source axis in `_models_cache_source_fingerprint()` only —
   one chokepoint, so the disk reader and the published-snapshot reader always
   agree.
2. When adding a volatile key, name the value it derives from and prove it
   inert: one test that fails if the key is dropped from the deny-list, and one
   that shows a genuine catalog change still invalidates the cache.
3. Keep the fingerprint cheap, deterministic, and safe to run synchronously —
   it is recomputed on cache reads, including a per-turn hot path.
4. Do not add a second, parallel cache-identity mechanism. The stat-based
   `_models_cache_file_fingerprint()` stays only as the conservative fallback for
   the axes above.
5. Diagnostics are not catalog data: expiring a probe diagnostic is not a new
   source axis. Give the diagnostic its own bounded lifetime and fence any
   refresh with the existing generation/identity checks; do not make warm reads
   probe synchronously.
6. Scope every probe to one owner. Resolve a probe's endpoint and credential on
   the thread that owns the profile (never from the process-shared `cfg` inside a
   detached worker), and let a result modify only a catalog that still carries the
   owner and generation it was produced for. A diagnostic writer must pass
   `_endpoint_reprobe_target_is_current()`; cache-only callers pass
   `allow_reprobe=False`.
7. This is a runtime contract: changes here update this document and are
   described in the PR body. Release-note wording belongs in the PR body, not in
   `CHANGELOG.md`, which release commits own.

## Tests

`tests/test_issue7540_codex_catalog_fingerprint.py` covers both invariant groups:
timestamp-only churn keeps the fingerprint identical (and a session visit after a
Codex refresh needs no live rebuild), while genuine changes — a new model, a
visibility change, any catalog field, any unknown field — still invalidate.

`tests/test_profile_switch_models_disk_cache.py` covers the switch: the disk
snapshot survives it and is served without a live rebuild, each source-axis
change or a delete/recreate forces a fresh rebuild, and a same-path config
edit still deletes the snapshot.

`tests/test_issue8158_models_endpoint_error_retry_lifetime.py` covers the
diagnostic lifetime: the reporter's failure→recovery shape as a
`reproduction`-marked test, an unexpired diagnostic that is *not* re-probed, a
warm read that does not block on the retry, concurrent readers coalescing onto
one probe, a failed retry re-stamping, per-provider and per-profile scope, the
stale-generation fence, the durable (boot) path, and the live-probe clear
keeping the zero-model group. Its seeding helpers stamp the seeded catalog with
the loaded config version, so a manually published snapshot is not discarded as
stale by the first read.

`tests/test_issue8158_models_endpoint_probe_ownership.py` covers the ownership
boundaries: a retry keeps its own profile's endpoint and credential after another
profile loads its config, a retry cannot write into another profile's disk
snapshot, a retry captures its generation before the worker starts, the real
`/api/models/live` handler clears only on well-formed answers (empty included)
and never on a transport error, malformed payload or cache hit, a late live
success cannot clear a newer diagnostic, and a `prefer_cache` read starts no
retry on either the memory or the disk path.

## References

Issues/PRs: #2443, #7540, #7556, #7558, #8158. RCAs: `t_d127953d`, `t_16551f61`.
