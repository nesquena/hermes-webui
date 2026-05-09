# Mini-SDD: External Memory Review API

## Goal
Expose a generic review-and-approval API for external memory providers that maintain human-reviewable candidate queues.

## Scope
- Discover custom external memory providers for the active Hermes profile.
- Allow custom providers via `$HERMES_HOME/external_memory_providers.json`.
- List, search, edit, approve, reject, and delete reviewable candidate rows.
- Keep approval fail-closed when optional indexing is not configured or fails.
- Do not ship any built-in provider, endpoint, hostname, IP address, model, or collection default.

## Non-goals
- No provider-specific implementation is bundled.
- No coupling to any private memory backend.
- No automatic discovery of existing agent memory plugins.

## Provider registration
A provider is registered in the active Hermes home:

```json
{
  "providers": [
    {
      "id": "custom_store",
      "label": "Custom Store",
      "db_path": "custom_store/items.sqlite",
      "config_path": "custom_store/config.json"
    }
  ]
}
```

## Candidate table contract

```sql
candidates(
  id text primary key,
  text text not null,
  source text not null default 'agent',
  metadata_json text not null default '{}',
  state text not null default 'candidate',
  content_sha256 text not null,
  created_at real not null,
  updated_at real not null
)
```

## Optional indexing config
Indexing URLs and collection/model names must come from provider config or environment variables:

- `ollama_url` / `HERMES_EXTERNAL_MEMORY_OLLAMA_URL`
- `embed_model` / `HERMES_EXTERNAL_MEMORY_EMBED_MODEL`
- `qdrant_url` / `HERMES_EXTERNAL_MEMORY_QDRANT_URL`
- `qdrant_collection` / `HERMES_EXTERNAL_MEMORY_QDRANT_COLLECTION`

When these are missing, approval fails clearly and leaves the candidate unchanged.

## Acceptance
- No providers appear until a custom provider is registered.
- Custom providers registered in JSON can be selected and queried.
- Missing indexing config disables approval rather than falling back to defaults.
- Existing local memory/profile editing remains unchanged.
