# Hermes Web UI -- Changelog

## [v0.50.77] — 2026-04-17

### Changed
- **Color scheme system replaced with theme + skin axes** — the old monolithic theme list (`dark`, `slate`, `solarized`, `monokai`, `nord`, `oled`, `light`) is split into two orthogonal axes: **theme** (`light` / `dark` / `system`) and **skin** (accent palette: Default gold, Ares red, Mono gray, Slate blue-gray, Poseidon ocean blue, Sisyphus purple, Charizard orange). Users can now mix any theme with any skin via the new **Appearance** settings tab. Internally, `.dark` class on `<html>` replaces `data-theme`; skin uses `data-skin` attribute and overrides only 5 accent CSS vars per skin, eliminating ~200 lines of duplicated palette overrides. (PR #627 by @aronprins)

### Migration notes
- **Legacy theme names are silently migrated on first load** to the closest theme + skin pair: `slate → dark+slate`, `solarized → dark+poseidon`, `monokai → dark+sisyphus`, `nord → dark+slate`, `oled → dark+default`. Both backend (`api/config.py::_normalize_appearance`) and frontend (`static/boot.js::_normalizeAppearance`) apply the same mapping.
- **Custom themes set via `data-theme` CSS overrides will reset** to `dark + default` on first load. The pre-PR `theme` setting was open-ended ("no enum gate -- allows custom themes"); the new system enumerates valid values. Users who maintained custom CSS will need to re-apply via a skin choice or by overriding skin variables (`--accent`, `--accent-hover`, `--accent-bg`, `--accent-bg-strong`, `--accent-text`).

### Fixed
- **Send button stays active after clearing composer text** — input listener now correctly toggles disabled state. (PR #627)
- **Composer workspace/model label flash on page load** — chips now wait for `_bootReady` before populating, eliminating the placeholder-then-real-value flicker. (PR #627)
- **Topbar border invisible in light mode** — added `:root:not(.dark)` border override. (PR #627)
- **User message bubble text contrast** — accent-colored bubbles now use skin-aware text colors meeting WCAG AA (Poseidon dark improved from 2.8 → 6.5 ratio). (PR #627)
- **Settings skin persistence race condition** — save now waits for server confirmation before applying. (PR #627)
## [v0.50.78] — 2026-04-17

### Fixed
- **Gemma 4 thinking tokens no longer shown raw in chat** — added `<|turn|>thinking\n...<turn|>` to the streaming think-token parser in `static/messages.js` and `_strip_thinking_markup()` in `api/streaming.py`. Previously Gemma 4's reasoning output appeared as raw text prepended to the answer. (Closes #607)
## [v0.50.79] — 2026-04-17

### Fixed
- **Default model no longer shows as "(unavailable)" for non-OpenAI users** — changed the hardcoded fallback `DEFAULT_MODEL` from `openai/gpt-5.4-mini` to `""` (empty). When no default model is configured, the WebUI now defers to the active provider's own default instead of pre-selecting an OpenAI model that most providers don't have. Users who want a specific default can still set `HERMES_WEBUI_DEFAULT_MODEL` env var or pick a model in Preferences. (Closes #646)

## [v0.50.80] — 2026-04-18

### Fixed
- **Clicking a skill no longer silently loads content into a hidden panel** — `openSkill()` now calls `ensureWorkspacePreviewVisible()` so the workspace panel auto-opens when you click a skill in the Skills tab. (Closes #643)
- **Long thinking/reasoning traces now scroll instead of being clipped** — the thinking card body now uses `overflow-y: auto` when open, so long traces are fully readable. (Closes #638)
- **Sidebar nav icon hit targets are now correctly aligned** — added `display:flex; align-items:center; justify-content:center` to `.nav-tab` so clicking the icon itself (not below it) activates the tab. (Closes #636)
- **Safari iOS input auto-zoom fixed** — bumped `textarea#msg` base font-size from 14px to 16px, which prevents Safari from zooming the viewport on input focus (Safari zooms when font-size < 16px). Visual difference is negligible. (Closes #630)

## [v0.50.76] — 2026-04-17

### Fixed
- **CSP blocked external images in chat** — `img-src` in the Content Security Policy was restricted to `'self'` and `data:`, causing the browser to block any external image URLs (e.g. from Wikipedia, GitHub, or other HTTPS sources) that the agent rendered in a response. Expanded to `img-src 'self' data: https: blob:` so external images load correctly. (Closes #608)

## [v0.50.75] — 2026-04-17

### Fixed
- **Test isolation: `pytest tests/` was overwriting `~/.hermes/.env` with test placeholder keys** — two unit tests in `test_onboarding_existing_config.py` called `apply_onboarding_setup()` in-process without mocking `_get_active_hermes_home`, so every test run wrote `OPENROUTER_API_KEY=test-key-fresh` (or `test-key-confirm`) to the production `.env`. Also added `HERMES_BASE_HOME` to the test server subprocess env (hard-locks profile resolution inside the server to the isolated temp state dir) and stripped real provider keys from the inherited subprocess environment. (PR #620)

## [v0.50.71] — 2026-04-16

### Fixed
- **Docker: `HERMES_WEBUI_DEFAULT_WORKSPACE` was silently overridden by `settings.json`** — the startup block in `api/config.py` unconditionally restored the persisted `default_workspace`, so any container that had previously written `settings.json` would shadow the env var on the next start. The env var now wins when explicitly set, matching the documented priority order. (Closes #609, PR #610)
- **Docker: workspace trust validation rejected subdirectories of `DEFAULT_WORKSPACE`** — `resolve_trusted_workspace()` only trusted paths under `Path.home()` or in the saved list; subpaths of a Docker volume mount like `/data/workspace/myproject` failed with "outside the user home directory". Added a third trust condition for paths under the boot-time `DEFAULT_WORKSPACE`, which was already validated at startup. (Closes #609, PR #610)

## [v0.50.70] — 2026-04-16

### Changed
- **Chat transcript redesigned** — unified `--msg-rail`/`--msg-max` CSS variables align all message elements on one column. User turns render as per-theme tinted cards. Thinking cards are bordered panels with gold rule. Inline code inherits `--strong`. Action toolbar fades in on hover. Error-prefixed assistant rows get `[data-error="1"]` red-accent card treatment. Day-change `.msg-date-sep` separators added. Transcript fades to transparent behind composer. (PR #587 by @aronprins)
- **Approval and clarify cards as composer flyouts** — cards slide up from behind the composer top edge rather than floating as disconnected banners. `overflow:hidden` outer + `translateY` inner animation clips travel. `focus({preventScroll:true})` prevents autoscrolling. (PR #587 by @aronprins)

### Fixed
- **Streaming lifecycle stabilised** — DOM order stays `user → thinking → tool cards → response` with no mid-stream jump. Live tool cards inserted inline before the live assistant row. Ghost empty assistant header suppressed on pure-tool turns. (PR #587 by @aronprins)
- **Session reload persistence hardened** — last-turn reasoning attached before `s.save()`, so hard-refresh right after a response preserves the thinking trace. `role=tool` rows preserved in `S.messages`. CLI-session tool-result fallback parses output envelopes and attaches snippets to matching cards. (PR #587 by @aronprins)
- **Workspace panel first-paint flash fixed** — `[data-workspace-panel]` attribute set at document parse time via inline script. (PR #587 by @aronprins)

### Added
- `docs/ui-ux/index.html` — static inventory of every message-area element loading live `static/style.css`. (PR #587 by @aronprins)
- `docs/ui-ux/two-stage-proposal.html` — proposal page for the two-stage plan/execute flow (#536). (PR #587 by @aronprins)

## [v0.50.69] — 2026-04-16

### Fixed
- **Docker: workspace file browser no longer appears empty on macOS** — `docker_init.bash` now auto-detects the correct `WANTED_UID` and `WANTED_GID` from the mounted `/workspace` directory at startup. On macOS, host UIDs start at 501 (not 1000), so the default value of 1024 caused the container user to run as a different UID than the files, making the workspace appear empty. The auto-detect reads `stat -c '%u'` on `/workspace` and uses it when no explicit `WANTED_UID` is set — falling back to 1024 if the path doesn't exist or returns 0 (root). Setting `WANTED_UID` explicitly in a `.env` file still takes full precedence. (Closes #569)
- **Session message count inconsistency resolved** — the topbar already correctly shows only visible messages (excluding `role='tool'` tool-call entries). The sidebar previously showed raw `message_count` which included tool messages, but PR #584 removed that display entirely — there is no longer any count displayed in the sidebar. No code change needed; documenting with regression tests. (Closes #579)

## [v0.50.68] — 2026-04-16

### Fixed
- **Light theme: add/rename folder dialogs now use correct light colors** — `.app-dialog`, `.app-dialog-input`, `.app-dialog-btn`, `.app-dialog-close`, and `.file-rename-input` had hardcoded dark-mode backgrounds with no light-theme overrides. Dialog backgrounds, borders, and inputs now adapt correctly to the light theme. (Closes #594)
- **Workspace panel no longer snaps open then immediately closed** — on page load, `boot.js` was restoring the panel open/closed state from `localStorage` before knowing whether the loaded session has a workspace. `syncWorkspacePanelState()` then snapped it closed, causing a visible jank. The restore is now deferred until after `loadSession()` and only applied when the session actually has a workspace. (Closes #576)
- **Model dropdown reflects CLI model changes without server restart** — `/api/models` was returning a startup-cached snapshot of `config.yaml`. The fix adds a mtime-based reload check: if `config.yaml` has changed on disk since last read, the cache is refreshed before building the model list. Page refresh now picks up CLI model changes immediately. (Closes #585)
- **Docker Compose: macOS users guided on UID/GID setup** — the `docker-compose.yml` comment for `WANTED_UID`/`WANTED_GID` now explicitly notes that macOS UIDs start at 501 (not 1000) and tells users to run `id -u`/`id -g`. Also clarifies that the default `${HOME}/.hermes` volume mount works on both macOS and Linux. (Closes #567)
- **Voice transcription already shows "Transcribing…" spinner** — issue #590 noted that no feedback was shown between pressing stop and text appearing. This was already implemented (`setComposerStatus('Transcribing…')` fires before the fetch in `_transcribeBlob`). Confirmed and documented; closing as already fixed.

## [v0.50.67] — 2026-04-16

### Added
- **Subpath mount support** — Hermes WebUI can now be served behind a reverse proxy at any subpath (e.g. `/hermes-webui/` via Tailscale Serve, nginx, or Caddy). A dynamic `<base href>` is injected as the first script in `<head>`, and all client-side URL references are converted from absolute to relative. The server-side route handlers are unchanged. No configuration needed — works transparently for both root (`/`) and subpath deployments. (PR #588 by @vcavichini)

## [v0.50.66] — 2026-04-16

### Fixed
- **WebUI agent now receives full runtime route from provider resolver** — previously `api_mode`, `acp_command`, `acp_args`, and `credential_pool` were not forwarded into `AIAgent.__init__()` in the WebUI streaming path. Users switching between Codex accounts or using credential pools found the switch worked in the CLI but not the WebUI. The fix passes all four fields from the resolved runtime into the agent constructor. (PR #582 by @suinia)

## [v0.50.65] — 2026-04-16

### Fixed
- **`HERMES_WEBUI_SKIP_ONBOARDING=1` now works unconditionally** — previously the env var was gated on `chat_ready=True`, so hosting providers (e.g. Agent37) that set it but hadn't yet wired up a provider key would still see the wizard on every page load. The var is now honoured as a hard operator override regardless of `chat_ready`. If you set it, the wizard is gone. (Fixes skip-onboarding regression)
- **Onboarding wizard can no longer overwrite config or env files when `SKIP_ONBOARDING` is set** — `apply_onboarding_setup` now checks the env var first and refuses to touch `config.yaml` or `.env` if it is set. This is a belt-and-suspenders guard: even if a stale JS bundle somehow triggers the setup endpoint while `SKIP_ONBOARDING` is active, no files are written.


## [v0.50.64] — 2026-04-16

### Changed
- **Sidebar session items decluttered** — the meta row under every session title (message count, model slug, and source-tag badge) has been removed. Each session now renders as a single line: title + relative-time bucket headers. The visible session count at a typical viewport height roughly doubles. The `source_tag` field is still populated on the session object and available for a future tooltip or filter facet. `[SYSTEM:]`-prefixed gateway titles fall back to `"Session"` rather than leaking system-prompt content. Removes `_formatSourceTag()`, `.session-meta`, `cli-session`, `[data-source=…]`, `_SOURCE_DISPLAY`, and the associated CSS badge rules. (PR #584 by @aronprins)

## [v0.50.63] — 2026-04-16

### Fixed
- **Onboarding wizard no longer fires for non-standard providers** — providers outside the quick-setup list (`minimax-cn`, `deepseek`, `xai`, `gemini`, etc.) were always evaluated as `chat_ready=False` because `_provider_api_key_present()` only knew the four built-in env-var names. Those users saw the wizard on every page load and risked `config.yaml` being silently overwritten if the provider dropdown defaulted. The fix adds a `hermes_cli.auth.get_auth_status()` fallback covering every API-key provider in the full registry, and tightens the frontend guard so an unchanged unsupported-provider form never POSTs. (Fixes #572, PR #575)
- **MCP server toolsets now included in WebUI agent sessions** — previously the WebUI read `platform_toolsets.cli` directly from `config.yaml`, which only carries built-in toolset names. MCP server names (`tidb`, `kyuubi`, etc.) were silently dropped, so MCP tools configured via `~/.hermes/config.yaml` were unavailable in chat. The fix delegates to `hermes_cli.tools_config._get_platform_tools()` — the same code the CLI uses — which merges all enabled MCP servers automatically. Falls back gracefully when `hermes_cli` is unavailable. (PR #574 by @renheqiang)

## [v0.50.62] — 2026-04-16

### Fixed
- **Docker startup no longer hard-exits when hermes-agent source is not mounted** — previously `docker_init.bash` would call `error_exit` if the agent source directory was missing, preventing the container from starting at all. Users running a minimal `docker run` without the two-container compose setup hit this immediately. Now the script checks for the directory and `pyproject.toml` first, prints a clear warning explaining reduced functionality, and continues startup. The WebUI already has `try/except` fallbacks throughout for when hermes-agent is unavailable. (Fixes #570, PR #573)

## [v0.50.61] — 2026-04-16

### Added
- **Office file attachments** — `.xls`, `.xlsx`, `.doc`, and `.docx` files can now be selected via the attach button. The file picker's `accept` attribute is extended to include Office MIME types, and the backend MIME map is updated so these files are served with correct content-type headers when accessed through the workspace file browser. Files are saved as binary to the workspace; the AI can reference them by name the same way it does PDFs. (PR #566 by @renheqiang)

## [v0.50.60] — 2026-04-16

### Changed
- **Test robustness** — two onboarding setup tests (`test_setup_allowed_with_confirm_overwrite`, `test_setup_allowed_when_no_config_exists`) now skip gracefully when PyYAML is not installed in the test environment, matching the pattern already used in `test_onboarding_mvp.py`. No production code changed. (PR #564)

## [v0.50.59] — 2026-04-16

### Fixed
- **False "Connection lost" message after settled stream** — the UI no longer injects a fake `**Error:** Connection lost` assistant message when an SSE connection drops after the stream already completed normally. The fix tracks terminal stream states (`done`, `stream_end`, `cancel`, `apperror`) and, on a disconnect, fetches `/api/session` to confirm the session is settled before silently restoring it instead of calling the error path. Real failures still go through the error path as before. (Fixes #561, PR #562 by @halmisen)

## [v0.50.58] — 2026-04-16

### Fixed
- **Custom provider name in model dropdown** — when a `custom_providers` entry in `config.yaml` has a `name` field (e.g. `Agent37`), the model picker now shows that name as the group header instead of the generic `Custom` label. Multiple named providers each get their own group. Unnamed entries still fall back to `Custom`. Brings the web UI into parity with the terminal's provider display. (Fixes #557)

## [v0.50.57] — 2026-04-15

### Added
- **Auto-generated session titles** — after the first exchange, a background thread generates a concise title from the first user message and assistant reply, replacing the default first-message substring. Updates live in the UI via a new `title` SSE event. Manual renames are preserved; generation only runs once per session. Includes MiniMax token budget handling and a local heuristic fallback. (Fixes #495, PR #535 by @franksong2702)

### Changed
- **SSE stream termination** — streams now end with `stream_end` instead of `done` so the background title generation thread has time to emit the title update before the client disconnects.

## [v0.50.55] — 2026-04-15

### Fixed
- **Docker honcho extra** — `docker_init.bash` now installs `hermes-agent[honcho]` so `honcho-ai` is included in the venv on every fresh Docker build. Fixes `"Honcho session could not be initialized."` errors on rebuilt containers. (Fixes #553)
- **Version badge** — `index.html` version badge corrected to v0.50.55 (was missing the bump for this release).

## [v0.50.54] — 2026-04-15

### Changed
- **OpenRouter model list** — updated to 14 current models across 7 providers. All slugs verified live against the OpenRouter catalog. Removed `o4-mini`, old Gemini 2.x entries, and Llama 4. Added Claude Opus 4.6, GPT-5.4, Gemini 3.1 Pro Preview, Gemini 3 Flash Preview, DeepSeek R1, Qwen3 Coder, Qwen3.6 Plus, Grok 4.20, and Mistral Large. Both Claude 4.6 and 4.5 generations preserved. Fixed `grok-4-20` → `grok-4.20` slug and Gemini `-preview` suffixes.

## [v0.50.53] — 2026-04-15

### Fixed
- **Custom endpoint slash model IDs** — model IDs with vendor prefixes that are intrinsic (e.g. `zai-org/GLM-5.1` on DeepInfra) are now preserved when routing to a custom `base_url` endpoint. Previously, all prefixed IDs were stripped, causing `model_not_found` errors on providers that require the full vendor/model format. Known provider namespaces (`openai/`, `google/`, `anthropic/`, etc.) are still stripped as before. (Fixes #548, PR #549 by @eba8)

## [v0.50.52] — 2026-04-15

### Fixed
- **Simultaneous approval requests** — parallel tool calls that each require approval no longer overwrite each other. `_pending` is now a list per session; each entry gets a stable `approval_id` (uuid4) so `/api/approval/respond` can target a specific request. The UI shows a "1 of N pending" counter when multiple approvals are queued. Backward-compatible with old agent versions and old frontend clients. Adds 14 regression tests. (Fixes #527)

## [v0.50.51] — 2026-04-15

### Fixed
- **Orphaned tool messages** — conversation histories containing `role: tool` messages with no matching `tool_call_id` in a prior assistant message are now silently stripped before sending to the provider API. Fixes 400 errors from strictly-conformant providers (Mercury-2/Inception, newer OpenAI models). Adds 13 regression tests. (Fixes #534)

## [v0.50.50] — 2026-04-15

### Fixed
- **Code block syntax highlighting** — Prism theme now follows the active UI theme. Light mode uses the default Prism light theme; dark mode uses `prism-tomorrow`. Theme swaps happen immediately on toggle including on first load. Adds `id="prism-theme"` to the Prism CSS link so JavaScript can locate and swap it. (Closes #505, PR #530 by @mariosam95)

## [v0.50.49] — 2026-04-15

### Fixed
- **IME composition** — `isComposing` guard added to every Enter keydown handler so CJK/Japanese/Korean input method users never accidentally send mid-composition (fixes #531). Covers chat composer, command dropdown, session rename, project create/rename, app dialog, message edit, and workspace rename. Adds 3 regression tests. (PR #537 by @vansour)

## [v0.50.48] fix: toast when model is switched during active session (#419)

Synthesized from PRs #516 (armorbreak001), #517 and #518 (cloudyun888).

When a user switches the model via the model picker while a session already
has messages, a 3-second toast now reads: "Model change takes effect in
your next conversation." This avoids the confusing situation where the
dropdown shows the new model but the current conversation continues with
the original one.

The toast fires from `modelSelect.onchange` in `static/boot.js`, after the
existing provider-mismatch warning. It checks `S.messages.length > 0` (the
reliable in-memory array, always initialized by `loadSession`). The
`showToast` call is guarded with `typeof` for safety during boot.

Key differences from submitted PRs: placement in boot.js onchange (covers
all selection paths including chip dropdown, since `selectModelFromDropdown`
calls `sel.onchange`), and uses `S.messages` not `S.session.messages`.

4 new tests in `tests/test_provider_mismatch.py::TestModelSwitchToast`.

Total tests: 1272 (was 1268)


## [v0.50.47] fix/feat: batch fixes — root workspace, custom providers, cron cache, system theme

Synthesized from PRs #506, #507, #508, #509, #510, #514, #515, #519, #521.

### Fixes

**Allow /root as a workspace path** (PRs #510, #521 by @ccqqlo)
Removes `/root` from `_BLOCKED_SYSTEM_ROOTS` in `api/workspace.py`, so
deployments running as root (Docker, VPS) can set `/root` as their workspace
without a "system directory" rejection.

**Guard against split on missing [Attached files:]** (PR #521 by @ccqqlo)
`base_text` extraction in `api/streaming.py` now guards: `msg_text.split(...)[0]
if ... in msg_text else msg_text`. Previously split on the empty case returned
an empty string, causing attachment-matching to silently fail on messages with
no attachments.

**custom_providers models visible regardless of active provider** (#515, #519 by @shruggr, @cloudyun888)
`get_available_models()` in `api/config.py` no longer discards the 'custom'
provider from `detected_providers` when the user has `custom_providers` entries
in `config.yaml`. Previously, switching active_provider away from 'custom'
hid all custom model definitions from the picker.

**Cron skill picker cache invalidated on form open and skill save** (PRs #507, #508 by @armorbreak001)
`toggleCronForm()` now unconditionally nulls `_cronSkillsCache` before fetching,
so skills created in the same session appear immediately. `submitSkillSave()` also
nulls `_cronSkillsCache` after a successful write, mirroring the existing
`_skillsData = null` pattern. Fixes #502.

### Features

**System (auto) theme following OS prefers-color-scheme** (#504 / PRs #506, #509, #514 by @armorbreak001, @cloudyun888)
New "System (auto)" option in the theme picker follows the OS dark/light preference
via `window.matchMedia`. Changes:
- `static/boot.js`: `_applyTheme(name)` helper resolves 'system' via matchMedia,
  sets `data-theme`, and registers a MQ change listener for live OS tracking.
  `loadSettings()` calls `_applyTheme()` instead of direct assignment.
- `static/index.html`: flicker-prevention script resolves 'system' before first
  paint. Adds "System (auto)" as first theme option. onchange calls `_applyTheme()`.
- `static/commands.js`: adds 'system' to valid `/theme` names.
- `static/panels.js`: `_settingsThemeOnOpen` reads from localStorage (preserves
  'system' string). `_revertSettingsPreview` calls `_applyTheme()`.
- `static/i18n.js`: cmd_theme description lists 'system' first in all 5 locales.

### Tests

22 new tests in `tests/test_batch_fixes.py`.

Total tests: 1268 (was 1246)


## [v0.50.46] feat: clarify dialog flow and refresh recovery (#520)

Adds a full clarify dialog UX for interactive agent questions — modeled after
the approval card but for free-form clarification prompts.

### Backend

New `api/clarify.py` module with a per-session pending queue backed by
`threading.Event` unblocking, gateway notify callbacks, duplicate deduplication
while unresolved, and resolve/clear helpers.

Three new HTTP endpoints in `api/routes.py`:
- `GET /api/clarify/pending` — poll for pending clarify prompt
- `POST /api/clarify/respond` — resolve the pending prompt
- `GET /api/clarify/inject_test` — loopback-only, for automated tests

`api/streaming.py` wires `clarify_callback` into `AIAgent.run_conversation()`.
Emits `clarify` SSE events; blocks the tool flow until the user responds, times
out (120s), or the stream is cancelled. Also adds a 409 guard on `chat/start` so
page-refresh races return the active stream id instead of starting a duplicate.

### Frontend

`static/messages.js`: clarify card with numbered choices, Other button, and
free-text input. Composer is locked while clarify is active. DOM self-heals if
the card node is removed during a rerender. SSE `clarify` event listener plus
1.5s fallback polling. Session switch and reconnect start/stop clarify polling.
409 conflict flow reattaches to the active stream and queues the user message.
`CLARIFY_MIN_VISIBLE_MS = 30000` timer dedup mirrors the approval card pattern.

`static/ui.js`: `lockComposerForClarify()` / `unlockComposerForClarify()` with
saved-state restore. `updateSendBtn()` respects the disabled state.

`static/sessions.js`: `loadSession()` starts/stops clarify polling on switch
and inflight reattach.

`static/index.html` / `static/style.css`: clarify card markup with ARIA roles
and full responsive/mobile styles.

`static/i18n.js`: 6 new keys in all 5 locales (en, es, de, zh-Hans, zh-Hant).

### Tests

- `tests/test_clarify_unblock.py`: 14 new tests covering queue resolution,
  notify callbacks, clear-on-cancel, and all three HTTP endpoints.
- `tests/test_sprint30.py`: 31 new clarify tests (HTML markup, CSS classes,
  i18n keys, messages.js functions, streaming registration flags).
- `tests/test_sprint36.py`: expand search window for `setBusy` check after
  additional `stopClarifyPolling()` calls push it past the old 800-char limit.

Total tests: 1246 (was 1209)

Co-authored-by: franksong2702


## [v0.50.45] fix: suppress N/A source_tag in session list (#429)

Feishu and WeChat sessions (and any session with an unrecognised or legacy
`source` value in hermes-agent's state.db) were showing "N/A" or raw tag
strings in the session list sidebar.

Three fixes in `static/sessions.js`:

1. `_formatSourceTag()` now returns `null` for unrecognised tags instead of
   the raw string. Known platforms (telegram, discord, slack, feishu, weixin,
   cli) still display their human-readable label. Unknown/legacy values are
   silently suppressed.

2. The `metaBits` push is guarded: stores the result in `_stLabel` and only
   pushes if it is non-null. Prevents `null` or unrecognised platform names
   from appearing in the session metadata line.

3. The `[SYSTEM:]` title fallback now uses `_SOURCE_DISPLAY[s.source_tag] ||
   'Gateway'` — the raw `s.source_tag` middle term is removed so a session
   whose source is "N/A" does not use that as its visible title.

No backend changes. The upstream issue (hermes-agent not reliably setting
`source` for older Feishu/WeChat sessions) is tracked separately.

7 new tests in `tests/test_issue429.py`. Updated 1 existing test in
`tests/test_sprint40_ui_polish.py` to match the new guarded push pattern.

- Total tests: 1202 (was 1195)

## [v0.50.44] fix: code-in-table CSS sizing + markdown image rendering (#486, #487)

**CSS: inline code inside table cells** (fixes #486)

Inline `` `code` `` spans inside `<td>` and `<th>` cells were rendering too
large relative to the cell height — the `.msg-body code` rule sets `12.5px`
which sits awkward against the table's `12px` base font.

Fix: added two targeted rules in `static/style.css`:

    .msg-body td code,.msg-body th code { font-size:0.85em; padding:1px 4px; vertical-align:baseline; }
    .preview-md td code,.preview-md th code { font-size:0.85em; padding:1px 4px; vertical-align:baseline; }

Covers both the chat message surface (`.msg-body`) and the markdown preview
panel (`.preview-md`).

**JS renderer: `![alt](url)` image syntax** (fixes #487)

Standard markdown image syntax was not handled by `renderMd()`. The `!` was
left as a stray character and `[alt](url)` was consumed by the link pass,
producing `! <a href="url">alt</a>` instead of an `<img>`.

Fix: added an image pass to both `inlineMd()` (for images in table cells,
list items, blockquotes, headings) and the outer `renderMd()` pipeline (for
images in plain paragraphs):

- Regex: `![alt](https?://url)` — only `http://` and `https://` URIs accepted;
  `javascript:` and `data:` URIs cannot match.
- Alt text passes through `esc()` — XSS-safe.
- URL double-quotes percent-encoded to `%22` — attribute breakout prevented.
- Reuses `.msg-media-img` class — same click-to-zoom and max-width styling as
  agent-emitted `MEDIA:` images.
- `img` added to `SAFE_TAGS` allowlist so the generated `<img>` is not escaped.
- In `inlineMd()`: image pass runs while the `_code_stash` is still active,
  so `![alt](url)` inside a backtick span stays protected and is never rendered
  as an image. A new `_img_stash` (`\x00G`) protects rendered `<img>` tags
  from the autolink pass touching `src=` values.

**Tests**

45 new tests in `tests/test_issue486_487.py`:
- 13 CSS source checks and rendering tests for #486
- 22 JS source checks and rendering tests for #487
- 10 combination edge cases (code + image + link all in same table)

- Total tests: 1195 (was 1150)

## [v0.50.43] fix: markdown link rendering + KaTeX CSP fonts

**Markdown link rendering — `renderMd()` in `static/ui.js`** (PR #475, fixes #470)

Three related bugs fixed:

1. **Double-linking via autolink pass** — `[label](url)` was converted to `<a href="...">`, then the bare-URL autolink pass re-matched the URL sitting inside `href="..."` and wrapped it in a second `<a>` tag. Fixed with three stash/restore layers: `\x00L` (inlineMd labeled links), `\x00A` (existing `<a>` tags before outer link pass), `\x00B` (existing `<a>` tags before autolink pass).

2. **`esc()` on `href` values corrupts query strings** — `esc()` is HTML-entity encoding; applying it to URLs converted `&` → `&amp;` in query strings. Removed `esc()` from href values in all three locations. Display text (link labels) still uses `esc()` for XSS safety. `"` in URLs replaced with `%22` (URL encoding) to close the attribute-injection vector identified during review.

3. **Backtick code spans inside `**bold**` rendered as `&lt;code&gt;`** — `esc()` was applied to code spans after bold/italic processing. Added `\x00C` stash to protect backtick spans in `inlineMd()` before bold/italic regex runs.

**Security audit:** `javascript:` injection blocked by `https?://` prefix requirement. `"` attribute breakout fixed by `.replace(/"/g, '%22')`. Label/display text still HTML-escaped.

24 tests in `tests/test_issue470.py`.

**KaTeX CSP font-src** (fixes #477)

`api/helpers.py` CSP `font-src` now includes `https://cdn.jsdelivr.net` so KaTeX math rendering fonts load correctly. Previously ~50 CSP font-blocking errors appeared in the console on any page with math content. The CDN was already allowed in `script-src` and `style-src` for KaTeX JS/CSS — this extends the same allowance to fonts.

3 tests in `tests/test_issue477.py`.

- Total tests: 1150 (was 1130)

## [v0.50.42] fix: session display + model UX polish (sprint 42)

**Context indicator always shows latest usage** (PR #471, fixes #437)
The context ring/indicator in the composer footer was reading token counts and cost
from the stored session snapshot with `||` — meaning stale non-zero values from
previous turns always won over a fresh `0` from the current turn. Replaced all six
field merges with a `_pick(latest, stored, dflt)` helper that correctly prefers the
latest usage when it's a real value (including `0`).

**System prompt no longer leaks as gateway session title** (PR #472, fixes #441)
Telegram, Discord, and CLI gateway sessions inject a system message before any user
turn. When the session title is set from this message, the sidebar shows
`[SYSTEM: The user has inv...` instead of a meaningful name. Added a guard in
`_renderOneSession()`: if `cleanTitle` starts with `[SYSTEM:`, replace it with the
platform display name (`Telegram session`, `Discord session`, etc.).

**Thinking/reasoning panel persists across page reload** (PR #473, fixes #427)
The full chain-of-thought from Claude, Gemini, and DeepSeek thinking models was lost
after streaming completed and on every page reload. Two-part fix:
- `api/streaming.py`: `on_reasoning()` now accumulates `_reasoning_text`; before the
  session is serialised at stream end, `_reasoning_text` is injected into the last
  assistant message so it's stored in the session JSON
- `static/messages.js`: in the `done` SSE handler, `reasoningText` is also patched
  onto the last assistant message as a belt-and-suspenders client-side fallback

**Custom model ID input in model picker** (PR #474, fixes #444)
Users who need a model not in the curated list (~30 models) can now type any model
ID directly in the dropdown. A text input at the bottom of the model picker lets
users enter any string (e.g. `openai/gpt-5.4`, `deepseek/deepseek-r2`, or any
provider-prefixed ID) and press Enter or click + to use it immediately.
i18n keys added to en, es, zh.

- Total tests: 1130 (was 1117)

## [v0.50.41] feat(ui): render MEDIA: images inline in web UI chat (fixes #450)

When the agent outputs `MEDIA:<path>` tokens — screenshots from the browser tool,
generated images, vision outputs — the web UI now renders them **inline in the chat**,
the same way Claude.ai handles images. No more relaying screenshots through Telegram.

**How it works:**
- Local image path (`MEDIA:/tmp/screenshot.png`): rendered as `<img>` via `/api/media?path=...`
- HTTP(S) URL to image (`MEDIA:https://example.com/img.png`): `<img>` directly from the URL
- Non-image file (`MEDIA:/tmp/report.pdf`): styled download link (📎 filename)
- Click any inline image to toggle full-size zoom

**New endpoint — `GET /api/media?path=<encoded-path>`:**
- Path allowlist: `~/.hermes/`, `/tmp/`, active workspace — covers all agent output locations
- Auth-gated: requires valid session cookie when auth is enabled
- Inline image MIME types: PNG, JPEG, GIF, WebP, BMP
- SVG always served as download attachment (XSS prevention)
- RFC 5987-compliant `Content-Disposition` headers (handles Unicode filenames)
- `Cache-Control: private, max-age=3600`

**Security:**
- Original version had `~` (entire home dir) as an allowed root — **fixed** by independent reviewer
- Restricted to `~/.hermes/`, `/tmp/`, and active workspace only
- `Path.resolve()` + `commonpath` checks prevent symlink traversal

**Changes:**
- `api/routes.py`: `_handle_media()` handler + `/api/media` route
- `static/ui.js`: `MEDIA:` stash in `renderMd()` (runs before `fence_stash`, stash token `\x00D`)
- `static/style.css`: `.msg-media-img` (480px max-width, zoom-on-click), `.msg-media-link`
- `tests/test_media_inline.py`: 19 new tests (static analysis + integration)

- Total tests: 1117 (was 1098)

## [v0.50.40] feat: session UI polish + parallel test isolation

**Session sidebar improvements:**
- `static/sessions.js` + `style.css`: Hide session timestamps to give titles full available width — no more title truncation from inline timestamps (PR #449)
- `static/style.css`: Active session title now uses `var(--gold)` theme variable instead of hardcoded `#e8a030` — adapts correctly across all 7 themes (PR #451, fixes #440)
- `api/models.py` + `api/gateway_watcher.py`: Return `None` instead of the string `'unknown'` for missing gateway session model — Telegram sessions no longer show `telegram · unknown` (PR #452, fixes #443)
- `static/style.css` + `static/sessions.js`: Mute Telegram badge from saturated `#0088cc` to `rgba(0, 136, 204, 0.55)`. Add `_formatSourceTag()` helper mapping platform IDs to display names (`telegram` → `via Telegram`) (PR #453, fixes #442)

**Bug fixes:**
- `api/config.py` `resolve_model_provider()`: Strip provider prefix from model ID when a custom `base_url` is configured (`openai/gpt-5.4` → `gpt-5.4`) — fixes broken chats after switching to a custom endpoint (PR #454, fixes #433)
- `static/panels.js` `switchToProfile()`: Apply profile default workspace to new session created during profile switch — workspace chip no longer shows "No active workspace" after switching profiles mid-conversation (PR #455, fixes #424)

**Test infrastructure:**
- `tests/conftest.py` + `tests/_pytest_port.py` (new): Auto-derive unique port and state dir per worktree from repo path hash (range 20000-29999). Running pytest in two worktrees simultaneously no longer causes port conflicts. All 43 test files updated from hardcoded `BASE = "http://127.0.0.1:8788"` to `from tests._pytest_port import BASE` (PR #456)

- Total tests: 1098 (was 1078)

## [v0.50.39] fix: orphan gateway sessions + first-password-enablement session continuity

Two bug fixes:

**PR #423 — Fix orphan gateway sessions in sidebar (@aronprins, fix by maintainer)**
`gateway_watcher.py`'s `_get_agent_sessions_from_db()` was missing the
`HAVING COUNT(m.id) > 0` clause that `get_cli_sessions()` already had. Sessions
with no messages (e.g. created then abandoned before any turns) would appear in the
sidebar via the SSE watcher stream even after the initial page load filtered them out.
One-line SQL fix applied to both query paths.

**PR #434 — First-password-enablement session continuity (@SaulgoodMan-C)**
When a user enables a password for the first time via POST `/api/settings`,
the current browser session was being terminated — requiring the user to log in
again immediately after setting their password. Fix: the response now includes
`auth_enabled`, `logged_in`, and `auth_just_enabled` fields, and issues a
`hermes_session` cookie when auth is first enabled, so the browser remains logged in.
Also: legacy `assistant_language` key is now dropped from settings on next save.
New i18n keys for password replacement/keep-existing states (en, es, de, zh, zh-Hant).

- `api/config.py`: `_SETTINGS_LEGACY_DROP_KEYS` removes `assistant_language` on load
- `api/routes.py`: first-password-enable session continuity with `auth_just_enabled` flag
- `static/panels.js`: `_setSettingsAuthButtonsVisible()` + `_applySavedSettingsUi()` helpers
- `static/i18n.js`: password state i18n keys across 5 locales
- `tests/test_sprint45.py`: 3 new integration tests (auth continuity + legacy key cleanup)

- Total tests: 1078 (was 1075)


## [v0.50.38] feat: mobile nav cleanup, Prism syntax highlighting, zh-CN/zh-Hant i18n

Three community contributions combined:

**PR #425 — Remove mobile bottom nav (@aronprins)**
The fixed iOS-style bottom navigation bar on phones has been removed. The sidebar drawer
tabs already handle all navigation — the bottom nav was redundant and consumed ~56px of
vertical chat space. `test_mobile_layout.py` updated with `test_mobile_bottom_nav_removed()`
and new sidebar nav coverage tests.

**PR #426 — Prism syntax highlighting with light + dark theme token colors (@GiggleSamurai)**
Fenced code blocks now emit `class="language-{lang}"` on `<code>` elements, enabling Prism's
autoloader to apply token-level syntax highlighting. Added 36-line `:root[data-theme="light"]`
token color overrides scoped to light theme only; dark/dim/monokai/nord themes unaffected.
Background guard uses `var(--code-bg) !important` to prevent Prism's dark background from
overriding theme variables. 2 new regression tests in `test_issue_code_syntax_highlight.py`.

**PR #428 — zh-CN/zh-Hant i18n hardening (@vansour)**
Pluggable `resolvePreferredLocale()` function with smart zh-CN/zh-SG/zh-TW/zh-HK variant
mapping. Full zh-Simplified and zh-Traditional locale blocks added to `i18n.js`. Login page
locale routing updated in `api/routes.py` (`_resolve_login_locale_key()` helper). Hardcoded
strings in `panels.js` cron UI extracted to i18n keys. 3 new test files:
`test_chinese_locale.py`, `test_language_precedence.py`, `test_login_locale.py`.

- Total tests: 1075 (was 1063)

## [v0.50.37] fix(onboarding): skip wizard when Hermes is already configured

Fixes #420 — existing Hermes users with a valid `config.yaml` were shown the first-run
onboarding wizard on every WebUI load because the only completion gate was
`settings.onboarding_completed` in the WebUI's own settings file. Users who configured
Hermes via the CLI before the WebUI existed had no such flag, so the wizard always fired
and could silently overwrite their working config.

**Changes:**
1. `api/onboarding.py` `get_onboarding_status()`: auto-complete when `config.yaml` exists
   AND `chat_ready=True`. Existing configured users are never shown the wizard.
2. `api/onboarding.py` `apply_onboarding_setup()`: refuse to overwrite an existing
   `config.yaml` without `confirm_overwrite=True` in the request body. Returns
   `{error: "config_exists", requires_confirm: true}` for the frontend to handle.
3. `static/index.html`: "Skip setup" button added to wizard footer — users are never
   trapped in the wizard.
4. `static/onboarding.js`: `skipOnboarding()` calls `/api/onboarding/complete` without
   modifying config, then closes the overlay.
5. `static/boot.js`: Escape key now dismisses the onboarding overlay.
6. `static/i18n.js`: `onboarding_skip` / `onboarding_skipped` keys added to en + es locales.
7. `tests/test_onboarding_existing_config.py`: 8 new unit tests covering gate logic and
   overwrite guard.

- Total tests: 1063 (was 1055)


## [v0.50.36] fix: workspace list cleaner — allow own-profile paths, remove brittle string filter

Two bugs in `_clean_workspace_list()` caused workspace additions to silently disappear on the next `load_workspaces()` call, breaking `test_workspace_add_no_duplicate` and `test_workspace_rename` (and potentially causing real-world workspace list corruption):

**Bug 1 — Brittle string filter removed:** `if 'test-workspace' in path or 'webui-mvp-test' in path: continue` dropped any workspace path containing those substrings. In the test server, `TEST_WORKSPACE` is `~/.hermes/profiles/webui/webui-mvp-test/test-workspace`, so every workspace added during tests was silently discarded on the next `load_workspaces()` call. The `p.is_dir()` check already handles genuinely non-existent paths — the string filter was redundant and harmful.

**Bug 2 — Cross-profile filter was too broad:** `if p is under ~/.hermes/profiles/: skip` was designed to block cross-profile workspace leakage, but it also removed paths under the *current* profile's own directory (e.g. `~/.hermes/profiles/webui/...`). Fixed: now only skips paths under `profiles/` that are NOT under the current profile's own `hermes_home`.

- `api/workspace.py`: remove string-match filter; fix cross-profile check to allow own-profile paths
- All 1055 tests now pass (was 1053 pass + 2 fail)

## [v0.50.35] fix: workspace trust boundary — cross-platform, multi-workspace support

v0.50.34's workspace trust check was too restrictive: it required all workspaces to be under `DEFAULT_WORKSPACE` (/home/hermes/workspace), which blocked every profile-specific workspace (~/CodePath, ~/hermes-webui-public, ~/WebUI, ~/Camanji, etc.) and prevented switching between workspaces at all.

Replaced with a three-layer model that works cross-platform and supports multiple workspaces per profile:

1. **Blocklist** — `/etc`, `/usr`, `/var`, `/bin`, `/sbin`, `/boot`, `/proc`, `/sys`, `/dev`, `/root`, `/lib`, `/lib64`, `/opt/homebrew` always rejected, closing the original CVSS 8.8 vulnerability
2. **Home-directory check** — any path under `Path.home()` is trusted; `Path.home()` is cross-platform (`~/...` on Linux/macOS, `C:\\Users\\...` on Windows); allows all profile workspaces simultaneously since they don't need to share a single ancestor
3. **Saved-workspace escape hatch** — paths already in the profile's saved workspace list are trusted regardless of location, covering self-hosted deployments with workspaces outside home (`/data/projects`, `/opt/workspace`, etc.)

- `api/workspace.py`: rewritten `resolve_trusted_workspace()` with the three-layer model
- `tests/test_sprint3.py`: updated error-message assertions from `"trusted workspace root"` → `"outside"` (covers both old and new error strings)
- 1053 tests total (unchanged)

## [v0.50.34] fix(workspace): restrict session workspaces to trusted roots [SECURITY] (#415)

Session creation, update, chat-start, and workspace-add endpoints accepted arbitrary caller-supplied workspace paths. An authenticated caller could repoint a session to any directory the process could access, then use normal file read/write APIs to operate on attacker-chosen locations. CVSS 8.8 High (AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H).

- `api/workspace.py`: new `resolve_trusted_workspace(path)` helper — resolves path, checks existence + is_dir, enforces `path.relative_to(_BOOT_DEFAULT_WORKSPACE)` containment; requests outside the WebUI workspace root fail with 400
- `api/routes.py`: apply `resolve_trusted_workspace()` to all four entry points — `POST /api/session/new`, `POST /api/session/update`, `POST /api/chat/start` (workspace override), `POST /api/workspaces/add`
- `tests/test_sprint3.py`, `tests/test_sprint5.py`: regression tests for rejected outside-root paths on all four entry points; existing workspace tests updated to use trusted child directories
- `tests/test_sprint1.py`, `tests/test_sprint4.py`, `tests/test_sprint13.py`: aligned to new trusted-root contract
- Fix: use `_BOOT_DEFAULT_WORKSPACE` (respects `HERMES_WEBUI_DEFAULT_WORKSPACE` env for test isolation) rather than `_profile_default_workspace()` (reads agent terminal.cwd which may differ)
- Original PR by @Hinotoi-agent (cherry-picked; branch was 6 commits behind master)
- 1053 tests total (up from 1051; 2 pre-existing test_sprint5 isolation failures on master, not introduced by this PR)

## [v0.50.33] fix: workspace panel close button — no duplicate X on desktop, mobile X respects file preview (#413)

**Bug 1 — Duplicate X on desktop:** `#btnClearPreview` (the X icon) was always visible regardless of panel state, so desktop browse mode showed both the chevron collapse button and the X simultaneously. Fixed in `syncWorkspacePanelUI()`: on non-compact (desktop) viewports, `clearBtn.style.display` is set to `none` when no file preview is open, and cleared (shown) when a preview is active.

**Bug 2 — Mobile X collapsed the whole panel instead of dismissing the file:** `.mobile-close-btn` was wired to `closeWorkspacePanel()` directly, bypassing the two-step close logic. Fixed by changing `onclick` to `handleWorkspaceClose()`, which calls `clearPreview()` first if a file is open, and falls through to `closeWorkspacePanel()` otherwise.

**Also:** widened the `test_server_delete_invalidates_index` window from 600 → 1200 chars to accommodate the session_id validation guards added in v0.50.32 (#412).

- `static/boot.js`: `syncWorkspacePanelUI()` sets `clearBtn.style.display` based on `hasPreview` when `!isCompact`
- `static/index.html`: `.mobile-close-btn` onclick changed from `closeWorkspacePanel()` to `handleWorkspaceClose()`
- `tests/test_sprint44.py`: 10 new regression tests covering both fixes
- `tests/test_mobile_layout.py`: updated to accept `handleWorkspaceClose()` as valid onclick
- `tests/test_regressions.py`: widened delete handler window to 1200 chars
- 1051 tests total (up from 1041)

## [v0.50.32] fix(sessions): validate session_id before deleting session files [SECURITY] (#409)

`/api/session/delete` accepted arbitrary `session_id` values from the request body and built the delete path directly as `SESSION_DIR / f"{sid}.json"`. Because pathlib discards the prefix when `sid` is an absolute path, an attacker could supply `/tmp/victim` and cause the server to unlink `victim.json` outside the session store. Traversal-style values (`../../etc/target`) were also accepted. CVSS 8.1 High (AV:N/AC:L/PR:L/UI:N/S:U/C:N/I:H/A:H).

- `api/routes.py`: validate `session_id` against `[0-9a-z_]+` allowlist (covers `uuid4().hex[:12]` WebUI IDs and `YYYYMMDD_HHMMSS_hex` CLI IDs) before path construction; resolve candidate path and enforce `path.relative_to(SESSION_DIR)` containment before unlinking; only invalidate session index on successful deletion path, not on rejected requests
- `tests/test_sprint3.py`: 2 new regression tests — absolute-path payload rejected and file preserved, traversal payload rejected and file preserved
- Original PR by @Hinotoi-agent (cherry-picked; branch was 4 commits behind master)
- 1041 tests total (up from 1039)

## [v0.50.31] fix: delegate all live model fetching to agent's provider_model_ids()

`_handle_live_models()` in `api/routes.py` previously maintained its own per-provider fetch logic and returned `not_supported` for Anthropic, Google, and Gemini. Now it delegates entirely to the agent's `hermes_cli.models.provider_model_ids()` — the single authoritative resolver — and `_fetchLiveModels()` in `ui.js` no longer skips any provider.

**What each provider now returns (live data where credentials are present, static fallback otherwise):**
- `anthropic` — live from `api.anthropic.com/v1/models` (API key or OAuth token with correct beta headers)
- `copilot` — live from `api.githubcopilot.com/models` with required Copilot headers
- `openai-codex` — Codex OAuth endpoint → `~/.codex/` cache → `DEFAULT_CODEX_MODELS`
- `nous` — live from Nous inference portal
- `deepseek`, `kimi-coding` — generic OpenAI-compat `/v1/models`
- `opencode-zen`, `opencode-go` — OpenCode live catalog
- `openrouter` — curated static list (live returns 300+ which floods the picker)
- `google`, `gemini`, `zai`, `minimax` — static list (non-standard or Anthropic-compat endpoints)
- All others — graceful static fallback from `_PROVIDER_MODELS`

The hardcoded lists in `_PROVIDER_MODELS` remain as credential-missing / network-unavailable fallbacks. `api/routes.py` shrank by ~100 lines. Updated 2 tests to reflect the improved behavior.

- 1039 tests total (up from 1038)

## [v0.50.30] fix: openai-codex live model fetch routes through agent's get_codex_model_ids()

`_handle_live_models()` was grouping `openai-codex` with `openai` and sending `GET https://api.openai.com/v1/models` — which returns 403 because Codex auth is OAuth-based via `chatgpt.com`, not a standard API key. The live fetch silently failed, so users only ever saw the hardcoded static list.

- `api/routes.py`: dedicated early-return branch for `openai-codex` that calls `hermes_cli.codex_models.get_codex_model_ids()` — the same resolver the agent CLI uses. Resolution order: live Codex API (if OAuth token available, hits `chatgpt.com/backend-api/codex/models`) → `~/.codex/` local cache (written by the Codex CLI) → `DEFAULT_CODEX_MODELS` hardcoded fallback. Users with a valid Codex session now get their exact subscription model list including any models not in the hardcoded list.
- `api/routes.py`: improved label generation for Codex model IDs (e.g. `gpt-5.4-mini` → `GPT 5.4 Mini`)
- `tests/test_opencode_providers.py`: structural regression test verifying the dedicated `openai-codex` branch exists and calls `get_codex_model_ids()`
- 1038 tests total (up from 1037)

## [v0.50.29] fix: correct tool call card rendering on session load after context compaction (closes #401) (#402)

- `static/sessions.js`: replace the flat B9 filter in `loadSession()` with a full sanitization pass that builds `origIdxToSanitizedIdx` — each `session.tool_calls[].assistant_msg_idx` is remapped to the new sanitized-array position as messages are filtered; for tool calls whose empty-assistant host was filtered out, they attach to the nearest prior kept assistant
- `static/sessions.js`: set `S.toolCalls=[]` instead of pre-filling from session-level `tool_calls` — this lets `renderMessages()` use its fallback derivation from per-message `tool_calls` (which already carry correct indices into the sanitized message array); the fix eliminates the "200+ tool cards all on the wrong message" symptom on context-compacted session load
- `tests/test_issue401.py`: 8 regression tests — 4 static structural checks and 4 behavioural Node.js tests covering index remapping, multiple consecutive empty assistants, no-filtering pass-through, and `tool`-role message exclusion
- Original PR by @franksong2702 (cherry-picked onto master; branch was 31 commits behind)
- 1037 tests total (up from 1029)

## [v0.50.28] fix: expand openai-codex model catalog to match DEFAULT_CODEX_MODELS

`_PROVIDER_MODELS["openai-codex"]` only listed `codex-mini-latest`, so profiles using the `openai-codex` provider (e.g. a CodePath profile with `default: gpt-5.4`) showed only one entry in the model dropdown. Updated to mirror the agent's authoritative `DEFAULT_CODEX_MODELS` list: `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.2-codex`, `gpt-5.1-codex-max`, `gpt-5.1-codex-mini`, `codex-mini-latest`. Added 2 regression tests.

- 1029 tests total (up from 1027)

## [v0.50.27] feat: relative time labels in session sidebar (#394)

- `static/sessions.js`: new `_sessionCalendarBoundaries()` (DST-safe via `new Date(y,m,d)` construction), `_localDayOrdinal()`, `_formatSessionDate()` (includes year for dates from prior years); `_formatRelativeSessionTime()` now uses calendar midnight boundaries consistent with `_sessionTimeBucketLabel()` — no more label/bucket mismatch; all relative time strings call `t()` for localization; meta row only appended when non-empty (removes redundant group-header fallback); dead `ONE_DAY` constant removed
- `static/style.css`: add `session-item.active .session-title{color:#1a5a8a}` to light-theme block (fixes active title color in light mode)
- `static/i18n.js`: 11 new i18n keys (`session_time_*`) in both English and Spanish locale blocks; callable keys use arrow-function pattern consistent with existing `n_messages`
- `tests/test_session_sidebar_relative_time.py`: 5 tests — structural presence checks, behavioral Node.js tests via subprocess (yesterday/week boundary correctness, `just now` threshold, year-in-date for old sessions, full i18n key coverage for en+es)
- Original PR by @Jordan-SkyLF (two-pass review: blocking issues fixed in second commit)
- 1027 tests total (up from 1022)

## [v0.50.26] fix(sessions): redact sensitive titles in session list and search responses [SECURITY] (#400)

- `api/routes.py`: apply `_redact_text()` to session titles in all four response paths — `/api/sessions` merged list, `/api/sessions/search` empty-q, title-match, and content-match; use `dict(s)` copy before mutating to avoid corrupting the in-memory session cache
- `tests/test_session_summary_redaction.py`: 2 integration tests verifying `sk-` prefixed secrets in session titles are redacted from both list and search endpoint responses
- Original PR by @Hinotoi-agent (note: fix commit had a display artifact — `sk-` prefix was visually rendered as `***` in terminal output but the actual bytes were correct and the token was recognized by the redaction engine)
- 1022 tests total (up from 1020)

## [v0.50.25] Multi-PR batch: mobile scroll, import timestamps, profile security, mic fallback

### fix: restore mobile chat scrolling and drawer close (#397)
- `static/style.css`: `min-height:0` on `.layout` and `.main` (flex shrink chain fix); `-webkit-overflow-scrolling:touch`, `touch-action:pan-y`, `overscroll-behavior-y:contain` on `.messages`
- `static/boot.js`: call `closeMobileSidebar()` on new-conversation button and Ctrl+K shortcut so the transcript is visible immediately after starting a chat
- `tests/test_mobile_layout.py`: 41 new lines covering CSS fixes and both JS call sites
- Original PR by @Jordan-SkyLF

### fix: preserve imported session timestamps (#395)
- `api/models.py`: `Session.save(touch_updated_at=True)` — new flag; `import_cli_session()` accepts `created_at`/`updated_at` kwargs and saves with `touch_updated_at=False`
- `api/routes.py`: extract `created_at`/`updated_at` from `get_cli_sessions()` metadata and forward to import; post-import save also uses `touch_updated_at=False`
- `tests/test_gateway_sync.py`: +53 lines — integration test verifying imported session keeps original timestamp and sorts correctly; also fix session file cleanup in test finally block
- Original PR by @Jordan-SkyLF

### fix(profiles): block path traversal in profile switch and delete flows (#399) [SECURITY]
- `api/profiles.py`: new `_resolve_named_profile_home(name)` — validates name via `^[a-z0-9][a-z0-9_-]{0,63}$` regex then enforces path containment via `candidate.resolve().relative_to(profiles_root)`; use in `switch_profile()`
- `api/profiles.py`: add `_validate_profile_name()` call to `delete_profile_api()` entry
- `api/routes.py`: add `_validate_profile_name()` at HTTP handler level for both `/api/profile/switch` and `/api/profile/delete`
- `tests/test_profile_path_security.py`: 3 new tests — traversal rejected, valid name passes (cherry-picked from @Hinotoi-agent's PR, which was 62 commits behind master)

### feat: add desktop microphone transcription fallback (#396)
- `static/boot.js`: detect `_canRecordAudio`; keep mic button enabled when MediaRecorder available even without SpeechRecognition; full MediaRecorder recording → `/api/transcribe` fallback path with proper cleanup and error handling
- `api/upload.py`: add `transcribe_audio()` helper — temp file, calls transcription_tools, always cleans up
- `api/routes.py`: add `/api/transcribe` POST handler — CSRF-protected, auth-gated, 20MB limit
- `api/helpers.py`: change `Permissions-Policy` `microphone=()` → `microphone=(self)` (required for getUserMedia)
- `tests/test_voice_transcribe_endpoint.py`: 87 new lines (3 tests with mocked transcription)
- `tests/test_sprint19.py`: regression guard for microphone Permissions-Policy
- `tests/test_sprint20.py`: 3 updated tests for new fallback capability checks
- Original PR by @Jordan-SkyLF

- 1020 tests total (up from 1003)

## [v0.50.24] feat: opt-in chat bubble layout (closes #336)

- `api/config.py`: Add `bubble_layout` bool to `_SETTINGS_DEFAULTS` (default `False`) and `_SETTINGS_BOOL_KEYS` — new setting is opt-in, server-persisted, and coerced to bool on save
- `static/style.css`: 11 lines of CSS-only bubble layout — user rows `align-self:flex-end` / max-width 75%, assistant rows `flex-start`, all gated on `body.bubble-layout` class so the default full-width canvas is untouched; 700px responsive rule widens to 92%
- `static/boot.js`: Apply `body.bubble-layout` class from settings on page load; explicitly remove the class in the catch path so the feature stays off on API failure
- `static/panels.js`: Load checkbox state in `loadSettingsPanel`; write `body.bubble_layout` in `saveSettings` and immediately toggle `body.bubble-layout` class for live preview without a page reload
- `static/index.html`: Checkbox in the Appearance settings group, positioned between Show token usage and Show agent sessions
- `static/i18n.js`: English label + description keys; Spanish translations included in the same PR
- `tests/test_issue336.py`: 22 new tests covering config registration, JS class management in boot and panels, CSS selectors, HTML structure, i18n coverage for en+es, and API round-trip (default false, persist true/false, bool coercion)
- 1003 tests total (up from 981)

## [v0.50.23] Add OpenCode Zen and Go provider support (fixes #362)

- `api/config.py`: Add `opencode-zen` and `opencode-go` to `_PROVIDER_DISPLAY` — providers now show human-readable names in the UI instead of raw IDs
- `api/config.py`: Add full model catalogs for both providers to `_PROVIDER_MODELS` — Zen (pay-as-you-go credits, 32 models) and Go (flat-rate $10/month, 7 models) now show the correct model list in the dropdown instead of falling through to the unknown-provider fallback
- `api/config.py`: Add `OPENCODE_ZEN_API_KEY` / `OPENCODE_GO_API_KEY` to the env-var fallback detection path — providers are correctly detected as authenticated when keys are set in `.env`
- `tests/test_opencode_providers.py`: 6 new tests covering display registration, model catalog registration, and env-var detection for both providers
- 985 tests total (up from 979)

## [v0.50.22] Onboarding unblocked for reverse proxy / SSH tunnel deployments (fixes #390)

- `api/routes.py`: Onboarding setup endpoint now reads `X-Forwarded-For` and `X-Real-IP` headers before falling back to raw socket IP — reverse proxy (nginx/Caddy/Traefik) and SSH tunnel users are no longer incorrectly blocked
- Added `HERMES_WEBUI_ONBOARDING_OPEN=1` env var escape hatch for operators on remote servers who control network access themselves
- Error message now includes the env var hint so users know how to unblock themselves
- 18 new tests covering all IP resolution paths (`TestOnboardingIPLogic`, `TestOnboardingSetupEndpoint`)

> Living document. Updated at the end of every sprint.
> Repository: https://github.com/nesquena/hermes-webui

---

## [v0.50.21] Live reasoning, tool progress, and in-flight session recovery (PR #367)

- **Durable inflight reload recovery** (`static/ui.js`, `static/messages.js`): `saveInflightState` / `loadInflightState` / `clearInflightState` backed by `localStorage` (`hermes-webui-inflight-state` key, per-session, 10-minute TTL). Snapshots are saved on every token, tool event, and tool completion, and cleared when the run ends/errors/cancels. On a full page reload with an active stream, `loadSession()` hydrates from the snapshot before calling `attachLiveStream(..., {reconnecting:true})` — partial messages, live tool cards, and reasoning text all survive the reload.
- **Live reasoning cards during streaming** (`static/ui.js`, `static/messages.js`): The generic thinking spinner now upgrades to a live reasoning card when the backend streams reasoning text. `_thinkingMarkup(text)` and `updateThinking(text)` centralize the markup so the spinner and card share the same DOM slot. Works with models that emit reasoning via the agent's `reasoning_callback` or `tool_progress_callback`.
- **`tool_complete` SSE events** (`api/streaming.py`, `static/messages.js`): Tool progress callback now accepts the current agent signature `on_tool(*cb_args, **cb_kwargs)` — handles both the old 3-arg `(name, preview, args)` form and the new 4-arg `(event_type, name, preview, args)` form. `tool.completed` events transition live tool cards from running to done cleanly.
- **In-flight session state stable across switches** (`static/messages.js`, `static/sessions.js`): `attachLiveStream` refactored out of `send()` into a standalone function; partial assistant text mirrored into `INFLIGHT` state on every token; `data-live-assistant` DOM anchor preserved across `renderMessages()` calls so switching away and back doesn't lose or duplicate live output.
- **Reload recovery** (`api/models.py`, `api/routes.py`, `api/streaming.py`, `static/sessions.js`): `active_stream_id`, `pending_user_message`, `pending_attachments`, and `pending_started_at` now persisted on the session object before streaming starts and cleared on completion (or exception). `/api/session` returns these fields. After a page reload or session switch, `loadSession()` detects `active_stream_id` and calls `attachLiveStream(..., {reconnecting:true})` to reattach to the live SSE stream.
- **Session-scoped message queue** (`static/ui.js`, `static/messages.js`): Global `MSG_QUEUE` replaced with `SESSION_QUEUES` keyed by session ID. Queued follow-up messages are associated with the session they were typed in and only drained when that session becomes idle — no cross-session bleed.
- **`newSession()` idle reset** (`static/sessions.js`): Sets `S.busy=false`, `S.activeStreamId=null`, clears the cancel button, resets composer status — ensures a fresh chat is immediately usable even if another session's stream is still running.
- **Todos survive session reload** (`static/panels.js`): `loadTodos()` now reads from `S.session.messages` (raw, includes tool-role messages) rather than `S.messages` (filtered display), so todo state reconstructed from tool outputs survives reloads.
  - 12 new regression tests in `tests/test_regressions.py`; 961 tests total (up from 949)

## [v0.50.20] Silent error fix, stale model cleanup, live model fetching (fixes #373, #374, #375)

### Fix: Chat no longer silently swallows agent failures (fixes #373)

- **`api/streaming.py`**: After `run_conversation()` completes, the server now checks whether the agent produced any assistant reply. If not (e.g., auth error swallowed internally, model unavailable, network timeout), it emits an `apperror` SSE event with a clear message and type (`auth_mismatch` or `no_response`) instead of silently emitting `done`. A `_token_sent` flag tracks whether any streaming tokens were sent.
- **`static/messages.js`**: The `done` handler has a belt-and-suspenders guard — if `done` arrives but no assistant message exists in the session (the `apperror` path should usually catch this first), an inline "**No response received.**" message is shown. The `apperror` handler now also recognises the new `no_response` type with a distinct label.

### Cleanup: Remove stale OpenAI models from default list (fixes #374)

- **`api/config.py`**: `gpt-4o` and `o3` removed from `_FALLBACK_MODELS` and `_PROVIDER_MODELS["openai"]`. Both are superseded by newer models already in the list (`gpt-5.4-mini` for general use, `o4-mini` for reasoning). The Copilot provider list retains `gpt-4o` as it remains available via the Copilot API.

### Feature: Live model fetching from provider API (closes #375)

- **`api/routes.py`**: New `/api/models/live?provider=openai` endpoint. Fetches the actual model list from the provider's `/v1/models` API using the user's configured credentials. Includes URL scheme validation (B310), SSRF guard (private IP block), and graceful `not_supported` response for providers without a standard `/v1/models` endpoint (Anthropic, Google). Response normalised to `{id, label}` list, filtered to chat models.
- **`static/ui.js`**: `populateModelDropdown()` now calls `_fetchLiveModels()` in the background after rendering the static list. Live models that aren't already in the dropdown are appended to the provider's optgroup. Results are cached per session so only one fetch per provider per page load. Skips Anthropic and Google (unsupported). Falls back to static list silently if the fetch fails.
  - 25 new tests in `tests/test_issues_373_374_375.py`; 949 tests total (up from 924)


## [v0.50.19] Fix UnicodeEncodeError when downloading files with non-ASCII filenames (PR #378)

- **Workspace file downloads no longer crash for Unicode filenames** (`api/routes.py`): Clicking a PDF or other file with Chinese, Japanese, Arabic, or other non-ASCII characters in its name caused a `UnicodeEncodeError` because Python's HTTP server requires header values to be latin-1 encodable. A new `_content_disposition_value(disposition, filename)` helper centralises `Content-Disposition` generation: it strips CR/LF (injection guard), builds an ASCII fallback for the legacy `filename=` parameter (non-ASCII chars replaced with `_`), and preserves the full UTF-8 name in `filename*=UTF-8''...` per RFC 5987. Both `attachment` and `inline` responses use it.
  - 2 new integration tests in `tests/test_sprint29.py` covering Chinese filenames for both download and inline responses, verifying the header is latin-1 encodable and `filename*=UTF-8''` is present; 924 tests total (up from 922)

## [v0.50.18] Recover from invalid default workspace paths (PR #366)

- **WebUI no longer breaks when the configured default workspace is unavailable** (`api/config.py`): The workspace resolution path was refactored into three composable functions — `_workspace_candidates()`, `_ensure_workspace_dir()`, and `resolve_default_workspace()`. When the configured workspace (from env var, settings file, or passed path) cannot be created or accessed, the server falls back through an ordered priority list: `HERMES_WEBUI_DEFAULT_WORKSPACE` env var → `~/workspace` (if exists) → `~/work` (if exists) → `~/workspace` (create it) → `STATE_DIR/workspace`.
- **`save_settings()` now validates and corrects the workspace path** (`api/config.py`): If a client posts an invalid or inaccessible `default_workspace`, the saved value is corrected to the nearest valid fallback rather than persisting an unusable path.
- **Startup normalizes stale workspace paths** (`api/config.py`): If the settings file stores a workspace that no longer exists, the server rewrites it with the resolved fallback on startup so the problem self-heals.
  - 7 tests in `tests/test_default_workspace_fallback.py` (2 from PR + 5 added during review: fallback creation, RuntimeError on all-fail, deduplication, env var priority, unwritable path returns False); 922 tests total (up from 915)

## [v0.50.17] Docker: pre-install uv at build time + fix workspace permissions (fixes #357)

- **Docker containers no longer need internet access at startup** (`Dockerfile`): `uv` is now installed at image build time via `RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh` (run as root, so `uv` lands in `/usr/local/bin` — accessible to all users). The init script skips the download if `uv` is already on PATH (`command -v uv`), and falls back to downloading with a proper `error_exit` if it isn't. This fixes startup failures in air-gapped, firewalled, or isolated Docker networks where `github.com` is unreachable at runtime.
  - **Fix applied during review**: the original PR installed `uv` as the `hermeswebuitoo` user (to `~hermeswebuitoo/.local/bin`), which is not on the `hermeswebui` runtime user's `PATH`. Changed to install as `root` with `UV_INSTALL_DIR=/usr/local/bin` so `uv` is in the system PATH for all users.
- **Workspace directory now writable by the hermeswebui user** (`docker_init.bash`): The init script now uses `sudo mkdir -p` and `sudo chown hermeswebui:hermeswebui` for `HERMES_WEBUI_DEFAULT_WORKSPACE`. Docker auto-creates bind-mount directories as `root` if they don't exist on the host, making them unwritable by the app user. The `sudo chown` corrects ownership after creation.
  - 15 new structural tests in `tests/test_issue357.py`; 915 tests total (up from 900)

## [v0.50.16] Fix CSRF check failing behind reverse proxy on non-standard ports (PR #360)

- **CSRF no longer rejects POST requests from reverse-proxied deployments on non-standard ports** (`api/routes.py`, fixes #355): When serving behind Nginx Proxy Manager or similar on a port like `:8000`, browsers send `Origin: https://app.example.com:8000` while the proxy forwards `Host: app.example.com` (port stripped). The old string comparison failed this as cross-origin. Two changes fix it:
  - `_normalize_host_port()`: properly splits host:port strings including IPv6 bracket notation (`[::1]:8080`)
  - `_ports_match(scheme, origin_port, allowed_port)`: scheme-aware port equivalence — absent port equals `:80` for `http://` and `:443` for `https://`. This prevents the previous cross-protocol confusion where `http://host` could incorrectly match an `https://host:443` server (security fix applied on top of the original PR)
  - `HERMES_WEBUI_ALLOWED_ORIGINS` env var: comma-separated explicit origin allowlist for cases where port normalization alone isn't sufficient (e.g. non-standard ports like `:8000` where the proxy strips the port entirely). Entries without a scheme (`https://`) are rejected with a startup warning.
- **Security fix applied during review**: the original `_ports_match` treated both port 80 and port 443 as interchangeable with "absent port", which is scheme-unaware. An `http://host` origin would pass for an `https://host:443` server. Fixed by making the default-port lookup scheme-specific.
  - 29 new tests in `tests/test_sprint29.py` (5 from PR + 24 added during review): cover scheme-aware port matching, cross-protocol rejection, unit tests for `_normalize_host_port` and `_ports_match`, allowlist validation, comma-separated origins, no-scheme allowlist warning, the bug scenario with and without the allowlist; 900 tests total (up from 871)

## [v0.50.15] KaTeX math rendering for LaTeX in chat and workspace previews (fixes #347)

- **LaTeX / KaTeX math now renders in chat messages and workspace file previews** (`static/ui.js`, `static/workspace.js`, `static/style.css`, `static/index.html`): Inline math (`$...$`, `\(...\)`) and display math (`$$...$$`, `\[...\]`) are rendered via KaTeX instead of displaying as raw text. Follows the existing mermaid lazy-load pattern: delimiters are stashed before markdown processing, placeholder elements are emitted, and KaTeX JS is loaded from CDN on first use — no KaTeX JS is loaded unless math is present.
  - `$$...$$` and `\[...\]` → centered display math (`<div class="katex-block">`)
  - `$...$` and `\(...\)` → inline math (`<span class="katex-inline">`); requires non-space at `$` boundaries to avoid false positives on currency amounts like `$5`
  - KaTeX JS lazy-loaded from jsdelivr CDN with SRI hash; KaTeX CSS loaded eagerly in `<head>` to prevent layout shift
  - `throwOnError:false` — invalid LaTeX degrades to a `<code>` span rather than crashing the message
  - `trust:false` — disables KaTeX commands that could execute code
  - `<span>` added to `SAFE_TAGS` allowlist for inline math spans (tag name boundary check preserved)
- **Fix: fence stash now runs before math stash** (`static/ui.js`): The original PR had math stash before fence stash, meaning `\`$x$\`` inside backtick code spans was incorrectly extracted as math instead of being protected as code. Order corrected — fence_stash runs first so code spans protect their contents.
- **Workspace file previews now render math** (`static/workspace.js`): Added `requestAnimationFrame(renderKatexBlocks)` after markdown file preview renders, matching the chat message path. Without this, math placeholders appeared in previews but were never rendered.
  - 29 tests in `tests/test_issue347.py` (18 original + 11 new covering stash ordering, workspace wiring, false-positive prevention); 870 tests total (up from 841)

## [v0.50.14] Security fixes: B310 urlopen scheme validation, B324 MD5 usedforsecurity, B110 bare except logging + QuietHTTPServer (PR #354)

- **B324 — MD5 no longer triggers crypto warnings** (`api/gateway_watcher.py`): `_snapshot_hash` uses MD5 only as a non-cryptographic change-detection hash. Added `usedforsecurity=False` so systems with strict crypto policies (FIPS mode etc.) don't reject the call.
- **B310 — urlopen now validates URL scheme** (`api/config.py`, `bootstrap.py`): Both `get_available_models()` and `wait_for_health()` validate that the URL scheme is `http` or `https` before calling `urllib.request.urlopen`, preventing `file://` or other dangerous scheme injection. Added `# nosec B310` suppression after each validated call.
- **B110 — bare `except: pass` blocks replaced with `logger.debug()`** (12 files): All `except Exception: pass` and `except: pass` blocks now log the failure at DEBUG level so operators can diagnose issues in production without changing behavior. A module-level `logger = logging.getLogger(__name__)` was added to each file.
- **`QuietHTTPServer`** (`server.py`): Subclass of `ThreadingHTTPServer` that overrides `handle_error()` to silently drop `ConnectionResetError`, `BrokenPipeError`, `ConnectionAbortedError`, and socket errno 32/54/104 (client disconnect races). Real errors still delegate to the default handler. Reduces log spam from SSE clients that disconnect mid-stream.
- **Session title redaction** (`api/routes.py`): The `/api/sessions` list endpoint now applies `_redact_text` to session titles before returning them, consistent with the per-session `redact_session_data()` already applied elsewhere.
- **Fix**: `QuietHTTPServer.handle_error` uses `sys.exc_info()` (standard library) not `traceback.sys.exc_info()` (implementation detail); `sys` is now explicitly imported in `server.py`.
  - 19 new tests in `tests/test_sprint43.py`; 841 tests total (up from 822)

## [v0.50.13] Fix session_search in WebUI sessions — inject SessionDB into AIAgent (PR #356)

- **`session_search` now works in WebUI sessions** (`api/streaming.py`): The agent's `session_search` tool returned "Session database not available" for all WebUI sessions. The CLI and gateway code paths both initialize a `SessionDB` instance and pass it via `session_db=` to `AIAgent.__init__()`, but the WebUI streaming path was missing this step. `_run_agent_streaming` now initializes `SessionDB()` before constructing the agent and passes it in. A `try/except` wrapper makes the init non-fatal — if `hermes_state` is unavailable (older installs, test environments), a `WARNING` is printed and `session_db=None` is passed instead, preserving the prior behavior gracefully.
  - 7 new tests in `tests/test_sprint42.py`; 822 tests total (up from 815)

## [v0.50.12] Profile .env isolation — prevent API key leakage on profile switch (fixes #351)

- **API keys no longer leak between profiles on switch** (`api/profiles.py`): `_reload_dotenv()` now tracks which env vars were loaded from the active profile's `.env` and clears them before loading the next profile. Previously, switching from a profile with `OPENAI_API_KEY=X` to a profile without that key left `X` in `os.environ` for the duration of the process — effectively leaking credentials across the profile boundary. A module-level `_loaded_profile_env_keys: set[str]` tracks loaded keys; it is cleared and repopulated on every `_reload_dotenv()` call.
- **`apply_onboarding_setup()` ordering fixed** (`api/onboarding.py`): the belt-and-braces `os.environ[key] = api_key` direct assignment is now placed **after** `_reload_dotenv()`. Previously the key was wiped by the isolation cleanup when `_reload_dotenv()` ran immediately after the direct set.
  - 2 new tests in `tests/test_profile_env_isolation.py`; 815 tests total (up from 813)

## [v0.50.11] Chat table styles + plain URL auto-linking (fixes #341, #342)

- **Tables in chat messages now render with visible borders** (`static/style.css`): The `.msg-body` area had no table CSS, so markdown tables sent by the assistant were unstyled and unreadable. Four new rules mirror the existing `.preview-md` table styles: `border-collapse:collapse`, per-cell padding and borders via `var(--border2)`, and an alternating-row tint. Two `:root[data-theme="light"]` overrides ensure the borders and header background adapt correctly in light mode. (fixes #341)
- **Plain URLs in chat messages are now clickable** (`static/ui.js`): Bare URLs like `https://example.com` were rendered as plain text. A new autolink pass in `renderMd()` converts `https?://...` URLs to `<a>` tags automatically. Runs after the SAFE_TAGS escape pass (protecting code blocks), before paragraph wrapping. Also applied inside `inlineMd()` so URLs in list items, blockquotes, and table cells are linked too. Trailing punctuation stripped; `esc()` applied to both href and link text. (fixes #342)
  - 11 new tests (4 in `tests/test_issue341.py`, 7 in `tests/test_issue342.py`); 813 tests total (up from 802)
- **Test infrastructure fix** (`tests/test_sprint34.py` #349): two static-file opens used bare relative paths that failed when pytest ran from outside the repo root; replaced with `pathlib.Path(__file__).parent.parent` consistent with the rest of the suite. 813/813 now pass from any working directory.

## [v0.50.10] Title auto-generation fix + mobile close button (PR #333)

- **Session title now auto-generates for all default title values** (`'Untitled'`, `'New Chat'`, empty string): The condition in `api/streaming.py` that triggers `title_from()` previously only matched `'Untitled'`. It now also covers `'New Chat'` (used by some external clients/forks) and any empty/falsy title, so sessions started from those states get a proper auto-generated title after the first message.
- **Redundant workspace panel close button hidden on mobile** (`static/style.css`): On viewports ≤900px wide, both the desktop collapse button (`#btnCollapseWorkspacePanel`) and the mobile-specific X button (`.mobile-close-btn`) were rendered simultaneously. The desktop button is now hidden on mobile and `.mobile-close-btn` is hidden by default (desktop) and shown only on mobile — eliminating the duplicate control.
  - 11 new tests in `tests/test_sprint41.py`; 802 tests total (up from 791)

## [v0.50.9] Onboarding works from Docker bridge networks (PR #335, fixes #334)

- **Docker users can now complete onboarding without enabling auth first** (closes #334): The onboarding setup endpoint previously only accepted requests from `127.0.0.1`. Docker containers connect via bridge network IPs (`172.17.x.x`, etc.), so the endpoint returned a 403 mid-wizard with no clear explanation. The check now accepts any loopback or RFC-1918 private address (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) using Python's `ipaddress.is_loopback` and `is_private`. Public IPs are still blocked unless auth is enabled.

## [v0.50.8] Model dropdown deduplication — hyphen vs dot separator fix (PR #332)

- **Model dropdown no longer shows duplicates for hyphen-format configs** (e.g. `claude-sonnet-4-6` from hermes-agent config): The server-side normalization in `api/config.py` now unifies hyphens and dots when checking whether the default model is already in the dropdown. Previously, `claude-sonnet-4-6` (hermes-agent format) and `claude-sonnet-4.6` (WebUI list format) were treated as different models, causing the same model to appear twice — once as a raw unlabelled entry and once with the correct display name. The raw entry is now suppressed and the labelled one is selected as default.
- **README updated**: test count corrected to 791 / 51 files; all module line counts updated to current values; `onboarding.py`, `state_sync.py`, `updates.py` added to the architecture listing.

## [v0.50.7] OAuth provider onboarding path — Codex/Copilot no longer blocks setup (PR #331, fixes #329 bug 2)

- **OAuth providers now have a proper onboarding path** (closes bug 2): Users with `openai-codex`, `copilot`, `qwen-oauth`, or any other OAuth-authenticated provider now see a clear confirmation card instead of an unusable API key input form.
  - If already authenticated (`chat_ready: true`): blue "Provider already authenticated" card with a direct Continue button — no key entry required.
  - If not yet authenticated: amber card explaining how to run `hermes auth` or `hermes model` in a terminal to complete setup.
  - Either state includes a collapsible "switch provider" section for users who want to move to an API-key provider instead.
  - `_build_setup_catalog` now includes `current_is_oauth` boolean; fixed a latent `KeyError` crash when looking up `default_model` for OAuth providers.
  - 5 new i18n keys in English and Spanish (`onboarding_oauth_*`).
  - 15 new tests in `tests/test_sprint40.py`; 791 tests total (up from 776)

## [v0.50.6] Skip-onboarding env var + synchronous API key reload (PR #330, fixes #329 bugs 1+3)

- **`HERMES_WEBUI_SKIP_ONBOARDING=1`** (closes bug 1): Hosting providers can set this env var to bypass the first-run wizard entirely. Only takes effect when `chat_ready` is also true — a misconfigured deployment still shows the wizard. Accepts `1`, `true`, or `yes`.
- **API key takes effect immediately after onboarding** (closes bug 3): `apply_onboarding_setup` now sets `os.environ[env_var]` synchronously after writing the key to `.env`, so the running process can use it without a server restart. Also attempts to reload `hermes_cli`'s config cache as a belt-and-suspenders measure.
  - 8 new tests in `tests/test_sprint39.py`; 776 tests total (up from 768)

## [v0.50.5] Think-tag stripping with leading whitespace (PR #327)

- **Fix think-tag rendering for models that emit leading whitespace** (e.g. MiniMax M2.7): Some models emit one or more newlines before the `<think>` opening tag. The previous regex used a `^` anchor, so it only matched when `<think>` was the very first character. When the anchor failed, the raw `</think>` tag appeared in the rendered message body.
  - `static/ui.js` (stored messages): removed `^` anchor from `<think>` and Gemma channel-token regexes; switched from `.slice()` to `.replace()` + `.trimStart()` so stripping works regardless of position
  - `static/messages.js` (live stream): `trimStart()` before `startsWith`/`indexOf` checks; partial-tag-prefix guard also uses trimmed buffer
  - 10 new tests in `tests/test_sprint38.py`; 768 tests total (up from 758)

## [v0.50.3] Onboarding completes gracefully for pre-configured providers (PR #323, fixes #322)

- **OAuth/CLI-configured providers no longer blocked by onboarding** (closes #322): Users with providers already set up via the CLI (`openai-codex`, `copilot`, `nous`, etc.) hit `Unsupported provider for WebUI onboarding` when clicking "Open Hermes" on the finish page. The wizard now marks onboarding complete and lets them through — the agent setup is already done, no wizard steps needed.
  - 5 new tests in `tests/test_sprint34.py`; 758 tests total (up from 753)

## [v0.50.2] Workspace panel state persists across refreshes

- **Workspace panel open/closed persists** (localStorage key `hermes-webui-workspace-panel`): Once you open the workspace/files pane, it stays open after a page refresh. Closing it explicitly saves the closed state, which also survives a refresh. The restore happens in the boot sequence before the first render, so there is no flash of the wrong state. Works for both desktop and mobile.
  - State is stored as `'open'` or `'closed'` — `'open'` restores as `'browse'` mode; any preview state is re-evaluated normally.
  - 7 new tests in `tests/test_sprint37.py`; 753 tests total (up from 746)

## [v0.50.1] Mobile Enter key inserts newline (PR #315, fixes #269)

- **Enter inserts newline on mobile** (closes #269): On touch-primary devices (detected via `matchMedia('(pointer:coarse)')`), the Enter key now inserts a newline instead of sending. Users send via the Send button, which is always visible on mobile. Desktop behavior is unchanged — Enter sends, Shift+Enter inserts a newline.
  - The `ctrl+enter` setting continues to work as before on all devices.
  - Users who explicitly set send key to `enter` on mobile can override in Settings.
  - 4 new tests in `tests/test_mobile_layout.py`; 746 tests total (up from 742)

## [v0.50.0] Composer-centric UI refresh + Hermes Control Center (PR #242)

Major UI overhaul by **[@aronprins](https://github.com/aronprins)** — the biggest single contribution to the project. Rebased and reviewed on `pr-242-review`.

- **Composer as control hub** — model selector, profile chip, and workspace chip now live in the composer footer as pill buttons with dropdowns. The context window usage ring (token count, cost, fill) replaces the old linear pill.
- **Hermes Control Center** — a single sidebar launcher button (bottom of sidebar) replaces the gear icon settings modal. Tabbed 860px modal: Conversation tab (transcript/JSON export, import, clear), Preferences tab (all settings), System tab (version, password). Always resets to Conversation on close.
- **Activity bar removed** — turn-scoped status (thinking, cancelling) renders inline in the composer footer via `setComposerStatus`.
- **Session `⋯` dropdown** — per-row pin/archive/duplicate/move/delete actions move from inline buttons into a shared dropdown menu; click-outside/scroll/Escape handling.
- **Workspace panel state machine** — `_workspacePanelMode` (`closed`/`browse`/`preview`) in boot.js with proper transitions and discard-unsaved guard.
- **Icon additions** — save, chevron-right, arrow-right, pause, paperclip, copy, rotate-ccw, user added to icons.js.
- **i18n additions** — 6 new keys across en/de/zh/zh-Hant for control center sections.
- **OLED theme** — 7th built-in theme (true black background for OLED displays), originally contributed by **[@kevin-ho](https://github.com/kevin-ho)** in PR #168.
- **Mobile fixes** — icon-only composer chips below 640px, `overflow-y: hidden` on `.composer-left` to prevent scrollbar, profile dropdown `max-width: min(260px, calc(100vw - 32px))`.
- 742 tests total; all existing tests pass; version badge in System tab updated to v0.50.0.

## [v0.49.4] Cancel stream cleanup guaranteed (PR #309, fixes #299)

- **Reliable cancel cleanup** (closes #299): `cancelStream()` no longer depends on the SSE `cancel` event to clear busy state and status text. Previously, if the SSE connection was already closed when cancel fired, "Cancelling..." would linger indefinitely. Now `cancelStream()` clears `S.activeStreamId`, calls `setBusy(false)`, `setStatus('')`, and hides the cancel button directly after the cancel API request — regardless of SSE connection state. The SSE cancel handler still runs when the connection is alive (all operations are idempotent).
  - 9 new tests in `tests/test_sprint36.py`; 742 tests total (up from 733)

## [v0.49.3] Session title guard + breadcrumb nav + wider panel (PRs #301, #302)

- **Preserve user-renamed session titles** (PR #301 by **[@franksong2702](https://github.com/franksong2702)** / closes #300): `title_from()` now only runs when the session title is still `'Untitled'`. Previously it overwrote user-assigned titles on every conversation turn.
  - Fixed in both `api/streaming.py` (streaming path) and `api/routes.py` (sync path).
- **Clickable breadcrumb navigation** (PR #302 by **[@franksong2702](https://github.com/franksong2702)** / closes #292): Workspace file preview now shows a clickable breadcrumb path bar. Each segment navigates directly to that directory level. Paths with spaces and special characters handled correctly. `clearPreview()` restores the directory breadcrumb on close.
- **Wider right panel** (PR #302): `PANEL_MAX` raised from 500 to 1200 — right panel can now be dragged wider on ultrawide screens.
- **Responsive message width** (PR #302): `.messages-inner` now scales up gracefully at 1400px (1100px max) and 1800px (1200px max) viewport widths instead of capping at 800px on all screen sizes.
  - 12 new tests in `tests/test_sprint35.py`; 733 tests total (up from 721)

## [v0.49.2] OAuth provider support in onboarding (issues #303, #304)

- **OAuth provider bypass** (closes #303, #304): The first-run onboarding wizard now correctly recognizes OAuth-authenticated providers (GitHub Copilot, OpenAI Codex, Nous Portal, Qwen OAuth) as ready, instead of always demanding an API key.
  - New `_provider_oauth_authenticated()` helper in `api/onboarding.py` checks `hermes_cli.auth.get_auth_status()` first (authoritative), then falls back to parsing `~/.hermes/auth.json` directly for the known OAuth provider IDs (`openai-codex`, `copilot`, `copilot-acp`, `qwen-oauth`, `nous`).
  - `_status_from_runtime()` now has an `else` branch for providers not in `_SUPPORTED_PROVIDER_SETUPS`; OAuth-authenticated providers return `provider_ready=True` and `setup_state="ready"`.
  - The `provider_incomplete` status note no longer says "API key" for OAuth providers — it now says "Run 'hermes auth' or 'hermes model' in a terminal to complete setup."
  - 21 new tests in `tests/test_sprint34.py`; 721 tests total (up from 700)

## [v0.49.1] Docker docs + mobile Profiles button (PRs #291, #265)

- **Two-container Docker setup** (PR #291 / closes #288): New `docker-compose.two-container.yml` for running the Hermes Agent and WebUI as separate containers with shared volumes. Documents the architecture clearly; localhost-only port binding by default.
- **Mobile Profiles button** (PR #265 by **[@Bobby9228](https://github.com/Bobby9228)**): Adds Profiles to the mobile bottom navigation bar (last position: Chat → Tasks → Skills → Memory → Spaces → Profiles). Uses `mobileSwitchPanel()` for correct active-highlight behaviour; `data-panel="profiles"` attribute set; SVG matches other nav icons; 3 new tests.
  - 700 tests total (up from 697)

## [v0.49.0] First-run onboarding wizard + self-update hardening (PRs #285, #287, #289)

- **One-shot bootstrap and first-run setup wizard** (PR #285 — first-run onboarding flow): New users are greeted with a guided onboarding overlay on first load. The wizard checks system status, configures a provider (OpenRouter, Anthropic, OpenAI, or custom OpenAI-compatible endpoint), sets a workspace and optional password, and marks setup as complete — all without leaving the browser.
  - `bootstrap.py`: one-shot CLI bootstrap that writes `~/.hermes/config.yaml` and `~/.hermes/.env` from flags; idempotent and safe to re-run
  - `api/routes.py`: `/api/onboarding/status` (GET) and `/api/onboarding/complete` (POST) endpoints; real provider config persistence to `config.yaml` + `.env`
  - `static/onboarding.js`: full wizard JS module — step navigation, provider dropdown, model selector, API key input, Back/Continue flow, i18n support
  - `static/index.html`: onboarding overlay HTML shell + `<script src="/static/onboarding.js">` load
  - `static/i18n.js`: 40+ onboarding keys added to all 5 locales (en, es, de, zh-Hans, zh-Hant)
  - `static/boot.js`: on load, fetches `/api/onboarding/status` and opens wizard when `completed=false`
  - Wizard does NOT show when `onboarding_completed=true` in settings
  - 14 new tests in `tests/test_onboarding.py`; 693 tests total (up from 679)

- **Self-update git pull diagnostics** (PR #287): Fixes multiple failure modes in the WebUI self-update flow when the repo has a non-trivial git state.
  - `_run_git()` now returns stderr on failure (stdout fallback, then exit-code message) — users see actionable git errors instead of empty strings
  - New `_split_remote_ref()` helper splits `origin/master` into `('origin', 'master')` before `git pull --ff-only` — fixes silent failures where git misinterpreted the combined string as a repository name
  - `--untracked-files=no` added to `git status --porcelain` — prevents spurious stash failures in repos with untracked files
  - Early merge-conflict detection via porcelain status codes before attempting pull
  - 4 new unit tests in `tests/test_updates.py`

- **Skip flaky redaction test in agent-less environments** (PR #289): `test_api_sessions_list_redacts_titles` added to the CI skip list for environments without hermes-agent installed. Test still runs with the full agent; security coverage preserved by 6 pure-unit tests and 2 other API-level redaction tests.
  - 697 tests total (up from 693)

## [v0.48.2] Provider/model mismatch warning (PR #283, fixes #266)

- **Provider mismatch warning** (PR #283): WebUI now warns when you select a model from a provider different from the one Hermes is configured for, instead of silently failing with a 401 error.
  - `api/streaming.py`: 401/auth errors classified as `type='auth_mismatch'` with an actionable hint ("Run `hermes model` in your terminal to switch providers")
  - `static/ui.js`: `populateModelDropdown()` stores `active_provider` from `/api/models` as `window._activeProvider`; new `_checkProviderMismatch()` helper compares selected model's provider prefix against the configured provider
  - `static/boot.js`: `modelSelect.onchange` calls `_checkProviderMismatch()` and shows a toast warning immediately on selection
  - `static/messages.js`: `apperror` handler shows "Provider mismatch" label (via i18n) instead of "Error" for auth errors
  - `static/i18n.js`: `provider_mismatch_warning` and `provider_mismatch_label` keys added to all 5 locales (en, es, de, zh-Hans, zh-Hant)
  - Check skipped for `openrouter` and `custom` providers to avoid false positives
  - 21 new tests in `tests/test_provider_mismatch.py`; 679 tests total (up from 658)
## [v0.48.1] Markdown table inline formatting (PR #278)

- **Inline formatting in table cells** (PR #278, @nesquena): Table header and data cells now render `**bold**`, `*italic*`, `` `code` ``, and `[links](url)` correctly. Previously `esc()` was used, which displayed raw HTML tags as text. Changed to `inlineMd()` consistent with list items and blockquotes. XSS-safe: `inlineMd()` escapes all interpolated values. Two-line change in `static/ui.js`. Fixes #273.
## [v0.48.0] Real-time gateway session sync (PR #274)

- **Real-time gateway session sync** (PR #274, @bergeouss): Gateway sessions from Telegram, Discord, Slack, and other messaging platforms now appear in the WebUI sidebar and update in real time as new messages arrive. Enable via the "Show agent sessions" checkbox (renamed from "Show CLI sessions").
  - `api/gateway_watcher.py`: background daemon thread polling `state.db` every 5s using MD5 hash-based change detection
  - New SSE endpoint `/api/sessions/gateway/stream` for real-time push to browser
  - Dynamic source badges: telegram (blue), discord (purple), slack (dark purple), cli (green)
  - Zero changes to hermes-agent — WebUI reads the shared `state.db` that both components access
  - 10 new tests in `test_gateway_sync.py` covering metadata, filtering, SSE, and watcher lifecycle
  - 658 tests (up from 648)
## [v0.47.1] Spanish locale (PR #275)

- **Spanish (es) locale** (PR #275, @gabogabucho): Full Spanish translation for all 175 UI strings. Exposed automatically in the language selector via existing `LOCALES` wiring. Includes regression tests verifying locale presence, representative translations, and key-parity with English. 648 tests (up from 645).
## [v0.47.0] — 2026-04-11

### Features
- **`/skills [query]` slash command** (PR #257): Fetches from `/api/skills`, groups results by category (alphabetically), renders as a formatted assistant message. Optional query filters by name, description, or category. Shows in the `/` autocomplete dropdown. i18n for en/de/zh/zh-Hant. 1 regression test added.
- **Shared app dialogs replace native `confirm()`/`prompt()`** (PR #251, extracted from #242 by @aronprins): `showConfirmDialog()` and `showPromptDialog()` in `ui.js`, backed by `#appDialogOverlay`. Replaces all 11 native browser dialog call sites across panels.js, sessions.js, ui.js, workspace.js. Full keyboard focus trap (Tab/Escape/Enter), ARIA roles, danger mode, focus restore, mobile-responsive buttons. i18n for en/de/zh/zh-Hant. 5 new tests in `test_sprint33.py`.
- **Session `⋯` action dropdown** (PR #252, extracted from #242 by @aronprins): Replaces 5 per-row hover buttons (pin/move/archive/duplicate/delete) with a single `⋯` trigger. Menu uses `position:fixed` to avoid sidebar clipping. Full close handling: click-outside, scroll, Escape, resize-reposition. `test_sprint16.py` updated to assert the new trigger exists and old button classes are gone.

### Bug Fixes
- **Custom provider with slash model name no longer rerouted to OpenRouter** (PR #255): `resolve_model_provider()` now returns immediately with the configured `provider`/`base_url` when `base_url` is set, before the slash-based OpenRouter heuristic runs. Fixes `google/gemma-4-26b-a4b` with `provider: custom` being silently routed to OpenRouter (401 errors). 1 regression test added. Fixes #230.
- **Android Chrome: workspace panel now closeable on mobile** (PR #256): `toggleMobileFiles()` now shows/hides the mobile overlay. New `closeMobileFiles()` helper closes the right panel with correct overlay tracking. Overlay tap-to-close calls both `closeMobileSidebar()` and `closeMobileFiles()`. Mobile-only `×` close button added to workspace panel header. Fix applied during review: `closeMobileSidebar()` now checks if the right panel is still open before hiding the overlay. Fixes #247.
- **Android Chrome: profile dropdown no longer clipped on mobile** (PR #256): `.profile-dropdown` switches to `position:fixed; top:56px; right:8px` at `max-width:900px`, escaping the `overflow-x:auto` stacking context that was making it invisible. Fixes #246.

### Tests
- **Mobile layout regression suite** (PR #254): 14 static tests in `tests/test_mobile_layout.py` that run on every QA pass. Covers: CSS breakpoints at 900px/640px, right panel slide-over, mobile overlay, bottom nav, files button, profile dropdown z-index, chip overflow, workspace close, `100dvh`, 44px touch targets, 16px textarea font. All pass against current and future master.

**CSS hotfix (commit a2ae953, post-tag):** session action menu — icon now displays inline-left of text. The `.ws-opt` base class (`flex-direction:column`) was causing SVG icons to stack above the label. Fixed with 3 CSS rule overrides on `.session-action-opt`.

**645 tests (up from 624 on v0.46.0 — +21 new tests)**

---

## [v0.46.0] — 2026-04-11

### Features
- **Docker UID/GID matching** (PR #237 by @mmartial): New `docker_init.bash` entrypoint adds `hermeswebui`/`hermeswebuitoo` user pattern so container-created files match the host user UID/GID. Prevents `.hermes` volume mounts from being owned by root. Configure via `WANTED_UID` and `WANTED_GID` env vars (default 1000/1000). README updated with setup instructions.
  - `Dockerfile` — two-user pattern with passwordless sudo; `/.within_container` marker for in-container detection; starts as `hermeswebuitoo`, switches to correct UID/GID
  - `docker-compose.yml` — mounts `.hermes` at `/home/hermeswebui/.hermes`; uses `${UID:-1000}/${GID:-1000}` for UID/GID passthrough
  - `server.py` — detects `/.within_container` and prints a note when binding to 0.0.0.0

### Security
- **Credential redaction in API responses** (PR #243 by @kcclaw001): All API endpoints now redact credentials from responses at the response layer. Session files on disk are unchanged; only the API output is masked.
  - `api/helpers.py` — `redact_session_data()` and `_redact_value()` apply pattern-based redaction to messages, tool_calls, and title; covers GitHub PATs, OpenAI/Anthropic keys, AWS keys, Slack tokens, HuggingFace tokens, Authorization Bearer headers, and PEM private key blocks
  - `api/routes.py` — `GET /api/session`, `GET /api/session/export`, `GET /api/memory` all wrapped with redaction
  - `api/streaming.py` — SSE `done` event payload redacted before broadcast
  - `api/startup.py` — new `fix_credential_permissions()` called at startup; `chmod 600` on `.env`, `google_token.json`, `auth.json`, `.signing_key` if they have group/other read bits set
  - `tests/test_security_redaction.py` — 13 new tests covering redaction functions and endpoint structural verification

### Bug Fixes
- **Custom model list discovery with config API key** (PR #238 by @ccqqlo): `get_available_models()` now reads `api_key` from `config.yaml` before env vars when fetching `/v1/models` from custom endpoints (LM Studio, Ollama, etc.). Priority: `model.api_key` → `providers.<active>.api_key` → `providers.custom.api_key` → env vars. Also adds `OpenAI/Python 1.0` User-Agent header. Fixes model picker collapsing to single default model for config-only setups. 1 new regression test.
- **HTML entity decode before markdown processing** (PR #239 by @Argonaut790): Adds `decode()` helper in `renderMd()` to fix double-escaping of HTML entities from LLM output (e.g. `&lt;code&gt;` becoming `&amp;lt;code&amp;gt;` instead of rendering). XSS-safe: decode runs before `esc()`, only 5 entity patterns (`&lt;`, `&gt;`, `&amp;`, `&quot;`, `&#39;`).
- **Simplified Chinese translations completed** (PR #239 by @Argonaut790): 40+ missing keys added to `zh` locale (123 → 164 keys). New `zh-Hant` (Traditional Chinese) locale with 163 keys.
- **Cancel button now interrupts agent execution** (PR #244 by @huangzt): `cancel_stream()` now calls `agent.interrupt()` to stop backend tool execution, not just the SSE stream. `AGENT_INSTANCES` dict (protected by `STREAMS_LOCK`) tracks active agents. Race condition fixed: after storing agent, immediately checks if cancel was already requested. Frontend: removes stale "Cancelling..." status text; `setBusy(false)` always called on cancel. 6 new unit tests in `tests/test_cancel_interrupt.py`.

**624 tests (up from 604 on v0.45.0 — +20 new tests)**

---

## [v0.45.0] — 2026-04-10

### Features
- **Custom endpoint fields in new profile form** (PR #233, fixes #170): The New Profile form now accepts optional Base URL and API key fields. When provided, both are written into the new profile's `config.yaml` under the `model` section, enabling local-endpoint setups (Ollama, LMStudio, etc.) to be configured in one step without editing YAML manually. The write is a no-op when both fields are left blank, so existing profile creation behavior is unchanged.
  - `api/profiles.py` — `_write_endpoint_to_config()` merges `base_url`/`api_key` into `config.yaml` using `yaml.safe_load` + `yaml.dump`, preserving any existing keys
  - `api/routes.py` — accepts `base_url` and `api_key` from POST body; validates that `base_url`, if provided, starts with `http://` or `https://` (returns 400 for invalid schemes)
  - `static/index.html` — two new inputs added to the New Profile form: Base URL (with `http://localhost:11434` placeholder) and API key (password type)
  - `static/panels.js` — `submitProfileCreate()` reads both fields, validates URL format client-side before sending, and includes them in the create payload; `toggleProfileForm()` clears them on cancel
  - 9 tests in `tests/test_sprint31.py` covering: config write (base_url, api_key, both, merge, no-op), route acceptance, profile path in response, and invalid-scheme rejection

**604 tests (up from 595)**

## [v0.44.1] — 2026-04-10

- **Unskip 16 approval tests** (PR #231): `test_approval_unblock.py` was importing `has_pending` and `pop_pending` from `tools.approval`, which the agent module had removed. The import failure tripped the `APPROVAL_AVAILABLE` guard and skipped all 16 tests in the file. Neither symbol was used in any test body. Removing the stale imports restores **595/595 passing, 0 skipped**.

## [v0.44.0] — 2026-04-10

### Features
- **Lucide SVG icons** (PR #221): Replaces all emoji icons in the sidebar, workspace, and tool cards with self-hosted Lucide SVG paths via `static/icons.js`. No CDN dependency — icons are bundled directly. The `li(name)` renderer uses a hardcoded whitelist, so server-supplied tool names never inject arbitrary SVG. All 35 `onclick=` functions verified to exist in JS; all 21 icon references verified in `icons.js`.

### Bug Fixes
- **Approval card hides immediately on respond/stream-end** (PR #225): `respondApproval()` and all stream-end SSE handlers (done, cancel, apperror, error, start-error) now call `hideApprovalCard(true)`. Previously the 30s minimum-visibility guard deferred the hide, leaving the card visible with disabled buttons for up to 30s after the user clicked Approve/Deny or the session completed. The poll-loop tick correctly keeps no-force so the guard still protects against transient polling gaps. Adds 11 structural tests for the timer logic.
- **Login page CSP fix** (PR #226): Moves `doLogin()` and Enter key listener from inline `<script>`/`onsubmit`/`onkeydown` attributes into `static/login.js`. Inline handlers are blocked by strict `script-src` CSP, causing silent login failure. i18n error strings now passed via `data-*` attributes instead of injected JS literals. Also guards `res.json()` parse with try/catch so non-JSON server errors fall back to the password-error message. Fixes #222.
- **Update error messages** (PR #227): `_apply_update_inner()` now fetches before pulling and surfaces three distinct failure modes with actionable recovery commands: network unreachable, diverged history (`git reset --hard`), and missing upstream tracking branch (`git branch --set-upstream-to`). Generic fallback truncates to 300 chars with a sentinel for empty output. Adds 13 tests covering all new diagnostic code paths. Fixes #223.
- **Approval pending check** (PR #228): `GET /api/approval/pending` always returned `{pending: null}` after the agent module renamed `has_pending` to `has_blocking_approval`. The route now checks `_pending` directly under `_lock`, matching how `submit_pending` writes to it. Fixes `test_approval_submit_and_respond`.

### Tests
- 579 passing, 16 skipped at this tag (595/595 after v0.44.1 unskip — +24 new tests across PRs #225, #227, #228)

## [v0.43.1] — 2026-04-10

- **CSRF fix for reverse proxies** (PR #219): The CSRF check now accepts `X-Forwarded-Host` and `X-Real-Host` headers in addition to `Host`, so deployments behind Caddy, nginx, and Traefik no longer reject POST requests with "Cross-origin request rejected". Security is preserved — requests with no matching proxy header are still rejected. Fixes #218.

## [v0.43.0] — 2026-04-10

### Features
- **Auto-install agent dependencies on startup** (PRs #215 + #216): When `hermes-agent` is found on disk but its Python dependencies are missing (common in Docker deployments where the agent is volume-mounted post-build), `server.py` now calls `api/startup.auto_install_agent_deps()` to install from `requirements.txt` or `pyproject.toml`. Falls back gracefully — failures are logged and never fatal.

### Bug Fixes
- **Session ID validator broadened** (PR #212): `Session.load()` rejected any session ID containing non-hex characters, breaking sessions created by the new hermes-agent format (`YYYYMMDD_HHMMSS_xxxxxx`). Validator now accepts `[0-9a-z_]` while rejecting path traversal patterns (null bytes, slashes, backslashes, dot-extensions).
- **Test suite isolation** (PR #216): `conftest.py` now kills any stale process on the test port (8788) before starting the fixture server. Stale QA harness servers (8792/8793) could occupy 8788 and cause non-deterministic test failures across the full suite.

## [v0.42.2] — 2026-04-10

### Bug Fixes
- **CSP blocking inline event handlers** (PR #209): `script-src 'self'` blocked all 55+ inline `onclick=` handlers in `index.html`, making the settings panel, sidebar navigation, and most interactive controls non-functional. Added `'unsafe-inline'` to `script-src`. Also restores `https://cdn.jsdelivr.net` to `script-src` and `style-src` for Mermaid.js and Prism.js (dropped in v0.42.1).

## [v0.42.1] — 2026-04-11

### Bug Fixes
- **i18n button text stripping** (post-review): Three sidebar buttons (`+ New job`, `+ New skill`, `+ New profile`) and three suggestion buttons had `data-i18n` on the outer element, which caused `applyLocaleToDOM` to replace the entire `textContent` — stripping the `+` prefix and emoji characters on locale switch. Fixed by wrapping only the translatable label text in a `<span data-i18n="...">`.
- **German translation corrections** (post-review): Fixed `cancelling` (imperative → progressive `"Wird abgebrochen…"`), `editing` (first-person verb → noun `"Bearbeitung"`), and completed truncated descriptions for `empty_subtitle`, `settings_desc_check_updates`, and `settings_desc_cli_sessions`.

## [v0.42.0] — 2026-04-10

### Features
- **German translation** (PR #190 by **[@DavidSchuchert](https://github.com/DavidSchuchert)**): Complete `de` locale covering all UI strings — settings, commands, sidebar, approval cards. Also extends the i18n system with `data-i18n-title` and `data-i18n-placeholder` attribute support so tooltip text and input placeholders are now translatable. German speech recognition uses `de-DE`.

### Bug Fixes
- **Custom slash-model routing** (PR #189 by **[@smurmann](https://github.com/smurmann)**): Model IDs like `google/gemma-4-26b-a4b` from custom providers (LM Studio, Ollama) were silently misrouted to OpenRouter because of the slash-heuristic. Custom providers now win: entries in `config.yaml → custom_providers` are checked first, so their model IDs route to the correct local endpoint regardless of format.
- **Phantom Custom group in model picker** (PR #191 by @mbac): When `model.provider` was a named provider (e.g. `openai-codex`) and `model.base_url` was set, `hermes_cli` reported `'custom'` as authenticated, producing a duplicate "Custom" group in the dropdown. The real provider's group was missing the configured default model. Fixed by discarding the phantom `custom` entry when a real named provider is active.
- **Hyphen/space model group injection** (PR #191): The "ensure default_model appears" post-pass used `active_provider.lower() in group_name.lower()`, which fails for `openai-codex` vs display name `OpenAI Codex` (hyphen vs space). Now uses `_PROVIDER_DISPLAY` for exact display-name matching.

## [v0.41.0] — 2026-04-10

### Features
- **Optional HTTPS/TLS support** (PR #199): Set `HERMES_WEBUI_TLS_CERT` and
  `HERMES_WEBUI_TLS_KEY` env vars to enable HTTPS natively. Uses
  `ssl.PROTOCOL_TLS_SERVER` with TLS 1.2 minimum. Gracefully falls back to HTTP
  if cert loading fails. No reverse proxy required for LAN/VPN deployments.

### Bug Fixes
- **CSP blocking Mermaid and Prism** (PR #197): Added Content-Security-Policy and
  Permissions-Policy headers to every response. CSP allows `cdn.jsdelivr.net` in
  `script-src` and `style-src` for Mermaid.js (dynamically loaded) and Prism.js
  (statically loaded with SRI integrity hashes). All other external origins blocked.
- **Session memory leak** (PR #196): `api/auth.py` accumulated expired session tokens
  indefinitely. Added `_prune_expired_sessions()` called lazily on every
  `verify_session()` call. No background thread, no lock contention.
- **Slow-client thread exhaustion** (PR #198): Added `Handler.timeout = 30` to kill
  idle/stalled connections before they exhaust the thread pool.
- **False update alerts on feature branches** (PR #201): Update checker compared
  `HEAD..origin/master` even when on a feature branch, counting unrelated master
  commits as missing updates. Now uses `git rev-parse --abbrev-ref @{upstream}` to
  track the current branch's upstream. Falls back to default branch when no upstream
  is set.
- **CLI session file browser returning 404** (PR #204): `/api/list` only checked
  the WebUI in-memory session dict, so CLI sessions shown in the sidebar always
  returned 404 for file browsing. Now falls back to `get_cli_sessions()` — the same
  pattern used by `/api/session` GET and `/api/sessions` list.

## [v0.40.2] — 2026-04-09

### Features
- **Full approval UI** (PR #187): When the agent triggers a dangerous command
  (e.g. `rm -rf`, `pkill -9`), a polished approval card now appears immediately
  instead of leaving the chat stuck in "Thinking…" forever. Four one-click buttons:
  Allow once, Allow session, Always allow, Deny. Enter key defaults to Allow once.
  Buttons disable immediately on click to prevent double-submit. Card auto-focuses
  Allow once so keyboard-only users can approve in one keystroke. All labels and
  the heading are fully i18n-translated (English + Chinese).

### Bug Fixes
- **Approval SSE event never sent** (PR #187): `register_gateway_notify()` was
  never called before the agent ran, so the approval module had no way to push
  the `approval` SSE event to the frontend. Fixed by registering a callback that
  calls `put('approval', ...)` the instant a dangerous command is detected.
- **Agent thread never unblocked** (PR #187): `/api/approval/respond` did not call
  `resolve_gateway_approval()`, so the agent thread waited for the full 5-minute
  gateway timeout. Now calls it on every respond, waking the thread immediately.
- **`_unreg_notify` scoping** (PR #187): Variable was only assigned inside a `try`
  block but referenced in `finally`. Initialised to `None` before the `try` so the
  `finally` guard is always well-defined.

### Tests
- 32 new tests in `tests/test_sprint30.py`: approval card HTML structure, all 4
  button IDs and data-i18n labels, keyboard shortcut in boot.js, i18n keys in both
  locales, CSS loading/disabled/kbd states, messages.js button-disable behaviour,
  streaming.py scoping, HTTP regression for all 4 choices.
- 16 tests in `tests/test_approval_unblock.py` (gateway approval unit + HTTP).
- **547 tests total** (499 → 515 → 547).

---

## [v0.40.1] — 2026-04-09

### Bug Fixes
- **Default locale on first install** (PR #185): A fresh install would start in
  English based on the server default, but `loadLocale()` could resurrect a
  stale or unsupported locale code from `localStorage`. Now `loadLocale()` falls
  back to English when there is no saved code or the saved code is not in the
  LOCALES bundle. `setLocale()` also stores the resolved code, so an unknown
  input never persists to storage.

---

## [v0.40.0] — 2026-04-09

### Features
- **i18n — pluggable language switcher** (PR #179): Settings panel now has a
  Language dropdown. Ships with English and Chinese (中文). All UI strings use
  a `t()` helper that falls back to English for missing keys. The login page
  also localises — title, placeholder, button, and error strings all respond to
  the saved locale. Add a language by adding a LOCALES entry to `static/i18n.js`.
- **Notification sound + browser notifications** (PR #180): Two new settings
  toggles. "Notification sound" plays a short two-tone chime when the assistant
  finishes or an approval card appears. "Browser notification" fires a system
  notification when the tab is in the background.
- **Thinking / reasoning block display** (PR #181, #182): Inline `<think>…</think>`
  and Gemma 4 `<|channel>thought…<channel|>` tags are parsed out of assistant
  messages and rendered as a collapsible lightbulb "Thinking" card above the reply.
  During streaming, the bubble shows "Thinking…" until the tag closes. Hardened
  against partial-tag edge cases and empty thinking blocks.

### Bug Fixes
- **Stray `}` in message row HTML** (PR #183): A typo in the i18n refactor left
  an extra `}` in the `msg-role` div template literal, producing `<div class="msg-role user" }>`.
  Removed.
- **JS-escape login locale strings** (PR #183): `LOGIN_INVALID_PW` and
  `LOGIN_CONN_FAILED` were injected into a JS string context without escaping
  single quotes or backslashes. Now uses minimal JS-string escaping.

---

## [v0.39.1] — 2026-04-08

### Bug Fixes
- **_ENV_LOCK deadlock resolved.** The environment variable lock was held for
  the entire duration of agent execution (including all tool calls and streaming),
  blocking all concurrent requests. Now the lock is acquired only for the brief
  env variable read/write operations, released before the agent runs, and
  re-acquired in the finally block for restoration.

---

## [v0.39.0] — 2026-04-08

### Security (12 fixes — PR #171 by @betamod, reviewed by @nesquena-hermes)

- **CSRF protection**: all POST endpoints now validate `Origin`/`Referer` against `Host`. Non-browser clients (curl, agent) without these headers are unaffected.
- **PBKDF2 password hashing**: `save_settings()` was using single-iteration SHA-256. Now calls `auth._hash_password()` — PBKDF2-HMAC-SHA256 with 600,000 iterations and a per-installation random salt.
- **Login rate limiting**: 5 failed attempts per 60 seconds per IP returns HTTP 429.
- **Session ID validation**: `Session.load()` rejects any non-hex character before touching the filesystem, preventing path traversal via crafted session IDs.
- **SSRF DNS resolution**: `get_available_models()` resolves DNS before checking private IPs. Prevents DNS rebinding attacks. Known-local providers (Ollama, LM Studio, localhost) are whitelisted.
- **Non-loopback startup warning**: server prints a clear warning when binding to `0.0.0.0` without a password set — a common Docker footgun.
- **ENV_LOCK consistency**: `_ENV_LOCK` now wraps all `os.environ` mutations in both the sync chat and streaming restore blocks, preventing races across concurrent requests.
- **Stored XSS prevention**: files with `text/html`, `application/xhtml+xml`, or `image/svg+xml` MIME types are forced to `Content-Disposition: attachment`, preventing execution in-browser.
- **HMAC signature**: extended from 64 bits to 128 bits (16-char to 32-char hex).
- **Skills path validation**: `resolve().relative_to(SKILLS_DIR)` check added after skill directory construction to prevent traversal.
- **Secure cookie flag**: auto-set when TLS or `X-Forwarded-Proto: https` is detected. Uses `getattr` safely so plain sockets don't raise `AttributeError`.
- **Error path sanitization**: `_sanitize_error()` strips absolute filesystem paths from exception messages before they reach the client.

### Tests
- Added `tests/test_sprint29.py` — 33 tests covering all 12 security fixes.

---

## [v0.38.6] — 2026-04-07

### Fixed
- **`/insights` message count always 0 for WebUI sessions** (#163, #164): `sync_session_usage()` wrote token counts, cost, model, and title to `state.db` but never `message_count`. Both the streaming and sync chat paths now pass `len(s.messages)`. Note: `/insights` sync is opt-in — enable **Sync to Insights** in Settings (it's off by default).

---

## [v0.38.5] — 2026-04-06

### Fixed
- **Custom endpoint URL construction** (#138, #160): `base_url` ending in `/v1` was incorrectly stripped before appending `/models`, producing `http://host/models` instead of `http://host/v1/models`. Fixed to append directly.
- **`custom_providers` config entries now appear in dropdown** (#138, #160): Models defined under `config.yaml` `custom_providers` (e.g. Ollama aliases, Azure model overrides) are now always included in the dropdown, even when the `/v1/models` endpoint is unreachable.
- **Custom endpoint API key reads profile `.env`** (#138, #160): Custom endpoint auth now checks `~/.hermes/.env` keys in addition to `os.environ`.

---

## [v0.38.4] — 2026-04-06

### Fixed
- **Copilot false positive in model dropdown** (#158): `list_available_providers()` reported Copilot as available on any machine with `gh` CLI auth, because the Copilot token resolver falls back to `gh auth token`. The dropdown now skips any provider whose credential source is `'gh auth token'` — only explicit, dedicated credentials count. Users with `GITHUB_TOKEN` explicitly set in their `.env` still see Copilot correctly.

---

## [v0.38.3] — 2026-04-06

### Fixed
- **Model dropdown shows only configured providers** (#155): Provider detection now uses `hermes_cli.models.list_available_providers()` — the same auth check the Hermes agent uses at runtime — instead of scanning raw API key env vars. The dropdown now reflects exactly what the user has configured (auth.json, credential pools, OAuth flows like Copilot). When no providers are detected, shows only the configured default model rather than a full generic list. Added `copilot` and `gemini` to the curated model lists. Falls back to env var scanning for standalone installs without hermes-agent.

---

## [v0.38.2] — 2026-04-06

### Fixed
- **Tool cards actually render on page reload** (#140, #153): PR #149 fixed the wrong filter — it updated `vis` but not `visWithIdx` (the loop that actually creates DOM rows), so anchor rows were never inserted. This PR fixes `visWithIdx`. Additionally, `streaming.py`'s `assistant_msg_idx` builder previously only scanned Anthropic content-array format and produced `idx=-1` for all OpenAI-format tool calls (the format used in saved sessions); it now handles both. As a final fallback, `renderMessages()` now builds tool card data directly from per-message `tool_calls` arrays when `S.toolCalls` is empty, covering historical sessions that predate session-level tool tracking.

---

## [v0.38.1] — 2026-04-06

### Fixed
- **Model selector duplicates** (#147, #151): When `config.yaml` sets `model.default` with a provider prefix (e.g. `anthropic/claude-opus-4.6`), the model dropdown no longer shows a duplicate entry alongside the existing bare-ID entry. The dedup check now normalizes both sides before comparing.
- **Stale model labels** (#147, #151): Sessions created with models no longer in the current provider list now show `"ModelName (unavailable)"` in muted text with a tooltip, instead of appearing as a normal selectable option that would fail silently on send.

---

## [v0.38.0] — 2026-04-06

### Fixed
- **Multi-provider model routing (#138):** Non-default provider models now use `@provider:model` format. `resolve_model_provider()` routes them through `resolve_runtime_provider(requested=provider)` — no OpenRouter fallback for users with direct provider keys.
- **Personalities from config.yaml (#139):** `/api/personalities` reads from `config.yaml` `agent.personalities` (the documented mechanism). Personality prompts pass via `agent.ephemeral_system_prompt`.
- **Tool call cards survive page reload (#140):** Assistant messages with only `tool_use` content are no longer filtered from the render list, preserving anchor rows for tool card display.

---

## [v0.37.0] /personality command, model prefix routing fix, tool card reload fix
*April 6, 2026 | 465 tests*

### Features
- **`/personality` slash command.** Set a per-session agent personality from `~/.hermes/personalities/<name>/SOUL.md`. The personality prompt is prepended to the system message for every turn. Use `/personality <name>` to activate, `/personality none` to clear, `/personality` (no args) to list available personalities. Backend: `GET /api/personalities`, `POST /api/personality/set`. (PR #143)

### Bug Fixes
- **Model dropdown routes non-default provider models correctly (#138).** When the active provider is `anthropic` and you pick a `minimax` model, its ID is now prefixed `minimax/MiniMax-M2.7` so `resolve_model_provider()` can route it through OpenRouter. Guards added: `active_provider=None` prevents all-providers-prefixed, case is normalised, shared `_PROVIDER_MODELS` list is no longer mutated by the default_model injector. (PR #142)
- **Tool call cards persist correctly after page reload.** The reload rendering logic now anchors cards AFTER the triggering assistant row (not before the next one), handles multi-step chains sharing a filtered anchor in chronological order, and filters fallback anchor to assistant rows only. (PR #141)

---

## [v0.36.3] Configurable Assistant Name
*April 6, 2026 | 449 tests*

### Features
- **Configurable bot name.** New "Assistant Name" field in Settings panel.
  Display name updates throughout the UI: sidebar, topbar, message roles,
  login page, browser tab title, and composer placeholder. Defaults to
  "Hermes". Configurable via settings or `HERMES_WEBUI_BOT_NAME` env var.
  Server-side sanitization prevents empty names and escapes HTML for the
  login page. (PR #135, based on #131 by @TaraTheStar)

---

## [v0.36.2] OpenRouter model routing fix
*April 5, 2026 | 440 tests*

### Bug Fixes
- **OpenRouter models sent without prefix, causing 404 (#116).** `resolve_model_provider()` was stripping the `openrouter/` prefix from model IDs (e.g. sending `free` instead of `openrouter/free`) when `config_provider == 'openrouter'`. OpenRouter requires the full `provider/model` path to route upstream correctly. Fixed with an early return that preserves the complete model ID for all OpenRouter configs. (#127)
- Added 7 unit tests for `resolve_model_provider()` — first coverage on this function. Tests the regression, cross-provider routing, direct-API prefix stripping, bare models, and empty model.

---

## [v0.36.1] Login form Enter key fix
*April 5, 2026 | 433 tests*

### Bug Fixes
- **Login form Enter key unreliable in some browsers (#124).** `onsubmit="return doLogin(event)"` returned a Promise (async functions always return a truthy Promise), which could let the browser fall through to native form submission. Fixed with `doLogin(event);return false` plus an explicit `onkeydown` Enter handler on the password input as belt-and-suspenders. (#125)

---

## [v0.36] Self-Update Checker with One-Click Update
*April 5, 2026 | 433 tests*

### Features
- **Update checker.** Non-blocking background check on boot detects when the
  WebUI or hermes-agent git repos are behind upstream. Blue banner shows
  "WebUI: N updates, Agent: N updates available" with Update Now / Later.
- **One-click update.** "Update Now" runs `git stash && git pull --ff-only &&
  git stash pop` on each behind repo, then reloads the page. Concurrent update
  attempts blocked via lock. Dirty working trees safely stashed and restored.
- **Settings toggle.** "Check for updates" checkbox in Settings panel. Persisted
  server-side. Disabled = no background fetch, no banner.
- **30-minute cache.** Git fetch runs at most twice per hour regardless of tab
  count. Results cached server-side with TTL.
- **Session-scoped dismissal.** "Later" dismisses banner for the current tab
  session (sessionStorage). New tabs get a fresh check.
- **Test mode.** `?test_updates=1` URL param shows the banner with fake data
  (localhost only) for UI testing without needing to actually be behind.

### Architecture
- New `api/updates.py`: `check_for_updates()`, `apply_update()`. Thread-safe
  caching with `_cache_lock`. Concurrent apply blocked with `_apply_lock`.
  Default branch auto-detected (master/main).
- `api/routes.py`: `GET /api/updates/check`, `POST /api/updates/apply`.
  Simulate endpoint gated to 127.0.0.1.
- `static/ui.js`: `_showUpdateBanner()`, `dismissUpdate()`, `applyUpdates()`.
- `static/boot.js`: fire-and-forget check on boot (does not block UI).
- `api/config.py`: `check_for_updates` in settings defaults + bool keys.
- Docker safe: all git ops gated by `.git` directory existence check.

---

## [v0.35.1] Model dropdown fixes
*April 5, 2026 | 433 tests*

### Bug Fixes
- **Custom providers invisible in model dropdown (#117).** `cfg_base_url` was scoped inside a conditional block but referenced unconditionally, causing a `NameError` for users with a `base_url` in config.yaml. Fix: initialize to `''` before the block. (#118)
- **Configured default model missing from dropdown (#116).** OpenRouter and other providers replaced the model list with a hardcoded fallback that didn't include `model.default` values like `openrouter/free` or custom local model names. Fix: after building all groups, inject the configured `default_model` at the top of its provider group if absent. (#119)

---

## [v0.35] Security hardening
*April 5, 2026 | 433 tests*

### Security fixes
- **ENV race condition (HIGH):** Two concurrent sessions could interleave `os.environ` writes, clobbering workspace and session keys. Fixed with a global `_ENV_LOCK` in `streaming.py` that serializes the env save/restore block across all sessions. (#108)
- **Predictable signing key (MEDIUM):** Session cookies were signed with `sha256(STATE_DIR)` -- deterministic and forgeable if the install path is known. Now generates a cryptographically random 32-byte key on first startup, persisted to `STATE_DIR/.signing_key` (chmod 600). (#108)
- **Upload path traversal (MEDIUM):** Filenames like `..` survived the `[^\w.\-]` sanitization regex because dots are allowed. Fixed by rejecting dot-only filenames and validating the resolved path stays within the workspace sandbox via `safe_resolve_ws()`. (#108)
- **Weak password hashing (MEDIUM):** Bare SHA-256 with a predictable salt replaced with PBKDF2-SHA256 at 600k iterations (OWASP recommendation) using the random signing key as salt. No new dependencies (stdlib `hashlib.pbkdf2_hmac`). (#108)

**Breaking change:** Existing session cookies and password hashes are invalidated on first restart after upgrade. Users with password auth enabled will need to re-set their password.

---

## [v0.34.3] Light theme final polish
*April 5, 2026 | 433 tests*

### Bug Fixes
- **Light theme: sidebar, role labels, chips, and interactive elements all broken.** Session titles were too faint, active session used washed-out gold, pin stars were near-invisible bright yellow, and all hover/border effects used dark-theme white `rgba(255,255,255,.XX)` values invisible on cream. Fixed with 46 scoped `[data-theme="light"]` selector overrides covering session items, role labels, project chips, topbar chips, composer, suggestions, tool cards, cron list, and more. (#105)
- Active session now uses blue accent (`#2d6fa3`) for strong contrast. Pin stars use deep gold (`#996b15`). Role labels are solid and high contrast.

---

## [v0.34.2] Theme text colors
*April 5, 2026 | 433 tests*

### Bug Fixes
- **Light mode text unreadable.** Bold text was hardcoded white (invisible on cream), italic was light purple on cream, inline code had a dark box on a light background. Fixed by introducing 5 new per-theme CSS variables (`--strong`, `--em`, `--code-text`, `--code-inline-bg`, `--pre-text`) defined for every theme. (#102)
- Also replaced remaining `rgba(255,255,255,.08)` border references with `var(--border)`, and darkened light theme `--code-bg` slightly for better contrast.

---

## [v0.34.1] Theme variable polish
*April 5, 2026 | 433 tests*

### Bug Fixes
- **All non-dark themes had broken surfaces, topbar, and dropdowns.** 30+ hardcoded dark-navy rgba/hex values in style.css were stuck on the Dark palette regardless of active theme. Fixed by introducing 7 new CSS variables (`--surface`, `--topbar-bg`, `--main-bg`, `--input-bg`, `--hover-bg`, `--focus-ring`, `--focus-glow`) defined per-theme, replacing every hardcoded reference. (#100)

---

## [v0.34] Sprint 26 -- Pluggable UI Themes
*April 5, 2026 | 433 tests*

### Features
- **6 built-in themes.** Dark (default), Light, Slate, Solarized Dark, Monokai,
  Nord. Defined as CSS variable overrides on `:root[data-theme="name"]` — the
  entire UI adapts automatically.
- **Theme picker in Settings.** Dropdown with instant live preview. Changes
  apply immediately as you click through options.
- **`/theme` slash command.** `/theme dark`, `/theme light`, etc.
- **Theme persistence.** Saved server-side in `settings.json` and client-side
  in `localStorage` for flicker-free loading on page refresh.
- **Flash prevention.** Inline `<script>` in `<head>` reads localStorage before
  the stylesheet loads — no flash of the wrong theme.
- **Custom theme support.** Any theme name is accepted (no enum gate). Create a
  `:root[data-theme="name"]` CSS block and it works. See `THEMES.md`.
- **Unsaved changes guard.** Settings panel now tracks dirty state and shows a
  "You have unsaved changes" bar with Save/Discard buttons when closing with
  unpersisted changes. Theme preview reverts on discard.

### Architecture
- `static/style.css`: 6 theme blocks using CSS variable overrides. Light theme
  includes scrollbar and selection overrides.
- `static/commands.js`: `/theme` command with validation.
- `static/panels.js`: Settings dirty tracking, revert-on-discard, unsaved bar.
- `static/boot.js`: Theme applied from server settings on boot.
- `api/config.py`: `theme` field in `_SETTINGS_DEFAULTS` (no enum gate).
- `THEMES.md`: Full documentation for creating custom themes.

### Tests
- 9 new tests in `test_sprint26.py`: default theme, round-trip persistence for
  all 6 built-in themes, custom theme acceptance, settings isolation.
  Total: **433 tests**.

---

## [v0.33] /insights Sync + state.db Bridge Fix
*April 5, 2026 | 424 tests*

### Features
- **Opt-in `/insights` sync.** New "Sync usage to /insights" setting (default: off). When enabled, after each turn the WebUI mirrors session token usage, cost, model, and title into `state.db` so `hermes /insights` includes browser session activity. (#92, #93)

### Bug Fixes
- **state_sync.py correctness fixes.** Three bugs in the initial implementation caught during code review: wrong class name (`HermesState` → `SessionDB`), wrong constructor argument type (`str` → `Path`), wrong title update method (`_execute_write` with bad signature → `set_session_title`). Also fixed a SQLite connection leak (persistent connection opened per call, never closed). (#95)

---

## [v0.32] Auto-Compaction Handling + /compact Command (Issue #90)
*April 5, 2026 | 424 tests*

### Features
- **Auto-compaction detection.** When the agent's `run_conversation()` triggers
  context compression and rotates the session ID, the WebUI detects the mismatch
  and renames the session file + cache entry so messages don't split across files.
- **`compressed` SSE event.** Frontend receives a notification when compression
  fires, shows a system message ("Context was auto-compressed") and a toast.
- **`/compact` slash command.** Type `/compact` to request the agent compress
  the conversation context. Sends a natural-language message that triggers the
  agent's compression preflight.
- **Real context window data.** The context usage indicator now uses actual
  `context_length`, `threshold_tokens`, and `last_prompt_tokens` from the agent's
  compressor instead of the client-side model name lookup. Tooltip shows the
  auto-compress threshold. Hides gracefully when the agent has no compressor.

### Architecture
- `api/streaming.py`: Session ID mismatch detection after `run_conversation()`,
  file rename, SESSIONS cache update under lock, `compressed` SSE event,
  `context_length`/`threshold_tokens`/`last_prompt_tokens` in usage dict.
- `static/commands.js`: `/compact` command.
- `static/messages.js`: `compressed` SSE event handler.
- `static/ui.js`: `_syncCtxIndicator()` rewritten to use server-side compressor
  data instead of client-side model estimates.

---

## [v0.31.2] CLI session delete fix
*April 5, 2026 | 424 tests*

### Bug Fixes
- **CLI sessions could not be deleted from the sidebar.** The delete handler only
  removed the WebUI JSON session file, so CLI-backed sessions came back on refresh.
  Added `delete_cli_session(sid)` in `api/models.py` and call it from
  `/api/session/delete` so the SQLite `state.db` row and messages are removed too.
  (#87, #88)

### Notes
- The public test suite still passes at 424/424.
- Issue #87 already had a comment confirming the root cause, so no new issue comment
  was needed here.

## [v0.31] UI Polish + Deployment Hardening
*April 4, 2026 | 424 tests*

### Bug Fixes
- **Profile dropdown overlaps chat messages.** `.topbar` had no stacking context,
  causing the dropdown to paint over `.messages`. Added `position:relative;z-index:10`
  to `.topbar`. (#71)
- **Workspace dropdown clipped by sidebar.** `.sidebar overflow:hidden` swallowed
  the upward-opening workspace dropdown entirely. Changed to `overflow:visible`
  (scroll lives on `.session-list`); added `position:relative;z-index:10` to
  `.sidebar-bottom`. (#71)
- **Slash-command autocomplete behind tool cards.** `.composer-wrap` had
  `position:relative` but no `z-index`, letting tool cards bleed over it.
  Added `z-index:10`. (#71)
- **Skill picker clipped inside Settings modal.** `.settings-panel overflow-y:auto`
  clipped the absolute-positioned skill picker. Moved scroll to `.settings-body`,
  set panel to `overflow:visible`, raised skill picker to `z-index:1100`. (#71)
- **CLI session badge blocks action buttons on hover.** Added
  `.session-item.cli-session:hover::after { display:none }` so the gold "cli"
  label hides on hover, making archive/delete/pin fully reachable. (#71)
- **Workspace dropdown name and path crowded on same line.** `.ws-opt` was a plain
  block with inline spans. Added `flex-direction:column;gap:4px` so name and path
  stack cleanly. (#71)
- **Both servers sharing same state directory.** `api/config.py` and `start.sh`
  both defaulted to `~/.hermes/webui-mvp` (an internal dev name). Changed default
  to `~/.hermes/webui` -- generic, appropriate for any deployment. Override with
  `HERMES_WEBUI_STATE_DIR`. (#72, #73)

---

## [v0.30.1] CLI Session Bridge Fixes
*April 4, 2026 | 424 tests*

### Bug Fixes
- **CLI sessions not appearing in sidebar.** Three frontend gaps: `sessions.js`
  wasn't rendering CLI sessions (missing `is_cli_session` check in render loop),
  sidebar click handler didn't trigger import, and the "cli" badge CSS selector
  wasn't matching the rendered DOM structure. (#58)
- **CLI bridge read wrong profile's state.db.** `get_cli_sessions()` resolved
  `HERMES_HOME` at server launch time, not at call time. After a profile switch,
  it kept reading the original profile's database. Now resolves dynamically via
  `get_active_hermes_home()`. (#59)
- **Silent SQL error swallowed all CLI sessions.** The `sessions` table in
  `state.db` has no `profile` column — the query referenced `s.profile` which
  caused a silent `OperationalError`. The `except Exception: return []` handler
  swallowed it, returning zero CLI sessions. Removed the column reference and
  added explicit column-existence checks. (#60)

### Features
- **"Show CLI sessions" toggle in Settings.** New checkbox in the Settings panel
  to show/hide CLI sessions in the sidebar. Persisted server-side in
  `settings.json` (`show_cli_sessions`, default `true`). When disabled, CLI
  sessions are excluded from `/api/sessions` responses. (#61)

---

## [v0.30] CLI Session Bridge (Community: @thadreber-web)
*April 4, 2026 | 424 tests*

### Features
- **CLI session bridge.** The WebUI now reads sessions from the hermes-agent's
  SQLite store (`state.db`). CLI sessions appear in the sidebar with a gold
  "cli" indicator badge. Click to import into the WebUI store with full message
  history — replies then work through the normal agent pipeline.
- **`/api/session/import_cli` endpoint.** Imports a CLI session into the WebUI
  JSON store. Idempotent — returns existing session if already imported.
  Derives title from first message, inherits active profile and workspace.
- **`/api/sessions` merges CLI sessions.** Sidebar shows both WebUI and CLI
  sessions sorted by last activity. Deduplication ensures WebUI sessions take
  priority when the same session_id exists in both stores.
- **CLI session fallback on `/api/session`.** If a session_id isn't found in
  the WebUI store, falls back to reading from the CLI SQLite store.

### Architecture
- `api/models.py`: `get_cli_sessions()`, `get_cli_session_messages()`,
  `import_cli_session()`. All use parameterized SQL queries and `with` for
  connection management. Graceful fallback on missing sqlite3 or state.db.
- `api/routes.py`: CLI fallback in GET `/api/session`, merged list in
  GET `/api/sessions`, POST `/api/session/import_cli`.
- `static/style.css`: `.cli-session` indicator styles (gold border + badge).

---

## [v0.29] Sprint 23: Agentic Transparency + Polish
*April 4, 2026 | 424 tests*

### Features

- **Token/cost display.** Agent usage (input tokens, output tokens, estimated
  cost) is now read after each conversation and persisted on the session.
  A muted badge appears below the last assistant message when enabled.
  Off by default — toggle via the Settings panel checkbox or `/usage` slash
  command. Persists server-side across refreshes.

- **Subagent delegation cards.** `subagent_progress` events now render with
  a shuffle icon and a blue indented left border to visually distinguish child
  tool activity from parent tool calls. `delegate_task` cards display as
  "Delegate task" with cleaner formatting.

- **Skill picker in cron create form.** The "New Job" form now has a search
  input + tag chip picker for attaching skills to cron jobs. Skills fetched
  from `/api/skills`, filtered on keyup, added/removed as tag chips.
  `submitCronCreate()` sends `skills` array in the POST body. Backend already
  supported the field — this was a pure frontend gap.

- **Skill linked files viewer.** Skill preview panel now renders a "Linked
  Files" section below SKILL.md content when a skill has `references/`,
  `templates/`, `scripts/`, or `assets/` subdirectories. Clicking a file
  loads it in the preview panel with syntax highlighting.
  New `file` query param on `GET /api/skills/content` serves linked files
  with path traversal protection.

- **Workspace tree state persists across refreshes.** Expanded directory
  paths are saved to `localStorage` keyed by workspace path
  (`hermes-webui-expanded:{path}`). On every root load (page refresh,
  session switch), the saved state is restored and previously-expanded
  directories are pre-fetched so the tree renders fully on first paint.

- **Timestamps fixed.** `api/streaming.py` now stamps `timestamp` on every
  message that lacks one at conversation completion. The `done` SSE event
  also stamps `_ts` on the last assistant message immediately. Timestamps
  were already rendered in the UI (Sprint 14, hover-to-reveal) but most
  messages had no timestamp field, so nothing ever showed.

- **`/usage` slash command.** Instant toggle for token usage display.
  Shows a toast, persists to server, updates the Settings checkbox if open,
  re-renders immediately.

### Bug Fixes

- **XSS via inline onclick + esc().** Skill names and file paths embedded in
  `onclick` HTML attributes used `esc()` for encoding. `esc()` converts `'`
  to `&#39;` (HTML-safe) but browsers decode it back before executing JS,
  allowing skill names with apostrophes to break out of string literals.
  Fixed by switching to `data-*` attributes + `addEventListener`.

- **rglob wildcard injection.** The `name` query param for
  `/api/skills/content?file=` was passed directly to `SKILLS_DIR.rglob()`,
  which accepts glob patterns. `name=*` would match an arbitrary directory
  and use it as the trust base for path traversal checking.
  Fixed by rejecting names containing `* ? [ ]` metacharacters with 400.

- **`_fmtTokens(null)` returned "null".** `String(null)` = `"null"` would
  appear in the usage badge for sessions missing fields. Fixed with a
  `!n || n < 0` guard returning `'0'`.

- **Usage badge on wrong row.** Badge used `:last-child` which could target
  a user message row. Fixed by adding `data-role` to message rows and
  scanning backwards for the last `assistant` row.

- **Tool name resolution.** Tool call entries in session JSON sometimes
  stored the literal string `"tool"` as the name when the call ID couldn't
  be resolved. Fixed: defaults to empty string and skips unresolvable entries.

- **Inline import inside loop.** `import json as _j2` inside the done-handler
  loop in `streaming.py` moved to module-level.

### Session Model

- Added `input_tokens`, `output_tokens`, `estimated_cost` fields to Session
  (defaults: 0, 0, None). Included in `compact()`, session JSON, and all
  API responses. Backward-compatible via `**kwargs`.

- Added `args` capture to `tool_calls` session JSON entries (truncated
  snapshot of tool inputs, up to 6 keys / 120 chars each).

### Settings

- New `show_token_usage` boolean setting (default: `false`). Stored in
  `settings.json`, loaded on boot alongside `send_key`.

### Tests

- Renamed `test_sprint24.py` → `test_sprint23.py`.
- Strengthened session usage assertions (explicit field presence checks).
- Added: path traversal rejection test, wildcard name rejection test,
  cron create with skills array test.
- Total: 424 tests (up from 415).

---

## [v0.28.1] CI Pipeline + Multi-Arch Docker Builds
*April 3, 2026 | 426 tests*

### Features
- **GitHub Actions CI.** New workflow triggers on tag push (`v*`). Builds
  multi-arch Docker images (linux/amd64 + linux/arm64), pushes to
  `ghcr.io/nesquena/hermes-webui`, and creates a GitHub Release with
  auto-generated release notes. Uses GHA layer caching for fast rebuilds.
- **Pre-built container images.** Users can now `docker pull ghcr.io/nesquena/hermes-webui:latest`
  instead of building locally.

---

## [v0.27] Profile Creation Fallback for Docker (Issue #44)
*April 3, 2026 | 426 tests*

### Bug Fixes
- **Profile creation works without hermes-agent.** In Docker containers where
  `hermes_cli` is not importable, profile creation now falls back to a local
  implementation that creates the directory structure and optionally clones
  config files. Previously returned `RuntimeError` with "hermes-agent required".
- **Name validation uses `fullmatch()`.** Prevents trailing-newline bypass of
  the `$` anchor in `re.match()`. Not reachable from the web UI (name is
  stripped), but fixed for defense-in-depth.
- **`clone_from` validated in `create_profile_api()`.** Defense-in-depth:
  prevents path traversal if called by a non-HTTP client.
- **Fallback return uses full 9-key schema.** Previously returned only 2 keys
  (`name`, `path`), inconsistent with the normal response shape.
- **Atomic directory creation.** `mkdir(exist_ok=False)` prevents TOCTOU race
  on concurrent profile creates.

### Architecture
- `api/profiles.py`: `_validate_profile_name()`, `_create_profile_fallback()`,
  `_PROFILE_ID_RE`, `_PROFILE_DIRS`, `_CLONE_CONFIG_FILES` constants matching
  upstream `hermes_cli.profiles`.
- `docker-compose.yml`: Removed `:ro` from `~/.hermes` mount (required for
  profile writes). Localhost-only binding preserved.

---

## [v0.26] Profile System Polish -- 10 Post-Sprint-23 Fixes
*April 3, 2026 | 426 tests*

### Bug Fixes
- **Profile switch base dir bug.** When `HERMES_HOME` was mutated to a
  `profiles/` subdir at startup, `switch_profile()` doubled the path
  (e.g. `~/.hermes/profiles/X/profiles/X`). New `_resolve_base_hermes_home()`
  detects profile subdirs and walks up to the actual base.
- **Cross-provider model routing.** Picking a model from a different provider
  than the config's default now routes through OpenRouter instead of trying
  a direct API call to a provider whose key may not exist.
- **Legacy sessions missing profile tag.** `all_sessions()` now backfills
  `profile='default'` for pre-Sprint-22 sessions so the profile filter works.
- **Workspace list cleanup.** Stale paths, test artifacts, and cross-profile
  entries are now cleaned on load. Legacy global workspace file migrated
  once for the default profile.
- **API error messages.** `api()` helper now parses JSON error bodies and
  surfaces the human-readable message instead of raw JSON.
- **Workspace dropdown moved to sidebar.** The workspace picker now opens
  upward from the sidebar bottom instead of clipping behind the topbar.

### Features
- **Rate limit error display.** Rate limit errors (429) now show a distinct
  card with a rate limit icon and hint, instead of the generic error message.
- **SSE `apperror`/`warning` events.** Server can send typed error events
  that the frontend handles with appropriate UX (rate limit card, fallback
  notice, etc.).
- **Smart model resolver.** `_findModelInDropdown()` handles name mismatches
  between config model IDs and dropdown values (e.g. `claude-sonnet-4-6` vs
  `anthropic/claude-sonnet-4.6`).
- **Profile switch starts new session.** When the current session has messages,
  switching profiles automatically starts a fresh session to prevent
  cross-profile tagging.
- **Per-profile toolsets.** Agent now reads `platform_toolsets.cli` from the
  active profile's config at call time, not the boot-time snapshot.
- **Per-profile fallback model.** `fallback_model` config is read from the
  active profile and passed to AIAgent.

### Architecture
- `api/profiles.py`: `_resolve_base_hermes_home()` replaces naive env var read.
- `api/workspace.py`: `_clean_workspace_list()`, `_migrate_global_workspaces()`.
- `api/streaming.py`: Per-profile toolsets and fallback model at call time.
- `api/models.py`: `all_sessions()` backfills `profile='default'`.
- `static/ui.js`: `_findModelInDropdown()`, `_applyModelToDropdown()`.
- `static/messages.js`: `apperror` and `warning` SSE event handlers.

---

## [v0.25] Sprint 23 -- Profile/Workspace/Model Coherence
*April 3, 2026 | 423 tests*

### Features
- **Profile-local workspace storage.** Each named profile now stores its own
  `workspaces.json` and `last_workspace.txt` under `{profile_home}/webui_state/`.
  Default profile continues using the global STATE_DIR for backward compat.
- **Profile switch returns defaults.** `POST /api/profile/switch` response now
  includes `default_model` and `default_workspace` from the new profile's
  config.yaml, enabling one-round-trip state sync.
- **Session profile filter.** Session sidebar filters to the active profile by
  default. "Show N from other profiles" toggle reveals sessions from all
  profiles, modeled on the existing archived toggle. Resets on profile switch.

### Bug Fixes
- **Model picker ignores profile on switch.** `switchToProfile()` now clears
  the `hermes-webui-model` localStorage key so the profile's default model
  applies instead of a stale preference from another profile.
- **Workspace list was global.** Switching profiles no longer shows the wrong
  profile's workspaces.
- **`DEFAULT_WORKSPACE` was a boot-time singleton.** Now resolved dynamically
  through `_profile_default_workspace()`.
- **Session list showed all profiles.** Now filtered to active profile.
- **`switchToProfile()` didn't refresh workspaces or sessions.** Now refreshes
  workspace list, session list, and resets profile filter on switch.

### Architecture
- `api/workspace.py` rewritten with profile-aware path resolution.
- `api/profiles.py`: `switch_profile()` returns `default_model` and
  `default_workspace`.
- `static/sessions.js`: Profile filter with toggle UI.
- `static/panels.js`: Full cascade refresh on profile switch.
- 8 new tests in `test_sprint23.py`.

---

## [v0.24] Sprint 22 -- Multi-Profile Support (Issue #28)
*April 3, 2026 | 415 tests*

### Features
- **Profile picker (topbar).** Purple-accented chip with SVG user icon. Click
  to open dropdown listing all profiles with gateway status dots (green =
  running), model info, and skill count. Click any profile to switch; "Manage
  profiles" link opens the sidebar panel.
- **Profiles management panel.** New sidebar tab with full CRUD UI. Profile
  cards show name, model/provider, skill count, API key status, and gateway
  status badge. "Use" button switches profile, delete button removes non-default
  profiles (with confirmation).
- **Profile creation.** "+ New profile" form with name validation (`[a-z0-9_-]`),
  optional "clone config from active" checkbox. Wraps the CLI's
  `hermes_cli.profiles.create_profile()`.
- **Profile deletion.** Confirm dialog. Auto-switches to default if deleting
  the active profile. Blocked while agent is running.
- **Seamless profile switching.** No server restart. Profile switch updates
  `HERMES_HOME`, patches module-level caches in hermes-agent's `skills_tool`
  and `cron/jobs`, reloads `.env` API keys and `config.yaml`, refreshes the
  model dropdown, skills, memory, and cron panels.
- **Per-session profile tracking.** `profile` field on Session records which
  profile was active at creation. Backward-compatible (`null` for old sessions).

### Bug Fixes
- **Hardcoded `~/.hermes` paths.** Memory read/write and model discovery used
  hardcoded paths. Now resolved through `get_active_hermes_home()`.
- **Module-level path caching.** hermes-agent modules snapshot `HERMES_HOME`
  at import time. Profile switch now monkey-patches `SKILLS_DIR`, `CRON_DIR`,
  `JOBS_FILE`, `OUTPUT_DIR` so they track the active profile.

### Architecture
- New `api/profiles.py`: profile state management wrapping `hermes_cli.profiles`.
  Thread-safe (`_profile_lock`). Lazy imports avoid circular deps.
- `api/config.py`: module-level `cfg` replaced with reloadable `get_config()`
  / `reload_config()`. Dynamic `_get_config_path()` resolves through profile.
- `api/streaming.py`: `HERMES_HOME` added to env save/restore block.
- Profile switch blocked while agent streams are active.
- 5 new API endpoints: `GET /api/profiles`, `GET /api/profile/active`,
  `POST /api/profile/switch`, `POST /api/profile/create`,
  `POST /api/profile/delete`.
- Zero modifications to hermes-agent code.

---

## [v0.23] Sprint 21 -- Mobile Responsive + Docker
*April 3, 2026 | 415 tests*

### Features
- **Mobile responsive layout (Issue #21).** Full mobile experience with
  hamburger sidebar (slide-in overlay), bottom navigation bar (5-tab iOS
  pattern), and files slide-over panel. Touch targets minimum 44px. Composer
  positioned above bottom nav. Session clicks auto-close sidebar. Desktop
  layout completely unchanged — all mobile elements hidden via `@media`.
- **Docker support (Issue #7).** Dockerfile (`python:3.12-slim`), docker-compose.yml
  with named volume for state persistence, optional `~/.hermes` mount for
  agent features. Binds to `127.0.0.1` by default for security.

### Bug Fixes (from review)
- **CSS cascade broke mobile slide-in.** `position:relative` rules after the
  media query overrode `position:fixed` on mobile. Wrapped in `@media(min-width:641px)`.
- **mobileSwitchPanel() always reopened sidebar.** Chat tab now closes sidebar
  instead of reopening it over the main chat area.
- **Dockerfile missing pip install.** Added `pip install -r requirements.txt`.
- **No .dockerignore.** Added exclusions for `.git`, `tests/`, `.env*`.
- **docker-compose tilde expansion.** Changed `~/.hermes` default to
  `${HOME}/.hermes` (Docker Compose doesn't shell-expand `~`).

### Architecture
- Mobile navigation functions in `boot.js`: `toggleMobileSidebar()`,
  `closeMobileSidebar()`, `toggleMobileFiles()`, `mobileSwitchPanel()`.
- `sessions.js`: `closeMobileSidebar()` called after session click.
- 69 new CSS lines in `@media(max-width:640px)` block.
- New files: `Dockerfile`, `docker-compose.yml`, `.dockerignore`.

---

## [v0.22] Sprint 20 -- Voice Input + Send Button Polish
*April 3, 2026 | 415 tests*

### Features
- **Voice input via Web Speech API.** Microphone button in the composer.
  Tap to start recording, tap again (or send) to stop. Live interim
  transcription appears in the textarea. Auto-stops after ~2s of silence.
  Final text stays editable before sending. Appends to existing textarea
  content rather than replacing it. Button hidden when browser doesn't
  support Web Speech API. No API keys, no external libraries, no server
  changes. Works in Chrome, Edge, Safari (partial). Firefox unsupported
  (button stays hidden).
- **Send button polish.** Send button redesigned as a 34px icon-only circle
  with upward arrow SVG. Hidden by default — appears with pop-in spring
  animation when textarea has content or files are attached. Disappears
  on send or when content is cleared. Hidden while agent is responding.
  Blue fill (#7cb9ff) with glow, scale hover/active for tactile feedback.

### Architecture
- Voice input IIFE in `boot.js`: SpeechRecognition lifecycle with
  `continuous=false`, `interimResults=true`, error handling via `showToast()`.
- `_prefix` variable snapshots existing textarea content on recording start
  so dictation appends rather than overwrites.
- `btnSend.onclick` stops active recognition before sending (send guard).
- CSS: `.mic-btn`, `.mic-btn.recording` (red pulse), `.mic-status`,
  `.mic-dot`, `@keyframes mic-pulse`.
- `updateSendBtn()` in `ui.js` tracks textarea content, pending files,
  and busy state. Hooked into `setBusy()`, `renderTray()`, `autoResize()`,
  and input event listener.
- CSS: `.send-btn` redesigned (circle, glow), `.send-btn.visible` +
  `@keyframes send-pop-in` (spring animation).

### Tests
- 52 new tests in `test_sprint20.py`: voice input HTML, CSS, JS, append
  behaviour, error handling, regressions.
- 33 new tests in `test_sprint20b.py`: send button HTML, CSS, JS,
  animation, visibility logic, regressions. Total: **415 tests**.

---

## [v0.21] Sprint 19 -- Auth + Security Hardening
*April 3, 2026 | 328 tests*

### Features
- **Password authentication (Issue #23).** Optional password auth, off by default.
  Enable via `HERMES_WEBUI_PASSWORD` env var or Settings panel. Password-only
  (single-user app). Signed HMAC HTTP-only cookie with 24h TTL. Minimal dark-themed
  login page at `/login`. API calls without auth return 401; page loads redirect.
  New `api/auth.py` module with hashing, verification, session management.
- **Security headers.** All responses now include `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`.
- **POST body size limit.** Non-upload POST bodies capped at 20MB via `read_body()`.
- **Settings panel additions.** "Access Password" field and "Sign Out" button
  (only visible when auth is active).

### Architecture
- New `api/auth.py`: password hashing (SHA-256 + STATE_DIR salt), signed cookies,
  auth middleware, public path allowlist.
- Auth check in `server.py` do_GET/do_POST before routing.
- `password_hash` added to `_SETTINGS_DEFAULTS`.

### Tests
- 10 new tests in `test_sprint19.py`: auth status, login flow, security headers,
  cache-control, settings password field, request size limit. Total: **328 tests (328 passing)**.

---

## [v0.20] Sprint 18 -- File Preview Auto-Close + Thinking Display + Workspace Tree
*April 3, 2026 | 318 tests*

### Features
- **File preview auto-close on directory navigation.** When viewing a file in
  the right panel and navigating directories (breadcrumbs, up button, folder
  clicks), the preview now automatically closes instead of showing stale
  content. `clearPreview()` extracted as named function and called from
  `loadDir()`. Unsaved preview edits prompt for confirmation before discarding.
- **Thinking/reasoning display.** Assistant messages with structured content
  arrays containing `type:'thinking'` or `type:'reasoning'` blocks (Claude
  extended thinking, o3 reasoning) now render as collapsible gold-themed cards
  above the response text. Collapsed by default. Click the header to expand and
  see the model's reasoning process. Uses `esc()` on all content for XSS safety.
- **Workspace tree view (Issue #22).** Directories expand/collapse in-place
  with toggle arrows. Single-click toggles a directory open/closed. Double-click
  navigates into it (breadcrumb view). Subdirectory contents fetched lazily from
  the API and cached in `S._dirCache`. Nesting depth shown via indentation.
  Empty directories show "(empty)" placeholder. Breadcrumb navigation still
  works alongside the tree view.

### Bug Fixes
- **Stale tree cache on session switch.** `S._dirCache` and `S._expandedDirs`
  are now cleared when navigating to the root directory, preventing session B
  from showing session A's cached file listings.
- **clearPreview() discards unsaved edits.** Navigation now checks
  `_previewDirty` and prompts before discarding unsaved preview changes.

### Architecture
- `clearPreview()` extracted from inline handler to named function in `boot.js`.
- Thinking card styles added to `style.css` (gold-themed, collapsible).
- Tree toggle and empty-directory styles added to `style.css`.

---

## [v0.19] Sprint 17 -- Workspace Polish + Slash Commands + Settings
*April 3, 2026 | 318 tests*

### Features
- **Workspace breadcrumb navigation.** Clicking into subdirectories now shows a
  breadcrumb path bar (e.g. `~ / src / components`) with clickable segments to
  navigate back. An "up" button appears in the panel header when inside a
  subdirectory. File operations (rename, delete, new file/folder) stay in the
  current directory instead of jumping back to root. Foundation for Issue #22
  (tree view).
- **Slash commands.** Type `/` in the composer to see an autocomplete dropdown
  of built-in commands. New `commands.js` module with command registry. Built-in
  commands: `/help`, `/clear`, `/model <name>`, `/workspace <name>`, `/new`.
  Arrow keys navigate, Tab/Enter select, Escape closes. Unrecognized commands
  pass through to the agent normally.
- **Send key setting (Issue #26).** New setting in Settings panel to choose
  between Enter (default) and Ctrl/Cmd+Enter as the send key. Persisted to
  `settings.json` via the existing settings API. Setting loads on boot.
  Server-side validation ensures only valid values (`enter`, `ctrl+enter`).

### Architecture
- New `static/commands.js` module (7th JS module): command registry, parser,
  autocomplete dropdown, and built-in command handlers.
- `send_key` added to `_SETTINGS_DEFAULTS` in `api/config.py` with enum validation
  (`_SETTINGS_ENUM_VALUES` rejects unknown values server-side).
- `S.currentDir` state tracking added to `ui.js` for workspace navigation.

### Tests
- 6 new tests in `test_sprint17.py`: send_key default, round-trip save with
  cleanup, invalid value rejection, unknown key ignored, commands.js served,
  workspace root listing. Total: **318 passed**.

---

## [v0.18.1] Safe HTML Rendering + Sprint 16 Tests
*April 2, 2026 | 289 tests*

### Features
- **Safe HTML rendering in AI responses.** AI models sometimes emit HTML tags
  (`<strong>`, `<em>`, `<code>`, `<br>`) in their responses. Previously these
  showed as literal escaped text. A new pre-pass in `renderMd()` converts safe
  HTML tags to markdown equivalents before the pipeline runs. Code blocks and
  backtick spans are stashed first so their content is never touched.
- **`inlineMd()` helper.** New function for processing inline formatting inside
  list items, blockquotes, and headings. The old code called `esc()` directly,
  which escaped tags that had already been converted by the pre-pass.
- **Safety net.** After the full pipeline, any HTML tags not in the output
  allowlist (`SAFE_TAGS`) are escaped via `esc()`. XSS fully blocked -- 7
  attack vectors tested.
- **Active session gold style.** Active session uses gold/amber (`#e8a030`)
  instead of blue, matching the logo gradient. Project border-left skipped
  when active (gold always wins).

### Tests
- **74 new tests** in `test_sprint16.py`: static analysis (6), behavioral (10),
  exact regression (1), XSS security (7), edge cases (51). Total: 289 passed.

---

## [v0.18] Sprint 16 -- Session Sidebar Visual Polish
*April 2, 2026 | 237 tests*

### Features
- **SVG action icons.** Replaced all emoji HTML entities (star, folder, box,
  duplicate, trash) with monochrome SVG line icons that inherit `currentColor`.
  Consistent rendering across macOS, Linux, and Windows. Defined in a top-level
  `ICONS` constant in `sessions.js`.
- **Action buttons overlay.** All session action buttons (pin, move, archive,
  duplicate, trash) wrapped in a `.session-actions` container with
  `position:absolute`. Titles now use full available width instead of being
  truncated by invisible buttons. Actions appear on hover with a gradient fade
  from the right edge. Overlay auto-hides during inline rename via
  `:has(.session-title-input)`.
- **Pin indicator.** Small gold filled-star icon rendered inline before the
  title only when pinned. Unpinned sessions get full title width with zero
  space reservation.
- **Project border indicator.** Sessions assigned to a project show a colored
  left border matching the project color, replacing the old always-visible
  blue folder button.

### Bug Fixes
- **Session title truncation.** Action icons reserved ~30px of space even when
  invisible, truncating titles. Fixed by overlay container approach.
- **Folder button felt sticky.** Replaced `.has-project` persistent blue button
  with colored left border. Folder button now only appears in hover overlay.

---

## [v0.17.3] Bug Fixes
*April 2, 2026*

### Bug Fixes
- **NameError crash in model discovery.** `logger.debug()` was called in the
  custom endpoint `except` block in `config.py`, but `logger` was never
  imported. Every failed custom endpoint fetch crashed with `NameError`,
  returning HTTP 500 for `/api/models`. Replaced with silent `pass` since
  unreachable endpoints are expected. (PR #24)
- **Project picker clipping and width.** Picker was clipped by
  `overflow:hidden` on ancestor elements. Width calculation improved with
  dynamic sizing (min 160px, max 220px). Event listener `close` handler
  moved after DOM append to fix reference-before-definition. Reordered
  `picker.remove()` before `removeEventListener` for correct cleanup. (PR #25)

---

## [v0.17.2] Model Update
*April 2, 2026*

### Enhancements
- **GLM-5.1 added to Z.AI model list.** New model available in the dropdown
  for Z.AI provider users. (Fixes #17)

---

## [v0.17.1] Security + Bug Fixes
*April 2, 2026 | 237 tests*

### Security
- **Path traversal in static file server.** `_serve_static()` now sandboxes
  resolved paths inside `static/` via `.relative_to()`. Previously
  `GET /static/../../.hermes/config.yaml` could expose API keys.
- **XSS in markdown renderer.** All captured groups in bold, italic, headings,
  blockquotes, list items, table cells, and link labels now run through `esc()`
  before `innerHTML` insertion.
- **Skill category path traversal.** Category param validated to reject `/`
  and `..` to prevent writing outside `~/.hermes/skills/`.
- **Debug endpoint locked to localhost.** `/api/approval/inject_test` returns
  404 to any non-loopback client.
- **CDN resources pinned with SRI hashes.** PrismJS and Mermaid tags now have
  `integrity` + `crossorigin` attributes. Mermaid pinned to `@10.9.3`.
- **Project color CSS injection.** Color field validated against
  `^#[0-9a-fA-F]{3,8}$` to prevent `style.background` injection.
- **Project name length limit.** Capped at 128 chars, empty-after-strip rejected.

### Bug Fixes
- **OpenRouter model routing regression.** `resolve_model_provider()` was
  incorrectly stripping provider prefixes from OpenRouter model IDs (e.g.
  `openai/gpt-5.4-mini` became `gpt-5.4-mini` with provider `openai`),
  causing AIAgent to look for OPENAI_API_KEY and crash. Fix: only strip
  prefix when `config.provider` explicitly matches that direct-API provider.
- **Project picker invisible.** Dropdown was clipped by `.session-item`
  `overflow:hidden`. Now appended to `document.body` with `position:fixed`.
- **Project picker stretched full width.** Added `max-width:220px;
  width:max-content` to constrain the fixed-positioned picker.
- **No way to create project from picker.** Added "+ New project" item at
  the bottom of the picker dropdown.
- **Folder button undiscoverable.** Now shows persistently (blue, 60%
  opacity) when session belongs to a project.
- **Picker event listener leak.** `removeEventListener` added to all picker
  item onclick handlers.
- **Redundant sys.path.insert calls removed.** Two cron handler imports no
  longer prepend the agent dir (already on sys.path via config.py).

---

## [v0.17] Sprint 15 -- Session Projects + Code Copy + Tool Card Toggle
*April 1, 2026 | 237 tests*

### Features
- **Session projects.** Named groups for organizing sessions. A project filter
  bar (subtle chips) sits between the search input and the session list. Each
  project has a name and color. Click a chip to filter; "All" shows everything.
  Create inline (+), rename (double-click), delete (right-click). Assign sessions
  via folder icon button with dropdown picker. Projects stored in `projects.json`.
  Session model gains `project_id` field. 5 new API endpoints.
- **Code block copy button.** Every code block gets a "Copy" button in the
  language header bar (or top-right for plain blocks). Click copies to clipboard,
  shows "Copied!" for 1.5s.
- **Tool card expand/collapse.** When a message has 2+ tool cards, "Expand all /
  Collapse all" toggle appears above the card group.

---

## [v0.16.2] Model List Updates + base_url Passthrough
*April 1, 2026 | 247 tests*

### Bug Fixes
- **MiniMax model list updated.** Replaced stale ABAB 6.5 models with current
  MiniMax-M2.7, M2.7-highspeed, M2.5, M2.5-highspeed, M2.1 lineup matching
  hermes-agent upstream. (Fixes #6)
- **Z.AI/GLM model list updated.** Replaced GLM-4 series with current GLM-5,
  GLM-5 Turbo, GLM-4.7, GLM-4.5, GLM-4.5 Flash lineup.
- **base_url passthrough to AIAgent.** `resolve_model_provider()` now reads
  `base_url` from config.yaml and passes it to AIAgent, so providers with
  custom endpoints (MiniMax, Z.AI, local LLMs) route to the correct API.

---

## [v0.16.1] Community Fixes -- Mobile + Auth + Provider Routing
*April 1, 2026 | 247 tests*

Community contributions from @deboste, reviewed and refined.

### Bug Fixes
- **Mobile responsive layout.** Comprehensive `@media(max-width:640px)` rules
  for topbar, messages, composer, tool cards, approval cards, and settings modal.
  Uses `100dvh` with `100vh` fallback to fix composer cutoff on mobile browsers.
  Textarea `font-size:16px` prevents iOS/Android auto-zoom on focus.
- **Reverse proxy basic auth support.** All `fetch()` and `EventSource` URLs now
  constructed via `new URL(path, location.origin)` to strip embedded credentials
  per Fetch spec. `credentials:'include'` on fetch, `withCredentials:true` on
  EventSource ensure auth headers are forwarded through reverse proxies.
- **Model provider routing.** New `resolve_model_provider()` helper in
  `api/config.py` strips provider prefix from dropdown model IDs (e.g.
  `anthropic/claude-sonnet-4.6` → `claude-sonnet-4.6`) and passes the correct
  `provider` to AIAgent. Handles cross-provider selection by matching against
  known direct-API providers.

---

## [v0.16] Sprint 14 -- Visual Polish + Workspace Ops + Session Organization
*March 30, 2026 | 233 tests*

### Features
- **Mermaid diagram rendering.** Code blocks tagged `mermaid` render as
  diagrams inline. Mermaid.js loaded lazily from CDN on first encounter.
  Dark theme with matching colors. Falls back to code block on parse error.
- **Message timestamps.** Subtle HH:MM time next to each role label. Full
  date/time on hover tooltip. User messages get `_ts` field when sent.
- **File rename.** Double-click any filename in workspace panel to rename
  inline. `POST /api/file/rename` endpoint with path traversal protection.
- **Folder create.** Folder icon button in workspace panel header. Prompt
  for name, `POST /api/file/create-dir` endpoint.
- **Session tags.** Add `#tag` to session titles. Tags shown as colored
  chips in sidebar. Click a tag to filter the session list.
- **Session archive.** Archive icon on each session. Archived sessions
  hidden by default; "Show N archived" toggle at top of list. Backend
  `POST /api/session/archive` with `archived` field on Session model.

### Bug Fixes
- **Date grouping fix.** Session list groups (Today/Yesterday/Earlier) now
  use `created_at` instead of `updated_at`, preventing sessions from jumping
  between groups when auto-titling touches `updated_at`.

---

## [v0.15] Sprint 13 -- Alerts + Session QoL + Polish
*March 30, 2026 | 221 tests*

### Features
- **Cron completion alerts.** New `GET /api/crons/recent` endpoint. UI polls every
  30s (pauses when tab is hidden). Toast notification per completion with status icon.
  Red badge count on Tasks nav tab, cleared when tab is opened.
- **Background agent error alerts.** When a streaming session errors out and the user
  is viewing a different session, a persistent red banner appears above the messages:
  "Session X has encountered an error." View button navigates, Dismiss clears.
- **Session duplicate.** Copy icon on each session in the sidebar (visible on hover).
  Creates a new session with the same workspace and model, titled "(copy)".
- **Browser tab title.** `document.title` updates to show the active session title
  (e.g. "My Task -- Hermes"). Resets to "Hermes" when no session is active.

### Bug Fixes
- Click guard added for duplicate button to prevent accidental session navigation.

---

## [v0.14] Sprint 12 -- Settings Panel + Reliability + Session QoL
*March 30, 2026 | 211 tests*

### Features
- **Settings panel.** Gear icon in topbar opens slide-in overlay. Persist default
  model and workspace server-side in `settings.json`. Server reads on startup.
- **SSE auto-reconnect.** When EventSource drops mid-stream, attempts one reconnect
  using the same stream_id after 1.5s. Shared `_wireSSE()` function eliminates
  handler duplication.
- **Pin sessions.** Star icon on each session. Pinned sessions float to top of sidebar
  under a gold "Pinned" header. Persisted in session JSON.
- **Import session from JSON.** Upload button in sidebar. Creates new session with
  fresh ID from exported JSON file.

### Bug Fixes
- `models.py` uses `_cfg.DEFAULT_MODEL` module reference so `save_settings()` changes
  take effect for `new_session()`.
- Full-scan fallback sort in `all_sessions()` now accounts for pinned sessions.
- `save_settings()` whitelists known keys only, rejecting arbitrary data.
- Escape key closes settings overlay.

---

## [v0.13] Sprint 11 -- Multi-Provider Models + Streaming Smoothness
*March 30, 2026 | 201 tests*

### Features
- **Multi-provider model support.** New `GET /api/models` endpoint discovers configured
  providers from `config.yaml`, `auth.json`, and API key environment variables. The model
  dropdown now populates dynamically from whatever providers the user has set up (Anthropic,
  OpenAI, Google, DeepSeek, Nous Portal, OpenRouter, etc.). Falls back to the hardcoded
  OpenRouter list when no providers are detected. Sessions with unlisted models auto-add
  them to the dropdown.
- **Smooth scroll pinning.** During streaming, auto-scroll only when the user is near the
  bottom of the message area. If the user scrolls up to read earlier content, new tokens
  no longer yank them back down. Pinning resumes when they scroll back to the bottom.

### Architecture
- **Routes extracted to api/routes.py.** All 49 GET/POST route handlers moved from server.py
  into `api/routes.py` (802 lines). server.py is now a 76-line thin shell: Handler class
  with structured logging, dispatch to `handle_get()`/`handle_post()`, and `main()`.
  Completes the server split started in Sprint 10.
- **Cleaned up duplicate dead-code routes** that existed in the old `do_GET` (skills/save,
  skills/delete, memory/write were duplicated in both GET and POST handlers).

### Bug Fixes
- Regression tests updated for new route module structure.

---

## [v0.12.2] Concurrency + Correctness Sweeps
*March 31, 2026 | 190 tests*

Two systematic audits of all concurrent multi-session scenarios. Each finding
became a regression test so it cannot silently return.

### Sweep 1 (R10-R12)
- **R10: Approval response to wrong session.** `respondApproval()` used
  `S.session.session_id` -- whoever you were viewing. If session A triggered
  a dangerous command requiring approval and you switched to B then clicked
  Allow, the approval went to B's session_id. Agent on A stayed stuck. Fixed:
  approval events tag `_approvalSessionId`; `respondApproval()` uses that.
- **R11: Activity bar showed cross-session tool status.** Session A's tool
  name appeared in session B's activity bar while you were viewing B. Fixed:
  `setStatus()` in the tool SSE handler is now inside the `activeSid` guard.
- **R12: Live tool cards vanished on switch-away and back.** Switching back to
  an in-flight session showed empty live cards even though tools had fired.
  Fixed: `loadSession()` INFLIGHT branch now restores cards from `S.toolCalls`.

### Sweep 2 (R13-R15)
- **R13: Settled tool cards never rendered after response completes.**
  `renderMessages()` has a `!S.busy` guard on tool card rendering. It was
  called with `S.busy=true` in the done handler -- tool cards were skipped
  every time. Fixed: `S.busy=false` set inline before `renderMessages()`.
- **R14: Wrong model sent for sessions with unlisted model.** `send()` used
  `$('modelSelect').value` which could be stale if the session's model isn't
  in the dropdown. Fixed: now uses `S.session.model || $('modelSelect').value`.
- **R15: Stale live tool cards in new sessions.** `newSession()` didn't call
  `clearLiveToolCards()`. Fixed.

---

## [v0.12.1] Sprint 10 Post-Release Fixes
*March 31, 2026 | 177 tests*

Critical regressions introduced during the server.py split, caught by users and fixed immediately.

- **`uuid` not imported in server.py** -- `chat/start` returned 500 (NameError) on every new message
- **`AIAgent` not imported in api/streaming.py** -- agent thread crashed immediately, SSE returned 404
- **`has_pending` not imported in api/streaming.py** -- NameError during tool approval checks
- **`Session.__init__` missing `tool_calls` param** -- 500 on any session with tool history
- **SSE loop did not break on `cancel` event** -- connection hung after cancel
- **Regression test file added** (`tests/test_regressions.py`): 10 tests, one per introduced bug. These form a permanent regression gate so each class of error can never silently return.

---

## [v0.12] Sprint 10 -- Server Health + Operational Polish
*March 31, 2026 | 167 tests*

### Post-sprint Bug Fixes
- SSE loop now breaks on `cancel` event (was hanging after cancel)
- `setBusy(false)` now always hides the Cancel button
- `S.activeStreamId` properly initialized in the S global state object
- Tool card "Show more" button uses data attributes instead of inline JSON.stringify (XSS/parse safety)
- Version label updated to v0.2
- `Session.__init__` accepts `**kwargs` for forward-compatibility with future JSON fields
- Test cron jobs now isolated via `HERMES_HOME` env var in conftest (no more pollution of real jobs.json)
- `last_workspace` reset after each test in conftest (prevents workspace state bleed between tests)
- Tool cards now grouped per assistant turn instead of piled before last message
- Tool card insertion uses `data-msg-idx` attribute correctly (was `msgIdx`, matching HTML5 dataset API)

### Architecture
- **server.py split into api/ modules.** 1,150 lines -> 673 lines in server.py.
  Extracted modules: `api/config.py` (101), `api/helpers.py` (57), `api/models.py` (114),
  `api/workspace.py` (77), `api/upload.py` (77), `api/streaming.py` (187).
  server.py is now the thin routing shell only. All business logic is independently importable.

### Features
- **Background task cancel.** Red "Cancel" button appears in the activity bar while a task
  is running. Calls `GET /api/chat/cancel?stream_id=X`. The agent thread receives a cancel
  event, emits a 'cancel' SSE event, and the UI shows "*Task cancelled.*" in the conversation.
  Note: a tool call already in progress (e.g. a long terminal command) completes before
  the cancel takes effect -- same behavior as CLI Ctrl+C.
- **Cron run history viewer.** Each job in the Tasks panel now has an "All runs" button.
  Click to expand a list of up to 20 past runs with timestamps, each collapsible to show
  the full output. Click again to hide.
- **Tool card UX polish.** Three improvements:
  1. Pulsing blue dot on cards for in-progress tools (distinct from completed cards)
  2. Smart snippet truncation at sentence boundaries instead of hard byte cutoff
  3. "Show more / Show less" toggle on tool results longer than 220 chars

---

## [v0.11] Sprint 9 -- Codebase Health + Daily Driver Gaps
*March 31, 2026 | 149 tests*

The sprint that closed the last gaps for heavy agentic use.

### Architecture
- **app.js replaced by 6 modules.** `app.js` is deleted. The browser now loads 6 focused files:
  `ui.js` (530), `workspace.js` (132), `sessions.js` (189), `messages.js` (221),
  `panels.js` (555), `boot.js` (142). The modules are a superset of the original app.js
  (two functions -- `loadTodos`, `toolIcon` -- were added directly to the modules after the split).
  No single file exceeds 555 lines.

### Features
- **Tool call cards inline.** Every tool Hermes uses now appears as a collapsible card
  in the conversation between the user message and the response. Live during streaming,
  restored from session history on reload. Shows tool name, preview, args, result snippet.
- **Attachment metadata persists on reload.** File badges on user messages survive page
  refresh. Server stores filenames on the user message in session JSON.
- **Todo list panel.** New task-list tab in the sidebar. Shows current task list parsed
  from the most recent todo tool result in message history. Status icons use Lucide
  square, loader, check, and x states. Auto-refreshes when panel is active.
- **Model preference persists.** Last-used model saved to localStorage. Restored on page
  load. New sessions inherit it automatically.

### Bug Fixes
- Tool card toggle arrow only shown when card has expandable content
- Attachment tagging matches by message content to avoid wrong-turn tagging
- SSE tool event was missing `args` field
- `/api/session` GET was not returning `tool_calls` (history lost on reload)

---

## [v0.10] Sprint 8 -- Daily Driver Finish Line
*March 31, 2026 | 139 tests*

### Features
- **Edit user message + regenerate.** Hover any user bubble, click the pencil icon.
  Inline textarea, Enter submits, Escape cancels. Truncates session at that point and re-runs.
- **Regenerate last response.** Retry icon on the last assistant bubble only.
- **Clear conversation.** "Clear" button in topbar. Wipes messages, keeps session slot.
- **Syntax highlighting.** Prism.js via CDN (deferred). Python, JS, bash, JSON, SQL and more.

### Bug Fixes
- Reconnect banner false positive on normal loads (90-second window)
- Session list clipping on short screens
- Favicon 404 console noise (server now returns 204)
- Edit textarea auto-resize on open
- Send button guard while inline edit is active
- Escape closes dropdown, clears search, cancels active edit
- Approval polling not restarted on INFLIGHT session switch-back
- Version label updated to v0.10

### Hotfix: Message Queue + INFLIGHT
- **Message queue.** Sending while busy queues the message with toast + badge.
  Drains automatically on completion. Cleared on session switch.
- **Message stays visible on switch-away/back.** loadSession checks INFLIGHT before
  server fetch, so sent message and thinking dots persist correctly.

---

## [v0.9] Sprint 7 -- Wave 2 Core: CRUD + Search
*March 31, 2026 | 125 tests*

### Features
- **Cron edit + delete.** Inline edit form per job, save and delete with confirmation.
- **Skill create, edit, delete.** "+ New skill" form in Skills panel. Writes to `~/.hermes/skills/`.
- **Memory inline edit.** "Edit" button opens textarea for MEMORY.md. Saves via `/api/memory/write`.
- **Session content search.** Filter box searches message text (up to 5 messages per session)
  in addition to titles. Debounced API call, results appended below title matches.

### Architecture
- `/health` now returns `active_streams` and `uptime_seconds`
- `git init` on `<repo>/`, pushed to GitHub

### Bug Fixes
- Activity bar overlap on short viewports
- Model chip stale after session switch
- Cron output overflow in tasks panel

---

## [v0.8] Sprint 6 -- Polish + Phase E Complete
*March 31, 2026 | 106 tests*

### Architecture
- **Phase E complete.** HTML extracted to `static/index.html`. server.py now pure Python.
  Line count progression: 1778 (Sprint 1) → 1042 (Sprint 5) → 903 (Sprint 6).
- **Phase D complete.** All endpoints validated with proper 400/404 responses.

### Features
- **Resizable panels.** Sidebar and workspace panel drag-resizable. Widths persisted to localStorage.
- **Create cron job from UI.** "+ New job" form in Tasks panel with name, schedule, prompt, delivery.
- **Session JSON export.** Downloads full session as JSON via "JSON" button in sidebar footer.
- **Escape from file editor.** Cancels inline file edit without saving.

---

## [v0.7] Sprint 5 -- Phase A Complete + Workspace Management
*March 30, 2026 | 86 tests*

### Architecture
- **Phase A complete.** JS extracted to `static/app.js`. server.py: 1778 → 1042 lines.
- **LRU session cache.** `collections.OrderedDict` with cap of 100, oldest evicted automatically.
- **Session index.** `sessions/_index.json` for O(1) session list loads.
- **Isolated test server.** Port 8788 with own state dir, conftest autouse cleanup.

### Features
- **Workspace management panel.** Add/remove/rename workspaces. Persisted to `workspaces.json`.
- **Topbar workspace quick-switch.** Dropdown chip lists all workspaces, switches on click.
- **New sessions inherit last workspace.** `last_workspace.txt` tracks last used.
- **Copy message to clipboard.** Hover icon on each bubble with checkmark confirmation.
- **Inline file editor.** Preview any file, click Edit to modify, Save writes to disk.

---

## [v0.6] Sprint 4 -- Relocation + Session Power Features
*March 30, 2026 | 68 tests*

### Architecture
- **Source relocated** to `<repo>/` outside the hermes-agent git repo.
  Safe from `git pull`, `git reset`, `git stash`. Symlink maintained at `hermes-agent/webui-mvp`.
- **CSS extracted (Phase A start).** All CSS moved to `static/style.css`.
- **Per-session agent lock (Phase B).** Prevents concurrent requests to same session from
  corrupting environment variables.

### Features
- **Session rename.** Double-click any title in sidebar to edit inline. Enter saves, Escape cancels.
- **Session search/filter.** Live client-side filter box above session list.
- **File delete.** Hover trash icon on workspace files. Confirm dialog.
- **File create.** "+" button in workspace panel header.

---

## [v0.5] Sprint 3 -- Panel Navigation + Feature Viewers
*March 30, 2026 | 48 tests*

### Features
- **Sidebar panel navigation.** Four tabs: Chat, Tasks, Skills, Memory. Lazy-loads on first open.
- **Tasks panel.** Lists scheduled cron jobs with status badges. Run now, Pause, Resume.
  Shows last run output automatically.
- **Skills panel.** All skills grouped by category. Search/filter. Click to preview SKILL.md.
- **Memory panel.** Renders MEMORY.md and USER.md as formatted markdown with timestamps.

### Bug Fixes
- B6: New session inherits current workspace
- B10: Tool events replace thinking dots (not stacked alongside)
- B14: Cmd/Ctrl+K creates new chat from anywhere

---

## [v0.4] Sprint 2 -- Rich File Preview
*March 30, 2026 | 27 tests*

### Features
- **Image preview.** PNG, JPG, GIF, SVG, WEBP displayed inline in workspace panel.
- **Rendered markdown.** `.md` files render as formatted HTML in the preview panel.
- **Table support.** Pipe-delimited markdown tables render as HTML tables.
- **Smart file icons.** Type-appropriate icons by extension in the file tree.
- **Preview path bar with type badge.** Colored badge shows file type.

---

## [v0.3] Sprint 1 -- Bug Fixes + Foundations
*March 30, 2026 | 19 tests*

The first sprint. Established the test suite, fixed critical bugs.

### Bug Fixes
- B1: Approval card now shows pattern keys
- B2: File input accepts valid types only
- B3: Model chip label correct for all 10 models (replaced substring check with dict)
- B4/B5: Reconnect banner on mid-stream reload (localStorage inflight tracking)
- B7: Session titles no longer overflow sidebar
- B9: Empty assistant messages no longer render as blank bubbles
- B11: `/api/session` GET returns 400 (not silent session creation) when ID missing

### Architecture
- Thread lock on SESSIONS dict
- Structured JSON request logging
- 10-model dropdown with 3 provider groups (OpenAI, Anthropic, Other)
- First test suite: 19 HTTP integration tests

---

## [v0.2] UI Polish Pass
*March 30, 2026*

Visual audit via screenshot analysis. No new features -- design refinement only.

- Nav tabs: icon-only with CSS tooltip (5 tabs, no overflow)
- Session list: grouped by Today / Yesterday / Earlier
- Active session: blue left border accent
- Role labels: Title Case, softened color, circular icons
- Code blocks: connected language header with separator
- Send button: gradient + hover lift
- Composer: blue glow ring on focus
- Toast: frosted glass with float animation
- Tool status moved from composer footer to activity bar above composer
- Empty session flood fixed (filter + cleanup endpoint + test autouse)

---

## [v0.1] Initial Build
*March 30, 2026*

Single-file web UI for Hermes. stdlib HTTP server, no external dependencies.
Three-panel layout: sessions sidebar, chat area, workspace panel.

**Core capabilities:**
- Send messages, receive SSE-streamed responses
- Session create/load/delete, auto-title from first message
- File upload with manual multipart parser
- Workspace file tree with directory navigation
- Tool approval card (4 choices: once, session, always, deny)
- INFLIGHT session-switch guard
- 10-model dropdown (OpenAI, Anthropic, Other)
- SSH tunnel access on port 8787

---

*Last updated: v0.36, April 5, 2026 | Tests: 433*

### Markdown sweep
- ROADMAP.md, TESTING.md, SPRINTS.md, README.md, and THEMES.md refreshed to match v0.36 and 433 tests.
