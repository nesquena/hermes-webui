# Transparent Stream — A Chronological Activity Display Mode

- **Status:** Proposed
- **Author:** @franksong2702
- **Created:** 2026-06-09
- **Tracking issue:** [#3820](https://github.com/nesquena/hermes-webui/issues/3820)
- **Parent contract:** [Live-to-Final Assistant Replies](./live-to-final-assistant-replies.md) ([#3400](https://github.com/nesquena/hermes-webui/issues/3400) / #3401 / #3741)

## Problem

After the live-to-final redesign (#3401/#3741), the execution trace of a turn
is presented as a **Compact Worklog / Activity** abstraction: reasoning, progress
text, and tool calls are grouped and summarized into rows like:

```text
Thinking
terminal
Checked the web 6 times, ran 3 tools
```

For the default Web UI audience this is the intended direction — the final
answer stays primary and supporting activity is quieter.

But a class of power users runs the Web UI as an **agent cockpit**: they want to
watch the model work step by step, in real execution order. #3820 documents that
the pre-#3401 *transparent chronological stream* is no longer reachable. The
old behavior exposed concrete events directly in the transcript:

```text
assistant progress text
Thinking / reasoning preview
tool: web_search   query / compact output preview
assistant progress text
tool: memory       action preview / result preview
assistant progress text
```

Two rounds of investigation on the issue established that **this is not a
discoverability problem** and **cannot be fixed by the existing
`Compact tool activity` toggle** (`simplified_tool_calling`):

1. **The toggle is asymmetric.** The live path branches on
   `isSimplifiedToolCalling()` (`static/ui.js` — `ensureLiveWorklogShell`,
   `appendThinking`, `finalizeThinkingCard`), but the **static rebuild path does
   not**: the tool-call loop in `renderMessages()` unconditionally buckets tool
   calls into an Activity group and summarizes them via `_toolWorklogSummary`.
   So the moment a turn settles or the page reloads, the per-tool view collapses
   back into the grouped Activity card regardless of the flag.

2. **Even with the flag off, the live view is not a usable cockpit stream.**
   Measured in the running UI with `_simplifiedToolCalling=false`: individual
   `.tool-card-row` nodes do exist in the DOM, but the running `terminal` card
   was pushed out of the viewport (`top=-24`) while the `Thinking` card
   dominated the readable area (`top=55 bottom=296`). "Tool cards exist in the
   DOM" is **not** equivalent to "the concrete tool call is the primary,
   readable item in the transcript flow."

The issue discussion has converged on treating this as a real
**display-mode split**, not another overloaded disclosure default. The
maintainer has invited a PR that rebuilds the desired visual behavior on top of
the refactored codebase, and this RFC records the proposed product boundary for
that work.

## Goals

Introduce **Transparent Stream** as an opt-in display mode that satisfies the
following acceptance criteria. All four must hold; any one missing means #3820
is not solved.

1. **Each tool call is a first-class chronological event** in the main
   transcript, in real execution order — never aggregated into
   `Ran 11 commands` / `Checked the web 6 times`.
2. **Reasoning, progress text, and tool calls interleave** in the order they
   actually occurred (`progress → Thinking → tool → result → progress → …`).
3. **Each tool card exposes a compact preview by default** — tool name, the key
   argument (command / query / path / URL / action), a short output preview, and
   running/success/error status — with full args and output available on expand.
4. **The mode is consistent across all three render paths**: live streaming,
   settled transcript render, and reload/replay. It must not look transparent
   live and collapse back into Worklog after settle or reload.

Plus two boundaries:

- **The final answer keeps a clear boundary.** The settled final answer stays
  primary and visually separated from the execution trace; the trace does not
  bleed into it.
- **During live runs, the currently-running (or latest) tool call is the
  primary visible item** — not pushed below a viewport-dominating Thinking card.
  This is the specific UX failure documented in the issue and is part of the
  live acceptance criteria, not a nice-to-have.

## Non-goals

- **Not a rollback of #3400/#3401.** Compact Worklog remains the default Web UI
  mode. Transparent Stream is opt-in.
- **Not a safety/blocking mechanism.** Transparent Stream improves *visibility*
  of destructive tool calls (relevant to #3813), but actual interruption/blocking
  remains the responsibility of approval gating. Visibility is not a substitute
  for a gate.
- **Not reusing `simplified_tool_calling`.** That flag stays only to preserve
  existing stored preferences; the new mode does not read it. We do not extend
  the old `Compact tool activity` checkbox semantics.
- **Not full transcript virtualization.** Performance hardening for very long
  transparent transcripts (#3714) is acknowledged but scoped to a later slice.

## Proposal

### 1. A new, dedicated preference

A new preference, independent of `simplified_tool_calling`:

```python
# api/config.py preferences
"chat_activity_display_mode": "compact_worklog",  # | "transparent_stream"
```

- Default `compact_worklog` — existing users see no change.
- Surfaced in Settings as an explicit **segmented choice** (not a checkbox):
  - **Compact Worklog** — default; results-first, supporting activity quiet.
  - **Transparent Stream** — advanced; every step shown chronologically.
- The legacy `Compact tool activity` checkbox is marked deprecated. It is not
  the seam for this feature.

Mirrored to the front end as `window._chatActivityDisplayMode`, with a single
predicate used everywhere:

```js
function chatActivityMode(){ return window._chatActivityDisplayMode || 'compact_worklog'; }
function isTransparentStream(){ return chatActivityMode() === 'transparent_stream'; }
```

### 2. Decouple "event sequence" from "grouping strategy"

The live backend emits ordered SSE events (`token`, `interim_assistant`,
`reasoning`, `tool`, `tool_complete`, `done`, ...). Transparent Stream should
preserve that order instead of applying the Compact Worklog grouping strategy on
top. Settled messages and replay snapshots may not already expose the exact same
event shape, so each implementation slice must normalize the available
transcript, journal, reasoning, and tool-call metadata into an ordered activity
event sequence before rendering.

The fix should therefore be a strategy seam queried at the three render paths,
all feeding a shared renderer where the data is sufficiently normalized:

```text
SSE live ─────────┐
journal replay ───┼──► normalizeToEvents() ──► renderActivityEvent() ──► DOM
settled messages ─┘                              (single source of render truth)
```

Using one `renderActivityEvent()` for all three paths is what prevents the
live-vs-static asymmetry that #3820 keeps hitting.

### 3. Concrete integration points (current tree, `static/ui.js`)

These are the three regions that currently fail the criteria above. Line
numbers refer to the tree observed when this RFC was written and are expected to
drift; the function names are the durable anchors.

- **A. Settled rebuild** — the tool-call loop in `renderMessages()`. It has
  **no transparent-stream branch at all** today.
  Add `if(isTransparentStream())`: for each `tc`, build a standalone card with
  the existing `buildToolCard(tc)`, anchor it to its assistant segment via
  `_assistantAnchorForActivity(...)`, and insert it
  inline in `aIdx/segmentSeq/burstId` order — bypassing `ensureActivityGroup`
  and `_syncToolCallGroupSummary`. This is the "missing half" the maintainer
  identified, and the highest-value fix because it kills the settle/reload
  collapse.

- **B. Live** — `ensureLiveWorklogShell()` and the live
  `reasoning`/`tool`/`interim_assistant` handlers. The transparent branch must
  **not** just fall back to the old `thinkingRow` (that is the path that pushed
  the running tool card off-screen). It must append each event as an
  independent row and keep the **currently-running tool card as the primary
  visible item**, with Thinking shown as a preview above it rather than the
  dominant block.

- **C. Summary bypass** — in transparent mode, `_syncToolCallGroupSummary()` /
  `_toolWorklogSummary()` do not run. No "Checked the web 6 times" aggregation
  rows.

### 4. Reload/replay consistency

The journal replay path already replays events one by one. In transparent mode
it simply must not invoke the settle-time collapse
(`_convertLiveActivityGroupToSettled`) and must route events through the same
`renderActivityEvent()` as live and settled. Replaying a session in transparent
mode should reproduce the same picture the live stream left.

## Open questions

- **Default on upgrade.** Stay `compact_worklog` for everyone (current proposal),
  or honor a prior `simplified_tool_calling=false` as a hint to start in
  transparent mode? Leaning toward: no implicit migration — keep it an explicit
  opt-in.
- **Per-session vs global.** Is display mode a global preference, or should it be
  switchable per session/pane? Proposal: global preference first; per-session
  override is a possible follow-up.
- **Tool preview density.** How much output preview is shown by default before
  "show more"? This is the dimension we agreed is negotiable; needs a concrete
  default (e.g. first N lines / M chars).
- **Performance ceiling.** At what transcript length does transparent rendering
  need windowing/virtualization (#3714)? Proposal: gate transparent rendering to
  the existing render window first, measure, then decide on virtualization.

## Rollout plan

Shipped as independently-mergeable slices. Ordering principle: **certain,
testable, small-footprint changes first; subjective, hard-to-test, viewport-feel
changes later.**

1. **PR-1 — Internal mode scaffold (no user-facing selector yet).** Add the
   `chat_activity_display_mode` preference, boot-time plumbing, and the
   `isTransparentStream()` predicate behind tests, but do **not** expose a
   Settings control that appears to do nothing. This keeps the first slice safe
   without creating a confusing empty toggle.
2. **PR-2 — Settled/reload path + visible selector (integration point A).** Add
   the Settings segmented control when transparent mode has at least one visible
   behavior: after settle and after reload, tool calls render as inline
   chronological cards instead of a collapsed Activity summary. Deterministic
   test: reload a transparent-mode session -> tool calls remain per-tool and in
   order.
3. **PR-3 — Live path (integration points B + C).** Interleaved per-event live
   rendering with the running tool card as the primary visible item; bypass the
   summary aggregation. Includes the viewport acceptance check from the issue
   evidence. Most subjective, so it follows the deterministic PR-2.
4. **PR-4 — Replay consistency + performance.** Same `renderActivityEvent()` on
   the replay path; gate to the render window for long transcripts.

Per the [RFCs README](./README.md): this RFC is a design direction, not an
invitation to implement fragments. Before opening any implementation PR, confirm
in #3820 that the slice is wanted and that @ai-ag2026 is not already building it,
to avoid duplicate work.
