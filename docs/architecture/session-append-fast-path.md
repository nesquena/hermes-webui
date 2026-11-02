# Session append-only fast path contract

Current contract for the append-only reload of `GET /api/session`: when a
browser that holds more rows than the window ceiling refreshes a session, it
may ask for only the rows strictly after its last rendered index instead of
re-downloading the whole transcript — and the checks that decide when that is
safe.

Start here before changing the append request/response shape
(`msg_after` / `anchor_fp` / `transcript_generation` / `prefix_fp`), the
state.db loader or display-merge cache decisions on that path, the append
window extraction, or the client-side merge in `static/sessions.js`
(#7925, #7899).

## When the fast path applies

The client sends the append shape only when **all** of these hold:

- the reload width (the transcript it holds) exceeds the server's
  `_msg_limit_max` ceiling, so a bounded replace cannot express it;
- the client still holds a contiguous transcript (`S.messages` non-empty);
- the session exposes a finite `transcript_generation`;
- the client holds a prefix digest covering exactly its view's held end
  (`[0, _oldestIdx + S.messages.length)`).

Otherwise the reload takes the bounded replace or the authoritative full
fetch — master's shapes. Only focus, visibility, and SSE-driven reloads of an
already-loaded session reach this path; other external-refresh reasons clear
`S.messages` first and reload from scratch.

## Request fields

| Field | Meaning |
|---|---|
| `msg_after=N` | The client's last rendered absolute row index; the response must start strictly after it. Mutually exclusive with `msg_before` (paging wins). |
| `anchor_fp` | FNV-1a digest of the anchor row's **public projection** (internal fields stripped, content redacted) — the exact bytes the client rendered. Minted browser-side; the server re-derives it. |
| `transcript_generation` | The generation the client's view was built under. Bumped only on destructive rewrites (truncate/retry/undo). |
| `prefix_fp` | The server-computed digest over rows `[0, N+1)` of the merged display document, echoed back from the load that established the client's view. Server-minted and server-verified; the browser only stores and returns it. Coverage is the whole projected row minus the explicit exclusion set (`_PREFIX_FP_EXCLUDED_FIELDS`), hydrated anchor scenes included, seeded with the out-of-row inputs that change a covered row's delivered projection (the prefix's own session-level tool cards, the effective `api_redact_enabled`, the active-turn token — see the input inventory below); on a display-merge cache hit it is reused from the cache entry instead of re-minted. |
| `msg_limit` | Bounds the **returned window** only. It never selects the limited-display document: an append request's anchor validation and merge run against the full display document, exactly like master's bare GET. |

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
browser receives it — the whole projected row minus an explicit exclusion set
(`_PREFIX_FP_EXCLUDED_FIELDS` = `_PUBLIC_MESSAGE_INTERNAL_FIELDS`, exactly the
fields stripped before delivery) — so any *new* rendered field is covered by
default instead of being missed by a whitelist. It is a fingerprint
rather than a revision counter because a counter must be bumped by every
writer that touches an earlier row, and one missed writer means silently
stale rows. The whole-document digest is computed once per merge and reused
by both the cache entry and the response mint: a display-merge cache hit
reuses the entry's digest (guarded by a scene/tool-call digest), and a cold
load hands the freshly built digest to the mint instead of computing the
same value a second time. Ordinary windowed opens therefore pay for one
whole-document hash, not two.

### The full input inventory (2026-10-31 re-gate)

"Complete by construction" has to mean the whole *delivered projection* of a
covered row, not just the row's own fields. Two silent findings proved the
point: `_display_prefix_fingerprint` read session-level `tool_calls` only
through `_hydrate_anchor_activity_scenes`, which it called only when the
session had a saved anchor scene — so a scene-free session's cards never
reached the digest — and `redact_session_data` runs *after* the mint, so
neither the digest nor the display-merge memo's `meta` tuple carried
`api_redact_enabled`. Both changed what the browser renders for rows the
client already holds, without touching a row.

Everything on the GET path between the merge and `j(...)` that can do that is
therefore enumerated here. **Bound** inputs are mixed into the digest (or, for
the display-merge memo, into the `meta` tuple that guards digest reuse) and
each has a test that changes it on a held prefix and asserts the append is
refused.

| # | Input | Where it acts on the GET path | Why it changes a held row's projection | How it is bound | Test |
|---|---|---|---|---|---|
| 1 | The merged rows themselves (`_all_msgs`) | the merge | the row's own fields | hashed as the whole projected row minus `_PREFIX_FP_EXCLUDED_FIELDS` | `test_prefix_fingerprint_refuses_a_nondestructive_earlier_row_edit`, `test_prefix_fingerprint_exclusion_set_is_explicit_and_rendered_fields_covered` |
| 2 | `session.anchor_activity_scenes` | `_hydrate_anchor_activity_scenes` before the mint | attaches `_anchor_activity_scene` / `_anchor_stream_id` to an assistant row without ever modifying a row | rows hydrated before hashing | `test_prefix_fingerprint_mint_tracks_anchor_scene_saves`, `test_append_verify_refuses_after_anchor_scene_save` |
| 3 | `session.tool_calls` (session-level cards) | the response's `tool_calls` field, and the scene hydration | a card's summary/result renders on its owner row, and the client keeps its copy of the list across an accepted append | `_prefix_owned_tool_calls(tool_calls, total)` (the response path's own window selector) mixed into the digest; the whole card list in `meta[1]` | `test_prefix_fingerprint_covers_scene_free_session_tool_cards`, `test_append_verify_refuses_a_scene_free_tool_card_edit`, `test_append_verify_refuses_a_card_edit_with_no_new_rows`, `test_handler_focus_refresh_refuses_after_a_scene_free_card_edit` |
| 4 | `api_redact_enabled` | `redact_session_data`, **after** the mint | masks/unmasks credentials in `messages` and in `tool_calls` | digest seed `redact=`, plus `meta[2]` | `test_prefix_fingerprint_tracks_the_redaction_setting`, `test_append_verify_refuses_after_the_redaction_setting_flips`, `test_handler_focus_refresh_refuses_after_the_redaction_setting_flips` |
| 5 | The active-turn token (`active_stream_id` + `pending_started_at`) | `redact_session_data`, **after** the mint | decides whether a covered user row is delivered with `_active_turn_user: true` (`ui.js:15016`, `sessions.js:4969` read it) | digest seed `active_turn=`, plus `meta[3]` | `test_prefix_fingerprint_tracks_the_active_turn_token`, `test_append_verify_refuses_when_the_active_turn_token_changes` |

Input 3 has a deliberate boundary, pinned by
`test_prefix_fingerprint_excludes_cards_owned_by_appended_rows`: a card owned
by a row at or after the coverage end belongs to the appended rows and stays
OUT of the covered prefix's digest, or every turn that runs a tool would
refuse its own append. A card whose `assistant_msg_idx` is not an integer
cannot be attributed to any row, so the window selector drops it — but the
response path still delivers such a card on a full load, so it is covered
unconditionally rather than dropped.

Covered **by construction** (a pure function of the hashed rows, so no
separate binding is needed — and none is possible without duplicating the
merge):

- `_display_owner_positions` / `_display_rebased_tool_calls` — derived from
  the merged rows and the card list, both of which are bound (1, 3).
- `_stamp_pending_source_for_display`, `_suppress_native_image_display_mirrors`
  — applied to the state.db rows **before** the merge, so their effect is
  inside the hashed rows.
- `_project_native_image_payload_conflicts_for_display` — applied to the
  merged rows before the digest.
- `_merged_message_count`, `visible_message_count`, `last_message_at` /
  `updated_at`, `attach_todo_state` — derived from the merged rows.
- The window (`_messages_offset`, `msg_limit`, `expand_renderable`) —
  request-derived; mint and verify derive the coverage end from the same rule.

Deliberately **not** bound, because they are not a held row's projection:

- `title` — redacted like a row, but a header field, not a transcript row.
- `todo_state`, `runtime_journal`, `runtime_journal_snapshot` — panels, and
  derived from the merged rows.
- `model` / `model_provider`, `context_length`, `threshold_tokens`,
  `last_prompt_tokens`, `regeneration_revision`, `read_only`,
  `is_cli_session`, `continuation_session_id`, `_messages_truncated`,
  `_msg_limit_max` — session metadata and response flags.
- `pending_user_message` / `pending_attachments` / `pending_user_source` — the
  optimistic bubble, not a transcript row; the materialized user row *is* a row
  and is covered by (1) and (5), and the turn boundaries that change the
  pending state also move the active-turn token (5).

Sidecar-only sessions (no state.db rows for the session) are **not** covered
by any digest memo: they return from
`_limited_webui_messages_for_display_with_sidecar` before the display-merge
cache, so every windowed GET mints the digest from the rows it actually
returns. See "Server-side invariants" for why that gap is retained.

## Response fields

| Field | Meaning |
|---|---|
| `messages` | On an accepted append: only the rows strictly after the anchor (the contiguous suffix, capped by `msg_limit`). On a refusal: the full document. |
| `_append_only` | `true` exactly when the append was accepted; the client appends instead of replacing. |
| `_messages_offset` | Absolute index of the first returned row in the merged display document. |
| `_messages_truncated` | On an append: more rows exist beyond the returned window (or a row arrived clipped) — the client answers with the full fetch. |
| `prefix_fp` | Fresh digest over rows `[0, _messages_offset + len(messages))`. Every full, limited, paging, and append response mints one; the client stores it with the view the response establishes. A paging response's digest covers a lower range than the client's held end and is deliberately not adopted. |

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
  append, the digest binds the covered prefix's own cards (#3 above), so such
  an edit refuses the append and the authoritative full fetch re-delivers the
  card.
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
  server) invalidates it: the next reload takes the full fetch.
- The append branch keeps the loaded session-level tool card list across
  loadSession's metadata adoption (the `messages=0` response carries
  `tool_calls: []`), merges the response's rebased new owners into it, and
  restores it even when the response carries no new cards.
- The append branch re-expands the render window with the same bounded rule
  as the replace path, so the hidden-rows affordance describes the loaded
  transcript after an append.
