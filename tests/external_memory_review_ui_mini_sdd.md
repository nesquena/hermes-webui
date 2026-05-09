# Mini-SDD: External Memory Review UI

## Goal
Expose a generic review surface inside the existing Memory panel for custom external memory providers with candidate approval queues.

## Scope
- Add an `External Memory` entry to the Memory panel.
- Load providers from `/api/external-memory/providers`.
- Show a clear empty state when no providers are configured.
- For a selected provider, show:
  - pending candidates
  - approved rows
  - text search results
- Allow review actions:
  - edit before approval
  - approve
  - reject
  - delete local row
- Render common metadata:
  - state
  - type
  - confidence
  - source
  - rationale
  - optional indexing point id

## Non-goals
- No hardcoded provider name or built-in backend assumption.
- No hardcoded endpoint, model, collection, IP address, hostname, or private infrastructure.
- No automatic integration with existing memory plugins unless they explicitly register a review queue.

## UX contract
- The UI remains generic and provider-neutral.
- If no providers are registered, the panel explains how to register one via `external_memory_providers.json`.
- Provider-specific semantics live in provider metadata/config; the review panel only renders the common candidate shape.
- A semantic sentence guidance block is displayed to help normalize knowledge before approval.
