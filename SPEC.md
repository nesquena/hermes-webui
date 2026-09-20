# SPEC — Hermes WebUI Agent Runtime Version Convergence

Status: **APPROVED — 2026-09-20**

Target baseline: `origin/master @ 2cf8e8a5eae5a42deaa888d027c408defbe76eae` (`exp-v0.52.317`)

Supersedes (does not merge): `8c2f185d8ae4e16a1ecc01dd4221978fafd14ca5` (`fix(updates): refresh agent version and relax fetch timeout`) on the legacy `fix/webui-agent-version-update-check-20260915` branch.

## 1. Feature Scope & Goals

### Objective

Make `GET /api/settings` report the **Hermes Agent version that is actually running**, not the version implied by the Agent checkout / VERSION file / git tag / source package.

Baseline behaviour (`api/updates.py._detect_agent_version()` at `2cf8e8a5`):

1. read `_AGENT_DIR/VERSION` file
2. `_describe_git_version(agent_dir)` — `git describe` on the agent checkout
3. `_read_agent_source_version(agent_dir)` — package `__init__.py`
4. `_detect_agent_version_from_gateway_health()` — **only as a final fallback**
5. literal `'not detected'`

The baseline returns the *on-disk* Agent version whenever a checkout / VERSION file / git tag / source package is present. The running Agent process is consulted only when every on-disk probe fails. The baseline also caches that value at module-import time into the `AGENT_VERSION` module constant, so a long-lived WebUI process can never see a newer running gateway version after the Agent is upgraded and restarted.

The agent-runtime version is the authoritative answer for the Settings → System badge. On-disk state is *evidence about what the running process should be*, not *evidence about what the running process actually is*.

### User-visible contract

For the Settings → System Agent version badge:

1. **Primary truth:** the version reported by the live Agent gateway/health endpoint.
2. **Fallback:** the existing process-static `AGENT_VERSION` when no live runtime version can be confirmed.
3. The field must never prefer a newer on-disk checkout over a confirmed older running gateway.
4. The field must refresh on every `GET /api/settings`, not only at WebUI process startup.
5. `webui_version` behaviour remains unchanged.

### Success definition

A user who upgrades the Hermes Agent (and restarts its gateway) while the WebUI process remains alive sees the version of the *running* Agent on the next `GET /api/settings`, without restarting WebUI.

If no live Agent runtime can be confirmed (no gateway configured, gateway down, malformed payload, auth failure surfaces as no-version), the UI keeps the current best-effort fallback instead of returning an empty value or breaking the settings endpoint.

## 2. Locked Decisions

The following decisions are approved and are not open for implementation reinterpretation:

1. **Agent version means runtime version, not on-disk version.** For the System Agent badge, a confirmed gateway-reported version always wins over any on-disk probe.
2. The upstream frontend update-check timeout remains **300 seconds** (`static/boot.js`, `static/panels.js`, `tests/test_api_timeout.py`).
3. The `15s → 30s` backend Git fetch timeout changes from `8c2f185d` are **not part of this change** — they remain on the legacy branch and are not bundled.
4. The runtime-version probe is **per-request**, not cached at module import.
5. Gateway URL resolution and authentication are unified through a **single canonical helper** rather than two parallel implementations.
6. This work starts from `exp-v0.52.317 / 2cf8e8a5`; it does not merge the 774-commit divergence into the legacy `fix/...` branch.

## 3. Non-Goals

This ticket must not:

- change `static/boot.js` update-check timeout;
- change `static/panels.js` update-check timeout;
- change `tests/test_api_timeout.py` expectations;
- change Git fetch timeout values in `api/updates.py` (the `15s → 30s` discussion stays on the legacy branch);
- implement or partially copy PR #6830's worker-pool, concurrency, aggregate-budget, or per-repository locking design;
- change update apply/force behaviour;
- redesign the Settings UI;
- introduce new dependencies;
- change release-channel semantics;
- change WebUI asset/version cache-busting semantics;
- alter `webui_version` resolution;
- introduce a module-level cache of the runtime-version result;
- edit `CHANGELOG.md`.

Any update-check timeout work becomes a separate ticket/spec.

## 4. Technical Stack & Constraints

### Existing stack

- Server: Python
- Browser: vanilla JavaScript
- Build model: no frontend build step or bundler
- Tests: repository pytest runner via `./scripts/test.sh`
- Python lint: `python3 scripts/ruff_lint.py --diff origin/master`

### Repository constraints

- Keep one logical behaviour fix per PR.
- Prefer a single canonical gateway-URL/auth helper; do not keep two divergent copies.
- No new packages or frameworks.
- Behaviour changes require regression evidence.
- Tests must prove observable `/api/settings` output (real HTTP or in-process call into the route), not only source-string presence.
- Release-note-worthy wording belongs in the PR body; ordinary contributor work does not edit `CHANGELOG.md`.

## 5. Architecture

### 5.1 Current (baseline) data path — checkout-first, import-time cached

```text
GET /api/settings
    ↓
api/routes.py:14047
    ↓
from api.updates import AGENT_VERSION
    ↓
AGENT_VERSION = _detect_agent_version()            # runs once at module import
    ├─ _AGENT_DIR/VERSION file
    ├─ git describe on agent checkout
    ├─ read source package __init__.py
    ├─ _detect_agent_version_from_gateway_health()  # last-resort, no auth
    └─ 'not detected'
    ↓
settings["agent_version"] = AGENT_VERSION
```

Two concrete defects:

- **Order**: on-disk state wins over the running gateway. If the agent checkout was upgraded but the gateway was not restarted, the badge reports the *new* on-disk version while the running gateway is still the *old* version.
- **Staleness**: `AGENT_VERSION` is a module-level constant computed at import. A long-lived WebUI process cannot refresh after a gateway restart. `8c2f185d`'s `current_agent_version()` wrapper addresses the staleness but, in `2cf8e8a5`, the legacy branch is 774 commits behind and not the right merge target — and the wrapper itself does not fix the order.

### 5.2 Target data path — runtime-first, per-request, unified chokepoint

```text
GET /api/settings
    ↓
api/routes.py
    ↓
resolve_runtime_agent_version()
    ↓
┌─ canonical gateway helpers (api.agent_health + api.updates):
│      gateway_url = resolve_gateway_base_url()
│      paths       = shared _REMOTE_PROBE_PATHS
│      probe       = shared _http_probe()             # bounded body + no redirects
│      auth        = shared _remote_gateway_api_key()
│      version     = _version_from_gateway_health_payload(payload)
│
├─ version confirmed     → settings["agent_version"] = version
│
└─ version not confirmed → settings["agent_version"] = AGENT_VERSION
                           (preserves the existing import-time constant
                            as a safe fallback, not as the primary truth)
```

The "canonical helper" lives in `api/updates.py`. The behaviour it must match — and where the unification must happen — is `api/agent_health._remote_gateway_base_url()` and `api/agent_health._remote_gateway_api_key()`. The probe path, auth header, and timeout for the runtime-version probe use the same semantics as the gateway-health subsystem. There is exactly one place in the codebase that decides "what is the gateway URL and how do we authenticate to it".

### 5.3 Authoritative value

The authoritative value for `settings["agent_version"]` is:

- **live gateway health version**, when confirmed;
- otherwise the existing `AGENT_VERSION` fallback.

An on-disk Agent checkout, VERSION file, git tag, or source package is **never** allowed to override a confirmed live gateway version for this UI field. The on-disk probes in `_detect_agent_version()` remain in the codebase for the *fallback* path only; the fallback returns whatever `_detect_agent_version()` returns today, in the same order, so we do not silently change behaviour when no gateway is reachable.

### 5.4 Gateway resolution contract

The runtime-version probe and the existing gateway-health subsystem must share one set of URL/auth semantics.

Required URL precedence, aligned with `api/agent_health._remote_gateway_base_url()`:

1. `GATEWAY_HEALTH_URL`
2. `HERMES_GATEWAY_HEALTH_URL`
3. `HERMES_API_URL`
4. `HERMES_WEBUI_GATEWAY_BASE_URL`

Required trailing-suffix normalization (must mirror `_remote_gateway_base_url`):

- strip `/health/detailed`
- strip `/v1/health`
- strip `/health`
- strip `/status`

Trailing slashes are stripped.

Required probe paths must mirror the shared gateway-health subsystem exactly:

1. `/health/detailed`
2. `/health`
3. `/v1/health`

The runtime-version resolver must consume the shared probe path list rather than maintaining a second hardcoded list.

Required authentication, aligned with `api/agent_health._remote_gateway_api_key()`:

- Read `HERMES_WEBUI_GATEWAY_API_KEY` first, then `API_SERVER_KEY`.
- When present, attach `Authorization: Bearer <key>` to the probe request.
- A configured/reachable gateway must not be falsely treated as absent solely because the version path forgot the API key.

Probe safety:

- Per-request bounded; the concrete budget is the same order of magnitude as the existing 0.75 s probe in `_detect_agent_version_from_gateway_health()` but **explicitly tested** for the runtime-version path (see §6 Case C and §11).
- The shared gateway health HTTP client must reject redirects so a Bearer credential cannot be forwarded to another origin.
- Successful response bodies are capped using the existing gateway-health body limit before JSON decode; oversized payloads are treated as unconfirmed version data.
- No global module-level cache.

This specification does **not** authorise a larger gateway-health refactor. Only the minimum sharing/alignment required to make version resolution correct is in scope.

## 6. API Contract

### Endpoint

`GET /api/settings`

### Existing response fields (unchanged shape)

```json
{
  "webui_version": "...",
  "agent_version": "..."
}
```

### Required behaviour

#### Case A — live gateway reports a version (runtime beats on-disk)

Given:

- on-disk checkout resolves to a *different* value (or is absent)
- live gateway reports `"new"`

Then:

```json
{ "agent_version": "new" }
```

#### Case B — checkout and runtime disagree, runtime is older (the badge must NOT show the newer checkout)

Given:

- on-disk checkout resolves to `"new"`
- currently running gateway reports `"old"`

Then:

```json
{ "agent_version": "old" }
```

The running process wins. This is the case the baseline gets wrong today.

#### Case C — gateway probe fails or returns no version

Given:

- gateway probe times out, refuses connection, returns non-2xx, returns malformed JSON, returns JSON without a recognised version field, or the probe raises
- on-disk state resolves to `"fallback"`

Then:

```json
{ "agent_version": "fallback" }
```

The settings endpoint must still succeed.

#### Case D — no on-disk state and no live runtime

Existing `'not detected'` behaviour is preserved as the final literal. The change must not convert a working `/api/settings` response into a server error merely because runtime version detection is unavailable.

#### Case E — `webui_version` is untouched

The runtime-version refactor must not touch `WEBUI_VERSION` resolution, the `settings["webui_version"]` field, or any asset cache-busting code path.

## 7. Data Model

No persistent data model changes.

No database, state file, cache schema, config schema, or migration is introduced.

The only affected value is transient response data:

```text
settings.agent_version: string
```

Ownership:

- live runtime value: Hermes Agent gateway health response;
- fallback value: WebUI process-local `api.updates._detect_agent_version()` (preserved as-is, in the existing order, to avoid silent regressions when no gateway is reachable);
- response composition: `api/routes.py`.

## 8. Error Handling & Edge Cases

### Gateway unreachable

- Do not fail `GET /api/settings`.
- Fall back to `_detect_agent_version()`.

### Gateway timeout

- Keep the probe bounded.
- Do not make this task a general timeout redesign.
- Fall back to `_detect_agent_version()`.

### Malformed JSON

- Treat as no confirmed runtime version.
- Fall back.

### JSON without a recognised version field

- Treat as no confirmed runtime version.
- Fall back.

### Authenticated gateway

- Runtime-version resolution honours the existing gateway-health authentication contract (`HERMES_WEBUI_GATEWAY_API_KEY` / `API_SERVER_KEY`).
- A configured/reachable gateway must not be falsely treated as absent solely because this version path forgot the existing API key.

### On-disk upgraded before gateway restart

- Confirmed runtime version wins over on-disk version. This is the central invariant.

### Gateway restarted after WebUI startup

- A later `GET /api/settings` must surface the new live runtime version without restarting WebUI. No module-level cache.

### No gateway configured / local-only source checkout

- Preserve current best-effort fallback behaviour (`_detect_agent_version()` returns whatever it returns today).
- Do not force a new dependency on a running gateway for normal WebUI operation.

### Health probe exception

- Fail soft for version display only.
- Do not leak credentials, URLs containing secrets, or raw exception details into the settings payload.

## 9. Expected File Scope

Expected production files:

- `api/updates.py` — add the per-request runtime resolver and reuse the shared gateway URL/auth/probe semantics from `api.agent_health`.
- `api/agent_health.py` — harden the shared HTTP probe so authenticated health requests reject redirects and retain the existing bounded-body contract.
- `api/routes.py` — change the `settings["agent_version"]` injection at `~line 14047` to call `resolve_runtime_agent_version()` first and fall back to `AGENT_VERSION`.

Expected tests:

- `tests/test_version_badge.py` — extend with runtime-first cases.
- `tests/test_updates.py` — extend if `api/updates.py` gains a new helper.

Possible documentation:

- `docs/docker.md` only if the final behaviour needs a small clarification that the Agent badge reflects the running gateway when reachable.

Files explicitly expected to remain unchanged:

- `static/boot.js`
- `static/panels.js`
- `tests/test_api_timeout.py`
- update apply/force timeout code
- `CHANGELOG.md`
- `webui_version` resolution path

## 10. Code Style

Follow the existing Python style and keep the call path flat.

Required API (the only signature this SPEC authorises):

```python
# in api/updates.py

def resolve_gateway_base_url() -> str | None:
    """Return the configured Hermes Agent gateway base URL, or None.

    Semantics aligned with api.agent_health._remote_gateway_base_url:
    Priority: GATEWAY_HEALTH_URL > HERMES_GATEWAY_HEALTH_URL
    > HERMES_API_URL > HERMES_WEBUI_GATEWAY_BASE_URL.
    Trailing health suffixes (/health/detailed, /health, /v1/health, /status)
    are stripped. Returns None when no env var is set so the caller can
    skip the probe entirely (the existing _gateway_health_base_url default
    of http://hermes-agent:8642 is deliberately dropped here — the runtime
    version is display-only and must not require a gateway in single-binary
    local-only setups).
    """

def resolve_runtime_agent_version(*, timeout_s: float = 0.75) -> str | None:
    """Return the live Agent runtime version, or None when no confirmed value.

    Single per-request resolution. Reuses api.agent_health's canonical
    gateway API key, shared probe paths, redirect-safe HTTP probe, and
    bounded-body limit. Never raises; never returns ''; returns None on any
    network failure, redirect, non-2xx, oversized body, malformed JSON, or
    JSON without a recognised version field. Never logs credentials or secrets.
    """
```

Caller change in `api/routes.py` (one-block edit, inside the existing `try`):

```python
try:
    from api.updates import (
        AGENT_VERSION,
        WEBUI_VERSION,
        resolve_runtime_agent_version,
    )
    settings["webui_version"] = WEBUI_VERSION
    runtime_version = resolve_runtime_agent_version()
    settings["agent_version"] = runtime_version or AGENT_VERSION
except Exception:
    pass
```

Do not add a class, service object, registry, dependency, or generalised version subsystem for this fix.

## 11. Testing Strategy

### TDD requirement

Implementation begins only after a regression test is written and shown to fail on the `2cf8e8a5` baseline for the expected reason (i.e. the baseline still returns the on-disk value when the running gateway disagrees).

### Required regression cases

1. **Live version overrides on-disk version (Case A)**
   - Patch `_detect_agent_version()` to return `"on-disk-v"`.
   - Patch the probe target so it returns `"runtime-v"`.
   - `GET /api/settings` → `settings["agent_version"] == "runtime-v"`.

2. **Runtime wins over newer on-disk checkout (Case B)**
   - Patch `_detect_agent_version()` to return `"new"`.
   - Patch the probe target to return `"old"`.
   - `GET /api/settings` → `settings["agent_version"] == "old"`.

3. **Gateway failure falls back safely (Case C)**
   - Patch the probe target to raise / time out / return malformed JSON / return JSON without a version field, in separate tests.
   - Patch `_detect_agent_version()` to return `"fallback"`.
   - `GET /api/settings` succeeds and returns `"fallback"`.

4. **Documented Docker gateway env aliases**
   - With `HERMES_API_URL` set, the probe must hit that URL.
   - With `HERMES_WEBUI_GATEWAY_BASE_URL` set (and no higher-priority env), the probe must hit that URL.
   - With `GATEWAY_HEALTH_URL=http://host:port/health` set, the trailing `/health` must be stripped and the probe must hit `http://host:port/health`.

5. **Authenticated gateway semantics**
   - With `HERMES_WEBUI_GATEWAY_API_KEY=secret` set, the authenticated health probe must include `Authorization: Bearer secret`.
   - With `API_SERVER_KEY=secret` set (and no `HERMES_WEBUI_GATEWAY_API_KEY`), the authenticated health probe must include `Authorization: Bearer secret`.
   - Without any key set, no `Authorization` header is sent.
   - Redirect responses are not followed by the authenticated probe.
   - In all cases, the secret value must not appear in `settings["agent_version"]` or any other settings field.

6. **Probe path and body safety**
   - If only `/v1/health` returns a usable version, the resolver returns that version.
   - Oversized health bodies are not decoded and return no confirmed runtime version.
   - Shared gateway-health probe behaviour remains bounded and redirect-safe.

7. **Existing WebUI version behaviour unchanged (Case E)**
   - `settings["webui_version"] == WEBUI_VERSION` (the module constant).
   - `WEBUI_VERSION` resolution code path is byte-identical.

### Neighbouring regression sweep

At minimum:

```bash
./scripts/test.sh tests/test_version_badge.py tests/test_updates.py -q
```

If `api/agent_health.py` is touched (it should not be — the unification is in `api/updates.py`), also run the focused gateway-health tests identified from the changed module.

### Timeout invariant

Run the existing timeout regression unchanged:

```bash
./scripts/test.sh tests/test_api_timeout.py -q
```

It must continue to prove the upstream **300000 ms** update-check timeout.

### Lint

```bash
python3 scripts/ruff_lint.py --diff origin/master
```

### Diff guard

Before review:

```bash
git diff --check
git diff --name-only origin/master...HEAD
```

The diff must contain:

- no `static/boot.js` timeout change;
- no `static/panels.js` timeout change;
- no `tests/test_api_timeout.py` expectation change;
- no `UPDATE_FETCH_TIMEOUT_SECONDS` change in `api/updates.py`;
- no `CHANGELOG.md` change.

## 12. Acceptance Criteria

The ticket is done only when all are true:

- [ ] Work started from `2cf8e8a5` on a new branch (no merge of the 774-commit legacy divergence).
- [ ] A regression test fails on the baseline and passes after the fix.
- [ ] `GET /api/settings` prefers a confirmed running gateway version.
- [ ] A confirmed running gateway version cannot be overridden by a newer on-disk checkout.
- [ ] Gateway failure falls back to `_detect_agent_version()`'s result without breaking the settings endpoint.
- [ ] `GATEWAY_HEALTH_URL` / `HERMES_GATEWAY_HEALTH_URL` / `HERMES_API_URL` / `HERMES_WEBUI_GATEWAY_BASE_URL` all reach the same probe semantics.
- [ ] Trailing `/health`, `/health/detailed`, `/v1/health`, `/status` suffixes are normalised the same way `api/agent_health._remote_gateway_base_url` does.
- [ ] `HERMES_WEBUI_GATEWAY_API_KEY` / `API_SERVER_KEY` are honoured by the probe and the secret is never echoed into the response.
- [ ] `webui_version` behaviour is byte-identical.
- [ ] Frontend update-check timeout remains 300000 ms.
- [ ] Backend Git fetch timeout behaviour is unchanged by this ticket.
- [ ] No new dependency or unnecessary abstraction is introduced.
- [ ] No module-level cache of the runtime-version result is introduced.
- [ ] Affected and neighbouring tests pass.
- [ ] Ruff diff gate passes.
- [ ] `git diff --check` passes.
- [ ] Final diff contains only files required by this logical fix.

## 13. Boundaries

### Always

- Start from the current upstream baseline.
- Test observable API behaviour.
- Prefer live runtime truth.
- Fail soft to the existing static fallback for display-only version detection.
- Reuse a single canonical gateway configuration/auth helper.
- Keep the diff minimal.

### Ask first

- Any persistent state/schema change.
- Any new dependency.
- Any change to update/apply concurrency.
- Any change to frontend update timeouts.
- Any change to backend Git fetch timeout policy.
- Any broader gateway-health refactor beyond what is required for semantic alignment.

### Never

- Merge the legacy branch's 774-commit divergence.
- Reintroduce 90-second frontend update checks.
- Reintroduce on-disk-checkout-first ordering for the System Agent badge.
- Allow an on-disk probe to override a confirmed running gateway version.
- Echo gateway credentials or secret-bearing URLs into `/api/settings`.
- Edit `CHANGELOG.md` in this ordinary contributor change.
- Suppress or delete failing tests to obtain green status.
- Add a module-level cache for the runtime-version result.

## 14. Contract Routing

Task type: focused runtime-version correctness fix.

Touched areas:

- `GET /api/settings`
- Agent version detection
- gateway URL/auth resolution (canonical helper, not duplicated)

Relevant public docs:

- `AGENTS.md`
- `CONTRIBUTING.md`
- `docs/GUIDELINES.md`
- `docs/CONTRACTS.md`
- `docs/docker.md`
- existing gateway health implementation in `api/agent_health.py`

State/invariant:

> The System Agent version badge represents the confirmed running Agent when one is reachable; on-disk state is fallback evidence, not stronger runtime truth.

Evidence required before done:

- baseline RED → fixed GREEN regression;
- fallback test;
- documented gateway-env coverage;
- auth-semantic coverage (header attached, secret not echoed);
- trailing-suffix normalisation coverage;
- unchanged 300 s timeout regression;
- unchanged `webui_version` regression;
- neighbouring tests and lint.

## 15. Risks & Mitigations

### Risk: settings requests perform live I/O on every call

Mitigation:

- keep the runtime probe bounded (≤ 1 s default);
- reuse existing health semantics where possible;
- preserve fallback behaviour;
- do not expand this ticket into update/network retry architecture.

### Risk: gateway URL/auth logic drifts again

Mitigation:

- route every consumer through one canonical helper in `api/updates.py` whose behaviour mirrors `api/agent_health._remote_gateway_base_url` / `_remote_gateway_api_key`;
- add regression coverage for documented env aliases and trailing-suffix stripping.

### Risk: runtime and on-disk intentionally differ during upgrade

Mitigation:

- this is exactly why runtime truth has precedence for the System badge.

### Risk: upstream lands #6156 / #6289 while implementation is in progress

Mitigation:

- refresh from `origin/master` before implementation;
- if upstream has already landed an equivalent or stronger fix, validate it against this spec and avoid duplicating it.

### Risk: dropping the `http://hermes-agent:8642` default of `_gateway_health_base_url`

Mitigation:

- the runtime-version probe is **display-only**; in single-binary local-only setups, the user does not have a gateway at `http://hermes-agent:8642`, and a probe there would block the settings endpoint or surface a confusing error. Returning `None` (no gateway configured) and falling back to the existing on-disk chain is strictly safer than the current default, which silently masks misconfigured deployments by trying a hardcoded host.

## 16. Deferred Follow-Up

Separate ticket: **Update-check network budget hardening**.

That ticket may evaluate:

- configurable Git fetch timeout;
- aggregate update-check deadline;
- WebUI/Agent concurrent checks;
- per-repository serialisation;
- apply/check lock interaction;
- production latency measurements such as those discussed around upstream PR #6830.

None of that work is bundled into this runtime-version fix.

## 17. Open Questions

None. The three previously ambiguous design decisions have been explicitly approved:

- runtime version is authoritative over on-disk state;
- upstream 300 s frontend timeout remains;
- backend timeout redesign is deferred;
- the per-request probe replaces the import-time module constant;
- URL/auth resolution is unified through one canonical helper.

## 18. Review Notes (for the human approver)

The first revision of this SPEC described the baseline as "import-time `AGENT_VERSION` constant, stale in long-lived processes". That description was directionally correct about staleness but missed that the baseline's `_detect_agent_version()` is already *on-disk first, gateway last*. The two defects the SPEC targets — order and staleness — are now both stated explicitly. The "reuse existing gateway configuration/auth semantics" requirement has been tightened to "single canonical helper whose behaviour mirrors `api/agent_health._remote_gateway_base_url` / `_remote_gateway_api_key`", because the existing `api/updates._gateway_health_base_url()` does not honour auth and uses a different env ordering + suffix set.
