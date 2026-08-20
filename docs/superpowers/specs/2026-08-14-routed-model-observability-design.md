# Routed Model Observability Design

## Decision

Implement routed-model observability entirely in Hermes WebUI by consuming the
Hermes Agent `post_api_request` lifecycle payload. For an OpenAI-compatible
streaming response, Hermes Agent fills `response_model` from the normalized
response object after the SSE stream closes. WebUI will capture that safe scalar
for the active WebUI turn, normalize it into the existing gateway-routing
metadata shape, persist it on the final assistant message and session, and
render it in the existing assistant footer.

This is the selected minimal design because it uses an existing response-boundary
contract, keeps TokenTable and Hermes Agent unchanged, and avoids inferring a
model from token text or from the requested model.

## Verified Context

- WebUI requests `model=auto` through the TokenTable OpenAI-compatible endpoint.
- TokenTable's production request record identifies the routed main-tier model.
- A production `stream=true` probe returned `model=gpt-5.6-sol` on all ten
  observed SSE chunks.
- Hermes Agent's normalized streaming response preserves that SSE model as
  `response.model`, then exposes it as `response_model` in `post_api_request`.
- Hermes WebUI's current `on_token(text)` callback receives text only; it cannot
  truthfully identify the routed model.
- WebUI already persists safe routing metadata as session `gateway_routing` and
  `gateway_routing_history`, per-message `_gatewayRouting`, and the terminal
  `done` session payload. The UI already has a routing-aware assistant footer.

## Scope

The implementation is limited to the isolated Hermes WebUI worktree. It will:

1. observe successful `post_api_request` events for active WebUI turns;
2. retain only bounded, display-safe routing scalars;
3. attach the final successful routed model to the final assistant message;
4. persist the same normalized object at session level and in routing history;
5. display Requested, Routed, and Provider in the assistant footer;
6. preserve the metadata through normal `done` delivery, run-journal replay,
   session reload, and browser re-rendering.

It will not modify TokenTable API, database, schema, environment, PM2, or
streaming behavior. It will not modify Hermes Agent, repair non-streaming model
overrides, expose endpoint URLs or secrets, restart a live WebUI or gateway, or
push, open a PR, merge, or deploy.

## Architecture

### WebUI lifecycle bridge

Add a focused WebUI module responsible for routed-model capture. It installs one
idempotent process-local observer for Hermes Agent's `post_api_request` lifecycle
event and maintains a lock-protected registry of active WebUI captures.

Before `agent.run_conversation(...)`, the streaming worker begins a capture with:

- WebUI session/task identifier;
- stream identifier;
- requested model after WebUI resolution;
- safe requested-provider identifier.

The observer accepts an event only when all of the following hold:

- `platform` is `webui`;
- `task_id` belongs to an active capture;
- the event contains a non-empty scalar `response_model`;
- the capture still belongs to the current stream.

The observer stores only `response_model`, the safe provider identifier, and the
turn correlation identifiers. It must not retain the response body, assistant
message, user content, usage payload, base URL, headers, credentials, or error
payload.

WebUI sessions serialize turns with their existing per-session agent lock. The
capture registry remains keyed by session/task plus stream so simultaneous tabs
or sessions cannot overwrite one another. Registration is process-wide and
idempotent; capture state is run-scoped and removed on success, cancellation,
error, and stale-stream exit.

### Multi-call and retry semantics

A Hermes turn may make several successful API calls around tool use. The routed
model displayed on the final assistant message is the last non-empty
`response_model` observed for that turn, because it represents the API call that
produced the final assistant response.

Self-heal retries remain inside the same capture. A failed request cannot replace
the value because `post_api_request` fires only after normalized success. A later
successful retry replaces an earlier successful value. No value from a prior
turn or stale stream may be reused.

### Metadata normalization and persistence

Extend the existing routing allowlist with a bounded `source` scalar, then
normalize the capture into the existing routing contract:

```json
{
  "requested_model": "auto",
  "requested_provider": "tokentable",
  "used_model": "gpt-5.6-sol",
  "used_provider": "tokentable",
  "source": "openai-compatible-sse",
  "model_changed": true,
  "provider_changed": false,
  "has_failover": false
}
```

The canonical provider value comes from WebUI's already-resolved provider or
configured custom-provider name. It is never derived by displaying the base URL.
The renderer may humanize the safe identifier, for example `tokentable` to
`TokenTable`.

After the conversation result has been reconciled into `s.messages`, but before
`s.save()`, WebUI will:

1. finish and remove the current capture;
2. normalize it with the existing gateway-routing helper;
3. set `s.gateway_routing`;
4. append the object to the bounded `s.gateway_routing_history`;
5. set `_gatewayRouting` on the last new assistant message for this turn.

The current stale-stream ownership check remains authoritative. Metadata must not
be attached if the response itself is no longer allowed to write back.

No new database or session schema is required. The existing arbitrary message
metadata and session serialization paths carry the fields into the terminal
`done` payload and reload responses. The API-message sanitizer continues to strip
display metadata before the next provider request.

## UI

Reuse the existing final assistant-message footer. When `_gatewayRouting`
contains a non-empty `used_model`, render three labelled values:

```text
Requested: auto · Routed: gpt-5.6-sol · Provider: TokenTable
```

The group is quiet secondary metadata, not response body content and not a
banner. On desktop it may remain on one line; existing footer wrapping plus a
narrow-screen style must allow the three values to wrap without horizontal
overflow. Rendering uses DOM `textContent`, not HTML interpolation.

The footer appears only after the routed model is known at successful response
completion. It must render identically from the live `done` session, run-journal
replay, and a later session reload.

## Missing or Invalid Data

- If `response_model` is missing, empty, non-scalar, or over the existing bounded
  scalar limit, do not claim a routed model and do not show the three-field group.
- If the turn is cancelled, errors, or loses stale-stream ownership, discard the
  capture without attaching metadata.
- Never fall back from `used_model` to the requested `auto` value.
- Never copy the previous turn's routed model onto the current turn.
- Existing explicit gateway metadata remains valid. A fresh SSE-derived capture
  fills the same normalized contract for this turn and takes precedence over a
  missing generic gateway payload; it does not fabricate routing attempts or
  failover history.

## Testing Strategy

Implementation follows strict RED to GREEN.

### Backend unit coverage

- the observer accepts a matching WebUI `post_api_request` event;
- unrelated platforms, task IDs, and stream IDs are ignored;
- simultaneous captures remain isolated;
- the last successful response model wins within one turn;
- missing and malformed values produce no routed-model metadata;
- finish, cancel, exception, and stale-stream paths clear registry state;
- normalization preserves only the explicit safe allowlist.

### Streaming and persistence coverage

With a fake AIAgent/lifecycle event, prove that a requested `auto` turn whose
successful response reports `gpt-5.6-sol` results in:

- final assistant `_gatewayRouting` metadata;
- session `gateway_routing` and bounded history;
- the same metadata in the terminal `done` session payload;
- the same metadata after session save/reload;
- no metadata forwarded in sanitized provider conversation history.

Also cover tool-loop multiple calls, cancellation/error, and stale writeback.

### UI and QA coverage

- a static/frontend test proves the three labels read from `_gatewayRouting`;
- unsafe strings render through `textContent`;
- existing gateway/failover/duration/usage footer metadata still renders;
- isolated local QA uses a disposable Hermes state directory and a fake local
  OpenAI-compatible streaming provider;
- capture desktop and narrow viewport evidence showing the final three fields,
  reload persistence, and no horizontal overflow.

No production TokenTable request is required for RED/GREEN or browser QA, and no
currently running WebUI or gateway process is restarted.

## Acceptance Criteria

Given a WebUI turn with requested model `auto`, safe provider name `tokentable`,
and a successful OpenAI-compatible streaming response whose normalized
`response.model` is `gpt-5.6-sol`:

- the final assistant message persists requested, routed, provider, and source;
- the session persists the same current routing object and bounded history;
- live completion, replay, and reload show Requested `auto`, Routed
  `gpt-5.6-sol`, and Provider `TokenTable`;
- later turns receive no display metadata in provider-bound history;
- missing/error/cancel/stale cases never display a false routed model;
- TokenTable and Hermes Agent remain byte-for-byte unchanged.
