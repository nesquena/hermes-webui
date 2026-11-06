# Agent workflow scope — local validation

Pointer source: AGENTS.md (headroom pointer pass, 2026-11-05). Moved verbatim from
`AGENTS.md`; these rules define what an AI assistant may do in this repo without
per-step approval.

## Local validation scope

Within scope: local tests with disposable fixtures, fix change-caused failures, rerun
affected tests — no per-step approval, only with confirmed isolated state, no live
credentials/services (not a network sandbox).

Never authorizes real state changes, credentials, service restarts, or non-localhost
exposure — explicit human approval; onboarding safety rules still apply.

Blocked → report the blocker, don't claim completion.
