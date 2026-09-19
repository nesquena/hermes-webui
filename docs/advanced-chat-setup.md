# Advanced chat setup

Two optional features for self-hosted Hermes WebUI deployments. **Most users need neither** — the defaults (in-process chat, no prefill) work out of the box.

## Session recall prefill

WebUI can attach ephemeral prefill messages to new browser-originated
agent turns. This is useful when a deployment already has a local recall or
router script for Joplin, Obsidian, Notion, llm-wiki, or another third-party
notes source and wants browser chat to know where durable context lives.

Prefer a compact router-style prefill (for example, "Joplin has the durable
project context; use the available notes/search tools before answering
detail-dependent questions") instead of dumping the full note corpus into every
new browser session. The prefill should point the agent toward retrieval; the
notes/search tools should provide the specific facts on demand.

Static JSON remains supported through `prefill_messages_file` or
`HERMES_PREFILL_MESSAGES_FILE`. For dynamic recall, opt in explicitly with a
WebUI-specific script hook:

```yaml
webui_prefill_messages_script:
  - python3
  - /path/to/notes_recall.py
webui_prefill_messages_script_timeout: 5
```

or:

```bash
HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT="python3 /path/to/notes_recall.py" \
HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT_TIMEOUT=5 \
./ctl.sh restart
```

The script may print either an OpenAI-style JSON message list, a JSON object with
a `messages` list, or plain text; plain text is wrapped as one `user` prefill
message so dynamic recall text becomes ordinary context instead of an extra
system instruction. If the hook must provide system-level guidance, emit JSON
messages with an explicit `role: "system"` entry instead. Script output is capped
at 256 KiB before parsing. Parsed prefill context is then bounded by
`webui_prefill_context_max_chars` or `HERMES_WEBUI_PREFILL_CONTEXT_MAX_CHARS`
(default: 12,000 characters; set to `0` to disable). When a dynamic script
exceeds the budget and a compact static prefill file is configured, WebUI falls
back to that file. If no compact fallback is available, WebUI injects a short
retrieval instruction instead of sending the oversized note/body payload with
every new browser turn. The browser only receives a compact status event
(`source`, `label`, message count, compaction metadata, and redacted errors),
never the prefill message bodies.

## Session title generation

Hermes WebUI derives a provisional session title from the first user message
and, after the first response, may call an LLM to generate a better title
(and periodically refresh it for long sessions).

For structured messages containing text and native images, title generation
uses the user text without flattening or modifying the stored message. Title
comparison and title-model inputs remove the internal `[Workspace::v1: ...]`
prefix and one terminal `[Attached files: ...]` or
`[Attached files for this steer: ...]` suffix separated by a blank line.
For structured content, this cleanup applies to the first text part that
provides title content. Literal legacy `[Workspace: ...]` text and later text
parts remain unchanged. Initial generation, explicit regeneration, and adaptive
refresh use this title-specific cleanup.

Background generation requires user text and a substantive assistant response.
It recognizes the sanitized provisional title as well as the existing raw
placeholder, so internal metadata does not make an image-containing turn look
manually titled. Image-only or metadata-only content does not provide title
text. Existing manual-title protection and the title-generation setting still
apply; this cleanup does not rewrite the transcript or native image parts.

Automatic title-generation LLM calls honor the active Hermes profile's
`auxiliary.title_generation.enabled` setting (default: `true`):

```yaml
auxiliary:
  title_generation:
    enabled: false
```

When disabled:

- the provisional first-message title stays in place and is never replaced
  or overwritten by an automatic LLM call or local fallback;
- the periodic adaptive refresh is skipped;
- the explicit "regenerate title" action returns a
  `title_generation_disabled` response instead of calling a title model.

The WebUI's `auto_title_refresh_every` setting remains a separate control for
periodic refreshes of already-generated titles; it does not re-enable
automatic generation when the auxiliary flag is off.

## Gateway-backed browser chat

By default, browser chat runs through WebUI's in-process legacy runtime. Advanced
self-hosted deployments can opt into routing new browser turns through a running
Hermes Gateway API server while preserving the existing WebUI `/api/chat/start`
and `/api/chat/stream` browser contract:

```bash
HERMES_WEBUI_CHAT_BACKEND=gateway \
HERMES_WEBUI_GATEWAY_BASE_URL=http://127.0.0.1:8642 \
HERMES_WEBUI_GATEWAY_API_KEY=... \
./ctl.sh restart
```

Gateway-backed approval prompts need one more explicit opt-in because they use the Gateway runs API path:

```bash
HERMES_WEBUI_CHAT_BACKEND=gateway \
HERMES_WEBUI_GATEWAY_BASE_URL=http://127.0.0.1:8642 \
HERMES_WEBUI_GATEWAY_API_KEY=... \
HERMES_WEBUI_GATEWAY_USE_RUNS_API=true \
./ctl.sh restart
```

Use this when the connected gateway advertises approval support and you want tool approval cards to appear in WebUI. Without `HERMES_WEBUI_GATEWAY_USE_RUNS_API=true`, gateway chat stays on the legacy chat-completions transport and approval-capable commands can remain pending in the agent without a WebUI approval card.

When YOLO is enabled for a gateway-backed browser session, WebUI approves every
approval already parked for that session: Runs API prompts are relayed by their
exact `run_id` and mirror token, and local/no-run waiters are all released. It
then automatically answers later Runs API approval requests while the WebUI
session flag remains active. The flag is committed only after every currently
parked remote relay succeeds; a later prompt that races that unconfirmed drain
remains visible instead of being speculatively auto-approved. The handoff is
also shared with local approval admission: a local waiter arriving after
the current drain snapshot waits for the same session handoff and is released
immediately if YOLO has committed, rather than being parked behind an enabled
session. This is client-managed compatibility behavior: the current Runs API has
no session-YOLO toggle, so a request briefly reaches the approval boundary before WebUI answers
it, and Agent-owned policy such as unrestricted computer-use mode is unchanged.
Native API session YOLO is tracked in [Hermes Agent PR #61946](https://github.com/NousResearch/hermes-agent/pull/61946).

`HERMES_WEBUI_CHAT_BACKEND` is intentionally strict: only `gateway`,
`api_server`, or `api-server` enable the bridge. Generic truthy values such as
`1` or `true` are ignored so existing deployments do not change execution
ownership accidentally. If `HERMES_WEBUI_GATEWAY_API_KEY` is omitted, WebUI falls
back to `API_SERVER_KEY` when present. When Gateway returns HTTP 401, WebUI
reports a `gateway_auth_error` that points at this WebUI↔Gateway key mismatch
rather than showing the Gateway's generic provider-style "Invalid API key" body.
`/api/health/agent` also includes a redacted `gateway_chat` block so operators can
see whether gateway mode, base URL, and API-key presence are configured without
exposing the key value. That `gateway_chat` field is an operator diagnostic
payload only; it is not currently rendered as a user-facing health banner in the
browser UI.

The bridge is best used by operators who already run Hermes Gateway/API Server
locally and want browser-originated chat to use the same runtime/tool path as
messaging surfaces. Attachments, cancellation, approvals, and clarify prompts
still follow WebUI's current compatibility path and may not match every messaging
surface until the runtime-adapter migration is complete.

### Usage and context-window accounting

A gateway-backed session builds no in-process agent or `ContextCompressor`, so
the only usage data it ever sees is what the OpenAI-compat terminal chunk
carries. WebUI now persists that into durable per-session totals and forwards
enough of it for the browser context ring to size itself correctly:

- `input_tokens`, `output_tokens`, `estimated_cost`, `cache_read_tokens`, and
  `cache_write_tokens` accumulate into session lifetime totals on every
  terminal chunk. `static/messages.js` derives each turn's own badge by
  subtracting the pre-turn session totals, so these fields are cumulative by
  design, not per-turn.
- `last_prompt_tokens` / `threshold_tokens` are presence-sensitive, not
  truthiness-sensitive: an explicit `0` from the gateway (the compressor's
  post-compaction clamp) is trusted outright and overwrites the previous
  numerator, exactly like any other real value — WebUI can tell "genuinely
  zero right now" apart from "this gateway never sent the field" only by
  checking whether the key is present in the payload at all, so a plain
  falsy check can't be used here the way it is for the billing/cache
  counters above. A gateway that omits the key entirely (older versions
  before hermes-agent#105905) leaves the previous numerator exactly where it
  was; WebUI does not attempt to infer one from `input_tokens` or from
  whether any tool-progress events were observed for the turn. That
  inference used to exist and was removed: an empty tool-call list only
  proves no tool-progress *events* arrived, not that no tools ran — a
  gateway or intermediate proxy that doesn't emit those optional events
  would report a multi-call aggregate as if it were a single real prompt,
  which is a confidently wrong numerator and worse than a stale one.
- `context_length` (the ring's denominator) is resolved once per session from
  the connected model/provider and persisted, so a browser reload does not
  re-derive it. `256000` is the resolver's "unknown model" fallback and is
  deliberately never persisted, so an unrecognized model can still resolve
  correctly once its metadata becomes available instead of being pinned wrong
  forever. `threshold_tokens` mirrors the gateway's real compression
  threshold when sent, or defaults to 75% of `context_length` — the same
  default `ContextCompressor` uses — so the "Auto-compress at X" tooltip has
  a number instead of hiding.
- `@provider:model` route hints are parsed with the shared
  `_split_provider_qualified_model` grammar (`api/routes.py`, #6722) at both
  the outgoing request body and the context-length lookup, so a model tag
  with its own colon (`@openrouter:meta/llama-4:free`) or a custom provider
  slug derived from a host:port authority
  (`@custom:10.8.71.41:8080:Qwen3`) resolve to the same lane both places.
