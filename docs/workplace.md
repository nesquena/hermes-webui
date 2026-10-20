# Managed workplace mode

This fork includes a chat-first preset for a supervised company workstation.
Compose enables it by default; the native `.env.example` also enables it when
copied to `.env`. Existing native installations must explicitly set
`HERMES_WEBUI_MANAGED_MODE=1` in their server environment. Without that variable,
native behavior remains compatible with upstream.

## Operator setup

Configure these variables privately on the **WebUI server**, then restart it:

```dotenv
HERMES_WEBUI_MANAGED_MODE=1
HERMES_WEBUI_ADMIN_PIN=
```

Fill in a private PIN of at least six characters (or a longer passphrase).
There is deliberately no default. Missing, too short, or over-128-character
PINs leave administration locked. Do not commit your populated `.env`, paste
the PIN into chat, or place it in frontend JavaScript. Compose forwards the two
variables only to the WebUI service, including the two/three-container layouts.

Before handing the workstation to an employee, unlock Settings, complete the
provider/model/workspace setup, verify a real conversation, and click **Lock
administration**. The employee view skips the setup wizard; it does not create
a provider configuration or silently mark onboarding complete on disk.

Use the existing login/SSO configuration independently. The admin PIN is not
login protection and does not make an otherwise open WebUI safe to expose on
a network. Use HTTPS for network access. For a shared kiosk, lock administration
before handing over the keyboard; an unlocked cookie is shared by tabs in the
same browser profile. Separate employees need separate authenticated deployment
boundaries; this feature does not create per-employee conversation isolation.

## Employee view

- Chat, conversation history, New conversation, attachments, Send and Stop.
- Settings remains as the administrator entry point and requests the PIN.
- Tasks, Kanban, Skills, Memory, Spaces, Profiles, Todos, Insights and Logs are
  hidden on desktop and mobile, including before the first render.
- Model, profile, workspace, toolset, reasoning and YOLO selectors, microphone,
  continuous voice, saved prompts and the workspace browser are hidden.
- External CLI/messaging/cron/webhook/worker histories, update checks, welcome
  suggestions and dashboard plugins are off through server-owned policy.
- Public share creation is administrative; existing share URLs cannot be
  viewed by locked employee/anonymous browsers while this mode is enabled.

Errors, tool approvals and clarification questions remain visible. Hiding the
YOLO selector does **not** change an existing agent's approval policy. Configure
that policy explicitly before use.

## Administration

The PIN is verified on the server with PBKDF2-SHA256. A successful unlock issues
a random, HttpOnly, SameSite=Strict cookie (Secure when the existing WebUI TLS
configuration requires it), valid for ten minutes. It is bound to the current
login session when login is enabled. Lock revokes it immediately on the server;
PIN rotation and server restart invalidate it too. Other tabs reconcile their
presentation on focus and every 30 seconds; the API checks every request.
The same-origin/CSRF checks apply to unlock and lock. Unlock is limited to five
attempts per socket peer per minute, with a global cap of fifty per minute.
Behind a reverse proxy employees share that peer limit; forwarded IP headers
are intentionally not trusted to bypass it.

All HTTP write methods pass through the policy before route dispatch, including
extension sidecar proxies. Only a small explicit set of conversation operations
is available while locked; new write routes require administration by default.
Sensitive GET routes (provider settings, logs, cron execution, sidecars, etc.)
also require administration. Operational reads needed by chat remain available.
`GET /api/settings` returns a limited UI snapshot without password hashes or
unknown saved keys. Changing browser state or `localStorage` cannot grant API
administration.

## Defaults and customization

`api/workplace.py:POLICY` is the authoritative workplace presentation policy.
It is applied after `_SETTINGS_DEFAULTS` and saved preferences in
`api/config.py:load_settings()`. Loading does not rewrite the settings file.
The policy remains effective in administrator view, although administrators can
open all navigation panels and configure providers and models in Settings.
Change this policy in server code, rather than in user-editable `hidden_tabs`.

Keep the existing branding until company assets are available. The assistant
name can be changed in Settings (`bot_name`), or initially through
`HERMES_WEBUI_BOT_NAME` when no name has already been saved. A complete logo
replacement also requires the inline SVG marks in `static/index.html`, favicons,
Apple touch icon and PWA `static/manifest.json`; `bot_name` alone is not a full
brand replacement.

The default model still uses the existing Hermes `config.yaml`/environment
resolution. This feature does not add a second provider/model resolver.

## Scope and rollback

This is a WebUI administration gate and a presentation preset, **not an agent
sandbox or a model/tool allowlist**. Chat APIs retain their existing request
semantics, including per-turn/session parameters. A permitted agent can still
access its configured tools, files and process environment. Do not give that
agent access to operator secrets, unrestricted shell tools, unrelated company
files or the host configuration. Enforce those boundaries in agent policy and
OS/container permissions; use a separate runner where appropriate. Conversation
file/media downloads remain available to support assistant-generated artifacts.

Set `HERMES_WEBUI_MANAGED_MODE=0` and restart to restore the standard UI and
route behavior. No sessions or credentials are deleted. If an administrator
saves settings while the mode is active, effective policy values can be saved
alongside their changes; adjust those ordinary preferences after rollback if
needed. The secret PIN is never stored in `settings.json`.

## Verification

Run the isolated route/policy tests and neighboring checks:

```bash
HERMES_HOME=/tmp/hermes-workplace-test-home \
HERMES_WEBUI_STATE_DIR=/tmp/hermes-workplace-test-state \
./scripts/test.sh tests/test_workplace.py tests/test_auth.py \
  tests/test_settings_appearance_defaults_honoured.py \
  tests/test_sidebar_tab_visibility.py tests/test_index_shell_template_cache.py
```

Check desktop and mobile: locked chat, cancel/wrong PIN, successful unlock,
Settings save, manual lock, expiry, reload and a second tab. Verify a locked
direct `POST /api/settings` returns 403 and cannot change the saved value.
