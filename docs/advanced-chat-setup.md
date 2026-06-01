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
