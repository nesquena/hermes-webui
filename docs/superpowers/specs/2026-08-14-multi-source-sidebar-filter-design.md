# Multi-Source Sidebar Filter Design

## Goal

Allow the session-source dropdown to select multiple high-level origins at
once while keeping the sidebar toolbar readable. The feature applies equally
to WebUI, CLI, TUI, Matrix, Telegram, Slack, Discord, cron, webhook, API, and
future dynamic origins.

## Interaction design

The `Sources` dropdown becomes a multi-select menu.

- Each origin row contains a native checkbox, its full source label, and its
  authoritative session count.
- Checking or unchecking an origin applies immediately while the menu remains
  open.
- At least one origin is always selected. Unchecking the final selected origin
  restores WebUI instead of showing a confusing empty state.
- Escape closes the menu and returns focus to the `Sources` trigger. An outside
  pointer interaction closes it without changing the current selection.
- The active toolbar displays the first two selected origins as removable,
  readable chips. Additional selections collapse into one `+N` overflow
  button.
- Clicking `+N` or `Sources` opens the same dropdown. Removing a visible chip
  updates the session list immediately.
- Selection order is stable: existing selected origins keep their relative
  order, and newly checked origins append to the end. This makes the two visible
  chips predictable across refreshes.
- Project chips remain a separate filter dimension beneath source filtering.

## Client state and persistence

Replace the single `_sessionSourceFilter` value with the ordered
`_sessionSourceFilters` collection.

- The collection contains normalized origin keys and never becomes empty.
- Existing `hermes-session-source-filter` single-value storage is migrated on
  first read without losing the selected origin.
- New persistence stores the ordered selection under one versioned key. Reads
  validate shape, normalize keys, remove duplicates, and fall back to WebUI if
  no valid values remain.
- Changing the source set clears the active project and batch-selection state
  once, paints from compatible cached state if available, and performs one
  sidebar request.
- The complete normalized selection participates in request identity, cache
  identity, stale-response guards, and refresh deduplication. A response for one
  source combination must never paint another combination.

## HTTP contract

Use repeated query parameters:

```text
sidebar_source=webui&sidebar_source=matrix
```

- The client emits one `sidebar_source` parameter per selected origin.
- The server reads all repeated values, normalizes them, removes duplicates,
  and validates every value against the existing origin-key syntax and length
  limit.
- If any supplied value is malformed, the request fails closed rather than
  silently widening the result set.
- If no source parameter is supplied, the existing API behavior remains in
  effect: return all permitted origins. The WebUI default is a browser-state
  invariant, and the browser always sends at least one source.
- Session filtering uses membership in the selected-origin set and returns the
  union in the existing authoritative ordering.
- `session_origin_counts` and labels continue to describe all known origins,
  not only the selected subset, so unchecked menu options remain discoverable.
- Archived pagination and visible-only filtering apply to the selected union,
  not independently per origin.

## Cache and state ownership

- Client source-filter state owns the ordered selected-origin list and releases
  obsolete cached render state when the selection changes.
- Server session-list cache keys include the complete normalized source tuple.
  Selection order does not create duplicate server cache entries: the server
  canonicalizes the tuple for cache identity while preserving client order for
  toolbar presentation.
- Source-menu open state remains DOM-local and is released on Escape, outside
  interaction, selection teardown, or sidebar rerender.
- `_sessionSelectMode` and `_selectedSessions` remain independent owners of
  batch-selection state. A source-set change exits selection mode and clears
  selected IDs before painting the new union.

## Error and edge-case behavior

- Zero valid persisted origins falls back to WebUI.
- One selected origin behaves like the existing single-origin filter.
- Multiple and duplicate origins return one deduplicated union.
- Unknown but syntactically valid future origins remain selectable when exposed
  by server metadata; the implementation must not enumerate adapters in UI or
  request logic.
- A selected origin whose count later reaches zero remains selected until the
  user removes it, preserving explicit intent.
- Search, project filters, archived paging, active-session recovery, source
  refresh, and multi-select operate against the complete selected union.
- The toolbar never scrolls horizontally. At desktop and narrow widths it shows
  at most two source chips plus `+N` and the `Sources` trigger.

## Accessibility and responsive behavior

- Use native checkboxes and buttons with explicit labels, checked state,
  expanded state, and keyboard focus.
- Checkbox rows remain full-label and wrap safely inside the menu.
- Immediate updates do not close or reset menu focus.
- Toolbar chips have individually named remove buttons.
- Hover is not required for source selection or removal.
- Narrow and touch layouts preserve the same operations without clipping or
  horizontal scrolling.

## Verification

Automated tests must fail before implementation and cover:

- parsing zero, one, many, duplicate, malformed, and unknown-future sources;
- union filtering and global origin counts;
- complete server/client cache identity and stale-response rejection;
- migration from the previous single persisted value;
- ordered add, remove, last-remove fallback, and immediate request behavior;
- two visible chips plus `+N` overflow;
- menu focus, Escape, outside close, and checkbox state;
- search, project, archive, active-session, and batch-selection neighboring
  behavior.

After automated verification, deploy to the existing local host and verify the
signed-in UI at desktop and narrow widths. Confirm immediate menu updates,
stable chip ordering, `+N`, removal, persistence after refresh, combined source
results, and no regression to the batch-selection interaction.
