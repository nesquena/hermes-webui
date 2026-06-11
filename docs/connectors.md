# Connectors Panel

The Connectors panel lives in **Settings → Connectors** and is linked from the
existing **Settings → System → Gateway Status** card. Gateway Status remains the
read-only runtime view; Connectors is the guided configuration surface.

Connectors configures verified Hermes Agent gateway channels through Hermes'
native `config.yaml`:

```yaml
platforms:
  telegram:
    enabled: true
    token: "..."
```

It does not create a WebUI-specific connector store.

## Supported In The Panel

The panel now lists the Hermes Agent gateway platforms documented and exposed
by Hermes runtime metadata. This intentionally includes platforms that the
original WebUI did not show yet:

- Telegram
- Discord
- Slack
- Google Chat
- WhatsApp
- Signal
- SMS
- Email
- Home Assistant
- Mattermost
- Matrix
- DingTalk
- Feishu / Lark
- WeCom
- WeCom Callback
- Weixin
- BlueBubbles / iMessage
- QQ
- Yuanbao
- Microsoft Teams
- LINE
- ntfy
- Browser / Open WebUI
- API Server
- Webhook

The panel also augments this list from `gateway.config.Platform` and
`gateway.platform_registry` when Hermes exposes additional runtime/plugin
platforms.

Only fields verified against Hermes Agent gateway configuration and adapters are
editable from WebUI:

- Telegram: bot token, reply mode, mention/group/topic filters.
- Discord: bot token, reply mode, mention/free-response channel filters.
- Webhook: host, port and default secret. Route definitions still live in
  `config.yaml` or Hermes gateway tooling.
- API Server: API key, host, port, model name and CORS origins.

The other platforms are listed as runtime-managed/read-only. This is deliberate:
they are real Hermes Agent gateway channels, but their current setup relies on
environment variables, OAuth/device setup, bridge state, or platform-specific
gateway tooling. WebUI should not present those as editable until the
corresponding Hermes config shape has been verified.

## Secrets

Secrets are written only to the active Hermes `config.yaml`. API responses and
the browser UI receive masked values such as:

```text
••••••abcd
```

Submitting an existing masked value preserves the stored secret. The Connectors
backend reads and writes raw YAML so environment placeholders such as
`${TELEGRAM_BOT_TOKEN}` are not expanded into real secret values during save.

## Testing Locally

1. Start WebUI.
2. Open **Settings → Connectors**.
3. Select Telegram or Discord.
4. Enter the required bot token.
5. Click **Save**.
6. Click **Test**.
7. Enable the connector.
8. Restart or reload the Hermes gateway so runtime changes take effect.

The **Test** action performs configuration validation only. It does not start a
gateway process, contact Telegram/Discord, or probe external services.

The runtime note shown in Connectors reuses the same Gateway Status backend
payload as **Settings → System**, so the two panels do not maintain separate
gateway liveness logic.

## Source References

- Hermes gateway configuration: `gateway/config.py`
- Hermes platform registry: `gateway/platform_registry.py`
- Hermes platform plugins: `plugins/platforms/*/plugin.yaml`
- Telegram docs: `website/docs/user-guide/messaging/telegram.md`
- Discord docs: `website/docs/user-guide/messaging/discord.md`
- Slack docs: `website/docs/user-guide/messaging/slack.md`
- Email docs: `website/docs/user-guide/messaging/email.md`
- Webhook docs: `website/docs/user-guide/messaging/webhooks.md`
- API server docs: `website/docs/user-guide/features/api-server.md`
