# Sidebar Source Filter and Selection Design

## Goal

Keep every high-level session origin—WebUI, CLI, TUI, Matrix, Telegram, Slack,
Discord, and future adapters—readable and directly filterable without rendering
one truncated pill per origin. Make batch selection available from each date
heading without leaving a permanent control or exposing every row checkbox.

## Source filtering

Replace the horizontal `.session-source-tabs` strip with a compact filter
summary and a `Sources` button.

- With the default WebUI filter active, the summary reads `WebUI` and can be
  removed; the adjacent `Sources` button shows the total number of available
  origins.
- Opening `Sources` displays an anchored menu containing every origin's full
  label and count. Labels and counts continue to come from the existing dynamic
  origin taxonomy; no messaging backend is hardcoded into the UI.
- Choosing one origin keeps the existing single-origin filtering contract and
  server pushdown. The control is a presentation change, not a multi-origin
  query change.
- The active origin is marked in the menu and summarized outside it. Selecting
  the active origin again is a no-op. Clearing the summary returns to WebUI,
  preserving the current default and storage contract.
- The menu closes on selection, Escape, and outside interaction. Keyboard focus
  returns to the `Sources` button after Escape.
- Project chips remain a separate filter dimension beneath source filtering.

## Batch selection

Replace the always-visible date-group checkbox with a low-emphasis `Select`
control on the right side of each non-pinned date heading.

- Desktop reveals the control when the date heading is hovered or keyboard
  focus enters it. Touch and narrow layouts keep a subdued but reachable icon.
- Activating it enters selection mode with zero rows selected and immediately
  renders the existing bottom action dock, including count, Select all,
  Deselect all, Move, Archive, Delete, and Exit.
- Entering selection mode does not check every date heading or permanently show
  every row checkbox. An unselected row checkbox appears on row hover/focus;
  selected rows keep their checkbox visible. Touch/narrow layouts show row
  checkboxes while selection mode is active because hover is unavailable.
- Date headings remain collapse/expand targets except for the `Select` control,
  whose pointer and click events do not bubble.
- Escape and the dock Exit action leave selection mode and clear selection.

## Accessibility and responsive behavior

- Use native buttons and checkboxes with explicit accessible names and pressed,
  expanded, or checked state where appropriate.
- All source labels remain readable in the menu at desktop and narrow widths.
- The source menu stays within the sidebar and does not introduce horizontal
  scrolling.
- Hover is enhancement only; keyboard and touch users retain every operation.
- Respect existing reduced-motion behavior and do not add dependencies or a
  build step.

## State ownership and invariants

- `_sessionSourceFilter` remains the authoritative selected origin and keeps its
  existing local-storage and request-pushdown lifecycle.
- `_sessionSelectMode` owns whether batch selection is active;
  `_selectedSessions` owns selected IDs. Enter and exit continue to clear the
  set, and rerenders prune IDs that are no longer visible/selectable.
- Source-menu open state is DOM-local and is released on selection, Escape,
  outside interaction, and sidebar rerender.

## Verification

- Add browser-level JavaScript behavior tests for dynamic source labels/counts,
  opening/closing, active-origin selection, and selection-mode disclosure.
- Run affected sidebar origin and batch-selection tests through
  `./scripts/test.sh`.
- Manually verify desktop, narrow, and touch-equivalent states in the locally
  deployed build, including long origin names and many origins.

