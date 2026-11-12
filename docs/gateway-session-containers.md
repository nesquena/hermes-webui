# Canonical sessions across Linux containers

Gateway native bootstrap tickets are loopback-only. Mounting its state directory
into a separate WebUI container does not make that container a native client.
For an explicitly trusted WebUI installation, an optional WebUI helper can run
in the Gateway's network namespace and expose a private Unix socket to WebUI.
No Agent source changes or REST key privilege changes are involved.

The helper grants **native operator access** to its allowed profiles. Treat access
to its socket as access to those profiles' conversations and tools. It is suitable
only when both containers belong to the same trusted operator. This is not a
multi-tenant network service; never publish it over TCP or mount it into untrusted
containers. Existing WebUI authentication and request-profile authorization still
apply. Both processes must run with the same Linux UID as the Gateway.

The helper is a frame relay, not an agent runner: the Gateway remains the sole
owner of admission, tools, approvals, execution and durable history. It obtains
short-lived native tickets itself and does not send tickets to WebUI. It starts no
Gateway and stores no conversations or alternate run registry.

## Wiring

Prepare a dedicated directory owned by the service UID, mode `0700`. Mount it at
`/run/webui-session` in both containers (read-only in WebUI). The socket is created
with mode `0600`; both peers verify the other's UID using Linux `SO_PEERCRED`.
Symlink socket paths, unsafe directory permissions and foreign owners are refused.

Run the helper with the installed Agent's Python environment and this WebUI
checkout on `PYTHONPATH`. It also needs the Agent source on `PYTHONPATH`, the
Gateway home at its **same canonical absolute path**, and any private bootstrap
runtime directory at its same path. Those mounts can be read-only. It does not
need WebUI's home path to match the Gateway's.

For example, add a service to an operator-managed Compose project:

```yaml
services:
  session-helper:
    image: ${GATEWAY_IMAGE} # same pinned image/runtime as the gateway service
    user: "10000:10000"    # use your actual Gateway and WebUI service UID/GID
    network_mode: service:gateway
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    environment:
      HERMES_HOME: /opt/data
      XDG_RUNTIME_DIR: /tmp/hermes-runtime
      PYTHONPATH: /webui:/opt/hermes
      PYTHONDONTWRITEBYTECODE: "1"
    volumes:
      - ./webui-checkout:/webui:ro
      - ./gateway-data:/opt/data:ro
      - ./gateway-runtime:/tmp/hermes-runtime:ro
      - ./session-socket:/run/webui-session
    entrypoint: /opt/hermes/.venv/bin/python
    command:
      - -m
      - api.gateway_session_bridge
      - --home
      - /opt/data
      - --socket
      - /run/webui-session/session.sock
      - --profile
      - default
      - --profile
      - work
```

Adapt paths to the installed image; this example assumes its Python/source live
under `/opt/hermes`. The Gateway must also mount `gateway-runtime` and use that
`XDG_RUNTIME_DIR`. Add `./session-socket:/run/webui-session:ro` to WebUI's volumes.
There are no helper ports. Do not mount the Docker daemon socket into WebUI.
Restart the helper after recreating the Gateway container so it joins the new
network namespace. Ordinary helper shutdown handles `SIGTERM` and removes its
socket; it does not interrupt Gateway-owned turns.
Each `--profile` names one existing profile under the Gateway home; omission
denies access. Profile names are resolved by the helper, never supplied as paths
by WebUI. Add only profiles the WebUI operator is authorized to control.
Use `--profile default` for the root Gateway home even if its WebUI display name
has been renamed. WebUI normalizes recognized root aliases to `default` in the
socket handshake and saved recovery binding; named profiles remain distinct.

In each eligible WebUI profile's `config.yaml`, set:

```yaml
webui_chat_backend: gateway
webui_gateway_session_transport: unix
webui_gateway_session_socket: /run/webui-session/session.sock
```

Keep the normal profile-specific REST configuration for API-owned sessions.
External sessions use the private socket; the REST API key is never promoted.
Composer eligibility reads the session's own profile configuration, including
config-only setups without a backend environment variable. Error and Stop
responses preserve that eligibility so another turn does not require a reload.
Detached workers resolve the same explicit profile home, including root aliases,
even when the WebUI process is running under a different named profile.
The helper supports the same canonical-session Agent version requirements and
text/control limitations as the [local transport](advanced-chat-setup.md).

## Recovery and removal

Disconnecting the helper does not cancel a Gateway turn. Reconnecting uses the
same durable input ID. While an admission's outcome is uncertain, WebUI retains
its existing stream and recovery worker and retries with bounded backoff. Opening
the conversation again attaches to that worker; it does not discard the pending
request or admit a replacement. A WebUI restart restores the saved request.
WebUI journals the transport mode, socket path and profile
with that request and refuses recovery if the connection configuration changes.
Keep the socket path bound to the same Gateway deployment; do not repoint it to
another instance while turns are pending.

For rollback, let pending turns settle, remove the profile's transport setting,
then stop the helper and remove its dedicated mounts. External sessions return to
the read-only composer. No Gateway state migration or source rollback is needed.
The helper removes its own socket on orderly shutdown, but intentionally refuses
to replace an existing socket at startup. After an unclean exit, verify no helper
is running before removing only that stale socket and restarting it.
