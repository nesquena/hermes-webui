# Gateway dual-store display merge

Current contract for the chronological-union branch of
`_merged_session_messages_for_display()` in `api/routes.py`: how one logical
turn that BOTH stores hold is reconciled into a single displayed row, which
copy survives, and which payload is allowed to travel between them.

This describes display reconciliation only. It does not change what either
store persists, and it must not: the rows it reasons about are shared with the
module-level session cache, and `Session.save()` rewrites the sidecar JSON from
that same in-memory object.

## Why two copies exist

With `HERMES_WEBUI_CHAT_BACKEND=gateway` the Agent executes browser turns, so
the browser session *is* the messaging session and one turn is written twice:

- the **Agent store** (`state.db`) holds the run's own row — no stable `id`,
  its own timestamp, and provider payloads such as `api_content`,
  `codex_message_items`, `codex_reasoning_items`;
- the **WebUI sidecar** holds a row written by `_run_gateway_chat_streaming`,
  stamped with a stable `id` by `_assign_stable_message_ids`.

`_session_message_merge_key()` keys an identified row as
`("message_id", id)` and an unidentified one as `("legacy", role, content, …)`.
Two key *shapes* never compare equal, so before this reconciliation existed the
turn survived twice and `GET /api/session` served two adjacent identical rows
while the sidecar JSON on disk held one.

## What may be paired

An **identified row is authoritative and is never reconciled away**: two rows
that both carry ids are distinct messages even when their text matches, because
a user can send the same prompt twice. Only an unidentified row is folded into
an identified one, and only across stores — a store may never dedupe against
itself, since neither input list is homogeneous (sidecar rows can mix
id-stamped and legacy rows after lineage stitching; the Agent store can carry
ids too).

Pairing is decided by three things, in order of cost:

1. `_cross_store_pairing_key()` — role, normalized visible content and
   `tool_calls`. Deliberately narrower than the shared
   `_session_message_visible_key()`: it omits the `api_content` suffix that
   `_session_message_key_with_sidecar()` appends, because that field is
   state.db-only by design and keying on it would give the two copies of ONE
   turn different keys. The shared key is intentionally left stricter — other
   merge paths depend on it.
2. Tool identity — `tool_call_id` and `tool_name`, which the pairing key does
   not carry. Two results from different tool calls that print the same text
   (`OK`, `{}`) would otherwise collide.
3. `_message_private_identity_compatible()` — contradictory stable id,
   contradictory state.db row alias, or two different non-empty `api_content`
   values each mean the rows are *not* one message. **Absence is not
   contradiction**: the common shape, where the Agent copy carries provider
   bytes the sidecar copy lacks, still pairs.

Rows that fail (3) are preserved as distinct. That matches
`_reconcile_api_content_sidecars()`, which already states the rule for this
module: if both sides carry different non-empty provider sidecars, neither is
attached and the rows stay separate.

Pairing is **one-to-one**. Each `(visible key, source store)` owns a FIFO queue
of still-unmatched survivors, built in transcript order; a match consumes
exactly one entry. Without that, repeated identical answers collapse onto one
row, or one kept row absorbs an unbounded number of opposite-store rows.

## What travels, and in which direction

The lanes are asymmetric on purpose:

| Payload | Direction | Policy |
| --- | --- | --- |
| Display metadata | either way | fill-only-if-absent |
| Semantic payload (`reasoning`, `reasoning_content`, `reasoning_details`, `codex_*_items`) | Agent → sidecar only | fill-only-if-absent |
| `api_content` | Agent → sidecar only | fill-only-if-absent |

Display metadata is sidecar-authored, so it travels either way. The Agent store
owns the real reasoning/Codex trace and is the only store `api_content` is
written to, while a *sidecar-authored* `reasoning` is the unreliable one — it
can hold a verbatim copy of the reply (NousResearch/hermes-agent#13007). So
neither semantic payload nor `api_content` is pushed into an Agent survivor.

Everything lands on a **copy** of the survivor. The reconciliation mutates
nothing it is handed, so a plain `GET`, or a metadata-only sidebar poll, cannot
seed a payload that a later unrelated save commits to disk.

## Dedup for rows that find no twin

An unidentified row that consumes no survivor falls through to two checks,
never to the second-granularity merge key alone:

- **same store** — full-precision `_session_message_dedup_key()`. One store's
  own clock needs no rounding tolerance, so two same-second rows from one store
  are genuinely distinct messages.
- **opposite store** — the coarse merge key, and only against a row already
  kept from the other store. That is the case the rounding exists for: a legacy
  turn written to both stores with sub-second drift and no id on either side.
  This collapse is one-to-one as well, via a consumable pool per key and store.

## Known limits

- **Occurrence order decides which survivor a repeat pairs with.** When a
  visible key repeats and the stores hold unequal numbers of those repeats (the
  sidecar has two identical answers, the Agent store only the later one), the
  oldest compatible survivor is consumed, so the later turn's payload can
  attach to the earlier stable id. Timestamps do not fix this in general:
  preferring the nearest candidate inverts the assignment whenever inter-store
  write drift exceeds the gap between consecutive repeats, which is the more
  common shape. A correct fix is sequence alignment across the two stores, not
  a per-row heuristic.
- **Legacy both-unidentified twins with one-sided `api_content` still render
  twice**, because the coarse merge key includes that field and the two copies
  therefore never share a key. Unchanged upstream behavior, not introduced by
  the reconciliation.
- A false pair that diverges **only** in `reasoning`, with no id, no row alias
  and no `api_content` on either side, is undetectable — no identity signal
  remains, and pairing on visible content is the premise of the branch.
- Two pre-existing failure modes are shared with `origin/master`: a non-numeric
  `timestamp` and a non-dict row each raise out of the `sorted()` key and 500
  the session, while every key *helper* tolerates both.

## Tests

`tests/test_gateway_dual_store_duplicate_turn.py` is the regression surface for
all of the above, including the directional lanes, one-to-one pairing, tool
identity, input immutability, and the identity-compatibility guard.
