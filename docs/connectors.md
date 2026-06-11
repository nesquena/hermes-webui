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

The first panel version exposes only fields verified against Hermes Agent
gateway configuration and adapters:

- Telegram: bot token, reply mode, mention/group/topic filters.
- Discord: bot token, reply mode, mention/free-response channel filters.
- Webhook: host, port and default secret. Route definitions still live in
  `config.yaml` or Hermes gateway tooling.
- API Server: API key, host, port, model name and CORS origins.

Slack and Email are listed as supported by Hermes but read-only in WebUI because
their current adapters require runtime environment variables such as
`SLACK_APP_TOKEN` or `EMAIL_*`. They should be configured in the runtime
environment until Hermes exposes a safe editable config shape for them.

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
- Telegram docs: `website/docs/user-guide/messaging/telegram.md`
- Discord docs: `website/docs/user-guide/messaging/discord.md`
- Slack docs: `website/docs/user-guide/messaging/slack.md`
- Email docs: `website/docs/user-guide/messaging/email.md`
- Webhook docs: `website/docs/user-guide/messaging/webhooks.md`
- API server docs: `website/docs/user-guide/features/api-server.md`
