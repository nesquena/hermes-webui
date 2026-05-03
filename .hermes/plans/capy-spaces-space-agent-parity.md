# Capy Spaces: Space Agent Feature-Parity Architecture Plan

Created: 2026-04-27 23:28 CDT
Research targets:
- Space Agent checkout: `/tmp/space-agent` at `1289793`
- Local Space Agent reference clone: `/Users/bschmidy10/workspace/space-agent-reference` at `1289793bab727a46e62365992a65ffb3476c4091` (`v0.64`)
- Hermes WebUI checkout: `/Users/bschmidy10/hermes-webui` at `5720fa5`
- Hermes Agent checkout: `/Users/bschmidy10/.hermes/hermes-agent` at `e403379b`

## Current Implementation Status

Last updated: 2026-05-03 13:03 CDT on branch `feat/capy-spaces-foundation`.

Current latest known completed code slice: source logical app URL resolution now supports Space Agent-style `space.spaces.resolveAppUrl` calls while preserving Capy's metadata-only adapter boundary. The server-side adapter accepts source-style `logicalPath` / `logical_path` / `path` payloads, resolves only safe app-owned logical paths (`~`, `~/...`, `/~/...`, `/app/L0...`, `L0`/`L1`/`L2`), rejects external/dangerous filesystem/query/fragment paths without echoing raw input, and omits source/API auth markers from serialized responses. Use `git log -1 --oneline` for the exact commit hash.

Recent completed slices:

- `feat(spaces): support source resolve app URL alias`
  - Added RED/GREEN backend coverage proving `space.spaces.resolveAppUrl` accepts Space Agent-style logical app paths, returns safe app URLs for home, user-space assets, `/app/...` module paths, and `L0`/`L1`/`L2` module paths, rejects `javascript:`, external HTTPS, relative traversal, query-string credential markers, and private filesystem roots without echoing raw unsafe input, and omits source/API auth markers from serialized adapter results.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`). Run `git log -1 --oneline` and the final report for the full validation bundle.

- `feat(spaces): support source get current space alias`
  - Added RED/GREEN backend coverage proving `space.spaces.getCurrentSpace` accepts Space Agent-style `activeSpaceId` payloads, returns safe current Space detail metadata, returns `{space: None}` when no active Space is supplied, and omits executable fields plus credential-like markers from serialized adapter results.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`121 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock/status screenshot QA captured the alias status with empty browser console, `window.__harnessErrors=[]`, visible leak check false, and screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_8931e002bee9441ea9b9229337de818e.png`. Local/tailnet WebUI health verified OK after restart.

- `feat(spaces): support source toggle widgets alias`
  - Added RED/GREEN backend coverage proving `space.spaces.toggleWidgets` accepts Space Agent-style camelCase `spaceId`/`widgetIds` payloads, flips target widgets' `layout.minimized` metadata in both directions, persists those metadata-only changes, and omits executable fields plus credential-like markers from serialized adapter results.
  - Validation at completion before screenshot/restart: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`121 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed.

- `feat(spaces): support source render widget alias`
  - Added RED/GREEN backend coverage proving `space.spaces.renderWidget` accepts Space Agent-style camelCase `spaceId`/`widgetId` payloads, maps source layout size/position fields into safe Capy widget layout metadata, marks generated-body inputs quarantined/disabled, and omits executable fields plus credential-like markers from stored/public metadata.
  - Validation at completion before screenshot/restart: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`). Run `git log -1 --oneline` and the final report for the full validation bundle.

- `feat(spaces): support source repair layout alias`
  - Added RED/GREEN backend coverage proving `space.spaces.repairLayout` accepts Space Agent-style camelCase `spaceId`, applies saved `widgetPositions`/`widgetSizes`/`minimizedWidgetIds` layout metadata to existing widgets, clamps unsafe/out-of-range layout values, persists the repaired metadata, and omits generated/executable fields plus credential-like markers.
  - Validation at completion before screenshot/restart: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`119 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed.

- `feat(spaces): support source rearrange widgets alias`
  - Added RED/GREEN backend coverage proving `space.spaces.rearrangeWidgets` accepts Space Agent-style camelCase `spaceId` plus widget layout payloads, maps source `position`/`size` and `col`/`row`/`cols`/`rows` fields into safe Capy widget layouts, persists those metadata-only layout changes, and omits generated/executable fields plus credential-like markers.
  - Validation at completion before screenshot/restart: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`118 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed.

- `feat(spaces): support source reposition alias`
  - Added RED/GREEN backend coverage proving `space.spaces.repositionCurrentSpace` accepts Space Agent-style camelCase `spaceId`/`resetCamera`/`viewport` payloads, returns safe current Space detail metadata plus a sanitized reposition request summary, does not mutate stored layout, and omits generated/executable fields plus credential-like markers.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`117 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and mock/status screenshot QA captured the alias status with empty browser console, `window.__harnessErrors=[]`, visible leak check clean, and screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_03e2e453fbba47e692e71bb07f5ecf4b.png`. Local/tailnet WebUI health should be verified after commit and LaunchAgent restart.

- `feat(spaces): support source reload aliases`
  - Added RED/GREEN backend coverage proving `space.spaces.reloadWidget` accepts Space Agent-style camelCase `spaceId`/`widgetId`, queues safe refresh event metadata, and `space.spaces.reloadCurrentSpace` returns safe current Space detail metadata while omitting generated/executable fields plus credential-like markers.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`116 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock/status screenshot QA captured the alias status with empty browser console, `window.__harnessErrors=[]`, visible leak check clean, and screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_cc0ce4a71d044e1dafdc32b108955496.png`. Local/tailnet WebUI health verified OK after restart.

- `feat(spaces): support camelcase widget event aliases`
  - Added RED/GREEN backend coverage proving `space.current.widget.event` and `space.current.widget.events` accept camelCase `activeSpaceId`/`widgetId`, queue/list safe widget event metadata, and omit generated/executable payload fields plus credential-like markers.
  - Validation at completion: focused RED failed before implementation with `Invalid widget_id`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`116 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock/status screenshot QA captured the alias status with empty browser console, `window.__harnessErrors=[]`, visible leak check false, and screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_a271830eb4e446d6b98f91e9564efb68.png`. Local health returned OK on attempt 2 after restart and tailnet `/health` returned OK.

- `feat(spaces): support source widget read aliases`
  - Added RED/GREEN backend coverage proving `space.spaces.listWidgets`, `readWidget`, and `getWidget` accept Space Agent-style camelCase `spaceId`/`widgetId` payloads, return safe widget summaries/details, and omit generated/executable bodies plus credential-like markers.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`115 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock/status screenshot QA captured the alias status with empty browser console and a clean visible safety-marker check; screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_0a61b4f7766743658d0eb2bdf1715e1d.png`.

- `feat(spaces): support source space duplicate alias`
  - Added RED/GREEN backend coverage proving `space.spaces.duplicateSpace` accepts Space Agent-style camelCase `spaceId` payloads, creates a safe copied Space with widget summaries and metadata, and omits generated/executable bodies plus credential-like markers from serialized adapter results and the persisted duplicate.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`113 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock/status screenshot QA captured the duplicate alias status with empty browser console and a clean sensitive-marker DOM check.

- `feat(spaces): support source space delete aliases`
  - Added RED/GREEN backend coverage proving `space.spaces.removeSpace` and `space.spaces.deleteSpace` accept Space Agent-style camelCase `spaceId` payloads, delete Spaces through Capy's revisioned primitive, and omit generated/executable bodies plus credential-like markers from serialized adapter results.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`112 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock/status screenshot QA captured the alias status with empty browser console and a clean sensitive-marker DOM check; screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_c25825dd57914d4fa087937684be2d44.png`.

- `feat(spaces): support source bulk widget delete aliases`
  - Added RED/GREEN backend coverage proving `space.spaces.removeWidgets` accepts Space Agent-style camelCase `spaceId`/`widgetIds` payloads, `space.spaces.removeAllWidgets` removes all safe widget summaries for a Space, both routes delete through Capy's revisioned primitive, and responses omit generated/executable bodies plus credential-like markers.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`111 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock/status screenshot QA captured the alias status with empty browser console and a clean sensitive-marker DOM check; screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_0cec2fea852e4b459ed01713dc1782d7.png`.

- `feat(spaces): support source widget delete aliases`
  - Added RED/GREEN backend coverage proving `space.spaces.deleteWidget` and `space.spaces.removeWidget` accept Space Agent-style camelCase payloads, delete widgets through Capy's revisioned metadata-only primitive, and omit generated/executable bodies plus credential-like markers from serialized adapter results.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`). Mock/status screenshot QA captured the alias status with empty browser console and a clean sensitive-marker DOM check; screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_c31ef34648784f41896352556399fa50.png`.

- `feat(spaces): support source widget patch alias`
  - Added RED/GREEN backend coverage proving `space.spaces.patchWidget` accepts Space Agent-style camelCase payloads, patches safe widget metadata such as title/layout/weather, and omits renderer/html/script/source/data plus credential-like markers from serialized adapter/detail results.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`109 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock/status screenshot QA captured the alias status with empty browser console and a clean sensitive-marker DOM check; screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_37d569344cad479bb44c2de17f28b573.png`.

- `feat(spaces): support source widget upsert aliases`
  - Added RED/GREEN backend coverage proving `space.spaces.upsertWidget` and `space.spaces.upsertWidgets` accept Space Agent-style widget payloads, preserve bounded declarative metadata such as layout/weather/notes, map `type` to `kind`, and omit generated/executable bodies plus credential-like markers from stored adapter metadata and serialized results.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`108 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock/status screenshot QA captured the alias status with empty browser console and a clean sensitive-marker DOM check; screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_604553d15ed443c7aebd2c458972a494.png`.

- `feat(spaces): support source example installer alias`
  - Added RED/GREEN backend coverage proving `space.spaces.installExampleSpace` accepts a Space Agent-style `{id, sourcePath}` payload for the `retro-arcade` example, maps it to Capy's safe metadata-only Game Sandbox template, and omits raw `sourcePath`, generated widget bodies, event handlers, and credential-like markers from serialized results.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`107 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock/status screenshot QA captured the alias status with empty browser console and no visible secrets; screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_94d12f4b68fc472ab410bfddfbdce16c.png`.

- `feat(spaces): edit notes widgets from detail view`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Notes widget detail view renders editable notes metadata, posts only `{notes: {body, format, updated_from}}` through the typed widget patch API, refreshes safe detail metadata, and omits generated/secret-like fields from DOM.
  - Validation at completion: focused RED failed before implementation because no `api/spaces/widget/patch` request was sent; focused GREEN passed (`1 passed`), full Spaces UI behavior suite passed (`71 passed`), `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, and `git diff --check` passed. Mock-state browser QA captured widget manager and Notes detail/save states with empty `window.__harnessErrors` and visible leak regex false; screenshot artifacts `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_97ebdbc048a649a1b3c40b3430541d7a.png` and `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_612685d1fcee47ad932ccea2e5ceb2d7.png`.

- `feat(spaces): support source camelcase space aliases`
  - Added RED/GREEN backend coverage proving Space Agent source-style camelCase aliases `space.spaces.createSpace`, `space.spaces.listSpaces`, and `space.spaces.openSpace` / `getSpace` / `readSpace` route through Capy's safe metadata-only create/list/detail primitives while ignoring supplied generated/widget bodies.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`106 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock/status screenshot QA captured the alias status with empty browser console and no visible secrets; screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_1ed4676bfe3948889ecde8b01d1338b4.png`.

- `feat(spaces): support source-style space create/get aliases`
  - Added RED/GREEN backend coverage proving `space.spaces.create` creates a safe metadata-only Space while ignoring supplied widget/generated bodies, and `space.spaces.get` reads safe detail metadata without exposing renderer/html/script/source/data fields or credential-like values.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`105 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock-state browser QA captured the alias status with empty browser console and no visible secrets; screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_155bd084d7654cf4841cf43e49f2e9d6.png`.

- `feat(spaces): support widget see and reload aliases`
  - Added RED/GREEN backend coverage proving `space.widget.see` and `space.current.widget.see` return safe widget detail plus the sandbox runtime contract, and `space.current.widget.reload` queues a metadata-only `widget.refresh` event while omitting renderer/html/script/source/data fields and credential-like values from results/event inboxes.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`105 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock-state screenshot/status QA captured the new alias results with empty browser console and no visible secrets; screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_0447bc8d55d54955938b6333fe8dc8ba.png`.

- `feat(spaces): warn on imported space.spaces APIs`
  - Added RED/GREEN backend coverage proving imports report metadata-only warnings for unsupported Space Agent `space.spaces.*` references such as `space.spaces.create` and `space.spaces.list`, while preserving the existing `space.current.*` warning behavior and continuing to omit raw action maps, generated renderer/source fields, and credential-like values from import/export responses.
  - Validation at completion: focused RED failed before implementation because only `space.current.*` APIs were warned on; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`104 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Backend-only screenshot/status QA showed the new warning coverage with empty browser console and no visible secrets; screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_db61eb3123424489ada3cb4213c553d1.png`.

- `feat(spaces): warn on unsupported import APIs`
  - Added RED/GREEN backend and real-`static/spaces.js` coverage proving imports report metadata-only warnings for unsupported `space.current.*` references and render the warnings without exposing raw YAML, widget paths, generated renderer/script fields, action maps, or credential-like values.
  - Validation at completion: focused RED tests failed before implementation due to missing `warnings` / UI warning rendering; focused GREEN tests passed (`3 passed`), Spaces foundation + UI behavior suites passed (`174 passed`), `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py tests/test_spaces_ui_js_behaviour.py`, and `git diff --check` passed. Mock-state browser QA showed the import warning card with empty `window.__harnessErrors` and DOM leak check false; screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_010aabc8cd1740d8b2258687167c3632.png`.

- `feat(spaces): extend widget runtime contract policy`
  - Added RED/GREEN backend and real-`static/spaces.js` coverage proving the runtime-contract tool route exposes `network_policy` and `approval_required_for` metadata and the widget detail view renders only sanitized policy/checkpoint text.
  - Validation at completion: focused RED tests failed before implementation due to missing network/approval fields; focused GREEN tests passed (`2 passed`), Spaces foundation + UI behavior suites passed (`174 passed`), `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py tests/test_spaces_ui_js_behaviour.py`, and `git diff --check` passed. Mock-state browser QA rendered policy/approval fields with `window.__harnessErrors=[]` and DOM leak check false; screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_effa90c9933142d598b521d4c395ccc7.png`.

- `feat(spaces): show widget runtime contract in details`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the widget detail flow calls `space.widget.runtime_contract`, renders the metadata-only mode/execution/allowed/blocked message contract, and continues to omit generated renderer/html/data fields plus credential-like values from DOM.
  - Validation at completion: focused RED test failed before implementation due to the missing `/api/spaces/tool` runtime-contract call; focused GREEN tests passed (`4 passed`), Spaces UI + foundation suites passed (`174 passed`), `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_ui_js_behaviour.py tests/test_spaces_foundation.py`, and `git diff --check` passed.

- `feat(spaces): request research demo PDF export`
  - Added RED/GREEN backend and real-`static/spaces.js` regressions proving `demo_research_harness_pdf_export` advances Research Harness progress, records a safe artifact summary, queues a metadata-only PDF export event, and renders only safe smoke status metadata in the UI.
  - Validation at completion: focused RED tests failed as expected before implementation (`action` still `installed`, UI omitted action/queued-event status); focused GREEN tests passed (`2 passed`), relevant Spaces demo/UI/foundation suites passed (`178 passed`), `py_compile api/spaces.py tests/test_spaces_demo_parity.py tests/test_spaces_ui_js_behaviour.py`, `node --check static/spaces.js`, and `git diff --check` passed. Browser QA used a `/tmp/capy-spaces-progress/` mock harness with screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_4001ef5f03e14f19be7b79d8709fc8c4.png`.
- `feat(spaces): expose research run routes`
  - Added RED/GREEN route regressions proving direct Research Harness progress/artifact HTTP routes update typed Capy Space metadata while omitting raw markdown, executable/generated fields, and credential-like values.
  - Validation at completion: focused RED route tests failed as expected before implementation; focused GREEN route tests passed (`2 passed`), Spaces foundation + demo parity suites passed (`107 passed`), `py_compile api/spaces.py api/routes.py tests/test_spaces_foundation.py` and `git diff --check` passed. Mock/status screenshot QA had empty `window.__harnessErrors` and no sensitive leak regex matches; artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_97724956191744c7bf5a81959b8968b3.png`.
- `2f1f731 feat(spaces): update research harness progress`
  - Added safe metadata-only helpers/tool actions for updating Research Harness plan/source/notes widgets from agent progress events without exposing generated or credential-like payloads.
- `c3897f6 feat(spaces): mark research artifacts export-ready`
  - Added safe markdown artifact metadata summaries and PDF-export readiness markers for the Research Harness summary widget.
- `addd152 feat(spaces): preview revision restore targets`
  - Added RED/GREEN backend and real-`static/spaces.js` UI regressions proving revision history exposes safe restore-preview metadata from snapshots while omitting generated renderer/script/source/data fields and secret-looking values.
  - Validation at completion: focused RED tests failed as expected before implementation; focused GREEN tests passed (`2 passed`), full Spaces foundation + UI behavior suites passed (`167 passed`), relevant combined Spaces suites passed (`171 passed`), `node --check`, `py_compile`, and `git diff --check` passed, local WebUI health OK, mock-state browser QA screenshot captured at `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_4f53c64e33054c35bd9efaddef629a04.png` with empty harness errors and no DOM leak.
- `feat(spaces): show recovery event status`
  - Added RED/GREEN backend and real-`static/spaces.js` UI regressions proving recovery metadata shows queued repair/event status while omitting prompt text, payload summaries, renderer/script fields, and secret-looking values.
  - Fixed the stale demo parity catalog test so the provider setup smoke added in the previous slice is counted in full-suite validation.
  - Validation at completion: focused recovery event-status tests passed (`2 passed`), Spaces UI JS behavior suite passed (`69 passed`), Spaces foundation suite passed (`97 passed`), demo parity suite passed (`4 passed`), relevant combined suite passed (`170 passed`), full WebUI suite passed (`2955 passed`, `1 warning`, `8 subtests passed`), `py_compile` and `git diff --check` passed, WebUI health OK, mock-state browser QA screenshot captured.
- `feat(spaces): include queued event anchors in context`
  - Added a RED/GREEN backend regression proving `space.current.context` includes queued widget event anchors while omitting prompt text, renderer/script fields, and secret-looking payloads.
  - Validation at completion: focused context test passed, full Spaces foundation suite passed (`96 passed`), `py_compile` and `git diff --check` passed.
- `feat(spaces): show event bridge details in widget UI`
  - Added a RED/GREEN real-`static/spaces.js` regression proving widget detail fetches top-level `event_bridge` metadata, renders only safe key summaries, and omits `api_key`, secrets, generated renderer/html/data fields, and script/error markers.
  - Mock-state browser QA artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_6ed20107db85488d8f127e9a22a1efb4.png`.
- `feat(spaces): expose event bridge metadata in details`
  - Added `event_bridge` to the allowlisted widget detail metadata keys after a RED/GREEN backend regression test.
  - Keeps `api_key` and generated renderer/html/data fields omitted from serialized widget detail responses.
  - Screenshot QA artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_9def59eab69b4b5083b264b36ee476c3.png`.
- `852cc03 feat(spaces): expose recovery tool actions`
  - Added safe recovery/safe-mode Space tool-adapter aliases.
  - Validation at completion: focused recovery test passed, related recovery tests passed, broader Spaces tests passed, full WebUI suite passed.
- `a474570 feat(spaces): expose demo smoke routes`
  - Added direct demo smoke API routes used by later demo parity tooling.
- `e2499e4 feat(spaces): add metadata shared data slots`
- `b381e21 feat(spaces): show safe shared data details`
- `577f224 feat(spaces): delete shared data slots safely`
- `b344b83 feat(spaces): use direct demo smoke routes`
  - Spaces demo smoke UI uses `GET /api/spaces/demo/runs`, `POST /api/spaces/demo/run`, and `POST /api/spaces/demo/run-all` instead of generic `space.demo*` tool-adapter calls.
  - Screenshot QA artifact: `/tmp/capy-screenshots/spaces-demo-direct-routes.png`.
- `8078b38 feat(spaces): show queued widget event anchors`
  - Widget event inbox shows safe `Event: <event_id>` anchors plus UTC timestamps while keeping prompt/payload details redacted and bounded.
  - Screenshot QA artifact: `/tmp/capy-screenshots/spaces-queued-event-anchors.png`.
- `feat(spaces): queue recovery repair prompts`
  - Safe recovery panel now offers an “Ask Capy to repair” widget action that queues a metadata-only `agent.repair` event without rendering generated widget bodies.
  - Fails closed when the shared prompt dialog is unavailable.
- `feat(spaces): include provider setup in demo smokes`
  - Added the model/provider setup template to the metadata-only Space Agent demo smoke catalog as `demo_provider_setup`.
  - Validation at completion: focused demo-smoke route test passed, full Spaces foundation suite passed (`95 passed`), `py_compile` and `git diff --check` passed.
- `feat(spaces): use active space for revision tool aliases`
  - Added `space.current.revisions` / `space.current.history` and `space.current.rollback` / `space.current.restore` aliases so Hermes-style tool calls can list and restore revision snapshots from the active Space without raw generated bodies.
  - Validation at completion: focused active-space rollback adapter test passed, full Spaces foundation suite passed (`96 passed`), `py_compile` and `git diff --check` passed.

Last known validation bundle:

- RED check for research direct routes: progress/artifact route tests failed as expected before implementation (missing routes produced no JSON response).
- Focused research route regressions: passed (`2 passed`).
- Spaces foundation + demo parity suites: passed (`107 passed`).
- `py_compile api/spaces.py api/routes.py tests/test_spaces_foundation.py`: passed.
- `git diff --check`: passed.
- Browser QA: mock/status screenshot page rendered the two new routes and validation status, `window.__harnessErrors` was empty, and leak regex for `<script>|renderer|SECRET|api_key|token` against visible text was false. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_97724956191744c7bf5a81959b8968b3.png`.
- WebUI local/tailnet health: verify after commit and LaunchAgent restart.

Known warning: unknown `pytest.mark.integration` in `tests/test_onboarding_network.py`.

Keep this section current after each Capy Spaces sprint slice so future agents can compare plan intent, branch state, tests, screenshots, and remaining gates without relying on chat history.

## 2026-04-28 Source Recreation Validation

Verdict: Space Agent is practically recreateable from source and useful as a live reference implementation for Capy Spaces parity work.

Validated facts:

- License is MIT, so source use, modification, redistribution, sublicensing, and commercialization are allowed with license/copyright notice preservation.
- A fresh local reference clone exists at `/Users/bschmidy10/workspace/space-agent-reference`.
- `npm install --omit=optional` completed successfully under Node `v22.22.2` / npm `10.9.7`.
- `node space version` prints `v0.64`.
- A single-user local smoke run succeeded with:
  - `CUSTOMWARE_PATH=/tmp/space-agent-reference-smoke-customware`
  - `SINGLE_USER_APP=true`
  - `HOST=127.0.0.1`
  - `PORT=39221`
- Smoke `/api/health` returned `{ "ok": true, "name": "space-agent-server", ... "source": "single-user-app", "username": "user" }`.
- Root `/` and `/enter` returned `200` during smoke. A guessed `/mod/_core/framework/main.js` path returned `404`, which is not a server boot failure and should be rechecked with actual module asset paths when doing browser QA.
- `npm audit` still reports one high-severity `lodash` advisory, so do not expose the reference app beyond localhost/Tailscale-gated development without dependency/security review.

Reference run command for future local QA:

```bash
cd /Users/bschmidy10/workspace/space-agent-reference
CUSTOMWARE_PATH=/Users/bschmidy10/.space-agent-reference-customware \
  SINGLE_USER_APP=true \
  HOST=127.0.0.1 \
  PORT=39221 \
  node space serve
```

Do not push or fork upstream automatically from autonomous Capy Spaces jobs. Treat this clone as read-only reference unless Brendan explicitly asks for direct Space Agent fork development.

## Active Sprint Guidance From Source Review

Scheduled Capy Spaces sprint cycles should now prefer these source-derived slices, in order, unless the repo already contains the slice:

0. Keep this plan's current-status section updated after every sprint slice: branch, latest commit, tests, service health, screenshot artifacts, and known warnings.
1. Promote safe admin/recovery plus rollback/time-travel to the next hard gate. Do not enable powerful generated/script widgets, local-service dashboards, agent-created modules, hosted sharing, or broad import trust until recovery/rollback can disable or restore broken spaces/widgets without rendering generated content.
2. Reconcile the current Capy Spaces data model against Space Agent's `~/spaces/<spaceId>/space.yaml` + `widgets/<widgetId>.yaml` schema while preserving Capy's stricter metadata-only/sandbox-first rules.
3. Add or harden missing safe `space.current` / `space.spaces`-style backend helpers as Capy-native Python APIs and WebUI routes; keep list/detail responses metadata-only.
4. Add or harden widget patch/reload/revision primitives before any arbitrary widget renderer execution.
5. Add source-derived prompt/context injection for active space metadata only: id, title, description, instructions, widget summary rows, event anchors, and revision id. Never inject raw renderer/html/script/data bodies.
6. Use one vertical demo as the next north star before expanding every demo horizontally. Preferred next vertical: Research Harness — widget event → scoped Capy prompt → live progress widgets → markdown artifact → PDF/export patch → rollback recovery → screenshot QA.
7. Define and test the sandbox/postMessage contract before richer widgets: allowed widget kinds, iframe/same-origin boundaries, event schemas, URL/network policy, approval points, and redaction rules.
8. Build demo parity templates in safe metadata-only increments and track them in the demo parity matrix: Weather Demo, Research Harness, Kanban, Notes, Browser Surface, Stock Chart, Game, Sequencer, Big Bang onboarding.
9. Add safe import/export compatibility with Space Agent ZIP/YAML layouts; imported JS renderer strings must be stored as disabled/untrusted artifacts pending explicit sandbox handling.
10. Defer hosted/public sharing until a threat model review. Local metadata-only export and explicit Telegram send/share can come first; hosted share requires sandbox, approval, and URL/data exposure tests.

## Executive Summary

Space Agent is a browser-first agent runtime: a thick client-side app shell where an onscreen agent can inspect and mutate a user-owned “space” made of YAML-backed widget definitions, customware layers, browser surfaces, skills, prompt extensions, and file-backed state. Its server is intentionally thin: authentication, file routing, proxying, shared-data integrity, runtime params, optional Git history, sharing, and packaged desktop hosting.

Capy/Hermes should not directly clone Space Agent’s trust model or generated-browser-JavaScript execution loop. The best implementation is a native **Capy Spaces** layer inside Hermes WebUI backed by Hermes Agent primitives:

1. Hermes Agent remains the authoritative tool/runtime engine.
2. Hermes WebUI becomes the visual workspace/canvas host.
3. A new `spaces` backend module stores structured space/widget/app state under profile-aware WebUI state or workspace-local `.capy/spaces/` roots.
4. Widgets are declarative, sandboxed, versioned artifacts, not arbitrary unbounded same-origin JS by default.
5. Agents create and edit spaces via first-class Hermes tools/APIs, not via raw browser eval.
6. Space Agent’s best UX ideas — spaces, widgets, browser panels, skills, imports/exports, sharing, current-space prompt context, onboarding, and local history — are replicated using safer Capy-native interfaces.

The end goal is **functional parity plus a stronger architecture**: a user can ask Capy to create a dashboard/workspace, add live widgets, browse websites, inspect/modify files, generate reports, manage memory/skills/cron jobs, package/share a space, roll back changes, and continue across Telegram/WebUI/local sessions.

### Demo-Video Parity Covenant

After reviewing the Space Agent demo video (`https://www.youtube.com/watch?v=F3ZzNgf-R7Y`, 46:51), the concrete parity bar is no longer abstract “spaces and widgets.” Capy Spaces must be able to reproduce the full demo arc on Brendan’s Mac Studio before we claim Space Agent-level parity:

1. Blank-space prompt → weather answer → persistent weather widget.
2. Prebuilt/generated prices, charts, news, and daily dashboards.
3. Browser games and canvas/WebAudio interactions with correct focus isolation.
4. A real notes app with folders, rename, WYSIWYG editing, markdown mode, copy/paste, images, and attachments.
5. Camera/surveillance dashboards with explicit URL/network permission handling.
6. Local-agent/service dashboards: API chat widget plus embedded browser UI for another local app/agent.
7. Browser-surface control where Capy and the user cooperate on the same live page, including captcha/login handoff.
8. Research harness widgets where the UI can send prompts back to Capy, Capy browses/researches, and widgets update planning/source/notes/summary live.
9. Agent-patched widget features such as “add export to PDF” without losing persistence or rollback.
10. Trello-style Kanban board with persistent cards/columns and direct manipulation.
11. Stock chart widgets using browser-origin or approved backend data fetches with robust error states.
12. Iterative repair of imperfect generated UI, e.g. the demo’s broken first snake game attempt, with focused keyboard capture and safe rerendering.
13. Music/sequencer/piano-roll widgets using WebAudio, persisted patterns, resize/rerender cleanup, and explicit audio permissions.
14. Provider/local-model setup equivalent to Space Agent’s OpenRouter/local-inference panel, mapped to Hermes profiles, LM Studio, and existing provider settings rather than widget-stored raw secrets.
15. Big Bang first-run onboarding space that shows off and teaches the workflow.
16. Time travel/Git rollback for every space/widget/module mutation.
17. Safe admin/recovery mode that still works if generated UI breaks the normal Spaces route.

Do not promise “perfect” one-shot generation. Space Agent itself showed an initial broken snake game. The parity target is stronger: **Capy can create, inspect, patch, persist, visually verify, roll back, and recover every demo class safely.**

## Research Findings

### Space Agent: Core Mechanics

Inspected Space Agent docs and code show these major subsystems:

- `app/L0/_all/mod/_core/spaces/`
  - Main spaces canvas.
  - Persists under authenticated user app files: `~/spaces/<spaceId>/`.
  - `space.yaml` stores manifest, metadata, agent instructions, layout, minimized widgets, timestamps.
  - `widgets/<widgetId>.yaml` stores widget definitions, preferred YAML schema, dimensions, metadata, and a `renderer` function source string.
  - `data/`, `assets/`, `scripts/` store widget-owned data/files/modules.
  - Runtime namespaces: `space.current.*` and `space.spaces.*` expose CRUD, widget read/patch/render/reload/layout/share helpers.
  - Prompt extensions add compact available-space/current-widget/current-space context.
  - It has a staged workflow: list/read/patch/see widgets instead of dumping source into history.

- `app/L0/_all/mod/_core/onscreen_agent/`
  - Floating browser-overlay agent surface.
  - Stores config in `~/conf/onscreen-agent.yaml`, UI state in browser storage, history in `~/hist/onscreen-agent.json`.
  - Builds prompts from firmware prompt + examples + live history + transient runtime sections.
  - Uses context tags (`space:open`, `browser:open`, etc.) to gate skill eligibility and auto-loading.
  - Executes browser-side JavaScript through a `_____javascript` protocol.
  - Provides seams for prompt extension, API request preparation, execution validation, and message processing.

- `app/L0/_all/mod/_core/skillset/`
  - Browser-side SKILL.md catalog and helper JS modules.
  - Skills have frontmatter metadata for `when`, `loaded`, and `placement`.
  - Skills can auto-load into system/transient prompt areas based on live tags.

- `server/`
  - Thin local infrastructure runtime.
  - Request flow: API preflight, `/api/proxy`, `/api/<endpoint>`, `/mod/...`, app-file routes, then page shells.
  - Owns auth/session, customware path resolution, optional Git history, quotas, proxy, temporary ZIP artifacts, hosted share clones, runtime params, cluster state.
  - Space Agent consciously keeps browser/app logic out of the backend unless security/data integrity requires backend ownership.

Key Space Agent parity features:

| Capability | Space Agent implementation | Capy target |
|---|---|---|
| Visual spaces | Routed canvas with movable/resizable widgets | WebUI canvas route/panel with `CapySpace` model |
| Widget persistence | YAML widget files with JS renderer strings | JSON/YAML widget specs with typed renderers; JS only in sandbox |
| Agent editing | Browser-side JS runtime APIs | Hermes tools + WebUI APIs + explicit tool progress |
| Skills | Browser SKILL.md catalog by context tags | Reuse Hermes skills + optional space-local skills |
| Prompt context | Current space instructions + compact widget transients | Inject structured space context into `AIAgent.run_conversation` system/context |
| Web/browser widgets | `<x-browser>` surfaces registered with runtime | Embed Browser/CDP surfaces through backend-managed browser sessions |
| File/data storage | `~/spaces/<id>/data|assets|scripts` | Workspace-local `.capy/spaces/<id>/...` or WebUI state dir |
| Share/export | ZIP export/import and optional hosted sharing | ZIP/export/import first; hosted later |
| Rollback/history | Optional writable-layer Git history | Hermes checkpoint manager + workspace Git + space revision log |
| Desktop host | Electron packaging/native host | Keep WebUI/PWA first; optional native host later |

### Capy/Hermes/WebUI: Existing Extension Points

Current WebUI already has several native hooks that make Capy Spaces feasible without a rewrite:

- Profile-aware state paths in `api/config.py`
  - `STATE_DIR`, `WORKSPACES_FILE`, `SESSION_DIR`, `SETTINGS_FILE`, profile-aware config loading.
- Workspace backend in `api/workspace.py`
  - Profile-aware workspace list and last workspace.
  - Trusted workspace root validation.
  - Blocks system roots and permits safe home/default/saved workspace roots.
  - File operations already tied to workspace trust.
- Routing in `api/routes.py`
  - Existing endpoints: `/api/workspaces`, `/api/workspaces/suggest`, `/api/workspaces/add`, `/api/workspaces/remove`, `/api/workspaces/rename`.
  - File APIs: `/api/file`, `/api/file/save`, `/api/file/raw`, `/api/list`, create/delete/rename directory endpoints.
  - Chat endpoints: `/api/chat/start`, `/api/chat`, `/api/chat/steer`.
  - Approval/clarify endpoints already bridge tool-side prompts back to WebUI.
- Streaming integration in `api/streaming.py`
  - Creates/reuses `AIAgent` with `platform='webui'`, `enabled_toolsets`, callbacks, session DB, and stable session id.
  - Sets `TERMINAL_CWD`, `HERMES_EXEC_ASK`, `HERMES_SESSION_KEY`, and profile-aware `HERMES_HOME` for tool execution.
  - Prepends workspace context to every user message and system message.
  - Provides SSE events for tokens, reasoning, tools, approvals, clarifications, done/error, and metering.
  - Has `SESSION_AGENT_CACHE` reuse, stream cancel, steer, and periodic session checkpointing.
- Frontend in `static/`
  - `messages.js` already starts agent runs and attaches SSE streams.
  - `workspace.js` already has a file tree, previews, sandboxed HTML iframe preview, raw-file URLs, edit/save, git badge.
  - `index.html` is the main insertion point for panels/routes.
- Hermes Agent core:
  - Tool registry is self-registering in `tools/registry.py` and `model_tools.py`.
  - Toolsets are declared in `toolsets.py`; WebUI resolves toolsets from config.
  - Filesystem checkpointing exists in `tools/checkpoint_manager.py` as shadow Git repos under `~/.hermes/checkpoints`, automatically around mutating file operations when enabled.
  - Skills, memory, session search, cron, browser/CDP, terminal, file, patch, image, TTS, delegation, and messaging are already first-class tools.
  - Gateway sessions persist Telegram/WebUI context and transcripts, providing a bridge between chat surfaces.

## Product Definition: Capy Spaces

A **Capy Space** is a durable, visual, agent-editable workspace object containing:

- A manifest: title, icon, description, instructions, created/updated timestamps, owner/profile/workspace binding, version.
- A canvas layout: camera, grid, widget positions/sizes/minimized state.
- Widgets: typed cards/panels with declarative config, renderer type, data bindings, permissions, and revision history.
- Assets/data: files generated or consumed by widgets.
- Skills/context: space-local instructions and optional reusable helpers.
- Browser surfaces: controlled browser panels that can be inspected/interacted with by Hermes browser tools.
- Widget-to-agent events: buttons/forms inside a widget can submit scoped prompts back to the active Capy session, with explicit event metadata and user-visible progress.
- First-run/demo templates: curated Big Bang, research, dashboard, browser-control, Kanban, game, and music templates used both for onboarding and regression tests.
- Tool permissions: a capability envelope limiting which Hermes tools a space/widget may invoke.
- History/checkpoints: revisions and rollback points for all space mutations.
- Recovery metadata: enough information to disable a bad widget/module or open safe mode without rendering untrusted content.
- Export/import package: portable ZIP/tarball with manifest, widgets, assets, and optional redacted session transcript.

Capy Spaces should be addressable from:

- WebUI route/panel: visual canvas and side inspector.
- Telegram: links/previews and commands like “open the Daily Ops space”, “add this to the research dashboard”.
- Hermes Agent tools: `space_list`, `space_create`, `space_upsert_widget`, etc.
- Filesystem: workspace-local `.capy/spaces/<space_id>/` for project-bound spaces.

## Target Architecture

### 1. Storage Layer

Add a new WebUI backend module:

- `api/spaces.py`
- State location, configurable:
  - Default: profile state dir: `{profile_home}/webui_state/spaces/`
  - Project-bound option: `<workspace>/.capy/spaces/`
- Recommended layout:

```text
spaces/
  index.json
  <space_id>/
    space.yaml
    widgets/
      <widget_id>.yaml
    data/
    assets/
    scripts/
    revisions/
      <timestamp>-<event>.json
    thumbnails/
      thumbnail.webp
```

Canonical `space.yaml` schema:

```yaml
schema: capy.space.v1
id: daily-ops
workspace: /Users/bschmidy10/workspace/example
profile: default
title: Daily Ops
icon: dashboard
color: '#6aa6ff'
description: Operational dashboard
agent_instructions: |
  Keep widgets compact. Prefer patches over rewrites.
created_at: '2026-04-27T23:28:00-05:00'
updated_at: '2026-04-27T23:28:00-05:00'
layout:
  columns: 24
  camera: {x: 0, y: 0, zoom: 1}
  widgets:
    news: {col: 0, row: 0, cols: 8, rows: 5, minimized: false}
permissions:
  toolsets: [web, file, browser]
  network: allowlist
  domains: []
```

Canonical widget schema:

```yaml
schema: capy.widget.v1
id: news
name: News Brief
kind: markdown | chart | table | browser | html | react | script | terminal | image | custom
cols: 8
rows: 5
metadata: {}
permissions:
  toolsets: [web]
  network: inherit
source:
  type: declarative
  spec: {}
renderer:
  type: markdown-template
  body: |
    # Today
    {{summary}}
data:
  refresh: manual | interval | agent
  bindings: []
```

Design rule: declarative renderers first; same-origin arbitrary JS never as default.

### 2. Backend API Layer

Add endpoints in `api/routes.py` delegating to `api/spaces.py`:

- `GET /api/spaces?workspace=<path>`
- `POST /api/spaces/create`
- `POST /api/spaces/update`
- `POST /api/spaces/delete`
- `POST /api/spaces/duplicate`
- `GET /api/spaces/get?id=<space_id>&workspace=<path>`
- `POST /api/spaces/widget/upsert`
- `POST /api/spaces/widget/patch`
- `POST /api/spaces/widget/delete`
- `POST /api/spaces/widget/event` — widget-to-agent event bridge, e.g. research form submit or “refresh this card”.
- `POST /api/spaces/layout/save`
- `POST /api/spaces/export`
- `POST /api/spaces/import`
- `GET /api/spaces/asset/raw?...`
- `POST /api/spaces/browser/create|navigate|snapshot|click|type|close` — active browser-surface bridge, backed by Hermes browser/CDP where available.
- `POST /api/spaces/checkpoint`
- `POST /api/spaces/rollback`
- `POST /api/spaces/recovery/disable-widget`
- `POST /api/spaces/recovery/disable-module`
- `POST /api/spaces/templates/install` — install Big Bang/demo templates into a new or existing space.

Implementation rules:

- Reuse `resolve_trusted_workspace()` from `api/workspace.py` for all workspace-bound paths.
- Never accept arbitrary absolute paths inside widget specs without resolving against the space root.
- Validate widget schemas before writing.
- Atomic writes only.
- Maintain `index.json` as cache, but rebuild from manifests if corrupt.
- Store a revision event for every mutation.
- Optionally call Hermes checkpoint manager for project-local `.capy/spaces` writes.

### 3. Hermes Tool Layer

Add a Hermes Agent tool module:

- `tools/spaces_tool.py`

Toolset:

- Add `spaces` to `toolsets.py`.
- Include it in WebUI default toolset only after API stabilizes, or gate behind config `webui.spaces.enabled` initially.

Tools:

- `space_list(workspace?)`
- `space_get(space_id, include_widgets=false)`
- `space_create(title?, workspace?, instructions?)`
- `space_update(space_id, fields)`
- `space_delete(space_id)`
- `space_export(space_id)`
- `space_import(path_or_upload)`
- `space_widget_list(space_id)`
- `space_widget_read(space_id, widget_id)`
- `space_widget_upsert(space_id, widget)`
- `space_widget_patch(space_id, widget_id, edits|fields)`
- `space_widget_delete(space_id, widget_id)`
- `space_widget_event(space_id, widget_id, event_name, payload)`
- `space_layout_save(space_id, layout)`
- `space_browser_create(space_id, url, widget_id?)`
- `space_browser_snapshot(space_id, browser_id)`
- `space_browser_click(space_id, browser_id, ref)`
- `space_browser_type(space_id, browser_id, ref, text)`
- `space_checkpoint(space_id, reason)`
- `space_rollback(space_id, revision)`
- `space_recovery_disable(space_id, target_type, target_id, reason)`
- `space_demo_run(name)` for scripted parity smoke tests, not normal user workflows.

Reasoning: Space Agent exposes browser runtime APIs. Capy should expose model-visible tools backed by server-side validation. The model can still use browser automation for visual inspection, but persistent mutations go through typed tools.

### 4. Prompt and Context Integration

Modify `api/streaming.py` around workspace context construction to include space context when a session has `active_space_id`:

- Add fields to WebUI `Session` model:
  - `active_space_id`
  - `active_space_title`
  - optional `space_context_version`
- On `/api/chat/start`, accept `space_id` and store it on the session.
- Add compact system section:

```text
## Active Capy Space
id: daily-ops
title: Daily Ops
workspace: /absolute/path
instructions:
...
widgets (id|name|kind|cols|rows|status):
news|News Brief|markdown|8|5|ok
...
Use Capy space tools for space/widget mutations. Prefer read+patch for existing widgets.
```

- Do not inject full widget source by default.
- Add a transient-like short section in the user message only when the active space changed.
- Add a skill `capy-spaces` under Hermes skills with workflow rules:
  - list/read before patching
  - use widget IDs
  - patch, don’t rewrite, unless broad change
  - verify renderer/schema after writes
  - use screenshots/browser tools for visual checks

### 5. Frontend UI Layer

Add files:

- `static/spaces.js`
- `static/spaces.css`
- Add panel/route entry in `static/index.html` and any panel router module.

UI components:

- Spaces dashboard/list.
- Canvas route with grid/camera pan/zoom.
- Widget cards with title, reload, resize, move, minimize, delete.
- Space metadata popover: title, icon/color, instructions.
- Widget inspector/editor: source/spec, metadata, permissions, data files.
- Chat-to-space bridge: composer includes active `space_id`; empty canvas examples submit prompts.
- Widget-to-agent bridge: widget buttons/forms can submit scoped prompts/events through `/api/spaces/widget/event`; frontend shows the originating widget, payload summary, active session, and progress events.
- First-run Big Bang space: seeded onboarding space with special instructions and reset action.
- Demo parity gallery: weather, research harness, browser-control, Kanban, stock chart, snake, sequencer, and notes examples as installable templates and regression fixtures.
- Import/export/share modal.
- Revision history/rollback panel.
- Safe recovery route: static/minimal `/spaces/recovery` or equivalent panel that does not render generated widgets and can disable widgets/modules, inspect files, run rollback, and launch a repair prompt.
- Browser widget renderer using sandboxed iframe or backend-managed browser session.

Rendering strategy by widget `kind`:

- `markdown`: sanitized markdown using existing renderer.
- `table/chart`: declarative JSON rendered by trusted built-in JS.
- `image`: raw asset URL.
- `browser`: WebUI-managed browser/CDP surface, not generic iframe.
- `html`: sandboxed iframe with strict flags; no parent access.
- `script/custom`: disabled by default, requires explicit trust and sandbox isolation.

Security defaults:

- Widget iframes use sandbox without `allow-same-origin` unless a specific trusted mode is enabled.
- No widget can call WebUI APIs with ambient cookies from inside sandbox by default; API access goes through a narrow postMessage bridge with per-widget capability tokens.
- Capabilities declared in widget manifest and enforced server-side.

### 6. Runtime Bridge / Widget Capability Model

Create a small browser runtime object for trusted built-in widgets only:

```js
window.capySpaces = {
  current: {
    id, widgetList(), readWidget(id), requestPatch(...)
  },
  widgets: {
    requestData(widgetId, request),
    saveData(widgetId, path, content),
  },
  agent: {
    submitPrompt(text, {spaceId, widgetId})
  }
}
```

For sandboxed widgets, expose a postMessage bridge:

- `capy:ready`
- `capy:data:get`
- `capy:data:put`
- `capy:asset:url`
- `capy:agent:prompt`
- `capy:resize`

Bridge validates:

- origin/frame id
- widget id
- requested action
- permissions from widget manifest
- path containment under widget data/assets root

### 7. Browser Surface Integration

Space Agent’s `<x-browser>` is powerful because the agent can inspect/control browser surfaces. Capy can do better by reusing Hermes browser/CDP tooling:

- Add widget kind `browser` with fields:

```yaml
kind: browser
browser:
  url: https://example.com
  controls: true
  tool_access: inspect | interact | full
```

- Backend creates/associates a browser session/surface id.
- Frontend renders an iframe/stream/screenshot/controlled browser view depending on backend support.
- Hermes tools map `browser_*` calls to the active browser surface when prompt context indicates a browser widget is selected.
- The visual UI exposes numeric/ref IDs returned by browser snapshots, matching current Hermes browser tools.

### 8. Versioning and Rollback

Use two layers:

1. Space-level revision log in `revisions/` for semantic changes.
2. Hermes checkpoint manager or workspace Git for filesystem-level rollback.

Revision event example:

```json
{
  "id": "20260427T232811Z-widget-upsert-news",
  "type": "widget.upsert",
  "actor": "agent:webui-session:<sid>",
  "before_hash": "...",
  "after_hash": "...",
  "summary": "Created News Brief widget"
}
```

Expose rollback UI and tool:

- Preview diff.
- Roll back one widget, full space manifest, or whole space tree.
- Never roll back files outside the space root.

### 9. Sharing and Import/Export

Phase 1:

- Local ZIP export/import only.
- Manifest includes `capy_space_export_version`.
- Import sanitizes IDs and paths.
- Import can create new space or replace current space with confirmation.
- Secrets redaction scan on export: `.env`, API-key-like fields, provider config, auth cookies, session IDs.

Phase 2:

- Signed local share link within WebUI.
- Optional hosted share receiver later.
- For Telegram: send `MEDIA:/path/to/space.zip` plus preview image.

### 10. Desktop/PWA Strategy

Do not start with Electron. Capy already runs as WebUI + Telegram + launchd on Mac Studio. The better path:

1. Make WebUI Spaces excellent.
2. Add PWA installability and visible macOS browser launch support.
3. Add optional native host only if a feature truly requires native windows, global shortcuts, or OS integration.

## Full Feature-Parity Milestones

Sequencing rule from the video review: **do not enable powerful generated/script widgets, local-service dashboards, or agent-created modules for normal use until rollback and safe recovery exist.** Space Agent can rely on admin mode after a broken UI; Capy Spaces needs the same escape hatch before we expose similar power.

2026-05-01 sequencing update: treat recovery/rollback as the next major product gate, not a late polish phase. Import/export, richer widgets, local-service dashboards, and hosted sharing may continue only as metadata-only/safe-mode-compatible slices until rollback and safe admin recovery can restore or disable broken spaces/widgets.

UI-facing acceptance update: every user-visible Capy Spaces slice should include automated tests plus browser/screenshot QA when visually relevant. Reports should include the screenshot artifact, visible pass/fail state, obvious layout issues, and confirmation that no raw renderer/source/script/secret-like values are visible.

Execution update: use one vertical demo as the near-term north star. Prefer the Research Harness until it works end-to-end: widget-origin prompt, scoped Capy event, live planning/source/notes/summary widgets, markdown artifact, PDF/export patch, revision events, rollback, and screenshot QA.

### Phase 0 — Safety and foundations

- Create `api/spaces.py` with schema validation, atomic writes, and path containment.
- Add tests for workspace trust, path traversal, malformed manifests, corrupt indexes.
- Add feature flag `webui.spaces.enabled`.
- Add `static/spaces.js/css` shell hidden behind flag.
- Add a minimal safe recovery route/panel that lists spaces and can disable an entire space without rendering generated widget content.
- Add revision-event IDs from the first write path, even if full diff UI comes later.

Acceptance:

- Can create/list/read/delete spaces through API.
- Cannot write outside trusted workspace or profile state dir.
- Corrupt space files fail soft.
- Safe recovery route opens when Spaces is enabled and does not execute widget code.

### Phase 1 — Basic Spaces UI

- Spaces list dashboard.
- Canvas grid with pan, move, resize, minimize, remove.
- Metadata editor.
- Empty-space onboarding prompts.
- First-run Big Bang template with special `agent_instructions`, reset action, and sample non-secret demo widgets.
- Active `space_id` attached to chat starts.

Acceptance:

- User can create a space in WebUI and persist layout.
- Chat sessions know active space in prompt context.
- A fresh WebUI profile can install/open the Big Bang space and then reset it.

### Phase 2 — Typed Widgets

- Implement `markdown`, `table`, `chart`, `image`, `html` renderers.
- Implement widget CRUD APIs and tools.
- Implement widget inspector and source/spec editor.
- Add renderer validation and sandboxing.
- Add built-in declarative templates for the video’s first wave: weather, prices/chart/news dashboard, Kanban, stock chart, and markdown report.

Acceptance:

- Capy can create a dashboard with multiple widgets via tools.
- Widgets survive reload.
- Existing widget edits use read+patch and do not dump full source into prompt by default.
- From a blank space, “what is the weather in Prague?” can remain a chat answer, then “show it to me in a widget” creates a persistent widget.
- Stock/news/chart widgets show useful blocked/rate-limited/error states rather than silent failure.

### Phase 3 — Agent-Native Space Tools

- Add `tools/spaces_tool.py` and `spaces` toolset.
- Add `capy-spaces` Hermes skill.
- Inject active-space prompt context in `api/streaming.py`.
- Stream space mutation events to frontend as live cards.

Acceptance:

- From WebUI or Telegram, user can ask “create a daily research dashboard” and Capy creates a space/widgets using tools.
- Tool calls are visible, reversible, and scoped.
- Active-space context injected into prompts is metadata-only: id, title, description, instructions, safe widget summary rows, event anchors, and revision IDs. Raw renderer/html/script/data bodies are never injected by default.

### Phase 3.5 — Sandbox and Widget Event Contract

- Define the allowed widget kinds and their execution boundary: declarative renderers first, sandboxed HTML second, trusted/same-origin JS only behind explicit per-space approval.
- Define the widget-to-Capy `postMessage`/event schema: event type, widget id, space id, bounded payload summary, created timestamp, correlation id, and approval requirements.
- Define URL/network policy for widgets: blocked/private URLs by default, explicit allowlists for local services and cameras, and user approval for risky destinations.
- Define redaction rules for prompts, payloads, headers, tokens, cookies, API keys, connection strings, source strings, renderer bodies, and DOM/script-like values.
- Add contract tests before adding richer generated widgets.

Acceptance:

- Unsafe or oversized widget event payloads are rejected or summarized without leaking raw content.
- Widget event UI shows actionable metadata anchors while redacting prompt/payload bodies.
- Richer widget rendering cannot bypass the safe recovery route.

### Phase 4 — Data/Assets/Scripts

- Add per-space `data/`, `assets/`, optional `scripts/` APIs.
- Add postMessage bridge for sandboxed widgets.
- Add `/api/spaces/widget/event` and frontend event UI so widget controls can submit scoped prompts back to the active Capy session.
- Add widget refresh controls and interval scheduler.
- Add data binding helpers for web fetch/file read/tool result input.

Acceptance:

- Widgets can store local data/assets safely.
- Widget refreshes do not require rewriting the widget definition.
- Research harness demo works: input widget submits a research prompt, Capy updates planning/source/notes/summary, stores markdown output, and can patch in PDF export.
- Notes-app demo can persist at least folders, note files, markdown/rich-text mode state, images/assets, and rename operations.

### Phase 5 — Browser Widgets

- Add browser widget kind and surface registry.
- Integrate Hermes browser snapshot/click/type/navigate APIs with selected browser widget.
- Add visual browser controls.
- Add visible-user co-control: if the user clicks/types/login-solves a captcha in the browser widget, the next agent snapshot reflects that state.
- Add local-service dashboard pattern for Agent Zero/Hermes/WebUI-style services: explicit allowlist, API connector widget, and browser panel widget.

Acceptance:

- User can ask Capy to open a site inside a space and interact with it.
- Agent can inspect/control the browser widget through existing browser tools.
- Demo-equivalent “check another local agent/app settings for updates” works against a controlled local test service.

### Phase 6 — Revision History and Rollback

- Add revision log APIs and UI.
- Integrate Hermes checkpoint manager for filesystem-level safety.
- Add diff/preview and rollback.
- Expand safe recovery route into an admin/recovery mode: static UI, file browser for `.capy/spaces`, disable-widget/module actions, rollback action, and “ask Capy to repair this space” prompt that runs without rendering the broken widgets.

Acceptance:

- Every agent mutation has a revision event.
- User can roll back a widget or full space.
- A deliberately broken widget can be disabled from recovery mode and the normal Spaces route loads afterward.

### Phase 7 — Import/Export/Share

- Add ZIP export/import.
- Add thumbnail generation.
- Add Telegram send/share integration.
- Add optional hosted share only after threat model review.

Acceptance:

- Spaces are portable without leaking credentials.
- Imported spaces cannot escape their destination root.

### Phase 8 — Advanced Parity

- Space-local skills/instructions.
- Multi-space search.
- Templates/presets gallery.
- Widgets backed by cron jobs or scheduled refresh.
- Collaboration/multi-user locking if WebUI auth/team mode is enabled.
- Optional native/PWA polish.

### Phase 9 — Video Demo Parity Hardening

Build and keep a scripted/manual demo suite that reproduces the video examples end-to-end. This phase is not “new product scope”; it is the proof that the previous phases achieved the vision.

Required fixtures/demos:

1. `demo_weather_widget`
2. `demo_daily_dashboard`
3. `demo_notes_app`
4. `demo_camera_dashboard`
5. `demo_local_agent_control_dashboard`
6. `demo_browser_cocontrol_google_or_test_site`
7. `demo_research_harness_pdf_export`
8. `demo_kanban_board`
9. `demo_stock_chart`
10. `demo_snake_iterative_repair`
11. `demo_step_sequencer_piano_roll`
12. `demo_big_bang_onboarding`
13. `demo_time_travel_restore`
14. `demo_safe_admin_recovery`

Acceptance:

- Every demo can be launched from the templates/gallery or scripted smoke command.
- Every demo survives browser reload and WebUI restart.
- Every demo has at least one rollback point.
- Generated/advanced widgets run in sandboxed or explicitly trusted mode with visible permissions.
- Video parity is not marked complete until this suite passes on Brendan’s Mac Studio.

## File-Level Implementation Plan

### Hermes WebUI

Create:

- `api/spaces.py`
- `static/spaces.js`
- `static/spaces.css`
- `tests/test_spaces_api.py`
- `tests/test_spaces_security.py`
- `tests/test_spaces_rendering.py` if frontend tests exist or can be added.
- `tests/test_spaces_recovery.py`
- `tests/test_spaces_demo_parity.py` for backend/template smoke coverage of video fixtures.
- `tests/fixtures/spaces_demo_parity/` with sanitized template fixtures for weather, research, Kanban, stock, notes, browser-control, snake, sequencer, Big Bang, rollback, and recovery.

Modify:

- `api/routes.py`
  - Add spaces endpoints.
  - Add `space_id` propagation in `/api/chat/start`.
- `api/models.py`
  - Add active space fields to `Session` serialization/compact/load/save.
- `api/streaming.py`
  - Add active space context to system prompt.
  - Emit space events if the active session mutates spaces.
- `api/workspace.py`
  - Reuse existing trust helpers; avoid duplicating path logic.
- `static/index.html`
  - Add Spaces panel, canvas containers, modals.
- Add static safe recovery route/panel entry that avoids generated widget render paths.
- `static/messages.js`
  - Include `space_id` in chat start body.
  - Render space mutation SSE/tool cards if needed.
- `static/workspace.js`
  - Link file tree and spaces assets/data views.
- Service worker cache list if new static files need caching.

### Hermes Agent

Create:

- `tools/spaces_tool.py`
- `skills/software-development/capy-spaces` or user skill `capy-spaces` if not in repo.

Modify:

- `toolsets.py`
  - Add `spaces` toolset.
- Potentially `model_tools.py` only if registry discovery is insufficient; current registry should discover a new self-registering tool file automatically.
- Docs/tests for tool schemas.

### Optional Later

- Gateway link previews for Telegram space cards.
- PWA manifest enhancements.
- Hosted sharing service.

## Security Model

Threats:

1. Generated widget code exfiltrates cookies/session/API keys.
2. Widget archive import writes outside destination.
3. Browser widget becomes arbitrary same-origin control surface.
4. Agent overwrites project files unintentionally.
5. Shared/exported spaces leak secrets.
6. Long-running widget scripts degrade WebUI performance.

Controls:

- Declarative widgets by default.
- Sandbox iframe with no same-origin access for HTML/script widgets.
- postMessage bridge with per-widget capability tokens.
- Server-side path containment and schema validation.
- No ambient WebUI API access from widget frames.
- Toolset-scoped permissions at space and widget level.
- Approval prompts for dangerous tools remain in WebUI/Telegram.
- Export redaction and denylist for secret files.
- Timeouts/resource budgets for widget refresh and script execution.
- Revision log plus checkpoint rollback.

Do not port directly from Space Agent:

- Same-origin model-generated `renderer` JS as default.
- Browser-side unrestricted execution protocol as persistence mechanism.
- Blind CORS/proxy expansion without allowlist/rate limits.
- Hosted share before local ZIP import/export is robust.

## Testing Strategy

Backend unit tests:

- Create/list/read/update/delete spaces.
- Widget upsert/patch/delete.
- Layout save.
- Import/export roundtrip.
- Path traversal rejection: `../`, symlinks, absolute asset paths.
- Corrupt YAML/JSON handling.
- Revision log creation.
- Workspace trust boundaries.

Hermes tool tests:

- Tool schemas validate.
- Tools call WebUI spaces API or shared storage correctly.
- Tool outputs stay compact.
- Patch rejects ambiguous or full-rewrite edits unless explicit.

Frontend tests/manual QA:

- Canvas interactions: pan/move/resize/minimize/delete.
- Space metadata saves.
- Widget renderer sandbox cannot access parent/cookies/localStorage.
- Chat from active space includes `space_id`.
- Visual browser widget lifecycle.

End-to-end scenarios:

1. “Create a daily news dashboard.”
2. “Add a web browser widget for GitHub and summarize this repo.”
3. “Turn this markdown file into a report widget.”
4. “Patch the chart widget to use a 7-day range.”
5. “Export this space and send it to me on Telegram.”
6. “Roll back the last widget change.”

Video-demo parity suite:

1. `demo_weather_widget`: blank space → chat answer → persistent widget → reload verification.
2. `demo_notes_app`: folders, rename, rich-text/markdown modes, image/attachment persistence.
3. `demo_camera_dashboard`: approved public stream URLs render; private/local URLs require explicit approval/allowlist.
4. `demo_local_agent_control_dashboard`: API connector + embedded browser panel against a controlled local test service.
5. `demo_browser_cocontrol`: user and Capy both interact with one browser panel; element references update after user interaction.
6. `demo_research_harness_pdf_export`: widget-origin prompt triggers research progress, markdown artifact, and PDF/print export patch.
7. `demo_kanban_board`: persistent cards/columns, rename/edit/drag behaviors.
8. `demo_stock_chart`: Nvidia/Apple/Alphabet chart or mocked market-data adapter with blocked-source error handling.
9. `demo_snake_iterative_repair`: first broken/focused-keyboard regression plus patch/reload verification.
10. `demo_step_sequencer_piano_roll`: WebAudio permission, pattern persistence, resize cleanup.
11. `demo_big_bang_onboarding`: first-run install, reset, and space-specific instructions.
12. `demo_time_travel_restore`: roll back last widget edit and restore present state if supported.
13. `demo_safe_admin_recovery`: broken widget cannot prevent recovery route from loading and disabling it.

### Demo Parity Matrix

Keep this matrix current as fixtures, routes, UI tests, screenshot harnesses, and security assertions land. Status values should be one of: `not started`, `metadata smoke`, `partial`, `blocked`, or `complete`.

- `demo_weather_widget`
  - Required route/API coverage: typed demo smoke route plus widget create/read/update APIs.
  - Required UI test: blank-space prompt-to-widget flow or equivalent fake-DOM behavior test.
  - Required screenshot: weather widget visible after reload.
  - Required security assertions: no raw renderer/source/script leakage; blocked/rate-limited weather data shows useful fallback.
  - Current status: metadata smoke.
- `demo_research_harness_pdf_export`
  - Required route/API coverage: widget event queue, active-space context, artifact write, widget patch, export/PDF action, revision event creation.
  - Required UI test: event submission renders progress/source/notes/summary updates and bounded event metadata.
  - Required screenshot: research harness shows prompt, progress, sources, notes, summary, artifact, and export action.
  - Required security assertions: prompt/payload/source bodies redacted where appropriate; generated export patch is reversible; rollback restores pre-run state.
  - Current status: partial; this is the preferred next vertical demo.
- `demo_time_travel_restore`
  - Required route/API coverage: revision list, diff/preview, rollback widget, rollback full space, recovery-mode rollback.
  - Required UI test: rollback controls visible and do not render generated widget bodies.
  - Required screenshot: before/after rollback state plus recovery route if normal UI is broken.
  - Required security assertions: rollback cannot escape space root and records a new revision event.
  - Current status: metadata smoke.
- `demo_safe_admin_recovery`
  - Required route/API coverage: recovery snapshot, disable/enable space, disable/enable widget/module, repair prompt entry, rollback from safe mode.
  - Required UI test: deliberately broken widget cannot prevent recovery operations.
  - Required screenshot: recovery route lists safe metadata and actions without executing widget code.
  - Required security assertions: no renderer/html/script/data bodies; repair prompt is scoped to metadata and requires approval for risky actions.
  - Current status: partial.
- `demo_notes_app`, `demo_kanban_board`, `demo_stock_chart`, `demo_browser_cocontrol`, `demo_camera_dashboard`, `demo_local_agent_control_dashboard`, `demo_snake_iterative_repair`, `demo_step_sequencer_piano_roll`, `demo_big_bang_onboarding`
  - Required route/API coverage: to be filled when each demo enters active implementation.
  - Required UI test: one fake-DOM behavior test plus one smoke route or golden fixture test.
  - Required screenshot: visual pass state on Mac Studio.
  - Required security assertions: no secret/raw source leakage; explicit approval for network/audio/camera/browser-control risk.
  - Current status: not started or metadata-only fixture coverage, depending on existing tests.

Completion rule: Capy Spaces is not “Space Agent demo parity complete” until the above suite passes locally on the Mac Studio, the matrix marks every row `complete`, screenshot/browser QA artifacts exist for UI-facing demos, and at least the critical security tests pass in CI/local pytest.

## Update Compatibility Contract

Capy Spaces must remain durable across future Hermes WebUI and Hermes Agent updates. Treat this as a compatibility contract for every implementation PR and every upstream rebase/update.

1. **Optional by default during rollout**
   - Gate Capy Spaces behind a feature flag such as `HERMES_WEBUI_SPACES_ENABLED=1` until the foundation is stable.
   - WebUI must still boot and normal chat/workspace flows must still work if Spaces is disabled or if Spaces initialization fails.

2. **Isolated subsystem, minimal core edits**
   - Prefer new files (`api/spaces.py`, `static/spaces.js`, `static/spaces.css`, `tests/test_spaces_*.py`) over broad edits to existing chat, streaming, workspace, or session modules.
   - Existing WebUI files should receive only narrow route-registration, session-field, and navigation-hook changes.
   - Avoid monkeypatching global WebUI behavior.

3. **Backward-compatible session/data model**
   - `Session.active_space_id` must be optional and safe for old sessions that do not contain it.
   - Space files must include `schema_version`; loaders must tolerate missing/older fields and report migration needs without crashing.
   - Mutations must create revision-event IDs from the first implementation slice so future migrations and rollback tools have stable history anchors.

4. **Hermes Agent as stable backend dependency**
   - Avoid Hermes Agent core changes until a stable WebUI Spaces foundation exists.
   - When Hermes Agent integration becomes necessary, use public/stable primitives first: tools, toolsets, workspace cwd, checkpoint manager, browser tools, file tools, skills, memory, and gateway/session context.
   - Any required Hermes Agent change must be small, tested, profile-safe, and upstream-friendly.

5. **Compatibility tests are mandatory**
   - Every Spaces PR must add/update pytest coverage for the new behavior.
   - Every WebUI/Hermes update or rebase must run the full WebUI suite plus Spaces-focused tests with Brendan's Hermes agent virtualenv Python on the Mac Studio:
     ```bash
     /Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest tests -q
     /Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_spaces_foundation.py tests/test_spaces_demo_parity.py tests/test_spaces_ui_js_behaviour.py -q
     ```
   - Latest known Capy Spaces validation after the queued event anchor slice: full WebUI suite `2948 passed`, `1 warning`, `8 subtests passed`; broader Spaces/UI/demo tests `163 passed`. Older baseline before Spaces implementation was `2785 passed`, `8 subtests passed`, `1 warning` locally on Brendan's Mac Studio.

6. **Safe failure and recovery**
   - The safe recovery route/panel must not render generated widgets.
   - If a future update breaks widget rendering, the user must still be able to list spaces, disable widgets, export data, and roll back/recover through safe mode.

7. **Upstream rebase workflow**
   - Keep Capy Spaces in Brendan's fork/feature branches until upstreamed or stabilized.
   - For updates: fetch upstream, rebase/merge, run compatibility tests, then restart WebUI only after tests pass.
   - If compatibility breaks, fix the adapter/isolated Spaces layer rather than changing unrelated WebUI or Hermes Agent behavior.

## Migration Strategy

No direct migration from Space Agent data is required initially. If later useful:

- Build `space-agent-import` adapter that reads Space Agent ZIPs:
  - Map `space.yaml` fields to `capy.space.v1`.
  - Convert widget YAML into Capy widget schemas.
  - Treat JS renderer strings as untrusted `html/script` widgets requiring explicit enablement.
  - Preserve `assets/`, `data/`, and `scripts/` under import root.
  - Add import warnings for unsupported APIs such as `space.current.*` calls.

## Recommended Next Sprint

The original Phase 0 + thin Phase 1 skeleton has landed enough that the next sprint should focus on the revised gates:

1. Update this plan's current-status section at the start/end of each slice.
2. Expand safe recovery/admin UI so it can inspect metadata, disable/enable spaces/widgets/modules, launch a scoped repair prompt, and later roll back without rendering generated content.
3. Add rollback/time-travel MVP: revision list, diff/preview, widget rollback, full-space rollback, and recovery-mode rollback.
4. Drive the Research Harness vertical demo end-to-end using strict TDD.
5. Define the sandbox/postMessage contract before adding richer generated or trusted widget rendering.
6. Maintain screenshot/browser QA artifacts for UI-facing slices.

This gives a safe spine that future widget/tool/browser/share work can attach to without reworking the data model or expanding trust before recovery exists.

## Open Design Questions

- Should default storage be profile state or workspace-local `.capy/spaces/`? Recommendation: support both; default to workspace-local for project spaces, profile state for global/personal spaces.
- Should Space tools call WebUI API over HTTP or import shared Python storage functions directly? Recommendation: shared Python functions for in-process WebUI; HTTP adapter later for remote/gateway contexts.
- How should Telegram open/render spaces? Recommendation: send link to WebUI route plus static thumbnail; later add Telegram-native summaries.
- Which JS widget mode is acceptable? Recommendation: declarative first; sandboxed HTML second; trusted JS only behind explicit per-space setting.

## Final Recommendation

Build **Capy Spaces** as a Capy-native visual workspace system, not a repository clone. Use Space Agent as a UX and feature blueprint, but map it onto Hermes’ existing strengths: typed tools, approval flow, skills, memory, session search, gateway persistence, checkpoints, browser/CDP tools, and WebUI streaming. This path reaches full functional parity while avoiding Space Agent’s highest-risk assumption: letting browser-side generated JavaScript be the primary operating substrate.
