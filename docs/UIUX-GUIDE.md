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

Background-process notices show the task label and a translated outcome (completed,
failed, or unknown). Commands, exit codes and verbatim output remain inside the
keyboard-accessible disclosure. Unknown exit status must never appear successful.
These rows bypass the Markdown render cache: their dedicated plain-text renderer
owns escaping, while the original stored message and copy action remain intact.

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

## Background work and optional guide loading

The existing composer task counter opens a keyboard-accessible overview of
`/background` tasks, with the requested task, running/result-ready state and a
link to its conversation. Results from the consuming status endpoint are handled
by one poller per parent session and rendered in one batch. A response arriving
during navigation stays in that parent session's client state until the chat is
ready; it must not be appended to the newly selected conversation. Deleting a
session releases its poller, pending results and badge entries. This is browser
state, not new durable task recovery across a full page reload.

The optional bilingual feature catalogs load together only when requested from
Help or a feature deep link. Their guide script executes after all catalogs have
loaded. Concurrent requests share one load and network failures can be retried.
The service worker caches these assets after use without downloading them during
installation. Basic text help remains available if a catalog cannot be loaded.

Background process updates show a plain-language status explanation when expanded. Command, exit code, and verbatim logs live behind a separate keyboard-accessible disclosure. An exit code only describes process completion, not whether a business goal succeeded. Both disclosure states survive message rerenders.

### Streaming latency

New-session sends wait for session creation, not a second sidebar network refresh:
`newSession` already schedules that refresh. Streaming parse results belong to the
attached stream and are reused only while both text and reasoning are unchanged.
Repeated thinking updates preserve the existing text node when its content matches.

Prose animation is presentation, not simulated generation. Small arrivals retain
word pacing; backlogs over 12 words catch up in batches of at most 128 words per
frame, without punctuation pauses. Fade duration is 140–220 ms. Reduced-motion
behavior and the user's fade preference still apply. Finalization retains its
bounded drain and full final transcript render.

Private `run_timing` journal records distinguish prompt context, bot directory,
cron context, history preparation, checkpoint save, attachment preparation,
persistent-state scan, and history sanitization. They contain fixed stage names,
durations and outcomes only; never prompt text, model credentials or identity.

Text-only conversation history skips image-capability resolution entirely. The
first actual `image_url` part still resolves the existing image policy once per
history sanitization; native/text routing and attachment handling are unchanged.
This avoids a cold model-catalog lookup on a plain-text follow-up.

Deployment optimization: the default SynthPulse profile's 25 enabled MCP servers
use their existing `lazy: true` option. Disabled servers remain disabled. Tools
remain in the agent schema through validated per-profile manifests and connect on
first use. Expired/missing manifests fall back to eager discovery. This depends on
the agent's SDK cache-hint presence fix; refresh old manifests from their servers
instead of stripping their recorded zero TTL. Other profiles and model/effort
settings are unchanged.

Streaming partial thinking tags: the browser and server parsers retain a negative
opener search within each immutable input, and evaluate the partial-tail predicate
once. This avoids repeated full-suffix searches while preserving code-aware
handling of incomplete tags. Python also checks the remaining length before copying
a suffix. No state survives the parser call; reasoning, literal code, replay and
settled content retain the same semantics. Regression coverage executes the actual
JavaScript parser and its Python mirror on long partial-tag replies.

When the user's activity mode is “Final answer only”, the live scene entry point
skips activity projection as well as DOM rendering. Incoming activity still
updates its registry; switching back to Compact Worklog or Transparent Stream
projects the complete current registry. This removes hidden per-frame work
without changing event collection, final answers, or visible-mode rendering.

The subagent dock includes background bot delegations alongside ordinary workers.
Bot cards show the configured bot title, delegated task and durable queue status;
opening a child conversation preserves its bot profile. Queued work appears before
its child conversation exists. Result delivery remains in progress until the
coordinating bot's report finishes. The authenticated parent-session check also
protects the delegation listing; authority records are never included in it.

### Workspace overview, projects and schedule

Opening SynthPulse in the browser or as an installed app starts on the Studio
Overview for every role. The title-bar symbol returns to that same homepage.
Studio is first in desktop and mobile navigation. Chats is directly below it
and opens the searchable conversation overview. Its collapsible Sections list mirrors
the personal sidebar hierarchy. Choosing a section filters the cards and status
counts. Each section, including an empty subsection, has its own New conversation
button. The new chat uses the current bot and project and is filed in that section.
Selecting a card or a recent conversation opens its original chat, including saved drafts and
in-progress runs. Direct conversation links and the New Chat shortcut open Chat.

Overview reuses the conversation list and scheduler's visible records. Its
recent task list shows the last recorded run per task in the selected scope.
All destinations opens the same permitted panels as workspace navigation.
Project teams, files and resources have keyboard-operable disclosures. Opening
and closing a section keeps its inputs mounted.

The task calendar opens in the current week. Select a day in the month grid to
inspect its tasks and expand repeated times. Projections use the scheduler
owner's timezone and display in the viewer's timezone. Paused and completed
jobs have no future runs. Task history holds the actual execution results.
