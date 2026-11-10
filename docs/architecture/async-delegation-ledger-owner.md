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
  recorded profile first, then the authorized existing ledgers.
  - `found`: exactly one ledger has the row.
  - `missing`: every ledger was readable and none has the row — a genuine legacy
    no-row event, handled as before under the ambient home.
  - `unknown`: a ledger is unreadable, profile enumeration was incomplete, or
    the row exists in more than one ledger.
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
  authorized ledger. If enumeration is incomplete, replay stays pending for the
  next call. Atomic claims make a repeated launch-ledger restore harmless.

## Out of scope

Wakeup turns may still mirror the session profile into the process
`HERMES_HOME` while they run; that turn-level behavior predates this contract
and is not changed here.
