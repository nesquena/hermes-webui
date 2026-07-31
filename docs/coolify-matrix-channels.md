# Deploy profile-scoped Matrix Channels on Coolify

This branch adds **Settings → Channels → Matrix** to Hermes WebUI. Matrix credentials, allowlists, gateway processes, sessions, and status are isolated by the active Hermes profile.

## Before deployment

1. Back up the persistent Hermes volume mounted at:

   ```text
   /home/hermeswebui/.hermes
   ```

2. Keep the current Coolify service and its storage definitions. Do not create a new empty Hermes volume.
3. Push this branch to a repository Coolify can access.

## Coolify configuration

Point the existing Hermes WebUI service at this repository and branch.

- **Build pack:** Dockerfile
- **Dockerfile:** `/Dockerfile`
- **Exposed port:** `8787`
- **Health endpoint:** `/health`
- **Required persistent mount:** existing Hermes data volume → `/home/hermeswebui/.hermes`
- **Workspace mount:** existing workspace volume → `/workspace`

Keep the existing Quick Install environment variables, including:

```text
HERMES_WEBUI_HOST=0.0.0.0
HERMES_WEBUI_PORT=8787
HERMES_WEBUI_STATE_DIR=/home/hermeswebui/.hermes/webui
```

The image installs `libolm` and the complete Hermes 0.15.1 Matrix/E2EE Python dependency set. The mounted Hermes Agent source must remain available at:

```text
/home/hermeswebui/.hermes/hermes-agent
```

## Deploy and verify

1. Redeploy the existing Coolify service from this branch.
2. Wait for `/health` to return HTTP 200.
3. Confirm container logs include:

   ```text
   Profile channel gateway supervisor started
   ```

4. Open Hermes WebUI and select the `maverick` profile.
5. Go to **Settings → Channels → Matrix**.
6. Enter Maverick's Matrix homeserver, user ID, and write-only credential.
7. Add only explicitly authorized Matrix users. Start with:

   ```text
   @tyler:thibaultsolutions.com
   ```

8. Leave **Allowed rooms** blank for the first direct-message test, keep **Require mention** enabled for rooms, and select **E2EE required**.
9. Choose **Save & Restart Gateway**.
10. Confirm the status badge changes to **Running**.
11. Invite Maverick only to a new, non-sensitive test room and verify:
    - An unauthorized user receives no response.
    - An authorized user can receive a response.
    - Room messages require a mention.
    - Direct-message and room sessions remain separate.
    - Restarting the Coolify container restores the enabled Maverick gateway.

Only after these checks should Maverick be invited into the family room.

## Rollback

1. Select the previous Coolify deployment/image.
2. Redeploy without deleting or replacing `/home/hermeswebui/.hermes`.
3. If necessary, disable Matrix before rollback with **Disconnect**. This stops the managed profile gateway and removes only Matrix-specific profile values.

The feature preserves unrelated `.env` and `config.yaml` content. Matrix secrets are never returned by the API and are stored with file mode `0600`.
