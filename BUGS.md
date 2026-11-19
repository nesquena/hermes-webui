# Bugs Backlog

This file tracks UI bugs and polish items. Fixed items are kept for reference.

---

## Open Bugs

*No open bugs at this time.*

---

## Known Limitations

- **Two-container Docker setup: tools run in WebUI container** — In the two-container setup (hermes-agent + hermes-webui as separate containers), WebUI-initiated agent sessions run tools in the WebUI container, not the agent container. This is a known architectural constraint. Workaround: use the combined single-image approach, or initiate sessions via the CLI in the agent container. (#681)

- **Image-in-chat vs. saved-to-workspace mismatch** — When the agent displays an inline image (from a URL) and the user asks it to save that image, the agent issues a fresh download which may return a different file if the source URL is CDN-rotated or parameterized. The WebUI correctly renders whatever URL the agent provides. Fix requires agent-side URL caching. (#641)

- **MCP tools not available in WebUI sessions** — MCP servers must be configured in the active profile's config.yaml under mcp_servers:. If MCP tools are not appearing, check that the profile is correct and the MCP server process is reachable from inside the WebUI container. (#628)

- **os.environ race condition in concurrent sessions** — Concurrent agent sessions share process-level os.environ for TERMINAL_CWD, HERMES_SESSION_KEY, and HERMES_HOME. _ENV_LOCK serializes mutations but does not fully isolate env vars during agent execution. Upstream fix pending in hermes-agent. (#195)

---

## Fixed

### ~~Literal HTML in messages: `<strong>` tags and `&#39;` shown as text (bold nested in italic)~~ -- Fixed

- **Was:** When `**bold**` was nested inside `*italic*` (including blockquoted lines like `> *"… **bold** …"*`), the rendered message showed raw HTML instead of formatted text — either visible angle-bracket tags (`&lt;strong&gt;setting alerts&lt;/strong&gt;`) or a literal entity such as `it&#39;s` where an apostrophe belonged. The same shape broke in every `inlineMd()` path (list item, heading, table cell), and italic nested inside strikethrough (`~~*a*~~`) leaked literal `<em>` tags.
- **Cause:** In `renderMd()`, the bold pass runs before the italic and strikethrough passes. Those later passes call `esc()` on their captured text, so markup emitted moments earlier by the bold pass was escaped a second time: a `<strong>`/`<em>`/`<del>` tag became literal `&lt;strong&gt;` text, and an already-escaped entity such as `&#39;` became `&amp;#39;`. Present in both `inlineMd()` and the outer `renderMd()` block.
- **Fix:** Stash the bare `</?(strong|em|del)>` tag tokens and already-escaped entities between the bold pass and the italic/strikethrough passes (and again after the italic pass, so strikethrough cannot escape the `<em>` tags it emitted), then restore them. Only the tokens are stashed — never their content — so nested emphasis such as `**a *b* c**` still renders. The stash token carries a per-call random prefix because `renderMd()` does not strip NUL bytes from its input and `\x00B` is already owned by the autolink stash; a literal `\x00B0\x00` in a message used to be restored as a tag and broke a following link. (`static/ui.js`)

### ~~Session title truncation / hover actions~~ -- Fixed (Sprint 16)

- **Was:** Action icons reserved ~30px of space even when invisible, truncating titles.
- **Fix:** Wrapped all action buttons in a `.session-actions` overlay container with `position:absolute`. Titles now use full available width. Actions appear on hover with a gradient fade from the right edge.

### ~~Folder/project assignment interaction feels sticky~~ -- Fixed (Sprint 16)

- **Was:** Folder icon stayed permanently visible (blue, 60% opacity) when a session belonged to a project.
- **Fix:** Replaced `.has-project` persistent button with a colored left border matching the project color. The folder button now only appears in the hover overlay like all other actions.

### ~~Project picker clipping and width~~ -- Fixed (v0.17.3)

- **Was:** Picker was clipped by `overflow:hidden` on `.session-item` ancestors. With `position:fixed`, no containing block constrained width -- picker stretched to full viewport.
- **Fix:** Dynamic width calculation (min 160px, max 220px). Event listener reordering. Cleanup sequence corrected. (PR #25)

### ~~NameError crash in model discovery~~ -- Fixed (v0.17.3)

- **Was:** `logger.debug()` called in custom endpoint `except` block, but `logger` was never imported in `config.py`. Every failed endpoint fetch crashed with `NameError`.
- **Fix:** Replaced with silent `pass` -- unreachable endpoints are expected when no local LLM is configured. (PR #24)

---

## Notes

- Sprint 16 replaced all emoji HTML entities with monochrome SVG line icons (`ICONS` constant in `sessions.js`).
- All session action buttons now use the overlay pattern for consistent UX.
