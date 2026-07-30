# Canonical Session Resolution Contract

- **Status:** Proposed
- **Author:** @ai-ag2026
- **Created:** 2026-05-25
- **Tracking issue:** [#2361](https://github.com/nesquena/hermes-webui/issues/2361)
- **Related architecture:** [#1925](https://github.com/nesquena/hermes-webui/issues/1925), [`webui-run-state-consistency-contract.md`](webui-run-state-consistency-contract.md)

## Problem

WebUI can reach the same conversation through several browser-facing entrypoints:

- a URL route such as `/session/<session_id>`,
- a query parameter such as `?session=<session_id>` or `?session_id=<session_id>`,
- the browser's `localStorage` active-session value,
- sidebar rows built from `/api/sessions`,
- direct session open actions from links, search, or imported session lists,
- browser boot restore after reload, auth redirect, or PWA resume.

After automatic compression, those entrypoints can point at different rows in one
logical conversation lineage. A pre-compression parent snapshot can remain a
valid archived session while the user-facing conversation tip has moved to a
newer continuation. If each caller resolves IDs independently, the UI can appear
to lose the session, reopen an old one-message snapshot, duplicate sidebar rows,
or prefer the wrong transcript even though durable data is still present.

This contract defines the expected resolution semantics for those entrypoints. It
is intentionally narrower than the run adapter RFC: this is about choosing the
correct visible session target, not moving execution ownership.

## Goals

- Define one canonical browser-facing resolution concept for sessions and
  compression lineage.
- Make URL, query parameter, localStorage, sidebar, and direct-open behavior use
  the same mental model.
- Preserve archived parent snapshots without letting them become the default
  active target when a continuation exists.
- Give reviewers a small checklist for future session-routing, sidebar, and
  compression-lineage changes.

## Non-goals

- Do not delete archived `pre_compression_snapshot` rows.
- Do not merge or rewrite session files as part of this contract.
- Do not replace state.db/session sidecar reconciliation.
- Do not require a new backend endpoint before narrow frontend guards can land.
- Do not change explicit history browsing when the user deliberately opens an
  archived snapshot as a record.

## Terms

| Term | Meaning |
|---|---|
| Requested session ID | The ID supplied by route, query parameter, localStorage, sidebar click, or direct session open. |
| Canonical visible session | The session row WebUI should display by default for normal chat navigation. |
| `canonical_visible_session_id` | Proposed field/name for an API or helper output that identifies the canonical visible session. |
| Compression snapshot | A preserved archived parent row with `pre_compression_snapshot` set. |
| Continuation session | The active child/tip created after compression, usually represented by `continuation_session_id`, `_lineage_tip_id`, or newer lineage metadata. |
| Lineage relation | Links such as `parent_session_id`, `_lineage_root_id`, `_lineage_tip_id`, and `_compression_segment_count` that connect rows belonging to one logical conversation. |

## Resolution Rules

1. **Directly valid non-snapshot IDs stay stable.** If the requested session ID
   exists and is not a `pre_compression_snapshot`, it should normally resolve to
   itself.
2. **Snapshot parents defer to visible continuation tips.** If the requested
   session ID is a `pre_compression_snapshot` and the session list has a newer
   non-snapshot continuation in the same lineage, normal chat navigation should
   resolve to that continuation as the `canonical_visible_session_id`.
3. **Explicit archive/history inspection remains possible.** A future UI affordance
   may intentionally open a snapshot as a historical record, but that should be a
   distinct mode from ordinary boot restore, URL restore, or sidebar continuation.
4. **Local browser state is advisory.** `localStorage` may remember the last active
   ID, but browser boot restore must treat it as a requested session ID and still
   run canonical resolution before rendering.
5. **Query aliases share the same resolver.** `?session=...`, `?session_id=...`,
   and `/session/...` should feed the same requested-ID path instead of carrying
   separate precedence rules.
6. **Sidebar collapse and session loading agree.** The row chosen as the visible
   representative for a lineage should match the target opened by `loadSession()`
   for that lineage during ordinary navigation.
7. **404 self-heal is separate from lineage resolution.** Missing/deleted sessions
   should still use the stale-route recovery path. A present archived parent with
   a live continuation is not a 404; it is a canonicalization problem.

## Entry Point Matrix

| Entry point | Input | Expected resolution |
|---|---|---|
| URL route | `/session/<id>` | Treat `<id>` as requested; resolve to canonical visible session before ordinary render. |
| Query parameter | `?session=<id>` or `?session_id=<id>` | Same as URL route. Query spelling must not change the target semantics. |
| localStorage | last active session ID | Advisory requested ID during browser boot restore; canonicalize before render. |
| Sidebar click | visible row ID or lineage representative | Open the same canonical visible session that the row represents. |
| Direct session open | programmatic call/search/import link | Use the shared requested-ID resolver unless the caller explicitly opts into archive inspection. |
| Browser boot restore | URL and/or localStorage state after reload/auth/PWA resume | Prefer explicit URL/query input, then localStorage, then canonicalize the requested ID. |

## Review Checklist

For PRs that touch session routing, compression lineage, sidebar collapse, boot
restore, direct session open, or URL parsing, answer:

- Which entrypoints provide the requested session ID?
- Does the code path accept both route and query parameter forms where relevant?
- Does localStorage go through the same canonicalization path as URL restore?
- Can a `pre_compression_snapshot` become the default active chat when a
  non-snapshot `continuation_session_id` / `_lineage_tip_id` exists?
- Do sidebar collapse and `loadSession()` pick the same visible representative?
- Is missing-session 404 recovery kept distinct from present-but-archived lineage
  canonicalization?
- What regression proves route, query parameter, localStorage, and sidebar paths
  agree for compressed lineage rows?

## Rollout Plan

1. Document this proposed contract and link it from the public contract index.
2. Keep narrow bugfixes small while referencing the relevant rule they preserve.
3. Add shared frontend helper coverage for URL/query/localStorage/sidebar
   requested-ID inputs.
4. If backend session APIs later expose `canonical_visible_session_id`, make the
   frontend resolver prefer the backend value while preserving client fallback for
   older WebUI servers.
5. If #1925 moves execution/session ownership behind an adapter, carry this
   contract forward as an adapter-facing session-navigation invariant.
