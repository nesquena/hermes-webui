# Session append-only fast path contract

Current contract for the append-only reload of `GET /api/session`: when a
browser that holds more rows than the window ceiling refreshes a session, it
may ask for only the rows strictly after its last rendered index instead of
re-downloading the whole transcript — and the checks that decide when that is
safe.

Two ideas carry the whole thing, and both are load-bearing:

- **The proof is the projection.** The server does not hash the transcript
  and then approximate what it will deliver. It builds the one *delivered
  projection* of the covered rows — the same transform the response body is
  serialized from — and hashes that. The proof and the delivered rows are two
  views of the same bytes, so they cannot describe different things.
- **The client's claim is row-gated.** The digest is an interval-level
  statement ("absolute rows `[0, K)` are as I last delivered them") that the
  browser cannot split into per-row proofs, so it records, per held row,
  whether that row is a verbatim server row — and only sends the append when
  every held row qualifies and the interval is contiguous.

Start here before changing the append request/response shape
(`msg_after` / `anchor_fp` / `transcript_generation` / `prefix_fp`), the
state.db loader or display-merge cache decisions on that path, the append
window extraction, the delivered projection, or the client-side merge in
`static/sessions.js` (#7925, #7899).

## When the fast path applies

The client sends the append shape only when **all** of these hold:

- the reload width (the transcript it holds) exceeds the server's
  `_msg_limit_max` ceiling, so a bounded replace cannot express it;
- the client still holds a contiguous transcript (`S.messages` non-empty);
- the session exposes a finite `transcript_generation`;
- **every row the client holds is a verbatim server row**, and the held
  interval ends exactly where the stored digest's coverage ends
  (`_sessionPrefixFpEnd === _oldestIdx + S.messages.length`) —
  `_heldRowsArePrefixProven()`.

That last condition is the whole admission test, and it is deliberately
per-row. The digest covers absolute rows `[0, K)` as an interval the client
cannot split, so what it can honestly vouch for is *which* of its held rows
are the server's own copies. A row fetched with `msg_before` qualifies — it
is a server row like any other, and paging back must not cost the next focus
reload its fast path. A row the client synthesised locally does not: a
streamed turn, an INFLIGHT live-tail merge, a recovered prompt, a handoff
marker. Neither does a row that arrived through the lossy transport — the
bounded-payload clipper's preview, `_content_truncated` (see "The proof
describes the row; clipping is transport"). Anything unproven, and the reload
takes the bounded replace or the authoritative full fetch — master's shapes.
Only focus, visibility, and SSE-driven reloads of an already-loaded session
reach this path; other external-refresh reasons clear `S.messages` first and
reload from scratch.

## Request fields

| Field | Meaning |
|---|---|
| `msg_after=N` | The client's last rendered absolute row index; the response must start strictly after it. Mutually exclusive with `msg_before` (paging wins). |
| `anchor_fp` | FNV-1a digest of the anchor row's **public projection** (internal fields stripped, content redacted) — the exact bytes the client rendered. Minted browser-side; the server re-derives it. |
| `transcript_generation` | The generation the client's view was built under. Bumped only on destructive rewrites (truncate/retry/undo). |
| `prefix_fp` | The server-computed digest over rows `[0, N+1)` of the merged display document, echoed back from the load that established the client's view. Server-minted and server-verified; the browser only stores and returns it. The digest's input is the **delivered projection** of the covered rows — scene-hydrated, internal fields stripped, credentials redacted, active-turn marked, plus the covered prefix's own session-level tool cards in their delivered form — i.e. the very bytes the response serializes (see "The projection is the proof"). The format is domain-separated by the tag `prefix_fp/3`, which sits inside the hash: a digest minted by an older build can never compare equal to a new-format one, so a format change fails closed instead of silently reusing a stale proof. On a display-merge cache hit it is reused from the cache entry instead of re-minted. |
| `msg_limit` | Bounds the **returned window** only. It never selects the limited-display document: an append request's anchor validation and merge run against the full display document, exactly like master's bare GET. The payload clipping it can trigger is transport-layer and stays out of the proof (see "The proof describes the row; clipping is transport"). |

`anchor_fp` and `prefix_fp` are deliberately different mechanisms:
`anchor_fp` is client-derived (so it proves what the browser rendered, byte
for byte, across the JS/Python boundary), while `prefix_fp` is server-derived
(so it can cover thousands of rows without a browser re-hash).

## Refusal semantics

Every check that fails — unparsable or out-of-range `msg_after`, generation
mismatch, anchor digest mismatch, prefix digest mismatch or absence — makes
the server answer with the **authoritative full transcript** and no
`_append_only` flag, which is exactly what master returns for the same
request. A refusal is always safe; the fast path is a pure optimization and
can never serve rows the client should not keep.

`prefix_fp` is the check that closes the non-destructive-edit hole: the
generation bump only reports destructive rewrites, so a settled-reasoning
backfill or a saved attachment on an *earlier* row would otherwise leave the
client stitching a stale prefix onto a fresh tail. The digest is complete by
construction (2026-10-29 finding 2): each covered row is hashed as the
browser receives it, so any *new* rendered field is covered by default
instead of being missed by a whitelist. The 2026-10-31 re-gate then proved
that out-of-row inputs — a scene save, a redaction flip, an active-turn
boundary, a card summary edit — also move a covered row's delivered bytes;
that round bound them one by one, and the by-construction round settled the
point structurally instead (below). It is a fingerprint rather than a
revision counter because a counter must be bumped by every writer that
touches an earlier row, and one missed writer means silently stale rows.

The whole-document projection is computed once per merge and shared by the
cache entry, the response body and the mint: on a display-merge cache hit
the entry's projection is sliced down to this request's coverage, and the
entry's digest is reused outright when the coverages agree — full coverage,
which is every full load and every tail window that reaches the document
end — guarded by the entry's `meta`, while a cold load projects the document
exactly once instead of building the proof and the body separately. The
hash itself only ever covers `[0, count)`, so an ordinary windowed open pays
for one whole-document projection, never a second whole-document hash.

## The projection is the proof (2026-11-02 by-construction)

Through the 2026-10-31 re-gate the digest still hashed the *pre-transform*
rows and then mixed out-of-row "seeds" into the hash to compensate for the
transforms the delivery applies afterwards. That shape is gone. The GET path
now builds one projection of the covered rows —
`_display_prefix_projection` — by running the delivery's own transforms, and
the digest hashes the serialization of the result. Two steps, both of them
the delivery code itself:

1. **Scene hydration** — `_hydrate_anchor_activity_scenes` over the request's
   scene records (and the same session-level card list), at
   `message_offset=0` so a row's absolute index is its own. A saved anchor
   scene attaches `_anchor_activity_scene` / `_anchor_stream_id` to an
   assistant row without modifying any row — exactly the kind of input
   the old approximation had to reach from outside.
2. **The public projection** — `_public_message_projection`, the very
   function `redact_session_data` uses to build the response body: it strips
   the internal replay fields, applies credential redaction under the
   captured `api_redact_enabled`, and marks a covered user row
   `_active_turn_user: true` when the row's own `_active_turn_token` equals
   the session's active-turn token (`ui.js` and `sessions.js` read that
   flag).

The covered prefix's own session-level tool cards ride along in their
delivered (scrubbed, redacted) form — the response's `tool_calls` field for
that prefix — so a card summary edit on a row the client already holds moves
the digest too.

The handler slices the response body out of that same projection — the
returned window is the projected rows from the response's own offset, so the
body and the digest can never describe different rows — and hands the
projection to the mint, so the proof and the body are two views of one
computation. The format is domain-separated by the tag `prefix_fp/3`
(bumped from `/2` when the input became the delivered projection): the tag
is inside the hash, so an older build's digest can never compare equal to a
new-format one, and a format change fails closed.

The strip boundary the digest used to maintain by hand is still pinned —
`_PREFIX_FP_EXCLUDED_FIELDS` remains, as the strip set minus
`_active_turn_token` (stripped before delivery like the rest, but an *input*
to the delivery rather than dead weight: its effect is hashed through the
`_active_turn_user` marking the projection applies) — but it no longer
selects the hash input. The projection performs the strip, so an excluded
field cannot reach the hash at all.
`test_prefix_fingerprint_exclusion_set_is_explicit_and_rendered_fields_covered`
still enumerates the boundary and the default-coverage property (a new
unlisted field moves the digest; a stripped one never does).

**One behaviour change worth stating.** The scene-hydration guard counts how
many assistant rows share a message ref among the rows it is *handed*, so the
count is scope-dependent. The projection hydrates the covered PREFIX
(`message_offset=0`), while master's windowed path hydrated the WINDOW slice
(`message_offset=<offset>`). The two disagree in exactly one case: a ref that
occurs twice in the prefix but once inside the window. Master then matched by
ref and attached the scene; the prefix-scoped hydration counts two, treats the
ref as ambiguous, and falls through to the positional index match — so that row
usually carries no scene. This is the stricter reading of the guard's intent
(the same scene must not attach to two assistant rows, and master's window
scope cannot see a duplicate outside the window), but it is a user-visible
difference in an extreme corner — two assistant rows with identical
whitespace-normalised content *and* identical timestamps.
`tests/test_7925_projection_scope_agreement.py` pins the divergence itself
and the property that the delivered window is the projection's own slice.

### Why the seed mechanism is gone

Not because the four seeds were individually wrong — each was added because
a finding proved a real hole — but because the shape was wrong. A seed is a
second, hand-maintained model of a transform the delivery code also
performs: every future change to the delivery must be mirrored in the seed,
and the two silent findings of the 2026-10-31 re-gate (a scene-free session's
cards never reaching the digest; the redaction setting applied after the
mint) plus the maintainer's two mid-request reproductions were that
mirroring failing in practice. The input-inventory table this document used
to carry was the same problem in documentation form: it could only ever be
complete with respect to the inputs somebody had already thought of.

With the projection there is nothing to keep in sync. A transform that
cannot change a delivered byte cannot move the digest, and one that can is
inside the hashed bytes — a scene save, a redaction flip, an active-turn
boundary and a card summary edit all move the digest because they all move
the delivered rows. Completeness no longer rests on enumerating the GET
path's inputs; it rests on the delivery code, which is the one place that
knows what it delivers. A new transform is covered the day it is written,
with no doc edit and no test remembering to bind it.

The work stays bounded (#7899): the projection covers exactly `[0, count)` —
the client's held prefix, the widest range an append can vouch for — and
never walks past it. The slice happens before any traversal, hydration is
skipped entirely when the session holds no scene records, and the card work
is skipped when it holds no cards, so a document longer than the covered
prefix costs nothing extra. Rows at or after `count` belong to the appended
window and are not the proof's business.

### The proof describes the row; clipping is transport

`prefix_fp` describes a row's **semantic projection** — what the row *is*,
as delivered. `_messages_for_limited_payload` is a **transport-layer** bound
on over-long hidden tool-result payloads: it replaces the content with a
preview and stamps the row `_content_truncated` /
`_content_original_chars`. The two are deliberately separate:

- clipping runs *after* the projection, only on bounded windows (any
  `msg_limit`: windowed opens, `msg_before` paging, and the append window —
  the full fetch skips it), and never enters the digest. Whether a row is
  clipped depends on which window it lands in, so hashing the clipped form
  would make the same row hash differently at mint and at verify time and
  refuse appends neither side changed;
- the client therefore keeps a clipped row's range out of the fast path:
  `_content_truncated` is visible to it, such a row never joins the proven
  set (see "Client-side invariants"), and on the append path
  `_messages_truncated` folds the clip in — so the reload answers with the
  authoritative full fetch, which skips the clipper and repairs the row.

The covered prefix's card boundary is unchanged and still pinned by
`test_prefix_fingerprint_excludes_cards_owned_by_appended_rows`: a card
owned by a row at or after the coverage end belongs to the appended rows and
stays OUT, or every turn that runs a tool would refuse its own append. A
card whose `assistant_msg_idx` is not an integer cannot be attributed to any
row, so the window selector drops it — but the response path still delivers
such a card on a full load, so it is covered unconditionally rather than
dropped.

Still outside the proof, and now structurally so rather than by decision:

- `title` — redacted like a row, but a header field, not a transcript row.
- `todo_state`, `runtime_journal`, `runtime_journal_snapshot` — panels, and
  derived from the merged rows.
- `model` / `model_provider`, `context_length`, `threshold_tokens`,
  `last_prompt_token`, `regeneration_revision`, `read_only`,
  `is_cli_session`, `continuation_session_id`, `_messages_truncated`,
  `_msg_limit_max` — session metadata and response flags.
- `pending_user_message` / `pending_attachments` / `pending_user_source` —
  the optimistic bubble, not a transcript row. The materialized user row
  *is* a row and is covered; the turn boundaries that change the pending
  state also move the active-turn token, hashed through the
  `_active_turn_user` marking.

None of these is a held transcript row's projection, so none can move a
covered row's delivered bytes — which is why they need no binding at all
now, where the previous round had to argue each one out.

## Response fields

| Field | Meaning |
|---|---|
| `messages` | On an accepted append: only the rows strictly after the anchor (the contiguous suffix, capped by `msg_limit`). On a refusal: the full document. |
| `_append_only` | `true` exactly when the append was accepted; the client appends instead of replacing. |
| `_messages_offset` | Absolute index of the first returned row in the merged display document. |
| `_messages_truncated` | On an append: more rows exist beyond the returned window (or a row arrived clipped — the clipper's preview) — the client answers with the full fetch. |
| `prefix_fp` | Fresh digest over rows `[0, _messages_offset + len(messages))`, computed over the delivered projection of exactly that range. Every full, limited, paging, and append response mints one; the client stores it with the view the response establishes. A paging response's digest covers a lower range than the client's held end and is deliberately not adopted — a narrower mint never replaces a wider proof. |

## Server-side invariants

- **The append never takes the limited-display branch.** One predicate,
  `_limited_display_request = msg_limit is numeric and not append_request`,
  gates the `since_timestamp` computation, the display-merge cache probe, the
  stable-signature state.db load, the exact owner-position resolution, and
  the limited/full document branch itself (the two-condition form
  `msg_limit is not None and _msg_after is None` was removed: it disagreed
  with the predicate only on requests carrying both `msg_after` and
  `msg_before`, which the frontend never sends). On a cache hit the probe
  empties `state_db_messages`; letting an append reach that branch merged the
  sidecar with nothing and dropped state.db-only rows from the answer.
- **The append window is the contiguous suffix** starting at `msg_after + 1`.
  Renderable rows decide how many rows fit the `msg_limit` budget; they never
  decide where the window begins, so a tool-result row that landed after the
  anchor was captured still travels with the window.
- **The append response carries only the window's session-level tool cards**,
  rebased into window coordinates. Older cards are the client's to keep — and
  because a card edit on an already-held owner row is invisible to an accepted
  append, the digest binds the covered prefix's own cards in their delivered
  form ("The projection is the proof"), so such an edit refuses the append and
  the authoritative full fetch re-delivers the card.
- **One input snapshot per request.** The scene records, the session tool
  cards, the effective `api_redact_enabled` and the active-turn token are
  read once — `_display_prefix_fp_inputs` — *before* anything is projected,
  and that one snapshot feeds the projection, the digest and the
  display-cache entry's `meta` guard. `redact_session_data` receives the
  captured switch (`redact_enabled=`) and is told the messages are already
  the delivered projection (`_messages_projected=`), so it neither re-reads
  the setting nor rebuilds the delivered form a second time. A save that
  lands mid-request therefore moves the *next* request's digest and that
  append is refused: the published proof can never describe inputs the
  delivered rows do not carry. Pinned by
  `test_handler_mid_request_scene_save_cannot_publish_a_proof_of_new_inputs`
  and
  `test_handler_mid_request_card_edit_cannot_publish_a_proof_of_new_inputs`.
- **Sidecar-only sessions carry no digest memo — the gap is retained.** A
  session with no state.db rows returns from
  `_limited_webui_messages_for_display_with_sidecar` *before* the
  display-merge cache, so it never owns a merge entry and every windowed GET
  re-hashes the whole document (1/1/1 hashes over three `msg_limit=30` GETs
  where the state.db-backed shape gives 1/0/0; 400–470 ms against master's
  171–209 ms on the 32 MiB shape). A memo keyed on the sidecar stat signature
  was tried here and **removed** (2026-10-31 round-2 review): the rows are
  materialized before the key is sampled, so a GET that snapshotted the
  document before a durable earlier-row rewrite could sample the *post-save*
  stat and reuse a digest a later GET published under it — the append that
  echoed that proof was accepted against the rewritten document while the
  response still delivered the pre-rewrite rows, leaving the stale row on
  screen. Sound reuse requires carrying and validating the **read-generation
  of the actual sidecar/lineage snapshot** (which the stat signature is not:
  a content-only rewrite keeps count, last timestamp and every other key
  component), and upstream has no such generation today; writer-side eviction
  alone does not close it either, because in-flight readers can still borrow
  or publish a wrong-generation proof. Until that exists, this path mints
  from the actual response rows and pays the hash. Regression:
  `test_sidecar_only_retained_read_cannot_borrow_a_later_digest` (and the
  miss/publication interleaving
  `test_sidecar_only_memo_miss_mints_from_the_returned_rows`); the retained
  gap itself is pinned by
  `test_sidecar_only_windowed_get_rehashes_the_document_on_every_get`.

## Client-side invariants

- The stored digest is invalidated whenever the view is dropped (session
  switch, non-keep-stale reload, new session) and re-adopted from every
  response that establishes a view. A response without `prefix_fp` (older
  server) leaves the held proof usable only while it still covers every row
  the client holds — otherwise the next reload takes the full fetch.
  Adoption is monotonic in coverage: a narrower mint (a `msg_before` page's)
  never replaces a wider proof, so paging back cannot shrink what the held
  view can vouch for.
- **Provenance is tracked per row, by identity.** `_prefixProvenRows` is a
  WeakSet of the held rows that are verbatim server rows; it is per-session
  and reset wherever the held proof is reset. Identity rather than a parallel
  index array is what makes the gate both cheap and total: every local
  append/splice/replace leaves the new object unproven with no hook at the
  mutation site, and a WeakSet never pins a dropped row.
- **Which rows qualify.** Every row a response delivered — a full or limited
  load's window, an accepted append's window, and a `msg_before` page's rows
  alike — except two shapes marked on the row itself: `_displayOnlyRow` (a
  display-only row such as a recovered prompt parked outside the canonical
  window: drawn by the client, never delivered as a transcript row) and
  `_content_truncated` (the clipper's preview, above). Rows the client
  synthesised — a streamed turn, an INFLIGHT live-tail merge, a recovered
  prompt, a handoff marker — never enter the set in the first place.
- **The admission test is re-run when the response lands.**
  `_heldRowsArePrefixProven()` gates the request (non-empty held rows, a
  stored digest, held end == coverage end, every held row proven) and is
  re-validated on the live view before an append's window is concatenated.
  The 2026-10-21 shape probe (length / last row / `_oldestIdx`) cannot see a
  same-length local swap, and the live-tail merge can splice a locally-owned
  row into the middle of the held range while an append fetch is in flight —
  so the rows the response would be concatenated onto are re-judged, and any
  failure routes to the authoritative full fetch, which replaces the view
  with the server's rows. Pinned by
  `test_local_row_swapped_in_midflight_loses_the_proof` and
  `test_same_length_local_row_swap_midflight_loses_the_proof`, and by the
  clipped-row cases
  `test_bounded_window_row_that_arrived_clipped_loses_the_proof` /
  `test_backward_paged_row_that_arrived_clipped_loses_the_proof`.
- After an accepted append, the window's rows are marked proven (a clipped
  one is not) and the fresh mint is adopted, so the next append vouches for
  the longer prefix. The jump-to-start full load establishes the widest
  proof there is and every row's provenance, so it must not cost the next
  focus reload its fast path.
- The append branch keeps the loaded session-level tool card list across
  loadSession's metadata adoption (the `messages=0` response carries
  `tool_calls: []`), merges the response's rebased new owners into it, and
  restores it even when the response carries no new cards.
- The append branch re-expands the render window with the same bounded rule
  as the replace path, so the hidden-rows affordance describes the loaded
  transcript after an append.
