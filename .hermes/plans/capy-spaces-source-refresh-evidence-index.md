# Capy Spaces Source-Refresh Evidence Index

Created: 2026-06-30 21:01 CDT
Last count refresh: 2026-07-09 12:58 CDT
Branch: `feat/capy-spaces-foundation`

## Purpose

This is a review-oriented companion index for the large Capy Spaces / Memory Tree sprint history. The roadmap, parity plan, and demo checklist still retain the chronological evidence log, but reviewers should start here when assessing the accumulated GitHub source-refresh safety work.

The index is intentionally docs-only. It does not change the Memory Tree implementation, tests, or route contracts.

## Clean-room and safety boundaries

- OpenHuman remains product inspiration only; do not copy GPLv3 OpenHuman code, tests, schemas, comments, fixtures, or prompts.
- Source-refresh content is untrusted advisory context. It must not bypass prompt-preflight, approval, sandbox preview, visual-QA, recovery, rollback, or creator-loop gates.
- Public route/job/catalog/search/relevant-memory receipts stay metadata-only and must not expose raw fetched bodies, renderer/source/html/script/data fields, raw prompts, credentials, tokens, API-auth fields, raw final URLs, or secret-looking fixture values.

## Reviewer entry points

| Review goal | Start here | Then inspect |
| --- | --- | --- |
| Route hardening contract | `api/capy_memory.py` final-URL/path matchers and source-refresh worker | Focused `tests/test_capy_memory_tree.py` route regression names from the family under review |
| Evidence that hostile drift fails closed | `tests/test_capy_memory_tree.py` tests containing `final_url_drift`, `before_body_read`, and `relevant_memory_empty` | Plan evidence paragraphs only as historical context |
| Product-visible safety receipts | `api/routes.py`, `api/spaces.py`, `static/spaces.js` | `tests/test_spaces_foundation.py`, `tests/test_spaces_ui_js_behaviour.py`, visual QA evidence in integration readiness plan |
| Integration handoff / PR slicing | `.hermes/plans/capy-spaces-integration-readiness-2026-06-28.md` | This index plus the canonical roadmap status summary |

## Evidence snapshot

Snapshot generated from the checked-out `tests/test_capy_memory_tree.py` on 2026-07-09 12:58 CDT:

| Search/count | Count | Meaning |
| --- | ---: | --- |
| `def test_.*github.*(?:final_url|redirect).*drift` | 135 | GitHub source-refresh drift regressions across route families. |
| `def test_.*before_body_read` | 130 | Regressions that explicitly name no-body-read behavior. |
| `def test_.*relevant_memory_empty` | 130 | Regressions that also prove Spaces relevant-memory remains empty after hostile drift. |
| `def test_.*ingests_github.*metadata_only` | 154 | Positive metadata-only GitHub ingestion regressions. |

### Lag-scan status

The 2026-07-09 12:58 CDT autonomous sprint selector reran the local-only relevant-memory lag scans from `capy-spaces-development/references/relevant-memory-name-count-lag-scan.md` after aligning the stale GitHub secret-scanning alert-locations malformed-route regression so its name advertises both before-fetch/before-body-read and relevant-memory-empty evidence. The checked-out `tests/test_capy_memory_tree.py` now has no remaining candidates:

| Scan | Result | Meaning |
| --- | ---: | --- |
| Strict name/count lag: existing `relevant_memory_for_space(...)` call and `relevant["results"] == []` assertion, but no `_relevant_memory_empty` name | 0 | No route-specific GitHub drift regression already proving relevant-memory-empty is missing the mechanical name/count marker. |
| Missing-assert variant: existing `relevant_memory_for_space(...)` call but no explicit `relevant["results"] == []` assertion | 0 | No drift regression with a relevant-memory lookup still needs the empty-results assertion added. |
| No-relevant-call variant: no-body-read + no-search drift regression without a `relevant_memory_for_space(...)` lookup | 0 | No obvious older GitHub drift regression remains in this evidence-alignment backlog. |

Future autonomous sprints should avoid re-running this lane as the default work queue unless new route families are added. Prefer the canonical roadmap's next implementation slices: remaining prompt-preflight/advisory-memory enforcement, broader compaction producers, source-refresh scheduling/fetcher coverage, progress producer expansion, and model-route invocation plumbing.

### Regeneration recipe

Use this local-only Python snippet from the repository root to refresh the mechanical counts above before any future index update. It parses test function names only; it does not execute tests, open network connections, or inspect private vault content.

```bash
python3 - <<'PY'
from pathlib import Path
import re
text = Path('tests/test_capy_memory_tree.py').read_text()
funcs = re.findall(r'^def (test_[A-Za-z0-9_]+)\(', text, re.M)
patterns = {
    'github drift regressions': r'test_.*github.*(?:final_url|redirect).*drift',
    'before-body-read regressions': r'test_.*before_body_read',
    'relevant-memory-empty regressions': r'test_.*relevant_memory_empty',
    'positive metadata-only GitHub ingestion regressions': r'test_.*ingests_github.*metadata_only',
}
for label, pattern in patterns.items():
    print(f'{label}:', sum(1 for name in funcs if re.search(pattern, name)))
PY
```

The family buckets below are curated navigation aids over those function names. When the mechanical counts change, refresh this table in the same commit and keep the bucket notes reviewer-oriented rather than appending another chronological paragraph.

Coarse drift-test family buckets from function names:

| Family bucket | Drift-test count | Reviewer notes |
| --- | ---: | --- |
| Actions / workflows / runners / artifacts / cache / OIDC | 46 | High-value because these routes often expose nested IDs, policy metadata, artifact handles, and secret-adjacent fields. |
| Issues / PRs / comments / reactions / labels / timeline / milestones | 23 | Review exact owner/repo/number/comment-id parity and no-leak assertions for hostile bodies and actor/user fields. |
| Security / Dependabot / code scanning / secret scanning / private vulnerability reporting | 17 | Prioritize metadata-only output and no public leakage of advisory, secret, key, or vulnerability details. |
| Deployments / Pages / environments | 16 | Check route-tail and ID drift; these routes mix repo IDs, deployment IDs, environment names, and status rows. |
| Repository metadata / contents / README / license / languages / branches / tags / topics / rulesets / properties | 11 | Confirm raw content bodies and URLs remain out of public receipts. |
| Traffic / stats / social surfaces | 11 | Confirm aggregate counts/previews only and no actor/body leakage. |

These buckets are navigation aids, not a substitute for tests. Some tests cover multiple route concepts and may be counted in the most obvious bucket only.

## Canonical source-refresh invariant

For every route-specific source-refresh connector, reviewers should expect the same invariant unless a plan explicitly narrows it:

1. The registered source resolves to an exact sanitized HTTPS `api.github.com` route for the intended owner/repo/org/id.
2. Noncanonical origins, malformed paths, unsafe IDs, query/userinfo/fragment auth material, raw prompts, or lookalike hosts fail before network dispatch where the route contract requires it.
3. The response final URL is checked before any response-body read, JSON parsing, vault persistence, search indexing, or public receipt generation.
4. Drift leaves the job pending or refresh-failed, with no vault/search artifact and, where covered, no Spaces relevant-memory artifact.
5. Public results/jobs/catalog/search/relevant-memory envelopes remain metadata-only and omit raw final URLs, fetched bodies, prompts, renderer/source/html/script/data/API-auth fields, credentials, tokens, and secret-looking values.
6. Positive fixtures still prove bounded metadata-only ingestion for the canonical route.

## Documentation hygiene going forward

- Prefer adding compact rows to this index or a future appendix table over appending duplicate paragraph-length route summaries to every plan file.
- Keep the top of `capy-openhuman-inspired-roadmap.md`, `capy-spaces-space-agent-parity.md`, and `capy-spaces-video-demo-parity-checklist.md` focused on current status, next recommendation, and review entry points.
- When counts change, refresh the mechanical-count table with the regeneration recipe above and include the exact validation bundle in the commit message or sprint report.
- If a new route family is still necessary before integration, add one focused regression, one concise index row/update, and only a short status line in the three existing plan docs.
- Do not remove historical evidence until Brendan explicitly asks for a doc compaction/rewrite pass; this file is a reversible index layer over the existing logs.
