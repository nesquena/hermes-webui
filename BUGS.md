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
