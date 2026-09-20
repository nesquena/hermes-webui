# TICKETS — Hermes WebUI Agent Runtime Version Convergence

Spec: `SPEC.md`

Approved baseline: `origin/master @ 2cf8e8a5eae5a42deaa888d027c408defbe76eae` (`exp-v0.52.317`)

Legacy patch reference only: `8c2f185d8ae4e16a1ecc01dd4221978fafd14ca5`

## Dependency Graph

```text
HWA-001 Canonical live runtime resolver
        ↓
HWA-002 /api/settings runtime-first integration
        ↓
HWA-003 Regression + scope gate
        ↓
HWA-004 Git delivery + local/remote parity closeout
```

The tickets are intentionally sequential. HWA-001 establishes one authoritative gateway/version chokepoint. HWA-002 consumes it. HWA-003 proves no unrelated timeout/update behaviour moved. HWA-004 performs repository delivery and parity verification only after the product behaviour is green.

---

## HWA-001 — Canonical Live Agent Runtime Resolver

**Goal**

Create one small, per-request runtime-version resolver in `api/updates.py` that obtains the running Hermes Agent version from the configured gateway, using the same URL precedence, suffix normalization, and authentication semantics defined by the existing gateway-health subsystem.

This ticket does **not** wire the resolver into `/api/settings` yet.

**TDD order**

1. Add focused tests that fail on baseline `2cf8e8a5`.
2. Prove the failure is because the canonical resolver/semantics do not exist.
3. Add the minimum implementation to make those tests pass.

**Acceptance Criteria**

- [ ] A single `resolve_gateway_base_url() -> str | None` exists in `api/updates.py`.
- [ ] URL precedence is exactly:
  1. `GATEWAY_HEALTH_URL`
  2. `HERMES_GATEWAY_HEALTH_URL`
  3. `HERMES_API_URL`
  4. `HERMES_WEBUI_GATEWAY_BASE_URL`
- [ ] `/health/detailed`, `/health`, `/v1/health`, and `/status` suffixes are normalized consistently.
- [ ] A single `resolve_gateway_auth_headers()` honors `HERMES_WEBUI_GATEWAY_API_KEY` before `API_SERVER_KEY`.
- [ ] `resolve_runtime_agent_version(timeout_s=...)` performs a bounded live probe on every call.
- [ ] A recognized gateway version is returned as a non-empty string.
- [ ] Network error, timeout, non-2xx response, malformed JSON, or missing version returns `None`; the helper does not raise.
- [ ] No module-level cache is introduced.
- [ ] No secret is placed in a URL, return value, settings payload, or log message.
- [ ] No new dependency/class/service abstraction is introduced.
- [ ] Existing checkout-first `_detect_agent_version()` remains available only as fallback behaviour and is not rewritten by this ticket.

**Verification**

```bash
./scripts/test.sh tests/test_updates.py -q
python3 scripts/ruff_lint.py --diff origin/master
git diff --check
```

The new regression tests must be shown **RED before implementation** and **GREEN after implementation**.

**Dependencies**

None.

**Files likely touched**

- `api/updates.py`
- `tests/test_updates.py`

**Estimated scope**

S — 2 files.

---

## HWA-002 — Make `/api/settings` Runtime-First

**Goal**

Consume HWA-001's live runtime resolver inside `GET /api/settings`, so the System Agent badge represents the running Hermes Agent when confirmed and falls back safely to the existing `AGENT_VERSION` otherwise.

**TDD order**

1. Add observable endpoint regression tests first.
2. Demonstrate baseline/current route still returns static `AGENT_VERSION`.
3. Apply the smallest route integration.
4. Prove sequential settings calls can see a changed runtime version without restarting WebUI.

**Acceptance Criteria**

- [ ] `GET /api/settings` calls the live runtime resolver per request.
- [ ] When runtime reports `runtime-v` and static/on-disk state says `disk-v`, response returns `runtime-v`.
- [ ] When the checkout is newer than the still-running gateway, the older **runtime** version wins.
- [ ] When runtime detection returns `None`, response falls back to the existing `AGENT_VERSION`.
- [ ] Two sequential settings requests can return different runtime versions when the mocked live gateway version changes; no WebUI process restart/import reload is required.
- [ ] `settings["webui_version"] == WEBUI_VERSION` remains unchanged.
- [ ] Runtime-version failure never converts an otherwise valid settings request into a server error.
- [ ] No Settings UI JavaScript or visual layout is modified.
- [ ] The old checkout-first `current_agent_version()` design from `8c2f185d` is not reintroduced.

**Verification**

```bash
./scripts/test.sh tests/test_version_badge.py -q
./scripts/test.sh tests/test_updates.py tests/test_version_badge.py -q
python3 scripts/ruff_lint.py --diff origin/master
git diff --check
```

The endpoint regression must be shown **RED before the route fix** and **GREEN after**.

**Dependencies**

- HWA-001

**Files likely touched**

- `api/routes.py`
- `tests/test_version_badge.py`

HWA-001 files may remain modified in the same implementation branch, but this ticket itself should not require new production modules.

**Estimated scope**

S — 2 direct files.

---

## Checkpoint A — Functional Behaviour

After HWA-001 and HWA-002:

- [ ] Runtime-version resolver tests pass.
- [ ] `/api/settings` runtime-first tests pass.
- [ ] Sequential-request freshness is proven.
- [ ] Runtime beats disk/source state.
- [ ] Fallback remains safe.
- [ ] No new dependency exists.
- [ ] Diff still represents one logical behaviour fix.

No Git delivery proceeds if this checkpoint is not green.

---

## HWA-003 — Update/Timeout Non-Regression and Scope Gate

**Goal**

Prove that the Agent-version fix did not accidentally resurrect the legacy branch's timeout changes or alter unrelated updater behaviour.

This is a verification ticket. No production-code change is expected unless HWA-001/HWA-002 themselves introduced a regression that must be corrected within their approved scope.

**Acceptance Criteria**

- [ ] Existing `tests/test_api_timeout.py` remains unchanged.
- [ ] Existing update-check browser timeout remains `300000 ms`.
- [ ] `static/boot.js` has no change from the baseline for this ticket.
- [ ] `static/panels.js` has no change from the baseline for this ticket.
- [ ] Backend Git fetch timeout policy is unchanged.
- [ ] No `UPDATE_FETCH_TIMEOUT_SECONDS` change from the legacy `8c2f185d` patch is present.
- [ ] `CHANGELOG.md` is unchanged.
- [ ] `WEBUI_VERSION` resolution is unchanged.
- [ ] The full changed-file list is explainable by the runtime-version spec only.
- [ ] Diff lint/check gates pass.

**Verification**

```bash
./scripts/test.sh tests/test_api_timeout.py -q
./scripts/test.sh tests/test_updates.py tests/test_version_badge.py tests/test_api_timeout.py -q
python3 scripts/ruff_lint.py --diff origin/master
git diff --check
git diff --name-only origin/master...HEAD
git diff origin/master...HEAD -- static/boot.js static/panels.js tests/test_api_timeout.py CHANGELOG.md
```

The final command must show no ticket-induced changes in those protected files.

**Dependencies**

- HWA-001
- HWA-002

**Files likely touched**

None.

**Estimated scope**

XS — verification only.

---

## HWA-004 — Git Delivery and Local/Remote Parity Closeout

**Goal**

Deliver the approved runtime-version fix as a clean branch/PR and prove the local branch, pushed remote branch, and reviewed GitHub state all refer to the same intended commit. GitLab is checked for a corresponding managed mirror/project; absence is recorded rather than invented.

**Acceptance Criteria**

- [ ] Before delivery, refresh `origin/master` and confirm the implementation baseline is still compatible with the approved spec.
- [ ] If upstream already landed an equivalent fix, stop duplication and verify the upstream implementation against this SPEC instead.
- [ ] Delivery branch contains only SPEC/TICKETS plus the HWA-001/HWA-002 logical fix and any justified runtime-version documentation.
- [ ] Commit history contains no merge of the legacy 774-commit-behind branch.
- [ ] Required tests and lint from HWA-003 are green at the exact delivery HEAD.
- [ ] Branch is pushed only to an authorized writable remote/fork.
- [ ] GitHub PR/readback shows the same head SHA as the pushed branch.
- [ ] PR body includes:
  - Thinking Path
  - What Changed
  - Why It Matters
  - Verification
  - Risks / Follow-ups
  - Model Used
  - Contract Routing
- [ ] PR explicitly states that update-check timeout redesign is deferred.
- [ ] GitHub changed-file list contains no protected timeout files.
- [ ] Local HEAD, remote branch HEAD, and PR head SHA are identical before requesting review/merge.
- [ ] GitLab is queried for a matching managed project/mirror. If none exists or access is unavailable, record `NOT APPLICABLE / NOT VISIBLE`; do not claim parity.
- [ ] After merge, refresh local canonical checkout and prove:
  - local canonical branch = expected branch,
  - local canonical HEAD = GitHub merged/master HEAD,
  - worktree clean.
- [ ] Final closeout states whether runtime/deployment verification was performed. Repository parity must not be mislabeled as production deployment parity.

**Verification**

Repository delivery:

```bash
git status --short --branch
git fetch origin
git rev-parse HEAD
git rev-parse origin/master
git diff --check
```

After merge, canonical parity gate:

```bash
git fetch origin
git status --short --branch
git rev-parse HEAD
git rev-parse origin/master
git rev-list --left-right --count HEAD...origin/master
```

Expected final canonical repository state:

```text
branch: master
HEAD == origin/master
divergence: 0 0
worktree: clean
```

**Dependencies**

- HWA-003

**Files likely touched**

No new product files. Git/PR metadata only.

**Estimated scope**

S — delivery and verification.

---

## Checkpoint B — Ready for Review

Before entering Phase 5:

- [ ] HWA-001 complete.
- [ ] HWA-002 complete.
- [ ] HWA-003 complete.
- [ ] HWA-004 delivery HEAD is identified and reproducible.
- [ ] No hidden timeout/update scope creep.
- [ ] Local/remote SHA relationship is explicitly recorded.
- [ ] Any GitLab non-applicability/access limitation is explicitly recorded.
- [ ] Runtime/deployment status is separated from source-repository status.

---

## Deferred Ticket — HWA-FU-001 Update-Check Network Budget Hardening

**Goal**

Handle the legacy backend timeout concern separately from Agent runtime-version correctness.

**Acceptance Criteria**

To be specified in a separate SPEC before implementation. Candidate areas include configurable fetch timeout, aggregate update-check deadline, parallel WebUI/Agent checks, per-repository serialization, and apply/check lock interactions.

**Not authorized by the current SPEC.**
