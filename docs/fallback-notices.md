# Fallback notices

Confirmed model switches appear inline on the affected assistant turn, including
empty or tool-only replies. The notice shows the new model/provider and survives
transcript rebuilds, session switching, and reload after successful persistence.
Settings → **Show fallback notices** controls visibility (enabled by default);
hiding notices does not remove saved metadata. Transient retry/rate-limit warnings
remain live status messages, not evidence that a switch succeeded.

## Persistence and ownership

`_fallbackNotice` is display metadata, allowlisted to `message`, `to_model`, and
`to_provider`. It belongs to the current turn, never an earlier assistant reply.
Success, terminal error, credential-recovery, and cancellation writers bind the
notice and durability accounting to the same immutable publication generation.
Normal completion settles newer accepted generations before retiring its worker.

If an initial or follow-up save fails, the newest accepted generation is retained
by the bounded, owner-scoped in-memory dead-letter registry. The transfer and
publication fence are atomic under `STREAMS_LOCK`. A retry of an older row cannot
downgrade that owner; teardown removes a live token only when its exact generation
was persisted or transferred. Same-content publications are still distinct
generations. The dead-letter is **not durable storage**: it has bounded capacity,
retry/deadline metadata, and is lost on process restart. A failed disk write does
not promise reload persistence.

Stale-run cleanup (the 180 s reaper ceiling in `_active_run_stream_for_session`
and `_active_run_ids_for_session`) must not discard an accepted notice before its
bounded 300 s deadline: abandonment transfers the newest accepted live generation
into the owner-scoped dead-letter, and preserves an existing unexpired entry
until its own `deadline_at`. Expired entries retire on the next cleanup pass;
repeated cleanup is idempotent.

Cancellation clears the active stream in memory even if persistence fails. A
successful retry accounts only the exact notice actually saved. The cancel API's
`persistence_failed` result means stopping and saving are separate outcomes:
clients release stopped-stream UI state but retain a persistence warning rather
than replacing it with a success notification.

## Regression coverage

`tests/test_fallback_initial_save_ownership.py` exercises initial-save failure,
no initial notice, same-content generations, and repeated failure using the real
publication gate, final-save wrapper, and worker retirement.
`tests/test_stale_reaper_settlement.py` production-composes both reapers:
an unexpired accepted notice is transferred or retained, an expired entry
retires, and repeated cleanup is idempotent and bounded. The cancellation,
post-CAS, and lifecycle-persistence suites cover neighboring settlement,
replacement, and pre-start exits.
