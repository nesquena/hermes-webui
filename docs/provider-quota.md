# Provider quota: per-credential windows

`GET /api/provider/quota` answers two different questions at once, and they are
allowed to disagree:

- `account_limits` — the **aggregate** view the composer chip renders. It stays the
  single window set the usage API returned for the active key (or the credential
  pool's own state when the key call fails).
- `credentials` — the **per-credential** view the Settings › Providers card renders
  for a pooled provider, so each paid plan shows its own usage.

## When per-credential rows are fetched

Only **opencode-go** is widened. Its `/usage` endpoint reports windows per *plan*,
and a credential pool can hold several paid plans, so one call for the provider
cannot describe the pool.

- **One credential:** no probe, no `credentials` key. The payload is identical to the
  pre-per-credential shape and one request answers everything.
- **Two or more credentials:** one `/usage` probe per non-`dead` credential, each
  with its own `Authorization: Bearer …` and the mandatory `x-opencode-session`
  header. `dead` credentials are never probed; they keep their existing failure row.
- **Every other provider** keeps its previous payload exactly: `credentials` is not
  added, even when the pool has several entries.

Probes are never load-bearing: they run alongside the aggregate call and cannot fail
the endpoint.

## The 60 s cache, and refresh

Each credential's probe result — **including a failure** — is cached for 60 s, keyed
by `(active profile home, provider, credential identity)`. Identity is the entry's
`id`; an entry without one is keyed by its position plus label, so two id-less
entries never share a result and an id never collides with a label. The profile home
is in the key because a cloned profile copies `auth.json` and therefore repeats
credential ids.

`?refresh=1` (the Refresh button) bypasses that cache read, cached failures included,
so a credential fixed a moment ago is re-probed immediately. A result is written to the
cache only while its own probe still owns the key: every probe takes a generation
before its request starts, and a newer request replaces it. A probe that finishes
later — a success or a failure — is therefore refused the cache instead of overwriting
the refreshed value, so the next ordinary poll reads the new numbers whichever probe
finishes last. Skipping the cache read is not enough on its own: a probe whose request
already timed out keeps running, and a completion timestamp would only make it look
newest.

## Deadlines

One request-wide deadline (`_PROVIDER_QUOTA_TIMEOUT_SECONDS`, 3 s) bounds both the
aggregate call and the whole probe batch: the probes run concurrently and the
endpoint answers when the slower of the two is done, not after `N x 3 s`. A probe that
is still running at the deadline is recorded as a failure.

## How a failed probe appears

The row stays, with `plan: null` and `windows: []`, plus its normal pool `status`
and `unavailable_reason` — a missing, unauthorised, timed-out or unparseable probe
degrades one row, never the response. The aggregate `account_limits.windows` is
unaffected, so the chip keeps showing the last aggregate while a single plan looks
empty.

## `credentials` vs `account_limits.pool`

Both come from the same local pool snapshot and carry one row per configured
credential, but they serve different readers:

| Field | Reader | Shape |
|-------|--------|-------|
| `account_limits.pool` | pool bookkeeping (`total/available/exhausted/dead`, `queried_credentials`, best-remaining summary) | pool stats **plus** `credentials` rows |
| `credentials` (top level) | Settings › Providers card | flat array of `{label, status, plan, windows, details, unavailable_reason, retry_after}` |

`account_limits.pool`'s `queried_credentials`, `plans`, `best_remaining_by_window` and
`next_reset_at` are derived from its own rows, so they never read zero or empty beside
populated per-credential windows. A pool whose rows carry no windows — every provider
that is not probed — keeps the previous payload exactly (`0`, `[]`, `[]`, `null`).

`credentials` is derived from `account_limits.pool.credentials` — same non-secret
fields — and only exists for opencode-go with more than one credential. A client that
does not know the field keeps rendering the pool block; the panel prefers
`credentials` and falls back to `account_limits.pool` when it is absent. Neither field
ever carries key material: labels come from the configured label (or
`Credential N`), never from a token.

## Tests

`tests/test_provider_quota_per_credential_windows.py` runs the real
`get_provider_quota()` path with the upstream call and the credential pool stubbed: it
pins the profile-scoped cache key, the id-less identities, `refresh`, a superseded probe
that finishes after Refresh in either completion order (stale success and stale failure,
plus the reverse-order control), the concurrent deadline (measured wall time), a
timed-out probe's failure row, the pool summary derived from its rows, and the
restricted `credentials` field.
