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
  resolved makes the enumeration incomplete. That includes a profile folder
  that is a live symlink to a directory outside the Hermes home: the Agent may
  serve that ledger but WebUI's containment check cannot read it, so the scan
  fails closed rather than treating its rows as missing.
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
  A failing restore retries on the 5 s routing interval and logs at most once
  per 300 s per ledger.
- **Writes stay on the launch ledger (replay-only across profiles).** Restores
  of the launch profile's ledger (`get_process_profile_home()`), and of the
  ambient ledger for legacy events, call the Agent's
  `restore_undelivered_completions()` as on master, including its abandoned-run
  recovery (`running` rows whose owner PID is not alive become `unknown`) and
  the 48 h `dropped` cap. Every other ledger is only read
  (`pending_webui_completions`): one read-only `SELECT` of
  `state != 'running' AND delivery_state = 'pending' AND event_json IS NOT NULL`
  rows, through the same read-only URI as the owner lookup. No Agent restore
  runs there and no row is rewritten; rows past the 48 h cap are skipped, not
  dropped, and are left to their owning process.
  - Only rows whose `origin_ui_session_id` is a WebUI session whose turns run
    in this process are replayed. Rows without a WebUI session (including a
    session id WebUI does not know), rows with an invalid session profile,
    rows from sessions on the Gateway backend (they are delivered through
    `gateway_delegation_wakeup`) and rows from runner-owned turns are
    confirmed exclusions: they are skipped and wait for their owning process.
  - A row whose session cannot be classified because of a read error is not
    treated as excluded. For an in-process session WebUI is the owning
    process, so skipping it would strand the result. That includes the
    backend choice: the shared config and `.env` readers map an unreadable or
    malformed file to "no settings", which reads as the local default, so
    before a row counts as local, replay re-reads the profile `.env` strictly
    and, unless the backend is set explicitly in the env, parses the same
    `config.yaml` the backend lookup used. A missing file is the local
    default; an unreadable, malformed or non-mapping one leaves the row
    undetermined. The shared readers are unchanged. The classified rows are
    queued, and the ledger then goes through the sweep's failure path: a
    rate-limited warning and a retry on the 5 s interval, until the row
    classifies.
  - Each replayed row is queued once, marked `restored`, until its copy is
    handed back (release or claim retry) or settled.
    `claim_completion_delivery` remains the only cross-process gate, so a row
    another consumer also offers is delivered once.

## Out of scope

Wakeup turns may still mirror the session profile into the process
`HERMES_HOME` while they run; that turn-level behavior predates this contract
and is not changed here.
