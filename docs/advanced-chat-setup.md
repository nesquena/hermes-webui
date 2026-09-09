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

On the Runs API path, WebUI adds a `workspace` field to the `POST /v1/runs` request only when the session's canonical workspace path resolves to `/workspace` itself or a path beneath it. Any other session workspace — a sibling prefix such as `/workspace-other`, a path that escapes through `..` or a symlink, or an empty value — is omitted from the request, leaving the gateway's default working-directory behavior. Both containers must mount and agree on `/workspace` for this to be meaningful: WebUI validates the path in its own filesystem, and the gateway must revalidate it in its own filesystem before applying it; WebUI's validation alone does not prove the path exists or is safe on the gateway side.

On the Runs API path, mid-run guidance typed into an active browser run is relayed as `POST /v1/runs/{run_id}/steer` with a JSON body `{"text": ...}`. Because the gateway publishes its run id only once the run has started, WebUI waits up to five seconds for that startup publication before sending. The relay resolves the gateway base URL from the same configuration authority the run was created with (the `webui_gateway_base_url` config key or `HERMES_WEBUI_GATEWAY_BASE_URL`), refuses HTTP redirects, and treats a response that does not verifiably land as a 2xx on the steer route itself as a delivery failure — a redirect to a 200 page is not proof of delivery. If the steer request is answered with HTTP 404, 405, or 410 — whether the endpoint is unsupported or the run is no longer known — WebUI preserves the submitted text in the next-turn queue and does not cancel the current run. For HTTP 409, other HTTP errors, timeouts, and network failures it restores the draft for manual retry instead, because an uncertain response may still have followed acceptance and automatic re-delivery could duplicate guidance. An accepted relay means the guidance was delivered to the gateway, not that the agent is proven to have applied it, and relaying never cancels the run. If the agent accepts the guidance but finishes its turn before consuming it, the terminal `run.completed` payload's `pending_steer` field is translated into the existing `pending_steer_leftover` event, so the browser queues the leftover text for the next turn exactly like the in-process backend instead of dropping it. This relay requires the same `HERMES_WEBUI_GATEWAY_USE_RUNS_API=true` opt-in plus a gateway that advertises approval support; the legacy chat-completions transport has no run-id lifecycle to relay through, so mid-run guidance there degrades to the same next-turn queue (the run is never cancelled and nothing is claimed to have been applied live). The agent-side steer endpoint and its consumption of the `workspace` field live in Hermes Gateway, not in this repository, and are not verified here — this section describes WebUI's request behavior only and asserts no minimum agent version.

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
