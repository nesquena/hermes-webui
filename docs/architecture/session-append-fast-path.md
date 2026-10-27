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
| `prefix_fp` | The server-computed digest over rows `[0, N+1)` of the merged display document, echoed back from the load that established the client's view. Server-minted and server-verified; the browser only stores and returns it. |
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
client stitching a stale prefix onto a fresh tail. The digest covers the
render-relevant fields of each row (`role`, `content`, `reasoning`,
`attachments`, `tool_calls` — the latter two including their ids) and is
complete by construction: any rendered difference anywhere in `[0, N+1)`
changes it. It is a fingerprint rather than a revision counter because a
counter must be bumped by every writer that touches an earlier row, and one
missed writer means silently stale rows.

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
  stable-signature state.db load, and the exact owner-position resolution.
  On a cache hit the probe empties `state_db_messages`; letting an append
  reach that branch merged the sidecar with nothing and dropped state.db-only
  rows from the answer.
- **The append window is the contiguous suffix** starting at `msg_after + 1`.
  Renderable rows decide how many rows fit the `msg_limit` budget; they never
  decide where the window begins, so a tool-result row that landed after the
  anchor was captured still travels with the window.
- **The append response carries only the window's session-level tool cards**,
  rebased into window coordinates. Older cards are the client's to keep.

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
