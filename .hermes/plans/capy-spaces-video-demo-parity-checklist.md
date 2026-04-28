# Capy Spaces — Space Agent Demo Video Parity Checklist

Video reviewed: https://www.youtube.com/watch?v=F3ZzNgf-R7Y
Transcript duration: 46:51
Created: 2026-04-27

## Bottom line

The demo does **not** introduce a new product direction beyond the existing Capy Spaces plan. It does clarify the exact parity bar:

- Capy Spaces needs a persistent visual workspace/canvas.
- The agent must create and update widgets from natural language.
- Widgets must be able to cooperate through shared per-space data/files.
- Browser panels must be first-class, inspectable, and jointly controllable by user + agent.
- Space-specific instructions must influence the agent.
- UI widgets must be persistent and reconstructible after reload.
- Git/checkpoint rollback and a safe recovery/admin surface are not optional.

I cannot honestly claim these examples would work **perfectly today**, because Capy Spaces is still a plan, not an implemented subsystem. I can say they are all architecturally feasible in Capy/Hermes, and the target architecture should be adjusted to explicitly test each demo example before declaring Space Agent parity.

## Demo examples observed

| Timestamp | Space Agent demo example | What happened | Capy Spaces parity status | Required Capy capability |
|---|---|---|---|---|
| 02:18 | Prebuilt prices/charts/news/daily dashboards | Host says Space Agent can render prices, charts, news, dashboards. | Feasible, should be first-class. | Declarative chart/table/news widgets, web fetch/proxy, scheduled/manual refresh. |
| 02:25 | Browser games | Host says it can render and play games in the browser. | Feasible with sandboxed interactive widgets. | Canvas/HTML widget renderer, keyboard focus isolation, event cleanup, sandbox policies. |
| 03:31 | Weather in Prague, then “show it to me in a widget” | Agent first replies in chat, then creates weather widget. | Must be Phase 1 acceptance test. | Chat-to-widget action, weather fetch, declarative widget, persistence. |
| 06:58 | Notes app | Agent-created app with folder list, rename, WYSIWYG editing, markdown view, copy/paste, images, attachments. | Feasible but not MVP; medium/high complexity. | Multi-widget app pattern, shared per-space data store, file/asset APIs, rich-text editor widget, attachment handling. |
| 09:59 | Surveillance dashboard | Agent creates dashboard of public/home camera streams. | Feasible with caveats. | Stream/image/video widgets, URL validation, mixed content/CORS handling, explicit permission for private camera URLs. |
| 11:25 | Agent Zero control dashboard | Space has Agent Zero API chat widget plus embedded full Agent Zero web UI browser panel. | Feasible in Capy with stronger existing primitives. | API connector widget, browser panel widget, secret storage, local service allowlist, browser-control bridge. |
| 12:05 | Agent checks Agent Zero settings/update through browser UI | Agent controls embedded browser using element transcription and click commands. | Feasible; Capy already has browser/CDP tooling, but needs WebUI-embedded browser-surface bridge. | Browser surface registry, accessibility/snapshot extraction, `click_ref/type_ref` actions, transient page context. |
| 16:48 | Research harness | Agent-created UI with research input/output widgets and space instructions. UI can send message back to agent. | Core Capy Spaces target. | Widget-to-agent event channel, space-specific system instructions, progress-updating widgets, file output. |
| 17:27 | Claude Mythos research run | UI-triggered agent browses web, updates planning/source gathering/notes/summary, creates markdown research output. | Feasible; Hermes is strong here. | Research workflow widget, browser/search tools, markdown artifact persistence, streaming status updates. |
| 18:51 | Add export-to-PDF button | Agent patches existing research output UI, adds formatting/tables/links and native print/PDF flow. | Feasible; should be a Phase 2 test. | Widget patching, print/PDF export, DOM-safe formatting, revision rollback. |
| 20:59 | Trello-style colorful Kanban board | Agent creates a kanban board, cards/columns, rename behavior. | Feasible; good standard demo. | Drag/drop widgets or internal widget state, persistent JSON state, card editing, layout constraints. |
| 22:11 | Stock graph for Nvidia/Apple/Alphabet | Agent creates stock chart; likely uses Yahoo Finance or browser-side fetch. | Feasible with caveats around data APIs/rate limits. | Chart widget, market-data adapter/proxy, browser-origin fetch fallback, error states. |
| 23:19 | Snake game | First attempt broken; agent is prompted with bug report and fixes it. | Feasible; important to include because “perfect” one-shot is not guaranteed. | Interactive canvas widget, keyboard focus scoping, iterative patch/debug flow, rollback. |
| 26:05 | Step sequencer / mini synth | Multi-widget audio UI with sequencer, sound controls, guitar free-play. | Feasible but advanced/browser-specific. | WebAudio widget permission model, persisted patterns, keyboard/mouse handling, audio cleanup. |
| 26:52 | Add piano roll | Agent patches/extends music UI with piano roll; alignment fixed by resizing. | Feasible, should be later-stage demo. | Widget patching, responsive layout testing, resize/rerender hooks. |
| 28:14 | Local inference panel | Download/load/test HuggingFace model in browser/runtime. | Partly different in Capy. Capy already uses LM Studio/local providers; browser-side HF inference is optional, not necessary for parity. | Provider settings UI, LM Studio integration, optional browser-local inference if desired. |
| 29:53 | Agent-created modules | Agent can create per-user/per-group/global modules beyond spaces. | Feasible but should be controlled and not MVP. | Module/plugin system, signed/approved extensions, capability scopes, recovery mode. |
| 41:27 | User + agent cooperate on same browser page, e.g. Google | Agent navigates; user can also interact manually. | Feasible and aligned with Brendan’s visible-browser preference. | Visible browser/CDP bridge, shared control state, captcha/login handoff. |
| 41:48 | Fresh guest setup/API key flow | New user enters OpenRouter key and starts. | Capy equivalent should use existing Hermes model/provider config, not raw widget-stored keys. | Settings UI, secret handling, provider validation. |
| 42:42 | Big Bang first-run space | First new-user space has special onboarding instructions and shows off. | Feasible; recommended. | Seeded onboarding space, space instructions, sample widgets. |
| 43:25 | Time travel | User/group dirs are Git repos; can travel back two hours, return to present, or revert changes. | Required for safe parity. | Hermes checkpoint manager + Git snapshots, per-space revision history, restore UI. |
| 44:18 | Admin/recovery mode | Static firmware admin split view survives broken UI and can run agent/files/time travel/module manager. | Required before allowing powerful self-modification. | Safe-mode route outside generated content, file browser, rollback, repair-agent prompt, module disable switch. |

## Acceptance criteria before saying “every demo works perfectly”

Capy Spaces should not claim full video parity until these are demonstrably true on Brendan’s Mac Studio:

1. Weather widget demo passes from a blank space.
2. Notes app demo supports create/edit/rename/folders/markdown/rich text/image/file attachment persistence.
3. Camera/dashboard demo can render allowed stream URLs and rejects unsafe/private URLs unless approved.
4. Agent Zero/local-service dashboard works against a local test service with API + browser panel.
5. Embedded browser panel supports user+agent co-control and element-reference actions.
6. Research harness supports widget-to-agent triggers, live progress updates, markdown artifact, and PDF export.
7. Kanban demo supports persistent cards/columns and drag/edit interactions.
8. Stock chart demo renders real market data with useful error handling when sources block/rate-limit.
9. Snake demo supports focused keyboard capture only and iterative repair.
10. Music/sequencer/piano-roll demo supports WebAudio, persistence, resize/rerender cleanup.
11. Onboarding Big Bang space exists and can be reset.
12. Time travel/rollback works after every widget/module edit.
13. Safe admin/recovery route can fix or disable broken spaces/widgets/modules.
14. All examples run after reload and survive WebUI restart.
15. All generated/patched UI has tests or golden smoke scripts.

## Design implications for the main plan

Add or emphasize these in `capy-spaces-space-agent-parity.md`:

- Treat browser-surface control as a core primitive, not a later nice-to-have.
- Add widget-to-agent events early; the research harness depends on this.
- Add a safe admin/recovery route before arbitrary advanced widgets or modules.
- Include a demo-parity test suite with fixtures named after the video examples.
- Avoid promising “perfect” one-shot generation; Space Agent itself showed a broken snake first attempt. The real parity target is fast iterative repair with persistence and rollback.
