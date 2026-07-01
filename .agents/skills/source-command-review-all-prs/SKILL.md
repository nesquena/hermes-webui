---
name: "source-command-review-all-prs"
description: "Batch deep PR review — invoke /review-pr (full process) on every open PR, push fixes where needed, formal APPROVE/REQUEST_CHANGES on each. Stops at review verdict; merge/tag/release belongs to a separate release agent."
---

# source-command-review-all-prs

Use this skill when the user asks to run the migrated source command `review-all-prs`.

## Command Template

# Review All Open PRs

Run a full deep review on every open PR on <https://github.com/nesquena/hermes-webui>. This is review-only — no merging, tagging, or release work.

## Process

### 1. Survey

- `gh pr list --state open --json number,title,author,headRefName`
- Fetch ALL details, diffs, and agent review comments in parallel
- Read every agent review comment carefully — they catch real bugs

### 2. Triage by review priority

- **Ready for full review**: green CI, agent has reviewed (or no auto-review), branch up to date
- **Stale**: needs `git rebase origin/master` first so the trace and tests reflect the actual proposed combined state — rebase if `maintainerCanModify` is true; otherwise leave a comment asking the contributor to rebase
- **Blocked**: external contributor needs design discussion before review work pays off — comment and skip

### 3. Run the full /review-pr process on each ready PR

Apply `.Codex/commands/review-pr.md` end-to-end per PR:

- Pull a fresh hermes-agent tarball (once per session, reuse for all PRs)
- Read the linked issue + PR body + commits + agent comments
- Security audit the diff before running anything
- `gh pr checkout` and trace against the proposed state
- End-to-end trace through both repos with file:line citations
- Build a behavioural harness when tests are static-analysis only
- Walk an edge-case matrix; verify the PR actually solves the issue
- Run the PR's tests AND the full suite
- Push fixes and regression tests directly to the PR branch when defects surface
- Submit `event=APPROVE` or `event=REQUEST_CHANGES` via the Reviews API
- Post the end-of-review summary

### 4. Final summary to the user

For each PR, report:

- Outcome (APPROVE / REQUEST_CHANGES / left a comment for the contributor)
- The most important thing the trace caught (if anything)
- Any commit links for fixes pushed
- Test counts (PR's tests + relevant slice of the full suite)

## What I do NOT do

- I do not merge any PR (`gh pr merge`).
- I do not create or push tags (`git tag`, `git push origin vX.Y.Z`).
- I do not bump the version label or write CHANGELOG version sections.
- I do not update SPRINTS / README / ROADMAP / THEMES.
- After approval, the PR is parked for the release agent.

## Decision framework

- Small focused defect → fix ourselves, push to the PR branch with a regression test
- Fundamental design issue → `event=REQUEST_CHANGES` with specific code samples and reasons
- Trace + tests verify correctness → `event=APPROVE`
- Genuine uncertainty → leave a detailed comment and skip the verdict; ask the agent or contributor for clarification
