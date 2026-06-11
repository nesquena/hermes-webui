# Connectors Panel

The Connectors panel lives in **Settings → Connectors** and is linked from the
existing **Settings → System → Gateway Status** card. Gateway Status remains the
read-only runtime view; Connectors is the guided configuration surface.

Connectors discovers Hermes Agent gateway surfaces from Hermes runtime metadata
and configures only the WebUI-verified subset through Hermes' native
`config.yaml`:

```yaml
platforms:
  telegram:
    enabled: true
    token: "..."
```

It does not create a WebUI-specific connector store.

## Discovery Model

Hermes runtime metadata is the source of truth for supported surfaces:

- `gateway.config.Platform` provides built-in gateway platforms.
- `hermes_cli.plugins.discover_plugins()` is called before reading
  `gateway.platform_registry`, so bundled and installed platform plugins can
  appear without editing WebUI.
- Existing `platforms.*` entries in `config.yaml` are also surfaced, even when
  runtime metadata is temporarily unavailable.

Static WebUI metadata is only an enrichment layer for labels, documentation
links and categories. It must not be used as a source list of supported
platforms.

## Categories

The panel separates Hermes surfaces by responsibility:

- **Messaging channels**: chat-style channels such as Telegram, Discord, Email,
  Matrix or plugin platforms discovered from the runtime.
- **Event webhooks**: inbound event surfaces such as Webhook and Microsoft Graph
  Webhook. These are not simple chat channels because their value lives in
  route/subscription configuration.
- **Developer API**: API surfaces such as the OpenAI-compatible API Server.

Only fields verified against Hermes Agent gateway configuration and adapters are
editable from WebUI:

- Telegram: bot token, reply mode, mention/group/topic filters.
- Discord: bot token, reply mode, mention/free-response channel filters.
- API Server: API key, host, port, model name and CORS origins.

The other surfaces are listed as runtime-managed/read-only. This is deliberate:
they are real Hermes Agent gateway capabilities, but their current setup relies
on environment variables, OAuth/device setup, bridge state, route definitions,
or platform-specific gateway tooling. WebUI should not present those as editable
until the corresponding Hermes config shape has been verified.

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
3. Select Telegram, Discord or API Server.
4. Enter the required bot token.
5. Click **Save**.
6. Click **Test**.
7. Enable the connector when the selected surface supports toggling.
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
- Webhook docs: `website/docs/user-guide/messaging/webhooks.md`
- API server docs: `website/docs/user-guide/features/api-server.md`
