# Exact persisted-message replay guard

- **Status:** Proposed
- **Author:** @stefanpieter
- **Created:** 2026-07-27

## Problem

A WebUI display transcript can receive the same already-persisted assistant rows again during reconciliation. When those rows are replayed on successive turns, the sidecar grows approximately exponentially. This was observed in three sessions, including one with 591,595 rows from which a conservative repair retained 1,796 legitimate rows. The repeated rows carried the same stable message `id`, fractional timestamp, and complete payload.

The resulting sidecars and terminal `done` journals reached hundreds of megabytes to more than one gigabyte. Session loads, draft saves, stream-status reconstruction, and the watchdog then blocked on JSON I/O.

## Decision

`Session.save()` is the final persistence boundary and MUST remove from its persisted snapshot only exact duplicate message dictionaries that carry both forms of stable event identity evidence: an exact built-in scalar `id` and an exact built-in scalar `timestamp` (falling back to `_ts` only when `timestamp` is absent or blank). Supported scalar types are `str`, `int`, and finite `float`; strings must be nonblank. Booleans, subclasses, containers, and non-finite numbers are not identity evidence, and scalar types remain distinct.

Exactness is defined by a strict canonical-JSON fingerprint (`allow_nan=False`) and complete dictionary equality inside a type-tagged `(id, timestamp)` bucket. The fingerprint keeps many enriched variants of one stable event linear while the equality check makes hash collisions fail closed. Rows with the same `id` but different timestamps, metadata, content, or scalar identity types remain distinct. If either the `id` or timestamp evidence is invalid, blank, whitespace-only, or absent, the row remains untouched even when its role/content match; repeated identical user or assistant text can be legitimate.

The first occurrence is retained and ordering of every retained row is preserved. `Session.save()` does not rebind or mutate the live `messages` list: it snapshots and guards the rows locally, then uses that one guarded value for payload serialization, persisted `message_count`, shrink comparison, backup construction, and the sidebar-index entry. This preserves appends that arrive through an existing list alias while a save is scanning; a later serialized save can persist them. A warning records the number of replay rows omitted from the persisted snapshot. When the existing shrink-backup safeguard runs, the sidecar snapshot's exact stable replay rows are removed before its normal atomic `.bak` replacement. This guard performs no additional post-save rewrite of `.bak`; outside that pre-existing shrink behavior, a backup remains byte-for-byte untouched, avoiding a new check-then-replace race with another writer.

## Compatibility

- No user-visible unique message is removed.
- Repeated rows lacking stable identity are preserved.
- Existing corrupted sessions require a one-time lossless repair; the guard prevents recurrence on later saves.
- The guard is intentionally at the shared save boundary so every mutation path receives the same protection.

## Verification

A regression test must prove that:

1. separated exact copies with the same nonblank `id` and timestamp collapse;
2. same-id rows with different timestamps or metadata survive;
3. identical rows without both forms of stable identity survive;
4. the persisted `message_count` matches the guarded message array;
5. the sidebar index describes the same guarded transcript snapshot;
6. a pre-existing backup remains byte-for-byte unchanged;
7. malformed identities never authorize deletion and scalar identity types remain separate;
8. an alias append that races the guard remains attached in memory and is persisted by a later save.
