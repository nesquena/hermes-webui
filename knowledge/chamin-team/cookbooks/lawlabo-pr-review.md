---
id: lawlabo-pr-review
name: LawLabo PR Review
owner_employee: chamin
goal: Review a LawLabo branch or PR for correctness, scope, and release risk.
non_goals:
  - Do not implement fixes unless Kei explicitly asks.
  - Do not mix Astro, Next, and Shared scopes in one verdict.
  - Do not review finance-domain workflows from the source pattern repo.
triggers:
  - "review this PR"
  - "OK or need fix?"
  - "check this branch"
  - "Claude did this; review it"
default_lanes:
  - correctness-reviewer
optional_lanes:
  - security-reviewer
  - data-auth-reviewer
  - frontend-ui-a11y-reviewer
  - content-claims-seo-reviewer
  - qa-critic
max_workers: 3
max_writer_lanes: 0
human_gates:
  - security or data leak risk
  - payment/auth/admin production path risk
  - legal-adjacent public claim risk
output_shape:
  - conclusion
  - findings
  - evidence checked
  - verification
  - remaining risk
---

# LawLabo PR Review Cookbook

Use this cookbook when Kei asks Chamin to review LawLabo work, especially work produced by Claude/Taeyoon.

## Scope Declaration

Declare one scope before review:
- Astro-only
- Next-only
- Shared-only

If the PR crosses surfaces, report the scope problem first.

## Routing

Default:
- `correctness-reviewer`

Add lanes only when triggered:
- `security-reviewer`: secrets, authn/authz, injection, headers, unsafe tools.
- `data-auth-reviewer`: Supabase, RLS, sessions, admin, PII, tenancy, service role.
- `frontend-ui-a11y-reviewer`: UI, layout, mobile, focus, contrast, hydration.
- `content-claims-seo-reviewer`: public copy, metadata, schema, pricing, legal-adjacent claims.
- `qa-critic`: high-risk change, multi-lane review, or uncertain evidence.

## Acceptance Criteria

Chamin can close the review when:
- scope is declared;
- changed files are inspected;
- triggered lanes have receipts or Chamin did the equivalent inspection;
- findings are ordered by severity;
- verification commands or missing verification are stated;
- final answer includes residual risk.

## Output

```markdown
Conclusion:
<verdict>

Findings:
- [severity] issue - evidence: path:line or command - handoff: action

Evidence checked:
- paths, commands, URLs, logs

Verification:
- run/not run and result

Remaining risk:
- explicit gaps
```

## Stop Conditions

Stop and ask Kei before:
- editing code;
- destructive operations;
- deploy/publish/send;
- crossing app scope;
- turning a review into a broad redesign.
