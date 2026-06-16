# Gateway Surfaces Settings

WebUI exposes Hermes Agent gateway surfaces in separate Settings sections
instead of a single generic section:

- **Settings → Messaging Channels** for chat-style channels such as Telegram,
  Discord, Email, Matrix or runtime platform plugins.
- **Settings → Webhooks** for inbound event surfaces such as Webhook and
  Microsoft Graph Webhook.
- **Settings → Developer API** for API surfaces such as the OpenAI-compatible
  Hermes API Server.

The existing **Settings → System → Gateway Status** card remains the read-only
runtime liveness view. Its configure link opens **Messaging Channels**, because
gateway status primarily reports messaging platform connectivity.

The backend still uses `/api/connectors` as a shared technical API, but the UI
does not present these different Hermes surfaces as one product category.

## Discovery Model

Hermes runtime metadata is the source of truth for supported gateway surfaces:

- `gateway.config.Platform` provides built-in gateway platforms.
- `hermes_cli.plugins.discover_plugins()` is called before reading
  `gateway.platform_registry`, so bundled and installed platform plugins can
  appear without editing WebUI.
- Existing `platforms.*` entries in `config.yaml` are also surfaced, even when
  runtime metadata is temporarily unavailable.

Static WebUI metadata is only an enrichment layer for labels, documentation
links, descriptions and categories. It must not be used as a source list of
supported platforms.

## Surface Categories

### Messaging Channels

Messaging channels are conversational Hermes gateway adapters. Email belongs in
this section because Hermes uses it as a channel for talking to the agent
through IMAP/SMTP credentials such as `EMAIL_ADDRESS`, `EMAIL_PASSWORD`,
`EMAIL_IMAP_HOST` and `EMAIL_SMTP_HOST`.

Only verified fields are editable from WebUI:

- Telegram: bot token, reply mode, mention/group/topic filters.
- Discord: bot token, reply mode, mention/free-response channel filters.

Other runtime-discovered messaging channels are read-only until their Hermes
configuration shape has been verified.

### Webhooks

Webhooks are event ingress surfaces, not chat connectors. Their useful
configuration lives in route/subscription data such as:

```yaml
platforms:
  webhook:
    extra:
      routes:
        github-pr: {}
```

WebUI lists webhook surfaces when Hermes runtime metadata or `config.yaml`
reports them, but keeps them read-only until route, secret, delivery and test
endpoint semantics are implemented against the verified Hermes schema.

### Developer API

Developer API surfaces are external API access paths. `api_server` is the
OpenAI-compatible Hermes API Server, protected by `API_SERVER_KEY`; it is not a
messaging connector and not a webhook feature.

The verified editable API Server fields are:

- API key
- host
- port
- model name
- CORS origins

## Secrets

Secrets are written only to the active Hermes `config.yaml`. API responses and
the browser UI receive masked values such as:

```text
••••••abcd
```

Submitting an existing masked value preserves the stored secret. The backend
reads and writes raw YAML so environment placeholders such as
`${TELEGRAM_BOT_TOKEN}` are not expanded into real secret values during save.

## Testing Locally

1. Start WebUI.
2. Open **Settings → Messaging Channels**.
3. Select Telegram or Discord.
4. Enter the required bot token.
5. Click **Save**.
6. Click **Validate**.
7. Enable the channel when the selected surface supports toggling.
8. Restart or reload the Hermes gateway so runtime changes take effect.

For **Settings → Developer API**, select API Server and validate the configured
API Server fields.

The **Validate** action performs configuration validation only. It does not
start a gateway process, contact Telegram/Discord, probe external services or
validate webhook delivery.

## Source References

- Hermes gateway configuration: `gateway/config.py`
- Hermes platform registry: `gateway/platform_registry.py`
- Hermes platform plugins: `plugins/platforms/*/plugin.yaml`
- Email docs: `website/docs/user-guide/messaging/email.md`
- Telegram docs: `website/docs/user-guide/messaging/telegram.md`
- Discord docs: `website/docs/user-guide/messaging/discord.md`
- Webhook docs: `website/docs/user-guide/messaging/webhooks.md`
- API server docs: `website/docs/user-guide/features/api-server.md`
