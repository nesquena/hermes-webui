# Contributors

Hermes WebUI is a community project. **66 people** have shipped code that landed in a release tag, including the long tail of folks whose work was salvaged into batch releases. This file is the canonical credit roll. Numbers are merged-PR count plus release-batch credit (a contributor whose patch was extracted into a clean PR or merged via squash gets the same credit as a standalone PR).

**Total contributors tracked:** 66  
**Total PRs landed:** 142  
**Last refreshed:** v0.50.245, 2026-04-30

Generated from `git log` + `gh api repos/.../pulls?state=closed` + the `CHANGELOG.md` attribution lines. If your name is missing or wrong, open a PR against `CONTRIBUTORS.md` — we cross-check against the changelog on each release.

---

## Top contributors (5+ merged PRs)

| # | Contributor | PRs | First release | Latest release |
|---|---|---:|---|---|
| 1 | [@franksong2702](https://github.com/franksong2702) | 22 | `v0.50.49` 2026-04-15 | `v0.50.245` 2026-04-30 |
| 2 | [@bergeouss](https://github.com/bergeouss) | 18 | `v0.50.49` 2026-04-15 | `v0.50.240` 2026-04-30 |
| 3 | [@aronprins](https://github.com/aronprins) | 8 | `v0.47.0` 2026-04-11 | `v0.50.77` 2026-04-17 |
| 4 | [@iRonin](https://github.com/iRonin) | 6 | `v0.41.0` 2026-04-10 | `v0.41.0` 2026-04-10 |
| 5 | [@24601](https://github.com/24601) | 6 | `v0.50.201` 2026-04-28 | `v0.50.201` 2026-04-28 |

## Sustained contributors (3–4 merged PRs)

| Contributor | PRs | Highlights |
|---|---:|---|
| [@renheqiang](https://github.com/renheqiang) | 4 | feat: add full Russian (ru-RU) localization — v0.50.93 |
| [@KingBoyAndGirl](https://github.com/KingBoyAndGirl) | 4 | fix: trust custom provider base_url in SSRF validation; fix: fetch live models for custom provider from model.base_u |
| [@ccqqlo](https://github.com/ccqqlo) | 3 | `v0.50.83` batch credit |
| [@deboste](https://github.com/deboste) | 3 | fix(frontend): use URL origin for fetch/EventSource to suppo; fix(api): resolve model provider from config to prevent misr |
| [@frap129](https://github.com/frap129) | 3 | fix(docker): Install Open SSH client; fix(docker): Install all dependencies for agent |

## Two-PR contributors

[@dso2ng](https://github.com/dso2ng), [@Michaelyklam](https://github.com/Michaelyklam), [@mmartial](https://github.com/mmartial), [@renatomott](https://github.com/renatomott), [@zichen0116](https://github.com/zichen0116), [@pavolbiely](https://github.com/pavolbiely), [@bsgdigital](https://github.com/bsgdigital), [@vansour](https://github.com/vansour), [@fecolinhares](https://github.com/fecolinhares).

## Single-PR contributors

Each of these folks landed exactly one merged change — bug fixes, locale work, doc improvements, infrastructure tweaks. Every one of them moved the project forward.

[@Argonaut790](https://github.com/Argonaut790), [@betamod](https://github.com/betamod), [@bschmidy10](https://github.com/bschmidy10), [@carlytwozero](https://github.com/carlytwozero), [@cloudyun888](https://github.com/cloudyun888), [@davidsben](https://github.com/davidsben), [@DavidSchuchert](https://github.com/DavidSchuchert), [@DrMaks22](https://github.com/DrMaks22), [@eba8](https://github.com/eba8), [@fxd-jason](https://github.com/fxd-jason), [@gabogabucho](https://github.com/gabogabucho), [@GiggleSamurai](https://github.com/GiggleSamurai), [@hacker2005](https://github.com/hacker2005), [@halmisen](https://github.com/halmisen), [@happy5318](https://github.com/happy5318), [@hi-friday](https://github.com/hi-friday), [@Hinotoi-agent](https://github.com/Hinotoi-agent), [@huangzt](https://github.com/huangzt), [@jeffscottward](https://github.com/jeffscottward), [@JKJameson](https://github.com/JKJameson), [@KayZz69](https://github.com/KayZz69), [@kcclaw001](https://github.com/kcclaw001), [@kevin-ho](https://github.com/kevin-ho), [@mangodxd](https://github.com/mangodxd), [@mariosam95](https://github.com/mariosam95), [@MatzAgent](https://github.com/MatzAgent), [@mbac](https://github.com/mbac), [@migueltavares](https://github.com/migueltavares), [@nickgiulioni1](https://github.com/nickgiulioni1), [@octo-patch](https://github.com/octo-patch), [@qxxaa](https://github.com/qxxaa), [@ruxme](https://github.com/ruxme), [@SaulgoodMan-C](https://github.com/SaulgoodMan-C), [@smurmann](https://github.com/smurmann), [@Stampede](https://github.com/Stampede), [@starship-s](https://github.com/starship-s), [@suinia](https://github.com/suinia), [@TaraTheStar](https://github.com/TaraTheStar), [@tgaalman](https://github.com/tgaalman), [@thadreber-web](https://github.com/thadreber-web), [@the-own-lab](https://github.com/the-own-lab), [@vcavichini](https://github.com/vcavichini), [@vCillusion](https://github.com/vCillusion), [@woaijiadanoo](https://github.com/woaijiadanoo), [@xingyue52077](https://github.com/xingyue52077), [@yunyunyunyun-yun](https://github.com/yunyunyunyun-yun), [@yzp12138](https://github.com/yzp12138).

---

## How credit is tracked

Most PRs in this repo land via one of three paths:

1. **Direct merge** — your PR is reviewed and merged on its own. Author shows up directly in `git log`.
2. **Squash into a batch release** — your PR is merged together with several other contributor PRs into a single release commit (e.g. `release: v0.50.245 — 10-PR batch`). The squashed commit carries a `Co-authored-by: <you>` trailer plus an entry in `CHANGELOG.md` crediting you by username and PR number.
3. **Salvaged from a larger PR** — when a PR mixes one good change with several unrelated or risky ones, we sometimes split it: the good parts ship in a clean follow-up PR, you get credit in the CHANGELOG entry, and the original PR is closed with a salvage map showing what went where.

All three paths count as a contribution. The number next to your name above is the total of merged PRs (path 1) plus PRs where you got attribution credit in CHANGELOG.md (paths 2 and 3).

## Special thanks

- **[@aronprins](https://github.com/aronprins)** — `v0.50.0` UI overhaul (PR #242). The CSS-only redesign that defined the design tokens, theme architecture, and three-panel layout that the rest of the app builds on. The PR didn't merge as-is — it was reshaped through `v0.50.0` — but it is the design language of the app.
- **[@franksong2702](https://github.com/franksong2702)** — most prolific external contributor. Mobile/responsive layout, session sidebar polish, cron output preservation, streaming-session sidebar exemption, and a long tail of profile/workspace fixes.
- **[@bergeouss](https://github.com/bergeouss)** — provider-management UI, OAuth status, two-container Docker docs, profile isolation hardening. Most of what users see when they touch Settings → Providers is bergeouss's work.

If you've contributed and aren't here, **open a PR**. We cross-check the CHANGELOG, but if a credit fell through (a Co-authored-by trailer that didn't make it into the changelog entry, an attribution in a comment that should be on the PR), this list is the right place to fix it.
