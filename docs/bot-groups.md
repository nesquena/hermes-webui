# Experimental Bot groups

This opt-in page is a thin client for Hermes Agent's `groups.*` protocol v2.
Hermes owns room membership, shared Agent context, execution, approvals and
durable history. WebUI does not run an additional Agent or coordination service.

## Scope and access

- Create a group with 2–6 different profiles on one Hermes installation.
- Mention a member, send a message to the shared discussion, replay history,
  stop the group, approve once or deny, and explicitly retry a failed task.
- This is **installation-owner access**, not multi-user social messaging.
  Profile-bound logins and profile-isolated deployments are denied. Do not
  enable it on an installation shared with untrusted WebUI administrators.
- Bot direct messages, attachments, cross-server invites, replica promotion,
  remote Dashboard OAuth and scheduling controls are not included in this slice.
  Existing Chat remains unchanged.

## Enable

Use an Agent runtime exposing groups protocol v2 with its hosted room driver
running. The reference contract was verified against Hermes Agent `v2026.9.7`.
Version alone is not sufficient: the page checks the advertised capabilities.

Install the optional `websockets>=15` package in the **WebUI environment** and
set these variables in the WebUI service environment:

```text
HERMES_WEBUI_BOT_GROUPS=1
HERMES_WEBUI_BOT_GATEWAY_URL=ws://127.0.0.1:9119/api/ws
HERMES_WEBUI_BOT_GATEWAY_TOKEN=<Dashboard token supplied on the server>
```

The port is an example; use the actual Dashboard port. Only a loopback `ws`
`/api/ws` endpoint is accepted (same host or an operator-managed SSH tunnel).
This is not the OpenAI-compatible API, and it is not the messaging-channel
Gateway. Keep the token server-side; never place it in a browser URL or a PR.
Restart WebUI to apply the environment and open **Bot groups** next to Chat.

To roll back, unset `HERMES_WEBUI_BOT_GROUPS` and restart WebUI. The navigation
and bridge become unavailable. No Hermes room or message is deleted, and
disabling the UI does not cancel already running Hermes tasks.

## State and transport contract

`api/bot_groups.py` allows only profiles, capabilities, room list/create/state/log,
send, stop, retry and exact approval operations. Existing authentication and
CSRF checks run before the bridge; the installation-owner check runs on every
request. Credentials and sensitive profile metadata never reach the browser.

Each HTTP request opens one bounded WebSocket RPC and closes it. No new
background process or database is introduced. The browser polls state/log while
the page is visible, releases the poll on exit, discards late room responses and
keeps only the latest 500 events in its display cache. It refuses to mix logs
from different authority epochs; it never promotes a replica.

Writes are **not automatically retried**. An unconfirmed send retains the draft
and event ID; clicking Send again reuses the identical payload and `thread_id`.
Changing the draft creates a new send identity. New groups likewise reuse a
room ID for an identical unconfirmed create. Drafts are in memory only: reload
recovers committed Hermes history, not an unsent draft.

Approvals carry the exact room/member/task/execution-generation/request identity
and offer only `once` or `deny`. Explicit task retry requires confirmation because
the earlier attempt may already have caused side effects. The UI does not invoke
ordinary Chat's approval path. Agent text is rendered as text, not trusted HTML.

## Verification

Run the bridge and HTTP boundary tests with the repository runner:

```bash
./scripts/test.sh tests/test_bot_groups.py tests/test_bot_groups_http.py -q
```

With Playwright and a browser installed, run the real WebUI HTTP + synthetic
WebSocket browser fixture at desktop and phone widths:

```bash
BOT_GROUPS_BROWSER_CHANNEL=chrome BOT_GROUPS_SCREENSHOTS=docs/images/bot-groups \
  ./scripts/test.sh tests/test_bot_groups_browser.py -q
```

For the real official handlers, hosted room service and SQLite contract:

```bash
HERMES_BOT_GROUPS_REFERENCE=/path/to/hermes-agent \
  ./scripts/test.sh tests/test_bot_groups_hermes_contract.py -q
```

That reference test substitutes only the Agent execution interface. Neither the
browser fixtures nor the contract test establishes real-provider LLM acceptance,
packaged deployment, or multi-user authorization. Use isolated Hermes/WebUI state
for a separate manual provider-backed trial before enabling this experimentally.

Visual evidence (synthetic data): [before, feature off](images/bot-groups/webui-before-1280.png),
[after, desktop](images/bot-groups/webui-after-1280.png),
[before, phone](images/bot-groups/webui-before-390.png),
[after, phone](images/bot-groups/webui-after-390.png).
