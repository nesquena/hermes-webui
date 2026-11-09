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

## Which rows are twins: the state.db row id

Text cannot tell two turns apart: a short reply ("Done.") or a prompt
("continue") repeats, either store can be missing rows the other holds
(archived, undone, compacted), and the two stores are stamped by two
processes' clocks. Every rule built on text, order or timestamps has paired a
turn with its copy in another turn and hidden a later Agent-only reply. So
rows pair only on proof: the state.db row id the two copies share.

1. **Stamping.** Before a Gateway run, `_run_gateway_chat_streaming` records
   the session's highest state.db row id (`state_db_message_id_floor()`). A
   reattached run reuses the floor its first worker saved in `gateway_run`.
   At the success writeback, once the stream is known to still own the
   session, the active user/assistant rows above the floor are the run's own
   (`state_db_session_rows_after()`). `stamp_gateway_turn_row_ids()` treats
   them as proof only while they hold exactly one turn -- a single user row.
   A concurrent Telegram turn for the same session, a steer, or a transcript
   rewrite (which reinserts the history above the floor) adds prompts, and
   then nothing is stamped. Inside one turn, a sidecar row takes the id of
   the single row matching it on the pairing key, together with that row's
   own state.db timestamp (`_state_db_row_timestamp`, private like the id).
   Any stamp the row carried before (a regeneration inherits the superseded
   run's) is dropped first, so an unproven row never pairs, and a failure
   while stamping leaves the rows unstamped rather than losing the reply.
   The in-process streaming path stamps its rows too, from the ids
   `append_message` returns rather than from a floor and a text match. The
   session transport is not stamped: it replaces the transcript with the
   Gateway's canonical rows.
2. **Exact lane.** Callers read the Agent store with row ids
   (`include_row_identity=True`) for this branch only. The ids are taken
   aside once; every other lane, and every row that leaves the merge, sees
   the default projection (`without_requested_row_identity()`), exactly as
   before. One row id on exactly one row per store, with the same role and
   pairing key and passing the checks above, is one message -- and, for a
   gateway stamp, only when the Agent row still has the stamped timestamp.
   A rewrite (`/retry`, `/undo`, `/compress`) reinserts rows under new ids
   with fresh timestamps, so a stamp it invalidated matches nothing. The
   Agent's `messages.id` is `AUTOINCREMENT`, so one file never reissues an
   id, but a state.db restored from a backup or recreated does; the
   timestamp is what keeps such a reissued id from pairing.

Nothing else pairs. A row without that proof renders as it did before this
reconciliation existed: a duplicate row is a cosmetic wart, a hidden reply
is lost data.

Only a pair with exactly one identified row reconciles; pairs where both or
neither carry an id are left to the merge-key and dedup lanes below. The
pass is linear in the number of rows.

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

An unidentified row with no paired survivor falls through to two checks,
never to the second-granularity merge key alone:

- **same store** — full-precision `_session_message_dedup_key()`. One store's
  own clock needs no rounding tolerance, so two same-second rows from one store
  are genuinely distinct messages.
- **opposite store** — the coarse merge key, and only against a row already
  kept from the other store. That is the case the rounding exists for: a legacy
  turn written to both stores with sub-second drift and no id on either side.
  This collapse is one-to-one as well, via a consumable pool per key and store.

## Known limits

- **Unproven twins render twice, as before this fix**: gateway turns written
  before stamping existed, turns the Agent renumbered (`/retry`, `/undo`,
  `/compress`), runs whose window held more than one prompt (steer,
  concurrent Telegram turn), and replies the stream joined across tool calls.
  The last is the common one for models that narrate before calling tools:
  `final_text` is every streamed delta, which matches no single Agent row, so
  only the prompt pairs. The complete fix is for the Gateway to report the
  row ids it persisted for a run, which needs a change in hermes-agent.
- **Stamping trusts that the Agent wrote the run's rows before the run
  reported completion**, as `append_message` does synchronously today. If a
  run's rows landed only after the next run recorded its floor, they would
  sit in that run's window; when it shows no rows of its own yet and the
  prompt and reply repeat the previous turn's text, the next turn would be
  stamped with the previous turn's ids.
- **A stamp the Agent has since renumbered contradicts the new id** wherever
  `_message_private_identity_compatible` or the append-only merge compares
  row ids, so an Agent row's `api_content` is not adopted onto that sidecar
  row, and in the append-only branch the two copies stay apart as on
  master. In-process rows already behave this way upstream.
- **Legacy both-unidentified twins with one-sided `api_content` still render
  twice**, because the coarse merge key includes that field and the two copies
  therefore never share a key. Unchanged upstream behavior, not introduced by
  the reconciliation.
- A false legacy pair that diverges **only** in `reasoning`, with no id, no
  row alias and no `api_content` on either side, is undetectable: no identity
  signal remains, and that lane pairs on visible content by definition.
- Two pre-existing failure modes are shared with `origin/master`: a non-numeric
  `timestamp` and a non-dict row each raise out of the `sorted()` key and 500
  the session, while every key *helper* tolerates both.

## Tests

`tests/test_gateway_dual_store_duplicate_turn.py` is the regression surface for
all of the above, including the directional lanes, one-to-one pairing, tool
identity, input immutability, the identity-compatibility guard, the exact
row-id lane, the shapes that broke every text-based fallback (an old turn
whose Agent copy is gone against a later Telegram turn with the same text, a
repeated prompt, rows before the first prompt, renumbered rows), the private
reader identity, a reissued row id, one id on two sidecar rows, and the
gateway writeback stamping (one-turn window, the stamped timestamp, dropped
inherited ids on the turn rows and on the eagerly saved display row, a
failure that keeps the reply, no read for a superseded stream).
