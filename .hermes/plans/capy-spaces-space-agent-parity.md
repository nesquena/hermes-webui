# Capy Spaces: Space Agent Feature-Parity Architecture Plan

Created: 2026-04-27 23:28 CDT
Research targets:
- Space Agent checkout: `/tmp/space-agent` at `1289793`
- Local Space Agent reference clone: `/Users/bschmidy10/workspace/space-agent-reference` at `1289793bab727a46e62365992a65ffb3476c4091` (`v0.64`)
- Hermes WebUI checkout: `/Users/bschmidy10/hermes-webui` at `5720fa5`
- Hermes Agent checkout: `/Users/bschmidy10/.hermes/hermes-agent` at `e403379b`

## Current Implementation Status

Last updated: 2026-07-09 CDT on branch `feat/capy-spaces-foundation`.

Integration/readiness checkpoint: `.hermes/plans/capy-spaces-integration-readiness-2026-06-28.md` captures the current stabilization result and review plan. The original checkpoint passed `3400 passed, 3 warnings in 50.98s`; the first 2026-06-30 refresh passed `3417 passed, 3 warnings in 100.02s`; the 2026-06-30 19:53 CDT targeted-validation refresh at 315 commits ahead again passed `3417 passed, 3 warnings in 51.58s`; the 2026-07-01 00:23 CDT targeted-validation refresh at 319 commits ahead passed `3417 passed, 3 warnings in 52.37s`; the 2026-07-01 01:33 CDT targeted-validation refresh at 320 commits ahead passed `3417 passed, 3 warnings in 51.43s`; the 2026-07-01 02:42 CDT targeted-validation refresh at 321 commits ahead passed `3417 passed, 3 warnings in 51.44s`; the 2026-07-01 03:52 CDT targeted-validation refresh at 322 commits ahead passed `3417 passed, 3 warnings in 55.22s`; the 2026-07-01 05:02 CDT targeted-validation refresh at 323 commits ahead passed `3417 passed, 3 warnings in 67.15s`; and the 2026-07-01 06:10 CDT targeted-validation refresh at 324 commits ahead passed `3417 passed, 3 warnings in 52.00s`, plus `node --check static/spaces.js`, Python compile checks, `git diff --check`, clean `feat/capy-spaces-foundation` status, and unchanged source-refresh evidence-index counts (134 GitHub drift regressions, 111 before-body-read regressions, 26 relevant-memory-empty regressions, 154 positive metadata-only GitHub ingestion regressions). A 2026-06-30 visual/UI checkpoint for UI-visible Spaces surfaces also passed with a `/tmp` harness loading checked-out `static/index.html` plus real `static/spaces.js`/CSS: final browser console warnings/errors were zero, Memory freshness/source queue/connector catalog/autonomy policy/progress events/safe recovery rendered, and no hostile sentinels leaked. A backup branch exists at `backup/capy-spaces-pre-integration-20260628-164146`, and `review/capy-spaces-memory-tree-stack-20260628` captured the first non-destructive review stack. After the large review stack has landed, keep the now-modest local sprint delta checkpoint-aware: prefer the lightweight checkpoint loop before another long feature run, and return to integration/doc consolidation if the branch again grows toward a large review-pressure delta. The recommended review stack shape remains: (1) Memory Tree source-refresh safety core, (2) Spaces integration and visible safety receipts, (3) policy/recovery integration, and (4) plans/runbook/parity docs.

Review/navigation update: `.hermes/plans/capy-spaces-source-refresh-evidence-index.md` now provides a compact route-family evidence index and canonical source-refresh invariant so reviewers do not have to start from the chronological per-route log below.

Disabled recovery snapshot trust-envelope slice: `recovery_snapshot()` now returns the same metadata-only prompt-preflight, autonomy-policy/model-route, progress, Memory Tree advisory/no-authority, and output-compaction receipts even when Spaces are disabled by feature flag. The focused backend regression proves the disabled fallback remains safe/admin-only, does not persist progress-log rows, and omits renderer/API-auth/raw-prompt fields while preserving the visible recovery boundary.

Recovery snapshot whole-Space repair prompt/policy slice: `recovery_snapshot()` whole-Space `latest_space_repair_event` summaries now have explicit regression evidence that the stored metadata-only prompt-preflight and supervised autonomy-policy/model-route receipts replay beside the existing Memory Tree advisory/no-authority envelope. The focused backend coverage keeps recovery-panel whole-Space repair status aligned with `list_space_repair_events(...)` while hostile forged memory authority, raw memory context, raw prompts, renderer/source/html/API-auth fields, bearer/token markers, and secret-looking fixture values stay absent from the snapshot surface.

Recovery snapshot widget latest-event prompt/policy slice: `recovery_snapshot()` now replays the stored metadata-only prompt-preflight and supervised autonomy-policy/model-route receipts on widget latest queued-event summaries, matching the listed widget-event receipt while preserving the Memory Tree advisory/no-authority envelope. The focused regression covers widget refresh hostile forged-memory, renderer/script/API-auth/token, and secret-looking fixtures and proves the snapshot latest-event surface stays metadata-only.

Recovery snapshot module repair prompt/policy slice: `recovery_snapshot()` now replays the stored metadata-only prompt-preflight and supervised autonomy-policy/model-route receipts on recovery-module latest repair summaries, in addition to the stored Memory Tree advisory/no-authority envelope. The focused regression covers hostile raw prompts, session ids, source/html/API-auth fields, bearer/token markers, and secret-looking module repair fixtures while proving the snapshot latest-event surface stays metadata-only and aligned with the queued/listed repair receipt.

Recovery snapshot latest repair advisory slice: `recovery_snapshot()` now replays the stored server-generated Memory Tree advisory/no-authority envelope on latest whole-Space repair and recovery-module repair summaries, matching the widget latest queued-event pattern. The focused regressions cover hostile caller-forged trusted-memory authority, raw memory context, renderer/source/html/API-auth, bearer/token, and secret-looking repair fixtures and prove the snapshot keeps only metadata-only `untrusted_advisory`, gate-bypass false, and required safety gates.

Session recovery audit progress slice: `audit_session_recovery(...)` now returns a metadata-only `tool.completed` progress receipt for the read-only recovery audit/status boundary using safe run id `session.recovery.audit`. The focused regression covers hostile session directory, backup path, message, raw-prompt, renderer/source, API-auth, bearer/token, and secret-looking fixtures and proves those values stay out of progress evidence; repair-safe internals still suppress nested audit progress so the repair lifecycle evidence stays explicit.

Session recovery progress lifecycle slice: `repair_safe_session_recovery(...)` now returns paired metadata-only repair-safe progress events (`tool.started` plus terminal `tool.completed` or `tool.failed`) while keeping the legacy `progress_event` as the terminal event. Its output-compaction evidence records the lifecycle event types alongside required prompt-preflight, supervised recovery action policy/model-route, and Memory Tree advisory/no-authority evidence without leaking raw session paths, API-auth fields, tokens, or secret-looking values.

Browser queued widget-event receipt slice: `static/spaces.js` now explicitly labels queued Ask Capy (`agent.prompt`) and widget refresh results as “Widget event receipt” cards while preserving the existing `/api/spaces/widget/event` POST bodies. The focused UI regressions assert prompt-preflight evidence, supervised action-policy/model-route, metadata-only widget-event progress, Memory Tree advisory/no-authority, output-compaction stats/artifacts, and omission of hostile raw-prompt/trusted-memory/renderer/script/API-auth/token/secret fixture fields from the scoped DOM.

Direct widget create/upsert Memory advisory/no-authority receipt slice: native `upsert_widget(..., include_safety_receipts=True)` and `/api/spaces/widget/upsert` now expose the server-generated Memory Tree advisory envelope and matching output-compaction lines, and `static/spaces.js` renders that card inside the “Widget create/update receipt” while hostile trusted-memory/raw-context/raw-prompt/renderer/script/API-auth/token/secret fixtures stay out of the scoped receipt DOM.

Direct widget create/upsert route-shape evidence alignment: checked-out `tests/test_spaces_ui_js_behaviour.py` now keeps the save-widget browser receipt fixture aligned with the live `/api/spaces/widget/upsert` helper shape. The regression expects the direct upsert `hint:fast` model-route receipt and retained `space:<space_id>` plus `revision:<event_id>` compaction artifacts, and explicitly rejects an invented widget artifact handle (`widget:<space_id>:<widget_id>`) for this route while continuing to omit hostile raw-context/raw-prompt/renderer/script/API-key/secret fields from the scoped receipt DOM.

Direct widget create/upsert receipt evidence alignment: checked-out `static/spaces.js` now has explicit save-widget regression coverage for the typed `/api/spaces/widget/upsert` request with `includeSafetyReceipts`, widget-list refresh, and prepended “Widget create/update receipt”. The receipt assertions cover prompt preflight, supervised `space.widget.upsert` action-policy/model-route evidence, metadata-only `widget.upsert:lab` progress, output-compaction stats/artifacts, and omission of hostile raw-context/raw-prompt/renderer/script/API-key/secret fixture fields from the DOM.

Direct widget edit/save receipt evidence alignment: checked-out `static/spaces.js` now has explicit generic edit-form regression coverage for the typed `/api/spaces/widget/patch` request, widget-list refresh, and prepended “Widget update receipt”. The receipt assertions cover prompt preflight, supervised `space.widget.patch` action-policy/model-route evidence, metadata-only `widget.patch:lab:weather` progress, Memory Tree advisory/no-authority, output-compaction stats/artifacts, and omission of hostile raw-context/raw-prompt/renderer/script/API-key/secret/trusted-memory fixture fields from the DOM.

Direct widget notes-edit receipt evidence alignment: checked-out `static/spaces.js` now has explicit notes-save regression coverage for the typed `/api/spaces/widget/patch` request, widget-detail refresh, and prepended “Widget update receipt”. The receipt assertions cover prompt preflight, supervised `space.widget.patch` action-policy/model-route evidence, metadata-only `widget.patch:lab:weather` progress, Memory Tree advisory/no-authority, output-compaction stats/artifacts, and omission of hostile raw-context/raw-prompt/renderer/script/API-key/secret/trusted-memory fixture fields from the DOM.

New direct widget patch receipt UI slice: checked-out `static/spaces.js` now sends `includeSafetyReceipts` for direct widget patch requests and prepends a “Widget update receipt” after the relevant widget-list/detail refresh. The first tracer regression covers the move-widget layout patch path with prompt preflight, supervised `space.widget.patch` action-policy/model-route evidence, metadata-only `widget.patch:<space_id>:<widget_id>` progress, Memory Tree advisory/no-authority evidence, output-compaction stats, and safe widget/Space artifact handles while hostile raw-context/raw-prompt/renderer/script/API-key/secret/trusted-memory fixture fields stay out of the DOM.

New direct Space duplicate receipt UI/API slice: checked-out backend coverage now proves `/api/spaces/duplicate` is a thin explicit-selector wrapper over `space.spaces.duplicateSpace`, and `static/spaces.js` exposes a direct Duplicate action that renders the safety receipt after confirmation and the Spaces home refresh. The focused regressions cover active-space-instructions prompt preflight, supervised `space.spaces.duplicatespace` action policy/model-route evidence, metadata-only `space.duplicate:<new_space_id>` progress, Memory Tree advisory/no-authority evidence, output-compaction stats, and safe duplicate Space/revision artifact handles while hostile trusted-memory/raw-context/renderer/script/API-auth/credential/token/secret/path fixture fields stay out of the route response and DOM.

New direct Space delete receipt UI slice: checked-out `static/spaces.js` now renders the direct `/api/spaces/delete` safety receipt after confirmed deletion and the Spaces home refresh with wording that matches the full safety envelope. The focused UI regression covers prompt preflight, supervised `space.delete` action policy/model-route evidence, metadata-only `space.delete:<space_id>` progress, Memory Tree advisory/no-authority evidence, output-compaction stats, and the safe delete revision artifact while hostile trusted-memory/raw-context/renderer/script/API-auth/credential/token/secret/unsafe-path fixture fields stay out of the DOM.

New direct Space create receipt UI slice: checked-out `static/spaces.js` now requests `includeSafetyReceipts` for new Space saves and prepends a “Space create receipt” card after the Spaces home refresh. The UI behavior regression covers the real `/api/spaces/create` direct-save receipt shape without create-time instructions: no prompt-preflight card renders, the supervised action policy/model-route hint reports prompt preflight `required`, and metadata-only `space.create:<space_id>` progress, Memory Tree advisory/no-authority evidence, and output-compaction stats render. Hostile trusted-memory/raw-memory/renderer/script/API-auth/credential/token/secret fixture fields stay out of the DOM while safe status labels and the `space:<space_id>` Space create metadata artifact handle remain visible.

New direct Space update receipt UI slice: checked-out `static/spaces.js` now prepends a “Space update receipt” card after Space edit/save completes. The UI behavior regression covers the `/api/spaces/update` safety receipt with active-space-instructions prompt preflight, supervised action policy/model-route hint, metadata-only `space.update:<space_id>` progress, Memory Tree advisory/no-authority evidence, and output-compaction stats. Hostile trusted-memory/raw-memory/renderer/script/API-auth/credential/token/secret fixture fields stay out of the DOM while safe status labels and the revision artifact handle remain visible.

New active-space lifecycle receipt UI slice: checked-out `static/spaces.js` now prepends an “Active space receipt” card after `Use in chat` / `Clear from chat`, rendering the server-returned prompt-preflight, autonomy-policy/model-route hint, metadata-only progress, Memory Tree advisory/no-authority, and output-compaction evidence for `space.activate` and `space.deactivate`. The focused UI regressions cover hostile trusted-memory/raw-context/renderer/script/source/data/API-auth/credential/token/secret fixtures and verify the browser surface keeps only allow-listed metadata (`untrusted_advisory`, gate-bypass false, required gates, safe run ids, and compaction counts).

New creator preview/commit Memory advisory UI rendering slice: checked-out `static/spaces.js` now renders the server-generated `memory_advisory` receipt inside both creator preview and creator commit result cards, between prompt preflight and output-compaction evidence. The focused UI behavior regression covers both cards with hostile trusted-memory/raw-context/renderer/script/source/data/API-auth/credential/token/secret fixtures and verifies the browser surface shows only `untrusted_advisory`, gate-bypass false, and required prompt-preflight/approval/sandbox/visual-QA/rollback gates.

New creator preview/commit Memory advisory/no-authority slice: `space.creator.preview` and `space.creator.commit` now expose the fixed server-generated Memory Tree advisory envelope in public responses and metadata-only output-compaction text. The focused regressions prove both creator-loop boundaries ignore hostile caller-supplied `memory_advisory` authority/gate-bypass/raw-context fields, report only metadata-only `untrusted_advisory`, gate-bypass false, and required prompt-preflight/approval/sandbox/visual-QA/rollback gates, and continue omitting raw prompts, renderer/script/source/data/API-auth, credentials, tokens, and secret-looking fixture values.

New recovery widget repair queue Memory advisory/no-authority slice: `queue_recovery_widget_repair_event(...)` and `/api/spaces/recovery/repair-widget` now expose the fixed server-generated Memory Tree advisory envelope in the public queue response, persisted widget-event summary, recovery snapshot latest-event metadata, and metadata-only output-compaction text. The focused regressions prove public recovery-widget repair surfaces report only metadata-only `untrusted_advisory`, gate-bypass false, and required prompt-preflight/approval/sandbox/visual-QA/rollback gates while omitting hostile trusted-memory, renderer/source/html/API-auth, raw prompt/session, generated-body, credential, token, and secret-looking fixture values.

New structured progress Memory advisory/no-authority slice: `progress_status(space_id=...)` now exposes the fixed server-generated Memory Tree advisory envelope beside local-only structured progress metadata and compaction receipts. The focused regression proves public progress status reports only metadata-only `untrusted_advisory`, gate-bypass false, and required prompt-preflight/approval/sandbox/visual-QA/rollback gates while omitting hostile trusted-memory and secret-looking fixture values.

New recovery toggle Memory advisory evidence slice: whole-Space and widget recovery enable/disable helper responses now have focused backend coverage proving the fixed server-generated Memory Tree advisory/no-authority envelope is present in public responses and threaded into metadata-only output-compaction text. The regressions assert `untrusted_advisory`, gate-bypass false, required prompt-preflight/approval/sandbox/visual-QA/rollback gates, and continued omission of recovery reason text, renderer/source/API-auth, bearer strings, credentials, tokens, and secret-looking fixture values.

New widget-delete Memory advisory/no-authority receipt slice: native `delete_widget(..., include_safety_receipts=True)` responses now include the server-generated Memory Tree advisory envelope and thread it into metadata-only output-compaction evidence. `static/spaces.js` renders the widget-delete Memory advisory card between widget-delete progress and compaction evidence, showing only `untrusted_advisory`, gate-bypass false, and required gate labels while hostile trusted-memory/raw-context/renderer/API-auth, credential, token, and secret-looking fixture values remain omitted.

New shared-data delete Memory advisory rendering slice: `static/spaces.js` now renders the server-generated Memory Tree advisory/no-authority card inside the shared-data delete receipt, between delete progress and compaction evidence. The UI test fixture includes hostile trusted-memory/raw-context/renderer/API-auth fields and verifies the receipt shows only `untrusted_advisory`, gate-bypass false, and required gate labels without leaking raw memory context, renderer/script/API-auth, credentials, tokens, or secret-looking values.

New Big Bang template reset Memory advisory rendering slice: `static/spaces.js` now renders the server-generated Memory Tree advisory/no-authority card inside the Big Bang template reset result, between template-reset progress and compaction evidence. The UI test fixture includes hostile trusted-memory/raw-memory/renderer/script/source-HTML/API-auth fields and verifies the reset card shows only `untrusted_advisory`, gate-bypass false, and required gate labels without leaking raw memory context, renderer/script/source HTML, API-auth, credentials, tokens, or secret-looking values.

New demo-suite browser Memory advisory rendering slice: `static/spaces.js` now renders the `space.demo.run_all` Memory Tree advisory/no-authority card inside the run-all suite result, between demo progress and compaction evidence. The UI test fixture includes hostile raw context/trusted-memory/API-auth fields and verifies the suite card shows only `untrusted_advisory`, gate-bypass false, and required gate labels without leaking renderer/source/html/script/data/API-auth, credentials, tokens, or secret-looking values.

New individual demo smoke Memory Tree advisory/no-authority receipt slice: `space.demo.run` now returns a server-generated `memory_advisory` envelope and threads that trust boundary into the metadata-only individual demo output-compaction receipt. Each demo smoke exposes only advisory status, `untrusted_advisory` authority, gate-bypass false, required gate labels, and demo/widget/revision metadata; `static/spaces.js` renders the Memory advisory card inside individual demo-smoke results while omitting raw prompts, renderer/source/html/script/data/API-auth, credentials, tokens, and secret-looking values.

New demo-suite Memory Tree advisory/no-authority receipt slice: `space.demo.run_all` now returns a server-generated `memory_advisory` envelope and threads that trust boundary into the metadata-only demo-suite output-compaction receipt. The receipt exposes only advisory status, `untrusted_advisory` authority, gate-bypass false, required gate labels, and demo pass/fail metadata while omitting raw prompts, renderer/source/html/script/data/API-auth, credentials, tokens, and secret-looking values.

New no-active-space current-context trust-envelope slice: `space.current.context`, `space.context`, and `space.current.prompt_context` now return required metadata-only `memory_context` prompt-preflight evidence, `space.current.context` autonomy-policy receipts, `context:none` structured progress, server-generated Memory Tree advisory/no-authority receipts, and output-compaction lines for preflight/policy/progress/advisory status even when no Space is active. The context body remains empty and public output omits raw memory/source/widget content, prompts, renderer/source/html/script/data/API-auth, credentials, tokens, and secret-looking values.

New GitHub Actions organization runner-group runners final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET /orgs/{org}/actions/runner-groups/{runner_group_id}/runners` Memory Tree source refreshes now prove query/fragment auth, lookalike-host drift, adjacent runner-group id drift, and route-tail drift fail before body read and leave no vault/search/relevant-memory artifact. Public envelopes omit raw final URLs, hostile runner payloads, prompts, renderer/source/html/script/data/API-auth, credentials, tokens, and secret-looking values.

New GitHub code-scanning single-alert final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET /repos/{owner}/{repo}/code-scanning/alerts/{alert_number}` Memory Tree source refreshes now prove query/fragment auth, repository drift, alert-id drift, and alert-instances route-tail drift fail before body read and leave no vault/search/relevant-memory artifact. Public envelopes omit raw final URLs, hostile alert body sentinels, prompts, renderer/source/html/script/data/API-auth, credentials, tokens, and secret-looking values.

New GitHub code-scanning single-analysis final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET /repos/{owner}/{repo}/code-scanning/analyses/{analysis_id}` Memory Tree source refreshes now prove query/fragment auth, repository drift, analysis-id drift, and route-tail drift fail before body read and leave no vault/search/relevant-memory artifact. Public envelopes omit raw final URLs, hostile analysis body sentinels, prompts, refs, renderer/source/html/script/data/API-auth, credentials, tokens, and secret-looking values.

New GitHub repository single-invitation final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET /repos/{owner}/{repo}/invitations/{invitation_id}` Memory Tree source refreshes now prove query/fragment auth, invitation-id drift, userinfo credentials, and lookalike-host drift fail before body read and leave no vault/search/relevant-memory artifact. Public envelopes omit raw final URLs, invitee/inviter/body sentinels, prompts, renderer/source/html/script/data/API-auth, credentials, tokens, and secret-looking values.

New GitHub README final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/readme` Memory Tree source refreshes now explicitly prove repository final-URL drift with query/fragment auth fails closed before any response-body read. Drift leaves jobs refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, README content/body, download/html/git links, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub commit-statuses final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/commits/{sha}/statuses` Memory Tree source refreshes now explicitly prove cross-repository final-URL drift with query/fragment auth fails closed before any response-body read. Drift leaves jobs refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, hostile status contexts/descriptions/target URLs, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub Contents final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/contents/{path}` Memory Tree source refreshes now explicitly prove repository drift fails closed before any response-body read. Drift leaves jobs refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, content/download/html/git links, raw content bodies, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub single-deployment final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET /repos/{owner}/{repo}/deployments/{deployment_id}` Memory Tree source refreshes now prove query/fragment/userinfo auth, repository/id/path/tail drift, lookalike authority, HTTP scheme drift, and non-string final URLs fail before body read and leave no vault/search/relevant-memory artifact. Public envelopes omit raw final URLs, deployment rows, prompts, renderer/source/html/script/data/API-auth, credentials, tokens, and secret-looking values.

New GitHub deployment-statuses final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET /repos/{owner}/{repo}/deployments/{deployment_id}/statuses` Memory Tree source refreshes now prove drift fails before body read and leaves no vault/search/relevant-memory artifact. Public envelopes omit raw final URLs, hostile rows, prompts, renderer/source/html/script/data/API-auth, credentials, tokens, and secret-looking values.

New GitHub deployments final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/deployments` Memory Tree source refreshes now explicitly prove query/fragment/userinfo auth, repository/path/tail drift, lookalike authority, HTTP scheme drift, and non-string final URLs fail closed before any response-body read. Drift leaves jobs refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, deployment rows, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub issue-events final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/issues/{number}/events` Memory Tree source refreshes now explicitly prove query/fragment auth drift fails closed before any response-body read. Drift leaves jobs refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, hostile event rows, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub Actions organization runner-group selected-repositories final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/orgs/{org}/actions/runner-groups/{runner_group_id}/repositories` Memory Tree source refreshes now explicitly prove query/fragment auth, organization/group-id drift, route-tail drift, userinfo credentials, and lookalike host drift fail closed before any response-body read. Drift leaves jobs refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, hostile repository names, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub Actions organization runner-groups final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/orgs/{org}/actions/runner-groups` Memory Tree source refreshes now explicitly prove response-final-URL organization drift fails closed before any response-body read. Drift leaves jobs refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, hostile runner-group names, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub repository custom-properties final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/properties/values` Memory Tree source refreshes now explicitly prove query/fragment drift fails closed before any response-body read. Drift leaves jobs refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, rejected property values, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub commit-comments final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/commits/{sha}/comments` Memory Tree source refreshes now explicitly prove query/fragment drift fails closed before any response-body read. Drift leaves jobs refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, comment bodies, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub secret-scanning final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/secret-scanning/alerts`, `GET https://api.github.com/repos/{owner}/{repo}/secret-scanning/alerts/{alert_number}`, and `GET https://api.github.com/repos/{owner}/{repo}/secret-scanning/alerts/{alert_number}/locations` Memory Tree source refreshes now explicitly prove clean hidden fetches use `hide_secret=true`, and missing `hide_secret` or query/fragment auth drift fails closed before any response-body read. Drift leaves jobs refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub issue-comment reactions final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions` Memory Tree source refreshes now have strengthened coverage proving query/fragment auth drift fails closed before any response-body read. Drift leaves the job refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, hostile reaction body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub issue-reactions final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/issues/{number}/reactions` Memory Tree source refreshes now have strengthened coverage proving query/fragment/userinfo auth or repository/issue drift fails closed before any response-body read. Drift leaves the job refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, hostile reaction body/user sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub issue-comments final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments` Memory Tree source refreshes now have strengthened coverage proving query/fragment auth drift fails closed before any response-body read. Drift leaves the job refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, hostile comment body/user sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub PR requested-reviewers final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/pulls/{number}/requested_reviewers` Memory Tree source refreshes now have strengthened coverage proving query/fragment/userinfo auth, repository, PR-number, or path drift fails closed before any response-body read. Drift leaves the job refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, reviewer/team sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub Actions organization variables final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/orgs/{org}/actions/variables` Memory Tree source refreshes now have strengthened coverage proving query/fragment auth/raw-prompt drift fails closed before any response-body read. Drift leaves the job refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, variable names/values, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub Actions repository variables final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/variables` Memory Tree source refreshes now have strengthened coverage proving query/fragment auth/raw-prompt drift fails closed before any response-body read. Drift leaves the job refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, variable names/values, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub repository License final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/license` Memory Tree source refreshes now have strengthened coverage proving repository drift with query/fragment auth/raw-prompt material fails closed before any response-body read. Drift leaves the job refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, license content/body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub issue-list final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/issues` Memory Tree source refreshes now have explicit coverage proving query/fragment auth/raw-prompt drift fails closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact and no unsafe public leakage of raw final URLs, hostile issue body/user/label sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub repository Events final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/events` Memory Tree source refreshes now reject query/fragment auth/raw-prompt drift before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact and no unsafe public leakage of raw final URLs, hostile event actor/body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub workflow-runs final-URL no-body-read + relevant-memory-empty evidence slice: exact unscoped `GET https://api.github.com/repos/{owner}/{repo}/actions/runs` and workflow-scoped `GET https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs` Memory Tree source refreshes now reject response final URLs that drift to another repository or workflow id and/or add query/fragment auth material before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, workflow-run body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub Actions organization single-secret final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/orgs/{org}/actions/secrets/{secret_name}` Memory Tree source refreshes now have strengthened coverage proving organization drift with query/fragment auth material fails closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, single-secret body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub repository Actions artifacts final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/artifacts` Memory Tree source refreshes now have strengthened coverage proving query/fragment auth drift fails closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, artifact names, archive/download URLs, workflow/commit bodies, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub Dependabot repository public-key final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/dependabot/secrets/public-key` Memory Tree source refreshes now have focused coverage proving response final URLs with query/fragment auth material, repository drift, or Actions public-key route drift fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, public-key body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub Dependabot single-alert final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/dependabot/alerts/{alert_number}` Memory Tree source refreshes now have focused coverage proving response final URLs with query/fragment auth material, repository drift, or alert-number drift fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, Dependabot alert body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub code-scanning alert-instances final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/code-scanning/alerts/{alert_number}/instances` Memory Tree source refreshes now have focused coverage proving drifted response final URLs fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact and no unsafe public leakage of raw final URLs, hostile body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub dependency graph SBOM final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/dependency-graph/sbom` Memory Tree source refreshes now have strengthened coverage proving repository-drift response final URLs fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, SBOM package/body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub Actions runner labels no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/runners/{runner_id}/labels` Memory Tree source refreshes now have focused coverage proving response final URLs that add query/fragment auth material, drift to another runner id, or add route tails fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, runner-label body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub pull-list final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/pulls` Memory Tree source refreshes now have focused coverage proving response final URLs with query/fragment/userinfo auth material or repository drift fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, pull-list body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub Actions runner-groups final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/runner-groups` Memory Tree source refreshes now have focused coverage proving response final URLs with query/fragment/userinfo auth material, route-tail drift, repository drift, or lookalike hosts fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, runner-group collection body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub Actions cache usage final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/cache/usage` and `GET https://api.github.com/repos/{owner}/{repo}/actions/cache/usage-by-ref` Memory Tree source refreshes now have focused coverage proving response final URLs with query/fragment auth material fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, cache size/ref body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub Pages latest-build final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/pages/builds/latest` Memory Tree source refreshes now have focused coverage proving response final URLs that drift to a concrete build id or carry query/fragment auth material fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, latest-build body sentinels, pusher logins, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub repository stargazers final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/stargazers` Memory Tree source refreshes now have focused coverage proving response final URLs with query/fragment/userinfo/auth drift fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, stargazer body sentinels, stargazer logins, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub Pages deployment final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/pages/deployments/{pages_deployment_id}` Memory Tree source refreshes now have focused coverage proving response final URLs with query/fragment auth material fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, deployment body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub Actions single artifact final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/artifacts/{artifact_id}` Memory Tree source refreshes now have focused coverage proving response final URLs with query/fragment auth material, repository drift, or artifact-id drift fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, artifact body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub repository collaborators final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/collaborators` Memory Tree source refreshes now have focused coverage proving response final URLs with query/fragment auth material or repository drift fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, collaborator body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub repository rulesets final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/rulesets` Memory Tree source refreshes now have focused coverage proving response final URLs with userinfo, query/fragment auth material, repository drift, or route drift fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, ruleset body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub Dependabot organization selected-repositories final-URL no-body-read evidence slice: exact `GET https://api.github.com/orgs/{org}/dependabot/secrets/{secret_name}/repositories` Memory Tree source refreshes now have focused coverage proving response final URLs with userinfo/query/fragment auth material or organization drift fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, selected-repository body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub Dependabot organization public-key final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/orgs/{org}/dependabot/secrets/public-key` Memory Tree source refreshes now have focused coverage proving response final URLs that add query/fragment auth material, drift to another organization, cross into Actions public-key routes, add route tails/encoded tails, or include userinfo fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, public-key material/body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub Actions organization runner labels no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/orgs/{org}/actions/runners/{runner_id}/labels` Memory Tree source refreshes now have explicit focused coverage proving response final URLs that drift to another runner id or carry query/fragment auth material fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, runner label body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub PR commits final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/pulls/{number}/commits` Memory Tree source refreshes now require response final URLs to stay on the same clean canonical GitHub PR-commits route before any response-body read or Spaces relevant-memory output. Query/fragment auth material fails closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, PR commit body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens/access-token markers, or secret-looking values.

New GitHub Actions workflow-access final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/permissions/access` Memory Tree source refreshes now have tightened tests proving response final URLs with cross-repository drift or query/fragment auth material fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search artifact and no unsafe public leakage of raw final URLs, workflow-access body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub Pages final-URL drift + relevant-memory-empty hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/pages` Memory Tree source refreshes now require the response final URL to remain the clean canonical GitHub Pages metadata route before any response-body read or Spaces relevant-memory output. Query/fragment auth material, raw-prompt fragments, and non-exact route drift fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, Pages body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub Actions organization selected-actions final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/orgs/{org}/actions/permissions/selected-actions` Memory Tree source refreshes now prove response final URLs with userinfo/query/fragment auth material or organization drift fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact and no unsafe public leakage of raw final URLs, hostile selected-actions body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub single pull-request final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/pulls/{number}` Memory Tree source refreshes now have strengthened coverage proving response final URLs with userinfo/query/fragment auth material, repository drift, or PR-number drift fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact and no unsafe public leakage of raw final URLs, hostile PR body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub check-suites final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/commits/{sha}/check-suites` Memory Tree source refreshes now have focused coverage proving response final URLs with query/fragment/auth material, userinfo, repository drift, or commit-SHA drift fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact and no unsafe public leakage of raw final URLs, check-suite body/app sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub environment deployment branch policy final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/environments/{environment_name}/deployment-branch-policies` and `GET https://api.github.com/repos/{owner}/{repo}/environments/{environment_name}/deployment-branch-policies/{policy_id}` Memory Tree source refreshes now use focused route-specific response final-URL checks and no-body-read coverage. Query/fragment/auth material, userinfo, repository/environment drift, or policy-id drift fail closed before any response-body read, leaving the job pending/refresh failed with no vault/search/relevant-memory artifact and no unsafe public leakage of raw final URLs, deployment-branch-policy body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub check-run annotations final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/check-runs/{check_run_id}/annotations` Memory Tree source refreshes now have focused coverage proving response final URLs with query/fragment/auth material, userinfo, repository drift, or check-run-id drift fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact and no unsafe public leakage of raw final URLs, annotation body/path sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub repository topics final-URL drift evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/topics` Memory Tree source refreshes now have strengthened coverage proving response final URLs with query/fragment/auth material fail closed before any response-body read and cannot surface as Spaces relevant-memory context. Drift leaves the job pending/refresh failed with no vault/search/relevant-memory artifact and no unsafe public leakage of raw final URLs, topic body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens/access-token markers, or secret-looking values.

New GitHub repository single-invitation final-URL drift evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/invitations/{invitation_id}` Memory Tree source refreshes now have focused coverage proving response final URLs with query/fragment/auth material, userinfo, lookalike hosts, or invitation-id drift fail closed before any response-body read. Drift leaves the job pending/refresh failed with no vault/search artifact and no unsafe public leakage of raw final URLs, invitation body sentinels, invitee/inviter logins, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub environment secrets final-URL drift evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/environments/{environment_name}/secrets` Memory Tree source refreshes now require response final URLs to remain on the same clean canonical `api.github.com` environment-secrets route for the same repository/environment before any response-body read, vault persistence, search indexing, or Spaces relevant-memory output. Query/fragment/auth and userinfo drift fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, private-name rows, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub pull review comments final-URL drift evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/pulls/{number}/comments` Memory Tree source refreshes now have focused no-body-read + relevant-memory-empty coverage proving response final URLs with query/fragment/auth material fail closed before body read, vault persistence, search indexing, or Spaces relevant-memory output. Drift leaves the job pending/refresh failed, with no vault/search/relevant-memory artifact and no unsafe public leakage of raw final URLs, review-comment rows/logins, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub release reactions final-URL drift evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/releases/{release_id}/reactions` Memory Tree source refreshes now have focused no-body-read + relevant-memory-empty coverage proving response final URLs must stay on the same clean canonical `api.github.com` release-reactions route for the same repository/release before body read, vault persistence, search indexing, or Spaces relevant-memory output. Query/fragment/auth, userinfo, and release-id drift fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, reaction rows/logins, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub pull review comment reactions final-URL drift evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions` Memory Tree source refreshes now have focused no-body-read + relevant-memory-empty coverage proving response final URLs must stay on the same clean canonical `api.github.com` pull-review-comment-reactions route for the same repository/comment before body read, vault persistence, search indexing, or Spaces relevant-memory output. Comment-id/query/fragment/auth drift fails closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, reaction rows, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub commit-comment reactions final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/comments/{comment_id}/reactions` Memory Tree source refreshes now require response final URLs to remain the same clean canonical `api.github.com` commit-comment-reactions route before any response-body read or Spaces relevant-memory result. Query/fragment/auth drift fails closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, reaction rows, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub issue-comment reactions final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions` Memory Tree source refreshes now require response final URLs to remain the same clean canonical `api.github.com` issue-comment-reactions route before any response-body read. Query/fragment/auth drift fails closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, reaction rows, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub issue reactions final-URL drift evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/issues/{number}/reactions` Memory Tree source refreshes now have focused coverage proving response final URLs must stay on the same clean canonical `api.github.com` issue-reactions route before any body read. Query/fragment/auth, userinfo, repository, and issue-number drift fail closed with the job pending/refresh failed, no vault/search artifact, no body read, and no unsafe public leakage of raw final URLs, reaction rows, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub Actions repository single-secret final-URL drift evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/secrets/{secret_name}` Memory Tree source refreshes now have focused no-body-read + relevant-memory-empty coverage proving response final URLs must stay on the same clean canonical `api.github.com` repository secret route before any body read. Secret-name drift plus query/fragment/auth markers fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact or Spaces relevant-memory output, no body read, and no unsafe public leakage of raw final URLs, hostile single-secret body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub workflow run jobs final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/jobs` Memory Tree source refreshes now require response final URLs to exactly match the clean canonical HTTPS `api.github.com` workflow-run jobs route for the same repository/run before any response-body read, JSON parsing, vault persistence, or Spaces relevant-memory result. Query/fragment/auth, run-id drift, cross-repository drift, route-tail drift, and secret-looking final URL markers fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage.

New GitHub environment variables final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/environments/{environment_name}/variables` Memory Tree source refreshes now require response final URLs to exactly match the clean canonical HTTPS `api.github.com` environment-variables route for the same repository and environment before any response-body read, JSON parsing, vault persistence, or Spaces relevant-memory result. Query/fragment/userinfo/auth, port/authority drift, route-tail drift, and secret-looking final URL markers fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage.

New GitHub commit-statuses final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/commits/{sha}/statuses` Memory Tree source refreshes now require response final URLs to exactly match the sanitized canonical HTTPS `api.github.com` commit-statuses route for the same repository and commit before any response-body read, JSON parsing, or vault persistence. Query/fragment/auth, cross-repository drift, and secret-looking final URL markers fail closed with the job pending/refresh failed, no vault/search artifact, no body read, and no unsafe public leakage.

New GitHub Actions organization secrets public-key final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/orgs/{org}/actions/secrets/public-key` Memory Tree source refreshes now use a route-specific matcher requiring response final URLs to exactly match the sanitized canonical HTTPS `api.github.com` organization secrets public-key route for the same org before any response-body read, JSON parsing, vault persistence, search indexing, or Spaces relevant-memory output. Query/fragment/auth, cross-org drift, tail/encoded-tail drift, uppercase/port authority drift, and non-string final URLs fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage.

New GitHub Actions organization secrets final-URL drift hardening slice: exact `GET https://api.github.com/orgs/{org}/actions/secrets` Memory Tree source refreshes now use a route-specific matcher requiring response final URLs to exactly match the sanitized canonical HTTPS `api.github.com` organization-secrets route for the same org before any response-body read or Spaces relevant-memory output. Query/fragment/userinfo/auth, explicit port, lookalike host, cross-org drift, route-tail/encoded-tail drift, and non-string final URLs fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, organization-secret body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub latest-release final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/releases/latest` Memory Tree source refreshes now reject HTTP origins before network open and require response final URLs to exactly match the sanitized canonical HTTPS `api.github.com` latest-release route before any response-body read or Spaces relevant-memory output. Query/fragment/userinfo/auth, repository/path/tail, lookalike-authority, and HTTP-to-HTTPS redirect drift fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, latest-release body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub Actions OIDC subject-claim final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/oidc/customization/sub` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical HTTPS `api.github.com` OIDC subject-claim route for the same repository before any response-body read, JSON parsing, vault persistence, search indexing, or Spaces relevant-memory output. Query/fragment/userinfo/auth, repository/path/tail, lookalike-authority, and non-string final URL drift fails closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, OIDC body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub deployments final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/deployments` and `GET https://api.github.com/repos/{owner}/{repo}/deployments/{deployment_id}` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical HTTPS `api.github.com` deployments route for the same repository and deployment id, where applicable, before any response-body read, JSON parsing, or vault persistence. Query/fragment/userinfo/auth, repository/id/path/tail drift, lookalike-authority, HTTP-scheme drift, and non-string final URLs fail closed with the job pending/refresh failed, no vault/search artifact, no body read, and no public leakage of raw final URLs, deployment body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub deployment-statuses final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/deployments/{deployment_id}/statuses` Memory Tree source refreshes now require the response final URL to exactly match the clean canonical HTTPS `api.github.com` deployment-statuses route for the same repository/deployment before any response-body read, JSON parsing, or vault persistence. Query/fragment/userinfo/auth, explicit port, lookalike host, cross-repository/deployment drift, path-tail/route drift, HTTP scheme, and non-string final URLs fail closed with the job pending/refresh failed, no vault/search artifact, no body read, and no public leakage of raw final URLs, status body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub issue-list final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/issues` Memory Tree source refreshes now require a clean canonical HTTPS `api.github.com` final URL for the same repo before any response-body read. Query/fragment/userinfo/port/host/repo/path/scheme/non-string drift fails closed with the job pending/refresh failed, no vault/search artifact, no body read, and no unsafe public leakage.

New GitHub commit-comments final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/commits/{sha}/comments` Memory Tree source refreshes now require exact sanitized canonical final-URL parity before any response-body read, search indexing, or Spaces relevant-memory output. Query/fragment drift fails closed with no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, comment bodies, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub milestones final-URL drift hardening slice: metadata-only exact `GET https://api.github.com/repos/{owner}/{repo}/milestones` Memory Tree source refreshes now require the response final URL to exactly match the sanitized clean HTTPS `api.github.com` milestones route for the same repo before any response-body read. Route/query/fragment/userinfo/port/scheme/non-string drift fails closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, milestone rows, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub release-assets final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/releases/{release_id}/assets` Memory Tree source refreshes now require a clean canonical HTTPS `api.github.com` response final URL with no query/fragment/userinfo/port and the same owner/repo/release id before any response-body read or Spaces relevant-memory output. Same-path auth/query drift, route/release drift, and noncanonical finals fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, hostile body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, access-token markers, or secret-looking values.

New GitHub PR files final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/pulls/{number}/files` Memory Tree source refreshes now require a string HTTPS `api.github.com` canonical response final URL with no userinfo/query/fragment and the same owner/repo/PR number before any response-body read. Drift fails closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, hostile body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, access-token markers, or secret-looking values.

New GitHub workflow run approvals final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/approvals` Memory Tree source refreshes now use a route-specific final-URL matcher requiring a string clean canonical HTTPS `api.github.com` final URL for the same repo/run id, with no userinfo/port/query/fragment, before any response-body read. Drift fails closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, hostile body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub workflow run pending-deployments final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/pending_deployments` Memory Tree source refreshes now use a route-specific final-URL matcher requiring the response final URL to exactly match the sanitized canonical HTTPS `api.github.com` pending-deployments route for the same repository/run before any response-body read. Query/fragment/userinfo/auth, repository/run/path-tail drift, lookalike hosts, and non-string finals fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, pending-deployment body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub workflow run attempt jobs final-URL drift hardening slice: metadata-only exact `GET https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs` Memory Tree source refreshes now require an exact clean canonical final URL before any response-body read. Query/fragment/userinfo auth, repository/run/attempt, or route-tail drift fails closed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, hostile job body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub Actions caches final-URL drift evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/caches` Memory Tree source refreshes now have expanded no-body-read coverage proving response final URLs with query/fragment auth material, userinfo, repository drift, or route-tail drift fail closed before any response-body read, JSON parsing, or vault persistence. Drifted cache payloads leave no vault/search/relevant-memory artifact and do not leak raw final URLs, cache ids/refs/keys/versions, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values in catalog/job/result/search output.

New GitHub workflow timing final-URL drift hardening slice: metadata-only exact `GET https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_id}/timing` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical API route before any response-body read. Query/fragment/userinfo/auth, repository/workflow id, or route-tail drift fails closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage.

New GitHub Actions organization runners final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/orgs/{org}/actions/runners` Memory Tree source refreshes now use a route-specific final-URL matcher and require the response final URL to exactly match the sanitized canonical HTTPS `api.github.com` organization runners route before any response-body read, JSON parsing, vault persistence, search indexing, or Spaces relevant-memory output. Query/fragment/userinfo/auth, organization/path/tail, and lookalike-host drift fails closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, runner body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, registration-token markers, tokens, or secret-looking values.

New GitHub Actions organization runner-downloads final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/orgs/{org}/actions/runners/downloads` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical HTTPS `api.github.com` organization runner-downloads route before any response-body read, JSON parsing, vault persistence, search indexing, or Spaces relevant-memory output. Query/fragment/userinfo/auth, organization/path/tail, and lookalike-host drift fails closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, runner-download body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub contributor-stats final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/stats/contributors` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical HTTPS `api.github.com` contributor-stats route before any response-body read, JSON parsing, vault persistence, search indexing, or Spaces relevant-memory output. Query/fragment/userinfo/auth drift fails closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, contributor body sentinels/logins, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub code-scanning default-setup final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/code-scanning/default-setup` Memory Tree source refreshes now require the response final URL to exactly match the clean canonical HTTPS `api.github.com` default-setup route before any response-body read, JSON parsing, vault persistence, search indexing, or Spaces relevant-memory output. Query/fragment/userinfo/auth, repository/path, or host drift fails closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub code-scanning analyses final-URL drift hardening slice: metadata-only exact `GET https://api.github.com/repos/{owner}/{repo}/code-scanning/analyses` Memory Tree source refreshes now reject query/fragment auth, userinfo, lookalike host, different-repo, tail-route, and non-string response final URLs before any response-body read. Drift fails closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, hostile body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub code-scanning alerts final-URL drift hardening slice: metadata-only exact `GET https://api.github.com/repos/{owner}/{repo}/code-scanning/alerts` now rejects final URLs with query/fragment/userinfo/port/lookalike/cross-repo/tail drift before response-body read; no vault/search/relevant-memory artifact; no raw final URL/prompt/API-auth/token leakage.

New GitHub commit-activity final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/stats/commit_activity` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical HTTPS `api.github.com` commit-activity route before any response-body read, JSON parsing, or vault persistence. Query/fragment/userinfo/auth drift fails closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, commit-activity body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub issue-events final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/issues/{positive_number}/events` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical HTTPS `api.github.com` issue-events route before any response-body read, JSON parsing, or vault persistence. Query/fragment/userinfo/auth, issue/repository/path drift, and other noncanonical finals fail closed with the job pending/refresh failed, no vault/search artifact, no body read, and no public leakage of raw final URLs, issue-event row/body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub branch-protection final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/branches/{branch}/protection` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical HTTPS `api.github.com` branch-protection route before any response-body read, JSON parsing, or vault persistence. Query/fragment auth, userinfo credentials, repository drift, and route-tail drift fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, hostile body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, or secret-looking values.

New GitHub workflow-list final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/workflows` Memory Tree source refreshes now require the response final URL to match the sanitized canonical HTTPS `api.github.com` workflow-list route before any response-body read, JSON parsing, or vault persistence. Query/fragment/userinfo/auth, repository/path drift, and other noncanonical finals fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, workflow row/body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub check-runs final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/commits/{40-hex-sha}/check-runs` Memory Tree source refreshes now require the response final URL to match the sanitized canonical HTTPS `api.github.com` check-runs route before any response-body read, JSON parsing, vault persistence, search indexing, or Spaces relevant-memory output. Query/fragment/userinfo/auth, repository/SHA/path drift, and other noncanonical finals fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, check-run row/body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub Actions runner-downloads final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/runners/downloads` Memory Tree source refreshes now have a centralized final-URL matcher plus focused regressions proving response final URLs must match the clean canonical HTTPS `api.github.com` runner-downloads route before any response-body read, JSON parsing, vault persistence, search indexing, or Spaces relevant-memory output. Query/fragment/userinfo/auth, repository/tail, lookalike-host, and other noncanonical drift fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, runner-download body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub participation-stats final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/stats/participation` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical `api.github.com` participation route before any response-body read, JSON parsing, vault persistence, or Spaces relevant-memory output. Query/fragment/userinfo/auth, repository/path/host/scheme/port drift, and non-string/noncanonical finals fail closed with the job pending, no vault/search/relevant-memory artifact, and no public leakage of raw final URLs, participation arrays/body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub Actions workflow permissions final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/permissions/workflow` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical HTTPS `api.github.com` workflow-permissions route before any response-body read, JSON parsing, vault persistence, search indexing, or Spaces relevant-memory output. Query/fragment/userinfo/auth, repository/path/host/scheme/port drift, and non-string final URLs fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, workflow-permission body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New Capy Memory search trust-envelope hardening slice: `/api/capy-memory/search` now returns the same metadata-only Memory Tree no-authority envelope plus `memory_context` prompt-preflight and `space.memory.read` autonomy-policy receipts as relevant-memory reads. Search queries reject raw, whitespace, encoded, and deeply encoded `api_key`/`access_token`/`raw_prompt`/secret markers without echoing them, fail closed on hostile `limit` values, and escape SQL LIKE wildcards so `%`/`_` cannot broaden advisory-memory retrieval while benign literal searches still work.

New GitHub Actions organization permissions final-URL drift hardening slice: exact `GET https://api.github.com/orgs/{org}/actions/permissions` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical HTTPS `api.github.com` organization permissions route before any response-body read, JSON parsing, vault persistence, search indexing, or Spaces relevant-memory output. Query/fragment/userinfo/auth, org drift, host/scheme/port/tail drift, or other noncanonical finals fail closed with the job pending/refresh failed, no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, org-permission policy body sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values.

New GitHub issue timeline final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/issues/{positive_number}/timeline` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical `api.github.com` issue timeline route before body reads, persistence, search indexing, or Spaces relevant-memory output. Query/fragment/auth drift fails closed with no vault/search/relevant-memory artifact, no body read, and no leakage of raw final URLs, timeline rows, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub PR requested-reviewers final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/pulls/{number}/requested_reviewers` Memory Tree source refreshes now have strengthened coverage proving query/fragment/userinfo auth, repository, PR-number, or path drift fails closed before any response-body read. Drift leaves the job refresh failed with no vault/search/relevant-memory artifact, no body read, and no unsafe public leakage of raw final URLs, reviewer/team sentinels, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values.

New GitHub issue-labels final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/issues/{positive_number}/labels` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical `api.github.com` issue-label route before body reads or persistence. Query/fragment/userinfo/auth, repo/issue/path/host/scheme/port drift, and non-string final URLs fail closed with no vault/search/relevant-memory artifact and no leakage of raw final URLs, hostile label fields, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub issue-comments final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical `api.github.com` issue-comments route before body reads or persistence. Query/fragment/auth drift fails closed with no vault/search artifact and no public leakage of raw final URLs, raw prompts, comment bodies, renderer/source/html/script/API-auth fields, credentials, access-token markers, tokens, or secret-looking values in result/job/catalog/search output.

New GitHub repository autolinks final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/autolinks` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical `api.github.com` route before body reads, vault persistence, search indexing, or Spaces relevant-memory output. Query/fragment/auth drift fails closed with no vault/search/relevant-memory artifact and no public leakage of raw final URLs, raw prompts, `url_template`, renderer/source/html/script/API-auth fields, credentials, access-token markers, tokens, or secret-looking values in result/job/catalog/search/relevant-memory output.

New GitHub repository security-advisories final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/security-advisories` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical `api.github.com` route before body reads, persistence, search indexing, or Spaces relevant-memory output. Query/fragment/userinfo/auth drift fails closed with no vault/search/relevant-memory artifact and no public leakage of advisory rows, raw final URLs, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values in result/job/catalog/search/relevant-memory output.

New GitHub check-suites final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/commits/{sha}/check-suites` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical `api.github.com` route before body reads, persistence, search indexing, or Spaces relevant-memory results. Query/fragment/auth drift fails closed with no vault/search/relevant-memory artifact and no public leakage of raw final URLs, raw prompts, check-suite URLs, renderer/source/html/script/API-auth fields, credentials, access-token markers, tokens, or secret-looking values in result/job/catalog/search/relevant-memory output.

New GitHub Secret Scanning final-URL drift hardening slice: exact alert-list, single-alert, and alert-locations Memory Tree source refreshes now require the response final URL to match the `hide_secret=true` metadata-only GitHub fetch route before body reads or persistence. Query/fragment/auth drift, stripped safety queries, and unsafe legacy raw alert-list origins fail closed without vault/search artifacts or public leakage of raw final URLs, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values, while exact legacy alert-list labels keep safe public origins and hidden clean fetch origins for due requeues.

New GitHub repository invitations list final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/invitations` Memory Tree source refreshes now require the response final URL to be a string that exactly equals the clean canonical list route/current fetch origin on raw `api.github.com` authority with the same repo path before any body read or persistence. Query/fragment/auth drift fails closed with no vault/search/relevant-memory artifact and no public leakage of raw final URLs, invitee sentinels, access tokens, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub private vulnerability reporting final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/private-vulnerability-reporting` Memory Tree source refreshes now require the response final URL to exactly match the sanitized canonical API route before any response-body read or persistence. Query/fragment/userinfo/auth, repository, route, scheme, port, or host drift fails closed with no vault/search/relevant-memory artifact, no Spaces relevant-memory output, and no public leakage of raw final URLs, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values in result/job/catalog/search/relevant-memory output.

New GitHub CODEOWNERS errors final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/codeowners/errors` Memory Tree source refreshes now require the response final URL to remain the same canonical `api.github.com` CODEOWNERS errors route, with the same repository and no query, fragment, or userinfo, before any response-body read or persistence. Drifted final URLs fail closed with no vault artifact, no search hit, no Spaces relevant-memory result, and no public leakage of raw final URLs, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values in result/job/catalog/search/relevant output.

New GitHub repository community-profile final-URL drift no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/community/profile` Memory Tree source refreshes now require the response final URL to remain the sanitized canonical community-profile API route before any response-body read or persistence. Drifted final URLs with query/fragment/userinfo/auth or other-route material fail closed with no vault artifact, no search hit, no Spaces relevant-memory result, and no public leakage of raw final URLs, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, or secret-looking values in result/job/catalog/search/relevant output.

New GitHub repository interaction-limits final-URL drift no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/interaction-limits` Memory Tree source refreshes now require exact clean canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth, repository, or lookalike-authority drift leaves metadata-only jobs pending with no vault/search/relevant-memory artifact and no public leakage of raw final URLs, raw prompts, API-auth fields, credentials, access-token markers, tokens, or secret-looking values in result/job/search/relevant output.

New GitHub Actions repository secrets public-key final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key` Memory Tree source refreshes now require exact clean canonical route parity before reading or persisting response bodies. Query/fragment/auth drift leaves metadata-only jobs pending with no vault/search/relevant-memory artifact and no public leakage of raw final URLs, raw prompts, renderer/source/html/script/data/API-auth fields, access-token markers, key material, credentials, tokens, or secret-looking values in result/job/catalog/search/relevant-memory output.

New GitHub Actions repository permissions final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/permissions` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth drift leaves metadata-only jobs pending with no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, raw prompts, values, API-auth fields, credentials, access-token markers, tokens, or secret-looking values in result/job/catalog/search/relevant-memory output.

New GitHub repository custom-properties final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/properties/values` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth drift fails closed with no vault/search artifact and no public leakage of property rows, raw custom-property values, raw final URLs, raw prompts, API-auth fields, credentials, access-token markers, tokens, or secret-looking values in result/jobs/catalog/search/relevant output.

New GitHub Actions repository variables final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/variables` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth drift fails closed with no vault/search artifact and no public leakage of variable rows, raw final URLs, raw prompts, values, API-auth fields, credentials, access-token markers, tokens, or secret-looking values in result/job/catalog/search output.

New GitHub Actions organization variables final-URL drift hardening slice: exact `GET https://api.github.com/orgs/{org}/actions/variables` Memory Tree source refreshes now require exact clean canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth, org, tail-route, or lookalike-authority drift fails closed with no vault/search artifact and no public leakage of variable values, raw final URLs, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking fixtures in result/job/catalog/search output.

New GitHub repository Actions artifacts final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/artifacts` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth or route drift fails closed with no vault/search artifact and no public leakage of artifact rows, raw final URLs, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, access-token markers, tokens, or secret-looking values in result/jobs/catalog/search output.

New GitHub Actions repository self-hosted runners final-URL before-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/runners` Memory Tree source refreshes now require exact sanitized canonical route parity before reading response bodies, vault persistence, search indexing, or Spaces relevant-memory output. Query/fragment/userinfo/auth drift fails closed with no vault/search/relevant-memory artifact and no public leakage of runner rows, raw final URLs, raw prompts, renderer/source/html/script/API-auth fields, credentials, access-token markers, tokens, or secret-looking values in result/jobs/catalog/search/relevant-memory output.

New GitHub repository assignees final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/assignees` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth drift, other-repository drift, lookalike authority drift, non-HTTPS drift, or tail-path drift fails closed with no vault/search/relevant-memory artifact and no public leakage of assignee logins, hostile body sentinels, raw final URLs, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, access-token markers, or secret-looking values in result/jobs/catalog/search/relevant-memory output.

New GitHub repository teams final-URL no-body-read + relevant-memory-empty hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/teams` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth, different repository/host/scheme, or tail drift fails closed with no vault/search/relevant-memory artifact and no public leakage of team names/slugs, raw final URLs, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, access-token markers, or secret-looking values in result/jobs/catalog/search/relevant-memory output.

New GitHub traffic popular paths final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/traffic/popular/paths` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth drift fails closed with no vault/search/relevant-memory artifact, no body read, and no public leakage of popular path rows, raw final URLs, raw prompts, renderer/source/html/script/API-auth fields, credentials, access-token markers, tokens, or secret-looking values in result/jobs/catalog/search/relevant-memory output.

New GitHub traffic popular referrers final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/traffic/popular/referrers` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth, repo, host, route, non-HTTPS, or tail drift fails closed with no vault/search/relevant-memory artifact, no body read, and no public leakage of referrer rows, raw final URLs, raw prompts, renderer/source/html/script/API-auth fields, credentials, access-token markers, tokens, or secret-looking values in result/jobs/catalog/search/relevant-memory output.

New GitHub repository forks final-URL no-body-read and relevant-memory evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/forks` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth drift, other-repository drift, lookalike authority drift, non-HTTPS drift, or tail-path drift fails closed with no vault/search/relevant-memory artifact and no public leakage of raw final URLs, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values in result/jobs/catalog/search/relevant-memory output.

New GitHub traffic views/clones final-URL no-body-read + relevant-memory evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/traffic/{views|clones}` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth drift, other-repository drift, lookalike authority drift, or tail-path drift fails closed with no vault/search/relevant-memory artifact and no public leakage of raw final URLs, traffic rows, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, access-token markers, or secret-looking fixtures in result/jobs/catalog/search/relevant-memory output.

New GitHub punch-card stats final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/stats/punch_card` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth drift, other-repository drift, lookalike authority drift, or tail-path drift fails closed with no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, hostile body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values in result/jobs/catalog/search/relevant-memory output.

New GitHub code-frequency stats final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/stats/code_frequency` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth drift, other-repository drift, or tail-path drift fails closed with no vault/search/relevant-memory artifact, no body read, and no public leakage of raw final URLs, hostile line-change body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values in result/jobs/catalog/search/relevant-memory output.

New GitHub repository stargazers final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/stargazers` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth drift fails closed with no vault/search/relevant-memory artifact, no body read, and no public leakage of stargazer logins, raw final URLs, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values in result/jobs/catalog/search/relevant-memory output.

New GitHub repository subscribers/watchers final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/subscribers` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth, repository, lookalike-authority, non-HTTPS, or tail drift fails closed before any response-body read with no vault/search/relevant-memory artifact and no public leakage of subscriber logins, hostile body sentinels, raw final URLs, prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values.

New GitHub repository Events final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/events` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Query/fragment/userinfo/auth drift fails closed with no vault/search/relevant-memory artifact, no body read, and no public leakage of event actors, hostile payload/body sentinels, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values in result/jobs/catalog/search/relevant-memory output.

New GitHub repository webhooks final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/hooks` Memory Tree source refreshes now require exact sanitized canonical route parity before reading or persisting response bodies. Drifted final URLs with query/fragment/userinfo/auth, host/repo/tail changes, or non-string responses fail closed with no vault/search/relevant-memory artifact, no body read, and no public leakage of webhook event markers, callback/config URLs, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking values in result/jobs/catalog/search/relevant-memory output.

New GitHub Actions runner-group repositories final-URL drift hardening slice: exact `GET https://api.github.com/orgs/{org}/actions/runner-groups/{runner_group_id}/repositories` Memory Tree source refreshes now require exact clean canonical route parity before reading or persisting response bodies. Drifted final URLs with query/fragment/userinfo/auth, different org/group/tail, or lookalike authority fail closed with no vault artifact and no public leakage of drifted repository names, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking values in result/jobs/catalog/search output.

New GitHub repository Contents final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/contents[/path...]` Memory Tree source refreshes now require HTTPS and exact sanitized canonical Contents route parity before reading or persisting response bodies. Drifted/different-repository or different-path refreshes fail closed before body read with no vault artifact and no public leakage of rejected content names, raw final URLs, query/fragment/userinfo auth, raw prompts, file bodies/content/encoding/download/html/git/_links metadata, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values in result, job, search, or relevant-memory output.

New GitHub repository README source-refresh now rejects final-URL route drift: exact `GET https://api.github.com/repos/{owner}/{repo}/readme` Memory Tree source refreshes now require the response final URL to stay on the sanitized canonical README API route before body parsing or vault persistence. Drifted README responses fail closed with no vault artifact, metadata-only public evidence, and no public leakage of raw final URLs, query/fragment/userinfo auth, raw prompts, README content/body, content/encoding/download/html/git/_links metadata, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values in result, job, search, or relevant-memory output.

New GitHub repository license final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/license` Memory Tree source refreshes now require HTTPS and reject response final URLs that drift to another repository, add query/fragment/userinfo, use a lookalike authority, or otherwise differ from the sanitized canonical license route before reading or persisting response bodies. Drifted/noncanonical refreshes fail closed with no vault artifact and no public leakage of redirected repository labels, raw final URLs, license bodies, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixture values in result, job, search, or relevant-memory output.

New relevant-memory route receipt slice: `/api/spaces/memory` now returns a full metadata-only Memory Tree trust envelope beside sanitized relevant-memory hits: server-generated no-authority `memory_advisory`, `memory_context` prompt-preflight, `space.memory.read` autonomy-policy evidence with `hint:summarize`, `space.memory:<space_id>` progress receipts, and output-compaction evidence built only from allow-listed metadata. Compaction, preflight, policy, and progress receipt failures fall back to safe metadata-only receipts so read-only relevant-memory lookups do not fail or leak summaries, origin URIs, renderer/source/html/script/data/API-auth fields, raw prompts, generated widget bodies, credentials, tokens, or secret-looking fixture values.

New active-space lifecycle receipt slice: direct `/api/spaces/activate` and `/api/spaces/deactivate` session context switches now return metadata-only active-space trust envelopes beside the sanitized session receipt: required active-space-switch prompt-preflight, `space.activate` / `space.deactivate` autonomy-policy evidence, server-generated Memory Tree advisory/no-authority, paired `tool.started` → `tool.completed` lifecycle progress receipts, and output-compaction evidence with safe `space.activate:<space_id>` / `space.deactivate:<space_id>` or neutral `space.deactivate:session` run ids. Alias conflicts, missing selectors, missing Spaces, and missing sessions fail before progress telemetry, while session messages, pending prompts, composer drafts, renderer/source/html/script/API-auth fields, raw prompts, generated widget bodies, credentials, tokens, and secret-looking fixtures stay omitted from route responses, progress logs, and compaction text.

New GitHub repository languages final-URL drift no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/languages` Memory Tree source refreshes now reject response final URLs that drift away from the sanitized `api.github.com` repository Languages route before any response-body read, JSON parsing, or vault persistence. Redirected/different-repository payloads remain pending with no vault/search/relevant-memory artifact and no public leakage of language rows, raw API routes, query/fragment auth markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values in job/source/search output.

New GitHub single branch final-URL drift no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/branches/{branch}` Memory Tree source refreshes now require response final URLs to match the clean canonical single-branch route for the same repository and branch before any response-body read, JSON parsing, or vault persistence. Query/fragment/userinfo auth, repository drift, branch-route drift, and raw-prompt fragments fail closed with no vault/search/relevant-memory artifact and no public leakage of raw final URLs, branch body sentinels, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values in result/job/search output.

New GitHub repository tags final-URL drift no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/tags` Memory Tree source refreshes now require HTTPS and exact sanitized canonical tags-route parity before any response-body read, JSON parsing, or vault persistence. Drift to another repo, query/fragment/userinfo auth, lookalike authority, or any final-URL mismatch fails closed with no vault/search/relevant-memory artifact and no public leakage of rejected tag names, raw final URLs, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values in result/job/catalog/search/relevant-memory output.

New GitHub repository branch-list final-URL no-body-read + relevant-memory-empty evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/branches` Memory Tree source refreshes now require HTTPS and exact sanitized canonical branch-list route parity before any response-body read, JSON parsing, vault persistence, search artifact, or Spaces relevant-memory result. Drift to another repo, query/fragment/userinfo auth, lookalike authority, or any final-URL mismatch fails closed with no public leakage of rejected branch names, raw final URLs, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values in result/job/catalog/search/relevant-memory output.

New direct Space update lifecycle slice: direct `/api/spaces/update` active-instruction safety receipts now return paired metadata-only `tool.started` and `tool.completed` progress receipts under stable `space.update:<space_id>` run ids after prompt preflight passes, preserve the completed event as `progress_event`, include `progress_event_types: tool.started, tool.completed` in output-compaction evidence, and avoid start-row telemetry for blocked hostile instruction updates while omitting renderer/source/html/script/data/API-auth fields, raw prompts, caller-forged memory authority, and secret-looking fixture values.

New Space creation lifecycle slice: direct/source-style `/api/spaces/create` safety receipts and `/api/spaces/create-from-session` now return paired metadata-only `tool.started` and `tool.completed` progress receipts under stable `space.create:<space_id>` / `space.create_from_session:<space_id>` run ids, preserve the completed event as `progress_event`, include `progress_event_types: tool.started, tool.completed` in compaction evidence, and avoid recording start rows for rejected hostile/duplicate create attempts.

New source duplicate/delete lifecycle slice: source-style Space duplicate/clone and delete/remove actions now return and persist paired metadata-only `tool.started` and `tool.completed` progress receipts under stable `space.duplicate:<target_space_id>` / `space.delete:<source_space_id>` run ids, keep the completed event as `progress_event`, and include `progress_event_types: tool.started, tool.completed` in compaction evidence without leaking renderer/source/html/script/data/API-auth fields, raw prompts, generated widget bodies, caller-forged memory authority, or secret-looking fixture values.

New source-refresh route lifecycle slice: manual `/api/capy-memory/source/refresh` and scheduled `/api/capy-memory/source/refresh/scheduled` route actions now return paired metadata-only `run.started` and `run.completed` lifecycle receipts under fixed `source-refresh.manual` / `source-refresh.scheduled` run ids while preserving the backward-compatible completed `progress_event`. Source-refresh compaction evidence includes `progress_event_types: run.started, run.completed`, route sanitization preserves safe legacy completed receipts, and partial recorder failures close active runs without leaking raw origin URLs, fetched source text, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking values.

New source repair-layout lifecycle slice: `space.spaces.repairLayout` now returns paired metadata-only `tool.started` and `tool.completed` progress lifecycle events under the safe `repair:<space_id>` run id while preserving the backward-compatible completed `progress_event`. The source-style repair compaction receipt includes `progress_event_types: tool.started, tool.completed`, and fallback receipts remain metadata-only without leaking renderer/source/html/script/data/API-auth fields, prompts, credentials, tokens, or secret-looking values.

New no-space health progress lifecycle slice: `space.api.health` and `space.health` now return paired metadata-only `tool.started` and `tool.completed` progress lifecycle events under the neutral `space.health:api` run id with no `space_id`, while preserving the backward-compatible completed `progress_event`. Health helper output-compaction evidence includes the safe event-type list and continues to omit raw renderer/source/html/script/data/API-auth fields, prompts, memory-authority forgeries, credentials, tokens, and secret-looking values.

New Browser Surface progress lifecycle slice: receipt-only Browser Surface tool actions (`space.browser.*` / `browser.*`) now return metadata-only `tool.started` plus `tool.completed` progress lifecycle events under the same safe `browser.<action>:<space_id>` run id while preserving the backward-compatible completed `progress_event`. Browser Surface output-compaction evidence now includes the safe event-type list, and browser requests still remain approval-gated, unexecuted, metadata-only, and free of raw URLs, prompts, renderer/source/html/script/API-auth fields, caller-forged memory authority, credentials, tokens, and secret-looking values.

New create-from-session receipt slice: `/api/spaces/create-from-session` now returns a metadata-only trust envelope when turning the current chat into a starter Space: required `create_from_session` prompt-preflight evidence, `space.create_from_session` autonomy-policy evidence, `space.create_from_session:<space_id>` structured progress, a server-generated Memory Tree advisory/no-authority receipt, and output-compaction evidence. The route now uses a minimal session receipt and sanitized Space title/widget-count metadata so chat messages, pending prompts, composer drafts, compression summaries, whitespace API-key/API-auth title markers, renderer/source/html/script/API-auth fields, caller-forged memory authority, credentials, tokens, generated widget bodies, and secret-looking values remain absent from backend responses and the UI receipt.

New source-refresh route preflight slice: manual and scheduled Memory Tree source-refresh route responses now return a top-level metadata-only `capy_memory_source_refresh` prompt-preflight receipt in addition to per-job auto-fetched-source preflight rows. Product-home result cards render the route-level Prompt preflight evidence beside autonomy policy, progress, Memory advisory, and output-compaction receipts; aggregate pass/warn/block/required status stays aligned across policy and compaction, while job/result-forged prompt hashes, raw prompts, renderer/source/API-auth fields, credentials, tokens, and secret-looking values remain omitted.

New camera stream advisory receipt slice: approved `space.camera.add_stream` / `camera.add_stream` results now include the server-generated Memory Tree advisory/no-authority envelope beside prompt-preflight, autonomy-policy, structured progress, and metadata-only output-compaction evidence. Camera stream compaction evidence now clamps forged memory authority through the shared advisory summary and marks itself `metadata_only`, while private stream URLs, hosts/ports, query/bearer tokens, renderer/source/html/script/data/API-auth fields, caller-forged `trusted_system_memory` / bypass flags, and secret-looking values stay out of public responses.

New Space Agent package import preflight slice: package imports with no active `instructions` / `agent_instructions` / `prompt` now still return a metadata-only required `prompt_preflight` receipt for the `space_agent_package_import` boundary, and direct imports plus `space.import` tool aliases propagate `prompt_preflight_status: required` through autonomy-policy and output-compaction evidence. Import responses continue to carry the server-generated Memory Tree advisory/no-authority envelope and quarantine generated widget bodies while omitting renderer/html/script/source/data/API-auth fields, credentials, caller-forged memory authority, and secret-looking values.

New GitHub repository invitations job-kind hardening slice: the list endpoint `GET https://api.github.com/repos/{owner}/{repo}/invitations` now consistently preserves the public label `github repository invitations {owner}/{repo}` while keeping the canonical GitHub API route only as hidden `fetch_origin_uri`, and propagates `source_refresh_kind: github_repository_invitations` through registration, due requeue, legacy pending jobs, custom fetcher fallback, source rows, and vault output. Public-label registrations and legacy raw/public payload variants reconstruct safely without leaking query/fragment auth, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values; single-invitation routes remain distinct.

New GitHub repository single-invitation source-refresh slice: exact `GET https://api.github.com/repos/{owner}/{repo}/invitations/{invitation_id}` JSON objects now use metadata-only public labels like `github repository invitation capy/spaces #701` with hidden clean-route reconstruction. Accepted payloads persist only repo path, invitation id, invitee login, inviter login, permission, expired flag, and safe created timestamp; list/feed/text fallback, id drift, route abuse, non-exact URL-ish aliases, dangerous nested source/data/body/html/script/auth/raw-prompt/API-auth fields, raw API routes, auth/query/fragment/userinfo/port/lookalike hosts, credentials, tokens, and secret-looking values fail closed or stay absent from receipts, jobs, catalog, search, relevant-memory hits, and vault output.

New GitHub dependency graph SBOM source-refresh slice: exact `GET https://api.github.com/repos/{owner}/{repo}/dependency-graph/sbom` JSON objects now use metadata-only public labels like `github dependency graph sbom capy/spaces` with hidden clean-route reconstruction. Accepted payloads persist only repo path, bounded package count, and up to five safe package name/version previews; raw SBOM docs, SPDX ids, download/checksum/license/external-ref/supplier/originator fields, API routes, auth/query/fragment/userinfo/port/uppercase/lookalike hosts, encoded/extra tails, text/JSON Feed fallback, oversized packages, final URL drift, renderer/source/html/script/data/API-auth fields, credentials, tokens, and secret-looking values fail closed or stay absent from public output.

New GitHub Dependabot organization private-name selected-repositories source-refresh slice: exact `GET https://api.github.com/orgs/{org}/dependabot/secrets/{secret_name}/repositories` JSON objects now use metadata-only public labels like `github dependabot organization private name repositories capy CAPY_MODE` with clean-route reconstruction from safe org/private-name metadata. Accepted payloads persist only org, safe private-name identifier, repository count, and bounded safe repository id/name/full-name/private previews; raw/legacy unsafe source IDs hash to stable safe IDs across registration, job-list, due-queue, run result, catalog, search, relevant-memory, and vault output. Raw API routes, query/fragment/userinfo/auth markers, raw prompts, renderer/source/html/script/data/body/API-auth fields, `selected_repositories_url`, encrypted/key material markers, non-empty `temp_clone_token`, credentials, tokens, adjacent routes, text/JSON Feed fallback, count drift, oversized/malformed rows, and final-URL drift fail closed or stay absent from public output.

New GitHub Dependabot organization single private-name source-refresh slice: exact `GET https://api.github.com/orgs/{org}/dependabot/secrets/{secret_name}` JSON objects now use metadata-only public labels like `github dependabot organization private name capy CAPY_MODE` with clean-route reconstruction from safe org/private-name metadata. Accepted payloads persist only org, safe private-name identifier, allow-listed visibility, selected-repository count, and safe timestamps; token-shaped source IDs normalize to stable safe IDs, while list/public-key/repositories adjacent routes, text/JSON Feed fallback, final-URL drift, raw API/auth/query/fragment/userinfo markers, raw prompts, renderer/source/html/script/data/body/API-auth fields, key material, credentials, tokens, and secret-looking values fail closed or stay absent from receipts, catalog, jobs, search, relevant-memory, and vault output.

New GitHub Pages latest-build source-refresh slice: exact `GET https://api.github.com/repos/{owner}/{repo}/pages/builds/latest` JSON object refreshes now use metadata-only public labels like `github pages latest build capy/spaces` with hidden clean fetch-route reconstruction. Accepted payloads persist only safe repo path, build id, allow-listed status, SHA prefix, safe pusher login, safe timestamps, nonnegative duration, and `error present`; arrays/feed/text fallback, unsafe fields, malformed/latest-adjacent routes, final URL drift, raw API URLs/auth/query/fragment/userinfo, prompts, renderer/source/data/html/script, and secret-looking values fail closed without vault/search leakage.

New GitHub Pages builds final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/pages/builds` Memory Tree source refreshes now require the response final URL to equal the sanitized canonical Pages builds fetch URL before any response-body read. Drift to another repo, query/fragment/auth/userinfo, raw-prompt fragments, or any noncanonical final URL fails closed with no vault/search/relevant-memory artifact and no public leakage of drifted build rows, raw final URLs, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking values in result/jobs/catalog/search/relevant-memory output.

New GitHub Pages deployment-status source-refresh slice: exact `GET https://api.github.com/repos/{owner}/{repo}/pages/deployments/{pages_deployment_id}` JSON object refreshes now use metadata-only public labels like `github pages deployment capy/spaces 123` or `github pages deployment capy/spaces abc123...` with hidden clean fetch-route reconstruction. Accepted payloads persist only repo, safe deployment id/SHA prefix, allow-listed status, and safe timestamps; malformed/adjacent pages routes, final-URL drift, JSON Feed/text fallback, raw URLs, auth markers, prompts, renderer/source/html/script/data/body fields, tokens, and secrets fail closed without catalog/search/vault leakage.

New GitHub Dependabot organization private-name source-refresh slice: exact `GET https://api.github.com/orgs/{org}/dependabot/secrets` JSON objects now use metadata-only public labels like `github dependabot organization private names capy` with clean-route reconstruction from safe org metadata/public labels. Accepted payloads persist only org, private-name count, bounded safe names, allow-listed visibility, selected-repository counts, and safe timestamps; raw route source ids are normalized to stable safe markers and raw API URLs/key material/auth/prompt/renderer fields stay out of receipts, catalog, jobs, search, relevant-memory, and vault output.

New GitHub Actions organization selected-actions source-refresh slice: exact `GET https://api.github.com/orgs/{org}/actions/permissions/selected-actions` JSON objects now use metadata-only public labels like `github actions organization selected actions capy` with clean-route reconstruction from safe org metadata. Accepted payloads persist only org, `github_owned_allowed`, `verified_allowed`, bounded safe `patterns_allowed`, and pattern counts; raw-route source ids are normalized to stable safe org selected-actions ids across receipts, catalog, jobs, search, relevant-memory, and vault output. Text fallback, JSON Feed-shaped bodies, adjacent repo-selected-actions/org-permissions/org-workflow routes, lookalike/uppercase/http/port/userinfo authorities, unsafe org segments, encoded/suffixed tails, final URL drift, raw API URLs, `selected_actions_url`, raw prompts, renderer/source/html/script/data/body/API-auth fields, credentials, tokens, and secret-looking values fail closed or stay absent from public output.

New GitHub Actions organization single-secret/private-name source-refresh slice: exact `GET https://api.github.com/orgs/{org}/actions/secrets/{secret_name}` JSON objects now use metadata-only public labels like `github actions organization private name capy CAPY_MODE` with clean-route reconstruction from safe org/private-name metadata. Accepted payloads persist only org, safe private-name identifier, allow-listed visibility, selected-repository count, and safe timestamps; raw route source ids are normalized to stable safe private-name ids across receipts, catalog, jobs, search, relevant-memory, and vault output. List/public-key/selected-repositories/repo-scoped adjacent routes, query/fragment/userinfo/port/lookalike/uppercase authorities, encoded/extra tails, final URL drift, non-JSON/text/JSON Feed fallback payloads, raw API URLs, raw prompts, renderer/source/html/script/data/body/API-auth fields, encrypted/key material, credentials, tokens, and secret-looking values fail closed or stay absent from public output.

New GitHub Actions organization secrets public-key source-refresh slice: exact `GET https://api.github.com/orgs/{org}/actions/secrets/public-key` JSON objects now use metadata-only public labels like `github actions organization secrets public key capy` with clean-route reconstruction from safe metadata/public labels. Accepted payloads persist only org and safe numeric-string `key_id`; documented `key`, `url`, raw prompt, API-auth, token, and key-material fields are ignored/omitted. Adjacent repo public-key, org secrets list, and single-secret routes, malformed/encoded tails, unsafe key ids, userinfo/port/lookalike/uppercase/trailing-dot authorities, auth query/fragment markers, JSON Feed/text fallback payloads, renderer/source/html/script/data fields, credentials, tokens, and secret-looking values fail closed or stay absent from receipts, catalog, jobs, search, relevant-memory, and vault output.

New GitHub Actions organization secrets source-refresh slice: exact `GET https://api.github.com/orgs/{org}/actions/secrets` JSON objects now use metadata-only public labels like `github actions organization secrets capy` with clean-route reconstruction. Accepted payloads persist only org, secret/private-name count, bounded safe secret names, allow-listed visibility, selected repository counts, and safe timestamps; documented `selected_repositories_url` plus any value/key material remain ignored/omitted. Dirty/raw source ids, names, titles, display names, userinfo/query/fragment auth markers, JSON Feed/text fallback payloads, lookalike hosts, malformed/adjacent routes, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, and secret-looking values fail closed or stay absent from receipts, catalog, jobs, search, relevant-memory, and vault output.

New GitHub Actions organization variables source-refresh slice: exact `GET https://api.github.com/orgs/{org}/actions/variables` JSON objects now use metadata-only public labels like `github actions organization variables capy` with clean-route reconstruction. Accepted payloads persist only org, variable count, bounded safe variable names, allow-listed visibility, selected repository count/id/name metadata when safe, and safe timestamps; documented `value` and `selected_repositories_url` fields are ignored/omitted. Dirty/raw source ids, names, titles, display names, userinfo/query/fragment auth markers, JSON Feed/text fallback payloads, lookalike hosts, malformed routes, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, and secret-looking values fail closed or stay absent from receipts, catalog, jobs, search, relevant-memory, and vault output.

New GitHub Actions organization permissions source-refresh slice: exact `GET https://api.github.com/orgs/{org}/actions/permissions` JSON objects now use metadata-only public labels like `github actions organization permissions capy` with clean-route reconstruction. Accepted payloads persist only org plus allow-listed `enabled_repositories` and `allowed_actions` policy metadata; selected-actions URLs, auth/prompt/renderer/source/html/script/data fields, route abuse, JSON Feed/text fallback, final URL drift, and adjacent org/repo permissions routes fail closed or stay omitted.

New camera template receipt slice: the Camera Dashboard template install route now returns the same metadata-only trust envelope as other high-risk templates: prompt-preflight evidence, `space.template.install.camera` autonomy-policy evidence with a vision-route hint, structured template-install progress, server-generated Memory Tree advisory/no-authority metadata, and output-compaction evidence. Hostile install payload fields such as renderer/html/script/source/data/API-auth, raw prompts, credentials/tokens/secrets, generated widget bodies, stream URLs, and `rtsp://` markers are ignored and remain absent from public route responses.

New GitHub Actions organization self-hosted runners source-refresh slice: exact `GET https://api.github.com/orgs/{org}/actions/runners` JSON objects now use metadata-only public labels like `github actions organization runners capy` with clean fetch-route reconstruction from kind/org metadata. Accepted payloads persist only org, runner count, and bounded safe runner id/name/status/busy/os/architecture/label names; route abuse, JSON Feed/text fallback, malformed/count-drift payloads, unsafe row fields/values, oversized rows, raw API/auth/query/fragment/userinfo fields, prompts, renderer/source/html/script/data fields, credentials, tokens, and secret-looking values fail closed without public leakage.

New GitHub Actions organization runner-groups source-refresh slice: exact `GET https://api.github.com/orgs/{org}/actions/runner-groups` JSON objects now use metadata-only public labels like `github actions organization runner groups capy` with clean fetch-route reconstruction. Accepted payloads persist only org, runner-group count, and bounded safe group id/name/visibility/allow-listed booleans/selected workflows; adjacent org/repo runner routes remain distinct, and route abuse, JSON Feed/text fallback, count drift, unsafe fields/values, final URL drift, raw API/auth/query/fragment/userinfo fields, prompts, renderer/source/html/script/data/body fields, credentials, tokens, and secret-looking values fail closed without public leakage.

New GitHub Actions organization runner-group runners source-refresh slice: exact `GET https://api.github.com/orgs/{org}/actions/runner-groups/{runner_group_id}/runners` JSON objects now use metadata-only public labels like `github actions organization runner group runners capy group 201` with clean fetch-route reconstruction. Accepted payloads persist only org, runner group id, runner count, and bounded safe runner id/name/status/busy/os/architecture/labels; adjacent org runner-groups/repositories/runners/downloads and repo-scoped runner-group routes remain distinct, and route abuse, JSON Feed/text fallback, count drift, unsafe top-level/runner/label fields, final URL drift, raw API/auth/query/fragment/userinfo fields, prompts, renderer/source/html/script/data/body fields, credentials, tokens, and secret-looking values fail closed without public leakage across receipts, catalog, jobs, search, relevant-memory, and vault output.

New GitHub Actions org runner-group selected repositories source-refresh slice: exact `GET https://api.github.com/orgs/{org}/actions/runner-groups/{runner_group_id}/repositories` JSON pages now use metadata-only public labels like `github actions runner group repositories capy group 201` with clean fetch-route reconstruction. Accepted payloads persist only safe repository id/name/full-name/private summaries with org/name consistency; documented URL/template/repository metadata stays ignored, and route abuse, JSON Feed/text fallback, malformed or oversized pages, raw API/auth/query/fragment/userinfo fields, raw prompts, renderer/source/html/script/data/body fields, non-empty temp clone tokens, credentials, tokens, and secret-looking values fail closed without public leakage across receipts, catalog, jobs, search, relevant-memory hits, or vault output.

New GitHub Actions single runner-group source-refresh slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/runner-groups/{runner_group_id}` JSON object refreshes now use metadata-only public labels like `github actions runner group capy/spaces group 201` with clean fetch-route reconstruction. Accepted payloads persist only safe runner-group fields with route id parity; JSON-only validation fails closed for unexpected fields, unsafe visibility/workflows, id/final-URL drift, route abuse, raw API/auth/query/fragment/userinfo fields, raw prompts, renderer/source/html/script/data fields, tokens, and secret-looking values without public leakage.

New GitHub Actions runner-group runners source-refresh slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/runner-groups/{runner_group_id}/runners` JSON refreshes now use metadata-only public labels like `github actions runner group runners capy/spaces group 201` with clean fetch-route reconstruction. Accepted payloads persist only repo, runner group id, runner count, and bounded safe runner id/name/status/busy/os/architecture/labels; route abuse, JSON Feed/text fallback, malformed/count-drift payloads, raw API/auth/query/fragment/userinfo fields, prompts, renderer/source/html/script/data fields, tokens, and secret-looking values fail closed without public leakage.

New GitHub Actions runner labels source-refresh slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/runners/{runner_id}/labels` JSON object refreshes now use metadata-only public labels like `github actions runner labels capy/spaces runner 101` while reconstructing the clean fetch route from private kind/repo/runner metadata. Accepted payloads persist only repo, runner id, label count, and bounded safe label names/types; route abuse, JSON Feed/text fallback, unsafe rows/fields, count drift, >100 labels, final URL drift, raw API/auth/query/fragment/prompt/script/source/html/data/body fields, credentials, tokens, and secret-like fixtures fail closed or stay omitted from receipts, catalog/jobs, vault, and search.

New GitHub Actions single artifact source-refresh slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/artifacts/{artifact_id}` JSON object refreshes now use hidden clean canonical fetch origins with public labels like `github actions artifact capy/spaces 201`. Accepted payloads persist only metadata-only repo/id/name/size/expired/timestamp summaries; id mismatch, JSON Feed fallback, unsafe public names, malformed/lookalike/port/uppercase/encoded-tail routes, raw API/auth/query/fragment/prompt/script/source/html/data/body fields, credentials, tokens, and secret-like fixtures fail closed or remain omitted from receipts, catalog/jobs, vault, and search.

New GitHub Actions self-hosted runners source-refresh slice: exact `GET https://api.github.com/repos/{owner}/{repo}/actions/runners` JSON refreshes are covered by the existing parser/source-refresh metadata path with public labels like `github actions runners {owner}/{repo}`. Accepted output remains metadata-only: runner count plus bounded runner id/name/status/busy/os/architecture/safe label names; raw API URLs/routes, query/fragment/userinfo/ports, registration tokens, API-auth fields, prompts, renderer/source/data/html/script fields, credentials, and secret-like fixtures stay omitted or fail closed. This is a docs/status alignment for already-implemented and tested coverage, not a new scheduler/UI expansion.

New GitHub workflow-runs row-bound slice: the unscoped `GET https://api.github.com/repos/{owner}/{repo}/actions/runs` and workflow-scoped `GET https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs` source-refresh parsers now reject JSON objects whose `workflow_runs` arrays exceed 100 rows. Bounded/empty workflow-run metadata remains metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no public leakage of rejected run rows, API URLs, auth/query/fragment markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, `logs_url`, `jobs_url`, or secret-looking fixtures; public labels continue to use hidden clean fetch origins.

New GitHub Actions artifacts row-bound slice: the repository artifacts (`https://api.github.com/repos/{owner}/{repo}/actions/artifacts`) and workflow-run artifacts (`https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts`) source-refresh parsers now reject exact `artifacts` arrays above 100 rows. Bounded artifact metadata remains metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no public leakage of rejected artifact rows, auth/query/fragment markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub repository security-advisories row-bound slice: the exact `https://api.github.com/repos/{owner}/{repo}/security-advisories` source-refresh parser now rejects JSON lists above 100 rows. Bounded advisory metadata remains metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no public leakage of raw advisory/security fixture rows, GHSA/CVE/sentinel details, API URLs/auth markers, raw prompts, renderer/source/html/script/API-auth fields, vulnerabilities, descriptions, private flags, credentials, tokens, or secret-looking fixtures.

New GitHub Dependabot alerts row-bound slice: the exact `https://api.github.com/repos/{owner}/{repo}/dependabot/alerts` source-refresh parser now rejects JSON lists above 100 rows. Bounded alert metadata remains metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no public leakage of raw alert rows, API URLs/auth markers, raw prompts, renderer/source/html/script/API-auth fields, advisory/vulnerability details, CVE/GHSA ids, version ranges, credentials, tokens, or secret-looking fixtures.

New GitHub code scanning alerts row-bound slice: the exact `https://api.github.com/repos/{owner}/{repo}/code-scanning/alerts` source-refresh parser now rejects JSON arrays above 100 rows. Bounded alert metadata remains metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no public leakage of rejected alert metadata, raw API routes, auth/query/fragment markers, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub repository contents row-bound slice: the contents source-refresh parser now rejects canonical `https://api.github.com/repos/{owner}/{repo}/contents[/path]` directory-list JSON arrays above 100 rows. Bounded contents metadata and single-object payloads remain safe and metadata-only, while oversized list payloads fail closed as pending refresh failures with no vault artifact and no public leakage of rejected row names/counts, API URLs/auth markers, raw prompts, renderer/source/data/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub PR commit-list row-bound slice: the pull-request commit-list source-refresh parser now rejects exact `/repos/{owner}/{repo}/pulls/{number}/commits` JSON arrays above 100 rows. Bounded PR commit metadata remains safe and metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no public leakage of raw/rejected commit metadata, URLs/auth markers, prompts, renderer/source/html/script/API-auth fields, credentials, tokens, secrets, or secret-looking fixtures in public output.

New GitHub commit-comment reactions row-bound slice: the commit-comment reactions source-refresh parser now rejects exact `https://api.github.com/repos/{owner}/{repo}/comments/{comment_id}/reactions` JSON arrays over 100 rows. Bounded commit-comment reaction metadata remains safe and metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no public leakage of rejected reactor metadata, raw/unsafe fields, URL/auth markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub issue reactions row-bound slice: the issue reactions source-refresh parser now rejects exact `https://api.github.com/repos/{owner}/{repo}/issues/{number}/reactions` JSON arrays over 100 rows. Bounded reaction metadata remains safe and metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no public leakage of rejected reaction metadata, URL/auth markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub issue-comment reactions row-bound slice: the issue-comment reactions source-refresh parser now rejects exact `https://api.github.com/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions` JSON arrays over 100 rows. Bounded comment-reaction metadata remains safe and metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no public leakage of rejected reactor logins/ids/counts, URL/auth markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub pull review comment reactions row-bound slice: the pull review comment reactions source-refresh parser now rejects exact `https://api.github.com/repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions` JSON arrays over 100 rows. Bounded pull-comment reaction metadata remains safe and metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no public leakage of rejected reactor logins/ids/counts, URL/auth markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub repository teams row-bound slice: the teams source-refresh parser now rejects exact `https://api.github.com/repos/{owner}/{repo}/teams` JSON arrays above 100 rows. Bounded team metadata remains safe and metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact, no search hit, and no rejected team names/slugs/ids, URL/auth markers, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub assignees row-bound slice: the assignees source-refresh parser now rejects exact `https://api.github.com/repos/{owner}/{repo}/assignees` JSON arrays above 100 rows. Bounded assignee metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no rejected assignee logins/counts, URL/auth markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub collaborators row-bound slice: the collaborators source-refresh parser now rejects exact `https://api.github.com/repos/{owner}/{repo}/collaborators` JSON arrays above 100 rows. Bounded collaborator metadata remains safe and metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no rejected collaborator logins/counts, URL/auth markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub environments row-bound slice: the environments source-refresh parser now rejects exact `/repos/{owner}/{repo}/environments` payloads whose `environments` array exceeds 100 rows. Bounded environment metadata remains metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no public leakage of rejected environment names, URLs, protection rules, deployment branch policies, raw prompts, renderer/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub fork-list row-bound slice: the fork-list source-refresh parser now rejects exact `https://api.github.com/repos/{owner}/{repo}/forks` JSON arrays above 100 rows. Bounded fork metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw/rejected fork row metadata, URL/auth markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub stargazers row-bound slice: the stargazers source-refresh parser now rejects exact `https://api.github.com/repos/{owner}/{repo}/stargazers` JSON arrays above 100 rows. Bounded stargazer metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no rejected logins/counts, URL/auth markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search/catalog output.

New GitHub subscribers/watchers row-bound slice: the subscribers source-refresh parser now rejects exact `https://api.github.com/repos/{owner}/{repo}/subscribers` JSON arrays above 100 rows. Bounded and empty subscriber metadata remain safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no rejected subscriber logins/counts, URL/auth markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub contributors final-URL no-body-read evidence slice: exact `GET https://api.github.com/repos/{owner}/{repo}/contributors` Memory Tree source refreshes now prove response final-URL drift is rejected before any response-body read. Query/fragment auth and repository drift leave jobs pending with no vault/search/relevant-memory artifacts and no public leakage of raw final URLs, hostile contributor rows, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub contributors row-bound slice: the contributors source-refresh parser now rejects exact `https://api.github.com/repos/{owner}/{repo}/contributors` JSON arrays above 100 rows. Bounded contributor metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no rejected contributor logins/counts, URL/auth markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub deploy keys final-URL no-body-read evidence-alignment slice: exact `GET /repos/{owner}/{repo}/keys` Memory Tree source refreshes now prove query/fragment auth drift, userinfo/credential drift, repository drift, and route-tail/single-key drift are rejected before any response-body read or persistence. The regression also checks Spaces relevant-memory-empty output for a benign space id, with no vault artifact, no search hit, and no public leakage of raw final URLs, hostile deploy-key body sentinels, SSH/public-key material, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixtures.

New GitHub deploy keys final-URL drift hardening slice: exact `GET https://api.github.com/repos/{owner}/{repo}/keys` Memory Tree source refreshes now require the response final URL to remain the sanitized canonical deploy-keys API route before any response-body read or persistence. Query/fragment/userinfo/credential drift fails closed with no vault artifact, no search hit, and no public leakage of raw final URLs, access-token markers, raw prompts, SSH/public key material, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixtures.

Current latest known completed code slice: the OpenHuman-inspired Capy Memory Tree / compaction / source freshness / policy / progress MVP surfaces are implemented as metadata-only foundations, with recent expansion through manual/scheduled source-refresh Memory Tree advisory/no-authority receipts in backend responses and product-home result UI, source registration/source-refresh queue trust labels in backend receipts and product-home UI, GitHub repository-tags, commit-list, and branch-list row-bound hardening, GitHub Actions selected-actions public-output hardening with hidden clean fetch-origin preservation, GitHub repository security-advisories public-label registration/catalog/job-list hardening with hidden clean fetch-origin preservation, GitHub single Dependabot alert metadata and due-requeue hidden fetch-origin preservation, Dependabot alerts list public-label registration plus >100-row fail-closed hardening with hidden clean fetch-origin preservation, code-scanning alerts list public-label registration with hidden clean fetch-origin preservation, code-scanning default-setup metadata, secret-scanning scan-history due-requeue hidden fetch-origin preservation, code-scanning analyses and single-analysis metadata, code-scanning alert instances, single code-scanning alert, single secret-scanning alert, secret-scanning alert-locations, secret-scanning scan-history, repository autolinks/deploy keys/private-vulnerability-reporting, workflow-scoped runs, commit-associated pull requests, repository topics public-origin hardening, legacy repository-invitations/teams/collaborators/milestones/forks public-origin hardening, assignees/subscribers/stargazers raw-host hardening, CODEOWNERS errors public-output redaction and parser coverage, runner-groups/downloads, workflow attempt jobs/pending deployments/artifacts, Actions repository/workflow/selected permissions/variables/secrets/public-key/private-name metadata, Dependabot alert/security/private-name/public-key metadata, GitHub environment deployment branch... [truncated]

New GitHub Actions organization runner labels source-refresh slice: Memory Tree source refresh now supports exact `GET https://api.github.com/orgs/{org}/actions/runners/{runner_id}/labels` JSON objects as metadata-only records. Public source/catalog/job/search/relevant-memory/vault output uses labels such as `github actions organization runner labels capy runner 101` while workers reconstruct only the hidden clean API route; persisted summaries include bounded safe label names/types, omitting raw API/auth/query/fragment/userinfo, raw prompts, renderer/source/html/script/data/body fields, credentials, tokens, and secret-looking values. Adjacent org/repo runner routes, route abuse, JSON Feed/text fallbacks, unsafe payloads, count drift, over-100 labels, and final-URL drift fail closed before body read or persistence and now explicitly leave Spaces relevant-memory empty.

New GitHub repository rulesets row-bound slice: the rulesets source-refresh parser now rejects exact `https://api.github.com/repos/{owner}/{repo}/rulesets` JSON arrays above 100 rows. Bounded rulesets metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw/rejected ruleset row metadata, URLs/auth markers, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, bypass actor/rule condition details, or secret-looking fixtures in public result/job/search output.

New GitHub milestones row-bound slice: the milestone-list source-refresh parser now rejects exact `/repos/{owner}/{repo}/milestones` JSON arrays above 100 rows. Bounded milestone metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw/rejected milestone metadata, URL/auth markers, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub commit-list row-bound slice: the commit-list source-refresh parser now rejects exact `/repos/{owner}/{repo}/commits` JSON arrays above 100 rows. Bounded commit-list metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw/rejected commit metadata, URL/auth markers, raw prompts, renderer/source/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub repository-tags row-bound slice: the tags source-refresh parser now rejects exact `GET https://api.github.com/repos/{owner}/{repo}/tags` JSON arrays above 100 rows. Empty and bounded tag metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw/rejected tag metadata, query/fragment auth markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub labels row-bound slice: the repository-labels and issue-labels source-refresh parsers now reject exact `GET https://api.github.com/repos/{owner}/{repo}/labels` and `/repos/{owner}/{repo}/issues/{number}/labels` JSON arrays above 100 rows. Bounded label metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw label row metadata, URL/auth markers, prompts, renderer/source/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub branch-list row-bound slice: the branch-list source-refresh parser now rejects exact `/repos/{owner}/{repo}/branches` JSON arrays above 100 rows. Empty and bounded branch metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no auth/query/fragment markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, tokens, secret-looking fixtures, or rejected branch metadata in public result/job/search output.

New GitHub commit-associated PR row-bound slice: the commit-associated pull-request source-refresh parser now rejects exact `/repos/{owner}/{repo}/commits/{40-hex-sha}/pulls` JSON arrays above 100 rows. Bounded PR metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw PR bodies, URLs/API-auth markers, raw prompts, renderer/source/html/script fields, credentials, tokens, or rejected row metadata in public result/job/search output.

New GitHub comment-list row-bound slice: the issue comments, pull review comments, and commit comments source-refresh parsers now reject exact comment-list JSON arrays above 100 rows. Bounded comment metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw comment bodies, URLs/auth markers, prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or rejected row metadata in public result/job/search output.

New GitHub pull-review row-bound slice: the pull-review source-refresh parser now rejects exact `GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews` JSON arrays above 100 review rows. Bounded review metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no rejected review metadata, URLs/auth markers, prompts, renderer/source/script/API-auth fields, credentials, or secret-looking values in public result/job/search output.

New GitHub PR file-list row-bound slice: the pull-request file-list source-refresh parser now rejects exact `/repos/{owner}/{repo}/pulls/{number}/files` JSON arrays above 100 rows. Bounded file-list metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw/rejected row metadata, URL/auth markers, prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub workflow-list row-bound slice: the workflow-list source-refresh parser now rejects exact `/repos/{owner}/{repo}/actions/workflows` payloads with more than 100 `workflows` rows. Empty and bounded workflow lists remain metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw workflow URLs, auth/query/fragment markers, prompts, renderer/source/html/script/API-auth fields, credentials, secret-looking fixtures, or rejected row metadata in public result/job/search output.

New GitHub check-runs row-bound slice: the check-runs source-refresh parser now rejects exact `/repos/{owner}/{repo}/commits/{sha}/check-runs` payloads with more than 100 `check_runs` rows. Bounded check-run metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw output, details/html/pull-request URLs, API-auth markers, raw prompts, renderer/script fields, credentials, or secret-looking fixtures in public result/job/search output.

New GitHub check-suites row-bound slice: the check-suites source-refresh parser now rejects exact `/repos/{owner}/{repo}/commits/{sha}/check-suites` payloads with more than 100 `check_suites` rows. Bounded check-suite metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw suite row fields, URLs, auth, prompts, renderer/source/html/script/API-auth fields, credentials, or secret-looking fixtures in public result/job/search/catalog output.

New GitHub repository-events row-bound slice: the repository-events source-refresh parser now rejects exact `/repos/{owner}/{repo}/events` JSON arrays above 100 rows. Bounded repository event metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw event row fields, payload/body, API URLs/query/fragment, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub issue-events row-bound slice: GitHub issue-events source-refresh now rejects exact `/repos/{owner}/{repo}/issues/{number}/events` arrays above 100 rows, no vault artifact, no public leakage of raw event row metadata, URL/auth markers, raw prompts, renderer/source/script/API-auth fields, credentials, or secret-looking fixtures.

New GitHub issue timeline row-bound slice: the issue timeline source-refresh parser now rejects exact `https://api.github.com/repos/{owner}/{repo}/issues/{number}/timeline` JSON arrays above 100 rows. Bounded timeline metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw/rejected timeline metadata, URL/auth markers, prompts, renderer/source/script fields, credentials, or secrets in public result/job/search output.

New source-refresh job-list advisory slice: `list_source_refresh_jobs()` and `GET /api/capy-memory/source/jobs` now return a top-level server-generated Memory Tree advisory/no-authority envelope in addition to the existing per-job metadata-only trust labels. Queue inspection remains advisory-only (`untrusted_advisory`, gate-bypass false, required prompt-preflight/approval/sandbox-preview/visual-QA/rollback gates), ignores caller/job-forged `trusted_system_memory` and bypass/context-authority fields, and continues to omit payload JSON, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, query/fragment secrets, and secret-looking fixtures.

New source-catalog advisory slice: `source_catalog()` and `GET /api/capy-memory/source/catalog` now return the same top-level server-generated Memory Tree advisory/no-authority envelope used by job-list/search/relevant-memory boundaries. Connector/source freshness inspection remains metadata-only advisory context, ignores caller/source/job-forged `trusted_system_memory`, context-authority, and bypass flags, and continues omitting raw prompts, raw context, renderer/source/html/script/API-auth fields, credentials, userinfo, query/fragment auth markers, and secret-looking fixture values from public catalog responses.

New GitHub check-run annotations row-bound slice: the check-run annotations source-refresh parser now rejects oversized JSON arrays above 100 rows. Exact bounded `/repos/{owner}/{repo}/check-runs/{check_run_id}/annotations` metadata refreshes still use the hidden canonical fetch route and public metadata label, while oversized annotation payloads fail closed as pending refresh failures with no vault artifact and no raw annotation messages, paths, raw details, API routes, query/fragment auth, raw prompts, or secret-looking fixtures in public receipt/job output.

New GitHub release-assets row-bound slice: the release-assets source-refresh parser now rejects exact `/repos/{owner}/{repo}/releases/{release_id}/assets` JSON arrays above 100 rows. Bounded release asset lists remain metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw asset row metadata, download/API URLs, query/fragment auth, raw prompts, renderer/source/html/script/API-auth fields, credentials, or secret-looking fixtures in public receipt/job/search output.

New GitHub release-reactions row-bound slice: the release-reactions source-refresh parser now rejects exact `https://api.github.com/repos/{owner}/{repo}/releases/{release_id}/reactions` JSON arrays above 100 rows. Bounded reaction lists remain metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw reaction row metadata, URLs, auth markers, prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub release-list row-bound slice: the release-list source-refresh parser now rejects exact `/repos/{owner}/{repo}/releases` JSON arrays above 100 rows. Bounded release lists remain metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw release bodies/URLs/assets, API-auth markers, raw prompts, renderer/source/html/script fields, credentials, secrets, or rejected row metadata in public result/job/search output.

New GitHub issue-list row-bound slice: the issue-list source-refresh parser now rejects exact `/repos/{owner}/{repo}/issues` JSON arrays above 100 rows. Bounded issue lists remain metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw issue bodies, URLs, auth markers, prompts, renderer/source/html/script/API-auth fields, credentials, or secret-looking fixtures in public job/search/receipt output.

New GitHub PR-list row-bound slice: the PR-list source-refresh parser now rejects exact `/repos/{owner}/{repo}/pulls` JSON arrays above 100 rows. Bounded PR lists remain metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw PR bodies, URLs, head/base refs, prompts, renderer/source/html/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public job/search/receipt output.

New GitHub PR requested-reviewers row-bound slice: the requested-reviewers parser now rejects exact `/repos/{owner}/{repo}/pulls/{number}/requested_reviewers` JSON payloads when either `users` or `teams` exceeds 100 rows, with no vault artifact and no raw reviewer/team metadata leakage.

New GitHub deployments row-bound slice: the deployments-list source-refresh parser now rejects oversized JSON arrays above 100 rows for exact `/repos/{owner}/{repo}/deployments` metadata refreshes. Empty and bounded lists remain metadata-only, while oversized deployment payloads fail closed as pending refresh failures with no vault artifact and no raw row details, descriptions, payloads, URLs, prompts, renderer/source/html/script/data/API-auth fields, credentials, or secret-looking fixtures in public receipt/job/search output.

New GitHub deployment-statuses row-bound slice: the deployment-statuses source-refresh parser now rejects oversized JSON arrays above 100 rows for exact `/repos/{owner}/{repo}/deployments/{deployment_id}/statuses` metadata refreshes. Bounded status lists remain metadata-only with hidden canonical fetch routes and public labels, while oversized payloads fail closed as pending refresh failures with no vault artifact and no status row metadata, descriptions, target/log/deployment URLs, payloads, raw prompts, renderer/script/API-auth fields, credentials, or secret-looking fixtures in public receipt/job/search output.

New GitHub workflow-run pending-deployments row-bound slice: the pending-deployments source-refresh parser now rejects exact `/repos/{owner}/{repo}/actions/runs/{run_id}/pending_deployments` JSON arrays above 100 rows. Bounded pending deployment lists remain metadata-only, while oversized payloads fail closed as pending refresh failures with no vault artifact and no raw deployment/environment/reviewer row metadata, URLs, prompts, renderer/source/script/API-auth fields, credentials, tokens, or secret-looking fixtures in public result/job/search output.

New GitHub environment deployment branch-policies row-bound slice: the deployment-branch-policies list parser now rejects exact `/repos/{owner}/{repo}/environments/{environment_name}/deployment-branch-policies` payloads with more than 100 `branch_policies` rows. Bounded policy-list metadata remains safe, while oversized payloads fail closed as pending refresh failures with no vault artifact and no rejected policy names/ids, URLs/auth markers, raw prompts, renderer/source/html/script/API-auth fields, credentials, or secret-looking fixtures in public catalog/job/search/result output.

New GitHub commit-statuses row-bound slice: the commit-statuses source-refresh parser now rejects oversized JSON arrays above 100 rows. Exact bounded `/repos/{owner}/{repo}/commits/{sha}/statuses` metadata refreshes still use the hidden canonical fetch route and public metadata label, while oversized status payloads fail closed as pending refresh failures with no vault artifact and no row contexts, raw API route/query/fragment markers, raw prompts, renderer/source/html/script/data/API-auth fields, tokens, or secret-looking fixtures in public receipt/job output.

New widget event inbox route receipt slice: the browser-facing `GET /api/spaces/widget/events` endpoint now returns the same metadata-only widget-runtime read envelope as `space.widget.events`: top-level required prompt-preflight, generated-widget-execution policy, `widget.events:<space_id>` progress, server-generated Memory Tree advisory/no-authority, output-compaction evidence, and sanitized event summaries. The widget manager renders a dedicated “Widget event inbox receipt” card above queued events while preserving legacy `{events}` compatibility and omitting caller/event-forged memory authority, raw prompts, renderer/source/html/script/data/API-auth fields, bearer/token values, generated widget bodies, and secret-looking fixtures.

New GitHub Actions cache usage-by-ref source-refresh slice: Memory Tree source refresh now supports exact `https://api.github.com/repos/{owner}/{repo}/actions/cache/usage-by-ref` JSON payloads as metadata-only records. Public source/catalog/search/vault output uses labels such as `github actions cache usage by ref {owner}/{repo}` while workers reconstruct only the hidden clean API route; persisted summaries include bounded Git refs, active cache counts, and byte totals, omitting cache keys, URLs, API-auth fields, prompts, renderer/source/html/script/data/body fields, tokens, cache-version material, and secret-looking fixtures. Text/JSON Feed fallbacks, unsafe refs, malformed counts, userinfo/port/lookalike hosts, route tails, and encoded suffixes fail closed before fetch or persistence.

New GitHub check-run annotations source-refresh slice: Memory Tree source refresh now supports exact `https://api.github.com/repos/{owner}/{repo}/check-runs/{check_run_id}/annotations` JSON array payloads as metadata-only records. Public source/catalog/search/vault output uses labels such as `github check run annotations {owner}/{repo} check run {id}` while due requeues preserve a hidden clean fetch origin; persisted summaries include only annotation counts and safe level/severity counts, omitting raw annotation paths, line numbers, messages, raw details, blob URLs, API-auth fields, prompts, renderer/source/html/script/data/body fields, tokens, and secret-looking fixtures.

New widget mutation advisory slice: source-style/current widget mutation helpers (`space.spaces.upsertWidget(s)`, `space.spaces.patchWidget`, `space.current.patchWidget`, `space.spaces.toggleWidgets`, `space.current.toggleWidgets`, widget delete/remove/delete-all aliases, generic `widget.patch` aliases, and direct `patch_widget(..., include_safety_receipts=True)`) now return server-generated Memory Tree advisory/no-authority envelopes beside prompt-preflight where applicable, autonomy-policy, structured progress, and output-compaction receipts. The compaction receipt preserves required Memory Tree gates before high-volume revision ids, while caller-forged trusted-memory authority, raw prompts, renderer/source/html/script/data/API-auth fields, bearer/token values, and secret-looking fixtures stay omitted.

New manual/scheduled source-refresh advisory slice: `POST /api/capy-memory/source/refresh` and `/api/capy-memory/source/refresh/scheduled` now return server-generated Memory Tree advisory/no-authority receipts beside existing prompt-preflight, autonomy-policy, structured progress, and output-compaction evidence. The product-home Source refresh result card renders the fixed advisory boundary and fixed required-gate list, so refreshed Memory Tree/source context remains metadata-only and cannot let caller/job/result-forged `trusted_system_memory`, bypass flags, raw memory context, origin URLs, renderer/source/html/script/API-auth fields, credentials, or secret-looking fixtures understate or bypass safety gates.

New recovery-module repair event-list advisory slice: recovery-module repair event-list tool responses now return a top-level server-generated Memory Tree advisory/no-authority receipt beside prompt-preflight, autonomy-policy, structured progress, and output-compaction evidence. The event-list compaction text includes the fixed advisory boundary (`untrusted_advisory`, gate-bypass false, prompt-preflight/approval/sandbox-preview/visual-QA/rollback gates), so reading recovery-module repair history cannot treat caller/event-forged memory authority or raw operator-note payloads as trusted context.

New whole-Space repair advisory slice: whole-Space repair queue responses and repair-event list tool aliases now return server-generated Memory Tree advisory/no-authority receipts beside prompt-preflight, autonomy-policy, structured progress, and output-compaction evidence. Queued repair events persist and replay the stored advisory envelope without regenerating legacy rows, while caller-forged trusted-memory authority, benign/raw memory-context payload fields, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, and secret-looking fixtures stay out of immediate and listed receipts.

New recovery-module repair advisory slice: `space.admin.recovery.repair_module` now returns and persists a server-generated Memory Tree advisory/no-authority receipt beside the existing repair prompt-preflight, autonomy-policy, structured progress, and output-compaction evidence. Listed recovery-module repair events replay only sanitized stored advisory metadata, while forged memory-authority fields and raw operator notes in prompt or payload summaries are omitted so recovery-module repair context cannot bypass prompt-preflight, approval, sandbox preview, visual QA, or rollback/recovery gates.

New session recovery advisory slice: `repair_safe_session_recovery(...)` now returns a server-generated Memory Tree advisory/no-authority receipt beside existing prompt-preflight, autonomy-policy, progress, and output-compaction evidence. The repair-safe compaction text threads only allow-listed advisory metadata (`untrusted_advisory`, gate-bypass false, required prompt-preflight/approval/sandbox-preview/visual-QA/rollback gates) so recovered session context remains metadata-only and cannot leak local paths, session messages, raw prompts, renderer/source/html/script/API-auth fields, credentials, or secret-looking fixtures.

New active-space current-context advisory slice: `space.current.context` / `space.context` / `space.current.prompt_context` now return the shared server-generated Memory Tree advisory/no-authority receipt. The active-space path exposes that receipt beside prompt-preflight, autonomy-policy, context status, structured progress, and output-compaction evidence; the no-current fallback keeps the advisory receipt, context status, and compaction evidence without inventing action/progress telemetry. Both paths thread only allow-listed advisory metadata into compaction (`untrusted_advisory`, gate-bypass false, required prompt-preflight/approval/sandbox-preview/visual-QA/rollback gates), so current Space context cannot treat Memory Tree snippets or caller-forged `trusted_system_memory` / bypass flags as trusted instructions and cannot leak renderer/source/html/script/API-auth fields, raw prompts, credentials, or secret-looking fixtures.

New source-refresh queue trust-label slice: `GET /api/capy-memory/source/jobs` now returns fixed per-job metadata-only/advisory/no-authority fields (`metadata_only`, `public_output`, `advisory_context`, `context_authority`, and `can_bypass_safety_gates`) generated by the server rather than job payloads. The product-home Source refresh queue renders safe `metadata-only` and `advisory only` labels for each queued job while continuing to omit raw origin/payload/error values, prompts, renderer/source/html/script/API-auth fields, credentials, and secret-looking fixtures.

New sitemap URLSet source-refresh slice: Memory Tree source refresh now supports XML sitemap `<urlset>` payloads from allowlisted HTTPS origins as metadata-only source summaries. The parser persists only safe URL counts, bounded host/path-only entries, optional safe `lastmod` dates, and sanitized origin provenance; credentials, query/fragment markers, auth-like query parameters, raw XML bodies, renderer/source/html/script/API-auth fields, raw prompts, and secret-looking fixture values are omitted, and sitemap payloads with zero safe `<loc>` entries fail closed without writing vault artifacts.

New source registration trust-label slice: `register_source_reference(...)` and `POST /api/capy-memory/source/register` now return fixed server-generated metadata-only/advisory/no-authority fields (`metadata_only`, `public_output`, `advisory_context`, `context_authority`, and `can_bypass_safety_gates`) alongside the existing sanitized origin/job receipt. Caller-supplied trust labels, userinfo/query/fragment auth markers, raw prompts, renderer/source/html/script/data/API-auth fields, credentials, and secret-looking fixtures remain omitted from the registration receipt and queued source-refresh job surfaces.

New GitHub environment deployment branch policy source-refresh slice: Memory Tree source refresh now supports exact `https://api.github.com/repos/{owner}/{repo}/environments/{environment_name}/deployment-branch-policies/{branch_policy_id}` JSON object payloads. Dirty registrations strip userinfo/query/fragment into a hidden clean fetch origin while public receipts use metadata labels such as `github environment deployment branch policy {owner}/{repo} {environment} #{id}`; persisted/search/vault output is limited to safe repository path, decoded environment name, policy id, policy name, and allow-listed `branch`/`tag` type while text/JSON Feed fallbacks, malformed ids/tails/hosts/ports, mismatches, unsafe names/types, URLs, prompts, scripts, API-auth fields, tokens, and secret-like fixtures fail closed.

New GitHub Actions repository permissions source-refresh slice: Memory Tree source refresh now has a narrow JSON-only parser for exact `https://api.github.com/repos/{owner}/{repo}/actions/permissions` payloads. It persists only safe repository path, `enabled` boolean, and allow-listed `allowed_actions` enum (`all`, `local_only`, `selected`) while omitting raw URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, tokens, and secrets. Text/JSON Feed fallbacks, malformed routes, selected-actions lookalikes, encoded subroutes, lookalike hosts, userinfo, and explicit ports fail closed before fetch, persistence, or public output.

New GitHub Actions selected-actions public-output hardening slice: exact `https://api.github.com/repos/{owner}/{repo}/actions/permissions/selected-actions` registrations, legacy source/job rows, and public memory hits now render only metadata labels such as `github actions selected actions {owner}/{repo}` in receipts, source catalog, source-refresh jobs, and search snippets while preserving the hidden canonical fetch origin for JSON-only workers. Raw route origins, route-shaped or credential-bearing source ids, snippets whose raw route appears beyond the normal truncation boundary, query/fragment auth, raw prompts, renderer/source/html/script/data/API-auth fields, bearer/token/secret fixtures, unsafe patterns, lookalike hosts, explicit ports, and encoded/suffixed selected-actions variants fail closed or scrub to neutral metadata-only labels without changing non-selected public-hit source-id behavior. Final response URLs must now equal the exact sanitized selected-actions endpoint, so query/fragment auth or raw-prompt drift fails closed before any response-body read, JSON parsing, vault persistence, catalog/search output, or Spaces relevant-memory surfacing.

New GitHub repository security-advisories source-refresh hardening slice: exact `https://api.github.com/repos/{owner}/{repo}/security-advisories` registrations now publish only labels like `github security advisories {owner}/{repo}` in receipts, source catalog, and source-refresh job lists while preserving hidden clean canonical fetch origins plus `github_security_advisories` kind for JSON-only workers. Dirty query/fragment/userinfo/raw-prompt/API-key markers stay out of public control-plane output, and due requeue preserves the hidden fetch origin from safe metadata without exposing raw API routes or advisory body/detail fields.

New GitHub code-scanning alerts list public-origin hardening slice: exact `https://api.github.com/repos/{owner}/{repo}/code-scanning/alerts` registrations now publish only labels like `github security scanning alerts {owner}/{repo}` in receipts, source catalog, source-refresh jobs, search, and vault output while preserving a hidden clean canonical fetch origin plus `github_code_scanning_alerts` kind for workers. Dirty query/fragment registrations strip auth/raw-prompt markers before queueing; runs fetch only the clean API route with `Accept: application/json`; malformed/lookalike/userinfo/port/uppercase/encoded/extra-tail routes continue to fail closed without exposing raw API routes, renderer/source/html/script/data/API-auth fields, tokens, or secret-looking fixtures.

New GitHub single Dependabot alert source-refresh slice: Memory Tree source refresh now supports exact `https://api.github.com/repos/{owner}/{repo}/dependabot/alerts/{alert_number}` JSON object payloads as metadata-only records. Dirty registrations and due requeues preserve a hidden clean canonical API fetch origin while public labels stay `github dependabot alert {owner}/{repo} #{alert_number}`; persisted/search/vault output shows only alert number, allow-listed state, dependency package ecosystem/name/manifest path, and allow-listed severity. Ignored advisory bodies/details, CVE/GHSA ids, version ranges, and raw URLs from realistic GitHub alert objects are omitted from persisted/search/vault output, while JSON Feed/generic JSON bypasses, payload number mismatches, route URL mismatches, malformed/lookalike/userinfo/port/uppercase/encoded/extra-tail routes, and hostile prompt/script/secret/API-auth markers fail closed. Legacy due-refresh jobs with unsafe raw Dependabot alert candidates in payload or source rows now fail closed to local `capy-memory://...` labels without preserving hidden fetch origins.

New GitHub Dependabot alerts list registration/final-URL drift slice: exact `https://api.github.com/repos/{owner}/{repo}/dependabot/alerts` registrations, including benign query/fragment noise, now publish only labels like `github dependabot alerts {owner}/{repo}` while preserving the hidden clean canonical fetch origin and `github_dependabot_alerts` kind in job payloads. Refresh still fetches the clean API URL with `Accept: application/json`, now rejects response final URLs with query/fragment/userinfo/auth drift before body reads, vault persistence, search indexing, or Spaces relevant-memory output, and public receipt/search/catalog/result/vault/relevant-memory output omits raw API URLs, auth/query/fragment markers, alert body sentinels, advisory bodies/details, CVE/GHSA ids, renderer/source/html/script fields, raw prompts, and secret-looking fixture values.

New GitHub code-scanning default-setup source-refresh slice: Memory Tree source refresh now supports exact `https://api.github.com/repos/{owner}/{repo}/code-scanning/default-setup` JSON object payloads as metadata-only records. Dirty registrations and due requeues use a hidden canonical fetch origin while public labels stay `github code scanning default setup {owner}/{repo}`; persisted/search/catalog/job/vault output shows only repository path, setup state/status, bounded languages, query suite, schedule, runner type/label, threat model, and safe updated timestamp, and fail-closes JSON Feed/text fallbacks, unsafe ignored fields, malformed/lookalike/userinfo/port/uppercase/encoded/extra-tail routes, and final-URL drift without raw URLs, auth/query/fragment markers, prompts, scripts, API-auth fields, tokens, or secret-like values.

New GitHub secret-scanning scan-history due-requeue slice: legacy/public-only source-refresh jobs using the metadata label `github secret scanning scan history {owner}/{repo}` now restore the hidden clean canonical fetch origin `https://api.github.com/repos/{owner}/{repo}/secret-scanning/scan-history` when queued by freshness automation, while source catalog and job-list public output keep the safe label and omit raw API URLs, auth/query/fragment markers, raw prompts, and secret-looking fixtures.

New GitHub workflow-run artifacts source-refresh slice: Memory Tree source refresh now supports exact `https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts` JSON payloads as metadata-only records. It strips query/fragment from fetch/public origins, requests `Accept: application/json`, persists only safe run id, artifact count, and bounded artifact ids/names/sizes/expired/timestamps, and fail-closes JSON Feed/text fallbacks, userinfo/ports/lookalike hosts, padded/nonpositive run ids, encoded/extra path tails, unsafe artifact names, raw URLs/API-auth fields/prompts/scripts/tokens, and redirect/final-URL drift before fetch, catalog, job, search, or vault output.

New GitHub code-scanning analyses source-refresh slice: Memory Tree source refresh now supports exact `https://api.github.com/repos/{owner}/{repo}/code-scanning/analyses` JSON list payloads as metadata-only records. It fetches only the hidden clean canonical API route, stores public labels such as `github code scanning analyses capy/spaces`, persists only bounded analysis count plus up to five safe id/ref/category/analysis-key/commit-prefix/result-count/rule-count/tool/timestamp/deletable fields, and fail-closes JSON Feed/text fallbacks, malformed/lookalike/userinfo/port/encoded/extra-tail routes, unsafe nested keys/values, raw URLs, SARIF IDs, API-auth fields, prompts, tokens, and secret-looking fixtures before fetch, queueing, catalog, search, or vault output.

New GitHub code-scanning single-analysis source-refresh slice: Memory Tree source refresh now supports exact `https://api.github.com/repos/{owner}/{repo}/code-scanning/analyses/{analysis_id}` JSON object payloads as metadata-only records. Dirty registrations and due requeues preserve a hidden clean canonical fetch origin while public receipts use labels such as `github code scanning analysis capy/spaces #101`; persisted summaries include only safe analysis id, ref, category, analysis key, commit prefix, result/rule counts, tool name/version, timestamp, and deletable metadata, while validation-only URL/SARIF/environment/error/warning/tool GUID fields are checked but never rendered. Query/fragment fetch origins plus response final-URL auth/query/fragment, repository drift, analysis-id drift, and tail-route drift now fail closed before any body read; text/JSON Feed fallbacks, mismatched ids/URLs, non-empty errors/warnings, unsafe environment or nested tool fields, userinfo/ports/lookalikes, encoded/extra-tail routes, prompts, renderer/source/html/script/data/API-auth fields, tokens, and secret-looking fixtures fail closed before fetch, queueing, catalog, search, or vault output.

New GitHub code-scanning alert instances source-refresh slice: Memory Tree source refresh now supports exact `https://api.github.com/repos/{owner}/{repo}/code-scanning/alerts/{alert_number}/instances` JSON list payloads as metadata-only records. It fetches and requeues only the hidden clean canonical API route, stores public labels such as `github code scanning alert instances capy/spaces #17`, persists only bounded instance count plus safe ref/state/category/analysis/line/commit-prefix metadata, and fail-closes JSON Feed/text fallbacks, malformed or unsafe rows, raw message/path/source/body/data/html/script/API-auth fields, query/fragment/userinfo/port/lookalike/encoded/extra-tail origins, raw prompts, tokens, and secret-looking fixtures before fetch, queueing, catalog, search, or vault output.

New GitHub single code-scanning alert source-refresh slice: Memory Tree source refresh now supports exact `https://api.github.com/repos/{owner}/{repo}/code-scanning/alerts/{alert_number}` JSON object payloads as metadata-only records. It fetches and requeues only the hidden clean canonical API route, stores public labels such as `github code scanning alert capy/spaces #17`, requires payload number plus route-bearing `url`/`html_url`/`instances_url` fields to match the requested repo/alert exactly, persists only bounded alert state/rule/severity/tool/timestamp metadata, and fail-closes JSON Feed/text fallbacks, unsafe ignored fields, raw prompt/script/API-auth markers, mismatched payload routes, query/fragment final-URL drift, userinfo/ports/lookalikes, encoded/extra-tail origins, and invalid alert numbers before fetch, queueing, catalog, search, or vault output.

New GitHub single secret-scanning alert source-refresh slice: Memory Tree source refresh now supports exact `https://api.github.com/repos/{owner}/{repo}/secret-scanning/alerts/{alert_number}` JSON object payloads as metadata-only records. It fetches the hidden clean canonical API route with `hide_secret=true`, stores public labels such as `github secret scanning alert capy/spaces #37`, persists only safe bounded alert metadata, and fail-closes JSON Feed/text fallbacks, non-empty `secret`, raw URLs/comments, API-auth markers, raw prompts, scripts, query/fragment/userinfo/port/encoded/extra-tail legacy origins, and invalid alert numbers before fetch, queueing, catalog, search, or vault output.

New GitHub secret-scanning alert locations source-refresh slice: Memory Tree source refresh now has a narrow JSON-only parser for exact `https://api.github.com/repos/{owner}/{repo}/secret-scanning/alerts/{alert_number}/locations` payloads. It fetches only the clean canonical API route with `hide_secret=true`, persists only safe repository path, alert number, bounded location count, location type, and safe path/line metadata behind a public label such as `github secret scanning alert locations capy/spaces #31`, and accepts documented safe body/review/discussion URL detail fields without rendering raw URLs/fragments. JSON Feed/text fallbacks, raw queued query/fragment/userinfo/port/encoded/extra-tail origins, raw body/html fields, renderer/source/data/API-auth markers, raw prompts, scripts, tokens, and secret-looking fixtures fail closed before fetch, queueing, catalog, search, or vault output.

New GitHub secret-scanning scan-history source-refresh slice: Memory Tree source refresh now supports exact `https://api.github.com/repos/{owner}/{repo}/secret-scanning/scan-history` JSON object payloads as metadata-only records. It fetches the clean canonical API route without alert-specific `hide_secret=true`, stores public labels such as `github secret scanning scan history capy/spaces`, persists only bounded scan-category counts plus safe whitelisted scan type/status/timestamp and custom-pattern scope/name metadata, and fail-closes JSON Feed/text fallbacks, missing/unknown scan types, malformed rows, raw API URLs, API-auth markers, raw prompts, renderer/source/html/script/data fields, token/secret-like pattern names, query/fragment/userinfo/port/encoded/extra-tail origins, and lookalike hosts before fetch, queueing, catalog, search, or vault output.

New GitHub repository autolinks/deploy keys/private vulnerability reporting source-refresh slice: Memory Tree source refresh already implements metadata-only coverage for exact GitHub repository autolinks, deploy keys, and private vulnerability reporting API payloads, exposing only public labels (`github autolinks {owner}/{repo}`, `github deploy keys {owner}/{repo}`, and `github private vulnerability reporting {owner}/{repo}`). It persists only autolink count/ids/key prefixes/alphanumeric flags, deploy key count/ids/safe titles/read-only and verified flags/safe created timestamps, and the private vulnerability reporting enabled boolean while omitting or failing closed on URL templates/raw URLs, key material, API-auth/query/fragment markers, raw prompts, renderer/source/html/script/data fields, tokens, secret-like fixtures, text/feed bypasses, malformed tails, and userinfo/ports/lookalikes/final-URL drift where applicable.

New GitHub workflow-scoped workflow-runs source-refresh slice: Memory Tree source refresh now has a narrow JSON-only parser for exact `https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs` payloads. It persists only safe repository path, workflow id, run count, and bounded run ids/names/statuses/conclusions/events/run numbers/attempts/branches/SHA prefixes/timestamps behind a metadata-only public label such as `github workflow runs {owner}/{repo} workflow {workflow_id}` while omitting logs/jobs/html URLs, workflow YAML/path/body fields, commit payloads, API-auth fields, raw prompts, renderer/source/html/script/data fields, tokens, and secret-looking fixtures. JSON Feed/text fallbacks, malformed or mismatched rows, missing/mismatched scoped workflow ids, lookalike/userinfo/port authorities, case-mismatched routes, suffixed/encoded runs tails, final-URL drift, and public raw API origin persistence fail closed before fetch, queueing, catalog, search, or vault output.

New GitHub commit pull-request source-refresh slice: Memory Tree source refresh now has a narrow JSON-only parser for exact `https://api.github.com/repos/{owner}/{repo}/commits/{40-hex-sha}/pulls` payloads. It persists only safe repository path, commit SHA prefix, pull-request count, and bounded PR numbers/titles/states/draft flags/authors/timestamps behind a metadata-only public label such as `github commit pull requests {owner}/{repo} {sha12}` while omitting PR bodies, URLs, head/base refs, API-auth fields, raw prompts, renderer/source/html/script/data fields, tokens, and secret-looking fixtures. JSON Feed/text fallbacks, malformed rows, userinfo/port/lookalike authorities, suffixed/encoded pulls tails, response final-URL drift before body read, due-refresh requeue drift, and legacy raw source/job/catalog origins fail closed or scrub to safe labels before fetch, queueing, catalog, search, relevant-memory, or vault output.

New GitHub repository topics public-origin hardening slice: canonical topics registrations now show metadata-only public labels such as `github repository topics {owner}/{repo}` in receipts, source catalog, source-refresh jobs, and vault records while preserving the hidden canonical fetch origin for exact `https://api.github.com/repos/{owner}/{repo}/topics` JSON requests. Uppercase, whitespace-wrapped uppercase, userinfo, explicit-port, suffixed-host, extra-tail, encoded-tail, and redirect/final-URL drift variants fail closed before fetch or persistence, keep `_refresh_open` uncalled where appropriate, write no vault artifact, and prevent raw topics API URLs, auth query markers, raw prompts, renderer/source/html/script/data/API-auth fields, bearer markers, and secret-looking fixtures from leaking while repositories merely named `topics` remain valid non-topics routes.

New legacy GitHub repository invitations public-origin hardening slice: stale Memory Tree source/job rows with uppercase or whitespace-wrapped uppercase `API.GITHUB.COM` repository-invitations origins now fail closed to local `capy-memory://...` labels in source catalog and source-refresh job public surfaces, never call `_refresh_open`, write no vault artifact, and prevent raw invitations API routes, access-token query markers, raw/system prompt fragments, renderer/source/html/script/data/body/API-auth fields, bearer markers, and secret-looking fixture values from leaking while preserving exact lowercase invitations ingestion plus valid GitHub contents paths whose filenames or repository names contain `invitations`.

New legacy GitHub teams public-origin hardening slice: stale Memory Tree source/job rows with uppercase or whitespace-wrapped uppercase `API.GITHUB.COM` teams origins now fail closed to local `capy-memory://...` labels in source catalog and source-refresh job public surfaces, preventing normalized raw Teams API routes, access-token query markers, raw-prompt fragments, hostile prompt text, API-key markers, scripts, and secret-looking fixture values from leaking while preserving exact lowercase teams ingestion.

New legacy GitHub collaborators public-origin hardening slice: stale Memory Tree source/job rows with uppercase or whitespace-wrapped uppercase `API.GITHUB.COM` collaborator origins now fail closed to local `capy-memory://...` labels in source catalog and source-refresh job public surfaces, never call `_refresh_open`, and prevent raw GitHub collaborator routes, auth query markers, raw-prompt fragments, hostile prompt text, API-key markers, scripts, and secret-looking fixtures from leaking while preserving exact lowercase collaborators ingestion.

New legacy GitHub milestones public-origin hardening slice: stale Memory Tree source/job rows with uppercase or whitespace-wrapped uppercase `API.GITHUB.COM` milestone origins now fail closed to local `capy-memory://...` labels in source catalog and source-refresh job public surfaces, never call `_refresh_open`, and prevent raw GitHub milestone routes, auth query markers, raw-prompt fragments, hostile prompt text, API-key markers, scripts, and secret-looking fixtures from leaking while preserving exact lowercase milestones ingestion, external milestone-shaped URLs, and GitHub contents paths such as `milestones%20plan.md`.

New GitHub assignees raw-host hardening slice: Memory Tree source refresh now rejects uppercase and whitespace-wrapped uppercase `API.GITHUB.COM` assignee origins before safe-origin normalization can turn them into eligible lowercase `https://api.github.com/repos/{owner}/{repo}/assignees` URLs. The regression keeps `_refresh_open` uncalled, stores registration, source-refresh job, and catalog public origins as local `capy-memory://...` labels, leaves unsafe jobs safely pending/failed, and prevents synthetic assignee logins, auth query markers, raw prompts, and secret-looking values from reaching serialized public output or vault files while preserving exact lowercase assignee ingestion.

New GitHub subscribers raw-host hardening slice: Memory Tree source refresh now rejects uppercase and whitespace-wrapped uppercase `API.GITHUB.COM` subscriber/watchers origins before registration or fetch normalization can turn them into eligible lowercase `https://api.github.com/repos/{owner}/{repo}/subscribers` URLs. The regression keeps `_refresh_open` uncalled, stores public origins as local `capy-memory://...` labels, leaves the jobs safely pending/failed, and prevents synthetic subscriber fixture logins, auth query markers, raw prompts, and secret-looking values from reaching vault/search/job output while preserving exact lowercase subscriber ingestion.

New GitHub stargazer raw-host hardening slice: Memory Tree source refresh now rejects uppercase and whitespace-wrapped uppercase `API.GITHUB.COM` stargazer-list origins before registration or fetch normalization can turn them into eligible lowercase `https://api.github.com/repos/{owner}/{repo}/stargazers` URLs. The regression keeps `_refresh_open` uncalled, stores public origins as local `capy-memory://...` labels, leaves the jobs safely pending/failed, and prevents synthetic stargazer fixture logins, auth query markers, raw prompts, and secret-looking values from reaching vault/search/job output.

New legacy GitHub forks public-origin hardening slice: stale Memory Tree source/job rows with uppercase `API.GITHUB.COM` forks origins now fail closed to local `capy-memory://...` labels in source catalog and source-refresh job public surfaces, preventing raw route, auth query, and raw-prompt fragment leakage while preserving exact lowercase forks metadata ingestion.

New GitHub CODEOWNERS errors public-output redaction slice: Legacy Memory Tree source/job rows that still contain raw `/repos/{owner}/{repo}/codeowners/errors` API origins now render public catalog and source-refresh job-list origins as `github codeowners errors {owner}/{repo}` for strict canonical routes, and fail closed to local `capy-memory://...` labels for lookalike/userinfo/malformed or non-string route payloads. This prevents stale queued metadata from leaking raw GitHub URLs, route paths, query/fragment auth markers, raw prompts, or secret-looking values into product freshness/job cards.

New GitHub CODEOWNERS errors source-refresh slice: Memory Tree source refresh now has a narrow JSON-only parser for exact `https://api.github.com/repos/{owner}/{repo}/codeowners/errors` payloads, accepting only canonical `api.github.com` authority and exact CODEOWNERS errors routes. It persists only the repository path, total error count, and up to five public line/optional-column/kind summaries while omitting raw CODEOWNERS source lines, suggestions, messages, URLs, API-auth fields, raw prompts, renderer/source/html/script/data fields, tokens, and secret-looking fixture values. JSON Feed/XML/text fallbacks, lookalike hosts, userinfo/ports, malformed tails, encoded suffixes, and redirect/final-URL route drift fail closed before unsafe fetch or persistence.

New GitHub Actions runner-groups source-refresh slice: Memory Tree source refresh now has a narrow JSON-only parser for exact `https://api.github.com/repos/{owner}/{repo}/actions/runner-groups` payloads, accepting only canonical API authority and exact runner-groups routes. It persists only repository path, bounded runner-group count, ids, names, allow-listed visibility, boolean policy flags, and safe selected workflow paths while omitting raw URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, tokens, and secrets. JSON Feed/text fallbacks, over-limit or malformed rows, unsafe names/workflows, non-boolean policy fields, lookalike hosts, userinfo, explicit ports, malformed tails, encoded suffixes, route-like raw `source_id`/display names, and legacy fallback catalog/jobs/queue/search leaks fail closed or scrub to neutral metadata-only labels before fetch, persistence, or public output.

New GitHub Actions runner-downloads source-refresh slice: Memory Tree source refresh now has a narrow JSON-only parser for exact `https://api.github.com/repos/{owner}/{repo}/actions/runners/downloads` payloads, accepting only canonical API authority and exact downloads routes. It persists only repository path, bounded runner application count, operating system, architecture, and filename while omitting raw download URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, tokens, and secrets. JSON Feed/text fallbacks, unsafe payload fields, lookalike hosts, userinfo, explicit ports, uppercase authorities, padded or unsafe path segments, suffixed/encoded downloads routes, and extra tails fail closed before unsafe fetch or persistence, and the broader self-hosted runners parser no longer swallows the downloads subroute.

New GitHub Actions organization runner-downloads source-refresh slice: Memory Tree source refresh now has a narrow JSON-only parser for exact `GET https://api.github.com/orgs/{org}/actions/runners/downloads` payloads, accepting only canonical API authority and exact organization downloads routes. It reconstructs fetch routes only from safe public labels such as `github actions organization runner downloads {org}` (for example `github actions organization runner downloads capy`) and persists only metadata-only organization name, bounded runner application count, operating system, architecture, and filename while omitting raw API/download URLs, API-auth fields, prompts/raw prompts, scripts, renderer/source/data/html fields, tokens, credentials, and secret-looking values from source catalog, jobs, search, and vault output. Lookalike hosts, userinfo, explicit ports, uppercase authority drift, unsafe or padded org segments, suffixes/extra tails, encoded suffixes, JSON Feed/text fallbacks, unsafe payload fields, malformed rows/count drift, final-URL drift, and legacy raw public-output leaks fail closed before unsafe fetch or persistence.

New GitHub Actions runner-downloads route hardening slice: the strict downloads matcher now rejects malformed/lookalike, userinfo, explicit-port, suffixed, tail, and encoded near-miss routes while the exact route plus public-origin/fetch-origin round trip remain covered for metadata-only source refresh.

New GitHub Actions single repository secret source-refresh slice: Memory Tree source refresh now has narrow JSON-only support for exact `https://api.github.com/repos/{owner}/{repo}/actions/secrets/{secret_name}` payloads. It fetches only the clean canonical URL, persists only safe repository path, uppercase/underscore private-name label, and safe created/updated timestamps, and fail-closes unsafe names, encoded slashes/suffixes, explicit ports, and lookalike hosts before fetch while omitting raw URLs, values, key material, API-auth markers, prompts, renderer/source/data/html/script/body fields, tokens, and secret-looking fixtures.

New GitHub Actions workflow attempt jobs source-refresh slice: Memory Tree source refresh now has a narrow JSON-only parser for exact `https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt_number}/jobs` payloads, accepting only canonical API authority and positive unpadded run/attempt numbers. It persists only run id, attempt number, total count, and bounded job names/status/conclusion/timestamps while omitting raw URLs, API-auth fields, prompts, scripts, secrets, steps, runner labels, and runner names.

New GitHub teams source-refresh slice: Memory Tree source refresh now has a narrow JSON-only parser for exact `https://api.github.com/repos/{owner}/{repo}/teams` payloads. It persists only repository path, team count, and bounded team names/slugs/ids/privacy/permission, maps GitHub `secret` privacy to public `private`, and omits descriptions, raw URLs, access tokens, prompts, scripts, API keys, and secrets.

New GitHub environment single-variable source-refresh slice: Memory Tree source refresh now has a narrow JSON-only parser for exact `/repos/{owner}/{repo}/environments/{environment_name}/variables/{variable_name}` API payloads, canonicalizes dirty registered origins to `https://api.github.com/repos/{owner}/{repo}/environments/{environment_name}/variables/{variable_name}`, and persists only repository path, environment name, variable name, and safe created/updated timestamps while omitting variable values, URLs, renderer/source/html/script/API-auth fields, raw prompts, tokens, and secret-looking fixtures. JSON Feed/text bypasses, malformed or encoded route tails, lookalike hosts, and final-URL route drift fail closed before unsafe fetch or persistence.

New GitHub single pull-request source-refresh slice: Memory Tree source refresh now has a narrow JSON-only parser for exact `/repos/{owner}/{repo}/pulls/{number}` API payloads, canonicalizes fetch origins to `https://api.github.com/repos/{owner}/{repo}/pulls/{number}`, and reconstructs only metadata-only PR title/state/draft/merged/author/timestamp/count/branch-ref summaries while omitting PR bodies, URLs, renderer/source/html/script/data/API-auth fields, raw prompts, tokens, query/fragment markers, URL-like branch refs, and secret-looking fixtures. Malformed, singular, suffixed, encoded, mixed-case, invalid owner/repo, JSON Feed, and text-fallback lookalikes fail closed before unsafe fetch or persistence while adjacent PR list/subresource routes remain intact.

New GitHub environment public-key source-refresh slice: Memory Tree source refresh now has a narrow JSON-only parser for exact `/repos/{owner}/{repo}/environments/{environment_name}/secrets/public-key` API payloads, reconstructing only metadata-only repository path, environment name, and public key id summaries while omitting raw public-key material, URLs, renderer/source/html/script/data/API-auth fields, raw prompts, tokens, query/fragment/userinfo markers, and secret-looking fixtures. Text/JSON Feed fallbacks, unsafe key ids, malformed or encoded public-key tails, lookalike hosts, explicit ports, and redirect route drift fail closed before unsafe fetch or persistence, and environment names with spaces or slashes are safely path-encoded when alias-derived refresh jobs reconstruct hidden fetch origins.

New GitHub environment deployment-branch-policies source-refresh slice: Memory Tree source refresh now has a narrow JSON-only parser for exact `/repos/{owner}/{repo}/environments/{environment_name}/deployment-branch-policies` API payloads, reconstructing only metadata-only repository path, environment name, policy count, and bounded safe policy id/name/type summaries while omitting branch-policy URLs, node IDs, renderer/source/html/script/data/API-auth fields, raw prompts, tokens, query/fragment/userinfo markers, and secret-looking fixtures. Text/JSON Feed fallbacks, unsafe policy names, malformed or encoded route tails, lookalike hosts, explicit ports, raw slash tails, and route drift fail closed before unsafe fetch or persistence.

New source-refresh queue UI slice: Capy Spaces product home now fetches `api/capy-memory/source/jobs?limit=5` and renders a styled, metadata-only “Source refresh queue” card beside Memory freshness/Connector catalog, showing only bounded source id/status/attempt/timestamp rows while omitting origin URIs, raw queued payloads, error bodies, renderer/source/html/script fields, raw prompts, API-auth fields, and secret-looking fixture values.

New source-refresh job-list safety slice: `GET /api/capy-memory/source/jobs` now returns bounded metadata-only source-refresh job receipts with explicit `metadata_only: true`, sanitized public `origin_uri`, status/attempt/timestamp fields, and no raw queued payloads, query/fragment auth markers, renderer/source/html/script fields, raw prompts, or secret-looking fixture values. This makes pending source-refresh work inspectable by UI/autonomous operators without turning job listings into a content or credential leak.

New widget blueprint/render advisory slice: source-style widget definition/blueprint/source/render helpers (`space.spaces.defineWidget`, `space.spaces.createWidgetSource`, `space.spaces.previewWidgetRecord`, and `space.spaces.renderWidget`) now include the shared server-generated Memory Tree advisory/no-authority envelope beside widget-runtime prompt-preflight, autonomy-policy, structured progress, and metadata-only output-compaction receipts. Caller-forged memory authority, raw generated widget bodies, renderer/source/html/API-auth fields, scripts, tokens, and secret-looking fixtures remain absent while blueprint/render context stays untrusted advisory metadata that cannot bypass prompt-preflight, approval, sandbox preview, visual QA, or rollback/recovery gates.

New checkpoint advisory slice: Space checkpoint rollback anchors now include the shared server-generated Memory Tree advisory/no-authority envelope beside recovery prompt-preflight, autonomy-policy, `checkpoint:<space_id>` structured progress, and metadata-only output-compaction receipts, and the Space detail checkpoint result card renders that advisory boundary. Caller-forged memory authority, raw memory context, hostile checkpoint reasons, renderer/source/API-auth fields, scripts, tokens, and secret-looking fixtures remain absent while checkpoint context stays untrusted advisory metadata that cannot bypass prompt-preflight, approval, sandbox preview, visual QA, or rollback/recovery gates.

New duplicate/clone advisory slice: source-style Space duplicate/clone helper responses (`space.spaces.duplicateSpace` / `space.spaces.cloneSpace`) now include the shared server-generated Memory Tree advisory/no-authority envelope beside active-instruction prompt-preflight, autonomy-policy, `space.duplicate:<space_id>` structured progress, and metadata-only output-compaction receipts. Caller-forged memory authority, raw memory context, renderer/source/API-auth fields, scripts, tokens, and secret-looking fixtures remain absent while copied Space context stays untrusted advisory metadata that cannot bypass prompt-preflight, approval, sandbox preview, visual QA, or rollback/recovery gates.

New save-meta/layout advisory slice: source-style Space metadata/layout mutation helpers (`space.spaces.saveSpaceMeta` / `space.current.saveMeta` and `space.spaces.saveSpaceLayout` / `space.current.saveLayout`) now include the shared server-generated Memory Tree advisory/no-authority envelope beside prompt-preflight, autonomy-policy, `save-meta:<space_id>` / `save-layout:<space_id>` structured progress, and metadata-only output-compaction receipts. Caller-forged memory authority, raw memory context, renderer/source/API-auth fields, scripts, tokens, and secret-looking fixtures remain absent while Space mutation context stays untrusted advisory metadata that cannot bypass prompt-preflight, approval, sandbox preview, visual QA, or rollback/recovery gates.

New layout repair/rearrange advisory slice: source-style layout repair/rearrange helpers (`space.spaces.repairLayout` and `space.spaces.rearrangeWidgets`) now include the shared server-generated Memory Tree advisory/no-authority envelope beside prompt-preflight, autonomy-policy, structured progress, and metadata-only output-compaction receipts. Caller-forged memory authority, raw memory context, bypass flags, renderer/source/API-auth/script markers, tokens, and secret-looking fixtures remain absent while layout context stays untrusted advisory metadata that cannot bypass prompt-preflight, approval, sandbox preview, visual QA, or rollback/recovery gates.

New revision-history advisory slice: revision history/recovery list tool responses (`space.revisions`, `space.revision.list`, `space.history`, and current-space aliases) now include the shared server-generated Memory Tree advisory/no-authority envelope beside required recovery prompt-preflight, autonomy-policy, `recovery.revision.list:<space_id>` structured progress, and metadata-only output-compaction receipts. Caller-forged memory authority, raw memory context, renderer/source/API-auth fields, scripts, tokens, and secret-looking fixtures remain absent while revision timeline context stays untrusted advisory metadata that cannot bypass prompt-preflight, approval, sandbox preview, visual QA, or rollback/recovery gates.

New active-instruction helper advisory slice: source-style current instruction helpers (`space.current.agentInstructions` and `space.current.specialInstructions`) now include the shared server-generated Memory Tree advisory/no-authority envelope beside active-space instruction prompt-preflight, `instructions:<space_id>` structured progress, autonomy-policy, and metadata-only output-compaction receipts. Caller-forged memory authority, raw memory context, raw active instructions, renderer/source/html/API-auth fields, scripts, tokens, and secret-looking fixtures remain absent while instruction context stays untrusted advisory metadata that cannot bypass prompt-preflight, approval, sandbox preview, visual QA, or rollback/recovery gates.

New reposition advisory slice: source-style current-space viewport/reposition helpers (`space.spaces.repositionCurrentSpace`, `space.current.reposition`, and `space.current.reposition_viewport`) now include the shared server-generated Memory Tree advisory/no-authority envelope beside creator-commit prompt-preflight, `layout.reposition:<space_id>` structured progress, autonomy-policy, and metadata-only output-compaction receipts. Caller-forged memory authority, top-level and nested viewport raw memory context, renderer/source/API-auth fields, scripts, tokens, and secret-looking fixtures remain absent while viewport/reposition context stays untrusted advisory metadata that cannot bypass prompt-preflight, approval, sandbox preview, visual QA, or rollback/recovery gates.

New research-artifact advisory slice: Research Harness artifact/export updates (`set_research_artifact` plus `space.research.artifact.set` / `space.research.report.set` adapters) now include the shared server-generated Memory Tree advisory/no-authority envelope beside artifact prompt-preflight, autonomy-policy, `research-artifact:<space_id>` structured progress, and metadata-only output-compaction receipts. Caller-forged memory authority, raw memory context, markdown bodies, renderer/source/API-auth fields, scripts, tokens, and secret-looking fixtures remain absent while exported research summaries stay untrusted advisory context that cannot bypass prompt-preflight, approval, sandbox preview, visual QA, or rollback/recovery gates.

New app-URL advisory slice: source-style logical app URL resolution (`space.spaces.resolveAppUrl`) now includes the shared server-generated Memory Tree advisory/no-authority envelope beside browser-surface prompt-preflight, destructive-external-action autonomy-policy, `resolve-app-url:space.spaces.resolveappurl` progress, and metadata-only output-compaction receipts. Caller-forged memory authority and raw memory context cannot mark app/navigation context as trusted or bypass safety gates, and unsafe logical paths, renderer/source/API-auth fields, scripts, tokens, and secret-looking fixtures remain absent from serialized responses and compaction evidence.

New template advisory slice: high-risk template install and Big Bang template reset responses now include the shared server-generated Memory Tree advisory/no-authority envelope beside template prompt-preflight, autonomy-policy, structured progress when present, and metadata-only output-compaction receipts. Template setup/reset context remains untrusted advisory metadata, cannot bypass prompt-preflight, approval, sandbox preview, visual QA, or rollback/recovery gates, and caller-forged trusted memory authority, raw memory context, renderer/source/html/API-auth/script/token/secret fixture values remain absent from serialized responses and compaction evidence.

New recovery-toggle advisory slice: whole-Space, widget, and recovery-module quarantine/disable/enable responses now include the shared server-generated Memory Tree advisory/no-authority envelope beside recovery prompt-preflight, autonomy-policy, structured progress, and metadata-only output-compaction receipts. Recovery/admin toggle context remains untrusted advisory metadata, cannot bypass prompt-preflight, approval, sandbox preview, visual QA, or rollback/recovery gates, and stored generated/module/widget bodies, operator reasons, renderer/source/html/API-auth/script/token/secret fixture values remain absent from serialized responses and compaction evidence.

New recovery-restore advisory slice: whole-Space revision restore and single-widget revision restore now include the shared server-generated Memory Tree advisory/no-authority envelope beside recovery prompt-preflight, autonomy-policy, structured progress, and metadata-only output-compaction receipts. Rollback/time-travel context remains untrusted advisory metadata, cannot bypass prompt-preflight, approval, sandbox preview, visual QA, or recovery gates, and renderer/source/html/API-auth/script/token/secret fixture values remain absent from serialized responses and compaction evidence.

New delete-space advisory slice: direct `delete_space(..., include_safety_receipts=True)` plus source-style deletion helpers (`space.spaces.deleteSpace` and `space.spaces.removeSpace`) now include the shared server-generated Memory Tree advisory/no-authority envelope beside delete prompt-preflight, autonomy-policy, `space.delete:<space_id>` structured progress, and metadata-only output-compaction receipts. Caller-forged memory authority, raw memory context, renderer/source/html/API-auth fields, scripts, tokens, and secret-looking fixtures remain absent while deletion context stays untrusted advisory and cannot bypass prompt-preflight, approval, sandbox preview, visual QA, or rollback/recovery gates.

New research-progress advisory slice: Research Harness progress updates (`set_research_progress` plus `space.research.progress.set` / `space.research.progress.update` adapters) now include the shared server-generated Memory Tree advisory/no-authority envelope beside creator-commit prompt-preflight, `space.research.progress` autonomy-policy, `research:<space_id>` structured progress, and metadata-only output-compaction receipts. Caller-forged memory authority cannot mark research progress/source context as trusted or bypass safety gates, and raw memory context, renderer/script/API-auth fields, and secret-looking fixtures remain absent from serialized receipts and compaction evidence.

New path-helper advisory slice: source-style logical storage path helpers (`space.spaces.buildSpace*Path`) now include the shared server-generated Memory Tree advisory/no-authority envelope beside prompt-preflight, autonomy-policy, `path.helper:<space_id>` progress, and output-compaction receipts. Caller-forged `memory_advisory` / `trusted_system_memory` authority cannot mark development/path-boundary context as trusted or bypass safety gates, and raw memory context, renderer/source/html/API-auth fields, scripts, tokens, and secret-looking fixtures remain absent from serialized receipts and compaction evidence.

New widget SDK advisory slice: no-space widget SDK/read-helper responses (`space.spaces.widgetApiVersion`, size/position/rendered-size helpers, source-style ID normalizers, and `space.spaces.currentId`) now include the shared server-generated Memory Tree advisory/no-authority envelope beside prompt-preflight, autonomy-policy, progress, and output-compaction receipts. Caller-forged helper `memory_advisory` / `trusted_system_memory` authority cannot mark SDK helper context as trusted or bypass safety gates, and hostile renderer/source/html/API-auth/raw-prompt/script/token fixtures remain absent from serialized receipts and compaction evidence.

New widget read/list advisory slice: widget collection/list and read/detail tool helpers (`space.current.widgets`, `space.current.widget.list`, `space.current.listWidgets`, `space.current.byId`, `space.current.widgetsById`, `space.spaces.listWidgets`, `space.spaces.widgets`, `space.spaces.readWidget`, `space.spaces.getWidget`, `space.widget.read`, `space.widget.get`, `space.current.readWidget`, `space.current.getWidget`, `space.widget.list`, `space.widgets.list`, `space.current.widgets.list`, `widget.list`, `widget.read`, and `widget.get`) now include the shared server-generated Memory Tree advisory/no-authority envelope beside widget-runtime prompt-preflight, generated-widget-execution autonomy-policy, `widget.read:<space_id>` progress, and output-compaction receipts. Caller-forged widget read/list payloads cannot mark memory as trusted or bypass safety gates, and renderer/html/source/data/API-auth/raw-prompt/script/token/secret fixtures remain absent from serialized responses and compaction evidence.

New collection/current-read advisory slice: Space collection/current read helpers (`space.list`, `space.spaces`, `space.spaces.list`, `space.spaces.listSpaces`, `space.spaces.items`, `space.spaces.all`, `space.spaces.byId`, `space.current`, `space.current.get`, `space.spaces.current`, and `space.spaces.getCurrentSpace`) now include the shared server-generated Memory Tree advisory/no-authority envelope beside prompt-preflight, autonomy-policy, progress, and output-compaction receipts. Caller-forged memory authority, raw memory context, renderer/source/html/API-auth/script/prompt/token/bearer fixture values stay out of serialized responses and compaction text.

New direct GET route receipt parity slice: HTTP read routes (`/api/spaces`, `/api/spaces/current`, `/api/spaces/get`, `/api/spaces/widgets`, and `/api/spaces/widget`) now delegate to the same receipt-bearing Space/widget read adapters, preserving the existing sanitized route payloads while returning metadata-only prompt-preflight, autonomy-policy, progress, Memory Tree advisory/no-authority, and output-compaction evidence without exposing generated widget bodies or hostile renderer/source/html/script/API-auth/raw-prompt/secret fixtures.

New package advisory slice: Space Agent package import/export responses now include the shared server-generated Memory Tree advisory/no-authority envelope (`untrusted_advisory`, gate bypass false, required prompt-preflight/approval/sandbox/visual-QA/rollback gates) beside package prompt-preflight, autonomy-policy, progress, and output-compaction receipts. Caller-forged package memory authority cannot mark imported/exported package context as trusted or bypass safety gates, and raw package YAML/archive/widget bodies, renderer/source/API-auth fields, scripts, tokens, and secret-looking fixtures stay out of serialized receipts and compaction evidence.

New shared-data advisory slice: shared-data set/list/read/delete tool responses now include the shared server-generated Memory Tree advisory/no-authority envelope beside shared-data prompt-preflight, autonomy-policy, progress, and output-compaction receipts. Caller-forged `trusted_system_memory` authority cannot mark shared context as trusted or bypass safety gates, and raw slot values, renderer/source/API-auth fields, scripts, tokens, and secret-looking fixtures stay out of serialized responses and compaction evidence.

New revision-route receipt slice: the HTTP `/api/spaces/revisions` route now reuses the same metadata-only recovery envelope as `space.revisions` tool aliases, returning required recovery prompt-preflight, autonomy-policy, `recovery.revision.list:<space_id>` progress, server-generated Memory Tree advisory/no-authority, and output-compaction receipts beside the existing sanitized `revisions` list. Revision timeline context stays untrusted and renderer/source/html/script/API-auth, raw prompt, snapshot, and secret-looking fixtures remain absent from route responses and compaction evidence.

New navigation advisory slice: source-style Space open/reload helpers (`space.spaces.open`, `space.spaces.openSpace`, `space.spaces.reloadCurrentSpace`, and `space.spaces.reloadSpace`) now include the shared server-generated Memory Tree advisory/no-authority envelope beside browser-navigation prompt-preflight, autonomy-policy, progress, and output-compaction receipts. Caller-forged memory authority cannot mark navigation context as trusted or bypass safety gates, and raw memory context, renderer/API-auth fields, scripts, and secret-looking fixtures stay out of serialized responses and compaction evidence.

Recent receipt-safety slice: receipt-only browser-surface tool actions now include the shared Memory Tree advisory/no-authority envelope (`untrusted_advisory`, gate bypass false, required prompt-preflight/approval/sandbox/visual-QA/rollback gates) beside existing browser-surface prompt-preflight, autonomy-policy, progress, and output-compaction receipts; caller-supplied memory authority cannot forge a trusted context or bypass safety gates, and raw URL/prompt/renderer/API-auth/history/text/secret fixtures remain omitted from receipts and compaction evidence.

New active-instruction safety slice: direct `/api/spaces/update` active-space instruction writes with `includeSafetyReceipts` now include the same server-generated Memory Tree advisory/no-authority envelope beside prompt-preflight, autonomy-policy, progress, and output-compaction evidence. Caller-forged `memory_advisory` authority is ignored, `can_bypass_safety_gates` remains false, and raw instruction/source/html/renderer/API-auth/secret fixtures stay out of serialized receipts and compaction text.

New create-space advisory slice: direct `/api/spaces/create` with `includeSafetyReceipts` and source-style create helpers (`space.create`, `space.spaces.create`, `space.spaces.createSpace`) now return the same server-generated Memory Tree advisory/no-authority envelope beside create-time prompt-preflight, autonomy-policy, progress, and output-compaction evidence, while caller-forged trusted memory authority, raw instructions, renderer/source/API-auth fields, scripts, and secret-looking fixtures remain omitted from receipts, compaction, and visual QA output.

## OpenHuman-Inspired Expansion Track

Canonical roadmap: `.hermes/plans/capy-openhuman-inspired-roadmap.md`.
Implementation handoff/evidence: `.hermes/plans/2026-05-17_123717-openhuman-inspired-capy-roadmap.md` and `/tmp/openhuman-review.md`.
Latest competitive audit: `.hermes/plans/2026-05-24_123241-capy-spaces-openhuman-space-agent-feature-audit.md`.

Decision summary:

- Adopt selected OpenHuman product/architecture ideas clean-room: Memory Tree, auto-fetch/source freshness, TokenJuice-style output compaction, user-visible autonomy modes, prompt-injection preflight, model routing hints, and structured progress events.
- Do **not** pivot to OpenHuman, do **not** rewrite Hermes/Capy as Rust/Tauri, and do **not** copy GPLv3 OpenHuman code, tests, schemas, comments, or fixtures.
- Keep Hermes as the persistent autonomous gateway/tool/cron/subagent layer and Capy Spaces as the safe metadata-only production canvas with recovery, revision history, sandbox preview, approval gates, and visual QA.

Roadmap priority for upcoming autonomous sprints:

1. Remaining prompt-preflight + advisory memory enforcement for high-risk recovery/tool boundaries; creator preview/commit, active-space instruction/context, direct active-space activate/deactivate session switches, direct Space update instruction writes, save-meta instruction writes, repair prompt, whole-Space and recovery-module repair-event list responses, source-style Space revision/history list responses, source-style layout repair, widget-runtime prompt queue/status UI, no-space widget SDK helpers, widget detail/read/get/see helpers, widget-runtime contract helper inspections, source-style widget definition/blueprint preview/source helpers, logical storage path helpers, receipt-only browser adapters, receipt-only development terminal/shell adapters with common payload pass/block classification, Space open/reload navigation helpers, source-refresh worker/manual refresh paths, camera stream prompt-preflight/action-policy/progress receipts, camera/package/recovery/checkpoint boundaries, and route-level direct GET receipts now have metadata-only foundations. Keep extending the same envelope to any remaining context-affecting route/tool boundary before memory can influence actions.
2. Product-visible compaction evidence for remaining long tool/subagent/recovery outputs; run-all, individual demos, direct `/api/spaces/update` active-instruction safety receipts, creator preview/commit, active context, Research Harness progress updates and artifact completion, scoped progress, manual and scheduled Memory Tree source-refresh receipts, receipt-only Browser Surface, receipt-only development terminal/shell adapters, WebUI streaming tool/subagent terminal callbacks, high-risk template installs, approved camera-stream, queued widget events, shared data slot set/read/list/delete, Space checkpoint anchors, revision/history list responses, source-style Space create/duplicate/delete, source-style Space metadata/layout/repair/rearrange/reposition, source-style widget upsert, source-style widget definition/blueprint preview/source/render, source-style/current widget patch/delete/bulk-delete/toggle, source-style/current widget read/list/see, source-style Space read/get, source-style Space collection/current read, legacy generic `widget.list`/`widget.read`/`widget.get`, widget-runtime contract helper inspections, source-style Space open/reload navigation, logical app URL helper, Big Bang template reset, whole-Space recovery toggle, whole-Space repair-event list, recovery-module repair-event list, widget recovery toggle, recovery-module quarantine/toggle, and trusted session recovery repair-safe receipts already use the first receipt pattern.
3. Broaden safe source-specific fetchers and cron/daemon triggering now that the metadata-only refresh worker, manual trigger path, product-home scheduled tick action, and local knowledge bridge exist.
4. Progress producer expansion across real long-running browser/development/repair tasks; Research Harness progress updates now emit `taskboard.updated` producer events, while creator visual-QA commit gates, Memory Tree source-refresh ingest workers, the demo smoke suite, individual browser demo smokes, WebUI streaming `run.started`/`run.completed`/`run.failed` lifecycle events, WebUI streaming `tool.started`/`tool.completed`/`tool.failed` lifecycle callbacks, source-style layout-repair/reposition progress events with companion action-policy receipts, logical path-helper development-boundary calls, receipt-only development terminal/shell actions, source-style Space open/reload navigation events, active-context tool calls, shared data slot set/read/list/delete actions, widget-event list reads, widget detail/see inspections, source-style widget defineWidget/upsert/renderWidget/blueprint preview actions, widget-runtime contract helper inspections, source-style Space deletions, Space checkpoint receipts, whole-Space repair-event list responses, recovery widget enable/disable toggles, recovery-module repair-event list responses, recovery revision restores, and Space Agent package import/export operations now emit the first workflow/gate/ingest/run/repair/browser/development/cooperation/construction/recovery/package events, and Space detail can inspect Space-scoped streams.
5. Model-route invocation plumbing; action-policy receipts now include safe metadata-only `model_route_resolution` decisions and fallback reasons, while actual Capy/Hermes invocation selection still needs wiring.
6. Optional connector catalog/sidecar exploration only after the remaining integration slices above are proven end-to-end.

Product implication: future Space Agent parity should be judged not only by demo widgets, but by whether Capy can remember, cite, compact, refresh, and safely apply local context while preserving metadata-only safety and rollback.

Recent completed slices:

- `feat(spaces): add camera template safety receipts`
  - Added RED/GREEN backend coverage proving the Camera Dashboard template install route returns prompt-preflight, autonomy-policy, structured progress, server-generated Memory Tree advisory/no-authority, and output-compaction receipts.
  - Kept camera setup metadata-only: hostile renderer/html/script/source/data/API-auth, raw prompt, credential/token/secret, generated widget body, stream URL, and `rtsp://` payload fields remain absent from serialized route output while the compaction text preserves safe template/progress/no-bypass evidence.

- `feat(capy-memory): ingest GitHub workflow run pending deployments metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub Actions workflow-run pending-deployments API payloads (`/repos/{owner}/{repo}/actions/runs/{run_id}/pending_deployments`) request `application/json` and persist only metadata-only deployment-gate summaries.
  - The parser reconstructs summaries from safe repository path, run id, pending deployment count, bounded environment ids/names, wait timer/status/timestamps, and safe reviewer user/team/app labels while omitting comments, raw URLs, deployment/environment bodies, API-auth fields, raw prompts, scripts, renderer/source/data/html fields, tokens, and secret-like fixture values. JSON Feed/text fallback, abusive authorities, padded run ids, encoded suffixes, extra tails, and redirect route drift fail closed before unsafe fetch or persistence.

- `feat(spaces): show source refresh queue on product home`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Capy Spaces product home fetches `api/capy-memory/source/jobs?limit=5` and renders a styled “Source refresh queue” card using the existing connector/card grid treatment.
  - The queue card displays only bounded metadata-only source id, allow-listed status, attempt count, and safe timestamps, with fail-soft unavailable/empty states and adversarial coverage for unknown statuses, negative attempts, invalid timestamps, over-limit rows, raw origin URIs, queued payloads, error URLs, renderer/source/html/script fields, raw prompts, API-auth fields, and secret-looking fixtures.

- `fix(capy-memory): harden GitHub Pages feed bypass`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub Pages API payloads (`/repos/{owner}/{repo}/pages`) reject JSON Feed-shaped `version` / `items` keys even when an otherwise valid Pages `status` is present.
  - Preserved metadata-only Pages summaries for safe status/build/CNAME/public/HTTPS/protected-domain fields while failing closed before vault/search persistence for feed summaries, API-auth markers, raw prompts, renderer/source/data/html fields, scripts, and secret-looking fixtures.

- `feat(capy-memory): ingest GitHub workflow-run approvals metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub Actions workflow-run approval API payloads (`/repos/{owner}/{repo}/actions/runs/{run_id}/approvals`) request `application/json` and persist only metadata-only deployment approval summaries.
  - The parser reconstructs summaries from safe repository path, run id, approval count, allow-listed approval state, actor login, environment names, and safe timestamps while omitting approval comments, URLs, API-auth query/fragment markers, raw prompts, scripts, renderer/source fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed/text fallback, redirect/final-URL mismatch, lookalike host, userinfo/port, padded run id, encoded suffix, and extra-tail variants fail closed before unsafe fetch or persistence.

- `feat(capy-memory): ingest GitHub single-deployment metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub single-deployment API payloads (`/repos/{owner}/{repo}/deployments/{deployment_id}`) request `application/json` and persist only metadata-only deployment summaries.
  - The parser reconstructs summaries from the exact safe repository path, matched deployment id, environment, ref, SHA prefix, task, production/transient flags, safe creator login, and timestamps while omitting descriptions, payloads, status/repository/creator URLs, API-auth query/fragment markers, raw prompts, scripts, renderer/source fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads and malformed encoded deployment-route variants fail closed before unsafe fetch or vault record creation.

- `fix(capy-memory): support encoded branch-protection names`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub branch-protection API routes with encoded slash branch names (for example `/repos/{owner}/{repo}/branches/feature%2Fsource-refresh/protection`) request `application/json`, fetch only the sanitized URL, and persist decoded branch metadata in bounded summaries.
  - Preserved metadata-only summaries for status-check counts, review settings, and branch-protection booleans while omitting raw URLs, API-auth/query/fragment markers, raw prompts, renderer/script fields, bypass identities, and secret-like fixture values from receipts/search/vault output.
  - Raw slash branches, double-encoded branches, empty/traversal branch segments, lookalike hosts, malformed protection tails, and encoded protection suffixes fail closed before fetch.

- `feat(capy-memory): ingest GitHub release reactions metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub release reactions API payloads (`/repos/{owner}/{repo}/releases/{release_id}/reactions`) request `application/json` and persist only metadata-only reaction summaries.
  - The parser reconstructs summaries from safe repository path, release id, reaction count, allow-listed reaction content counts, bounded reactor logins, reaction ids, and safe timestamps while omitting raw bodies, URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Malformed/encoded route variants, non-canonical raw authorities, explicit ports, HTTP/userinfo authorities, and JSON Feed-shaped bypass payloads fail closed before unsafe fetch or persistence.

- `feat(spaces): add development tool lifecycle progress receipts`
  - Added RED/GREEN backend coverage proving receipt-only `space.development.terminal` / `development.shell` actions emit a metadata-only `tool.started` → `tool.completed` progress lifecycle with a stable `development.terminal:<space_id>` run id.
  - Preserved backward-compatible `progress_event` completion receipts while adding a `progress_events` list and safe `progress_event_types` compaction evidence, without leaking raw commands, prompts, renderer/source/html/API-auth fields, bearer strings, local paths, or secret-looking fixtures to public receipts or progress logs.

- `feat(spaces): add instruction memory advisory receipts`
  - Added RED/GREEN backend coverage proving `space.current.agentInstructions` and `space.current.specialInstructions` return the server-generated Memory Tree advisory/no-authority envelope beside active-space instruction prompt-preflight, `instructions:<space_id>` progress, autonomy-policy, and output-compaction receipts.
  - Threaded advisory boundary metadata and required gates into current-instruction compaction evidence while ignoring caller-forged memory authority and omitting raw active instructions, renderer/source/html/API-auth fields, scripts, tokens, and secret-looking fixture values.

- `feat(spaces): add reposition memory advisory receipts`
  - Added RED/GREEN backend coverage proving source-style current-space viewport/reposition helpers return the server-generated Memory Tree advisory/no-authority envelope beside creator-commit prompt-preflight, `layout.reposition:<space_id>` progress, autonomy-policy, and output-compaction receipts.
  - Threaded advisory boundary metadata and required gates into reposition compaction evidence while ignoring caller-forged memory authority and omitting top-level or nested viewport raw memory context, renderer/source/API-auth fields, scripts, tokens, and secret-looking fixture values.

- `feat(spaces): add research artifact memory advisory receipts`
  - Added RED/GREEN backend coverage proving `space.research.artifact.set` returns the server-generated Memory Tree advisory/no-authority envelope beside artifact prompt-preflight, autonomy-policy, structured progress, and output-compaction receipts.
  - Threaded safe advisory-boundary metadata into research artifact compaction evidence while ignoring caller-forged memory authority and omitting raw memory context, markdown bodies, renderer/source/API-auth fields, scripts, tokens, and secret-looking fixtures.

- `feat(spaces): add widget SDK memory advisory receipts`
  - Added RED/GREEN backend coverage by strengthening `_assert_widget_sdk_helper_receipts` so no-space widget SDK helpers must return the server-generated Memory Tree advisory/no-authority envelope beside existing prompt-preflight, autonomy-policy, progress, and compaction receipts.
  - Threaded advisory boundary metadata into helper compaction evidence for widget API version, size/position/rendered-size helpers, ID normalizers, and current-id lookups while preserving no-space progress run ids and omitting forged memory authority, renderer/source/html/API-auth/raw-prompt/script/token/secret fixture values.

- `feat(capy-memory): ingest GitHub commit comment reactions metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub commit-comment reactions API payloads (`/repos/{owner}/{repo}/comments/{comment_id}/reactions`) request `application/json` and persist only metadata-only reaction summaries.
  - The parser reconstructs summaries from safe repository path, comment id, reaction count, allow-listed reaction content counts, bounded reactor logins, reaction ids, and safe timestamps while omitting raw bodies, URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Malformed/encoded route variants, non-canonical raw authorities, explicit ports, HTTP/userinfo authorities, and JSON Feed-shaped bypass payloads fail closed before unsafe fetch or persistence.

- `fix(capy-memory): harden GitHub release-list refresh route`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub release-list API payloads (`/repos/{owner}/{repo}/releases`) request `application/json` and reject text fallback, JSON Feed-shaped bypass payloads, and malformed tail rows before vault/search persistence.
  - Release-list summaries remain metadata-only, reconstructing only safe repository path, release count, bounded release names/tags, draft/prerelease flags, and publish timestamps while omitting release bodies, assets/archive URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-looking fixture values.

- `feat(capy-memory): ingest GitHub commit activity metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub commit-activity API payloads (`/repos/{owner}/{repo}/stats/commit_activity`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries only from safe repository path, week count, total commits, and active-week count while omitting raw week timestamps, URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Userinfo/query/fragment origins sanitize to the exact safe fetch route, while `application/feed+json`, malformed rows, extra keys, mismatched totals, and encoded/null-suffixed route bypasses fail closed before unsafe fetch or persistence.

- `feat(capy-memory): ingest GitHub interaction-limits metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub interaction-limits API payloads (`/repos/{owner}/{repo}/interaction-limits`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, allow-listed limit/origin values, and safe expiry timestamp while omitting raw URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Query/fragment origins sanitize to a durable public label plus exact safe fetch route, requeued jobs preserve that fetch route, while text fallback payloads, lookalike-host route bypasses, userinfo authorities, malformed route tails, and encoded route tricks fail closed before unsafe fetch or persistence.

- `fix(capy-memory): harden GitHub single-release refresh routes`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub single-release API payloads (`/repos/{owner}/{repo}/releases/{release_id}`) request `application/json` and persist only metadata-only release id, tag/name, draft/prerelease, and published timestamp evidence.
  - Malformed single-release route tails such as encoded/null-suffixed ids, extra segments, and nonnumeric ids now fail closed before text fallback can create unsafe vault/search records, keeping raw release notes, URLs, API-auth fields, prompts, scripts, renderer/source fields, and secret-like fixture values out of receipts/search/vault output.

- `feat(spaces): harden widget event ingress receipts`
  - Added RED/GREEN backend coverage proving queued `space.current.widget.event` ingress receipts now mark `output_compaction` as metadata-only and thread the event-scoped `widget-event:<event_id>` progress run id/status into the compaction text.
  - Persisted widget-event summaries regenerate only allow-listed metadata, preserving prompt-preflight/action-policy/progress evidence while keeping raw prompts, renderer/source/html/script fields, API-auth fields, bearer/token values, and secret-looking fixtures out of queued/listed event receipts.

- `feat(spaces): add development advisory receipts`
  - Added RED/GREEN backend coverage proving receipt-only development terminal actions return the shared Memory Tree advisory/no-authority envelope (`untrusted_advisory`, gate bypass false, required prompt-preflight/approval/sandbox/visual-QA/rollback gates) beside existing prompt-preflight, autonomy-policy, progress, and compaction receipts.
  - Threaded safe advisory-boundary metadata into development-tool compaction evidence while continuing to omit raw commands, prompts, renderer/source/html/script fields, API-auth fields, local paths, bearer strings, and secret-looking fixture values.

- `feat(streaming): add terminal compaction receipts`
  - Added RED/GREEN streaming progress coverage proving terminal tool and subagent callbacks return metadata-only `output_compaction` evidence with safe stream-scoped run ids, path/URL-shaped hostile stream-id fallback, and preserved numeric exit status; structured tool callback coverage also proves SSE progress-event surfacing of the metadata-only receipt.
  - The receipt builder never stringifies callback payloads, so hostile tool names, args, outputs, subagent summaries, raw prompts, renderer/source/html/script fields, API-auth fields, bearer strings, local paths, and secret-looking fixtures stay out of durable progress logs, status, and returned receipts.

- `fix(capy-memory): harden GitHub issue comments routes`
  - Added RED/GREEN Memory Tree source-refresh coverage proving malformed, missing-number, encoded/double-encoded, and lookalike-host GitHub issue/pull-request comments routes fail closed before fetch, and non-canonical uppercase `api.github.com` authorities are downgraded to local `capy-memory://` origins before jobs can persist raw API URLs.
  - The exact `/repos/{owner}/{repo}/{issues|pulls}/{number}/comments` metadata parser still ingests safe comment ids, commenters, and timestamps, while malformed routes, query/fragment auth markers, raw prompt markers, comments bodies, renderer/source/html/script fields, and secret-looking fixture values stay out of fetches, receipts, search, and vault output.

- `feat(spaces): add widget read safety receipts`
  - Added RED/GREEN backend coverage proving source-style and current-space widget read/get helpers now return metadata-only prompt-preflight, autonomy-policy, progress, and output-compaction receipts while preserving sanitized widget detail payloads and unsafe revision-event redaction.
  - Widget reads use `widget.read:<space_id>` progress ids, now cover legacy generic `widget.read`/`widget.get` aliases, share the allow-listed prompt metadata summarizer with widget list/detail surfaces, keep renderer/source/API-auth/raw-prompt/script/nested-prompt-metadata/revision-escape/secret fixture values out of serialized responses and compaction text, and read the target widget before recording progress so missing-widget reads cannot emit false `tool.completed` telemetry.

- `feat(spaces): add collection read safety receipts`
  - Added RED/GREEN backend coverage proving Space collection/current read helpers (`space.list`, `space.spaces.listSpaces`, `space.spaces.items`, `space.spaces.all`, `space.spaces.byId`, `space.current.get`, and `space.spaces.getCurrentSpace`) now return metadata-only prompt-preflight, autonomy-policy, progress, and output-compaction receipts while preserving sanitized list/current Space payloads.
  - Collection reads use the neutral `space.collection:list` progress id, selected current reads use `space.current.read:<space_id>`, no-current reads use `space.current.read:none`, and hostile renderer/source/html/API-auth/raw-prompt/bearer/secret fixture values stay out of serialized responses and compaction text.

- `feat(spaces): add current id safety receipts`
  - Added RED/GREEN backend coverage proving `space.spaces.currentId` now returns metadata-only prompt-preflight, autonomy-policy, progress, and output-compaction receipts while preserving the functional active/current Space id payload.
  - Helper progress uses the action-scoped `space.current:id` run id without a synthetic Space id and keeps renderer/source/API-auth/raw-prompt/script/bearer/secret fixture values out of serialized responses and compaction text.

- `feat(capy-memory): add manual refresh progress receipts`
  - Added RED/GREEN backend coverage proving the manual `/api/capy-memory/source/refresh` route now returns a sanitized top-level `run.completed` progress receipt with fixed `source-refresh.manual` run id for both all-source and targeted refresh actions, while omitting raw source URLs from metadata-only job responses.
  - Extended the real `static/spaces.js` UI behavior harness so product-home manual/connector Memory refresh results visibly render `Source refresh progress` evidence while hostile renderer/API-key/raw-prompt fixtures and raw source URLs remain absent from the DOM.

- `feat(spaces): add health safety receipts`
  - Added RED/GREEN backend coverage proving `space.api.health` and `space.health` now return metadata-only prompt-preflight, autonomy-policy, progress, and output-compaction receipts while preserving the functional Capy Spaces health payload.
  - Health progress uses the neutral no-space `space.health:api` run id, includes only Space count/action metadata in compaction evidence, and keeps renderer/source/API-auth/raw-prompt/script/secret fixture values out of serialized responses and compaction text.

- `feat(spaces): add widget API version safety receipts`
  - Added RED/GREEN backend coverage proving the no-space `space.spaces.widgetApiVersion` helper now returns metadata-only prompt-preflight, autonomy-policy, progress, and output-compaction receipts while preserving the functional `widget_api_version` and runtime metadata-only payload.
  - Helper progress uses the neutral action-scoped `widget.sdk:helper` run id without a synthetic Space id and keeps renderer/source/API-auth/raw-prompt/script/secret fixture values out of serialized responses and compaction text.

- `feat(spaces): add normalize id safety receipts`
  - Added RED/GREEN backend coverage proving `space.spaces.normalizeSpaceId` and `space.spaces.normalizeWidgetId` now return metadata-only prompt-preflight, autonomy-policy, progress, and output-compaction receipts while preserving normalized ID/fallback behavior.
  - Helper progress uses the safe action-scoped `spaces.sdk:id` run id without a synthetic Space id and keeps renderer/html/source/API-auth/script/token/secret fixture values out of serialized responses and compaction text.

- `feat(spaces): add widget SDK helper safety receipts`
  - Added RED/GREEN backend coverage proving no-space size, position, and rendered-size SDK helpers now return metadata-only prompt-preflight, autonomy-policy, progress, and output-compaction receipts while preserving their token/size/position payloads.
  - Helper progress uses safe action-scoped `widget.sdk:size`, `widget.sdk:position`, and `widget.sdk:rendered-size` run ids without space ids or raw renderer/source/API-auth/script fixture leakage.

- `feat(capy-memory): ingest GitHub commit-comments metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub commit-comments API payloads (`/repos/{owner}/{repo}/commits/{sha}/comments`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, commit SHA prefix, comment count, bounded commenter logins, comment ids, and safe timestamps while omitting raw comment bodies, diff hunks, file paths, URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Query/fragment origins sanitize to the exact safe GitHub API URL, while JSON Feed-shaped payloads fail closed before persistence or unsafe output.

- `fix(spaces): harden development tool preflight scanning`
  - Added RED/GREEN backend coverage proving receipt-only development terminal/shell actions block hostile prompt text hidden in the middle of large high-risk payload lists, compact/camelCase raw prompt/API-key aliases, oversized sparse lists, and wide sparse dict payloads.
  - Hardened the internal-only development-tool preflight corpus to scan a full bounded high-risk payload traversal with max char/part/node caps and fail-closed truncation, while keeping public receipts metadata-only and still refusing command execution, filesystem writes, raw request storage, and raw prompt/credential/source/renderer leakage.

- `feat(spaces): show shared data delete receipts`
  - Added RED/GREEN backend route and real-`static/spaces.js` coverage proving confirmed shared-data slot deletion now returns and renders metadata-only prompt-preflight, action-policy, progress, and compaction receipt evidence through the direct Space detail UI path.
  - Kept direct-route and DOM output safe: hostile raw slot values, renderer/script, API-auth, bearer, raw-prompt, and secret fixture fields stay absent while the visible receipt shows `space.shared_slot.delete`, `hint:summarize`, `shared-slot.delete:<space_id>`, and bounded compaction evidence.

- `feat(spaces): show delete safety receipts`
  - Added RED/GREEN real-`static/spaces.js` coverage proving confirmed Space deletion now preserves and renders the backend `prompt_preflight`, `autonomy_policy`, `progress_event`, and `output_compaction` receipt after the Spaces home refresh.
  - Kept the destructive-delete UI receipt metadata-only: hostile raw prompt, renderer/script, API-auth, secret, unsafe path, and unsafe compaction-artifact fixture fields stay absent from the DOM while the visible card shows preflight, policy, delete progress, and compaction evidence.

- `feat(capy-memory): ingest GitHub code-frequency metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub code-frequency API payloads (`/repos/{owner}/{repo}/stats/code_frequency`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, week count, total additions, total deletions, net changed lines, and active-week count while omitting raw URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Userinfo/query/fragment origins sanitize to the exact safe GitHub API URL, while feed-shaped payloads, malformed rows, positive deletion rows, encoded route tricks, and route-shaped text fallbacks fail closed before persistence or unsafe fetches.

- `feat(capy-memory): ingest GitHub Actions selected-actions metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub Actions selected-actions API payloads (`/repos/{owner}/{repo}/actions/permissions/selected-actions`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, GitHub-owned/verified booleans, allowed pattern count, and bounded safe pattern samples while omitting raw URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Query/fragment origins sanitize to the exact safe GitHub API URL, while unsafe traversal-like or URL/domain-like patterns, lookalike hosts, suffixed selected-actions routes, encoded suffixes, and explicit-port authorities fail closed before persistence or unsafe fetches.

- `feat(capy-memory): ingest GitHub Actions workflow access metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub Actions workflow-access API payloads (`/repos/{owner}/{repo}/actions/permissions/access`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path and the allow-listed external workflow access level (`none`, `user`, or `organization`) while omitting raw URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, userinfo, and secret-like fixture values from receipts/search/vault output.
  - Jobs persist only `source_refresh_kind` plus `repo_path` and reconstruct the fetch URL at run time; JSON Feed bypass payloads, text fallbacks, cross-repository final-URL drift, query/fragment auth final-URL drift, malformed access tails, userinfo origins, explicit ports, and lookalike-host route-shaped URLs fail closed before response-body reads, persistence, or unsafe fetch continuation.

- `feat(capy-memory): ingest GitHub repository custom properties metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub repository custom-properties API payloads (`/repos/{owner}/{repo}/properties/values`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, property count, bounded property names, value type, and multi-value counts while omitting raw property values, row/global URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Userinfo/query/fragment origins sanitize to the exact safe GitHub API URL, while text fallback payloads, JSON Feed bypasses, malformed route tails, explicit-port authorities, and lookalike-host route-shaped URLs fail closed before fetch or persistence.

- `feat(capy-memory): ingest GitHub Actions cache metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub Actions caches API payloads (`/repos/{owner}/{repo}/actions/caches`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, cache count, total safe byte size, bounded cache ids, refs, sizes, and safe timestamps while omitting cache keys, versions, URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Query/fragment origins sanitize to a durable display label and exact safe fetch route, while text fallback payloads, JSON Feed bypasses, malformed rows, userinfo/port origins, and lookalike-host route-shaped URLs fail closed without creating vault records or performing unsafe fetches.

- `feat(capy-memory): ingest GitHub repository webhooks metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub repository webhooks API payloads (`/repos/{owner}/{repo}/hooks`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, hook count, bounded hook ids/names, active flags, event names, safe timestamps, and last response codes while omitting callback URLs, delivery/test/ping URLs, config/auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Userinfo/query/fragment origins sanitize to the exact safe GitHub API URL, while secret config rows and lookalike-host route-shaped URLs fail closed without creating vault records or performing unsafe fetches.

- `feat(capy-memory): ingest GitHub workflow timing metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub Actions workflow-run timing API payloads (`/repos/{owner}/{repo}/actions/runs/{run_id}/timing`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe run id, required run duration, and bounded billable OS totals/job counts while omitting nested job-run details, URLs, API-auth fields, prompts, scripts, renderer/source fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads fail closed without creating vault records or persisting raw workflow timing output.

- `feat(spaces): record streaming run progress`
  - Added RED/GREEN streaming coverage proving WebUI browser-originated agent streams now emit exactly one metadata-only `run.started` event and one terminal `run.completed`/`run.failed` event per stream, including stale writeback/cancel return paths and eager user cancellation that would otherwise leave active-run counts inflated.
  - Run lifecycle progress uses sanitized `webui.run:*` ids via the same progress recorder path as tool/delta events, preserving legacy callback behavior without duplicate terminal counts.
  - Hostile fixtures covering raw prompts, renderer/source/API-auth fields, bearer/token text, scripts, paths, and secret-looking values remain absent from serialized Capy progress logs and product-surface receipts.

- `feat(spaces): record streaming delta progress`
  - Added RED/GREEN streaming coverage proving WebUI token and reasoning delta callbacks record one metadata-only Capy progress marker per stream/event family (`text.delta` and `thinking.delta`) before live metering.
  - The recorder persists only safe event type and bounded `webui.text:*` / `webui.thinking:*` run ids, dedupes high-volume deltas, clears per-stream dedupe markers during stream cleanup, and falls back for unsafe stream ids without echoing raw prompts, reasoning text, renderer/source/API-auth fields, scripts, or secret-looking fixture values.
  - Browser QA harness confirmed the existing Progress events card renders text/thinking families and recent event rows from actual checked-out `static/spaces.js` without console errors or rendered hostile fixture leaks.

- `fix: harden github advisories refresh parser`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub repository security-advisories API payloads (`/repos/{owner}/{repo}/security-advisories`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, advisory count, bounded GHSA/CVE ids, allow-listed severity/state, and safe timestamps while omitting advisory summaries/descriptions, vulnerabilities, URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Userinfo/query/fragment origins sanitize to the exact safe GitHub API URL, while JSON Feed/text fallback payloads, malformed tail rows, unsafe timestamps, and lookalike-host route-shaped URLs fail closed without creating vault records or performing unsafe fetches.

- `feat(spaces): preserve package import preflight compaction`
  - Added RED/GREEN backend coverage proving Space Agent package import `output_compaction` receipts now preserve the prompt-preflight status already present in the metadata-only action-policy receipt.
  - Kept package-import compaction evidence bounded to safe package format, Space id, widget count, policy/model-route/progress metadata, and prompt-preflight status while omitting package YAML, generated widget bodies, renderer/source/API-auth fields, scripts, and secret-looking fixture values.

- `feat(spaces): preserve creator memory advisory envelope`
  - Added RED/GREEN backend and real-`static/spaces.js` coverage proving creator-preview `memory_assist` now keeps the same metadata-only advisory/no-authority envelope as public Memory Tree retrieval at both the top level and per-hit level.
  - The creator memory-assist UI now renders a visible `Memory trust boundary` row with `context_authority: untrusted_advisory`, required gate labels, and `can_bypass_safety_gates: false`, while empty public-memory hits and hostile renderer/API-auth/script fixtures remain omitted.

- `feat(spaces): add first-fit layout safety receipts`
  - Added RED/GREEN backend coverage proving `space.spaces.buildCenteredFirstFitLayout` and `space.spaces.findFirstFitWidgetPlacement` now return metadata-only prompt-preflight, autonomy-policy, progress, and output-compaction receipts beside sanitized placement metadata.
  - Hostile renderer/source/API-auth/script fixtures are classified through the layout prompt-preflight envelope without leaking raw prompt/source/renderer/html/script/secret-looking values into serialized tool responses.

- `feat(capy-memory): mark public memory retrieval advisory`
  - Added RED/GREEN Memory Tree retrieval coverage proving `search_memory`, `/api/capy-memory/search`, `relevant_memory_for_space`, and `/api/spaces/memory` now expose explicit metadata-only advisory/no-authority envelopes.
  - `/api/capy-memory/search` now also exposes `memory_context` prompt-preflight and `space.memory.read` autonomy-policy receipts, rejects unsafe query/limit markers without echoing raw or encoded hostile values, and treats LIKE wildcards as literals.
  - Every public memory hit now carries `context_authority: untrusted_advisory`, `can_bypass_safety_gates: false`, and required gate labels for prompt-preflight, approval, sandbox preview, visual QA, and rollback/recovery, while hostile fixture strings remain absent from serialized output.

- `fix(capy-memory): fail-close GitHub PR-list raw hosts`
  - Added RED/GREEN Memory Tree source-refresh regression coverage for uppercase raw GitHub PR-list hosts before URL normalization.
  - Non-canonical PR-list routes now downgrade to local `capy-memory://` receipts and never fetch, create vault rows, or echo query/fragment/API-auth/raw-prompt markers.

- `feat(capy-memory): ingest GitHub environment variables metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub Actions environment variables API payloads (`/repos/{owner}/{repo}/environments/{environment_name}/variables`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, environment name, variable count, bounded variable names, and safe created/updated timestamps while omitting raw values, row/global URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads and lookalike-host route-shaped URLs fail closed without creating vault records or performing unsafe fetches.

- `feat(capy-memory): ingest GitHub environment private-name metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub environment private-name API payloads (`/repos/{owner}/{repo}/environments/{environment_name}/secrets`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, environment name, count, bounded safe names, and safe created/updated timestamps while omitting raw values, row/global URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Text fallback responses, lookalike-host route-shaped URLs, encoded path tricks, and malformed environment-secret routes fail closed without creating vault records or performing unsafe fetches.

- `feat(capy-memory): ingest GitHub Actions private-name metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub Actions repository private-name API payloads (`/repos/{owner}/{repo}/actions/secrets`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, count, bounded safe names, and safe created/updated timestamps while omitting raw values, row/global URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Text fallback responses and lookalike-host route-shaped URLs fail closed without creating vault records or performing unsafe fetches.

- `feat(capy-memory): ingest GitHub Actions public-key metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub Actions repository secrets public-key API payloads (`/repos/{owner}/{repo}/actions/secrets/public-key`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path and key id while omitting raw public-key material, row/global URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Text fallback responses and lookalike-host route-shaped URLs fail closed without creating vault records or performing unsafe fetches.

- `feat(capy-memory): ingest GitHub Dependabot public-key metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub Dependabot secrets public-key API payloads (`/repos/{owner}/{repo}/dependabot/secrets/public-key`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries and catalog display names from safe repository path and key id while omitting raw public-key material, raw fetch URLs in persisted job payloads, API-auth/query/fragment fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault/catalog output.
  - Malformed suffixes, lookalike hosts, explicit ports, unrelated encoded public-key paths, legacy requeue payloads, and non-JSON content fail closed without unsafe fetch or persistence; execution reconstructs the exact safe fetch URL transiently from the public alias.

- `feat(capy-memory): ingest GitHub Pages metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub Pages API payloads (`/repos/{owner}/{repo}/pages`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, Pages status/build type, public/custom-404/HTTPS booleans, safe CNAME, and protected-domain state while omitting raw URLs, source branch/path payloads, certificate descriptions/domains, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads, malformed Pages-shaped paths, legacy queued userinfo payloads, and non-JSON fallback content fail closed without creating vault records or performing unsafe fetches.

- `feat(capy-memory): ingest GitHub repository Actions artifacts metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub repository-level Actions artifacts API payloads (`/repos/{owner}/{repo}/actions/artifacts`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, artifact count, bounded artifact ids/names/sizes/expired flags, and safe timestamps while omitting archive/download/API/html URLs, workflow-run commit payloads, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads, unsafe userinfo authorities, and non-JSON fallback content fail closed without creating vault records or persisting raw artifact output.

- `feat(capy-memory): ingest GitHub Dependabot alerts metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub Dependabot alerts API payloads (`/repos/{owner}/{repo}/dependabot/alerts`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, alert count, bounded alert numbers/states/package ecosystem/package names/manifest paths/severities while omitting advisory summaries/descriptions, CVE/GHSA ids, vulnerable/patched version ranges, dismissed comments/reasons, raw URLs, API-auth fields, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads, unsafe tail rows, legacy queued userinfo payloads, non-JSON fallback content, and malformed Dependabot-alert routes fail closed without creating vault records or performing unsafe fetches.

- `feat(capy-spaces): complete structured progress delta taxonomy`
  - Progress status now accepts the roadmap's metadata-only non-content delta/event types: `thinking.delta`, `text.delta`, `tool.args.delta`, `subagent.spawned`, and `subagent.progress`.
  - Product-home Progress events now renders safe thinking/text family chips and recent-stream rows for those types while omitting raw thoughts, text deltas, tool args, prompts, renderer/source fields, API-auth fields, and secret-like fixture values from logs, status JSON, and DOM output.

- `feat(capy-memory): ingest GitHub repository events metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving exact GitHub repository Events API payloads (`/repos/{owner}/{repo}/events`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, event count, type counts, and bounded event id/type/actor/public/timestamp metadata while omitting event payloads, commit messages, repo/actor URLs, raw prompt/source/renderer/html/script/data fields, API-auth/query/fragment/userinfo markers, tokens, and secret-like fixture values from receipts/search/vault output.
  - Hardened the slice after review so unexpected raw/non-schema row keys, unsafe event-shaped hosts, legacy queued userinfo payloads, non-HTTPS/userinfo/explicit-port authorities, and cross-repo redirects fail closed before fetch/persistence.
  - JSON Feed bypass payloads, malformed event rows, and malformed event-shaped paths fail closed without creating vault records or fetching unsafe routes.

- `feat(capy-spaces): show system widget safety receipts`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the trusted system-widget add flow now preserves and renders backend `autonomy_policy`, `progress_event`, and `output_compaction` receipts after the widget manager refresh.
  - Kept the UI receipt metadata-only: hostile fixture renderer/script/API-auth/raw-prompt/secret fields in the route response remain absent from the DOM while the visible card shows action policy, required preflight, model-route hint, system-widget progress, and compaction evidence.

- `feat(spaces): record widget-event list progress`
  - Added RED/GREEN backend coverage proving widget-event list tool responses now emit metadata-only `tool.completed` progress receipts with safe `widget.events:<space_id>` run ids and thread those run ids into regenerated output-compaction evidence.
  - Kept inbox reads redacted and advisory-only: ignored request renderer/source/API-auth fields, scripts, tokens, secret-looking values, and persisted prompt-like event details are not exposed.

- `feat(capy-memory): ingest GitHub PR requested-reviewers metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub PR requested-reviewers API payloads (`/repos/{owner}/{repo}/pulls/{number}/requested_reviewers`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from PR number, reviewer/team counts, bounded safe reviewer logins, and bounded safe team slugs while omitting avatar/profile/API URLs, team descriptions, raw prompt/source/renderer/html/script/data fields, API-auth/query/fragment/userinfo markers, tokens, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed/text fallback payloads, malformed requested-reviewers-shaped paths, and non-string reviewer login/team slug rows fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub community profile metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed exact GitHub community-profile API payloads (`/repos/{owner}/{repo}/community/profile`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, health percentage, content-report flag, update timestamp, and bounded allow-listed community file name/path metadata while omitting raw URLs, descriptions, bodies, API-auth/query/fragment/userinfo markers, prompts, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed-looking bypass payloads, non-JSON fallback content, invalid/out-of-range health percentages, unsafe file name/path values, insecure HTTP origins, encoded/malformed path variants, and case-mismatched routes fail closed without creating vault records or performing unsafe fetches.

- `feat(capy-memory): ingest GitHub environments metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub repository environments API payloads (`/repos/{owner}/{repo}/environments`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, total environment count, bounded environment ids/names, and safe created/updated timestamps while omitting raw URLs, protection-rule/branch-policy payloads, descriptions, API-auth/query/fragment/userinfo markers, prompt/summary/body fields, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads and malformed tail rows fail closed without creating vault records.

- `feat(capy-spaces): classify browser-surface preflight payloads`
  - Receipt-only Browser Surface tool actions now run real `browser_surface` prompt-preflight classification over an internal-only corpus of high-risk browser payload fields before returning action-policy evidence.
  - Hostile URL/prompt/text/DOM/source/renderer/API-auth/token/ref/history payloads, including nested object values and camelCase aliases such as `apiKey`, `accessToken`, `rawPrompt`, `outerHTML`, and `typedText`, block with metadata-only categories; oversized or deeply nested high-risk payloads fail closed while browser execution remains disabled and approval-gated.
  - Responses, progress events, compaction receipts, and serialized test output still omit raw prompt text, URLs/query strings, DOM/script bodies, renderer/source fields, credentials, refs, typed text, and secret-like fixture values.

- `feat(capy-memory): ingest GitHub contents metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub repository contents API payloads (`/repos/{owner}/{repo}/contents` and `/repos/{owner}/{repo}/contents/{path...}`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, content path, item count, bounded item type/name/path rows, file sizes, and SHA prefixes while omitting raw/encoded file content, download/html/git/API URLs, link maps, API-auth/query/fragment/userinfo markers, prompt/summary/body fields, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - List and single-object payloads are supported; JSON Feed/text fallback bypasses, explicit-port authorities, percent-encoded route tricks, case-mismatched routes, unsafe tail rows, and malformed payloads fail closed without creating vault records or performing unsafe fetches.

- `feat(capy-memory): ingest GitHub collaborators metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub repository collaborators API payloads (`/repos/{owner}/{repo}/collaborators`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, collaborator count, and bounded collaborator login/id/role/site-admin metadata while omitting avatar/profile/API URLs, permissions maps, raw URLs, API-auth/query/fragment/userinfo markers, prompt/summary/body fields, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Empty lists are valid; JSON Feed/text fallback bypasses, host-spoof/noncanonical-authority legacy payloads, unsafe rows, non-string logins, non-integer ids, and malformed tail rows fail closed without creating vault records or performing unsafe fetches.

- `feat(capy-memory): ingest GitHub traffic clones metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub repository traffic clones API payloads (`/repos/{owner}/{repo}/traffic/clones`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, total clones, unique cloner counts, and bounded timestamped clone samples while omitting raw URLs, API-auth/query/fragment markers, prompt/summary/body fields, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads and malformed tail rows fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub traffic views metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub repository traffic views API payloads (`/repos/{owner}/{repo}/traffic/views`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, total views, unique visitor counts, and bounded timestamped view samples while omitting raw URLs, API-auth/query/fragment markers, prompt/summary/body fields, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads and malformed tail rows fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub subscribers metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub subscribers/watchers API payloads (`/repos/{owner}/{repo}/subscribers`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe repository path, subscriber count, and bounded safe subscriber logins while omitting avatar/profile/API URLs, API-auth/query/fragment markers, prompt-injection text, scripts, renderer/source/data fields, and secret-like fixture values from receipts/search/vault output.
  - Empty lists are valid; JSON Feed bypass payloads, unsafe logins, malformed ids, and malformed tail rows fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub README metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub README API payloads (`/repos/{owner}/{repo}/readme`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, README name/path, file size, and SHA prefix while omitting raw README content, encoding, download/html/git URLs, link maps, API-auth/query/fragment markers, prompt-injection text, scripts, renderer/source fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed/content-type bypass payloads, uppercase hosts, README-like suffix paths, and malformed payload rows fail closed without creating vault records or performing unsafe fetches.

- `feat(capy-memory): ingest GitHub issue labels metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub issue-label API payloads (`/repos/{owner}/{repo}/issues/{number}/labels`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, issue number, label count, bounded label names, colors, and default flags while omitting label descriptions, URLs, ids/node_ids, API-auth fields, query/fragment markers, prompt-injection text, scripts, renderer/source fields, and secret-like fixture values from receipts/search/vault output.
  - Empty label lists are valid; JSON Feed and text fallback bypass payloads plus malformed/hostile tail rows fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub release assets metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub release assets API payloads (`/repos/{owner}/{repo}/releases/{release_id}/assets`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, release id, asset count, bounded asset names, ids, sizes, download counts, states, content types, and timestamps while omitting download URLs, uploader objects, API-auth fields, raw prompts, scripts, renderer/source fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed/text fallback bypass payloads, encoded/case-mismatched release-assets-shaped path variants, and malformed tail rows fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub participation metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub repository participation stats API payloads (`/repos/{owner}/{repo}/stats/participation`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, week count, all/owner commit totals, and active-week count while omitting raw bodies, URLs, API-auth/query/fragment/userinfo markers, prompt-injection text, scripts, renderer/source fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads, malformed aggregate arrays, and malformed route variants fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub workflow-runs metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub Actions workflow-run list API payloads (`/repos/{owner}/{repo}/actions/runs`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from safe repository path, run count, bounded run ids/names/status/conclusion/event/run numbers/attempts/branches/SHA prefixes, and timestamps while omitting logs/jobs/html URLs, commit payloads, API-auth/query/fragment/userinfo markers, prompt-injection text, scripts, renderer fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads and malformed run rows fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub latest-release metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub latest-release API payloads (`/repos/{owner}/{repo}/releases/latest`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe repository path, release id, release name/tag, draft/prerelease flags, and publish timestamp while omitting release bodies, assets, URLs, API-auth/query/fragment markers, prompt-injection text, scripts, renderer fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads at the same endpoint fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub fork-list metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub fork-list API payloads (`/repos/{owner}/{repo}/forks`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe repository path, fork count, bounded safe fork full names, owner logins, default branches, and update timestamps while omitting fork descriptions, clone/profile/API URLs, API-auth/query/fragment markers, prompt-injection text, scripts, renderer/source fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed/generic text bypass payloads, malformed fork-shaped text paths (case mismatch, unsafe owner/repo, trailing slash, extra segment, encoded route/slash/leading-slash/NUL/question variants, or non-lowercase GitHub host), unsafe fork names/logins, malformed timestamps, and malformed tail rows fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub stargazer-list metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub stargazer-list API payloads (`/repos/{owner}/{repo}/stargazers`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe repository path, stargazer count, bounded safe logins, and `starred_at` timestamps when present while omitting avatar/profile/API URLs, API-auth/query/fragment markers, prompt-injection text, scripts, renderer/source/data/html fields, and secret-like fixture values from receipts/search/vault output.
  - Empty lists are valid; JSON Feed/generic JSON bypass payloads, malformed stargazer-shaped text paths (case mismatch, unsafe owner/repo, trailing slash, or extra segment), malformed timestamps, unsafe logins, and malformed tail rows fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub commit-status metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub commit status-list API payloads (`/repos/{owner}/{repo}/commits/{sha}/statuses`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe repository path, commit SHA prefix, status count, state counts, bounded status ids/contexts/creator logins, and timestamps while omitting status descriptions, target URLs, row URLs, avatar URLs, API-auth/query/fragment markers, prompt-injection text, scripts, renderer fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads and malformed/unsafe tail rows fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub deployment status metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub deployment-status API payloads (`/repos/{owner}/{repo}/deployments/{deployment_id}/statuses`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe repository path, deployment id, status count, state counts, bounded status ids/states/environments/creator logins, and timestamps while omitting status descriptions, target/log/deployment/repository/environment URLs, payloads, API-auth/query/fragment markers, prompt-injection text, scripts, renderer fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads at the same endpoint fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub deployment metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub deployment-list API payloads (`/repos/{owner}/{repo}/deployments`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe repository path, deployment count, bounded deployment ids/environments/refs/SHA prefixes/tasks/production-transient flags, and timestamps while omitting deployment descriptions, payloads, status URLs, creator URLs, API-auth/query/fragment markers, prompt-injection text, scripts, renderer fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads at the same endpoint fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub check-runs metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub check-runs API payloads (`/repos/{owner}/{repo}/commits/{sha}/check-runs`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe repository path, commit SHA prefix, check-run count, bounded check-run ids/names/statuses/conclusions, and timestamps while omitting output summaries, details/html URLs, pull-request rows, API-auth/query/fragment markers, prompt-injection text, script markers, renderer/source fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads and malformed/unsafe tail rows fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub PR-list metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub pull-request list API payloads (`/repos/{owner}/{repo}/pulls`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe repository path, PR count, bounded PR numbers/titles/states/draft flags, safe author logins, and timestamps while omitting raw PR bodies, head/base refs, pull-request nested payloads, URLs, API-auth/query/fragment markers, prompt-injection text, script markers, renderer/source/data fields, and secret-like fixture values from receipts/search/vault output.
  - Non-JSON fallback content, case-mismatched PR-list paths, JSON Feed bypass payloads, malformed/unsafe rows, and long raw titles carrying URL/query markers fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub issue-list metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub issue-list API payloads (`/repos/{owner}/{repo}/issues`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe repository path, issue count, bounded issue/pull-request numbers/titles/states/labels, and update timestamps while omitting raw bodies, HTML bodies, URLs, pull-request raw payloads, API-auth/query/fragment markers, prompt-injection text, script markers, renderer/source fields, and secret-like fixture values from receipts/search/vault output.

- `feat(capy-memory): ingest GitHub PR commit-list metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub pull-request commit-list API payloads (`/repos/{owner}/{repo}/pulls/{number}/commits`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from PR number, commit count, bounded commit SHA prefixes, first safe commit-message lines, author dates, and parent counts while omitting raw message bodies, emails, signatures, file paths, patches, HTML/API URLs, query/fragment auth markers, prompt-injection text, script markers, renderer/source fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads and malformed/unsafe tail rows fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub PR review-list metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub pull-request review-list API payloads (`/repos/{owner}/{repo}/pulls/{number}/reviews`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from PR number, review count, safe reviewer logins, allow-listed review states, review ids, and submitted timestamps while omitting review bodies, HTML/review URLs, commit ids, API-auth/query/fragment markers, prompt-injection text, raw prompt fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads and malformed/unsafe tail rows fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub milestone-list metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub milestone-list API payloads (`/repos/{owner}/{repo}/milestones`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe repository path, milestone count, bounded milestone numbers/titles/states/open and closed issue counts, due dates, and updated timestamps while omitting milestone descriptions, row URLs, API-auth/query/fragment markers, prompt-injection text, raw prompt fields, and secret-like fixture values from receipts/search/vault output.
  - JSON Feed bypass payloads, malformed rows, unsafe repo path segments, blocked title/state/timestamp values, invalid counts, and non-allow-listed hosts fail closed without creating vault records.
  - Source registration now also downgrades route-shaped lookalike hosts, malformed milestone tails, and encoded milestone suffixes to local `capy-memory://...` origins before jobs are queued, preventing invalid milestone sources from retaining raw API routes, query/fragment markers, tokens, or raw-prompt markers in durable source/job rows.

- `feat(capy-memory): ingest GitHub label-list metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub label-list API payloads (`/repos/{owner}/{repo}/labels`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from a non-URL labels origin label, safe repository path, label count, and bounded safe label names/colors/default flags while omitting label descriptions, row URLs, ids/node_ids, API-auth/query/fragment markers, prompt-injection text, script/html/source/data markers, and secret-like fixture fields from receipts/search/vault output.
  - JSON Feed bypass payloads, unsafe ignored fields including raw/body/code/content keys, URL-like label names, non-boolean default values, punctuation-obfuscated prompt/API-key label names, trailing-dot hosts, case-mismatched routes, unsafe repo path segments, and malformed/unsafe rows anywhere in the payload fail closed without creating vault records or performing a trailing-dot-host fetch.

- `feat(capy-memory): ingest GitHub release-list metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub release-list API payloads (`/repos/{owner}/{repo}/releases`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe repository path, release count, bounded release names/tags, draft/prerelease flags, and publish timestamps while omitting release bodies, assets, archive URLs, API-auth/query/fragment markers, prompt-injection text, scripts, renderer fields, and secret-like fixture fields from receipts/search/vault output.

- `feat(capy-memory): ingest GitHub commit-list metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub commit-list API payloads (`/repos/{owner}/{repo}/commits`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe repository path, commit count, bounded SHA prefixes, first safe commit-message lines, author dates, and parent counts while omitting raw message bodies, emails, signatures, avatar/html/API URLs, query/fragment auth markers, prompt-injection text, script markers, and secret-like fixture fields from receipts/search/vault output.

- `feat(capy-memory): ingest GitHub issue comments metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub issue and pull-request comments API payloads (`/repos/{owner}/{repo}/{issues|pulls}/{number}/comments`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the issue/PR number, comment count, safe commenter logins, comment ids, and timestamps while omitting comment bodies, HTML, reactions, URLs, author association, API-auth/query/fragment, prompt-injection, script, and secret-like fixture fields from receipts/search/vault output.

- `feat(capy-memory): ingest GitHub PR file-list metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub pull-request file-list API payloads (`/repos/{owner}/{repo}/pulls/{number}/files`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the PR number, file count, aggregate additions/deletions/changes, and bounded status counts while omitting filenames, previous filenames, patches, raw/content URLs, API-auth/query/fragment, prompt-injection, script, and secret-like fixture fields from receipts/search/vault output.

- `feat(capy-memory): ingest GitHub workflow-list metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub Actions workflow-list API payloads (`/repos/{owner}/{repo}/actions/workflows`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe repository path, workflow count, and a bounded prefix of workflow names/states/timestamps while omitting workflow paths/YAML/jobs, URLs, API-auth/query/fragment, prompt-injection, script, and secret-like fixture fields from receipts/search/vault output.

- `feat(capy-memory): ingest GitHub branch-list metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub branch-list API payloads (`/repos/{owner}/{repo}/branches`) produce metadata-only advisory summaries, including empty branch lists.
  - The parser reconstructs summaries from the safe repository path, branch count, bounded branch names, protected flags, and commit SHA prefixes while omitting branch protection details, URLs, API-auth/query/fragment, prompt-injection, script, and secret-like fixture fields from receipts/search/vault output.

- `feat(capy-memory): ingest GitHub repository topics metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub repository topics API payloads (`/repos/{owner}/{repo}/topics`) produce metadata-only advisory summaries, including empty topic lists.
  - The parser reconstructs summaries from the safe repository path, topic count, and bounded topic names while rejecting JSON Feed and HTML fallback bypasses plus malformed/unsafe names, and omits raw bodies, API-auth/query/fragment markers, prompt-injection, script, and secret-like fixture fields from receipts/search/vault output.

- `feat(capy-memory): ingest GitHub repository languages metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub repository languages API payloads (`/repos/{owner}/{repo}/languages`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the safe repository path, language count, total byte count, and a bounded top-language list while rejecting JSON Feed bypasses, unsafe path/language markers, malformed byte counts, API-auth/query/fragment markers, prompt-injection, script, and secret-like fixture fields from receipts/search/vault output.

- `feat(capy-memory): ingest GitHub tag metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub tags API payloads (`/repos/{owner}/{repo}/tags`) produce metadata-only advisory summaries, including empty tag lists.
  - The parser reconstructs summaries from bounded tag names, tag count, and commit SHA prefixes while omitting archive URLs, raw URLs, tag bodies, API-auth/query/fragment, prompt-injection, script, and secret-like fixture fields from receipts/search/vault output.
  - JSON Feed bypass payloads, unsafe repo path segments, and malformed/unsafe tag rows anywhere in the payload fail closed without creating vault records.

- `feat(capy-memory): ingest GitHub commit metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub commit API payloads (`/repos/{owner}/{repo}/commits/{sha}`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from the path-matched SHA prefix, first safe commit-message line, safe author/committer dates, parent count, changed-file count, and aggregate line stats while omitting emails, signatures, file paths, patches, raw URLs, API-auth/query/fragment, prompt-injection, raw message bodies, script, and secret-like fixture fields from receipts/search/vault output.

- `feat(capy-memory): ingest GitHub workflow jobs metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub Actions jobs-list API payloads (`/repos/{owner}/{repo}/actions/runs/{run_id}/jobs`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from allow-listed run id, total count, and a bounded prefix of safe job names/status/conclusion/timestamp fields while omitting job steps, runner labels/names, logs/html URLs, API-auth/query/fragment, prompt-injection, script, and secret-like fixture fields from receipts/search/vault output.

- `feat(capy-memory): ingest GitHub workflow-run refresh metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub Actions run API payloads (`/repos/{owner}/{repo}/actions/runs/{run_id}`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from allow-listed run id/name/status/conclusion/event/run-number/attempt/branch/SHA-prefix/timestamp fields while omitting logs/jobs/html URLs, commit messages, job payloads, API-auth/query/fragment, prompt-injection, script, and secret-like fixture fields from receipts/search/vault output.

- `feat(capy-memory): ingest GitHub branch refresh metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub branch API payloads (`/repos/{owner}/{repo}/branches/{branch}`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from allow-listed branch name, protected flag, and commit SHA prefix while omitting commit URLs, branch protection details, raw bodies, API-auth/query/fragment, prompt-injection, script, and secret-like fixture fields from receipts/search/vault output.

- `feat(capy-memory): ingest github repository metadata`
  - Added RED/GREEN Memory Tree source-refresh coverage proving allow-listed GitHub repository API payloads (`/repos/{owner}/{repo}`) produce metadata-only advisory summaries.
  - The parser reconstructs summaries from allow-listed repository name/description-or-fallback/default-branch/visibility/private/archived/count/topic/timestamp fields and omits raw body, URL, clone/homepage, API-auth/query/fragment, prompt-injection, script, and secret-like fixture fields from receipts/search/vault output.

- `feat(spaces): preflight source layout payloads`
  - Added RED/GREEN backend coverage proving `space.spaces.saveSpaceLayout`, `space.current.saveLayout`, and `space.spaces.rearrangeWidgets` run prompt-injection preflight over raw prompt-bearing payload fields before persistence or widget layout mutation.
  - Hostile prompt text is blocked before revisioning and is not persisted, while existing renderer/source/html/API-auth fixture redaction remains metadata-only.

- `feat(spaces): preflight checkpoint receipts`
  - Added RED/GREEN backend coverage proving Space checkpoint tool responses now include a top-level metadata-only `prompt_preflight` receipt alongside existing action-policy, `checkpoint:<space_id>` progress, and output-compaction evidence.
  - Kept rollback anchors advisory and redacted: hostile checkpoint reasons, renderer/source/html/API-auth fields, scripts, prompts, and secret-looking fixture values remain absent from returned receipts and persisted revision details.

- `feat(spaces): compact high-risk template install receipts`
  - Added RED/GREEN backend coverage proving direct, route, and tool-adapter Local Service Dashboard / Model Provider Setup installs now include metadata-only `output_compaction` receipts beside prompt-preflight, autonomy-policy, and progress evidence.
  - Added RED/GREEN real-`static/spaces.js` coverage proving Local Service Dashboard and Model Provider Setup install status cards render compaction evidence from the metadata-only receipt while hostile renderer/source/html/API-auth/token/password/secret fixture fields remain absent from the DOM.
  - The shared install receipt covers high-risk template installs (Browser Surface, Local Service Dashboard, Model Provider Setup, Game Sandbox, Music Sequencer) using only template, Space id, widget count, preflight, policy, and progress metadata; hostile renderer/source/html/API-auth/token/password/secret fixture fields remain absent.

- `feat(spaces): compact source collection receipts`
  - Added RED/GREEN backend coverage proving source-style Space list/collection/current read helpers include metadata-only `output_compaction` receipts beside sanitized Space summaries/details.
  - The receipts are reconstructed only from allow-listed action, Space count, Space id, and widget count metadata while omitting ignored renderer/source/API-auth fields, scripts, bearer strings, widget bodies, and secret-looking fixture values.

- `feat(spaces): preflight widget event list receipts`
  - Added RED/GREEN backend coverage proving widget-event list tool responses now include top-level metadata-only required prompt-preflight and autonomy-policy receipts beside safe event rows and regenerated output-compaction evidence.
  - Kept inbox reads redacted and advisory-only: ignored request renderer/source/API-auth fields, scripts, tokens, secret-looking values, and persisted prompt-like event details are not exposed.

- `feat(spaces): preflight revision history receipts`
  - Added RED/GREEN backend coverage proving source-style Space revision/history list tool responses now include top-level metadata-only required prompt-preflight and autonomy-policy receipts beside sanitized revision rows and compaction evidence.
  - Kept revision/history list responses redacted and advisory-only: ignored request renderer/source/API-auth fields, scripts, and secret-looking fixture values are not exposed.

- `feat(spaces): preflight module repair event list receipts`
  - Added RED/GREEN backend coverage proving recovery-module repair-event list tool responses now include top-level metadata-only prompt-preflight and autonomy-policy receipts beside safe event rows and existing output-compaction evidence.
  - Kept module repair-event lists redacted and advisory-only: no raw repair prompts, payloads, renderer/source/API-auth fields, scripts, bearer strings, or secret-looking fixture values are exposed.

- `feat(spaces): preflight repair event list receipts`
  - Added RED/GREEN backend coverage proving whole-Space repair-event list tool responses now include top-level metadata-only prompt-preflight and autonomy-policy receipts beside the safe event list and existing output-compaction evidence.
  - Kept list responses redacted and advisory-only: no raw repair prompts, payloads, renderer/source/API-auth fields, scripts, bearer strings, or secret-looking fixture values are exposed.

- `feat(spaces): compact source space read receipts`
  - Added RED/GREEN backend coverage proving source-style Space read helpers (`space.get`, `space.spaces.get`, `space.spaces.read`, `space.spaces.getSpace`, and `space.spaces.readSpace`) include metadata-only `output_compaction` receipts beside safe Space details.
  - Kept these read aliases non-navigational: no prompt-preflight, autonomy-policy, or progress-event receipt is emitted, while compaction evidence is reconstructed only from allow-listed action, Space id, and widget-count metadata.

- `feat(spaces): compact generic widget read receipts`
  - Added RED/GREEN backend coverage proving legacy generic `widget.list`, `widget.read`, and `widget.get` tool-route responses include metadata-only `output_compaction` receipts beside the safe widget summaries/details.
  - The receipts are reconstructed only from allow-listed action, Space id, and widget count metadata while omitting ignored renderer/source/API-auth fields, scripts, HTML event handlers, bearer strings, and secret-looking fixture values.

- `feat(spaces): compact native widget upsert receipts`
  - Added RED/GREEN backend coverage proving `upsert_widget(..., include_safety_receipts=True)` returns metadata-only `output_compaction` evidence beside prompt-preflight, autonomy-policy, and structured progress receipts.
  - The receipt is reconstructed only from allow-listed action, Space id, widget count, revision handle, policy/model-route status, and progress run metadata while omitting renderer/source/API-auth fields, scripts, bearer strings, and secret-looking fixture values.

- `feat(spaces): receipt local runtime no-op events`
  - Added RED/GREEN backend coverage proving `capy:ready` / `capy:resize` local widget runtime no-ops remain non-queued and omit widget event ids while returning metadata-only progress and output-compaction receipts.
  - Bounded long valid Space/widget ids into safe progress run ids and preserved hostile payload/prompt redaction, so local runtime handshakes can appear in progress/receipt evidence without persisting widget event payloads, renderer/source/API-auth fields, scripts, bearer strings, or secret-looking fixture values.

- `feat(spaces): compact module repair event receipts`
  - Added RED/GREEN backend coverage proving recovery-module repair-event list tool responses include metadata-only `output_compaction` evidence beside the safe event list.
  - The receipt is reconstructed only from allow-listed action, module id, event count, and event ids/names/statuses while omitting raw repair prompts, payloads, renderer/source/API-auth fields, scripts, bearer strings, and secret-looking fixture values.

- `feat(spaces): compact revision list receipts`
  - Added RED/GREEN backend coverage proving `space.revisions` and `space.current.revisions` tool responses include metadata-only `output_compaction` evidence beside sanitized revision/history lists.
  - The receipt is reconstructed only from allow-listed action, Space id, and public revision event ids while omitting ignored request payload markers, renderer/source/API-auth fields, scripts, bearer strings, widget bodies, and secret-looking fixture values.

- `feat(spaces): compact repair event list receipts`
  - Added RED/GREEN backend coverage proving whole-Space repair-event list tool responses include metadata-only `output_compaction` evidence beside the safe event list.
  - The receipt is reconstructed only from allow-listed action, Space id, event count, event ids/names/statuses, and optional active-Space id while omitting raw repair prompts, payloads, renderer/source/API-auth fields, scripts, bearer strings, and secret-looking fixture values.

- `feat(spaces): record shared data read progress`
  - Added RED/GREEN backend coverage proving shared data slot list/get tool responses emit metadata-only `tool.completed` progress events with safe `shared-slot.list:<space_id>` / `shared-slot.get:<space_id>` run ids.
  - Threaded those progress receipts into existing metadata-only output-compaction evidence while omitting raw slot request payloads, renderer/source/API-auth fields, scripts, bearer strings, and secret-looking fixture values from responses and progress logs.

- `feat(spaces): compact shared data read receipts`
  - Added RED/GREEN backend coverage proving shared data slot list/get tool responses include metadata-only `output_compaction` receipts.
  - The receipts are reconstructed only from allow-listed action and Space id metadata while omitting ignored request payload markers such as renderer/source/API-auth fields, scripts, bearer strings, and secret-looking fixture values.

- `feat(spaces): compact source refresh receipts`
  - Added metadata-only `output_compaction` evidence to manual and scheduled Memory Tree source-refresh route/tick receipts.
  - Receipts summarize only bounded counts, target source ids, prompt-preflight status, and model-route hints while omitting source-refresh origin URIs, fetched content, job internals, renderer/source/API-auth fields, raw prompts, scripts, and secret-looking fixture values.

- `feat(spaces): preflight development tool payloads`
  - Added RED/GREEN backend coverage proving receipt-only `space.development.terminal` / `space.development.shell` requests now classify common command/args/message/auth payload shapes through the `development_tool` prompt-preflight boundary.
  - The action-policy receipt now reflects the actual pass/block preflight status while the development boundary remains metadata-only and omits raw terminal commands, prompts, source/html/renderer/API-auth fields, scripts, bearer strings, and secret-looking fixture values.

- `feat(spaces): compact update safety receipts`
  - Added RED/GREEN backend coverage proving direct `/api/spaces/update` active-instruction safety receipts now include metadata-only `output_compaction` evidence beside prompt-preflight, autonomy-policy, and structured progress receipts.
  - The receipt is reconstructed only from allow-listed action, Space id, revision handle, policy/model-route status, and progress run metadata while omitting raw instruction text, renderer/source/html/API-auth fields, scripts, bearer strings, and secret-looking fixture values.

- `feat(spaces): preflight source-style Space creation`
  - Added RED/GREEN backend coverage proving `space.create` / `space.spaces.create` run `active_space_instructions` prompt preflight before persisting supplied create-time instructions.
  - Hostile create-time instruction injection is blocked before manifest creation with only a fixed safe error, while safe instruction creates return metadata-only prompt-preflight/action-policy/progress/compaction evidence and avoid echoing raw instruction text in the tool response.

- `feat(spaces): compact recovery module quarantine receipts`
  - Added RED/GREEN backend coverage proving `upsert_recovery_module(...)` returns a metadata-only `output_compaction` receipt beside its existing prompt-preflight, autonomy-policy, and structured progress evidence.
  - The quarantine receipt is reconstructed only from allow-listed action, recovery-module Space id, module id, revision handle, policy/model-route status, and progress run metadata while omitting raw module source/html/renderer/API-auth fields, raw prompts, scripts, bearer strings, and secret-looking fixture values.

- `feat(spaces): receipt-only development tool boundary`
  - Added RED/GREEN backend coverage proving `space.development.terminal` returns metadata-only development-surface, prompt-preflight, autonomy-policy, progress, and output-compaction receipts without executing commands or enabling filesystem writes.
  - The receipt is reconstructed only from allow-listed action, Space id, policy/model-route status, and progress run metadata while omitting raw terminal commands, prompts, source/html/renderer/API-auth fields, scripts, bearer strings, and secret-looking fixture values.

- `feat(capy-memory): ingest GitHub workflow refresh metadata`
  - Added RED/GREEN backend coverage proving the safe source-refresh fetcher can ingest allow-listed GitHub Actions workflow API JSON as metadata-only Memory Tree records when `api.github.com` is explicitly allow-listed.
  - The workflow record is reconstructed only from allow-listed workflow id, name, state, and created/updated timestamps while workflow paths/YAML/jobs, URLs/query tokens, body fields, API-key fields, script markers, prompt-injection text, and secret-looking fixture values remain absent from persisted vault Markdown, search results, and job receipts.

- `feat(capy-memory): ingest GitHub release refresh metadata`
  - Added RED/GREEN backend coverage proving the safe source-refresh fetcher can ingest allow-listed GitHub release API JSON as metadata-only Memory Tree records when `api.github.com` is explicitly allow-listed.
  - The release record is reconstructed only from allow-listed release id, name/tag, draft/prerelease flags, and publish timestamp while raw release bodies, HTML bodies, URLs/query tokens, API-key fields, script markers, prompt-injection text, and secret-looking fixture values remain absent from persisted vault Markdown, search results, and job receipts.

- `feat(capy-memory): ingest GitHub issue refresh metadata`
  - Added RED/GREEN backend coverage proving the safe source-refresh fetcher can ingest GitHub issue/PR API JSON as metadata-only Memory Tree records when `api.github.com` is explicitly allow-listed.
  - The GitHub record is reconstructed only from allow-listed issue/PR number, title, state, labels, and update timestamp while raw issue bodies, HTML bodies, query tokens, API-key fields, script markers, prompt-injection text, and secret-looking fixture values remain absent from persisted vault Markdown, search results, and job receipts.

- `feat(spaces): preflight layout repair receipts`
  - Added RED/GREEN backend coverage proving `space.spaces.repairLayout` now returns a metadata-only prompt-preflight receipt beside its action-policy, structured repair progress, and output-compaction evidence.
  - Threaded the actual preflight status into the layout repair action-policy receipt so successful sanitized layout repair reports `pass` rather than only `required`, while hostile renderer/source/API-auth/script/token/secret fixture markers remain absent from serialized responses.

- `feat(streaming): record tool start progress receipts`
  - Added RED/GREEN coverage proving WebUI streaming tool-start callbacks persist only metadata-only `tool.started` progress events under safe stream-scoped run ids.
  - The start/completion progress recorder omits hostile tool names, previews, args, command/path snippets, raw prompts, renderer/source/API-auth fields, scripts, bearer placeholders, and secret-looking fixture values while preserving visible progress lifecycle status.

- `feat(spaces): render runtime contract receipt evidence`
  - Added RED/GREEN backend coverage proving generic `widget.patch` tool-route responses now include metadata-only `output_compaction` beside prompt-preflight, action-policy, and `widget.patch:<space_id>` progress evidence.
  - Added RED/GREEN real-`static/spaces.js` coverage plus browser QA proving widget details render runtime-contract compaction evidence from the `space.widget.runtime_contract` receipt while hostile renderer/API-auth/script/secret fixture markers remain absent from the DOM.

- `feat(spaces): compact template reset receipts`
  - Added RED/GREEN backend coverage proving Big Bang template reset tool responses include metadata-only `output_compaction` receipts beside prompt-preflight, action-policy, and `template.reset:<space_id>` progress evidence.
  - Added RED/GREEN real-`static/spaces.js` coverage proving the reset status card renders compaction evidence while hostile renderer/source/API-auth/script/secret fixture markers remain absent from the DOM.

- `feat(spaces): compact checkpoint receipts`
  - Added RED/GREEN backend coverage proving Space checkpoint tool responses include metadata-only `output_compaction` receipts beside existing action-policy and `checkpoint:<space_id>` progress evidence.
  - The receipt is reconstructed only from allow-listed action, Space id, revision handle, policy/model-route status, and progress run metadata while omitting hostile checkpoint reasons, renderer/source/html/API-auth fields, scripts, bearer strings, and secret-looking fixture values.

- `feat(spaces): compact shared data receipts`
  - Added RED/GREEN backend coverage proving shared data slot set/delete tool responses include metadata-only `output_compaction` receipts beside existing prompt-preflight/action-policy/progress evidence.
  - The receipts are reconstructed only from allow-listed action, Space id, policy/model-route status, and progress run metadata while omitting raw slot values, metadata values, renderer/source/html/API-auth fields, scripts, bearer strings, and secret-looking fixture values.

- `feat(spaces): compact path helper receipts`
  - Added RED/GREEN backend coverage proving source-style logical storage path helpers return metadata-only `output_compaction` receipts beside their existing destructive-external-action policy and `path.helper:<space_id>` progress receipts.
  - The receipt is reconstructed only from allow-listed action, Space id, widget count, model-route hint, and progress run metadata while omitting raw logical paths, renderer/source/html/API-auth fields, scripts, tokens, and secret-looking fixture values.

- `feat(spaces): compact widget blueprint receipts`
  - Added RED/GREEN backend coverage proving source-style `defineWidget`, `createWidgetSource`, `previewWidgetRecord`, and `renderWidget` responses include metadata-only `output_compaction` receipts beside existing prompt-preflight, autonomy-policy, and structured progress evidence.
  - The receipts are reconstructed only from allow-listed action, Space id, widget count, safe revision handle where applicable, policy/model-route status, and progress run metadata while hostile renderer/source/html/script/API-auth/token/secret fixture markers and generated widget bodies remain absent from serialized responses.

- `feat(spaces): compact widget mutation receipts`
  - Added RED/GREEN backend coverage proving source-style/current widget patch, single delete, bulk delete, delete-all, and toggle responses include metadata-only `output_compaction` receipts beside existing prompt-preflight, autonomy-policy, and structured progress evidence.
  - The receipts are reconstructed only from allow-listed action, Space id, widget count, revision handles, policy/model-route status, and progress run metadata while hostile renderer/source/html/script/API-auth/token/secret fixture markers remain absent from serialized helper responses.

- `feat(spaces): compact widget upsert receipts`
  - Added RED/GREEN backend coverage proving source-style `space.spaces.upsertWidget` and `space.spaces.upsertWidgets` responses include metadata-only `output_compaction` receipts beside existing prompt-preflight, autonomy-policy, and structured progress evidence.
  - The receipt is reconstructed only from allow-listed action, Space id, widget count, safe revision handles, policy/model-route status, and progress run metadata while omitting raw prompts, widget bodies, renderer/source/html/script/data/API-auth fields, credentials, tokens, and secret-looking fixture values.

- `feat(spaces): compact navigation receipts`
  - Added RED/GREEN backend coverage proving source-style Space open/reload browser-navigation helpers return metadata-only `output_compaction` receipts beside existing required prompt-preflight, autonomy-policy, and structured progress evidence.
  - The receipts are reconstructed only from allow-listed action, Space id, widget count, policy/model-route status, progress run metadata, and a safe Space handle while hostile renderer/script/source/API-auth/token fixture markers remain absent from serialized helper responses.

- `feat(spaces): compact space recovery toggles`
  - Added RED/GREEN backend coverage proving whole-Space recovery disable/enable primitives return metadata-only `output_compaction` receipts beside existing prompt-preflight, autonomy-policy, and structured progress evidence.
  - The receipts are reconstructed only from allow-listed action, Space id, target kind, revision id, policy/preflight status, progress run metadata, and retained Space handle while omitting operator recovery reasons, renderer/source/API-auth fields, scripts, bearer strings, and secret-looking fixture values.

- `feat(spaces): compact widget read receipts`
  - Added RED/GREEN backend coverage proving source-style/current widget list, read/get, and see helper responses include metadata-only `output_compaction` receipts beside their existing widget summaries/details/contracts/events.
  - The receipts are reconstructed only from allow-listed action, Space id, widget count, and retained Space handle metadata while hostile request/manifest markers such as renderer/html/source/data/API-auth fields, scripts, tokens, prompts, and secret-looking fixture values remain absent from serialized responses.

- `feat(spaces): compact app URL helper receipts`
  - Added RED/GREEN backend coverage proving `space.spaces.resolveAppUrl` responses include metadata-only `output_compaction` receipts beside the existing browser-surface prompt-preflight, action-policy, and progress receipts.
  - The receipt is reconstructed only from allow-listed action/policy/progress metadata with `widget_count: 0`, while raw logical path request payloads, renderer/source/API-auth fields, scripts, credentials, prompt bodies, and secret-looking fixture values remain omitted.

- `feat(session): compact recovery repair receipts`
  - Added RED/GREEN backend coverage proving `/api/session/recovery/repair-safe` / `repair_safe_session_recovery(...)` returns a metadata-only `output_compaction` receipt beside existing prompt-preflight, autonomy-policy, and progress evidence.
  - The repair-safe receipt is reconstructed only from aggregate status/count/policy/progress fields, preserving clean/manual-review status and exit status while omitting local session paths, audit item details, scripts, API-auth fields, and secret-looking fixture values.

- `feat(spaces): compact reposition receipts`
  - Added RED/GREEN backend coverage proving source-style/current `repositionCurrentSpace`, `space.current.reposition`, and `space.current.reposition_viewport` responses include metadata-only `output_compaction` receipts beside existing prompt-preflight, autonomy-policy, and structured progress evidence.
  - The shared receipt preserves only allow-listed action, Space id, widget count, policy status/model-route hint, progress run/status metadata, and a safe Space handle while omitting raw viewport payloads, renderer/source/API-auth fields, prompts, scripts, and secret-looking fixture values.

- `feat(spaces): compact source-style layout helper receipts`
  - Added RED/GREEN backend coverage proving source-style/current `saveMeta`, `saveLayout`, `repairLayout`, and `rearrangeWidgets` responses include metadata-only `output_compaction` receipts beside existing prompt-preflight, autonomy-policy, and structured progress evidence.
  - The shared receipt preserves only allow-listed action, Space id, widget count, safe revision handles where applicable, policy status/model-route hint, and progress run/status metadata while omitting raw Space descriptions/instructions, widget titles/bodies, renderer/source/API-auth fields, prompts, scripts, layout payload secrets, and secret-looking fixture values.

- `feat(spaces): compact source-style duplicate/delete receipts`
  - Added RED/GREEN backend coverage proving `space.spaces.duplicateSpace` / `cloneSpace` and `space.spaces.removeSpace` / `deleteSpace` responses include metadata-only `output_compaction` receipts beside existing autonomy-policy and structured progress evidence.
  - The shared receipt keeps only allow-listed action, source/target/deleted Space ids, widget counts, safe revision-event ids, policy status/action/model-route hint, progress run/status, and retained Space/revision handles while omitting raw request payloads, Space descriptions/instructions, widget titles/bodies, renderer/source/API-auth fields, prompts, scripts, exception text, and secret-looking fixture values.

- `feat(spaces): preflight direct space updates`
  - Added RED/GREEN backend and route coverage proving direct `update_space` / `/api/spaces/update` agent-instruction writes run `active_space_instructions` prompt preflight before persistence, block hostile instruction injection without revision changes or raw prompt/secret leakage, and preserve safe instruction text after a passing preflight.
  - Added optional `includeSafetyReceipts` / `include_safety_receipts` support so direct update responses can return metadata-only prompt-preflight, autonomy-policy, and `space.update:<space_id>` progress receipts while the default route shape remains backward compatible, including string `"false"` handling.

- `feat(spaces): compact source-style space create receipts`
  - Added RED/GREEN backend coverage proving `space.create` tool responses include metadata-only `output_compaction` evidence beside existing autonomy-policy and structured progress receipts.
  - The create receipt keeps only safe action, Space id/name, widget-count, omitted-widget-payload counts, prompt-preflight/model-route status, and progress run metadata while omitting generated widget bodies, renderer/source/API-auth fields, scripts, and secret-looking fixture values.

- `feat(spaces): compact queued widget event receipts`
  - Added RED/GREEN backend coverage proving queued widget-event responses and read/list surfaces regenerate metadata-only `output_compaction` receipts without trusting forged persisted receipt text, event names, statuses, prompts, generated bodies, renderer/source/API-auth fields, or secret-looking fixture values.
  - Added RED/GREEN real-`static/spaces.js` coverage proving the queued widget-event inbox renders prompt-preflight and compaction evidence beside action-policy evidence while hostile fixture markers remain absent from the DOM.

- `feat(spaces): render creator visual QA progress receipts`
  - Added RED/GREEN real-`static/spaces.js` coverage proving creator commit success cards render backend `visual_qa_event` metadata-only progress evidence beside prompt-preflight, compaction, and action-policy receipts.
  - Reused the structured progress renderer so `space.visual_qa.completed` / `space.visual_qa` / safe `creator:<space_id>` run ids become visible without exposing raw prompts, generated code, renderer/source/API-auth fields, scripts, or secret-looking fixture values.

- `feat(spaces): compact camera stream receipts`
  - Added RED/GREEN backend coverage proving approved `space.camera.add_stream` / `camera.add_stream` tool responses include metadata-only `output_compaction` receipts beside the existing required prompt-preflight, action-policy, and structured progress evidence.
  - The camera compaction receipt preserves only safe action, Space/widget/stream ids, scheme/host-class/mixed-content, approval/preflight/policy/model-route, and progress run metadata while omitting raw camera hosts, ports, query tokens, renderer/source/API-auth fields, bearer strings, scripts, and secret-looking fixture values.

- `feat(spaces): add scheduled memory refresh UI`
  - Added RED/GREEN static-JS UI coverage proving the product-home Memory freshness card exposes a `Run scheduled refresh` action that posts to `/api/capy-memory/source/refresh/scheduled` with a bounded `{limit: 5}` payload.
  - Rendered the scheduled tick result as a metadata-only receipt with queued/processed counts, queued/completed job rows, prompt-preflight evidence, and `capy.memory.refresh.scheduled` action-policy evidence while hostile renderer/API-key/raw-prompt/script/secret/credential-like fixture markers remain absent from the DOM.

- `feat(spaces): add navigation preflight receipts`
  - Added RED/GREEN backend coverage proving source-style Space open/reload browser-navigation helpers return metadata-only `prompt_preflight` receipts beside existing autonomy-policy and structured progress evidence.
  - The new required-preflight receipts stay action-scoped, local-only, and raw-prompt-free while hostile renderer/script/source/API-auth/token fixture markers remain absent from serialized helper responses.

- `feat(spaces): require widget reload policy receipts`
  - Added RED/GREEN backend coverage proving widget reload/refresh aliases without a free-form prompt or reason still return metadata-only `required` prompt-preflight and action-policy receipts beside structured progress events.
  - Persisted the same required-preflight/action-policy evidence on queued widget refresh events while keeping hostile renderer/script/API-key/token fixture markers out of responses, event summaries, and progress telemetry.
  - Follow-up: queued widget reload/refresh events now return and list the server-generated Memory Tree advisory/no-authority envelope, thread that advisory boundary into compaction evidence, and ignore caller-forged memory authority so reload context cannot bypass prompt-preflight, approval, sandbox preview, visual QA, or rollback/recovery gates.

- `feat(spaces): add local service template policy receipts`
  - Added RED/GREEN backend coverage proving the Local Service Dashboard template install boundary returns metadata-only prompt-preflight and `space.template.install.local_service` autonomy-policy receipts for direct, route, and tool-adapter installs while preserving redaction of renderer/html/source/API-key/token/password/secret fields.

- `feat(spaces): add open navigation receipts`
  - Added RED/GREEN backend coverage proving Space Agent-style `space.spaces.open` / `openSpace` helpers now return metadata-only autonomy-policy receipts with destructive-external-action approval, required prompt-preflight status, `hint:fast` route evidence, and structured `space.open:<space_id>` progress receipts.
  - Added source-style current-space reload navigation receipts with safe `space.reload:<space_id>` progress telemetry while read/get aliases remain safe read-only responses and hostile renderer/html/source/API-auth/script/token/secret fixture values stay out of serialized responses.

- `feat(spaces): add path helper policy receipts`
  - Added RED/GREEN backend coverage proving Space Agent-style logical storage path helpers (`space.spaces.buildSpace*Path`) return metadata-only autonomy-policy receipts with destructive-external-action approval, required prompt-preflight status, `hint:fast` route evidence, and structured `path.helper:<space_id>` progress receipts.
  - Preserved existing virtual `~/spaces/...` logical path behavior while hostile renderer/html/source/API-auth/script/token/secret fixture values stay out of serialized responses and progress streams.

- `feat(spaces): add reposition policy receipts`
  - Added RED/GREEN backend coverage proving `space.spaces.repositionCurrentSpace` now returns metadata-only prompt-preflight, autonomy-policy, and `layout.reposition:<space_id>` progress receipts for browser/canvas viewport control requests.
  - Reused the layout safety receipt path so reposition stays non-executing and metadata-only while hostile viewport/source/renderer/API-auth/script/secret fixture values stay out of serialized responses and progress streams.

- `feat(spaces): require repair policy receipts`
  - Added RED/GREEN backend coverage proving whole-Space, widget, and module repair queues without free-form prompts still return metadata-only prompt-preflight-required and autonomy-policy receipts alongside structured repair progress.
  - Empty-prompt repair controls now expose the same visible safety envelope as prompted repairs; prompted repair queues also redact `prompt_preview`, so serialized responses/events/lists avoid raw prompts and hostile renderer/source/API-auth/script/secret fixture fields.
  - Validation at completion: focused RED failed on raw module prompt echo, focused GREEN passed, spec/quality reviews approved, full Spaces foundation suite passed (`567 passed`), `py_compile`, `git diff --check`, and `/tmp` real-static Visual/UI QA passed with no rendered DOM leaks. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_9b373a74d0e04cf4a6ec273a82320add.png`.

- `feat(policy): protect browser navigation preflight`
  - Added RED/GREEN policy coverage proving `browser_navigation` is now a central protected prompt-preflight boundary instead of normalizing to `unknown_boundary`.
  - Aligned policy status/direct preflight classification with existing Spaces open/reload navigation receipts while preserving metadata-only output, `raw_prompt_stored: false`, and raw prompt/secret omission.
  - Validation at completion: focused RED failed before implementation (`unknown_boundary`), focused GREEN passed (`2 passed`), full Capy policy suite passed (`17 passed`), focused Spaces navigation regression passed (`1 passed, 611 deselected`), `py_compile`, `git diff --check`, spec/quality reviews, backend receipt-envelope Visual/UI QA, and live local/tailnet health passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_0e12ec8a626e4a23b220e7fbaac942f2.png`.

- `feat(spaces): gate widget definitions`
  - Added RED/GREEN backend coverage proving `space.spaces.defineWidget` blocks hostile prompt-injection definitions plus wrapper/direct/nested unsafe field variants before exposing non-persisted blueprint metadata, without echoing raw prompt/system-prompt/secret fixture text or emitting false progress completion.
  - Aligned successful `defineWidget` responses with sibling blueprint helpers by returning metadata-only prompt-preflight, autonomy-policy, and `widget.blueprint.define:<space_id>` progress receipts while keeping generated renderer/html/script/source/data/API-auth markers out of serialized output.

- `feat(spaces): show checkpoint safety receipts`
  - Added RED/GREEN real-`static/spaces.js` coverage proving confirmed Space detail checkpoint actions render the backend-returned Action policy and Checkpoint progress receipts beside the rollback anchor.
  - Reused the existing action-policy and structured-progress renderers so checkpoint evidence shows supervised mode, model-route hint, safe `checkpoint:<space_id>` run id, and metadata-only progress status without exposing hostile reason text, raw prompts, generated widget bodies, renderer/source/API-auth fields, scripts, or secret-looking values.

- `feat(spaces): gate widget preview blueprints`
  - Added RED/GREEN backend coverage proving `space.spaces.previewWidgetRecord` now returns metadata-only prompt-preflight, autonomy-policy, and structured progress receipts before exposing a non-persisted widget blueprint preview.
  - Added hostile prompt-injection coverage proving preview blueprints fail closed before widget persistence or progress-event recording, while renderer/html/script/source/API-auth/raw-prompt and secret-looking fixture text stay omitted from errors and serialized responses.

- `feat(spaces): resolve model route receipts`
  - Added RED/GREEN backend coverage for safe configured route decisions, unsafe route fallback, credential-only route fallback, unknown-hint fallback, and metadata-only leak prevention in `resolve_model_route_hint(...)` / `action_policy_receipt(...)`.
  - Added real-`static/spaces.js` UI coverage and rendering so creator/action-policy receipts prefer `model_route_resolution`, show safe route/fallback details, and omit raw provider config, API-auth fields, renderer/source/script markers, prompts, and secret-looking fixture values.
  - Validation at completion: focused RED failed before implementation, reviewer-requested credential-only RED reproduced the configured/fallback classification bug, focused GREEN passed, full Capy policy, Spaces UI JS behavior, and Spaces foundation suites passed, syntax/diff checks passed, spec/quality reviews approved, and `/tmp` headless real-static Visual/UI QA passed. Screenshot artifact: `/tmp/capy-spaces-model-route-resolution-qa.png`.

- `feat(spaces): add layout repair safety receipts`
  - Added RED/GREEN backend coverage proving `space.spaces.repairLayout` returns a metadata-only autonomy-policy receipt with creator-commit approval, required prompt-preflight status, and `hint:fast` route evidence alongside its existing structured repair progress receipt.
  - Reused the layout action-policy receipt pattern so source-style layout repair now has visible safety evidence without exposing hostile renderer/source/API-auth/script/token fixture markers or weakening metadata-only repair progress telemetry.

- `feat(spaces): add space delete policy receipts`
  - Added RED/GREEN backend coverage proving `space.spaces.removeSpace` and `space.spaces.deleteSpace` now return metadata-only autonomy-policy receipts with creator-commit approval, required prompt-preflight status, and `hint:fast` route evidence after successful revisioned deletion.
  - Added structured `tool.completed` progress receipts with safe `space.delete:<space_id>` run ids plus fallback redaction coverage for secret-looking Space ids, while preserving hostile renderer/script/html/source/API-key/token leak checks.

- `feat(spaces): add shared data delete policy receipts`
  - Added RED/GREEN backend coverage proving `space.data.delete` returns a metadata-only `space.shared_slot.delete` autonomy-policy receipt with creator-commit approval, required prompt-preflight status, and `hint:summarize` route evidence alongside the existing structured progress receipt.
  - Reused the shared-data action-policy receipt path for `space.current.data.delete` as well, keeping delete telemetry metadata-only and preserving hostile renderer/script/API-key/secret fixture leak checks.

- `feat(spaces): show recovery disable widget receipts`
  - Added RED/GREEN real-`static/spaces.js` coverage proving confirmed Safe Recovery widget-disable actions prepend the returned metadata-only `Recovery action receipt` after the recovery panel refreshes.
  - Reused the shared recovery receipt renderer so widget-disable results expose supervised Action policy and structured Recovery progress evidence without rendering raw prompts, generated widget bodies, renderer/source/API-auth fields, scripts, or secret-looking values.

- `feat(spaces): show recovery disable receipts`
  - Added RED/GREEN real-`static/spaces.js` coverage proving confirmed Safe Recovery whole-Space disable actions prepend the returned metadata-only `Recovery action receipt` after the recovery panel refreshes.
  - Reused the shared recovery receipt renderer so disable-space results expose supervised Action policy and structured Recovery progress evidence without rendering raw prompts, generated widget bodies, renderer/source/API-auth fields, scripts, or secret-looking values.
  - Validation at completion: focused RED failed before implementation (`Recovery action receipt` missing), focused GREEN passed, related recovery UI tests passed (`5 passed, 196 deselected`), full Spaces UI JS behavior suite passed (`201 passed`), full demo parity suite passed (`12 passed`), full Spaces foundation suite passed (`515 passed`), `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, spec/quality reviews, and `/tmp` headless real-static Visual/UI QA passed with no rendered DOM leaks. Screenshot artifact: `/tmp/capy-spaces-progress/screens/recovery-disable-space-receipt.png`.

- `feat(spaces): show recovery restore receipts`
  - Added RED/GREEN real-`static/spaces.js` coverage proving confirmed Safe Recovery full-Space and widget revision restores prepend a metadata-only `Recovery action receipt` with Action policy and structured Recovery progress evidence after the recovery panel refreshes.
  - Reused the existing action-policy and progress receipt renderers with a recovery-specific heading so restore receipts expose supervised mode, approval gates, prompt-preflight status, model-route hint, safe run id, and metadata-only progress status without rendering or echoing raw prompts, generated widget bodies, renderer/source/API-auth fields, scripts, or secret-looking values.
  - Validation at completion: focused RED failed before implementation (`Recovery action receipt` missing), focused GREEN passed (`2 passed`), full Spaces UI JS behavior suite passed (`200 passed`), full Spaces foundation suite passed (`515 passed`), `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, spec/quality reviews, and a `/tmp` real-static headless Visual/UI QA harness passed with no rendered DOM leaks. Screenshot artifact: `/tmp/capy-spaces-qa/recovery-restore-policy-after.png`.

- `feat(spaces): scope package import policy receipts`
  - Added RED/GREEN backend coverage proving `space.import` tool-adapter package imports return an invoked-action autonomy-policy receipt (`autonomy_policy.action == "space.import"`) instead of collapsing all import evidence to the canonical direct helper name.
  - Passed the normalized import tool alias into the existing safe package-import policy receipt builder, so `space.import`, `space.package.import`, and `space.agent.import` remain distinguishable while preserving prompt-preflight status, `creator_commit` / `generated_widget_execution` approval gates, `hint:reasoning`, metadata-only receipts, and import quarantine/redaction behavior.
  - Validation at completion: focused RED failed before implementation (`autonomy_policy.action` was `space.agent.import`), focused GREEN passed, spec/quality reviews approved the slice, targeted package import/export tests and syntax/diff checks passed, and a `/tmp` real-static Visual/UI QA harness confirmed the import Action policy card renders without unsafe DOM leaks. Screenshot artifact: `/tmp/capy-spaces-progress/package-import-policy-qa.png`.

- `feat(spaces): surface package export policy receipts`
  - Added RED/GREEN backend coverage proving Space Agent package export responses include a metadata-only `autonomy_policy` receipt for `space.agent.export` with supervised mode, `creator_commit` and `generated_widget_execution` approval gates, required prompt-preflight status, `hint:reasoning` model-route evidence, and `metadata_only: true`.
  - Rendered the export Action policy evidence next to the package progress receipt in the real `static/spaces.js` export result while continuing to omit package YAML, widget files/bodies, renderer/source/API-auth fields, raw prompts, scripts, archive/zip payloads, and secret-looking fixture markers.
  - Validation at completion: focused RED failed before implementation (`autonomy_policy` missing), focused GREEN passed, package export/import and UI export safety regressions passed, `node --check static/spaces.js`, `py_compile api/spaces.py`, `git diff --check`, spec/quality reviews, and `/tmp` real-static headless Visual/UI QA passed with no visible unsafe DOM leaks. Screenshot artifact: `/tmp/capy-spaces-qa/package-export-policy.png`.

- `feat(spaces): add camera stream policy receipts`
  - Added RED/GREEN backend coverage proving `space.camera.add_stream` returns a metadata-only `autonomy_policy` receipt with action name, `destructive_external_action` approval gate, required prompt-preflight status, `hint:vision` model-route hint, and `metadata_only: true` after explicit approval.
  - Passed the invoked alias through the camera-stream tool path so receipts stay scoped to the actual action while raw private camera URLs, ports, bearer/token-like query data, renderer/script fields, API-auth fields, and secret-looking fixture markers remain absent from returned/stored metadata.
  - Validation at completion: focused RED failed before implementation (`autonomy_policy` missing), focused GREEN passed, targeted camera-stream regressions passed (`4 passed`), full Spaces foundation suite passed (`509 passed`), `py_compile`/`git diff --check` passed, and spec/quality reviews approved the slice.

- `feat(spaces): preflight save-meta instructions`
  - Added RED/GREEN backend coverage proving `space.spaces.saveSpaceMeta` / `space.current.saveMeta` run the `active_space_instructions` prompt-preflight boundary before persisting `agentInstructions` / `specialInstructions`.
  - Hostile instruction writes now fail closed without creating a new revision or replacing the prior safe instructions, while passing instruction writes return metadata-only prompt-preflight and action-policy receipts.
  - Validation at completion: focused RED failed before implementation (`DID NOT RAISE` / missing `prompt_preflight`), focused GREEN passed, full Spaces foundation suite passed, py_compile/diff checks passed, and backend probe/browser Visual/UI QA confirmed no unsafe instruction/renderer/API-auth leakage.

- `feat(spaces): record package progress events`
  - Added RED/GREEN backend coverage proving Space Agent package import/export boundaries return metadata-only `tool.completed` progress receipts with `package.import:<space_id>` and `package.export:<space_id>` run ids after successful sanitized package operations.
  - Hardened export ordering so unsupported package formats fail before recording completion telemetry, while successful import/export receipts omit raw YAML, renderer/source/API-auth fields, generated bodies, prompts, scripts, and secret-looking fixture markers.
  - Validation at completion: focused RED failed before implementation (`progress_event` missing), reviewer-requested invalid-format RED reproduced premature export progress recording, focused GREEN passed, full Spaces foundation suite passed, py_compile/diff checks passed, spec/quality review passed, and `/tmp` real-static browser Visual/UI QA confirmed the product-home progress stream surfaces package events without unsafe DOM leaks. Screenshot artifact: `/tmp/capy-progress-package-visual-qa.png`.

- `feat(spaces): preflight current instruction aliases`
  - Added RED/GREEN backend and policy coverage proving `space.current.agentInstructions` / `space.current.specialInstructions` run the `active_space_instructions` prompt-preflight boundary, return metadata-only action-policy receipts, and withhold hostile instruction text before direct agent-context injection.
  - Extended protected policy boundaries so active-space instructions are a first-class source/context boundary alongside creator, widget-runtime, repair, auto-fetched-source, and memory-context preflights.
  - Validation during slice: focused RED failed before implementation (`unknown_boundary` / missing `prompt_preflight`); follow-up RED caught the empty-instruction receipt gap; focused GREEN passed; full Capy policy and Spaces foundation suites passed; py_compile and diff checks passed.

- `feat(spaces): show single demo context status`
  - Added RED/GREEN real-`static/spaces.js` coverage proving an individual demo smoke receipt renders the shared metadata-only `Context layer status` card already used by run-all suite receipts.
  - Reused `renderContextLayerStatus(...)` inside `renderDemoSmokeResult(...)` so single-demo receipts expose bounded Memory, Autonomy/Preflight/Model hints, Progress, and allow-listed family counts without raw prompts, renderer/source fields, API-auth fields, unsafe progress families, scripts, or secret-looking fixture markers.
  - Validation at completion: focused RED failed before implementation (`Context layer status` missing from the single-demo receipt); focused GREEN passed; focused single-demo + run-all UI regressions passed (`2 passed`); full Spaces UI JS behavior suite passed (`191 passed`); `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, spec/quality reviews, and `/tmp` real-static Visual/UI QA passed with metadata-only leak checks. Screenshot artifact: `/tmp/capy-single-demo-context-status-qa.png`.

- `feat(spaces): record active context progress events`
  - Added RED/GREEN backend coverage proving `space.current.context` / `space.context` / `space.current.prompt_context` returns a metadata-only `tool.completed` progress receipt after successful active context retrieval.
  - Reused the shared Space-tool progress helper with a safe `context:<space_id>` run id so product-home and Space-scoped progress streams can show context retrieval without persisting raw prompts, renderer/source fields, API-auth fields, credentials, scripts, exception text, or secret-looking values.
  - Validation at completion: focused RED failed before implementation (`progress_event` missing from the context receipt); focused GREEN passed; full Spaces foundation suite passed (`494 passed`); progress suite passed (`13 passed`); `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality reviews, and live isolated browser Visual/UI QA passed with metadata-only leak checks. Screenshot artifact: `/tmp/capy-context-progress-qa/screens/context-progress-cdp.png`.

- `feat(spaces): record browser demo progress events`
  - Added RED/GREEN backend coverage proving the individual Browser Surface smoke emits metadata-only `run.started` / `run.completed` events on success and `run.started` / `run.failed` when the demo install path raises.
  - Wrapped `space_demo_run()` with safe `space-demo:<demo>` run ids and demo-slug Space ids without persisting raw prompts, renderer/source fields, API-auth fields, generated bodies, exception text, scripts, bearer tokens, or secret-looking fixture markers.
  - Fixed the product-home Progress events card mobile layout so its stats wrap inside narrow browser QA viewports; the `/tmp` real-static harness confirmed no console errors, no unsafe DOM leaks, no horizontal overflow, and readable aggregate run events. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_56b7f4a56f3a4636a2b09d217a4006ca.png`.

- `feat(spaces): record layout repair progress events`
  - Added RED/GREEN backend coverage proving source-style `space.spaces.repairLayout` emits a metadata-only `tool.completed` progress event with a Space-scoped `repair:<space_id>` run id after persisting safe repaired layouts.
  - Hardened the progress-event fallback receipt so recorder failures return only generic metadata, preserve `space_id` for scoped status consumers, and never expose renderer/source/API-auth fields, prompts, script markers, exception text, or secret-looking values.
  - Validation at completion: focused RED failed before implementation (`progress_event` missing from the repair receipt); focused GREEN passed; fallback RED/GREEN coverage passed; full Spaces foundation suite passed (`485 passed`); `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality reviews, and browser Visual/UI QA are recorded in the scheduled sprint report for this run.

- `feat(spaces): surface creator commit policy receipts`
  - Added RED/GREEN backend coverage proving `space.creator.commit` carries forward the preview `prompt_preflight` receipt and returns a commit-scoped `autonomy_policy` receipt after sandbox preview, visual-QA, and explicit approval gates pass.
  - Hardened the creator-loop preview receipt cache so commit can surface policy/preflight evidence without storing or returning raw prompts, generated bodies, renderer/source fields, API-auth fields, credentials, or unsafe screenshot paths.
  - Validation at completion: focused RED failed before implementation (`prompt_preflight` missing from commit receipt); focused GREEN passed; targeted creator preview/commit regressions, relevant UI receipt rendering coverage, browser Visual/UI QA, syntax/compile/diff checks, spec/quality reviews, and live health are recorded in the scheduled sprint report for this run.

- `feat(spaces): record demo suite progress events`
  - Added RED/GREEN backend coverage proving `space_demo_run_all()` emits metadata-only `run.started` / `run.completed` progress events for successful smoke-suite runs and records `run.failed` if post-demo processing fails, preventing stale active-run counts.
  - Wrapped the demo-suite smoke path with a fixed safe run id (`space-demo-suite:run-all`) and no raw demo output, prompts, widget bodies, renderer/source fields, API-auth fields, credentials, exception text, or secret-looking values in persisted progress records.
  - Validation at completion: focused RED failed before implementation (`run` family count missing) and failed-path RED reproduced stale `active_run_count == 1`; focused GREEN passed (`2 passed`); backend progress + Spaces foundation suites passed (`493 passed`); Spaces UI behavior + demo parity suites passed (`202 passed`); `py_compile api/spaces.py tests/test_spaces_foundation.py`, `node --check static/spaces.js`, `git diff --check`, spec/quality reviews, and `/tmp` real-static demo-suite progress browser QA passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_c55ed2a5678443dc821b3b67520a1556.png`.

- `fix(spaces): reject ambient package tool selectors`
  - Added RED/GREEN backend coverage proving non-current `space.import` and `space.export` reject same-valued ambient current selectors (`activeSpaceId` / `currentSpaceId`) before package creation/export handling, preserve candidate Spaces and event history, and avoid reflecting hostile renderer/source/API-auth/secret-looking fixture markers.
  - Hardened package Space-tool import/export branches to use the shared explicit non-current selector guard while preserving `space.current.export*` aliases through the current-space selector resolver.
  - Validation at completion: focused RED failed before implementation (`2 failed` with `DID NOT RAISE`); focused GREEN passed (`2 passed`); package Space-tool regressions passed (`4 passed, 366 deselected`); full Spaces foundation suite passed (`370 passed`); Spaces UI behavior + demo parity suites passed (`193 passed`); `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality reviews, and `/tmp` backend package-tool browser QA passed. Screenshot artifact: `/tmp/capy-spaces-progress/package-tool-ambient-qa.png`.

- `fix(spaces): reject nested runtime alias conflicts`
  - Added RED/GREEN backend coverage proving nested payload envelopes with conflicting capy runtime aliases such as `type: capy:ready` plus `messageType: capy:agent:prompt` fail closed before queueing widget events.
  - Hardened `_nested_payload_runtime_message_types(...)` to validate capy-shaped aliases within each nested payload object as a consistency set, mirroring the top-level runtime alias guard while continuing to ignore benign non-capy widget-local `type` labels.
  - Validation at completion: focused RED failed before implementation (`1 failed` with `DID NOT RAISE`); focused GREEN passed (`2 passed`); widget-event runtime regressions passed (`9 passed, 352 deselected`); full Spaces foundation suite passed (`361 passed`); Spaces UI behavior + demo parity suites passed (`188 passed`); `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality reviews, and `/tmp` real-static backend/UI QA harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/nested-runtime-alias-qa.png`.

- `fix(spaces): guard recovery module actions`
  - Added RED/GREEN real-`static/spaces.js` coverage proving forged recovery module disable/enable/repair clicks with an unsafe path/API-auth-looking module id fail closed before dialogs or POSTs.
  - Hardened static recovery module action handlers to path-sanitize `data-module-id` with the strict action-id helper before confirmation/prompt copy or route calls, and sanitized repair prompt placeholders to avoid reflecting unsafe module names.
  - Validation at completion: focused RED failed before implementation (`1 failed` with the unsafe id reflected in a dialog); focused GREEN passed (`1 passed`); targeted module UI regressions passed (`8 passed, 168 deselected`); Spaces UI behavior + demo parity suites passed (`188 passed`); `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, spec/quality reviews, and `/tmp` real-static safe-recovery module browser QA passed. Screenshot artifact: `/tmp/capy-spaces-progress/module-id-guard-qa.png`.

- `fix(spaces): reject ambient rollback route selectors`
  - Added RED/GREEN route coverage proving `POST /api/spaces/revision/restore` and `POST /api/spaces/revision/restore-widget` reject same-valued ambient current selectors before restoring a full Space or widget from revision history.
  - Hardened both direct revision restore route handlers with the shared non-current ambient-selector guard, matching recovery/research route behavior and the already-hardened Space-tool rollback aliases.
  - Validation at completion: focused RED failed before implementation (`1 failed` with status `200` instead of expected `400`); focused GREEN passed (`1 passed`); related rollback route regressions passed (`16 passed, 334 deselected`); full Spaces foundation suite passed (`350 passed`); Spaces UI behavior + demo parity suites passed (`187 passed`); `py_compile api/routes.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality reviews, and `/tmp` real-static recovery/rollback/sandbox browser QA passed. Screenshot artifact: `/tmp/capy-spaces-progress/recovery-rollback-sandbox.png`.

- `fix(spaces): reject ambient research selectors`
  - Added RED/GREEN backend coverage proving non-current Research Harness tool actions (`space.research.progress.set` and `space.research.artifact.set`) plus direct HTTP routes (`/api/spaces/research/progress` and `/api/spaces/research/artifact`) reject same-valued ambient current selectors before mutating progress widgets or artifact slots.
  - Hardened the Research tool branches to route non-current actions through the explicit non-current Space selector helper, and added ambient-current guards to the direct Research route handlers while preserving active-current `space.current.research.*` behavior.
  - Validation at completion: focused RED failed before implementation (`2 failed`); focused GREEN passed (`2 passed`); targeted Research regressions passed (`9 passed, 338 deselected`); full Spaces foundation suite passed (`347 passed`); Spaces UI behavior + demo parity suites passed (`185 passed`); `py_compile api/spaces.py api/routes.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality reviews, and `/tmp` real-static backend-only browser QA passed. Screenshot artifact: `/tmp/capy-spaces-progress/research-ambient-selector-qa.png`.

- `fix(spaces): reject ambient recovery route selectors`
  - Added RED/GREEN route coverage proving direct whole-Space recovery HTTP routes reject an explicit target `space_id` combined with ambient `activeSpaceId` before disabling, enabling, or queuing repair events; both target and ambient Spaces remain unchanged and hostile renderer/source/secret-looking fixture markers are not echoed.
  - Hardened the three direct recovery route handlers with a shared ambient-current selector guard reserved for non-current HTTP recovery/admin routes, keeping current-space selectors scoped to current-space paths.
  - Validation at completion: focused RED failed before implementation (`3 failed` with status `200` instead of expected `400`); focused GREEN passed (`3 passed`); related recovery route regressions passed (`6 passed, 332 deselected`); full Spaces foundation suite passed (`338 passed`); Spaces UI behavior + demo parity suites passed (`185 passed`); `node --check static/spaces.js`, `py_compile api/routes.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality reviews, and `/tmp` real-static recovery/admin browser QA passed. Screenshot artifact: `/tmp/capy-spaces-progress/recovery-ambient-route-qa.png`.

- `fix(spaces): reject ambient rollback selectors`
  - Added RED/GREEN backend coverage proving non-current rollback aliases (`space.recovery.rollback`, `space.safe_mode.restore`, `space.admin.rollback`, and `space.admin.recovery.restore`) and non-current widget-restore aliases (`space.recovery.restore_widget`, `space.safe_mode.restorewidget`, `space.admin.widget.rollback`, and `space.admin.recovery.restorewidget`) reject ambient current-space selectors before full-Space or widget restore side effects.
  - Hardened the rollback and widget-restore Space-tool branches with the shared ambient current-selector rejection used by repair/quarantine paths, preserving explicit `space_id`/`spaceId`, conflict checks, metadata-only receipts, and pure positional recovery widget restore compatibility.
  - Validation at completion: focused RED failed with expected `DID NOT RAISE` failures (`2 failed`); focused GREEN passed (`2 passed`); related rollback/recovery/widget-restore regressions passed (`19 passed, 316 deselected`); full Spaces foundation suite passed (`335 passed`); Spaces UI behavior + demo parity suites passed (`185 passed`); `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality reviews, and `/tmp` real-static recovery/admin browser QA passed. Screenshot artifact: `/tmp/capy-spaces-progress/noncurrent-rollback-alias-qa.png`.

- `fix(spaces): add safe mode repair aliases`
  - Added RED/GREEN backend coverage proving `space.safe_mode.repair` accepts Space-Agent-style `spaceId`, queues a metadata-only whole-Space `agent.repair` event, and `space.safe_mode.space_repair_events` lists that event through the same sanitized repair-event path.
  - Hardened `run_space_tool(...)` by extending the existing safe recovery repair queue/list allowlists rather than creating a parallel sanitizer path; safe-mode repair receipts omit active-current metadata and redact generated renderer/source/API-auth markers, script tags, prompt/body/session sentinels, and secret-looking values.
  - Validation at completion: focused RED failed with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`); targeted safe-mode/current/admin/recovery repair regressions passed (`7 passed, 324 deselected`, reviewer recheck `5 passed`); full Spaces foundation suite passed (`331 passed`); `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality subagent reviews, and `/tmp` real-static browser QA leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/safe-mode-repair-alias-qa.png`.

- `fix(spaces): sanitize session route receipts`
  - Added RED/GREEN backend route coverage proving `/api/spaces/activate` and `/api/spaces/deactivate` omit pending prompt/draft metadata from session receipts, and `/api/spaces/create-from-session` accepts camelCase `sessionId` while rejecting conflicting `session_id` / `sessionId` aliases before creating or activating a Space.
  - Hardened Capy Spaces session receipts to strip `pending_user_message`, `pending_attachments`, and `composer_draft`, preserving only compact safe session metadata such as `session_id`, `active_space_id`, counters, and timestamps.
  - Validation at completion: focused RED failed in a clean worktree with three expected failures; focused GREEN passed (`3 passed`); full Spaces foundation suite passed (`325 passed`); Spaces UI behavior + demo parity suites passed (`185 passed`); `py_compile api/routes.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality reviews, and `/tmp` real-static browser QA passed. Screenshot artifact: `/tmp/capy-spaces-progress/session-receipt-route-qa.png`.

- `fix(spaces): harden revision list tool aliases`
  - Added RED/GREEN backend Space-tool coverage proving `space.revisions` accepts camelCase `spaceId`, returns bounded metadata-only revision history, and rejects conflicting `space_id` / `spaceId` plus explicit-vs-positional `args[0]` aliases before selecting a Space. Current-space revision-list aliases now also reject conflicting active/current selectors vs `args[0]`.
  - Hardened the revision-list tool branch to reuse the shared Space selector alias resolver and validate positional selector consistency before listing revisions, bringing tool rollback discovery parity with the already-hardened `/api/spaces/revisions` route.
  - Validation at completion: focused RED failed before implementation (`Invalid space_id`) and reviewer-requested positional/current conflict REDs failed as expected (`DID NOT RAISE`); focused GREEN passed (`2 passed`); related revision tool/route/rollback regressions passed (`4 passed, 309 deselected`); full Spaces foundation suite passed (`313 passed`); Spaces UI behavior + demo parity suites passed (`185 passed`); `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, and `/tmp` real-static revision-list browser QA passed. Screenshot artifact: `/tmp/capy-spaces-progress/revision-list-tool-alias-qa.png`.

- `fix(spaces): verify revision snapshot ownership`
  - Added RED/GREEN backend rollback coverage proving full-Space restore rejects mismatched, missing, and malformed/whitespace-padded snapshot `space_id` values before mutation, and widget restore rejects mismatched snapshot ownership before replacing a widget.
  - Hardened revision event summaries to emit metadata-only restore previews/diffs only when the stored snapshot belongs exactly to the requested Space, and kept Space-detail plus recovery-panel revision summaries without restore previews non-actionable so unowned/foreign rollback snapshots cannot appear as actionable restore controls.
  - Validation at completion: focused RED failed before implementation for mismatched, missing, malformed, Space-detail non-actionable, and recovery-panel non-actionable unowned snapshot ownership; focused GREEN passed; full Spaces foundation + UI behavior + demo parity suites passed (`497 passed`); `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, spec/quality reviews, and `/tmp` real-static recovery browser QA passed. Screenshot artifact: `/tmp/capy-spaces-progress/revision-snapshot-ownership-qa.png`.

- `fix(spaces): ignore blank widget event tool aliases`
  - Added RED/GREEN backend tool-adapter coverage proving `run_space_tool("space.widget.event", ...)` ignores a blank `event_name` when a valid camelCase `eventName` is present, queues exactly one metadata-only `agent.prompt` event, and omits generated renderer/API-auth/secret-looking fixture markers from queued receipts/events.
  - Hardened the Space-tool widget-event adapter to strip blank event-name aliases before consistency checks, keep conflicting nonblank `event_name` / `eventName` aliases fail-closed, and pass the normalized event name into the canonical `queue_widget_event(...)` runtime/safety path.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN/regression set passed (`6 passed`); full Spaces foundation suite passed (`302 passed`); `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality reviews, and `/tmp` real-static browser QA harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/event-tool-blank-alias-qa.png`.

- `fix(spaces): ignore blank widget event route aliases`
  - Added RED/GREEN backend route coverage proving `POST /api/spaces/widget/event` ignores blank `space_id` / `widget_id` / `event_name` aliases when matching `spaceId` / `widgetId` / `eventName` are valid, queues exactly one metadata-only `agent.prompt` event, and omits generated renderer/API-auth/secret-looking fixture markers from route receipts/events.
  - Hardened the direct widget-event route to reuse the shared route selector resolver for Space/widget IDs, strip blank event-name aliases before consistency checks, keep conflicting nonblank event aliases fail-closed, and reject explicit non-object payloads before the canonical `queue_widget_event(...)` path.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); widget-event route regressions passed (`10 passed, 291 deselected`); full Spaces foundation suite passed (`301 passed`); Spaces UI behavior + demo parity suites passed (`183 passed`); `py_compile api/routes.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality reviews, and `/tmp` backend route-alias browser QA passed.

- `fix(spaces): reject conflicting recovery widget positional aliases`
  - Added RED/GREEN backend coverage proving `space.recovery.disable_widget` rejects mismatched named Space selectors versus source-style positional `args[0]`, and `space.recovery.enable_widget` rejects mismatched named widget selectors versus positional `args[1]`, before recovery side effects.
  - Hardened the shared recovery/admin/current widget quarantine tool branches to validate explicit `spaceId`/`widgetId` values against `[space_id, widget_id]` positional args only for adapters whose positional contract is actually Space + widget, preserving legacy single positional widget args with a named Space.
  - Validation at completion: focused RED failed before implementation (`2 failed`); focused GREEN passed (`2 passed`); related recovery/widget restore regressions passed (`7 passed, 293 deselected`); full Spaces foundation suite passed (`300 passed`); Spaces UI behavior + demo parity suites passed (`182 passed`); `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality reviews, and `/tmp` real-static recovery/admin browser QA passed.

- `fix(spaces): reject conflicting creator commit receipt aliases`
  - Added RED/GREEN backend coverage proving `space.creator.commit` rejects conflicting `preview_id` / `previewId` receipt aliases before consuming either cached preview or creating/updating a Space.
  - Hardened the creator commit receipt lookup path to reuse the shared alias-consistency resolver before cache pruning/pop, preserving both receipts on conflict and keeping the generic creator loop fail-closed before durable writes.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); targeted creator preview/commit regressions passed (`26 passed, 272 deselected`); full Spaces foundation suite, UI/demo suites, syntax/compile/diff checks, browser QA, and live health are recorded in the scheduled sprint report for this run.

- `fix(spaces): reject conflicting creator preview target aliases`
  - Added RED/GREEN backend coverage proving `space.creator.preview` rejects conflicting target Space selector aliases before creating a preview receipt, ignores blank higher-priority aliases while resolving a valid explicit target, leaves both candidate Spaces unchanged, and keeps generated renderer/API-auth/secret-looking fixture markers out of visible creator/recovery QA surfaces.
  - Hardened the creator-loop target selector helper to treat `target_space_id`, `targetSpaceId`, `space_id`, and `spaceId` as one consistency set before preview draft sanitization and receipt storage, while stripping blanks so empty aliases do not displace valid Space-Agent-style camelCase targets.
  - Validation at completion: focused RED failed before implementation (`1 failed` for conflicts; `1 failed` for blank-alias resolution); focused GREEN passed (`2 passed`); creator preview/commit regressions passed (`24 passed, 273 deselected`); full Spaces foundation suite passed (`297 passed`); Spaces UI behavior + demo parity suites passed (`182 passed`); `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, and `/tmp` real-static creator target alias browser harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/creator-target-alias-qa.png`.

- `fix(spaces): harden package route aliases` *(latest committed before this slice)*
  - Hardened direct Space Agent package import/export route selector aliases so camelCase `spaceId` is accepted and conflicts are rejected before package side effects.

- `fix(spaces): harden update route aliases`
  - Added RED/GREEN route coverage proving `POST /api/spaces/update` accepts camelCase `spaceId`, updates only the selected Space, and rejects conflicting `space_id` / `spaceId` selectors before mutating either candidate Space.
  - Hardened the direct HTTP Space update route to use the shared Capy route selector alias resolver, aligning this revision-producing metadata mutation route with recently hardened delete/research/shared-data/system-widget/safe-GET route boundaries.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); full Spaces foundation suite passed (`286 passed`); Spaces UI behavior + demo parity suites passed (`182 passed`); `node --check static/spaces.js`, `py_compile api/routes.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality review, and `/tmp` real-static update-route browser harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/update-route-alias-qa.png`.

- `fix(spaces): harden research route aliases`
  - Added RED/GREEN route coverage proving `POST /api/spaces/research/progress` and `POST /api/spaces/research/artifact` accept camelCase `spaceId`, preserve metadata-only sanitized progress/export receipts, and reject conflicting `space_id` / `spaceId` selectors before mutating either candidate Research Space.
  - Hardened both direct HTTP Research Harness routes to use the shared Capy route selector alias resolver, aligning Research E2E adapter entrypoints with the recent rollback/recovery/widget/delete alias consistency work.
  - Validation at completion: focused RED failed before implementation (`2 failed`); focused GREEN passed (`2 passed`); full Spaces foundation suite passed (`280 passed`); Spaces UI behavior + demo parity suites passed (`182 passed`); `py_compile api/routes.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality review, and `/tmp` real-static research route-alias browser harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/research-route-alias-qa.png`.

- `fix(spaces): harden delete route aliases`
  - Added RED/GREEN route coverage proving `POST /api/spaces/delete` accepts camelCase `spaceId`, deletes only the selected Space, and rejects conflicting `space_id` / `spaceId` selectors before deleting either candidate Space.
  - Hardened the direct HTTP Space delete route to use the shared Capy route selector alias resolver, aligning this destructive route with recovery/rollback/widget mutation route alias consistency.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); full Spaces foundation suite passed (`278 passed`); Spaces UI behavior + demo parity suites passed (`182 passed`); `node --check static/spaces.js`, `py_compile api/routes.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality review, and `/tmp` real-static delete-route browser harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/delete-route-alias-qa.png`.

- `fix(spaces): harden system widget route aliases`
  - Added RED/GREEN route coverage proving `POST /api/spaces/system-widget/upsert` accepts camelCase `spaceId`, keeps trusted system-widget receipts metadata-only, and rejects conflicting `space_id` / `spaceId` selectors before adding a system widget to either candidate Space.
  - Hardened the direct HTTP system-widget upsert route to use the shared Capy route selector alias resolver, aligning the trusted system-panel route with recently hardened widget upsert/patch/delete, restore, and recovery routes.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); full Spaces foundation suite passed (`277 passed`); Spaces UI behavior + demo parity suites passed (`182 passed`); `node --check static/spaces.js`, `py_compile api/routes.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality review, and `/tmp` real-static route-alias browser harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/system-widget-route-alias-qa.png`.

- `fix(spaces): reject conflicting recovery widget route ids`
  - Added RED/GREEN route coverage proving `POST /api/spaces/recovery/disable-widget` and `POST /api/spaces/recovery/enable-widget` reject conflicting top-level `id` / `widgetId` aliases before recovery state changes, preserve metadata-only public responses, and keep generated renderer/source/API-auth/secret-looking markers out of recovery summaries.
  - Hardened the direct HTTP recovery widget quarantine routes to resolve `widget_id`, `widgetId`, and `id` as one consistency set through the shared route-alias helper, aligning safe recovery/admin controls with the already-hardened widget restore route.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); full Spaces foundation suite passed (`270 passed`); Spaces UI behavior + demo parity suites passed (`182 passed`); `py_compile api/routes.py tests/test_spaces_foundation.py`, `git diff --check`, and `/tmp` real-static recovery/admin browser harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/recovery-widget-id-alias-qa.png`.

- `fix(spaces): reject conflicting restore-widget route ids`
  - Added RED/GREEN route coverage proving `POST /api/spaces/revision/restore-widget` rejects conflicting top-level `id` / `widgetId` aliases before restoring widget metadata, leaves both candidate widgets unchanged, and keeps public route responses/details free of generated renderer/source/API-auth/secret-looking markers.
  - Hardened the direct HTTP restore-widget route to resolve `widget_id`, `widgetId`, and `id` as one consistency set through the shared route-alias helper, aligning it with the recently hardened Space-tool rollback adapter behavior.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); related route/tool alias regressions passed (`6 passed, 264 deselected`); full Spaces foundation suite passed (`270 passed`); Spaces UI behavior + demo parity suites passed (`182 passed`); `node --check static/spaces.js`, `py_compile api/routes.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality review, and `/tmp` real-static recovery route-alias browser harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/restore-widget-route-id-alias-qa.png`.

- `fix(spaces): reject conflicting widget restore aliases`
  - Added RED/GREEN backend coverage proving `space.recovery.restore_widget` rejects conflicting `widget_id` / `widgetId` selectors and `id` / positional widget selector conflicts before restoring a widget revision, leaving current widget metadata unchanged and keeping public detail reads free of generated renderer/API-auth/secret-looking markers.
  - Hardened the shared Space-tool widget selector resolver so widget restore branches validate `widget_id`, `widgetId`, `id`, and positional `args[2]` as a consistency set before side effects, while preserving pure positional restore calls.
  - Validation at completion: focused RED failed before implementation (`1 failed`), follow-up `id` vs positional RED failed before the review-gap fix (`1 failed`); focused GREEN passed (`2 passed`); rollback/admin/widget-restore regressions passed (`9 passed, 260 deselected`); full Spaces foundation suite passed (`269 passed`); Spaces UI behavior + demo parity suites passed (`182 passed`); `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality review, and `/tmp` real-static recovery widget-restore browser harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/widget-restore-alias-qa.png`.

- `fix(spaces): reject conflicting rollback event aliases`
  - Added RED/GREEN backend coverage proving `space.admin.rollback` rejects conflicting `event_id` / `revisionEventId` and named-vs-positional revision-event selector aliases before restoring an older revision, leaves the current Space manifest unchanged, and does not expose generated renderer/API-auth/secret-looking markers in public details.
  - Hardened the shared `_space_tool_event_id(...)` resolver so recovery/admin/current rollback and widget-restore tool branches treat named plus positional revision-event aliases as a consistency set before side effects, while preserving pure positional widget restore.
  - Validation at completion: focused RED failed before implementation (`1 failed`), follow-up positional RED failed for named-vs-`args[1]` conflicts (`2 failed`); focused GREEN passed (`3 passed`); rollback/admin/widget-restore regressions passed (`11 passed, 256 deselected`); full Spaces foundation suite passed (`267 passed`); Spaces UI behavior + demo parity suites passed (`182 passed`); `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality review, and `/tmp` backend recovery/admin rollback browser harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/rollback-event-alias-qa.png`.

- `fix(spaces): redact recovery space display metadata`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the safe recovery panel redacts unsafe standalone Space `name`/`description` display metadata such as `source Space` and `data panel`, preserves the safe `Space ID: broken` operational anchor, and keeps recovery actions visible.
  - Hardened `renderRecoverySnapshot(...)` with a recovery-specific display sanitizer layered on the existing display-metadata redactor so recovery/admin Space cards omit standalone generated-body/source/html/script/data markers without broadening public product-home redaction.
  - Validation at completion: focused RED failed before implementation (`1 failed`), expanded review-gap RED failed for standalone `source`/`data` display markers (`1 failed`), focused GREEN passed (`1 passed`), Spaces UI behavior + demo parity suites passed (`182 passed`), full Spaces foundation suite passed (`265 passed`), `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, spec/quality reviews, and `/tmp` real-static recovery display metadata browser harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/recovery-space-display-metadata-qa.png`.

- `fix(spaces): honor bounded route history limits`
  - Added RED/GREEN route coverage proving `/api/spaces/revisions?limit=2` and `/api/spaces/widget/events?limit=1` return bounded newest-first metadata windows instead of overfetching entire rollback/event histories.
  - Wired the GET route adapters through the existing lower-level `list_revision_events(..., limit=...)` and `list_widget_events(..., limit=...)` clamps, preserving selector validation, default behavior, and metadata-only safety.
  - Validation at completion: focused RED failed before implementation (`2 failed`); focused GREEN passed (`2 passed`); full Spaces foundation suite passed (`263 passed`); Spaces UI behavior + demo parity suites passed (`180 passed`); `node --check static/spaces.js`, `py_compile api/routes.py tests/test_spaces_foundation.py`, `git diff --check`, and `/tmp` route-limit browser QA harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/route-limit-qa.png`.

- `fix(spaces): polish product home empty state`
  - Added RED/GREEN real-`static/spaces.js` coverage for an enabled but empty Capy Spaces product home, proving the empty-state grid is action-rich, avoids literal Material icon words, keeps resource links accessible, and does not render generated widget/source/API-auth/secret-looking content.
  - Replaced literal `open_in_new`/Material icon labels with safe glyphs, added explicit first-Space/research/kanban empty-state actions, improved the welcome close-button hit area, and renamed the recovery hard-gate copy to `Safe recovery controls` / `Generated widget execution: disabled` for clearer safe-mode messaging.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`4 passed`); Spaces UI behavior + foundation suites passed (`428 passed`); full WebUI suite passed (`5779 passed, 2 skipped, 3 xpassed, 8 subtests passed`); `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, local `/health`, browser console, and visual QA passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_9646e732b6d042dea2c55df19c73cc1d.png`.

- `fix(spaces): block events for recovery-disabled targets`
  - Added RED/GREEN backend coverage proving `queue_widget_event(...)` rejects both widget-level and whole-Space recovery-disabled targets and does not persist `widget.event.queued` records for those attempts.
  - Hardened the shared widget-event queue path used by runtime/postMessage adapters and HTTP/tool routes to check trusted recovery quarantine state after selector validation and before local no-op or durable event handling.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); targeted widget-event/recovery regressions passed (`23 passed, 236 deselected`); full Spaces foundation plus UI/demo validation, syntax/compile/diff checks, spec/quality reviews, and `/tmp` backend recovery-event browser harness QA are recorded in the scheduled sprint report for this run.

- `fix(spaces): redact recovery revision labels`
  - Added RED/GREEN real-`static/spaces.js` coverage proving a recovery snapshot with a hostile top-level `revision_event_id` renders `Revision: [REDACTED]` and omits path-like/API-auth/secret-looking marker text from `#capySpacesRecovery`.
  - Hardened `renderRecoverySnapshot(...)` to reuse the strict Space revision label helper for recovery/admin Space cards instead of directly escaping and displaying backend-provided `revision_event_id` values.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); related recovery panel regressions passed; Spaces UI behavior + demo parity suites, `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, spec/quality subagent reviews, and `/tmp` real-static recovery revision browser harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/recovery-revision-label-qa.png`.

- `fix(spaces): block creator commits without confirm dialog`
  - Added RED/GREEN real-`static/spaces.js` coverage proving that a creator preview with both sandbox and visual-QA gates checked still fails closed when `showConfirmDialog` is unavailable: no commit POST is sent, the metadata-only preview remains visible, and a fixed safe `Creator commit blocked` card is rendered.
  - Hardened the `commitCreatorSpec` click path to prepend the blocked receipt instead of silently returning, preserving the shared-dialog safety envelope while avoiding generated renderer/API-auth/secret-looking DOM leaks.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); focused creator preview/commit regressions passed (`12 passed, 146 deselected`); Spaces UI behavior + demo parity suites passed (`170 passed`); `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, spec/quality subagent reviews, and `/tmp` real-static creator no-dialog browser harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/creator-no-dialog-qa.png`.

- `fix(spaces): label non-candidate future revisions`
  - Added RED/GREEN real-`static/spaces.js` coverage proving a non-candidate future revision row renders a visible `timeline: future` label, keeps the ordinary `Restore` action, and does not steal the `Return to present` action from the candidate row.
  - Hardened `formatRevisionTimelineLabel(...)` so all backend-provided future timeline states are visible in the Space detail/recovery row renderer while preserving current-row action suppression and metadata-only redaction of generated renderer/API-auth/secret-looking fixture values.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); related revision/recovery label tests passed (`3 passed`); Spaces UI behavior + demo parity suites passed (`168 passed`); `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, spec/quality subagent reviews, and `/tmp` real-static future-revision browser harness leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/future-revision-label-qa.png`.

- `fix(spaces): label recovery return to present`
  - Added RED/GREEN real-`static/spaces.js` coverage proving safe recovery rollback rows label future `is_return_to_present_candidate` revisions as `Return to present` instead of generic `Restore revision`, while past rollback points keep the ordinary restore label.
  - Hardened `renderRecoveryRevisionRows(...)` to reuse the existing return-to-present helper already used by Space detail revision history, preserving current-revision action suppression, metadata-only restore previews/diffs, and generated renderer/API-auth/secret-looking DOM omissions.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); Spaces UI behavior + demo parity suites passed (`167 passed`); `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, spec/quality subagent reviews, and `/tmp` real-static recovery return-to-present harness browser leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/recovery-return-present-qa.png`.

- `fix(spaces): accept runtimeToken sandbox alias`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `messageType` + `runtimeToken` + `spaceId`/`widgetId` sandbox prompts queue the same metadata-only `agent.prompt` event through the existing approval gate.
  - Added conflicting-alias coverage proving mismatched `runtime_token` / `runtimeToken` values fail closed before shared-dialog approval or `api/spaces/widget/event`, without reflecting hostile token/prompt/renderer/script markers or secret-looking sentinels into the DOM.
  - Hardened the static sandbox bridge with a shared runtime-token alias resolver, preserving opaque-origin, exact-iframe-source, visible-shell, selector, and metadata-only queueing checks.

- `fix(spaces): hide current revision restore actions`
  - Added RED/GREEN real-`static/spaces.js` coverage proving current revision rows expose no `data-event-id` restore actions, while the return-to-present candidate keeps `Return to present` and a future non-candidate keeps `Restore`.
  - Hardened `renderRevisionHistory(...)` with a shared current-revision check so both full-Space restore and per-widget restore buttons are suppressed for the active manifest row, keeping the row as metadata-only rollback context rather than a no-op destructive control.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); related revision/detail tests passed (`2 passed`); Spaces UI behavior + demo parity suites passed (`155 passed`); `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, spec/quality subagent reviews, and `/tmp` real-static revision harness browser leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_2cbe90cc2f694468a37553120f3b9c49.png`.

- `fix(spaces): block unlisted sandbox messages in UI`
  - Added RED/GREEN real-`static/spaces.js` coverage proving an unlisted runtime message (`capy:debug:SECRET_VALUE_DO_NOT_LEAK`) with a valid runtime token and `origin: "null"` opens no dialog, sends no `api/spaces/widget/event` request, renders only the generic `Sandbox message blocked` status, and omits hostile type/prompt/renderer/script markers plus secret-looking sentinels from DOM.
  - Hardened the static sandbox bridge so `capy:ready`, `capy:resize`, and `capy:agent:prompt` are the only allowed runtime message types; every other `capy:*` discriminator fails closed before prompt approval or network queueing.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); targeted sandbox/postMessage suite passed (`15 passed, 125 deselected`); Spaces UI behavior + demo parity suites passed (`150 passed`); full Spaces foundation suite passed (`231 passed`); `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, spec/quality subagent reviews, and `/tmp` real-static sandbox unknown-message harness browser leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_91d31a271e554707ae87615e08f43640.png`.

- `fix(spaces): reject unknown sandbox messages`
  - Added RED/GREEN backend coverage proving an unlisted runtime message type (`capy:debug:dump`) is rejected through both direct `queue_widget_event(...)` and `/api/spaces/widget/event`, while the already-advertised `capy:agent:prompt` path remains queueable and rejected route responses/events omit renderer/source/API-auth markers plus secret-looking sentinels.
  - Hardened the runtime-contract gate by centralizing the advertised allowlist (`capy:ready`, `capy:resize`, `capy:agent:prompt`) and rejecting every detected `capy:*` event/payload discriminator that is not explicitly allowed, before any widget event is persisted.
  - Validation at completion: focused RED failed with `Failed: DID NOT RAISE <class 'ValueError'>`; focused GREEN passed (`1 passed`); targeted runtime/widget contract regressions passed (`3 passed`); full Spaces foundation suite passed (`231 passed`); Spaces UI behavior + demo parity suites passed (`149 passed`); `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality subagent reviews, and `/tmp` real-static runtime-contract backend harness leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_637c1b8e5b9945c6a2ace8473b3078b3.png`.

- `fix(spaces): accept runtime contract widgetId aliases`
  - Added RED/GREEN backend coverage proving `space.widget.runtime_contract`, `space.current.widget.runtime_contract`, and `widget.runtime_contract` accept Space Agent-style camelCase/positional selectors while continuing to reject unsafe widget IDs and omit renderer/source/API-auth markers plus secret-looking sentinels from serialized contract responses.
  - Hardened the runtime-contract tool branch to reuse `_space_tool_current_id(...)` and `_space_tool_widget_id(...)`, matching adjacent widget read/see aliases instead of requiring snake_case selectors.
  - Validation at completion: focused RED failed with `Invalid space_id`; focused GREEN passed (`1 passed`); targeted runtime/widget alias regressions passed (`3 passed`); full Spaces foundation suite passed (`231 passed`); Spaces UI behavior + demo parity suites passed (`149 passed`); `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, spec/quality subagent reviews, and `/tmp` real-static runtime-contract harness browser leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_866e421e2ee547b9a4f7b7e3ff89e662.png`.

- `fix(spaces): preserve quarantine on full rollback`
  - Added RED/GREEN backend coverage proving `restore_revision(...)` restores older safe widget title/layout metadata while preserving the trusted current `recovery.disabled` / `disabled_reason` envelope until the explicit enable recovery control runs.
  - Hardened full-Space rollback to merge current admin-owned disabled recovery envelopes into normalized snapshot widgets before writing the restored manifest, keeping recovery snapshots metadata-only and omitting generated renderer/source/API-auth markers plus secret-looking sentinel values.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); related rollback/quarantine regressions passed (`7 passed, 222 deselected`); full Spaces foundation suite passed (`229 passed`); Spaces UI behavior + demo parity suites passed (`149 passed`); `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, and `/tmp` real-static recovery harness browser leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_552f689c94504505956500200d2a4943.png`.

- `fix(spaces): preserve widget quarantine on rollback`
  - Added RED/GREEN backend coverage proving `restore_widget_revision(...)` restores a widget's safe title/layout from a revision snapshot while preserving the trusted current `recovery.disabled` / `disabled_reason` envelope until the explicit enable recovery control runs.
  - Hardened widget-level rollback to reuse the existing recovery-state preservation helper already used by upsert/update/patch paths, keeping recovery snapshots metadata-only and omitting generated renderer/script/API-auth markers plus secret-looking sentinel values.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); related rollback/quarantine regressions passed (`5 passed, 223 deselected`); full validation, screenshot artifact, commit hash, and live health are recorded in the scheduled sprint report for this run.

- `fix(spaces): sanitize root space metadata`
  - Added RED/GREEN backend coverage proving root Space `layout` and `capabilities` metadata are sanitized at create/update time, reflected safely through `space.get`, and persisted into manifests/revision snapshots without generated renderer/html/script/source/data/API-auth, credential-like, or secret-looking sentinel fields.
  - Hardened `create_space(...)`, `update_space(...)`, and `read_space_detail(...)` with a bounded root-metadata sanitizer that preserves benign keys such as `metadata_only`, grid labels, and numeric layout fields while dropping unsafe standalone/camel/snake marker keys and redacted values.
  - Validation at completion: focused RED failed before implementation (`1 failed`), expanded review-gap RED failed for root metadata/tool-create/restore (`3 failed`), and compact event-handler RED failed (`1 failed`); focused GREEN passed (`3 passed`); adjacent public metadata regressions passed (`4 passed`); full Spaces foundation suite passed (`223 passed`); Spaces demo parity suite passed (`10 passed`); `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, final spec/quality subagent review, and `/tmp` backend harness browser leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_51b7e9d276c64f67aac56ad420a41abc.png`.

- `fix(spaces): show stale creator commit block`
  - Added RED/GREEN real-`static/spaces.js` coverage proving a stale/failed `space.creator.commit` response renders `Creator commit blocked`, keeps the prior metadata-only preview visible, avoids saved-commit follow-up actions, and omits stale backend error details plus renderer/source/API-auth/generated-code/raw-prompt markers and secret-looking sentinels from DOM.
  - Hardened the creator commit click path to catch failed commit POSTs locally and prepend a fixed safe status card instead of throwing an unhandled rejection or rendering backend error bodies.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); focused creator regressions passed (`8 passed`); Spaces UI behavior + demo parity suites passed (`139 passed`); `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, and `/tmp` real-static creator-commit-stale harness browser leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_6f6cd7c3f94544a6a9d65741a7de9c9f.png`.

- `fix(spaces): reject stale creator preview commits`
  - Added RED/GREEN backend coverage proving an approved creator commit refuses to overwrite an explicitly previewed existing Space when its revision changes before commit.
  - Added RED/GREEN backend coverage proving a create-intent creator preview refuses to revise a Space whose slug appears after the preview receipt was issued.
  - Hardened creator preview receipts with server-side `commit_base` metadata (`exists` + `revision_event_id`) and commit-time revision/slug checks while keeping the preview receipt metadata server-only and generated/source/API-auth markers out of responses.

- `fix(spaces): redact import result metadata`
  - Added RED/GREEN real-`static/spaces.js` coverage proving hostile import-result metadata in backend response labels (`renderer`/`source`/`html`/`script`/API-auth markers, generated-code/raw-prompt markers, and secret-looking sentinels) does not appear in `#capySpacesRoot` while safe imported Space/widget labels and benign unsupported API warnings remain visible.
  - Hardened `renderSpaceImportResult(...)` with a dedicated display-metadata sanitizer for Space Agent import receipts so escaping is no longer the only defense for import-result labels.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`3 passed`); Spaces UI behavior + demo parity suites passed (`136 passed`); full Spaces foundation suite passed (`212 passed`); `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, and `/tmp` real-static import-result browser harness leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_238ebacddad646fda92de8746fb1aca2.png`.

- `feat(spaces): list blocked sandbox read messages`
  - Added RED/GREEN backend coverage proving `space.widget.runtime_contract` exposes `capy:data:get` and `capy:asset:url` in the metadata-only blocked-message list, so safe widget details communicate that read-style data/assets bridges are not available yet.
  - Updated real-`static/spaces.js` behavior coverage to render the expanded safe blocked-message list while preserving generic blocked runtime statuses and avoiding hostile payload/secret-looking sentinel leaks.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed` backend, `2 passed` UI/backend focused); full Spaces foundation suite passed (`208 passed`); Spaces UI behavior + demo parity suites passed (`132 passed`); `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, and `/tmp` real-static runtime-contract harness leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_2a98df3582954a6daa00baad33b99ab5.png`.

- `fix(spaces): avoid reflecting blocked sandbox message types`
  - Added RED/GREEN real-`static/spaces.js` coverage proving a hostile blocked runtime type such as `capy:raw:SECRET_VALUE_DO_NOT_LEAK` queues no widget event, opens no dialog, makes no data/asset backend call, and does not expose the hostile type, renderer/source/API-auth markers, script tags, or secret-looking sentinels in the DOM.
  - Hardened the sandbox/postMessage bridge to render a generic `Sandbox message blocked` status for all blocked runtime messages while preserving the existing fail-closed blocked-event contract.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`3 passed`); Spaces UI behavior + demo parity suites passed (`132 passed`); `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, and `/tmp` real-static sandbox/postMessage harness browser leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_daac5b3e79ba400ba8d2881ae49d5d0d.png`.

- `feat(spaces): expose admin repair tool aliases`
  - Added RED/GREEN backend coverage proving `space.admin.recovery.repair_space` queues a metadata-only `agent.repair` event, `space.admin.recovery.repair_events` lists the same safe repair event, and compact `space.admin.repair_space` routes through the same repair queue without returning `active_space_id`.
  - Hardened `run_space_tool(...)` by extending the existing whole-Space repair queue/list allowlists for admin aliases instead of introducing a parallel sanitizer path; responses continue to omit generated renderer/source/API-auth markers, synthetic secret-looking sentinels, session sentinel values, script tags, and generated bodies.
  - Validation at completion: focused RED failed with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`); targeted recovery/admin repair regressions passed (`4 passed`); full Spaces foundation suite passed (`206 passed`); `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, and `/tmp` real-static admin repair harness browser leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_3482bc63b43a4b5996d3e9bd5aa57125.png`.

- `feat(spaces): expose admin module recovery aliases`
  - Added RED/GREEN backend coverage proving `space.admin.recovery.disable_module` and `space.admin.enable_module` accept camelCase `moduleId`, toggle quarantined module disabled state through the existing recovery helpers, and keep responses/snapshots metadata-only.
  - Hardened recovery disabled-reason redaction to treat standalone auth markers as unsafe public metadata, after spec review caught that plain auth text could otherwise appear in module quarantine summaries.
  - Validation at completion: initial focused RED failed with `Unsupported Capy Spaces tool action`; follow-up RED failed because `auth failure` appeared in `disabled_reason`; focused GREEN passed (`1 passed`); targeted recovery/admin regressions passed (`3 passed`); full Spaces foundation suite passed (`201 passed`); `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, and `/tmp` real-static admin-module harness browser leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_5a86e8ef058f4afaaa6cedabd5e6e356.png`.

- `feat(spaces): expose current repair tool aliases`
  - Added RED/GREEN backend coverage proving `space.current.repair_space`, `space.current.repair`, and `space.current.repair_events` resolve `activeSpaceId`, queue/list metadata-only `agent.repair` events, return `active_space_id`, and omit generated renderer/source/API-auth markers plus secret-looking sentinels from serialized tool responses.
  - Hardened `run_space_tool(...)` so current-space repair aliases share the existing safe recovery repair queue/list implementation instead of requiring callers to switch from active-space context to explicit recovery namespace calls.
  - Validation at completion: focused RED failed before implementation (`1 failed`); focused GREEN passed (`1 passed`); full Spaces foundation suite passed (`198 passed`); `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, and `/tmp` real-static recovery harness browser leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_988e88bb8260491fab22af7d2f91e318.png`.

- `feat(spaces): expose recovery rollback tool aliases`
  - Added RED/GREEN backend coverage proving `space.recovery.rollback` restores a safe full-Space revision from `spaceId`/`eventId` payloads without returning generated renderer/API-auth markers or secret-looking sentinels.
  - Added RED/GREEN backend coverage proving `space.revision.restoreWidget` restores only the requested widget from a revision snapshot while sibling widgets remain current and tool responses stay metadata-only.
  - Added follow-up RED/GREEN coverage for current-space revision widget aliases and positional restore args (`[spaceId, eventId, widgetId]`).
  - Hardened `run_space_tool(...)` with a shared event-id resolver and safe-mode/recovery rollback aliases for full-Space and widget-level restores.
  - Validation at completion: focused RED failed as expected (`2 failed`, then `1 failed` for the review-found alias gap); focused GREEN passed (`3 passed`); full Spaces foundation suite passed (`197 passed`); Spaces UI JS behavior + demo parity suites passed (`130 passed`); `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, and `/tmp` real-static recovery harness browser leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_de5059dadd5742a7960d23089c6ccb97.png`.

- `feat(spaces): queue recovery space repairs`
  - Added RED/GREEN backend coverage for `POST /api/spaces/recovery/repair-space`, including marker-only prompt/payload redaction, session-id redaction in persisted repair events, and rejection of non-object payloads before any durable queue write.
  - Added RED/GREEN real-`static/spaces.js` coverage proving the recovery panel renders `Ask Capy to repair Space`, fails closed without `showPromptDialog`, POSTs the metadata-only repair payload, refreshes recovery, and displays only safe queued status.
  - Validation at completion: focused RED failed before implementation (`2 failed`) and focused GREEN passed (`5 passed`); full Spaces UI JS behavior + foundation suites passed (`312 passed`); demo parity suite passed (`10 passed`); `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py tests/test_spaces_foundation.py api/spaces.py api/routes.py`, `git diff --check`, and `/tmp` real-static recovery harness leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_798e234044c540208f1aa254cc1e2b6c.png`.

- `fix(spaces): preserve module quarantine on upsert`
  - Added RED/GREEN backend coverage proving `_upsert_recovery_module(...)` preserves an existing disabled recovery state even when an incoming module payload tries to reset `recovery.disabled` to false.
  - Hardened `upsert_recovery_module(...)` to merge incoming generated module metadata/bodies while retaining the trusted recovery envelope from the existing quarantine record; explicit recovery enable controls remain the only way to re-enable a disabled module.
  - Validation at completion: focused RED failed before implementation; focused GREEN passed (`1 passed`); focused module recovery regression set passed (`4 passed, 178 deselected`); full Spaces foundation suite passed (`182 passed`); `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, and `/tmp` real-static recovery harness leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_adb78b19148345eb8c814461141c2407.png`.

- `fix(spaces): preserve rollback return history`
  - Added RED/GREEN backend coverage proving a full-space restore to an older widget snapshot keeps newer revision IDs visible in `list_revision_events(...)`, then supports restoring back to that newer revision while public responses stay metadata-only.
  - Hardened `restore_revision(...)` to merge safe snapshot revision IDs with the current manifest timeline before recording `space.restored`, preserving time-travel history instead of truncating it to the restored snapshot's past.
  - Validation at completion: focused RED failed before implementation; focused GREEN passed (`1 passed`); rollback/history regression set passed (`4 passed`); full Spaces foundation suite passed (`175 passed`); Spaces UI JS behavior + demo parity suites passed (`124 passed`); `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, and `/tmp` browser QA harness leak checks passed. Screenshot artifact: `/private/tmp/capy-spaces-browser-harness/artifacts/capy-spaces-next-slice.png`.

- `fix(spaces): redact active context metadata`
  - Added RED/GREEN backend coverage proving unsafe Space description/instructions, widget titles, shared-data keys, and queued event names are redacted from compact active-space prompt context while safe Space id/name, widget id/kind, event id/status, and mutation guidance remain visible.
  - Hardened `build_agent_context()` so streaming prompt injection uses metadata-only redacted labels and never echoes hostile renderer/script/API-auth markers or secret-looking sentinels from source-derived metadata.
  - Validation at completion: focused RED failed before implementation; focused GREEN passed (`1 passed`); focused active-context regression set passed (`4 passed`); full Spaces foundation suite passed (`171 passed`); Spaces UI JS behavior + demo parity suites passed (`122 passed`); `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, and `/tmp` browser QA harness leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_d74ed11c3d144865b66b82d75909f216.png`.

- `fix(spaces): redact recovery space metadata`
  - Added RED/GREEN backend coverage proving unsafe Space `name` / `description` values and revision restore-preview labels are redacted in `recovery_snapshot()` while safe ids/widget summaries remain visible.
  - The recovery/admin hard gate continues to expose metadata-only rollback and repair controls without returning generated renderer/source/API-auth markers or secret-looking sentinel values.
  - Validation at completion: focused RED failed before implementation; focused GREEN passed (`2 passed`); full Spaces foundation suite passed (`170 passed`); Spaces UI JS behavior + demo parity suites passed (`122 passed`); `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, and `/tmp` browser QA harness panel-scoped leak checks passed. Screenshot artifact: `/tmp/capy-spaces-progress/recovery-redaction-harness.png`.

- `feat(spaces): link creator commits to recovery actions`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the `space.creator.commit` UI receipt keeps the revisioned metadata-only gate envelope and now shows `Open committed Space` plus `Manage committed widgets` actions wired to the sanitized committed `space_id`.
  - The receipt remains fail-closed for unsafe/missing path-style ids through a path-safe creator-id filter and continues to omit hostile renderer/script/API-auth markers and secret-looking values from DOM.
  - Validation at completion: focused RED failed before implementation; focused GREEN passed (`1 passed`); follow-up unsafe-id RED failed before the path-safe id filter and then passed; focused creator UI gate tests passed (`4 passed`); Spaces UI JS behavior + demo parity suites passed (`120 passed`); `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, and `/tmp` browser QA harness leak checks passed. Screenshot artifact: `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_5df4fc44c34548d08b1f764f1505a971.png`.

- `feat(spaces): add creator preview gate`
  - Added RED/GREEN backend coverage proving `space.creator.preview` returns a bounded non-persisted creator-loop spec (`stored: false`, `executed: false`) from a hostile prompt + widget fixture while preserving safe space/widget metadata, quarantining generated bodies, and omitting prompt/auth/source/data/generated-body markers and secret-looking values from serialized responses.
  - Added aliases for future source-style callers (`space.creator.spec.preview`, `space.spaces.previewCreatorSpec`) without creating Spaces or widgets; commit remains gated behind sandbox preview, visual QA, and revision checkpoints.
  - Validation at completion: focused RED failed before implementation, follow-up REDs caught widget title/prompt/description fallback leaks plus unbounded nested/wide prompt metadata, then focused GREEN passed (`4 passed`); full Spaces foundation suite passed (`157 passed`); Spaces UI JS behavior + demo parity suites passed (`116 passed`); `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py`, `git diff --check`, and `/tmp` browser QA harness leak checks passed.

- `feat(spaces): add sandbox postmessage event bridge`
  - Added RED/GREEN real-`static/spaces.js` coverage proving widget details show the safe runtime contract, approved sandbox prompts queue metadata-only `agent.prompt` events, cancelled/stale/old-token prompts do not queue, raw/eval/data mutation messages are blocked, and prompt/auth/source/data/generated-code/secret-looking values stay redacted.
  - Added runtime token rotation per rendered widget detail shell, fail-closed visible-shell checks, broader prompt redaction, and metadata-only queued status cards; generated widget bodies remain disabled and are never rendered or executed.
  - Validation at completion: focused RED failed before implementation for code-like prompt redaction and old-token invalidation, then follow-up RED failed for auth/prompt/generated-code marker redaction and raw/eval/data mutation blocking; focused GREEN passed; Spaces UI behavior + demo parity suites passed (`116 passed`); `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, `git diff --check`, read-only review, and `/tmp` browser QA harness leak checks passed.

- `feat(spaces): restore individual widgets from revisions`
  - Added RED/GREEN backend and real-`static/spaces.js` coverage proving a single widget can be restored from a revision snapshot while other widgets remain intact.
  - Added `/api/spaces/revision/restore-widget`, metadata-only public responses, safe revision event details, UI `Restore widget` buttons derived from safe restore diffs, and fail-closed shared-dialog handling.
  - Validation at completion: focused RED failed before implementation; focused GREEN passed; full Spaces UI JS behavior suite and full Spaces foundation suite passed; syntax/compile/diff checks passed; browser QA confirmed widget rollback controls are visible and no hostile renderer/source/API-auth markers or secret-looking values appear in the Spaces root.

- `feat(spaces): show revision restore diffs`
  - Added RED/GREEN backend and real-`static/spaces.js` coverage proving revision events include metadata-only `restore_diff` summaries and the Revision history UI renders what a restore would change (`Fields`, `Remove widgets`, `Update widgets`) without leaking hostile renderer/source/API-auth markers or secret-looking values.
  - Added the accelerated conveyor workflow to this plan: serialized implementation lane with parallel acceptance/test/harness/review prep for rollback, recovery, sandbox, Research Harness, and generic creator-loop slices.
  - Validation at completion: focused RED failed before implementation; focused GREEN passed (`2 passed`); focused rollback/recovery checks passed (`5 passed`), full Spaces UI JS behavior suite passed (`92 passed`), full Spaces foundation suite passed (`152 passed`), `node --check static/spaces.js`, `py_compile api/spaces.py tests/test_spaces_foundation.py tests/test_spaces_ui_js_behaviour.py`, and `git diff --check` passed. Browser QA confirmed the restore-diff row was visible and panel-scoped leak check was clean.

- `feat(spaces): show recovery restore previews`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the safe recovery panel renders restore-preview summaries (`Preview: ... · Widgets: ...`) for rollback points while continuing to omit hostile renderer/script/API-auth markers and secret-looking values from DOM.
  - Validation at completion: focused RED failed before implementation; focused GREEN passed; full Spaces UI JS behavior suite passed (`92 passed`), `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, and `git diff --check` passed.

- `feat(spaces): summarize kanban flow in smoke suite`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `Run all smokes` now renders a compact Kanban board checklist row (`columns 3 · cards 4 · drag/drop planned · card edit planned`) from safe `kanban_flow` metadata while continuing to omit hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `feat(spaces): link game install card to smoke`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Game Sandbox install status includes a `Run snake smoke` action wired to `demo_snake_iterative_repair`, while preserving `Open game sandbox` / `Manage game widgets` and continuing to omit hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `feat(spaces): link music install card to smoke`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Music Sequencer install status includes a `Run music smoke` action wired to `demo_step_sequencer_piano_roll`, while preserving `Open music sequencer` / `Manage music widgets` and continuing to omit hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `feat(spaces): link model setup install card to smoke`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Model Provider Setup install status includes a `Run provider setup smoke` action wired to `demo_provider_setup`, while preserving `Open model setup` / `Manage provider widgets` and continuing to omit hostile renderer/script/API-auth markers, token text, and secret-looking values from DOM.

- `feat(spaces): link camera install card to smoke`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Camera Dashboard install status includes a `Run camera smoke` action wired to `demo_camera_dashboard`, while preserving `Open camera dashboard` / `Manage camera widgets` and continuing to omit hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `feat(spaces): link browser install card to smoke`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Browser Surface install status includes a `Run browser smoke` action wired to `demo_browser_cocontrol_google_or_test_site`, while preserving `Open browser surface` / `Manage browser widgets` and continuing to omit hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `feat(spaces): link research install card to smoke`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Research Harness install status includes a `Run research smoke` action wired to `demo_research_harness_pdf_export`, while preserving `Open research harness` / `Manage research widgets` and continuing to omit hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `feat(spaces): link local service install card to smoke`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Local Service Dashboard install status includes a `Run local service smoke` action wired to `demo_local_agent_control_dashboard`, while preserving `Open local service dashboard` / `Manage service widgets` and continuing to omit hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `feat(spaces): link dashboard install card to smoke`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Daily Dashboard install status includes a `Run dashboard smoke` action wired to `demo_daily_dashboard`, while preserving `Open dashboard demo` / `Manage dashboard widgets` and continuing to omit hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `feat(spaces): link stock install card to smoke`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Stock Chart install status includes a `Run stock smoke` action wired to `demo_stock_chart`, while preserving `Open stock chart` / `Manage stock widgets` and continuing to omit hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `feat(spaces): summarize notes flow in smoke suite`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `Run all smokes` now renders a compact Notes app checklist row (`folders 2 · active Demo Project · editor saved · markdown saved · attachments agent-mediated`) from safe `notes_flow` metadata while continuing to omit hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `fix(spaces): avoid double-numbering notes checklist`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Notes app checklist renders clean ordered-list items (`Folder list ready`, `Editor draft saved`, `Markdown preview saved`, `Attachments remain agent-mediated`) instead of duplicating numeric prefixes such as `1. 1. ...`, while continuing to omit hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `fix(spaces): avoid double-numbering weather checklist`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Weather demo prompt → widget checklist renders clean ordered-list items (`Chat answer recorded`, `Widget created from request`, `Persistent widget verified after reload`) instead of duplicating numeric prefixes such as `1. 1. ...`, while continuing to omit hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `feat(spaces): add Big Bang onboarding walkthrough`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the main Spaces toolbar displays `Run Big Bang onboarding`, posts exactly `{demo: "demo_big_bang_onboarding"}`, then renders the safe Big Bang smoke result plus metadata-only widget manager for `demo-big-bang-onboarding` while omitting hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `feat(spaces): queue notes demo save event`
  - Added RED/GREEN backend coverage proving `demo_notes_app` records the safe folders/editor/preview/attachments state and queues one metadata-only `notes.save` event against `notes-editor`, exposing `queued_event_count`/`queued_event` for the visible smoke card and widget-manager event inbox while omitting hostile renderer/script/API-auth markers and secret-looking values from serialized responses.

- `feat(spaces): add camera walkthrough action`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the main Spaces toolbar displays `Run camera walkthrough`, posts exactly `{demo: "demo_camera_dashboard"}`, then renders the camera dashboard smoke result plus safe widget manager for `demo-camera-dashboard` while omitting hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `feat(spaces): add browser walkthrough action`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the main Spaces toolbar displays `Run browser walkthrough`, posts exactly `{demo: "demo_browser_cocontrol_google_or_test_site"}`, then renders the browser co-control smoke result plus safe widget manager/event inbox for `demo-browser-cocontrol-google-or-test-site` while omitting hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `feat(spaces): add research walkthrough action`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the main Spaces toolbar displays `Run research walkthrough`, posts exactly `{demo: "demo_research_harness_pdf_export"}`, then renders the Research Harness PDF-export smoke result plus safe widget manager/event inbox for `demo-research-harness-pdf-export` while omitting hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `feat(spaces): add kanban walkthrough action`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the main Spaces toolbar displays `Run kanban walkthrough`, posts exactly `{demo: "demo_kanban_board"}`, then renders the Kanban board smoke result plus safe widget manager/event inbox for `demo-kanban-board` while omitting hostile renderer/script/API-auth markers and secret-looking values from DOM.
  - Tightened the demo parity test expectation for the already-expanded Notes flow metadata (`folder_count`, `active_folder`, and `attachment_count`) so the combined Spaces UI + demo parity suite reflects the current safe metadata contract.

- `feat(spaces): add notes walkthrough action`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the main Spaces toolbar displays `Run notes walkthrough`, posts exactly `{demo: "demo_notes_app"}`, then renders the Notes app smoke result plus safe widget manager/event inbox for `demo-notes-app` while omitting hostile renderer/script/API-auth markers and secret-looking values from DOM.

- `feat(spaces): summarize weather suite observation`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `Run all smokes` now renders a compact Weather observation row (`Prague, CZ · 18 °C · partly cloudy · Agent bridge: 1 queued`) from the existing safe weather smoke result while continuing to omit raw prompt/answer text, hostile renderer/script/API-auth markers, and secret-looking values from DOM.

- `feat(spaces): show weather prompt hint in widget list`
  - Added RED/GREEN backend coverage proving the installed Weather Demo widget list exposes safe `weather`, `event_bridge`, and `prompt` metadata even before the first observation refresh, while omitting generated/source/API-auth markers.
  - Added RED/GREEN real-`static/spaces.js` coverage proving the widget manager renders a visible `Suggested prompt` card (`Ask Capy to refresh or explain the Prague weather widget` / `widget.refresh`) from list metadata without fetching or executing generated widget bodies.

- `feat(spaces): summarize weather flow in smoke suite`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `Run all smokes` now surfaces a compact safe `Weather demo checklist` row for the weather vertical (`chat answer recorded · widget created · reload verified`) while omitting raw prompt/answer text, hostile renderer/script/API-auth markers, and secret-looking values from DOM.

- `feat(spaces): show notes demo checklist`
  - Added RED/GREEN backend and real-`static/spaces.js` coverage proving the Notes app demo smoke carries a safe `notes_flow` summary and renders a visible `Notes app checklist` with folders/editor/markdown/attachment checkpoints while omitting hostile renderer/script/API-auth markers from DOM.

- `feat(spaces): show weather demo checklist`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the weather demo smoke result renders a safe `Weather demo checklist` with chat-answer, widget-created, and reload-persistence checkpoints while preserving answer preview/weather metadata and omitting hostile renderer/script/API-auth markers from DOM.

- `feat(spaces): link kanban install card to smoke`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Kanban board install status includes a `Run kanban smoke` action wired to the existing `demo_kanban_board` smoke route, while preserving `Open kanban board` / `Manage kanban widgets` actions and omitting hostile renderer/script/API-auth markers from DOM.
  - Validation at completion before commit: focused RED failed because `Run kanban smoke` was absent; focused GREEN passed (`1 passed`), focused install/smoke regressions passed (`3 passed`), Spaces UI behavior + demo parity suites passed (`84 passed`), `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, and `git diff --check` passed.

- `feat(spaces): link notes install card to smoke`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Notes app install status includes a `Run notes smoke` action wired to the existing `demo_notes_app` smoke route, while preserving `Open notes app` / `Manage notes widgets` actions and omitting hostile renderer/script/API-auth markers from DOM.
  - Validation at completion before commit: focused RED failed because `Run notes smoke` was absent; focused GREEN passed (`1 passed`), focused install/smoke regressions passed (`2 passed`), Spaces UI behavior + demo parity suites passed (`84 passed`), `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, and `git diff --check` passed.

- `feat(spaces): link weather install card to smoke`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Weather demo install status includes a `Run weather smoke` action wired to the existing `demo_weather_widget` smoke route, while preserving `Open weather demo` / `Manage weather widget` actions and omitting hostile renderer/script/API-auth markers from DOM.
  - Validation at completion before commit: focused RED failed because `Run weather smoke` was absent; focused GREEN passed (`1 passed`), Spaces UI behavior + demo parity suites passed (`84 passed`), `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, and `git diff --check` passed.

- `feat(spaces): show weather prompt queued status`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Weather demo Ask Capy button uses the shared prompt dialog, posts the user prompt only in the typed request, reloads widgets, then prepends `Weather prompt queued` with the safe `weather · agent.prompt · evt1` summary while omitting the raw prompt, hostile renderer/script/API-auth markers, and secret-looking values from DOM.

- `feat(spaces): show weather refresh queued status`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the Weather demo Refresh button posts only `{space_id, widget_id, event_name, payload}` metadata, reloads the widget list, then prepends `Weather refresh queued` with the safe `weather · widget.refresh · evt1` summary while omitting hostile renderer/script/API-auth markers from DOM.

- `feat(spaces): show inline weather bridge status`
  - Added RED/GREEN real-`static/spaces.js` coverage proving the widget manager renders `Agent bridge: 2 queued` plus the latest `widget.refresh` event for the Weather demo, while omitting prompt text, generated/source markers, API-auth fields, and secret-looking values from DOM.

- `feat(spaces): queue weather demo refresh event`
  - Added RED/GREEN backend coverage proving `demo_weather_widget` records the Prague weather observation, queues one safe `widget.refresh` event against `weather-current`, exposes `queued_event_count` for the visible smoke card, and keeps generated/source/API-auth markers out of the serialized result.

- `feat(spaces): show local service install status actions`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `Install local service dashboard` posts `{template: "service"}`, refreshes the Spaces list, prepends a safe `Local service dashboard installed` status card with direct open/manage actions, and keeps hostile `renderer`/`<script>`/API-auth markers out of DOM.

- `feat(spaces): show camera dashboard install status actions`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `Install camera dashboard` posts `{template: "camera"}`, refreshes the Spaces list, prepends a safe `Camera dashboard installed` status card with direct open/manage actions, and keeps hostile `renderer`/`<script>`/API-auth markers out of DOM.

- `feat(spaces): show stock chart install status actions`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `Install stock chart` posts `{template: "stock"}`, refreshes the Spaces list, prepends a safe `Stock chart installed` status card with direct open/manage actions, and keeps hostile `renderer`/`<script>`/API-auth markers out of DOM.
  - Validation at completion before commit: focused RED failed because `Stock chart installed` was absent; focused GREEN passed (`1 passed`), Spaces UI JS behavior plus stock template regressions passed (`77 passed`), `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, and `git diff --check` passed. Mock-state screenshot QA captured initial and installed states with empty browser console and a clean DOM leak check.

- `feat(spaces): show browser surface install status actions`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `Install browser surface` posts `{template: "browser"}`, refreshes the Spaces list, prepends a safe `Browser surface installed` status card with direct open/manage actions, and keeps hostile `renderer`/`<script>`/API-auth markers out of DOM.

- `feat(spaces): show dashboard install status actions`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `Install dashboard demo` posts `{template: "dashboard"}`, refreshes the Spaces list, prepends a safe `Dashboard demo installed` status card with direct open/manage actions, and keeps hostile `renderer`/`<script>`/API-auth markers out of DOM.

- `feat(spaces): show research install status actions`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `Install research harness` posts `{template: "research"}`, refreshes the Spaces list, prepends a safe `Research harness installed` status card with direct open/manage actions, and keeps hostile `renderer`/`<script>`/API-auth markers out of DOM.
  - Validation at completion before commit: focused RED failed because `Research harness installed` was absent; focused GREEN passed (`1 passed`), Spaces UI JS behavior plus research template regressions passed (`77 passed`), `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, and `git diff --check` passed.

- `feat(spaces): show kanban install status actions`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `Install kanban board` posts `{template: "kanban"}`, refreshes the Spaces list, prepends a safe `Kanban board installed` status card with direct open/manage actions, and keeps hostile `renderer`/`<script>`/API-auth markers out of DOM.
  - Validation at completion before commit: focused RED failed because `Kanban board installed` was absent; focused GREEN passed (`1 passed`). Run `git log -1 --oneline` and the final sprint report for the full validation bundle.

- `feat(spaces): show notes demo status actions`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `Install notes app` posts `{template: "notes"}`, refreshes the Spaces list, prepends a safe `Notes app installed` status card with direct open/manage actions, and keeps hostile `renderer`/`<script>`/API-auth markers out of DOM.
  - Added RED/GREEN coverage proving `demo_notes_app` smoke results show `Manage notes widgets` while preserving the saved-notes metadata preview and continuing to omit generated/source/API-auth markers.
  - Validation at completion before commit: focused RED failed because `Notes app installed` and `Manage notes widgets` were absent; focused GREEN passed (`2 passed`), focused install/smoke regressions passed (`4 passed`), Spaces UI JS behavior + demo parity suites passed (`81 passed`), `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, and `git diff --check` passed.

- `feat(spaces): show weather install status actions`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `Install weather demo` posts `{template: "weather"}`, refreshes the Spaces list, and prepends a safe status card with the installed Space name/widget count plus direct `Open weather demo` and `Manage weather widget` actions while omitting hostile `renderer`/`<script>`/API-auth markers from DOM.
  - Validation at completion before commit: focused RED failed because `Weather demo installed` was absent; focused GREEN passed (`1 passed`), focused install regressions passed (`3 passed`), Spaces UI JS behavior + demo parity suites passed (`79 passed`), `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, and `git diff --check` passed.

- `feat(spaces): link weather smoke to demo widgets`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `demo_weather_widget` smoke results expose `Open demo Space` and `Manage weather widget` actions for the created demo Space, while non-weather smoke results use a generic `Manage demo widgets` label and still omit hostile `renderer`/`<script>`/API-auth markers from DOM.
  - Validation at completion before commit: focused weather RED failed because `Open demo Space` was absent; focused research RED failed because the generic manage label was absent. Focused GREEN passed (`2 passed`), Spaces UI JS behavior + demo parity suites passed (`79 passed`), `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, and `git diff --check` passed.

- `feat(spaces): show weather smoke observation result`
  - Added RED/GREEN real-`static/spaces.js` coverage proving `demo_weather_widget` smoke results display Prague observation metadata (`18 °C`, condition, status, summary) directly in the demo-passed card while hostile `renderer`/`<script>`/API-auth markers from the mocked response stay absent from rendered DOM.
  - Validation at completion before commit: focused RED failed because `Current weather observation` was absent from the smoke result; focused GREEN passed (`1 passed`), Spaces UI JS behavior + demo parity suites passed (`79 passed`), `node --check static/spaces.js`, `py_compile tests/test_spaces_ui_js_behaviour.py`, and `git diff --check` passed.

- `feat(spaces): expose source API health helper`
  - Added RED/GREEN backend coverage proving `space.api.health` returns safe Capy Spaces service metadata (`name`, browser panel URL, metadata-only mode, schema version, enabled state, Space count, and high-level responsibilities) while omitting request-supplied generated/source/API auth markers.
  - Validation at completion before commit: focused RED failed with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`143 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock/status screenshot QA showed empty `window.__harnessErrors`, visible leak check false, and screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_5370d28343ff4130ab57616e4db6fc90.png`.

- `feat(spaces): support source resolve layout helper`
  - Added RED/GREEN backend coverage proving `space.spaces.resolveSpaceLayout` returns metadata-only resolved positions, rendered sizes, and minimized maps using Space Agent-style collision search, anchor sizing/position/minimized overrides, and safe payload omission.
  - Validation at completion before screenshot/restart: focused RED failed with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), focused first-fit/resolve layout regression set passed (`2 passed`), Spaces foundation + demo parity suites passed (`147 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed.

- `feat(spaces): support source widget position SDK helpers`
  - Added RED/GREEN backend coverage proving `space.spaces.defaultWidgetPosition`, `space.spaces.parseWidgetPositionToken`, and `space.spaces.clampWidgetPosition` return metadata-only position/token/size results with Space Agent-compatible defaults, token parsing, size-aware clamping, and safe fallbacks while omitting renderer/html/source/API auth markers from serialized adapter responses.
  - Validation at completion before screenshot/restart: focused RED failed with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), focused position/size adapter regression set passed (`3 passed`). Continue with `git log -1 --oneline` and the final sprint report for the exact validation bundle.

- `feat(spaces): support source widget size SDK helpers`
  - Added RED/GREEN backend coverage proving `space.spaces.defaultWidgetSize`, `space.spaces.normalizeWidgetSize`, and `space.spaces.parseWidgetSizeToken` return metadata-only widget size/token results with Space Agent-compatible defaults, clamping, token parsing, and safe fallbacks while omitting renderer/html/source/API auth markers from serialized adapter responses.
  - Validation at completion before screenshot/restart: focused RED failed with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), focused size/reposition adapter regression set passed (`3 passed`), Spaces foundation + demo parity suites passed (`143 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed.

- `feat(spaces): expose source widget API version`
  - Added RED/GREEN backend coverage proving `space.spaces.widgetApiVersion` returns metadata-only widget API version `1`, matching Space Agent's runtime namespace property, while omitting renderer/source/API auth markers from serialized adapter responses.
  - Validation at completion before commit: focused RED failed with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`). Run `git log -1 --oneline` and the final report for the full validation bundle.

- `feat(spaces): support source define widget alias`
  - Added RED/GREEN backend coverage proving `space.spaces.defineWidget` accepts a nested Space Agent-style `{definition}` payload, returns a non-persisted safe widget blueprint with normalized layout/kind/title metadata, and omits renderer/html/script/data/source bodies plus credential-like markers from serialized adapter responses.
  - Validation at completion before commit: focused RED failed with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), focused source widget adapter regression set passed (`3 passed`), full Spaces foundation suite passed (`136 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed.

- `feat(spaces): support source positional helper args`
  - Added RED/GREEN backend coverage proving source-style `args` payloads resolve Space ids and widget ids for open/list/read helper aliases while omitting generated/executable/source/API auth markers from serialized adapter responses.
  - Validation at completion before commit: focused RED failed with `Invalid space_id`; focused GREEN passed (`1 passed`), focused adapter regression set passed (`3 passed`), full Spaces foundation suite passed (`134 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock/status screenshot QA showed empty `window.__harnessErrors`, clean visible leak check, and screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_c9cf074201a24679aeb9a7a01f8dc01e.png`. Local health returned OK on attempt 2 after LaunchAgent restart and tailnet `/health` returned OK.

- `feat(spaces): support source open helper alias`
  - Added RED/GREEN backend coverage proving `space.spaces.open` accepts Space Agent-style `spaceId` payloads, and existing source-style `space.spaces.get` / `read` helpers now accept camelCase ids while omitting renderer/html/source/data/API auth markers from serialized adapter responses.
  - Validation at completion before screenshot/restart: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`133 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed.

- `feat(spaces): support source normalize id helpers`
  - Added RED/GREEN backend coverage proving `space.spaces.normalizeSpaceId` and `space.spaces.normalizeWidgetId` accept source-style id/name/value payloads, normalize ids compatibly with Space Agent slug semantics, use safe fallback ids, and omit renderer/html/source/API auth markers from serialized adapter responses.
  - Validation at completion before screenshot/restart: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`132 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed.

- `feat(spaces): support current bulk widget delete aliases`
  - Added RED/GREEN backend coverage proving `space.current.removeWidgets` and `space.current.removeAllWidgets` accept Space Agent-style active-space and widget ids, delete selected/all widgets through Capy's revisioned metadata-only primitive, include `active_space_id`, and omit renderer/html/source/API auth markers from serialized adapter responses.
  - Validation at completion before screenshot/restart: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), current/source delete regression set passed (`3 passed`), full Spaces foundation suite passed (`130 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed.

- `feat(spaces): support current widget delete aliases`
  - Added RED/GREEN backend coverage proving `space.current.deleteWidget` and `space.current.removeWidget` accept Space Agent-style active-space and widget ids, delete through Capy's revisioned metadata-only primitive, include `active_space_id`, and omit renderer/html/source/API auth markers from serialized adapter responses.
  - Validation at completion before screenshot/restart: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`129 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed.

- `feat(spaces): support source current save helpers`
  - Added RED/GREEN backend coverage proving `space.current.saveMeta` and `space.current.saveLayout` accept Space Agent-style active-space payloads, save only safe metadata/layout fields, and omit renderer/html/source/API auth markers from serialized adapter responses and persisted manifests.
  - Validation at completion before screenshot/restart: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`). Run `git log -1 --oneline` and the final report for the full validation bundle.

- `feat(spaces): support source widget size helper`
  - Added RED/GREEN backend coverage proving `space.spaces.sizeToToken` mirrors Space Agent-style widget size normalization for presets, bounded object sizes, and invalid sizes with safe fallbacks, while omitting renderer/html/source/API auth markers from serialized adapter responses.
  - Validation at completion before screenshot/restart: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`127 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed.

- `feat(spaces): support source current widget mutation aliases`
  - Added RED/GREEN backend coverage proving `space.current.patchWidget` accepts Space Agent-style active-space and widget ids, patches safe title/layout metadata from source-style position/size fields, and `space.current.reloadWidget` queues safe widget-refresh event metadata, with both responses omitting renderer/html/data/source bodies plus credential-like markers.
  - Validation at completion before screenshot/restart: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`2 passed`). Run `git log -1 --oneline` and the final report for the full validation bundle.

- `feat(spaces): support source current widget aliases`
  - Added RED/GREEN backend coverage proving `space.current.listWidgets`, `space.current.readWidget`, and `space.current.seeWidget` accept Space Agent-style active-space and widget ids, return safe widget summaries/details/contract/event metadata, and omit renderer/html/data/source bodies plus credential-like markers from serialized adapter results.
  - Validation at completion before screenshot/restart: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`124 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed.

- `feat(spaces): support source resolve app URL alias`
  - Added RED/GREEN backend coverage proving `space.spaces.resolveAppUrl` accepts Space Agent-style logical app paths, returns safe app URLs for home, user-space assets, `/app/...` module paths, and `L0`/`L1`/`L2` module paths, rejects `javascript:`, external HTTPS, relative traversal, query-string credential markers, and private filesystem roots without echoing raw unsafe input, omits source/API auth markers from serialized adapter results, and now includes metadata-only Action policy evidence for the browser/app navigation boundary.
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

- `feat(spaces): support source runtime collection aliases`
  - Added RED/GREEN backend coverage proving `space.spaces.items`, `space.spaces.all`, `space.spaces.byId`, `space.spaces.current`, `space.spaces.currentId`, `space.current.byId`, `space.current.agentInstructions`, and `space.current.specialInstructions` expose the Space Agent-style runtime namespace through safe Capy metadata only.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`135 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock/status screenshot QA captured the alias status with empty browser console and clean visible leak check; screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_c3b4c959b7fb4ea6a25ccb6847a7d799.png`.

- `feat(spaces): support source widget read aliases`
  - Added RED/GREEN backend coverage proving `space.spaces.listWidgets`, `readWidget`, and `getWidget` accept Space Agent-style camelCase `spaceId`/`widgetId` payloads, return safe widget summaries/details, and omit generated/executable bodies plus credential-like markers.
  - Validation at completion: focused RED failed before implementation with `Unsupported Capy Spaces tool action`; focused GREEN passed (`1 passed`), full Spaces foundation suite passed (`115 passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py`, and `git diff --check` passed. Mock/status screenshot QA captured the alias status with empty browser console and a clean visible safety-marker check; screenshot artifact `/Users/bschmidy10/.hermes/cache/screenshots/browser_screenshot_0a61b4f7766743658d0eb2bdf1715e1d.png`.

- `feat(spaces): support source space duplicate alias`
  - Added RED/GREEN backend coverage proving `space.spaces.duplicateSpace` / `cloneSpace` accept Space Agent-style camelCase `spaceId` payloads, create a safe copied Space with widget summaries and metadata, return metadata-only autonomy-policy plus `space.duplicate:<space_id>` progress receipts for the creation boundary, and omit generated/executable bodies plus credential-like markers from serialized adapter results and the persisted duplicate.
  - Validation at completion: initial focused RED failed before implementation with `Unsupported Capy Spaces tool action`; this follow-up RED failed on missing `autonomy_policy`; focused GREEN passed (`5 passed` across duplicate/clone receipt and conflict coverage). Final validation passed: targeted duplicate/clone + patch/notes regression bundle (`8 passed`), full suite (`6183 passed, 2 skipped, 3 xpassed, 8 subtests passed`), `py_compile api/spaces.py tests/test_spaces_foundation.py tests/test_spaces_demo_parity.py`, `node --check static/spaces.js`, `git diff --check`, and mock-state browser screenshot QA with empty console/page errors. During full-suite validation, fixed an overbroad widget.patch body-key redaction regression so safe notes/markdown bodies survive only on content widgets while generated/raw body keys remain omitted.

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
- `feat(spaces): expose provider setup walkthrough`
  - Added a direct Spaces toolbar action for `demo_provider_setup`, with safe provider-widget management and event-inbox rendering from the existing metadata-only demo route.
  - Added real-`static/spaces.js` regression coverage proving the action posts exactly `{demo: "demo_provider_setup"}`, fetches the safe widget/event summaries, and omits generated renderer/source/API-auth markers.
- `feat(spaces): use active space for revision tool aliases`
  - Added `space.current.revisions` / `space.current.history` and `space.current.rollback` / `space.current.restore` aliases so Hermes-style tool calls can list and restore revision snapshots from the active Space without raw generated bodies.
  - Validation at completion: focused active-space rollback adapter test passed, full Spaces foundation suite passed (`96 passed`), `py_compile` and `git diff --check` passed.
- `feat: preflight Space Agent imports`
  - Added active Space instruction prompt-preflight to Space Agent YAML/ZIP imports so role-override/system-prompt-exfiltration instructions are blocked before any Space/widget persistence.
  - Added metadata-only prompt-preflight and action-policy receipts for successful imports, including creator-commit/generated-widget-execution gates and `hint:reasoning` routing evidence without echoing raw active instructions.
- `7aabd6b6 feat: add widget patch safety receipts`
  - Added metadata-only `prompt_preflight`, `autonomy_policy`, and `progress_event` receipts to direct `widget.patch` / `space.widget.patch` / `space.current.widget.patch` tool-route mutations.
  - Widget patch metadata now uses a stricter patch-only payload summarizer so unsafe patch keys such as renderer/source/html/script/token/raw_prompt/generated body/body are omitted before preflight and persistence, while existing non-patch safe notes-body detail behavior stays intact.
- `9334d648 feat: add patchwidget alias safety receipts`
  - Extended the same creator-commit preflight, `hint:fast` autonomy policy, and `widget.patch:<space_id>` progress receipts to legacy `space.spaces.patchWidget` / `space.current.patchWidget` alias helpers.

Last known validation bundle:

- RED check for widget.patch receipts/leak coverage: extended regression failed before implementation (`status == 400` / unsafe body-style patch metadata not safely accepted).
- RED check for legacy patchWidget alias receipts: focused alias regressions failed before implementation with missing `prompt_preflight`.
- Focused widget.patch receipt/leak regression: passed (`1 passed`).
- Focused patchWidget alias receipt regressions: passed (`2 passed`).
- Full Spaces foundation suite: passed (`515 passed`).
- `py_compile api/spaces.py`: passed.
- `git diff --check`: passed.
- Spec review: PASS after body/generated-body leak gap was fixed; code quality review approved the receipt approach with no critical/important issues.
- Browser/Visual QA: fallback Chrome headless harness rendered the widget.patch metadata-only receipt using checked-out Spaces CSS; hierarchy, spacing, alignment, density, and readability looked clean, and rendered-text leak checks found no secret/body/generated-body/raw-prompt/source/token/script markers. Screenshot artifact: `/tmp/capy-widget-patch-receipt-qa.png`.
- WebUI local/tailnet health: local `/health` returned OK after LaunchAgent restart, tailnet `https://capy.tail9c6e3.ts.net/health` returned OK, Hermes gateway service `ai.hermes.gateway` was loaded/running, and Tailscale Serve still points `https://capy.tail9c6e3.ts.net/` to `http://127.0.0.1:8787`.

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

For sandboxed widgets, the currently enabled postMessage bridge is a strict allowlist:

- `capy:ready`
- `capy:agent:prompt`
- `capy:resize`

Future read/write/data/asset bridge candidates such as `capy:data:get`, `capy:data:put`, and `capy:asset:url` remain not-yet-enabled and must stay fail-closed until a separate safe data/assets contract lands with tests.

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

Acceleration update: use a conveyor workflow to move faster without weakening gates. Keep one serialized implementation lane for shared production files such as `static/spaces.js` and `api/spaces.py`, while parallel prep/review lanes draft acceptance criteria, RED tests, `/tmp` browser harnesses, and security reviews for upcoming slices. Every behavior slice still requires RED/GREEN TDD, scoped validation, visual QA when UI-facing, and a small committed checkpoint before the next implementation lane starts.

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
2. Operate the accelerated conveyor: keep production edits serialized, but prepare acceptance criteria, RED tests, browser harnesses, and review checklists for rollback, recovery, sandbox, Research Harness, and creator-loop work in parallel.
3. Expand safe recovery/admin UI so it can inspect metadata, disable/enable spaces/widgets/modules, launch a scoped repair prompt, and later roll back without rendering generated content.
4. Add rollback/time-travel MVP: revision list, diff/preview, widget rollback, full-space rollback, and recovery-mode rollback.
5. Drive the Research Harness vertical demo end-to-end using strict TDD.
6. Define the sandbox/postMessage/event contract before adding richer generated or trusted widget rendering.
7. Add an explicit generic creator-loop track after those gates: prompt → bounded space/widget spec → sandboxed preview → visual QA → patch/repair → revisioned commit/rollback. This is the platform unlock that moves Capy Spaces beyond curated demo cards.
8. Maintain screenshot/browser QA artifacts for UI-facing slices.

This gives a safe spine that future widget/tool/browser/share work can attach to without reworking the data model or expanding trust before recovery exists.

## Open Design Questions

- Should default storage be profile state or workspace-local `.capy/spaces/`? Recommendation: support both; default to workspace-local for project spaces, profile state for global/personal spaces.
- Should Space tools call WebUI API over HTTP or import shared Python storage functions directly? Recommendation: shared Python functions for in-process WebUI; HTTP adapter later for remote/gateway contexts.
- How should Telegram open/render spaces? Recommendation: send link to WebUI route plus static thumbnail; later add Telegram-native summaries.
- Which JS widget mode is acceptable? Recommendation: declarative first; sandboxed HTML second; trusted JS only behind explicit per-space setting.

## Final Recommendation

Build **Capy Spaces** as a Capy-native visual workspace system, not a repository clone. Use Space Agent as a UX and feature blueprint, but map it onto Hermes’ existing strengths: typed tools, approval flow, skills, memory, session search, gateway persistence, checkpoints, browser/CDP tools, and WebUI streaming. This path reaches full functional parity while avoiding Space Agent’s highest-risk assumption: letting browser-side generated JavaScript be the primary operating substrate.
