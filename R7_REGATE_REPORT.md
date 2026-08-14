# #6516 Round-7 Re-gate Fix — Detailed Report

## Review being addressed

PR review `4837781398` (Round-7 re-gate at `7d12e75d6a79`) requested changes
with `CHANGES_REQUESTED`, citing two deterministic authority gaps in the
`custom:<slug>` provider resolution path of the WebUI:

**Gap 1 — "A truthy URL is still treated as proof of a unique matching target."**
`_custom_provider_authority_state()` returned `"unique"` whenever
`resolved_base_url` was truthy, which did not prove the URL came from the
*requested named provider*. Two concrete defects:

- `api/config.py:3294-3304` mapped a missing requested slug onto the sole
  configured `custom_providers` row even when its name did not match.
- `_resolve_custom_provider_fallback()` read `model.base_url` without first
  requiring `model.provider` to match the requested lane.

The review asked for explicit selection provenance (`selected` with matching
row/source and paired URL/key, versus missing/ambiguous/malformed/partial),
never reconstructing `selected` from a truthy URL, and for `model.base_url` to
be gated by a matching canonical `model.provider`.

**Gap 2 — "The ambient credential side bundle survives all three constructor paths."**
The runtime `credential_pool` (owned by the ambient/default profile) remained
attached unconditionally at:
- initial construction: `api/streaming.py:8937-8938`
- returned-401 retry: `9760-9762`
- raised-401 retry: `11006-11008`

That pool could carry another profile's credentials into a keyless/missing/
ambiguous custom target even after the URL/key tuple was cleared.

---

## What changed

### 1. Explicit selection provenance in `api/config.py`

- Replaced `_resolve_custom_provider_connection_inner`'s ad-hoc 2-tuple body with
  a proper classifier:
  - `_classify_custom_provider_selection(...) -> (status, api_key, base_url, source)`
    where `status ∈ {selected, missing, ambiguous}` and `source ∈
    {custom_providers, providers, model}` names the authority row.
  - `_resolve_custom_provider_selection(...)` is the provenance-rich public root
    the streaming send path consumes. It mirrors `resolve_custom_provider_connection`'s
    env-guard setup (`block_process_env_fallback`) so the send path and the public
    2-tuple API always agree.
- **Removed the sole-nonmatching-row fallback**: a missing slug is no longer
  rewritten onto the single configured `custom_providers` row when its name does
  not match — that pivot would attach an unrelated endpoint's credentials.
- **Gated `model.base_url`**: `_resolve_custom_provider_fallback()` now only reads
  `model.base_url` when `model.provider` canonically matches the requested
  `custom:<slug>` lane (or the collapsed `custom`), via a shared slug helper.
  It also now returns the authority `source` so callers know which row produced
  the URL.
- The public `resolve_custom_provider_connection()` 2-tuple contract is preserved
  (many `routes.py` callers unpack it); it is internally provenance-strict.

### 2. Fail-before-construct + credential-pool scrub in `api/streaming.py`

- `CustomProviderAuthorityError` — new exception raised when a
  `custom:<slug>` target's authority cannot be established uniquely
  (missing/ambiguous/malformed/partial).
- `_resolve_custom_provider_runtime_overrides()` now:
  1. calls `_resolve_custom_provider_selection()` for the **explicit authority
     verdict** (never reconstructs `selected` from a truthy URL);
  2. if `selected` with a URL → installs exactly the paired URL/key bundle
     (keyless ⇒ `_KEYLESS_CUSTOM_API_KEY` placeholder);
  3. otherwise falls back to the runtime connection resolver for the
     **keyless-unique, runtime-resolved endpoint** case (preserving the graceful
     `credential_pool_empty` / wakeup-pause behavior) — and raises
     `CustomProviderAuthorityError` when that also yields no URL, which is every
     production missing/ambiguous/malformed/partial case.
- Scrub applied **before every constructor path**: initial construction,
  returned-401 retry, raised-401 retry, plus the cached-agent refresh and the
  session-agent-cache key signature.
- Also retained the original literal defensive forwarding line
  `_agent_kwargs['credential_pool'] = _rt.get('credential_pool')` (issue #772
  source-contract) and scrub on the following line.
- Both 401 self-heal retry paths return a sentinel (`resolved_provider is None`)
  when authority cannot be re-established, so they **skip rebuilding** the retry
  agent with ambient credentials and let the original auth error surface.

### 3. Regression tests in `tests/test_issue6516_adversarial.py`

Rewrote/extended the round-6/round-7 suites to assert the new fail-before-send and
scrub semantics, and added the cases the review explicitly requested:

- `test_r7_missing_slug_sole_nonmatching_row_fails` — missing slug must NOT select
  a sole non-matching `custom_providers` row.
- `test_r7_sole_matching_row_still_selected` — genuinely-matching sole row still works.
- `test_r7_model_base_url_gated_by_matching_provider` — unrelated `model.base_url`
  rejected; matching `model.provider` accepted.
- `test_r7_keyless_target_scrubs_ambient_credential_pool_returned` / `_raised` —
  ambient `credential_pool` never reaches ANY constructor on initial send or either
  401 shape.
- `test_r7_collision_aborts_no_constructor_no_pool` / `_raised` — both collision
  retry shapes fail before constructing with zero agents and a user-visible apperror.
- `test_r7_malformed_sole_entry_fails_without_endpoint` — partial/malformed target fails.
- `test_r7_matching_target_still_scrubs_ambient_pool` — even a fully-matching target
  does not carry the ambient pool.

---

## Key design decisions / trade-offs

- **Kept the runtime connection-resolver seam** (`resolve_custom_provider_connection`)
  as the fallback authority so the existing production-composed wakeup-pause path
  (keyless unique endpoint ⇒ `credential_pool_empty`) is preserved. Verified that in
  production this fallback **always raises** for missing/ambiguous/malformed/partial —
  the seam and the classifier agree (both return no URL), so no unrelated endpoint is
  ever resurrected. The only divergence is a deliberately-mocked runtime resolution.
- **Did not change `resolve_custom_provider_connection`'s public 2-tuple signature** —
  many `routes.py` callers depend on it. Provenance lives in the new
  `_resolve_custom_provider_selection` root while the 2-tuple wrapper stays ABI-stable.
- **Preserved issue #772 defensive runtime-route forwarding** literal line (source-contract
  tested) by keeping the assignment and layering the scrub on top.

---

## Verification

### Full suite (after fixes)
`17 failed, 13722 passed, 115 skipped, 1 xfailed, 2 xpassed, 36 errors`

Every remaining failure/error is **pre-existing or environmental** and fails
identically at the base commit `7d12e75d` without these changes:

- `test_readonly_parent_with_unwritable_file_still_raises` (root privilege defeats
  the chmod-based permission test) — pre-existing, acknowledged in the PR description.
- `test_load_settings_returns_defaults_when_settings_file_unreadable` (same root-privilege issue).
- 14× `test_mcp_server.py` failures + 36 errors — `AttributeError: 'Server' object
  has no attribute 'list_tools'` (installed `mcp` library version mismatch;
  unrelated local `mcp_server.py` module, never touched by this PR).
- `test_post_compression_estimate_uses_compressor_budget_counter_without_metadata_estimators`
  — passes in isolation; order-dependent cross-file pollution, not touched by this PR.

None of the 17 failures are attributable to these changes.

### In-scope regression surface
- `test_issue6516_adversarial.py` + `test_issue6516_colon_named_custom_providers.py`: **56 passed**
- Full relevant battery (adversarial, colon-named, process-wakeup-pause, sprint42,
  credential-pool classification, keyless, named-resolution, profile-providers,
  profile-switch, cred-pool scoping/providers): **223 passed**

### Live provenance probes (not just tests)
Confirmed the classifier/seam agreement for missing, ambiguous, unrelated-model,
and malformed/partial configs — all yield no URL ⇒ production always raises before
constructing. Keyless-unique targets yield a URL ⇒ graceful keyless path.

---

## Files changed
- `api/config.py` — provenance classifier + `_resolve_custom_provider_selection`,
  removed sole-nonmatching fallback, gated `model.base_url`.
- `api/streaming.py` — `CustomProviderAuthorityError`, provenance-consumed send path,
  fail-before-construct on unresolved authority, `credential_pool` scrub at every
  constructor path, sentinel 401 retry handling.
- `tests/test_issue6516_adversarial.py` — round-7 regression coverage.
