# Capy Spaces Integration Readiness — 2026-06-28

## Scope

This checkpoint covers the `feat/capy-spaces-foundation` branch in `/Users/bschmidy10/hermes-webui` after the autonomous Capy Spaces / Memory Tree hardening sprint reached 270 local commits ahead of `capy-fork/feat/capy-spaces-foundation`.

2026-06-30 refresh: the active branch was clean and 313 commits ahead before the first documentation checkpoint commit, then clean and 315 commits ahead before the 19:53 CDT targeted-validation refresh. 2026-07-01 00:23 CDT refresh: the active branch was clean and 319 commits ahead; the same targeted Memory Tree/Spaces checkpoint command passed `3417 passed, 3 warnings in 52.37s`, `node --check static/spaces.js`, Python compile checks, `git diff --check`, and the source-refresh evidence-index count recipe remained unchanged at 134 GitHub drift regressions, 111 before-body-read regressions, 26 relevant-memory-empty regressions, and 154 positive metadata-only GitHub ingestion regressions. 2026-07-01 01:33 CDT refresh: the branch was clean and 320 commits ahead; the same targeted command passed `3417 passed, 3 warnings in 51.43s`, `node --check static/spaces.js`, Python compile checks, `git diff --check`, and the source-refresh evidence-index count recipe again remained unchanged at 134 GitHub drift regressions, 111 before-body-read regressions, 26 relevant-memory-empty regressions, and 154 positive metadata-only GitHub ingestion regressions. 2026-07-01 02:42 CDT refresh: the branch was clean and 321 commits ahead; the same targeted command passed `3417 passed, 3 warnings in 51.44s`, `node --check static/spaces.js`, Python compile checks, `git diff --check`, and the source-refresh evidence-index count recipe remained unchanged at 134 GitHub drift regressions, 111 before-body-read regressions, 26 relevant-memory-empty regressions, and 154 positive metadata-only GitHub ingestion regressions. 2026-07-01 03:52 CDT refresh: the branch was clean and 322 commits ahead; the same targeted command passed `3417 passed, 3 warnings in 55.22s`, `node --check static/spaces.js`, Python compile checks, `git diff --check`, and the source-refresh evidence-index count recipe remained unchanged at 134 GitHub drift regressions, 111 before-body-read regressions, 26 relevant-memory-empty regressions, and 154 positive metadata-only GitHub ingestion regressions. 2026-07-01 05:02 CDT refresh: the branch was clean and 323 commits ahead; the same targeted command passed `3417 passed, 3 warnings in 67.15s`, `node --check static/spaces.js`, Python compile checks, `git diff --check`, and the source-refresh evidence-index count recipe remained unchanged at 134 GitHub drift regressions, 111 before-body-read regressions, 26 relevant-memory-empty regressions, and 154 positive metadata-only GitHub ingestion regressions. 2026-07-01 06:10 CDT refresh: the branch was clean and 324 commits ahead; the same targeted command passed `3417 passed, 3 warnings in 52.00s`, `node --check static/spaces.js`, Python compile checks, `git diff --check`, and the source-refresh evidence-index count recipe remained unchanged at 134 GitHub drift regressions, 111 before-body-read regressions, 26 relevant-memory-empty regressions, and 154 positive metadata-only GitHub ingestion regressions. Because the branch has crossed the 300-commit checkpoint threshold, autonomous sprints should prefer integration/doc consolidation, review-stack validation, and stabilization over adding new source-refresh route families until Brendan decides how to land or split the review stack.

Review/navigation update: `.hermes/plans/capy-spaces-source-refresh-evidence-index.md` is now the compact reviewer entry point for the noisy GitHub source-refresh route history. Use it for route-family counts, canonical safety invariants, and reviewer navigation before reading the chronological per-route logs in the roadmap/parity/checklist plans.

Backup branch created before integration planning:

```text
backup/capy-spaces-pre-integration-20260628-164146
```

No destructive history rewrite was performed on the active branch during this checkpoint.

## Stabilization result

Targeted verification command:

```bash
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_capy_memory_tree.py \
  tests/test_spaces_foundation.py \
  tests/test_spaces_ui_js_behaviour.py \
  tests/test_capy_policy.py \
  tests/test_session_recovery_api.py \
  -q -o 'addopts='
node --check static/spaces.js
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m py_compile \
  api/capy_memory.py api/spaces.py api/routes.py api/session_recovery.py \
  tests/test_capy_memory_tree.py tests/test_spaces_foundation.py tests/test_spaces_ui_js_behaviour.py
git diff --check
```

Observed targeted result:

```text
3400 passed, 3 warnings in 50.98s
node --check static/spaces.js: passed with no output
py_compile: passed with no output
git diff --check: passed with no output
```

Observed 2026-06-30 checkpoint refresh on the same command after the branch reached 313 local commits ahead:

```text
3417 passed, 3 warnings in 100.02s
node --check static/spaces.js: passed with no output
py_compile: passed with no output
git diff --check: passed with no output
```

Observed 2026-06-30 19:53 CDT targeted-validation refresh on the same command after the branch reached 315 local commits ahead:

```text
3417 passed, 3 warnings in 51.58s
node --check static/spaces.js: passed with no output
py_compile: passed with no output
git diff --check: passed with no output
git status --short --branch: clean on feat/capy-spaces-foundation, ahead 315
```

Observed 2026-07-01 00:23 CDT targeted-validation refresh on the same command after the branch reached 319 local commits ahead:

```text
3417 passed, 3 warnings in 52.37s
node --check static/spaces.js: passed with no output
py_compile: passed with no output
source-refresh evidence index count recipe: 134 GitHub drift regressions; 111 before-body-read regressions; 26 relevant-memory-empty regressions; 154 positive metadata-only GitHub ingestion regressions
git diff --check: passed with no output
git status --short --branch: clean on feat/capy-spaces-foundation, ahead 319
```

Observed 2026-07-01 01:33 CDT targeted-validation refresh on the same command after the branch reached 320 local commits ahead:

```text
3417 passed, 3 warnings in 51.43s
node --check static/spaces.js: passed with no output
py_compile: passed with no output
source-refresh evidence index count recipe: 134 GitHub drift regressions; 111 before-body-read regressions; 26 relevant-memory-empty regressions; 154 positive metadata-only GitHub ingestion regressions
git diff --check: passed with no output
git status --short --branch: clean on feat/capy-spaces-foundation, ahead 320
```

Observed 2026-07-01 02:42 CDT targeted-validation refresh on the same command after the branch reached 321 local commits ahead:

```text
3417 passed, 3 warnings in 51.44s
node --check static/spaces.js: passed with no output
py_compile: passed with no output
source-refresh evidence index count recipe: 134 GitHub drift regressions; 111 before-body-read regressions; 26 relevant-memory-empty regressions; 154 positive metadata-only GitHub ingestion regressions
git diff --check: passed with no output
git status --short --branch: clean on feat/capy-spaces-foundation, ahead 321
```

Observed 2026-07-01 03:52 CDT targeted-validation refresh on the same command after the branch reached 322 local commits ahead:

```text
3417 passed, 3 warnings in 55.22s
node --check static/spaces.js: passed with no output
py_compile: passed with no output
source-refresh evidence index count recipe: 134 GitHub drift regressions; 111 before-body-read regressions; 26 relevant-memory-empty regressions; 154 positive metadata-only GitHub ingestion regressions
git diff --check: passed with no output
git status --short --branch: clean on feat/capy-spaces-foundation, ahead 322
```

Observed 2026-07-01 05:02 CDT targeted-validation refresh on the same command after the branch reached 323 local commits ahead:

```text
3417 passed, 3 warnings in 67.15s
node --check static/spaces.js: passed with no output
py_compile: passed with no output
source-refresh evidence index count recipe: 134 GitHub drift regressions; 111 before-body-read regressions; 26 relevant-memory-empty regressions; 154 positive metadata-only GitHub ingestion regressions
git diff --check: passed with no output
git status --short --branch: clean on feat/capy-spaces-foundation, ahead 323
```

Observed 2026-07-01 06:10 CDT targeted-validation refresh on the same command after the branch reached 324 local commits ahead:

```text
3417 passed, 3 warnings in 52.00s
node --check static/spaces.js: passed with no output
py_compile: passed with no output
source-refresh evidence index count recipe: 134 GitHub drift regressions; 111 before-body-read regressions; 26 relevant-memory-empty regressions; 154 positive metadata-only GitHub ingestion regressions
git diff --check: passed with no output
git status --short --branch: clean on feat/capy-spaces-foundation, ahead 324
```

Warnings remained third-party deprecation warnings from `discord`, `lark_oapi`, and `websockets`; no test failures.

Observed 2026-07-01 13:06 CDT refreshed review-branch checkpoint after the active sprint branch reached 330 commits ahead and before PR handoff. The non-destructive review branch `review/capy-spaces-memory-tree-stack-20260701` was regenerated from `capy-fork/feat/capy-spaces-foundation`, compressed into four review commits, and verified tree-equivalent to the active sprint branch.

```text
targeted pytest: 3421 passed, 3 warnings in 66.32s
node --check static/spaces.js: passed with no output
py_compile: passed with no output
source-refresh evidence index count recipe: 134 GitHub drift regressions; 111 before-body-read regressions; 31 relevant-memory-empty regressions; 154 positive metadata-only GitHub ingestion regressions
git diff --check: passed with no output
full pytest: 8826 passed, 2 skipped, 3 xpassed, 3 warnings, 8 subtests passed in 176.12s
```

Warnings remained third-party deprecation warnings from `discord`, `lark_oapi`, and `websockets`; no test failures.

Observed 2026-06-30 visual/UI checkpoint for the UI-visible Spaces surfaces used a temporary `/tmp/capy_spaces_visual_harness.py` that served the checked-out `static/index.html` and real `static/spaces.js`/CSS with metadata-only mock Spaces, Memory Tree, source-refresh, policy, progress, and recovery API envelopes. Browser-harness against the user's visible Chrome was blocked by the unattended Chrome remote-debugging permission prompt, so the run used the documented headless/Playwright fallback. The harness reached `qaReady`, browser console warnings/errors were zero after the final mock pass, rendered Memory freshness / Source refresh queue / Connector catalog / Autonomy policy / Progress events / Safe recovery / QA Space evidence, and the rendered DOM leak scan found no `SECRET_VALUE_DO_NOT_LEAK`, `raw-prompt`, `<script`, `api_key`, `access_token`, or generated widget body markers. Screenshot evidence: `/Users/bschmidy10/.hermes/capy-spaces-visual-qa-final-2026-06-30.png`.

Observed 2026-07-01 13:06 CDT visual/UI refresh used temporary harness `/tmp/capy_spaces_visual_harness_20260701.py` serving checked-out `static/index.html`, real `static/spaces.js`, and real CSS with metadata-only mock Spaces, Memory Tree, source-refresh, policy, progress, recovery, onboarding, and detail API envelopes. The final browser pass rendered Memory freshness / Source refresh queue / Connector catalog / Autonomy policy / Progress events / Safe recovery / QA Space / Memory Tree context / recovery rollback surfaces, console warnings/errors were zero, and DOM leak scans found no `SECRET_VALUE_DO_NOT_LEAK`, `raw-prompt`, `<script`, `api_key`, `access_token`, `generated widget body`, `generated_code`, or `bearer placeholder` markers. Screenshot evidence: `mcp-output/playwright/capy-spaces-visual-qa-20260701-final.png` and `mcp-output/playwright/capy-spaces-visual-qa-20260701-detail.png`.

Observed full-suite result after the earlier review branch was created and the worktree returned to `feat/capy-spaces-foundation`:

```text
8805 passed, 2 skipped, 3 xpassed, 3 warnings, 8 subtests passed in 137.71s
```

Warnings were third-party deprecation warnings from `discord`, `lark_oapi`, and `websockets`; no test failures.

## Branch/diff state

Current branch:

```text
feat/capy-spaces-foundation
```

Remote comparison:

```text
270 commits ahead of capy-fork/feat/capy-spaces-foundation at the original 2026-06-28 checkpoint
313 commits ahead of capy-fork/feat/capy-spaces-foundation before the 2026-06-30 documentation refresh commit
315 commits ahead of capy-fork/feat/capy-spaces-foundation before the 2026-06-30 19:53 CDT targeted-validation refresh
319 commits ahead of capy-fork/feat/capy-spaces-foundation before the 2026-07-01 00:23 CDT targeted-validation refresh
320 commits ahead of capy-fork/feat/capy-spaces-foundation before the 2026-07-01 01:33 CDT targeted-validation refresh
321 commits ahead of capy-fork/feat/capy-spaces-foundation before the 2026-07-01 02:42 CDT targeted-validation refresh
322 commits ahead of capy-fork/feat/capy-spaces-foundation before the 2026-07-01 03:52 CDT targeted-validation refresh
323 commits ahead of capy-fork/feat/capy-spaces-foundation before the 2026-07-01 05:02 CDT targeted-validation refresh
324 commits ahead of capy-fork/feat/capy-spaces-foundation before the 2026-07-01 06:10 CDT targeted-validation refresh
```

Changed files versus tracked remote:

```text
.hermes/plans/capy-openhuman-inspired-roadmap.md
.hermes/plans/capy-spaces-space-agent-parity.md
.hermes/plans/capy-spaces-video-demo-parity-checklist.md
CAPY_RUNBOOK.md
api/capy_memory.py
api/capy_policy.py
api/routes.py
api/session_recovery.py
api/spaces.py
docs/capy-memory-tree.md
static/spaces.js
tests/test_capy_memory_tree.py
tests/test_capy_policy.py
tests/test_session_recovery_api.py
tests/test_spaces_foundation.py
tests/test_spaces_ui_js_behaviour.py
```

Largest changed files:

| File | Adds | Deletes | Interpretation |
|---|---:|---:|---|
| `tests/test_capy_memory_tree.py` | 54,833 | 12,456 | Main regression corpus for Memory Tree/source-refresh safety |
| `api/capy_memory.py` | 27,208 | 5,833 | Main implementation surface |
| `tests/test_spaces_foundation.py` | 1,674 | 56 | Spaces integration coverage |
| `api/spaces.py` | 721 | 85 | Spaces API integration |
| `.hermes/plans/capy-spaces-space-agent-parity.md` | 512 | 5 | Plan/status updates |
| `.hermes/plans/capy-spaces-video-demo-parity-checklist.md` | 492 | 7 | Demo parity status updates |
| `.hermes/plans/capy-openhuman-inspired-roadmap.md` | 256 | 27 | Roadmap status updates |

Commit subject counts:

```text
150 fix(capy-memory)
 62 feat(capy-memory)
 27 test(capy-memory)
 17 feat(capy-spaces)
  5 docs(capy-memory)
```

## Integration/squash strategy

Do not rewrite the active sprint branch directly. Use the backup branch above as recovery and create a separate review branch if squashing is desired.

Recommended reviewable stack:

### PR 1 — Memory Tree source-refresh safety core

Files:

```text
api/capy_memory.py
tests/test_capy_memory_tree.py
docs/capy-memory-tree.md
```

Focus:

- final-URL-before-body-read hardening,
- metadata-only source refreshes,
- relevant-memory no-artifact/no-leak behavior,
- route-specific GitHub API drift rejection,
- source-refresh job pending/failed semantics.

Reviewer guidance:

- Start with helper patterns and route-specific final-URL guards.
- Spot-check that every drift path fails before response body read / JSON parse / vault persistence.
- Check tests assert no vault/search/relevant-memory artifacts for hostile drift cases.

### PR 2 — Spaces integration and visible safety receipts

Files:

```text
api/spaces.py
api/routes.py
static/spaces.js
tests/test_spaces_foundation.py
tests/test_spaces_ui_js_behaviour.py
```

Focus:

- Spaces surfaces consuming Memory Tree/advisory context,
- metadata-only safety receipts,
- UI-visible compaction/provenance/status behavior,
- no raw prompt/widget/API-auth leakage.

Reviewer guidance:

- Prefer browser/visual QA for UI-visible changes after tests pass.
- Confirm receipt displays are metadata-only and do not expose raw bodies/secrets.

### PR 3 — Policy and recovery integration

Files:

```text
api/capy_policy.py
api/session_recovery.py
tests/test_capy_policy.py
tests/test_session_recovery_api.py
```

Focus:

- autonomy/policy visibility,
- recovery repair-safe behavior,
- policy receipt preservation.

Reviewer guidance:

- Verify policy/recovery changes cannot bypass approval, prompt-injection, sandbox, or recovery gates.

### PR 4 — Plans, runbook, and parity documentation

Files:

```text
.hermes/plans/capy-openhuman-inspired-roadmap.md
.hermes/plans/capy-spaces-space-agent-parity.md
.hermes/plans/capy-spaces-source-refresh-evidence-index.md
.hermes/plans/capy-spaces-video-demo-parity-checklist.md
CAPY_RUNBOOK.md
```

Focus:

- consolidate sprint status,
- remove repetitive per-route noise where possible,
- summarize route coverage in tables,
- define remaining gaps and acceptance criteria.

Reviewer guidance:

- Keep source-of-truth architecture and non-negotiables prominent.
- Move huge repeated route logs into tables or appendices.
- Start source-refresh safety review from `.hermes/plans/capy-spaces-source-refresh-evidence-index.md` before drilling into long chronological evidence paragraphs.

## Suggested non-destructive squash workflow

Only after another green stabilization pass:

```bash
cd /Users/bschmidy10/hermes-webui
git checkout feat/capy-spaces-foundation
git branch backup/capy-spaces-pre-squash-$(date +%Y%m%d-%H%M%S)
git checkout -b review/capy-spaces-memory-tree-stack-$(date +%Y%m%d)
git reset --mixed capy-fork/feat/capy-spaces-foundation
# Then add/commit by the four PR chunks above.
```

Do not force-push or delete the original sprint branch until the review stack is validated and accepted.

## PR readiness checklist

- [x] Create backup branch.
- [x] Run targeted Memory Tree/Spaces test suite. Latest 2026-07-01 13:06 CDT refresh on the regenerated review branch: `3421 passed, 3 warnings in 66.32s`.
- [x] Run JS syntax check for `static/spaces.js`.
- [x] Run Python compile check for changed backend/test files.
- [x] Run `git diff --check`.
- [x] Run a full repo pytest pass. Latest 2026-07-01 13:06 CDT review-branch pass: `8826 passed, 2 skipped, 3 xpassed, 3 warnings, 8 subtests passed in 176.12s`.
- [x] Run browser/visual QA for UI-visible Spaces surfaces: 2026-07-01 `/tmp/capy_spaces_visual_harness_20260701.py` using checked-out `static/index.html` + real `static/spaces.js`/CSS rendered Memory freshness / Source refresh queue / Connector catalog / Autonomy policy / Progress events / Safe recovery / QA Space / Memory Tree context / recovery rollback, final browser console warnings/errors were zero, and DOM leak scans found no hostile sentinels. Screenshots: `mcp-output/playwright/capy-spaces-visual-qa-20260701-final.png`, `mcp-output/playwright/capy-spaces-visual-qa-20260701-detail.png`.
- [x] Generate the non-destructive review branch with 4 chunk commits: `review/capy-spaces-memory-tree-stack-20260701`.
- [x] Push review branch / open draft PR after final green checks: `https://github.com/bschmidy10/hermes-webui/pull/1`.

## Autonomous sprint checkpoint policy

Until this 324-commit delta is integrated, the hourly autonomous sprint should shift from pure feature accumulation to checkpoint-aware hardening:

1. Every 10 new commits or 12 hours, run the targeted verification command above.
2. If tests fail, stop feature work and produce a stabilization-only fix.
3. If the branch exceeds 300 commits ahead, prefer integration/doc consolidation over adding new route families.
4. Keep new route work small: one route family, one focused drift/no-body-read behavior, one plan-doc update.
5. Avoid broad refactors until the review stack exists.
6. Record each checkpoint in this file or the canonical roadmap.

## Current recommendation

The refreshed `review/capy-spaces-memory-tree-stack-20260701` branch exists, is compressed into four review commits, is tree-equivalent to the active sprint branch as of the 330-commit checkpoint, and has passed targeted pytest, full pytest, static checks, and browser/visual QA. The next step is to push that review branch and open reviewable PRs/chunks from it.

Do not add another broad source-refresh route family unless a blocking safety gap is found during stabilization.
