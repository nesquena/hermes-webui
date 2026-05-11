# Hermes Fork Patches — Maintenance Reference

This document is the **single source of truth** for every customization
HermesOS Cloud carries on top of `nesquena/hermes-webui` upstream. Read this
first before doing an upstream pull — every fork addition lives inside a
labelled marker block so conflicts (if any) are localised.

If you're reading this after a daily Track Upstream rebase produced a
conflict-resolution PR, jump straight to the **Re-merge runbook** at the
bottom.

---

## 1. Fork commit timeline (newest → oldest)

| SHA | Subject | Files |
|---|---|---|
| _staged_ | fix(providers): runtime auth must not see UI-only auto-detected slug (+ Layer A regression tests) | `api/config.py`, `tests/test_hermes_fork_provider_resolution.py` |
| _staged_ | fix(ui): chat-list streaming spinner stays circular under HermesOS skin | `static/style.css` |
| _staged_ | fix(sidebar): widen collapsed strip to 32px + border-left for browser-sidebar-visible users | `static/style.css` |
| `37507d8` | fix(sidebar): capture-phase click delegate so the chevron actually toggles | `static/boot.js` |
| `0026fc5` | fix(sidebar): kill button-in-strip awkwardness — whole strip is the click target | `static/boot.js`, `static/style.css` |
| `6134d3a` | feat(providers): auto-name custom base_url groups (no config.yaml needed) | `api/config.py` |
| `21a4075` | revert(model-chip): drop model-name-prefix extraction, show routing provider | `static/panels.js` |
| `060b982` | fix(sidebar): subtler thin-strip collapsed state | `static/style.css` |
| `608ae3e` | fix(sidebar): collapse to thin strip with re-open chevron (not fully hidden) | `static/boot.js`, `static/style.css` |
| `3d9559f` | feat(profile): nickname renders in Profiles panel too (cards + detail header) | `static/panels.js` |
| `8a1e013` | docs: ctx-circle preservation + default-profile nickname fork-patch entries | `HERMES_FORK_PATCHES.md` |
| `a5a507a` | fix(profile): route the OTHER two profileChipLabel writers through nickname helper | `static/ui.js` |
| `53d59b1` | feat(ui): context-window indicator stays circular + default-profile nickname | `static/boot.js`, `static/panels.js`, `static/style.css`, `static/i18n.js` |
| `71ba1cd` | fix(skin): add hermesos + sienna to server-side `_SETTINGS_SKIN_VALUES` | `api/config.py` |
| `3585621` | fix(skin): bump rebrand migration to v3 — re-pin everyone to HermesOS | `static/index.html` |
| `fd12305` | fix(boot): name-collision with panels.js `_origSwitchPanel` — broke sessions | `static/boot.js` |
| `04b54d2` | docs: note ui.js providerChip + boot.js sidebar-collapse fork patches | `HERMES_FORK_PATCHES.md` |
| `d3d7733` | feat(ui): collapsible chat sidebar + smarter provider chip on model rows | `static/boot.js`, `static/panels.js`, `static/style.css`, `static/index.html`, `static/i18n.js` |
| `a7bfbfb` | fix(skin): bump rebrand migration to v2 so canary'd users get re-forced | `static/index.html` |
| `969235d` | feat(iframe): bake the bearer-from-hash shim into the WebUI bundle | `static/iframe-shim.js`, `static/index.html`, `static/sw.js` |
| `b6f75d4` | fix(skin): scope titlebar-hide selector so layout test regex still matches | `static/style.css` |
| `39b23e1` | fix(bootstrap): defer sentinel write until clean stream done | `api/streaming.py` |
| `2e9a5d5` | feat(bootstrap): first-run identity-discovery prompt on WebUI surface | `api/bootstrap.py`, `api/streaming.py` |
| `9e3310f` | feat(skin): drop app titlebar + force-rebrand legacy users to HermesOS | `static/style.css`, `static/index.html` |
| `c2680f7` | fix(skin): kill hardcoded navy gradients leaking into HermesOS palette | `static/style.css` |
| `facc029` | feat(skin): kill ALL rounded corners site-wide for HermesOS skin | `static/style.css` |
| `78928f8` | feat(skin): HermesOS as the default + Appearance picker tile | `static/boot.js`, `static/index.html` |
| `03c99a4` | feat(skin): HermesOS re-skin — vellum/ink/gold palette + sharp corners | `static/style.css` |
| `34accbb` | docs: HERMES_FORK_PATCHES.md as master maintenance reference | `HERMES_FORK_PATCHES.md` |
| `a3e58cf` | fix(settings): refresh-models toast — i18n + honest empty result | `static/panels.js`, `static/i18n.js` |
| `74fdb52` | fix(settings): switch provider logos to DuckDuckGo icon CDN | `api/providers.py`, `static/provider-logos/*` (deleted) |
| `6f86235` | fix(settings): wire up `/api/models/refresh` + drop fabricated model lists | `api/routes.py`, `api/config.py` |
| `b557e86` | fix(settings): close Settings before Authenticate + Nous Portal logo | `api/providers.py`, `static/panels.js` |
| `c4e10e5` | chore(settings): add Nous Portal to Recommended | `static/panels.js` |
| `8a741e5` | fix(settings): Recommended order — Venice first, drop Gemini/Xiaomi | `static/panels.js` |
| `4b9bc9e` | feat(settings): real provider logos + Recommended/All/OAuth tabs | `api/providers.py`, `static/panels.js`, `static/style.css` |
| `f0d5ba7` | feat(settings): curated model lists for first-class providers | `api/config.py` (now empty lists per `6f86235`) |
| `818a0bc` | fix(settings): i18n strings, drop CometAPI, switch to glyph logos | `api/providers.py`, `api/routes.py`, `static/i18n.js`, `static/panels.js`, `static/style.css`, `api/config.py` |
| `5a20763` | fix(settings): simplify provider-logo fallback | `static/panels.js`, `static/style.css` |
| `7457595` | feat(settings): first-class providers + logos + STT + OAuth | initial Settings UI commit, 6 files |
| `7569818` | ci: auto-build `:stable` on push to main | `.github/workflows/hermesdeploy-image.yml` |
| `6cb4282` | ci: daily upstream-tracking workflow | `.github/workflows/track-upstream.yml` |
| `d464b7d` | ci: hermes-deploy image build workflow | `.github/workflows/hermesdeploy-image.yml` |

---

## 2. Marker block inventory

Every fork addition is wrapped:

```
>>> hermes-fork: <short description>
... patch content ...
<<< hermes-fork
```

When upstream rebases, conflicts only ever land inside these blocks. Resolve
*only* what's inside, leave surrounding upstream code alone.

### `api/config.py`

| Line | Marker | Purpose |
|---|---|---|
| `660` | `first-class providers (HermesOS Cloud)` | Add Venice / CrofAI / Bankr / Xiaomi MiMo / CometAPI to `_PROVIDER_DISPLAY` |
| `669` | `built-in base-URL → canonical-slug map (HermesOS Cloud)` | `_BUILTIN_BASE_URL_PROVIDERS` table + `_builtin_provider_slug_for_base_url()` helper. Substring-match on hostname so `https://api.crof.ai/v1` and `https://crof.ai/v1` both resolve to slug `crof`. Covers crof/venice/bankr/cometapi/openrouter/anthropic/openai/groq/deepseek/minimax/moonshot/together/fireworks. |
| `956` | `built-in base-URL fallback (HermesOS Cloud)` | `_named_custom_provider_slug_for_base_url()` consults the built-in table AFTER the config.yaml lookup misses, so users hitting a known aggregator endpoint via `OPENAI_BASE_URL=…` auto-get the friendly group name in the dropdown — no `custom_providers:` block required in YAML. **Gated by `include_builtin_fallback: bool = True` kwarg**: the function's UI callers default to `True` (display "CrofAI" / "Venice" / etc. in the dropdown), and the runtime auth caller at `_resolve_configured_provider_id(resolve_alias=False)` passes `False` so the agent's provider arg stays `"custom"` and reads `OPENAI_API_KEY` instead of looking for fork-only env vars (`CROF_API_KEY`, `VENICE_API_KEY`, etc.) that aren't in the user's `.env`. |
| `~1673` | `protect runtime from @<built-in-slug>: leak (HermesOS Cloud)` | `resolve_model_provider` @-prefix guard. The dropdown JS adds `@<slug>:` to every model_id from a non-default provider group (see `_addLiveModelsToSelect` in `static/ui.js`). When the fork's auto-detect labels a group "CrofAI" because the user's `base_url` matched the built-in table, every model selected from that group becomes e.g. `@crof:kimi-k2.6-precision` — which `resolve_model_provider` would otherwise treat as an EXPLICIT per-message provider override and forward `provider="crof"` to the agent. The agent's `hermes_cli.auth.PROVIDER_REGISTRY` doesn't know `crof`/`venice`/`bankr`/etc., so it errors with `"Provider 'crof' is set in config.yaml but no API key was found"`. The guard translates `@<slug>:model` back to `("model", "custom", base_url)` when the @-prefix matches the built-in slug for the user's configured `base_url` AND `config.provider == "custom"`. Explicit `provider: openrouter` + `@crof:model` still routes through openrouter (the @-prefix is honoured). Tests: `tests/test_hermes_fork_provider_resolution.py`. |
| `1139` | `first-class providers (HermesOS Cloud)` | Empty model lists (placeholder keys in `_PROVIDER_MODELS`; live fetch is source of truth) |
| `3834` | `HermesOS Cloud skins server-side allowlist` | Adds `hermesos` + `sienna` to `_SETTINGS_SKIN_VALUES`. **Critical:** without this, `/api/claude-config` autosave normalises any unknown skin back to `default`, so a user who picks HermesOS in Appearance watches localStorage flip back to "default" on the next page load (server-state-wins race against the boot-time skin migration). |

### `api/providers.py`

| Line | Marker | Purpose |
|---|---|---|
| `107` | `first-class providers (HermesOS Cloud)` | Add 4 to `_PROVIDER_ENV_VAR` |
| `157` | `provider logos (HermesOS Cloud)` | `_PROVIDER_LOGO_URL` dict (Simple Icons + DuckDuckGo CDN URLs) + `_PROVIDER_LOGO_HUE` (letter-glyph fallback colors) |
| `971` | `provider logo` | Surface `logo_url` + `logo_hue` in `get_providers()` payload |
| `1010` | `surface custom_providers as configurable` | Flip `configurable: False → True` so user-defined custom providers in `config.yaml` get the standard edit UI |

### `api/routes.py`

| Line | Marker | Purpose |
|---|---|---|
| `699` | `first-class providers (HermesOS Cloud)` | Add Venice / CrofAI / Bankr / Xiaomi to `_OPENAI_COMPAT_ENDPOINTS` so generic `/v1/models` live-fetch works |
| `3879` | `refresh-models endpoint (HermesOS Cloud)` | New `POST /api/models/refresh` — the "Refresh models" button calls this. Drops cache + re-fetches via agent's `provider_model_ids()` |

### `api/streaming.py`

| Line | Marker | Purpose |
|---|---|---|
| `2022` | (no marker — defensive shim) | `_profile_home_path` fallback when `api.profiles` import fails, so the first-run bootstrap below always has a valid `Path` to gate on |
| `2660` | `first-run identity-discovery bootstrap (HermesOS Cloud)` | Resolve `api.bootstrap.build_first_run_system_prompt(_profile_home_path)` and combine with personality prompt into `agent.ephemeral_system_prompt` for the turn |
| `~3390` | `mark first-run sentinel only after a clean done` | Sentinel write is deferred until **after** `put('done', ...)` so a buggy first message (bad API key, rate limit, provider 5xx, SSE drop) never burns the bootstrap. Subsequent retries re-fire until the agent produces a real reply. |

### `static/iframe-shim.js` (whole file is a fork addition)

| Symbol / behaviour | Purpose |
|---|---|
| `__hermesIframeShimInstalled` guard | Idempotent re-install — re-reads `#iframe_token` if the dashboard re-mints a URL, keeps the patched transports in place. |
| Hash → sessionStorage handoff | Reads `iframe_token=` from `location.hash` on first paint, sessions-stores the bearer, wipes the hash so the URL bar doesn't expose it. |
| `fetch()` wrapper | Adds `Authorization: Bearer <token>` to every same-origin request that doesn't already have one. |
| `XMLHttpRequest.open` wrapper | Same — sets the header right before `.send()`. |
| `EventSource` wrapper | SSE has no header API; instead appends `?token=<bearer>` to same-origin URLs. **Requires** the dashboard Caddyfile to add an `@authQueryToken query token=<bearer>` matcher so SSE auth lands. |
| `WebSocket` wrapper | Same `?token=` URL approach as EventSource. |
| Same-origin guard | Cross-origin requests (CDN fonts, analytics) are passed through unmodified — the bearer never leaks off-host. |

Loaded by `static/index.html` as the FIRST executable script (right after the `<base>`-href bootstrap). Synchronous on purpose — every later script issues fetch/XHR/SSE/WS that needs the patches in place.

**Direct-URL users** (typing the gateway URL into a tab with no parent iframe / no `#iframe_token=` hash) are unaffected — the shim no-ops and falls back to whatever auth path the Caddyfile is configured for (cookie / forward_auth / bearer header).

**Companion in dashboard repo (still required):** `dashboard/src/lib/services/webui-instance-builder.ts` Caddyfile template needs two updates that don't live in this repo:
1. Add `/` to the `@public path …` list so the SPA shell loads unauthenticated (the shim then installs the bearer for everything else).
2. Add `@authQueryToken query token=<bearer>` matcher routing to webui so the shim's `?token=` URLs on EventSource/WebSocket actually authenticate.

### `api/bootstrap.py` (whole file is a fork addition)

| Symbol | Purpose |
|---|---|
| `DEFAULT_FIRST_RUN_PROMPT` | Identity-discovery prompt text. Tells the agent it just came online, instructs it to ask "who am I / who are you?", then write `SOUL.md` and `memories/USER.md`. Ported from `dashboard/src/components/chat/hooks/chat-bootstrap.ts` so iframe + direct-canary-URL deploys both fire it. |
| `get_first_run_prompt()` | Reads `HERMES_WEBUI_FIRST_RUN_PROMPT` env override; empty string disables. |
| `should_inject_first_run_prompt(profile_home)` | True when sentinel file `<profile_home>/.bootstrap_fired` is absent AND prompt non-empty. |
| `mark_first_run_fired(profile_home)` | Touches the sentinel; subsequent turns skip injection. |

**Companion in dashboard repo (kept, scoped):** `dashboard/src/components/chat/hooks/chat-bootstrap.ts` continues to serve the legacy gateway-backend chat surface (when `instance.backend !== "webui"`). WebUI-backed deploys are handled by this module instead. Either path delivers the same prompt text.

### `static/ui.js`

(No remaining fork edits — the earlier "extract provider from model name" patch was reverted after user feedback: the chip should show the user's routing provider, not the upstream model owner. With the chip back to `m.group`, users on a plain `custom` endpoint see "Custom", users who configure `custom_providers: { crof: {…} }` in config.yaml see "Crof", etc.)

### `static/boot.js`

| Line | Marker | Purpose |
|---|---|---|
| `229` | `default-profile display nickname (HermesOS Cloud)` | `_hermesDisplayProfileName(name)` helper + `_hermesDefaultProfileLabelInput(value)` oninput handler. See "Default-profile nickname" section below for the full surface. |
| `264` | `sidebar collapse toggle (HermesOS Cloud)` | `toggleSidebarCollapsed()` toggles `.sidebar-collapsed` on `.layout`. Persists in `localStorage[hermes-sidebar-collapsed]`. Wraps `switchPanel()` so clicking any rail tab auto-uncollapses. **`_wireSidebarClickToExpand` IIFE** registers a CAPTURE-PHASE click delegate on `.sidebar`: when collapsed, any click on the strip expands the sidebar AND stops propagation BEFORE the inline `onclick="toggleSidebarCollapsed()"` on the chevron button fires (which would otherwise immediately re-collapse). The `true` third arg to `addEventListener` is load-bearing — bubble phase double-toggles. |
| `1222` | `HermesOS skin tile (HermesOS Cloud)` | Adds `{name:'HermesOS', colors:['#d4af37','#c5a059','#8a6e26']}` as the FIRST entry in `_SKINS` so it shows up as the leftmost tile in the Appearance picker. Gold swatches match dashboard `--gold-leaf`. |
| `1514` | `hydrate the Settings nickname input from localStorage` | When the Preferences panel mounts, reads `localStorage[hermes-default-profile-label]` into the `#settingsDefaultProfileLabel` input value so the user sees their saved nickname instead of an empty field. |

### `static/style.css`

| Line | Marker | Purpose |
|---|---|---|
| `162` | `HermesOS skin (HermesOS Cloud)` | The full HermesOS palette block (~135 lines): vellum/ink/gold tokens for `:root[data-skin="hermesos"]` + dark mode + sienna-skin tweaks. Mirrors the dashboard's visual identity (Outfit body / Playfair serif / Space Mono code, sharp corners, gold accent). Hides the `.app-titlebar[role="banner"]` under HermesOS skin. The universal `*{border-radius:0}` rule has a small allowlist to preserve the ctx-indicator circles. |
| `456` | `collapsed-sidebar state (HermesOS Cloud)` | When `.layout.sidebar-collapsed` is set, the sidebar shrinks from 300px → 32px and the **entire** strip becomes the click target (`cursor:pointer` on the sidebar itself + hover bg highlight). The chevron icon (11×11px, opacity 0.7) is a static visual cue, not a button. `pointer-events:none` on the actual `#btnCollapseSidebar` so the parent's capture-phase delegate handles all clicks consistently. **32px (not 24px)** + `border-left` + `border-right` so the strip reads as a distinct surface when the user has a browser-level sidebar visible (Arc's tab sidebar etc.) — otherwise the rail + strip + browser-sidebar visually blur together. |
| (~`243`) | (no marker — inside the preserve-circles allowlist) | Adds `.ctx-indicator`, `.ctx-indicator-wrap`, `.ctx-ring`, `.ctx-ring-center`, `.session-state-indicator::before`, `.session-attention-indicator::before` so the composer's context-window progress badge + the chat-list streaming spinner + the unread dot all stay circular under the HermesOS skin's universal `*{border-radius:0}` rule. The streaming spinner in particular renders as a 100%-box with two coloured borders rotated via `@keyframes spin` — without `border-radius:50%` it becomes a rotating square. |
| `2712` | `provider logo (img with letter-glyph fallback layered underneath)` | `.provider-card-logo` + `.provider-card-logo-img` + `.provider-card-logo-letter` (layered img-over-glyph) + `.provider-card-auth-row` |
| `2739` | `Provider tabs` | `.provider-tabs` / `.provider-tab` / `.provider-card.is-hidden` |

### `static/index.html`

| Line | Marker | Purpose |
|---|---|---|
| `7` | `brand assets + Google Fonts (HermesOS Cloud)` | Replaces upstream's favicon list with HermesOS-branded SVG/PNG/ICO. Preconnects + loads Outfit / Playfair Display / Space Mono from Google Fonts (matched in the HermesOS skin's `font-family` declarations). |
| `23` | `iframe bearer-from-hash shim (HermesOS Cloud)` | `<script src="static/iframe-shim.js?v=__WEBUI_VERSION__"></script>` — runs synchronously BEFORE any other script. See the `static/iframe-shim.js` section for what it does. |
| `31` | `HermesOS skin whitelist + default + migration (HermesOS Cloud)` | Inline `<script>` runs before the rest of boot: (1) adds `hermesos` to the boot-time `skins` allowlist, (2) sets `hermesos` as the default for fresh installs (`localStorage.getItem('hermes-skin') \|\| 'hermesos'`), (3) **forced rebrand migration v3** — if `hermes-skin-rebrand-v3` flag is unset (everyone right now), force `s = 'hermesos'` and write the flag. Old `v1` + `v2` flags get cleared. Users who pick another skin AFTER the migration fires keep their choice. Bumping to v4/v5/etc. is a one-line change. |
| `149` | `collapse-sidebar button (HermesOS Cloud)` | The `#btnCollapseSidebar` chevron button in the chat panel head. Inline `onclick="toggleSidebarCollapsed()"` calls the boot.js helper. The SVG icon flips to chevron-right via CSS when `.sidebar-collapsed` is on `.layout`. |
| `1081` | `default-profile nickname (HermesOS Cloud)` | The `#settingsDefaultProfileLabel` input in Preferences. `oninput="_hermesDefaultProfileLabelInput(value)"` writes to localStorage on each keystroke and re-renders the composer chip + the Profiles panel if it's open. |

### `static/sw.js`

| Line | Marker | Purpose |
|---|---|---|
| `25` | `iframe shim must be pre-cached so iframe loads warm` | Adds `./static/iframe-shim.js + VQ` as the FIRST entry in `SHELL_ASSETS`. The cache key is `__WEBUI_VERSION__`-suffixed, so every image build automatically invalidates the cached shim — important when the bearer-from-hash logic changes. |

### Default-profile nickname (3 files)

The hermes-agent "default" profile name is hardcoded into the profile-resolution layer (session paths, active-profile API, gateway_state.json filename, etc.) — renaming the actual profile would break too much. Instead we render a UI-only nickname stored in `localStorage['hermes-default-profile-label']`.

| File | What |
|---|---|
| `static/boot.js` (~`function _hermesDisplayProfileName`) | Helper that returns the nickname when `name === 'default'` and a localStorage override exists, else the name as-is. Plus `_hermesDefaultProfileLabelInput(value)` — the `oninput` handler for the Settings input. Live-updates the composer chip + the profile-dropdown render as the user types. |
| `static/boot.js` (~`profileChipLabel.textContent =`) | Initial chip render at boot uses the helper. |
| `static/ui.js` (~`profileChipLabel.textContent =` × 2) | Topbar + session-sync chip renders also go through the helper. Without this, those overwrite the boot-time value on every session activation and the nickname blinks back to "default". |
| `static/panels.js` (~`profile-opt-name`) | The profile dropdown's per-row name uses `_hermesDisplayProfileName(p.name)`. Falls back to `p.name` if the helper isn't loaded yet (defensive). |
| `static/panels.js` (~`loadProfilesPanel` ~`profile-card-name`) | The Profiles management panel's card list also routes the name through the helper, so the sidebar reads "Ash (default) ACTIVE" instead of "default (default) ACTIVE". |
| `static/panels.js` (~`_renderProfileDetail` `title.textContent`) | The Profiles panel's detail-page title also uses the helper — so the top of the right pane reads "Ash" instead of "default". The Status row's `(default)` badge intentionally stays as-is (informative). |
| `static/boot.js` (~`_hermesDefaultProfileLabelInput`) | Live-rerenders `loadProfilesPanel()` if the user is currently viewing the Profiles panel while typing a new nickname, so the panel updates without needing a panel switch. |
| `static/index.html` (~`#settingsDefaultProfileLabel`) | The Settings → Preferences input that writes to localStorage on each keystroke. |
| `static/i18n.js` | `settings_label_default_profile_label` + `settings_desc_default_profile_label`. |

### `static/panels.js`

| Line | Marker | Purpose |
|---|---|---|
| `4072` | `render the localStorage-backed nickname when present` | Profiles management panel card list — `loadProfilesPanel()` routes the name through `_hermesDisplayProfileName(p.name)`. |
| `4105` | `detail header uses the nickname when set` | Profiles panel detail-page title (`_renderProfileDetail`) — top of the right pane reads "Ash" instead of "default". The Status row's `(default)` badge stays as-is. |
| `4219` | `render the localStorage-backed nickname` | Profile dropdown's per-row name uses `_hermesDisplayProfileName(p.name)`. Falls back to `p.name` if the helper isn't loaded yet (defensive). |
| `5200` | `Recommended provider list` | `_RECOMMENDED_PROVIDERS` array — curated picks shown in the Recommended tab |
| `5218` | `tabs row above the provider list` | Renders Recommended / All / OAuth chips |
| `5237` | `sort providers` | Recommended-list order first, then alphabetical |
| `5256` | `tab click handlers + initial filter` | Pure-DOM filter; no second `/api/providers` fetch on tab change |
| `5273` | `Voice/STT panel attached to Providers section` | Lazy-injects Voice section after the provider list. Loads via `loadVoicePanel()` |
| `5408` | `provider logo (real brand SVG via logo_url, letter-glyph fallback)` | `<img>` with `onerror.remove()` → reveals layered letter-glyph underneath |
| `5457` | `OAuth Authenticate button (Option A — terminal pretype)` | Adds `[Authenticate]` / `[Re-authenticate]` button to OAuth cards |
| `5678` | `zero-result refresh ≠ success` | Refresh toast: 0 models = "Add API key first"; >0 = "Models refreshed — N models" |
| `6260` | `OAuth Authenticate helper` | `_authenticateProvider(id)` — closes Settings, opens composer terminal, types `hermes auth add <provider>` |
| `6316` | `Voice/STT settings panel` | `loadVoicePanel()` — read/write `stt.provider`, `stt.model`, `GROQ_API_KEY`/`VOICE_TOOLS_OPENAI_KEY` via `/api/claude-config` PATCH |

### `static/i18n.js`

| Line | Marker | Purpose |
|---|---|---|
| `724` | `Settings UI strings` | English locale entries for every fork-introduced UI string |

**Locale keys added** (en only — other locales fall through to en via the `?? LOCALES.en[key]` chain in `t()`):

- `providers_authenticate` — "Authenticate"
- `providers_reauthenticate` — "Re-authenticate"
- `providers_refresh_models` — "Refresh models"
- `providers_refreshing` — "Refreshing…"
- `providers_models_refreshed` — "Models refreshed"
- `providers_models_refresh_empty` — "Add an API key first — the provider rejected the unauthenticated request"
- `voice_section_title` — "Voice / Transcription"
- `voice_section_desc` — "Speech-to-text provider for the mic button. Local = free but slow on small VMs; Groq is the fastest cloud option."
- `voice_provider_label` — "Transcription provider"
- `voice_model_label` — "Model"
- `voice_key_label` — "API key"
- `voice_saved` — "Voice settings saved"
- `settings_label_default_profile_label` — "Default profile nickname"
- `settings_desc_default_profile_label` — "Display name for the `default` profile (UI only — the underlying profile id stays `default` so persistence paths are untouched)."
- `sidebar_collapse_title` — "Hide chat list"
- `sidebar_expand_title` — "Show chat list"

---

## 3. First-class provider data (the values to update when providers change)

Editing any of these is purely additive within the marker blocks — no risk of
breaking upstream merges.

**`api/config.py:_PROVIDER_DISPLAY`** — adds to upstream's dict:
- `venice` → "Venice"
- `crof` → "CrofAI"
- `bankr` → "Bankr"
- `xiaomi` → "Xiaomi MiMo"

**`api/routes.py:_OPENAI_COMPAT_ENDPOINTS`** — adds OpenAI-compat `/v1/models` URLs:
- `venice` → `https://api.venice.ai/api/v1`
- `crof` → `https://crof.ai/v1`
- `bankr` → `https://gateway.bankr.bot/v1`
- `xiaomi` → `https://api.xiaomi.com/v1`

**`api/providers.py:_PROVIDER_ENV_VAR`** — adds env-var names for API keys:
- `venice` → `VENICE_API_KEY`
- `crof` → `CROF_API_KEY`
- `bankr` → `BANKR_API_KEY`
- `xiaomi` → `XIAOMI_API_KEY`

**`api/providers.py:_PROVIDER_LOGO_URL`** — full dict in source. Two CDN sources:
- Simple Icons (`https://cdn.simpleicons.org/<slug>/fff`) — covers ~14 well-known brands
- DuckDuckGo (`https://icons.duckduckgo.com/ip3/<domain>.ico`) — covers everything else, including OpenAI / Nous / Venice / Bankr / Crof / etc.

To add a new provider's logo: append one line to `_PROVIDER_LOGO_URL` with the appropriate CDN URL. To override (e.g. you have a hand-crafted SVG), commit it to `static/provider-logos/<slug>.svg` and point the dict entry at `"static/provider-logos/<slug>.svg"`. The frontend treats it identically.

**`static/panels.js:_RECOMMENDED_PROVIDERS`** — order-driven array. Editing
this changes which providers show under the Recommended tab AND the order
they appear in the All tab. Current:

```js
['venice','bankr','anthropic','openrouter','nous','openai-codex','crof']
```

---

## 4. Deploy-side patches (in dashboard repo, NOT this one)

As of 2026-05-11 the webui bearer-from-hash shim is **baked into the bundle**
(see `static/iframe-shim.js` + the loader tag in `static/index.html:23` + the
`SHELL_ASSETS` pre-cache in `static/sw.js:25`). Every fleet VM picks it up
automatically on `:stable` image pull — no sidecar / no sed-injection needed.

The remaining deploy-side surfaces all live in
[`dashboard/src/lib/services/webui-instance-builder.ts`](https://github.com/ashneil12/hermesdeploy/blob/main/dashboard/src/lib/services/webui-instance-builder.ts)
and `webui-runtime-env.ts`:

| Surface | Role |
|---|---|
| `Caddyfile` template (`@public` matcher) | Includes `/` so the SPA shell loads unauthenticated. The baked-in shim then installs the bearer for every subsequent request. |
| `Caddyfile` template (`@authQueryToken query token=<bearer>` matcher) | Routes SSE + WebSocket requests carrying `?token=<bearer>` (which can't set the `Authorization` header) to webui with auth. The shim writes this query for `EventSource` and `WebSocket`. |
| `WEBUI_RUNTIME_PATH` (`webui-runtime-env.ts`) | Includes `/usr/local/sbin:/usr/sbin:/sbin` so the container init's `groupmod`/`usermod` calls resolve. |
| `agentName` → `HERMES_WEBUI_BOT_NAME` env (`webui-instance-builder.ts`) | When dashboard user sets a custom agent name, it's emitted as an env var the webui reads on first-run bootstrap. |
| Per-VM gateway supervisor heartbeat | Compose's gateway-supervisor command includes a `_heartbeat_state_files()` tick (every 10s, bumps `updated_at` in `gateway_state.json` so cross-container webui's 120s freshness window doesn't expire after a quiet period). Live on VM 205; not yet in the builder template — port in next builder rev. |
| Proxmox VM CPU type | Template 9004 onward uses `x86-64-v3` — fixes NumPy `X86_V2 baseline` transcription crash on Hetzner AX hosts. |

---

## 5. Re-merge runbook (when upstream conflicts)

Daily Track Upstream workflow at 06:00 UTC tries
`git rebase upstream/master`. If it fails, it pushes an
`upstream-sync-<date>` branch and opens a PR.

**Resolve:**

1. Check out the conflict branch the workflow created.
2. `git status -sb` — list conflicted files.
3. For each conflicted file, look ONLY inside `>>> hermes-fork` ... `<<< hermes-fork` marker blocks. Outside the blocks = upstream change, accept as-is.
4. Inside a marker block, three cases:
   - **Both touched:** rare. Combine carefully. Often upstream added something parallel — keep both.
   - **Upstream removed a function our marker depends on:** check section 3 above, decide if the patch still makes sense. If not, delete the marker block (the feature is now upstream's responsibility).
   - **Upstream renamed a dict / function:** update the marker block's reference to match. Run `python3 -c "import ast; ast.parse(open('<file>').read())"` and `node -c <file.js>` to confirm syntax.
5. `git add` the resolved files, `git rebase --continue`.
6. `git push origin master --force-with-lease`.
7. CI auto-rebuilds `:stable` (~3-5 min).
8. Pull on the live VM to verify (current production target is **VM 205 on pve2**): SSH in, `sudo docker pull ghcr.io/ashneil12/hermes-webui:stable && cd /opt/hermes/instances/<id> && sudo docker compose up -d --force-recreate webui`.
9. Smoke-test (in this order — each step exercises a different fork patch):
   - Settings → Appearance → confirm HermesOS tile shows first, click another skin, refresh, confirm it persists (server-side allowlist working).
   - Settings → Providers → Recommended → confirm cards render with logos + the Voice / Transcription section is at the bottom.
   - Composer → click the sidebar chevron → confirm sidebar shrinks to 24px strip → click strip → confirm it re-expands (capture-phase delegate working).
   - Settings → Preferences → enter a nickname in "Default profile nickname" → confirm composer chip updates live.
   - Send any message → confirm it streams (first-run bootstrap doesn't burn on auth failures because sentinel is deferred until clean done).

**Common upstream changes that touch our markers:**

- New provider added to `_PROVIDER_DISPLAY` upstream → our block is unaffected (just adds to the same dict).
- Upstream refactors `_buildProviderCard` signature → fork patches at `panels.js:5393` and `5442` may need to re-thread the new arguments.
- Upstream restructures the Settings panel HTML → `loadVoicePanel` injection in `panels.js:5258` may need a new mount point.

---

## 6. Regression test suite (HermesOS Cloud)

The fork carries its own regression test files alongside the upstream
`tests/` directory. These exist to catch fork-specific bugs that upstream
CI would never see — they MUST pass before any push to `main` (the
`:stable` image build pulls from main).

**Run locally:**

```bash
uv run --python 3.12 --with pytest --with pyyaml python \
  -m pytest tests/test_hermes_fork_*.py -v
```

**Layer A — provider resolution + auth wiring (parameterized over every
entry in `_BUILTIN_BASE_URL_PROVIDERS`):**

`tests/test_hermes_fork_provider_resolution.py` — 116 tests covering:

1. `_builtin_provider_slug_for_base_url` lookup matrix (direct match,
   subdomain, uppercase, empty, unknown).
2. `_named_custom_provider_slug_for_base_url` UI vs runtime gating —
   `include_builtin_fallback=True` returns the slug, `=False` returns
   empty (so the runtime auth path doesn't flip to fork-only env vars).
3. `_resolve_configured_provider_id` UI vs runtime split — UI auto-resolves
   to slug, runtime stays `"custom"` so the agent reads `OPENAI_API_KEY`.
4. **`resolve_model_provider` @-prefix smuggling guard** (THE bug that
   shipped to prod on 2026-05-11): `@<built-in-slug>:model` with
   `provider: custom + base_url: <matching>` collapses back to
   `("model", "custom", base_url)`. Counter-cases also asserted: explicit
   `provider: openrouter` still honoured, mismatched slug not collapsed,
   unknown base_url not collapsed.
5. User-declared `custom_providers:` block wins over the built-in fallback.
6. Source-code wiring guards — asserts the `include_builtin_fallback`
   kwarg and the @-prefix guard marker block stay present in
   `api/config.py`.

**What goes in Layer A vs not:**

- ✅ Any new entry to `_BUILTIN_BASE_URL_PROVIDERS` is automatically tested
  for both the UI and runtime paths (the suite is parameterized over the
  table itself, not hardcoded slugs).
- ✅ Any fork-only resolution or alias logic affecting provider/model arg
  routing into the agent.
- ❌ Pure-display logic that doesn't touch runtime (provider chip label,
  group name) — visual regression, not auth regression.
- ❌ Upstream test territory (provider behaviour that any webui user
  would care about) — open an upstream PR with the test instead.

**Layer B (smoke) and Layer C (canary) are TBD — see
`.github/workflows/smoke-stable-image.yml` and
`scripts/smoke-canary-vm.sh` (dashboard repo) when they land.**

---

## 7. Don't-add-to-the-fork list

When you're tempted to "just patch one more thing," check whether upstream
will absorb it instead. Things that **don't** belong in this fork:

- Bug fixes that already exist in upstream master after a release tag — pull upstream first.
- Feature requests that are reasonable for any webui user — open an upstream PR; we'll get it back via Track Upstream.
- One-off canary tweaks — those go in the per-instance config files, not the image.
- Skin / theme changes — separate workstream, separate session.

The bar for adding to the fork: "this is HermesOS Cloud-specific platform
integration, not webui functionality" (e.g. provider list for our deployments,
sponsored-STT proxy URL if/when we add it, dashboard-side OAuth bridging).
