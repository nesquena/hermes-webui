# Occurrence identity, end to end — design note (#7653 round 12)

## The root cause

Tool calls are identified by `tid` alone through the whole pipeline. Providers
that reuse an id (`call_0` per turn, llama.cpp-style re-use, or a completed id
that a later turn re-arms) make `tid` ambiguous: two occurrences of one tid
share every tid-keyed map, so a failure verdict, a metadata field, or a render
key attached to "the tid" lands on **both** occurrences. Every finding in the
14/10 re-gate is one surface of this single defect.

## The identity model

An occurrence is identified by the triple `(tid, occurrence ordinal,
assistant_msg_idx)`:

* **tid** — the provider id. Used only to *group* occurrences of the same
  call; never alone as an identity.
* **occurrence ordinal** — a per-stream counter minted server-side when a
  start event arrives for an id this stream already saw (the rearm branch of
  `on_tool_start`). It distinguishes two live rows that share a tid *while the
  stream is running* and is carried on the event, the live mirror rows, and
  the persisted summary.
* **assistant_msg_idx** — the owning assistant message's index in the
  transcript. This is the durable identity: it survives reload, and it is the
  one the browser can match against rendered rows (which carry the message
  index). The server writes it into every `s.tool_calls` entry and rebases it
  into the returned window (`_tool_calls_for_message_window`,
  `api/routes.py`).

The browser keys verdicts, metadata, and render/dedup rows by
`(tid, assistant_msg_idx)`, falling back to the occurrence ordinal where the
index is not yet known (live stream-born rows).

## The chain, and how each finding collapses into it

| Finding | Surface | How the unified design resolves it |
|---|---|---|
| **1. Paging moves verdicts onto the wrong owner** | `static/sessions.js` `_loadOlderMessages` / `_syncToolCallsForLoadedMessages` | The server already rebases `assistant_msg_idx` into the returned window. Response summaries keep their indices unchanged in **both** paging strategies; prepends merge the **retained** transcript's summaries and shift only *their* owners by the page length (the retained rows moved down the combined array). |
| **2. Reload-then-reuse is still ID-wide** | `static/sessions.js` `_persistedIsErrorByTid`; `static/messages.js` `_mergeSettledToolCallsWithLiveMetadata` call sites | Every verdict (including singletons) is scoped to a verified owning `assistant_msg_idx`; unowned entries are skipped (fail closed — a row without a verdict renders from its own data). The map is rebuilt before the done/recovery reconciliation, and stream-born rows receive their real assistant owner. |
| **3. Same-id calls collapse into one card** | `static/messages.js` `_anchorSceneExistingRowKey`; `static/ui.js`; `api/streaming.py` rearm; `api/routes.py` replay | The occurrence ordinal is carried through live rows, the Anchor projection, saved summaries, replay, hydration, and every tool render/dedup key, so two occurrences of one tid produce two distinct keys and two cards. |
| **4. Recovery loses the current tool's `duration`/`started_at`** | `static/messages.js` `_mergeSettledToolCallsWithLiveMetadata` | Live metadata is reserved for **explicit-id** matches (occurrence-exact first, then the id's own unused live rows) *before* the name fallback, so an older same-name row can no longer consume the current row's live row. Verdict transfer stays occurrence-scoped (never driven by the loosened metadata match). |

## Implementation status

* **Finding 1 — implemented this round.** Response summaries keep the
  server-rebased indices; the prepend path merges the retained verdict map
  with owners shifted by the page length.
* **Finding 4 — implemented this round.** Explicit-id metadata reservation
  before the name fallback; verdict transfer unchanged (occurrence-exact
  only).
* **Findings 2 and 3 — designed, deferred to the next round.** They require
  threading the occurrence ordinal through the server event path and the
  browser render/dedup keys (a larger change than the point fixes above); the
  design above is the plan, and the round-11 occurrence-id work on the rearm
  events is its first half.
