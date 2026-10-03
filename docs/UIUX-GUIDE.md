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

Automatic compression is a live-only context barrier, not a special branded
tool card. Render it as a centered, non-interactive divider with quiet horizontal
rules: `Compressing context` while the compression barrier is active and
`Context auto-compressed` when the agent has continued or the compression
completion event arrives. Do not give it a caret, click target, leading status
dot, or standalone running badge. In settled final history, remove live-only
automatic compression rows unless they explain a visible recovery or error
state.

The existing two-stage proposal in `docs/ui-ux/two-stage-proposal.html` records a
compatible direction for long turns: live work can be grouped as a worklog, then
settled history can collapse while the final answer reads as the calm
conclusion. Treat that page as an existing proposal, not as shipped behavior
unless the code and tests prove it is implemented.

## Typography and content

Use three explicit font tokens:

- `--font-ui`: shell chrome, controls, composer, labels, and ordinary UI text
- `--font-conversation`: user/assistant message prose; by default this is
  `var(--font-ui)` in `static/style.css`
- `--font-mono`: code, file paths, command lines, tool payloads, technical logs,
  and terminal output
- `--font-emoji`: color emoji fallback; Apple platforms use Apple Color Emoji,
  Chrome/Edge can load the self-hosted Noto Color Emoji COLRv1 font, and Segoe
  UI Emoji remains the last Windows fallback

Use semantic tokens for typography. Keep prose on `--font-conversation` by
default so it tracks `--font-ui` whenever a skin intentionally retunes UI type.
Override `--font-conversation` only for a skin that intentionally wants a
distinct prose face.
Conversation prose must remain the system sans by default. Do not introduce a
global conversation serif through the `--font-conversation` token or
selector-level overrides without explicit design approval plus code and test
evidence; keep distinct editorial prose typography explicitly opt-in or
skin-scoped.
Avoid hard-coding selector-level font stacks when a token already carries the
intent.

Keep scale tight. Avoid introducing near-duplicate one-off font sizes, colors,
radius values, or spacing values when an existing token works.

## Message width

The width picker labels and accessible names follow the active locale, including
changes made while the menu is open. Keyboard selection returns focus to the
trigger; Escape closes without changing the selection. Long unbroken prose and
headings wrap within the message column, while tables and code retain their
dedicated horizontal overflow behavior.

The composer message-width button offers Default, Wide, and Full columns and
reflows the conversation immediately. The browser retains explicit selections,
including Default, when appearance autosave fails or returns a partial response;
a late response for an older selection must not revert a newer choice. The
selected width is also saved through the appearance settings endpoint. On narrow
screens the column stays within the available space, and enhanced Markdown tables
scroll horizontally without widening the conversation.

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

### Composer sizing

The composer grows with its content up to a 200px cap. Where the browser supports
`field-sizing: content` (the stylesheet sets it on `textarea#msg`, with
`field-sizing: fixed` while the placeholder shows) CSS owns that; everywhere else
the JavaScript fallback (`autoResize()` in `static/messages.js`) measures, and it
runs on every keystroke - so treat it as a hot path and keep these invariants when
touching it (regression coverage:
`tests/test_long_session_composer_typing_latency.py`, which derives every
dimension from `static/style.css`, and
`tests/test_issue5514_composer_grow_scroll_pin.py`):

- A single-row append that already fits its box skips the height round trip. That
  round trip reads `scrollHeight`, which forces a synchronous layout of the whole
  document, so its cost grows with the rendered transcript - this is the
  long-session typing-lag class. Do not remove the skip.
- The skip's ceiling is the textarea's natural ONE-ROW height (`line-height` +
  vertical padding + borders) or the CSS `min-height`, whichever is larger.
  Compare against that natural row, never against `min-height` alone: the natural
  row follows the appearance font size (44px at the 16px default, 48px at
  `data-font-size=large`, 51px at `xlarge`), while `min-height` stays 44px, so a
  min-height-only ceiling silently disables the skip for the larger sizes.
- Everything else still fully remeasures: an oversized composer, a replacement, a
  shrink, a multi-line append, and session/draft restore.
- Non-pixel computed values (a percentage, `calc()`, `auto`) fail closed to the
  full resize rather than enabling the skip from a bogus pixel parse.

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
