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
- **Parse memo:** one process-wide single slot, `_models_disk_payload_memo`,
  guarded by `_models_disk_payload_memo_lock`. It is NOT a cache of the catalog
  and never outlives a single session visit — see
  [The parse memo](#the-parse-memo-third-layer) below.
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

## The parse memo (third layer)

`_read_models_cache_disk_payload()` parses the disk snapshot and leaves the
result in a single process-wide slot, `_models_disk_payload_memo`, so the
session-visit freshness precheck and the loader that follows it in the same call
do not `json.load` the same file twice. It is a parse handoff, not a third
cache with its own freshness policy: it can only ever save the parse that
happens microseconds later inside one call.

### File key

The slot is keyed on `(path, st_mtime_ns, st_size, st_ino, st_dev)`. The stat
identity is taken from `os.fstat(f.fileno())` of the descriptor `json.load`
actually parsed, *after* `open()` — never from a separate `cache_path.stat()`
that runs before it. Key and payload therefore come from the same descriptor and
cannot disagree, so there is no precheck-then-open window to race.

`st_ino`/`st_dev` are load-bearing, not decoration. The real writer replaces the
file with a temp file plus `os.replace()`, which mints a new inode, while Linux
inode timestamps come from the coarse clock: a rapid same-size rewrite can carry
the same `st_mtime_ns` and the same `st_size`. Keyed on `(mtime_ns, size)`
alone, that generation is indistinguishable from the old one and the previous
catalog is served back.

### Generation fence

The stat identity says *which bytes* were parsed. It does not say *which
catalog generation* they belonged to, and those are not the same thing: a
rebuild is not guaranteed to change any part of the stat key. A same-size
rewrite inside the coarse-clock mtime tick, on a filesystem that reissues the
freed inode, produces a file whose `(st_mtime_ns, st_size, st_ino, st_dev)` is
identical to the pre-rebuild one.

So the entry also carries the catalog generation (`_models_rebuild_seq`) that
was current when the payload was parsed, and that generation fences the entry
on both sides:

- The **producer** captures the generation before it opens the file and
  publishes its entry only while that generation is still current. An
  invalidation that lands during the parse supersedes the bytes it parsed, so
  those bytes are never published.
- The **consumer** rejects an entry whose generation is no longer current, and
  drops it rather than leaving it resident.

Both are required, and neither alone is sufficient.
`invalidate_models_cache()` clears the slot *before* it advances the
generation, so a producer that captured the old generation can publish into
that gap — the entry is published while the generation is still current, and
the generation advances afterwards. Only the consumer's own check rejects it.

This matters because the epoch guard in `get_available_models_for_session_visit()`
cannot catch it: `disk_epoch` is read *after* the freshness precheck, so it
already holds the post-invalidation value and compares equal to the current
one. The generation fence is the only thing that distinguishes the
pre-invalidation catalog from the rebuilt one at that point.

### Consume-once ownership

The slot is **consume-once**: a hit clears the slot before returning, and every
later read re-parses. The memo hands its payload to exactly one reader.

This is what keeps the ownership invariant that predates the memo: a
pre-#7932-era read always produced a fresh `json.load`, so each caller owned its
catalog exclusively and could mutate it freely. Callers do mutate what they
receive — `_annotate_fast_tier_model_groups` writes `supports_fast_tier` into
the model dicts, and `warm_models_catalog_provenance_if_cold` publishes them
into the process-wide `_available_models_cache` without a deepcopy. Handing the
same nested dicts to a second reader would make one caller's in-place edits
visible to the other, and the corrupted catalog would then be served to a later
read as though it had just come off disk.

The read-validate-clear path runs entirely under `_models_disk_payload_memo_lock`
— not just the clear. Reading the slot and then calling `stat()` releases the
GIL, so without the full-scope lock two threads can both enter, both pass
validation, and both return the same object, which would make the consume step a
no-op in exactly the case it exists for. The producer's slot write takes the
same lock.

A visit served from the *memory* cache never reaches the loader that would
consume the precheck's parse, so it drops the slot instead. Without that, the
abandoned entry keeps a whole parsed catalog resident (about 2.2 MB at 5,000
models) and an unrelated later read can pick it up — which is what breaks the
"never outlives a single session visit" rule above.

`None` in the payload slot is a real answer — "this file parsed, but not to a
dict" — and is distinct from `_MODELS_DISK_PAYLOAD_UNSET`, which means "no memo
entry, read the file yourself". An unreadable or unparseable file clears the
slot so the next reader retries from scratch.

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
  Bump it when the cached payload shape changes incompatibly.
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

### The parse memo is dropped on every invalidation

`invalidate_models_cache()` clears `_models_disk_payload_memo` under
`_models_disk_payload_memo_lock` before it touches the disk snapshot, including
the `delete_disk=False` profile-switch path where the snapshot is deliberately
kept. Clearing memory alone is not a no-op here, because the memo is the one
cache that would otherwise survive.

None of the memo's key components is guaranteed to change across a rebuild: the
writer can land inside the same coarse-clock mtime tick at an identical size,
and a filesystem that reuses inodes can reissue the same `st_ino`/`st_dev` pair.
A surviving entry therefore keeps matching the freshly rebuilt file, and the
next reader consumes the **pre-invalidation** catalog — uncopied, per the
consume-once rule above — so its in-place edits would corrupt it for every
reader after it. Leaving a stale memo populated across an invalidation is a
correctness bug, not a missed optimization: a test that seeded its own provider
saw a previous test's catalog win because the memory cache was cleared while
the memo was not.

Clearing the slot is not sufficient on its own, because the slot is cleared
*before* the generation advances. A producer that parsed the pre-invalidation
file can publish into that gap, and the stat identity of the rebuilt file can
still match the published entry. The
[generation fence](#generation-fence) is what closes that window: the
published entry records the generation it was parsed under, and both the
producer and the consumer refuse to hand out a superseded generation.

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
5. This is a runtime contract: changes here update this document and are
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

`tests/test_issue8302_models_cache_memo_identity.py` covers the parse memo's
runtime invariants: two concurrent consumers never receive the same catalog
object (the read-validate-clear path is mutually exclusive, and exactly one wins
the single-use slot), a same-size `os.replace` rewrite is not served from the
memo, `invalidate_models_cache()` drops the slot, and the single-parse fast path
still works for an unchanged file.

`tests/test_issue8302_models_cache_memo_invalidation_epoch.py` covers the
generation fence and the memo's lifetime: an invalidation that overlaps a
session visit's precheck must not let the visit serve the pre-invalidation
catalog (the rebuild keeps the file's stat identity identical, so only the
generation can tell the two apart), a memo entry from a superseded generation is
never served, and a visit served from the memory cache drops the precheck parse
instead of leaving a parsed catalog resident.

`tests/test_issue8302_models_cache_memo_contract_doc.py` covers this document:
it names the memo under "What is cached", records its file key, generation
fence, consume-once ownership and invalidation rules, and re-checks every
`api/config.py` symbol the contract names so the doc cannot drift away from the
code.

## References

Issues/PRs: #2443, #7540, #7556, #7558, #7723, #8302. RCAs: `t_d127953d`,
`t_16551f61`.
