# Authoritative Insights model-cost reconciliation

## Upstream Candidate

Classification: upstream-candidate
Upstream base: `origin/master` at `e168b67e4278df618d1cab61fdb3a8dc55b29a81`; implemented on fork deployment baseline `16dc94ae884845b43016f798444762f97b7030ff`.
Problem: The Insights Models table derives model identity and estimated cost from the compact WebUI session index, so provider-qualified aliases can split one model and backfilled per-model ledger costs can appear as `N/A` or zero.
Reproduction: Record a WebUI session under a provider-qualified model alias, backfill authoritative cost in Agent `session_model_usage`, and request `/api/insights`; the legacy model table continues to use stale index values.
Expected behavior: When the Agent proves exact, internally consistent coverage, Insights uses authoritative model, daily, token, cache, and cost attribution; any unavailable, malformed, partial, or inconsistent response preserves the complete legacy result atomically.
Actual behavior: The endpoint has no supported path to consume the Agent ledger and can double-count session identities appearing in both the index and state database.
Root cause: `/api/insights` aggregates the WebUI index and non-WebUI state rows independently and has no validated reconciliation contract with Agent Insights.
Change: Consume the Agent public usage API through a read-only lifecycle; validate identity coverage and model/daily/total parity; merge provably index-only residuals; deduplicate index/state overlap by identity with state-authoritative fallback; reject out-of-window dates.
Verification: `.venv/bin/python -m pytest tests/test_insights.py -q` (30 passed); Ruff on changed tests; `py_compile` for `api/routes.py`; diff checks; live read-only probe showing one reconciled Fable row with model and daily totals equal.
Compatibility / risk: Older or incompatible Agent versions trigger the unchanged legacy response. The consumer is lazy and either repository may deploy first, though Agent-first activates the feature immediately after WebUI restart.
Maintenance owner: Brent Atchison / `atchisonbrent/hermes-webui` fork until upstream accepts or independently provides equivalent reconciliation.
Rollback: Revert the retained commit; `/api/insights` returns to legacy index/state aggregation without changing stored usage or cost data.
Upstream status: issue-ready
Private details removed: yes

## Reclassification trigger

Retire this fork delta when upstream Insights natively consumes authoritative Agent model usage. Rework it if Agent and WebUI become separate-host services with a versioned API and explicit timezone contract.
