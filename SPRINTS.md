# Hermes Web UI — Sprint Planning

> Forward-looking sprint plan and active queue.
>
> Current state: v0.50.281 | 3995 tests | port 8787
> Target A (CLI parity): ✅ Complete
> Target B (Claude parity): ~95% — full subagent transparency UI and code-execution cells remain
>
> Per-version detail: [CHANGELOG.md](./CHANGELOG.md)
> Sprint history (chronology): [ROADMAP.md](./ROADMAP.md)

---

## How sprints work here

A sprint is a thematic batch — usually 3–8 PRs landed together as a release. Each sprint has:

1. **Theme** — single-sentence framing of what changes
2. **Items** — selected work, with tracking issue / PR
3. **Out of scope** — what is explicitly deferred and why
4. **Risks** — known sharp edges
5. **Retro** — once shipped, what we learned

External contributor PRs that don't fit a planned sprint get released individually as patch versions (`v0.50.X`). Sprints are reserved for coherent batches where the items reinforce each other.

---

## Active sprint candidates

These are the issues currently labeled `sprint-candidate` — the inbox the next sprint plan draws from. Each item already has a confirmed root cause or design direction.

| # | Title | Type | Sprint fit |
|---|---|---|---|
| #1458 | Persistent-host crashes — bootstrap fork pattern, state.db FD leak, HTTP-unhealthy wedge | bug, stability | Stability sprint candidate |
| #1426 | OpenRouter free-tier `:free` variants invisible (tool-support filter) | bug + feat | Model picker sprint candidate |
| #1362 | In-app OAuth login for Codex and Claude (currently terminal-only) | feat, ux | Onboarding sprint candidate |
| #1360 | macOS desktop app — auto-scroll overrides user scroll (#677 regression) | bug, ux | Desktop app polish |
| #1291 | GLM mutually exclusive between main agent and auxiliary title generation | bug | Auxiliary-route sprint |

The active queue stays small — it's the next 1-2 sprints' worth of work. The broader backlog of feature requests lives in [ROADMAP.md → Forward Work](./ROADMAP.md#forward-work) and on the GitHub `enhancement` label.

---

## Planning principles

**Phase-0 fit assessment.** Every sprint candidate gets a marginal-benefit screen first: does this make the product noticeably better for real users, or does it add surface area that costs more to maintain than it earns? Five-question fit screen — need / shape / bloat / clutter / scope.

**Salvage over absorb.** When a contributor PR is partial or scoped wrong, prefer splicing the good parts into a maintainer-side PR with `Co-authored-by` attribution rather than asking for multiple rebase rounds. Only absorb whole when the PR is genuinely shippable as-is.

**Independent-review gate.** Self-built PRs need either (a) Opus advisor pass on the merged stage diff, or (b) independent review from a separate reviewer. High-risk batches (large LOC, security, locks, durability) get both.

**Per-PR release velocity.** When a single PR fixes a real bug and has a clean review, ship it as its own patch release the same day rather than waiting for a sprint batch. Friction-free is the goal — sprint batches exist for coherence, not for arbitrary grouping.

**No feature creep mid-PR.** If a contributor proposes a scope addition during review, file it as a separate PR and link from the original. The current PR keeps its original boundaries.

**Pre-release gate (mandatory).** Every release runs:
1. `pytest tests/ -q --timeout=120` clean
2. Browser sanity check (HTTP-level API tests against a test server)
3. Opus advisor pass on the merged stage diff with a written brief
4. CHANGELOG.md + ROADMAP.md + TESTING.md version stamp
5. CI green on Python 3.11 / 3.12 / 3.13

Skipping any of these requires a documented "I'm doing an override" from the maintainer.

---

## Sprint shape

A typical sprint runs 3-7 days end to end:

| Phase | Duration | Output |
|---|---|---|
| Triage | 0.5 day | Active queue → selected items + scope notes |
| Design / spike | 0.5–1 day | Design notes for items needing them; deferrals documented |
| Build | 2–4 days | Each item on its own branch + PR for independent review |
| Review | 0.5–1 day | Maintainer + Opus advisor passes; SHOULD-FIX absorbed in-release |
| Stage + ship | 0.5 day | Stage branch, full test suite, release PR, tag, deploy, verify live |
| Hygiene | 0.5 day | Close PRs / issues, GitHub release notes, docs sync, retro |

Smaller sprints (1–2 PRs) compress to 1–2 days end to end. Single-PR releases skip the stage branch and ship via a release PR directly off the contributor's branch (or a maintainer-rebased copy if the fork doesn't grant write access).

---

## Sprint history

Per-version detail is in [CHANGELOG.md](./CHANGELOG.md). High-level theme chronology is in [ROADMAP.md → Sprint History](./ROADMAP.md#sprint-history). A few notable sprints worth highlighting:

- **Sprint 19** — auth + security hardening. First sprint that made the app safe to leave running beyond localhost.
- **Sprint 21** — mobile responsive + Docker. Two-container compose enabled the first wave of self-host deployments.
- **Sprint 22** — multi-profile support. Major CLI-parity unlock; profile switching is now seamless without server restart.
- **Sprint 25** — macOS desktop application. Native Swift + WKWebView shell, universal Intel + Apple Silicon DMG, Sparkle 2 auto-update. Lives in the separate `hermes-webui/hermes-swift-mac` repo.
- **Sprint 26** — pluggable themes. CSS-variable-driven 8-theme system that lets community contributors add themes as pure CSS.
- **Sprint 34** — v0.50.0 UI overhaul. Composer-centric controls, Control Center modal, workspace state machine, rAF streaming throttle.

---

## Out of scope (across all sprints)

These are intentionally not on the roadmap. Listing them here to save planning cycles.

- **Multi-user collaboration** — single-user assumption throughout the codebase. Refactoring would be a from-scratch architecture change.
- **Sharing / public conversation URLs** — requires hosted backend with access control + CDN. Out of scope for self-hosted.
- **Plugin marketplace** — Hermes skills already cover this surface.
- **Anthropic / Claude proprietary features** — Projects AI memory, Claude artifacts sync. Not reproducible.
- **Linux / Windows native app wrappers** — macOS done; demand on other platforms not yet established. Web UI works in any browser.
- **App Store distribution** — sandboxing breaks the local-server model.
- **Auto-update mechanism for the Python webapp** — Sparkle 2 covers the Mac app; the webapp updates via `git pull` + restart, which is the same as every other Python service.

---

## Templates

When opening a new sprint plan, copy this structure:

```markdown
# Sprint NN — <theme>

**Version target:** vX.Y.Z
**Theme:** <single sentence>
**Date started:** YYYY-MM-DD
**Status:** PLANNED | IN PROGRESS | COMPLETED — vX.Y.Z

## Items
| # | Issue | Title | Complexity | Files | PR |
|---|-------|-------|------------|-------|-----|

## Rationale
Why these items, why now.

## Build approach
Per-item branch + PR; or single combined branch if items are tightly coupled.

## Out of scope
What did NOT make this sprint and why.

## Known risks
Sharp edges to watch during review.

## PR Status
| Issue | PR | Status |
|-------|-----|--------|

## Retro (post-ship)
What worked, what we'd do differently.
```

The maintainer's planning notes for each sprint live in the workspace repo (private), not in this file. This file is the public-facing planning shape.
