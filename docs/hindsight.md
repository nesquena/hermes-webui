# Hindsight integration

When the active Hermes profile has `memory.provider: hindsight`, the Memory panel
shows a Hindsight section with Recall, Reflect, recent memories, and Retain.
The browser talks only to same-origin WebUI endpoints; the WebUI reads the
active profile's `hindsight/config.json` and `.env`, and sends the
`HINDSIGHT_API_KEY` upstream server-side.

## Configuration

The agent's normal profile configuration is authoritative:

```yaml
memory:
  provider: hindsight
```

```json
{
  "mode": "local_external",
  "api_url": "https://hindsight.example",
  "bank_id": "shared-agent-memory",
  "recall_budget": "mid"
}
```

Set `HINDSIGHT_API_KEY` in the active profile's `.env` or through Hermes' secret
scope. The WebUI does not return the key to the browser, include it in client
storage, or echo it in error responses.

`api_url` must be an HTTPS origin and must resolve only to public addresses.
HTTP is available only for explicitly controlled development environments with
`HERMES_WEBUI_HINDSIGHT_ALLOW_INSECURE=1`; loopback, private, link-local, and
metadata addresses are always rejected. Bank IDs are limited to letters,
numbers, `_`, and `-`.

## WebUI endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/hindsight/status` | Profile/provider status and a cached reachability probe |
| GET | `/api/hindsight/memories?limit=&offset=&q=` | Recent/listed memory units |
| POST | `/api/hindsight/recall` | Semantic memory search |
| POST | `/api/hindsight/reflect` | Hindsight synthesis |
| POST | `/api/hindsight/retain` | Store a memory |

POST requests use the WebUI's existing authentication and CSRF checks. Requests
are rate-limited per client address and operation. Query/context/content/tags
are bounded before proxying. Upstream errors are normalized and redacted; the
upstream response is never passed through as `raw` data.

## Limits and behavior

- Recall/Reflect: maximum query length 4,000 characters.
- Reflect context: maximum 4,000 characters.
- Retain content: maximum 20,000 characters; context maximum 4,000.
- Recall/Reflect: 10 requests/minute per client address and operation.
- Status/List: 30 requests/minute per client address and operation.
- Retain: 20 requests/minute per client address.
- Upstream calls use a bounded executor and a 90-second request timeout.
- The Memory panel uses request sequencing and profile-scoped cache keys so a
  late response or profile switch cannot overwrite the active profile's view.

Hindsight availability is optional. When it is not configured, the Memory tab
stays usable and shows an actionable disabled state.
