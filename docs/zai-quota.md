# Z.AI (GLM Coding Plan) quota status — operator notes

The provider-quota chip and the Settings → Providers quota cards can surface
live Z.AI GLM Coding Plan usage (5-hour window first, then Monthly) plus a
peak-rate marker. Data comes from Z.AI's monitor endpoint
(`GET /api/monitor/usage/quota/limit`) — the same endpoint the Z.AI
subscription dashboard uses. It is **unofficial and undocumented**: the WebUI
parses it defensively and fails soft (the chip hides rather than showing
stale or wrong data) if Z.AI changes its shape.

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
