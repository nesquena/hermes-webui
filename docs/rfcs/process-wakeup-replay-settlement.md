# Process-Wakeup Replay Settlement Contract

- **Status:** Proposed
- **Author:** @franksong2702
- **Created:** 2026-08-11
- **Tracking:** [#6749](https://github.com/nesquena/hermes-webui/issues/6749), [#6758](https://github.com/nesquena/hermes-webui/pull/6758)
- **Related contracts:** [`live-to-final-assistant-replies.md`](live-to-final-assistant-replies.md), [`webui-run-state-consistency-contract.md`](webui-run-state-consistency-contract.md), [`turn-journal.md`](turn-journal.md), [`hermes-run-adapter-contract.md`](hermes-run-adapter-contract.md)

## Problem

A recovered `process_wakeup` turn can contain two observations of one apparent
assistant/tool/final arc: an arc already present in the durable WebUI transcript
and an arc returned by Hermes Agent after the active wakeup checkpoint is
materialized. Removing the older observation can repair a duplicate transcript,
but a false match deletes real conversation history.

PR #6758 demonstrated useful fail-closed checks for row order, tool closure,
stream/turn/run agreement, reasoning transfer, and exact content. It also exposed
three structural gaps that cannot be solved safely by extending a content matcher:

1. replay removal currently runs during display merge, before terminal failure,
   cancellation, compression, and persistence outcomes are final;
2. recovery rows do not currently carry one producer-owned identity contract;
   production journal recovery stamps a recovered stream identity, while the
   active turn, turn journal, WebUI run journal, and Agent result can obtain
   `turn_id` and `run_id` from different sources;
3. the message dictionary is open-ended, so a field allowlist can silently omit
   semantic data such as tool `name`, assistant `refusal`, or provenance.

This RFC defines the evidence required before WebUI may remove a recovered arc.
Until every requirement is implemented, destructive cleanup remains disabled.

## Goals

- Define one authoritative owner identity for a WebUI turn and its recovered
  message arc.
- Make replay reconciliation a non-mutating prepare step followed by explicit
  terminal classification and an atomic commit.
- Compare complete semantic rows and preserve history on unknown differences.
- Preserve truthful error, cancel, interrupt, tool-limit, compression, and
  persistence outcomes.
- Make cleanup idempotent across `Session.load`, replay, and a second settlement.
- Provide an incremental rollout with a default-off destructive phase and a
  simple rollback.

## Non-goals

- Do not deduplicate ordinary user-authored turns by content.
- Do not use `_partial_tool_calls.done` as proof that canonical tool calls closed.
- Do not rewrite the Hermes runtime adapter or make WebUI a second run owner.
- Do not repair legacy rows by inventing identity from timestamps or text.
- Do not delete generated errors, partials, or assistant/tool rows when the
  terminal outcome is ambiguous.
- Do not enable implementation phases merely because this RFC is proposed.

## Authority Model

### Owner tuple

Every row eligible for destructive replay settlement belongs to one immutable
owner tuple:

```text
(session_id, stream_id, run_id, turn_id, arc_id)
```

Each row also carries a stable `message_id`. `message_id` proves row lineage;
`arc_id` groups the ordered checkpoint/tool/final sequence. Neither identity is
a substitute for semantic equality.

All tuple members must be non-empty strings. Conflicting aliases, missing
members, or a type other than string make the row ineligible for deletion.

### Producers and consumers

| Value | Producer | Durable propagation | Consumers |
| --- | --- | --- | --- |
| `session_id` | WebUI session creation/resolution | session sidecar, turn/run journals | worker, recovery, settlement |
| `stream_id` | WebUI chat-start acceptance | pending turn metadata, turn/run journals, recovered rows | SSE, worker, recovery, settlement |
| `run_id` | authoritative runtime/adapter run creation | run journal envelope and pending turn metadata | Agent result, recovery, settlement |
| `turn_id` | turn-journal submission boundary | submitted event, pending turn metadata, every recovered row | worker, Agent result, settlement |
| `arc_id` | producer when the canonical assistant arc begins | every checkpoint/tool-call/tool-result/final row | recovery and settlement only |
| `message_id` | producer when each canonical row is created | transcript, context, journal projection | replay, reload, settlement |

The current implementation does not yet satisfy this table:

- `_active_turn_authority` reads `session.pending_run_id`, but no current
  producer reliably writes that value for this path;
- the WebUI run journal is keyed by the WebUI stream id, which must not be
  silently treated as an Agent-owned run id;
- journal recovery stamps `_recovered_stream_id` on reconstructed output but
  does not propagate the complete tuple to every row;
- the turn journal creates a `turn_id`, but the value is not yet carried through
  pending metadata and every recovered assistant/tool row.

Implementation must close these producer gaps before destructive settlement is
enabled. Aliases may be accepted at an ingestion boundary only when they resolve
to one value; conflicting aliases fail closed. The normalized stored form must
have one documented field per identity component.

## Eligible Arc Shape

An eligible arc is one contiguous, token-owned sequence:

```text
active checkpoint
assistant(tool_calls=[...])
tool(result for next outstanding call)
... zero or more additional closed assistant/tool groups ...
assistant(final, no tool_calls)
```

The sequence must satisfy all of the following:

- the checkpoint and every later row carry the same complete owner tuple;
- every row has a stable `message_id`, unique within the session;
- each assistant tool-call row contains at least one unique call id;
- tool results close outstanding calls in order;
- no user/system row, plain intermediary assistant, orphan tool result, nested
  call group, partial final, or second final appears inside the arc;
- the final contains user-visible assistant content and no new tool calls;
- exactly one old contiguous arc and one canonical contiguous arc share the
  same ordered message identities;
- the old arc is immediately adjacent to the owned checkpoint in the recovery
  shape defined by this contract.

Any other shape is report-only and remains unchanged.

## Complete Semantic Row Equality

Stable identities authorize comparison; they do not authorize deletion. For
each old/canonical row pair, settlement compares the complete row after removing
only the following documented volatile fields:

- render-time timing and usage projections: `_turnDuration`, `_turnTps`,
  `_firstTokenMs`, `_usedModel`, `_gatewayRouting`;
- local serialization timestamps when stable identity is already present:
  `timestamp`, `_ts`;
- reasoning fields handled by the transactional rule below:
  `reasoning_content`, `reasoning`, `_reasoning`, `thinking`.

The comparison includes `role`, typed/presence-aware `content`, `name`,
`refusal`, tool call ids, full tool names and arguments, full tool results,
stable message/arc ids, owner identity, and recovery provenance. It also includes
every unknown field. Therefore an unknown field present on only one row, or with
a different value, preserves both arcs.

Values are canonicalized without whitespace normalization or truncation. JSON
object key order may be normalized; list order and scalar type remain semantic.
Missing, `null`, empty string, empty list, and empty object remain distinct.
Non-finite numbers, cyclic values, non-string object keys, or unsupported values
fail closed.

### Reasoning transfer

Reasoning fields may be missing from the canonical projection while present on
the old display row. Prepare stages a deep copy of those fields only when every
non-reasoning semantic field matches. If both rows contain a reasoning field,
the values must be exactly equal. The staged transfer is applied only in the
same atomic commit that removes the old arc; any later guard or persistence
failure discards it.

## Prepare, Classify, Commit

### 1. Prepare

Prepare is pure and non-mutating. It may return a cleanup plan containing:

- the normalized owner tuple;
- ordered old and canonical `message_id` values;
- the exact old-arc location relative to the active checkpoint;
- canonical semantic fingerprints;
- staged reasoning transfers;
- bounded diagnostic reason codes.

Prepare returns no plan when identity, closure, uniqueness, adjacency, semantic
equality, or serialization is uncertain. It never removes or edits a row.

### 2. Classify

A plan is authorized only after WebUI has positively established normal
completion. Authorization requires all of the following, not merely the absence
of one known error:

- `result.completed is True`;
- `failed`, `partial`, `interrupted`, and `compression_exhausted` are explicitly
  false;
- result status is an accepted completed/success state;
- `finish_reason` and `turn_exit_reason` describe a normal text completion;
- `final_response` is non-empty and corresponds to the tuple-owned final row;
- the canonical tool chain is closed;
- no captured provider/auth/quota error, cancellation flag, tool/max-iteration
  limit, compression failure, or no-response classification exists;
- the stream still owns writeback;
- the ordinary, non-destructive session save has succeeded;
- the turn-journal terminal state is `completed`, with the same turn/stream
  owner, and no terminal collision is present.

Self-heal and retry paths must produce a new plan and proof from the successful
result. They must not reuse a plan prepared from the failed result.

### 3. Commit

Commit revalidates the plan against the in-memory session using stable ids and
the active checkpoint. It builds the cleaned transcript and reasoning transfer
on a deep copy, then persists through the existing atomic session-save boundary.

If cleanup persistence fails:

1. restore the unmodified in-memory transcript;
2. keep the already-saved non-destructive transcript on disk;
3. emit a bounded cleanup diagnostic;
4. preserve the truthful successful terminal outcome;
5. do not turn cleanup failure into a provider failure.

Cancellation or interruption observed before commit discards the plan. A late
cancel must never leave a cleaned successful transcript paired with a cancelled
terminal state.

## Legacy Data

Rows without the complete normalized owner tuple, stable `arc_id`, and stable
`message_id` are legacy rows. They may be counted and diagnosed, but not deleted.

There is no content-only destructive fallback. A future migration may attach
identity only when a durable producer record proves it; it must not infer identity
from content, timestamps, adjacency, or an equal tool-call id alone.

## State Matrix

| State | Prepare | Authorize | Persist cleanup |
| --- | --- | --- | --- |
| normal completion, closed owned arc | plan | yes after durable completion | yes |
| provider/auth/quota error after final text | optional diagnostic | no | no |
| cancelled or interrupted | optional diagnostic | no | no |
| partial or missing tool result | no plan | no | no |
| tool/max-iteration limit | optional diagnostic | no | no |
| compression failed/exhausted | optional diagnostic | no | no |
| ambiguous/multiple matches | no plan | no | no |
| missing/conflicting identity | no plan | no | no |
| unknown semantic difference | no plan | no | no |
| ordinary session save failure | discard | no | no |
| cleanup save failure | restore | proof remains success | no |
| second settlement/reload | no-op plan or already-clean | no second delete | no-op |

## Required Regression Gate

The enabling implementation must use the production chat-start, turn-journal,
run-journal recovery, Agent-result settlement, and `Session.save/load` path. A
helper-only synthetic matcher test is insufficient.

The positive fixture contains an owned process-wakeup checkpoint, multiple
ordered tool pairs, full tool names/arguments/results, a final with `refusal` and
provenance fields, stable message/arc ids, a completed terminal record, a hard
`Session.load`, and a second settlement. It must prove:

- exactly one checkpoint/tool/final arc remains;
- the final answer and all tool semantics are byte/semantic equivalent;
- reasoning is preserved exactly once;
- the run and turn journals contain one non-colliding completed terminal;
- the second settlement is a byte-for-byte or canonical semantic no-op.

Parameterized negative cases must preserve the old arc and truthful terminal:

- tool `name`, arguments, result content, assistant `refusal`, provenance, or an
  unknown field differs;
- any owner, `arc_id`, or `message_id` is missing, duplicated, or conflicting;
- tool-before-call, missing result, orphan result, partial final, or intermediary
  assistant;
- provider/auth/quota failure after final text;
- cancellation before classify, before save, and immediately before commit;
- interrupted, partial, no-response, tool-limit, and max-iteration results;
- compression failed/exhausted and continuation-session rotation;
- ordinary save failure and cleanup-save failure;
- reload after the non-destructive save but before cleanup commit;
- repeated prepare/commit attempts and replay from the same cursor.

Every regression must demonstrate that it fails against the unsafe behavior and
passes after the relevant phase is enabled.

## Observability

Diagnostics are bounded counters or structured log reason codes; they never
contain message content, tool arguments/results, credentials, or private prompt
text. Suggested counters:

- `wakeup_cleanup_candidate`
- `wakeup_cleanup_legacy_identity`
- `wakeup_cleanup_identity_conflict`
- `wakeup_cleanup_semantic_mismatch`
- `wakeup_cleanup_terminal_not_authorized`
- `wakeup_cleanup_commit_success`
- `wakeup_cleanup_commit_failed_preserved`

Logs include only session/run/turn identifiers already permitted by existing
diagnostics, the reason code, row counts, and whether the path was report-only.

## Rollout Plan

### Phase 0: Disable and observe

- keep destructive wakeup replay stripping disabled by default;
- add bounded report-only diagnostics for candidate shapes;
- preserve the existing transcript on every path.

Rollback: remove or disable diagnostics. No data migration is required.

### Phase 1: Close producer identity gaps

- define the normalized owner fields;
- persist `run_id` and `turn_id` from their authoritative producers;
- propagate owner tuple, `arc_id`, and `message_id` through pending metadata,
  Agent result projection, run/turn journals, recovery rows, transcript, and
  context;
- add producer/consumer contract tests without enabling deletion.

Rollback: stop writing new optional identity fields. Readers remain tolerant and
destructive cleanup remains off.

### Phase 2: Prepare and classify

- implement the pure cleanup plan and complete semantic comparison;
- add positive normal-completion proof and the full negative state matrix;
- keep commit report-only.

Rollback: disable plan construction/diagnostics.

### Phase 3: Atomic commit

- enable cleanup behind one default-off feature flag after the production-route
  and reload-idempotency gates pass;
- canary on test state, then opt-in deployments, then consider a default change
  only after maintainer review of diagnostics;
- remove the current semantic-only destructive matcher when the new path is the
  sole implementation.

Rollback: turn off the feature flag. Because legacy/report-only paths never
delete, no reverse data migration is required.

## Open Questions

1. Is the authoritative runtime `run_id` available before the first event on
   every legacy and adapter-backed path, or must WebUI preserve a distinct
   `stream_id`-to-`run_id` mapping event?
2. Should `arc_id` be runtime-owned, turn-journal-owned, or derived from a
   producer-issued final/message lineage id?
3. Which existing message metadata fields are confirmed volatile across
   recovery projections? The allowlist must remain explicit and reviewed.
4. Should a completed turn-journal event be written before cleanup authorization,
   or should cleanup commit and terminal event share a higher-level transaction?
5. Which feature flag and diagnostic surface should Phase 3 use?

This RFC remains `Proposed`. No destructive implementation phase is authorized
until maintainers accept the identity source, volatile-field allowlist, terminal
ordering, and rollout gate.
