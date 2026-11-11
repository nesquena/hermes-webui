# Async delegation ledger owner

Each profile has its own Agent ledger (`<profile home>/state.db`,
`async_delegations`). The Agent resolves that ledger through its
context-local Hermes-home override, then `HERMES_HOME`. A WebUI consumer that
claims or acknowledges a completion under the wrong home either misses the row
(a legacy no-row claim then "succeeds" while the owner row stays `pending`) or
restores nothing (#7214).

## Contract

- **Owner resolution** (`api/delegation_ledger_owner.resolve_ledger_owner`) is
  read-only and never creates a database. It checks the origin WebUI session's
  recorded profile first, then the authorized existing ledgers. Authorized
  ledgers are the root home plus the profile directories read from disk on each
  call, not the cached profile list the UI shows, so a profile created outside
  WebUI is never treated as absent. Entries that are not directories (stray
  files, dangling or looping symlinks) and tombstoned profiles, whose ledger
  the Agent refuses to open, are skipped. Any other entry that cannot be
  resolved makes the enumeration incomplete.
  - `found`: the origin session's profile ledger has the row (provenance
    decides, even if another ledger also has it); otherwise, exactly one
    authorized ledger has the row.
  - `missing`: every ledger was readable and none has the row — a genuine legacy
    no-row event, handled as before under the ambient home.
  - `unknown`: no provenance match, and a ledger is unreadable, profile
    enumeration was incomplete, or the row exists in more than one ledger. The
    event is deferred, not claimed, with a rate-limited INFO log.
  There is no owner cache, so there is no stale positive or negative entry.
- **One owner per claim.** `claim_async_delegation_delivery` resolves once and
  stores `ledger_home` on the claim. ACK, release and retry after a release use
  that home; they do not re-resolve.
- **Context-local only.** Ledger calls run inside `ledger_home_scope(home)`,
  which uses the Agent's context-local override (root included) and never
  writes `os.environ`. If that override API is present but fails, the ledger
  call does not run; Agent cores without the API keep the ambient ledger.
- **Unknown is neither success nor rejection.** An unknown owner is not
  claimed; the event is requeued by the shared retry sweep and resolved again.
- **Retry sweep.** The single shared timer restores only the owner ledgers that
  were queued for it (`None` = ambient, for legacy events) and requeues
  deferred unknown-owner events. It is not a recurring scan of every ledger.
- **Startup replay.** The Agent replays only its launch profile. On the first
  `restore_durable_process_completions` call, WebUI queues one restore of every
  authorized ledger. If enumeration is incomplete, a single delayed timer
  retries the replay, because the background drain calls the restore only once
  at startup. Atomic claims make a repeated launch-ledger restore harmless.
  Each restore is the Agent's `restore_undelivered_completions()`, which is not
  read-only: under each profile's home it also runs the Agent's abandoned-run
  recovery (`running` rows whose owner PID is not alive become `unknown`) and
  marks pending rows older than the 48 h replay cap `dropped`. The same writes
  happen when that profile's own process next starts; whether WebUI should make
  them for other profiles is an open scope question on the PR. A failing
  restore retries on the 5 s routing interval and logs at most once per 300 s
  per ledger.

## Out of scope

Wakeup turns may still mirror the session profile into the process
`HERMES_HOME` while they run; that turn-level behavior predates this contract
and is not changed here.
