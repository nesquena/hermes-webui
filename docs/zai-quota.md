# Z.AI (GLM Coding Plan) quota status — operator notes

The provider-quota chip and the Settings → Providers quota cards can surface
live Z.AI GLM Coding Plan usage (5-hour window first, then Monthly) plus a
peak-rate marker. Data comes from Z.AI's monitor endpoint
(`GET /api/monitor/usage/quota/limit`) — the same endpoint the Z.AI
subscription dashboard uses. It is **unofficial and undocumented**: the WebUI
parses it defensively and fails soft (the chip hides rather than showing
stale or wrong data) if Z.AI changes its shape.

## Credential routing (trust boundary)

The monitor credential is only ever sent to the origin the operator
configured for this provider. Requests never follow redirects and the
response body is size-capped; a redirected or oversized response fails
closed (no quota data) rather than risking the credential or the process.

- **No `base_url` configured** → the request goes to `https://api.z.ai`,
  the canonical monitor host.
- **`providers.zai.base_url` (or the active `model.base_url`) configured** →
  the monitor path is derived from that origin. Example:
  `https://open.bigmodel.cn/api/coding/paas/v4` becomes
  `https://open.bigmodel.cn/api/monitor/usage/quota/limit`. A regional
  endpoint key therefore never leaks to the global host (or vice versa).
- **Non-https origin** (except loopback `http`, kept for local proxies) →
  no request at all. The feature reports unavailable, and a local
  credential-pool snapshot (if one exists) answers in the monitor's place.
- The effective monitor origin is part of the quota cache key: changing the
  origin immediately re-fetches instead of serving the other origin's data.

## Failure behavior

Failures are shared, bounded, and never resurrect stale success:

- One transport call in flight per cache key; concurrent callers join it.
  A forced refresh also joins an in-flight request for its key — it skips
  only the completed cache, so simultaneous forced refreshes share one
  credentialed request instead of fanning out. If an owner stalls past the
  join timeout, exactly one waiter is elected replacement owner (bounded
  steal).
- A failed fetch publishes a short-lived (15 s) sanitized failure marker
  that absorbs immediate retries — an outage does not become one request
  per caller.
- A failed forced refresh atomically evicts the previously cached success,
  so the next ordinary request reports unavailable instead of the old
  "available".
- On auth/transport/parser failure (or no key), the pre-existing Z.AI local
  credential-pool snapshot, when present, keeps answering with the pool
  breakdown; on remote success the pool envelope is merged into the
  account-limits card.

## Peak-rate marker

Z.AI bills premium-model requests at a higher multiplier during **weekdays
14:00–18:00 Asia/Shanghai** (UTC+8). The chip shows a ⚡ marker while that
window is active, and the tooltip/card states the current rate and when it
switches (rendered in the server's local timezone).

Two facts about the multiplier worth knowing:

- **Z.AI exposes no billing API.** The multiplier values shown are
  plan-document annotations, not API data. They have changed between plan
  generations (V1: 3× peak / 1× off-peak; current docs: 3× peak / 2×
  off-peak for advanced models).
- **They are configurable** so each install can match its own plan terms.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ZAI_PEAK_MULTIPLIER` | `3` | Peak-hour rate multiplier annotation. Any positive number. |
| `ZAI_OFFPEAK_MULTIPLIER` | `2` | Off-peak rate multiplier annotation. Any positive number. |
| `ZAI_PEAK_TZ` | `Asia/Shanghai` | Billing-window timezone (IANA name). **Dangerous override**: setting e.g. `UTC` deliberately moves the peak window; invalid values warn once and fall back to Asia/Shanghai. |
| `ZAI_API_KEY` / `Z_AI_API_KEY` | — | Read-only aliases for `GLM_API_KEY` (all three names are accepted by Hermes Agent). Removing the provider key in Settings clears every Z.AI key name. |

Invalid multiplier values (non-numeric, zero, negative, NaN) are ignored —
the defaults apply independently per variable. Quota availability is never
affected by these settings.
