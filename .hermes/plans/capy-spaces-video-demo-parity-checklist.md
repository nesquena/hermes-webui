# Capy Spaces — Space Agent Demo Video Parity Checklist

Video reviewed: https://www.youtube.com/watch?v=F3ZzNgf-R7Y
Transcript duration: 46:51
Created: 2026-04-27
Last implementation-status update: 2026-05-06

## Current parity implementation notes

Capy Spaces now has implemented foundation slices, so this checklist is no longer purely architectural. Keep status conservative: metadata-only demo smokes and UI affordances are useful progress, but they are not full Space Agent video parity until the acceptance criteria below pass end-to-end on Brendan's Mac Studio.

Recent safe adapter progress:

- Source widget SDK helper coverage now includes metadata-only `space.spaces.defaultWidgetSize`, `normalizeWidgetSize`, and `parseWidgetSizeToken`, matching Space Agent's default size, normalization, token parsing, clamping, and fallback behavior without exposing generated/source/credential-like request markers.
- Source position SDK helper coverage now includes metadata-only `space.spaces.defaultWidgetPosition`, `parseWidgetPositionToken`, and `clampWidgetPosition`, matching Space Agent-style default position, token parsing, and size-aware grid-bound clamping without exposing generated/source/credential-like request markers.
- Source layout helper coverage now includes metadata-only `space.spaces.resolveSpaceLayout`, matching Space Agent's collision-safe placement for preferred positions, anchor overrides, minimized widgets, rendered sizes, and minimized maps without exposing generated/source/credential-like request markers.
- Source runtime property coverage now includes metadata-only `space.spaces.widgetApiVersion`, matching Space Agent's widget API version surface with Capy's current compatibility value while omitting generated/source/credential-like request markers.
- Source open/get helper coverage now includes metadata-only `space.spaces.open` and camelCase `spaceId` payload support for `space.spaces.get` / `read` / `open`, matching another common Space Agent navigation helper without echoing generated or credential-like payload fields.
- Source normalization helper coverage now includes metadata-only `space.spaces.normalizeSpaceId` / `normalizeWidgetId`, matching Space Agent slug behavior for diacritics, underscores, punctuation, and fallback ids without echoing generated or credential-like payload fields.

Recently landed:

- Recovery/safe-mode tool actions are exposed through safe metadata-only aliases.
- Demo smoke routes exist and the Spaces UI uses direct `/api/spaces/demo/*` routes instead of generic demo tool actions.
- Metadata shared data slots exist, show safe details, and can be deleted safely.
- Queued widget events show safe event anchors and UTC timestamps in the UI.
- Model/provider setup now appears in the metadata-only demo smoke catalog as `demo_provider_setup`, backed by the safe Model Provider Setup template, and the Spaces shell now exposes a direct `Run provider setup walkthrough` action that opens the safe widget manager/event inbox without exposing generated renderer/source/API-auth markers.
- Active-space revision and rollback tool aliases now support `space.current.revisions` / `space.current.history` and `space.current.rollback` / `space.current.restore` for metadata-only time-travel operations against the active Capy Space.
- The Research Harness demo smoke now drives the preferred vertical path through safe metadata only: live progress widgets, export-ready markdown artifact metadata, a queued `widget.export.pdf` event, and visible smoke-result status.
- Widget details now surface the safe runtime sandbox/postMessage contract from the `space.widget.runtime_contract` tool route without displaying generated widget bodies or secret-like fields.
- Runtime-contract details now include metadata-only network policy and approval checkpoints, clarifying that generated-code enablement, external navigation, and network fetches require explicit mediation/approval.
- Widget tool aliases now include metadata-only `space.widget.see` / `space.current.widget.see` and reload/refresh aliases that queue bounded `widget.refresh` events instead of executing generated widget code directly.
- Source-style Space Agent aliases now include metadata-only `space.spaces.create` and `space.spaces.get` / `space.spaces.read`, routed through the safe Capy space create/detail primitives while ignoring supplied generated/widget bodies.
- Source-style camelCase helper aliases now include metadata-only `space.spaces.createSpace`, `space.spaces.listSpaces`, and `space.spaces.openSpace` / `getSpace` / `readSpace`, matching additional Space Agent runtime helper names without exposing generated widget bodies or credential-like fields.
- Source-style example/template helper aliases now include metadata-only `space.spaces.installExampleSpace` / `installTemplate`, mapping bundled Space Agent demo ids such as `retro-arcade` onto safe Capy templates instead of copying or rendering untrusted source artifacts.
- Source-style widget upsert helper aliases now include metadata-only `space.spaces.upsertWidget` / `upsertWidgets`, preserving bounded declarative widget metadata while omitting generated/executable bodies and credential-like payloads.
- Source-style widget patch helper aliases now include metadata-only `space.spaces.patchWidget`, accepting Space Agent-style `spaceId`/`widgetId` payloads while applying only safe declarative metadata patches and omitting generated/executable bodies plus credential-like payloads from serialized results.
- Source-style widget delete helper aliases now include metadata-only `space.spaces.deleteWidget` / `removeWidget`, accepting Space Agent-style `spaceId`/`widgetId` payloads while deleting through Capy's revisioned primitive and omitting generated/executable bodies plus credential-like payloads from serialized results.
- Source-style bulk widget removal aliases now include metadata-only `space.spaces.removeWidgets` / `deleteWidgets` and `space.spaces.removeAllWidgets` / `deleteAllWidgets`, accepting Space Agent-style `spaceId` plus `widgetIds` where applicable while deleting through Capy's revisioned primitive and returning bounded metadata only.
- Source-style space removal aliases now include metadata-only `space.spaces.removeSpace` / `deleteSpace`, accepting Space Agent-style `spaceId` while deleting through Capy's revisioned primitive and returning bounded metadata only.
- Source-style space duplication aliases now include metadata-only `space.spaces.duplicateSpace` / `cloneSpace`, accepting Space Agent-style `spaceId` while copying only safe Space metadata and widget summaries into a new persisted Space.
- Source-style widget read helper aliases now include metadata-only `space.spaces.listWidgets`, `readWidget`, and `getWidget`, accepting Space Agent-style `spaceId`/`widgetId` payloads while returning only safe widget summaries/details.
- Current widget event bridge aliases now accept Space Agent-style camelCase `activeSpaceId`/`widgetId` payloads for `space.current.widget.event` and `space.current.widget.events`, while keeping queued/listed event responses metadata-only and redacted.
- Source-style current-space viewport helper coverage now includes metadata-only `space.spaces.repositionCurrentSpace`, accepting `spaceId`/`resetCamera`/`viewport` payloads without executing browser movement or exposing generated/credential-like fields.
- Source-style widget layout helper coverage now includes metadata-only `space.spaces.rearrangeWidgets`, accepting `spaceId` plus Space Agent-style widget `position`/`size` or `col`/`row`/`cols`/`rows` payloads and persisting safe Capy widget layouts without exposing generated/credential-like fields.
- Source-style layout recovery helper coverage now includes metadata-only `space.spaces.repairLayout`, applying saved `saveSpaceLayout` widget positions/sizes/minimized metadata to existing widgets, clamping unsafe layout values, and persisting a revisioned safe layout repair without exposing generated/credential-like fields.
- Source-style widget visibility helper coverage now includes metadata-only `space.spaces.toggleWidgets`, accepting `spaceId`/`widgetIds` payloads and toggling target widget `layout.minimized` metadata without exposing generated/credential-like fields.
- Source-style current-space lookup coverage now includes metadata-only `space.spaces.getCurrentSpace`, accepting `activeSpaceId`/current-space payloads and returning safe current Space detail metadata or a null current-space response without exposing generated/credential-like fields.
- Source-style current-widget helper aliases now include metadata-only `space.current.listWidgets`, `space.current.readWidget`, and `space.current.seeWidget`, accepting `activeSpaceId`/`widgetId` payloads while returning only safe widget summaries/details plus sandbox-contract and queued-event metadata.
- Source-style current-widget mutation aliases now include metadata-only `space.current.patchWidget` and `space.current.reloadWidget`, accepting `activeSpaceId`/`widgetId` payloads while applying only safe title/layout metadata patches or queuing bounded refresh-event metadata.
- Source-style current-widget removal aliases now include metadata-only bulk deletion via `space.current.removeWidgets` / `deleteWidgets` and `space.current.removeAllWidgets` / `deleteAllWidgets`, accepting `activeSpaceId` and preserving revisioned deletes without exposing generated/credential-like fields.
- Source-style logical app URL helper coverage now includes metadata-only `space.spaces.resolveAppUrl`, accepting safe app-owned logical paths while rejecting external, traversal, query/fragment, and private filesystem paths without echoing raw unsafe input.
- Source-style current-space metadata/layout helpers now include metadata-only `space.current.saveMeta` and `space.current.saveLayout`, accepting `activeSpaceId` payloads while saving only safe Capy Space metadata/layout fields and omitting generated/credential-like fields from responses.
- Source-style widget size utility coverage now includes metadata-only `space.spaces.sizeToToken`, mirroring Space Agent preset/object/fallback size normalization and clamping without exposing generated/source/API auth markers.
- Source-style runtime collection property coverage now includes metadata-only `space.spaces.items`, `space.spaces.all`, `space.spaces.byId`, `space.spaces.current`, `space.spaces.currentId`, `space.current.byId`, `space.current.agentInstructions`, and `space.current.specialInstructions`, matching another Space Agent runtime namespace shape without exposing generated widget bodies or credential-like request fields.
- The Spaces shell now exposes a direct `Run research walkthrough` action that launches the safe Research Harness PDF-export smoke from the main Spaces toolbar and renders the progress/rollback status plus the safe widget manager/event inbox for the demo Space without exposing generated renderer/source/API-auth markers.
- The Spaces shell now exposes a direct `Run kanban walkthrough` action that launches the safe Kanban board smoke from the main Spaces toolbar and renders the board preview plus the safe widget manager/event inbox for the demo Space without exposing generated renderer/source/API-auth markers.
- The Spaces shell now exposes a direct `Run notes walkthrough` action that launches the safe Notes app smoke from the main Spaces toolbar, renders the notes checklist/folder preview/attachment preview, queues one safe `notes.save` event for `notes-editor`, and opens the safe widget manager/event inbox for the demo Space without exposing generated renderer/source/API-auth markers.
- The demo smoke suite now surfaces a compact safe Weather observation / agent-bridge summary for the weather vertical in `Run all smokes`, making the suite output more visibly end-to-end while still omitting raw prompt/answer text and generated/source/API-auth markers.
- The Weather demo widget manager now shows the starter prompt hint and ready-for-agent-refresh state from safe widget-list metadata before the first refresh, making the visible weather vertical clearer without exposing generated/source/API-auth markers.
- The Weather demo smoke now records the Prague weather observation, queues one safe `widget.refresh` event for the persistent weather widget, surfaces that queued bridge count in the visible demo-smoke status, and shows an inline `Agent bridge` status in the weather widget manager without exposing prompts/generated/source/API-auth markers.
- The Camera Dashboard demo now has a direct `Run camera walkthrough` action from the main Spaces toolbar. It launches the safe metadata-only camera smoke, shows the persistent camera grid/permission/incident widgets, and opens the safe widget manager without exposing generated renderer/source/API-auth markers.
- The Browser Surface / browser co-control demo now has a direct `Run browser walkthrough` action from the main Spaces toolbar. It launches the safe metadata-only browser co-control smoke, shows the persistent browser panel/control/notes widgets, and opens the safe widget manager/event inbox without exposing generated renderer/source/API-auth markers.
- The Spaces shell now exposes a direct `Run weather walkthrough` action that launches the safe prompt → answer → persistent widget smoke from the main Spaces toolbar and renders the weather checklist/current observation without exposing generated renderer/source/API-auth markers.
- The Notes app install completion card now includes a direct `Run notes smoke` action that exercises the existing metadata-only saved-notes preview path from the installed demo card without exposing generated renderer/source/API-auth markers.
- The Kanban board install completion card now includes a direct `Run kanban smoke` action that exercises the existing metadata-only board preview path from the installed demo card without exposing generated renderer/source/API-auth markers.
- The Local Service Dashboard install flow now produces a visible metadata-only completion card with direct open/manage actions for the Agent Zero/local-service dashboard demo path, without exposing generated renderer or credential-like fields.
- Screenshot QA artifacts are expected for visually relevant Spaces work.

Next checkpoint emphasis:

1. Safe admin/recovery and rollback/time-travel before richer generated widgets or local-service dashboards.
2. Research Harness as the preferred vertical demo: widget event → Capy run → live progress widgets → markdown artifact → PDF/export patch → rollback.
3. Explicit sandbox/postMessage/event contract before trusted generated UI expands.
4. Demo parity matrix in `capy-spaces-space-agent-parity.md` must stay current with fixture, route, UI-test, screenshot, and security status.

## Bottom line

The demo does **not** introduce a new product direction beyond the existing Capy Spaces plan. It does clarify the exact parity bar:

- Capy Spaces needs a persistent visual workspace/canvas.
- The agent must create and update widgets from natural language.
- Widgets must be able to cooperate through shared per-space data/files.
- Browser panels must be first-class, inspectable, and jointly controllable by user + agent.
- Space-specific instructions must influence the agent.
- UI widgets must be persistent and reconstructible after reload.
- Git/checkpoint rollback and a safe recovery/admin surface are not optional.

Capy Spaces is now partially implemented, but I cannot honestly claim these examples work **perfectly today**. The target architecture should continue to explicitly test each demo example before declaring Space Agent parity.

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
