# UI/UX Guide

This document summarizes UI/UX principles that are already visible in the
repository. It is a contributor guide, not a new design proposal. Source
documents include [`DESIGN.md`](../DESIGN.md), [`README.md`](../README.md),
[`THEMES.md`](../THEMES.md), [`docs/ui-ux/index.html`](ui-ux/index.html),
[`docs/ui-ux/two-stage-proposal.html`](ui-ux/two-stage-proposal.html), and
design comments in `static/style.css`.

Use this guide when a change touches layout, chat rendering, composer chrome,
navigation, theme/skin behavior, responsive behavior, or visual hierarchy. For
purely backend changes, use the runtime/state contracts instead.

## Product shape

Hermes WebUI is a browser workbench for Hermes Agent with near-CLI parity and a
simple implementation shape: Python on the server, vanilla JavaScript in the
browser, no build step, no bundler, and no frontend framework.

The primary layout is three-panel:

- left sidebar for sessions and navigation,
- center panel for chat,
- right panel for workspace file browsing and previews.

Model, profile, workspace, attachments, voice input, context usage, Stop, and
Send controls live in the composer footer. Settings and session-level tools live
in the Hermes Control Center. Preserve this shape unless the change explicitly
justifies a different interaction model.

## Core feeling: calm developer console

The main artifact is the conversation. Tool calls, thinking traces, context
compaction records, token usage, runtime status, and other internals are useful,
but they are transcript metadata. They should sit below user and assistant prose
in visual priority.

Prefer:

- quiet surfaces,
- clear spacing,
- restrained accent use,
- progressive disclosure for debugging detail,
- legible text over decorative chrome.

Avoid turning the interface into a demo page of colorful cards. Errors,
approvals, and other action-required states may be prominent because the user
must notice and respond to them.

## Conversation hierarchy

A chat turn should read as one coherent story:

1. User message: right-aligned, compact bubble.
2. Assistant content: left-aligned, prose-first, not a heavy bubble.
3. Tool, thinking, progress, and context traces: quiet disclosure rows inside or
   adjacent to the assistant turn.
4. Raw logs and verbose details: hidden until explicitly expanded.

Do not render every internal event as a first-class chat card. A turn that used
many tools should summarize the work as inspectable activity, not make the user
read a stack of unrelated-looking cards.

## Tool, thinking, and activity traces

Tool cards are debug event rows, not chat messages. Show the icon, name, short
target or preview, and status first. Arguments, result snippets, and long logs
belong behind expansion, with result snippets truncated and full output behind a
show-more affordance where needed.

Thinking and context cards should share the quiet metadata visual family. They
should not overpower assistant prose. Collapsed activity summaries should be
terse, for example `Activity: 4 tools`, and should not duplicate the thinking
area, list every tool name in the summary, or add redundant trailing count
badges.

Visible interim assistant progress is part of the live conversation timeline,
not raw debug detail. Compact Activity may collapse tool arguments, long tool
results, and low-level reasoning detail, but it must not make concise
user-visible progress text available only inside a collapsed disclosure.

The existing two-stage proposal in `docs/ui-ux/two-stage-proposal.html` records a
compatible direction for long turns: live work can be grouped as a worklog, then
settled history can collapse while the final answer reads as the calm
conclusion. Treat that page as an existing proposal, not as shipped behavior
unless the code and tests prove it is implemented.

## Typography and content

Use split typography intentionally:

- assistant prose uses the same system sans stack as the rest of the UI by
  default (`--font-ui` in `static/style.css`),
- editorial serif assistant prose is historical/proposal or skin-scoped only;
  do not reintroduce a global assistant serif without explicit design approval
  plus code and test evidence,
- user bubbles and functional UI also use the system sans stack unless a scoped
  skin intentionally overrides typography,
- monospace is for code, file paths, commands, tool names, and compact metadata.

Keep scale tight. Avoid introducing near-duplicate one-off font sizes, colors,
radius values, or spacing values when an existing token works.

## Color, depth, and shape

Use one accent at a time. Semantic colors are for semantic state: success,
warning, error, and info. Do not mix many bright colors decoratively in the same
viewport.

Use almost no shadows in the transcript. Reserve shadows for popovers,
dropdowns, modals, and floating controls. Chat cards should usually use either a
subtle border or a subtle tint, not aggressive combinations of both.

Avoid stacks of nested rounded rectangles. Rows and list items should feel
compact; panels and cards may be slightly rounder; true pills are reserved for
chips and badges.

## Composer and controls

The composer is the command surface. Keep it legible, stable, and focused:

- no theatrical hover scaling for routine controls,
- no ambient chrome that crowds the model/workspace/profile controls,
- no new footer buttons on tight layouts without a clear value tradeoff,
- keep Stop/Send and context feedback easy to find while composing.

When adding a control, consider where users will find it on both wide desktop and
mobile. If a setting or quota/control surface does not fit in the composer, route
it through the appropriate Control Center panel instead of squeezing the footer.

## Responsive behavior

Mobile is not an afterthought. The repository documents a responsive layout with
a hamburger sidebar, mobile-accessible top tabs, a right-edge file slide-over,
full-height chat/composer behavior on phones, and touch-friendly controls.

For UI changes, verify the relevant states:

- wide desktop,
- ordinary laptop width,
- narrow/mobile width,
- open and closed side panels when relevant,
- long chat content and live streaming when relevant.

Controls should remain usable at touch sizes, and mobile navigation should not
steal chat height unnecessarily.

## Themes and skins

Theme and skin work should use the existing variable system. `THEMES.md` points
to the core palette variables in `static/style.css`; skin comments in the CSS
show the expected pattern for full palette rewrites and accent-only changes.

Current implementation has two appearance axes, sourced from `static/boot.js`:
`theme` is only `light`, `dark`, or `system` and resolves to the `.dark` class
for dark mode; `skin` is a separate axis applied with `data-skin` and currently
includes `default`, `ares`, `mono`, `slate`, `poseidon`, `sisyphus`,
`charizard`, `sienna`, `catppuccin`, `nous`, and `geist-contrast` / Geist Contrast. `slate` is both an active skin
and a legacy theme-name migration target; `solarized`, `monokai`, `nord`, and
`oled` are legacy theme names mapped to current theme/skin pairs. Do not follow
stale `data-theme`-only guidance without first proving the current
`static/boot.js`, `static/index.html`, and `static/style.css` contracts still
support it.

Do not hardcode new colors, radii, shadows, or typography values into isolated
components when a token or existing variable can carry the intent. If a token is
missing, explain why a new one is needed.

## Evidence expected for UI changes

For any interface or interaction change:

- include before/after images or a short video,
- mention the tested viewport sizes and responsive states,
- reference the affected visual inventory or design source when applicable,
- add or update tests for behavior, state persistence, or regression-prone DOM
  structure where practical,
- keep stable class or data hooks when they help future visual regression tests.

## Do / don't summary

Do:

- keep the conversation primary,
- collapse noisy internals by default when settled,
- make debugging details accessible without making them visually dominant,
- use existing tokens, variables, and component patterns,
- protect action-required states such as errors and approvals.

Don't:

- make every tool call look like a separate chat message,
- add decorative color or motion without a user-facing reason,
- introduce a frontend framework, bundler, or build step for ordinary UI work,
- hide important recovery, error, or approval state,
- treat proposal mockups as shipped behavior without code/test evidence.
