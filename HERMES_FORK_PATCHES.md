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
| `660` | `first-class providers (HermesOS Cloud)` | Add Venice / CrofAI / Bankr / Xiaomi MiMo to `_PROVIDER_DISPLAY` |
| `1079` | `first-class providers (HermesOS Cloud)` | Empty model lists (placeholder keys in `_PROVIDER_MODELS`; live fetch is source of truth) |

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

### `static/panels.js`

| Line | Marker | Purpose |
|---|---|---|
| `5185` | `Recommended provider list` | `_RECOMMENDED_PROVIDERS` array — curated picks shown in the Recommended tab |
| `5203` | `tabs row above the provider list` | Renders Recommended / All / OAuth chips |
| `5222` | `sort providers` | Recommended-list order first, then alphabetical |
| `5241` | `tab click handlers + initial filter` | Pure-DOM filter; no second `/api/providers` fetch on tab change |
| `5258` | `Voice/STT panel attached to Providers section` | Lazy-injects Voice section after the provider list. Loads via `loadVoicePanel()` |
| `5393` | `provider logo (real brand SVG via logo_url, letter-glyph fallback)` | `<img>` with `onerror.remove()` → reveals layered letter-glyph underneath |
| `5442` | `OAuth Authenticate button (Option A — terminal pretype)` | Adds `[Authenticate]` / `[Re-authenticate]` button to OAuth cards |
| `5663` | `zero-result refresh ≠ success` | Refresh toast: 0 models = "Add API key first"; >0 = "Models refreshed — N models" |
| `6245` | `OAuth Authenticate helper` | `_authenticateProvider(id)` — closes Settings, opens composer terminal, types `hermes auth add <provider>` |
| `6301` | `Voice/STT settings panel` | `loadVoicePanel()` — read/write `stt.provider`, `stt.model`, `GROQ_API_KEY`/`VOICE_TOOLS_OPENAI_KEY` via `/api/claude-config` PATCH |

### `static/style.css`

| Line | Marker | Purpose |
|---|---|---|
| `2505` | `provider logo` | `.provider-card-logo` + `.provider-card-logo-img` + `.provider-card-logo-letter` (layered img-over-glyph) + `.provider-card-auth-row` |
| `2532` | `Provider tabs` | `.provider-tabs` / `.provider-tab` / `.provider-card.is-hidden` |

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

## 4. Deploy-side patches (NOT in this repo)

These live on each canary/fleet VM at `/opt/hermes/instances/<id>/`. They are
**not** part of the hermes-webui fork — they're how the dashboard provisions
the runtime around the webui container. Long-term these should be ported into
[`dashboard/src/lib/services/webui-instance-builder.ts`](https://github.com/ashneil12/hermesdeploy/blob/main/dashboard/src/lib/services/webui-instance-builder.ts)
so every fleet VM gets them automatically — currently only canary 407 has
them.

| File on canary 407 | Role |
|---|---|
| `docker-compose.yml` | adds `shim-installer` service + bind-mounts `iframe-shim.js` and `shim-install.sh` |
| `shim-install.sh` | the install script (sourced as `/seed/shim-install.sh` in the sidecar). 3 idempotent steps: inject iframe shim, apt-install sudo + passwordless sudoers, link `hermes` CLI to `/usr/local/bin`. |
| `iframe-shim.js` | the bearer-from-hash shim that gets sed-injected into webui's `static/index.html`. Reads `#iframe_token=` from `location.hash` on first load, stores in sessionStorage, patches `fetch`/`XHR`/`EventSource`/`WebSocket` to inject `Authorization: Bearer` on every request. |
| `Caddyfile` | adds `@authQueryToken query token=<bearer>` matcher (for SSE/WebSocket which can't set headers), `/` added to `@public` (so the shim-bearing `index.html` loads unauthenticated). |
| `config.yaml` | `model.default`, `model.provider`, no `custom_providers:` (first-class entries supersede). |
| `.env` / `hermes.env` | provider API keys + `API_SERVER_HOST=0.0.0.0` + `API_SERVER_PORT=8642` + PATH including `/usr/sbin:/sbin` for the init script's `groupmod`/`usermod` calls. |

**Other canary-only fixes:**
- Proxmox VM CPU bumped to `x86-64-v3` (template 9004 + canary 407) — fixes NumPy `X86_V2 baseline` transcription crash.
- Gateway supervisor in compose patched with `heartbeat_state_files()` tick (every 10s, bumps `updated_at` in `gateway_state.json` so cross-container webui's freshness window doesn't expire after 120s).

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
8. Pull on canary 407 to verify: `sudo docker pull ghcr.io/ashneil12/hermes-webui:stable && cd /opt/hermes/instances/canary-407 && sudo docker rm -f agent-canary-407 agent-canary-407-shim-installer && sudo docker compose up -d webui shim-installer`.
9. Smoke-test: Settings → Providers → Recommended → confirm cards render with logos + the Voice / Transcription section is at the bottom.

**Common upstream changes that touch our markers:**

- New provider added to `_PROVIDER_DISPLAY` upstream → our block is unaffected (just adds to the same dict).
- Upstream refactors `_buildProviderCard` signature → fork patches at `panels.js:5393` and `5442` may need to re-thread the new arguments.
- Upstream restructures the Settings panel HTML → `loadVoicePanel` injection in `panels.js:5258` may need a new mount point.

---

## 6. Don't-add-to-the-fork list

When you're tempted to "just patch one more thing," check whether upstream
will absorb it instead. Things that **don't** belong in this fork:

- Bug fixes that already exist in upstream master after a release tag — pull upstream first.
- Feature requests that are reasonable for any webui user — open an upstream PR; we'll get it back via Track Upstream.
- One-off canary tweaks — those go in the per-instance config files, not the image.
- Skin / theme changes — separate workstream, separate session.

The bar for adding to the fork: "this is HermesOS Cloud-specific platform
integration, not webui functionality" (e.g. provider list for our deployments,
sponsored-STT proxy URL if/when we add it, dashboard-side OAuth bridging).
