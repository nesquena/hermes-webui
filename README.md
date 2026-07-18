# Hermes Web UI

[Hermes Agent](https://hermes-agent.nousresearch.com/) is a sophisticated autonomous agent that lives on your server, accessed via a terminal or messaging apps, that remembers what it learns and gets more capable the longer it runs.

Hermes WebUI is a lightweight, dark-themed web app interface in your browser for [Hermes Agent](https://hermes-agent.nousresearch.com/).
Full parity with the CLI experience - everything you can do from a terminal, you can do from this UI. No build step, no framework, no bundler. Just Python and vanilla JS.

Layout: three-panel. Left sidebar for sessions and navigation, center for chat,
right for workspace file browsing. Model, profile, and workspace controls live in
the **composer footer** — always visible while composing. A circular context ring
shows token usage at a glance. All settings and session tools are in the
**Hermes Control Center** (launcher at the sidebar bottom).

Setup Hermes so you can access it natively on every device:

<img width="1467" height="881" alt="image" src="https://github.com/user-attachments/assets/9a72cdf3-a5b4-45ed-a836-a715ce46287e" />

<table>
  <tr>
    <td width="50%" align="center">
      <img width="2940" height="1848" alt="Light mode with full profile support" src="https://github.com/user-attachments/assets/4ef3a59c-7a66-4705-b4e7-cb9148fe4c47" />
      <br /><sub>Light mode with full profile support</sub>
    </td>
    <td width="50%" align="center">
      <img alt="Customize your settings, configure a password" src="https://github.com/user-attachments/assets/941f3156-21e3-41fd-bcc8-f975d5000cb8" />
      <br /><sub>Customize your settings, configure a password</sub>
    </td>
  </tr>
</table>

<table>
  <tr>
    <td width="50%" align="center">
      <img alt="Workspace file browser with inline preview" src="docs/images/ui-workspace.png" />
      <br /><sub>Workspace file browser with inline preview</sub>
    </td>
    <td width="50%" align="center">
      <img alt="Session projects, tags, and tool call cards" src="docs/images/ui-sessions.png" />
      <br /><sub>Session projects, tags, and tool call cards</sub>
    </td>
  </tr>
</table>

This gives you nearly **1:1 parity with Hermes CLI from a convenient web UI** which you can access securely through an SSH tunnel from your Hermes setup. Single command to start this up, and a single command to SSH tunnel for access on your computer. Every single part of the web UI uses your existing Hermes agent and existing models, without requiring any additional setup.

---

## Contents

[<img width="750" alt="image" src="https://github.com/user-attachments/assets/7e9544a7-ba47-4fc7-8142-1d9d16b17065" />
](https://get-hermes.ai/setup/) 

- [Why Hermes](#why-hermes) — what it is and how it compares
- [Quick start](#quick-start) — clone + `bootstrap.py` / `start.sh` / `ctl.sh`
- [Features](#features) — chat, sessions, workspace, voice, profiles, security, themes, panels, mobile
- [Configuration & access](#configuration--access) — auto-discovery, overrides, remote/Tailscale/phone, manual launch
- [Nix flake/module](#nix-flake-and-nixos-module) — declarative install and service
- [Docker](#docker) — single- and multi-container deploys
- [Running tests](#running-tests)
- [Architecture](#architecture) — backend/frontend layout, state dir
- [Docs](#docs) — the full documentation index
- [Contributors](#contributors)

---

## Why Hermes

Most AI tools reset every session. They don't know who you are, what you worked on, or what
conventions your project follows. You re-explain yourself every time.

Hermes retains context across sessions, runs scheduled jobs while you're offline, and gets
smarter about your environment the longer it runs. It uses your existing Hermes agent setup,
your existing models, and requires no additional configuration to start.

What makes it different from other agentic tools:

- **Persistent memory** — user profile, agent notes, and a skills system that saves reusable
  procedures; Hermes learns your environment and does not have to relearn it
- **Self-hosted scheduling** — cron jobs that fire while you're offline and deliver results to
  Telegram, Discord, Slack, Signal, email, and more
- **10+ messaging platforms** — the same agent available in the terminal is reachable from your phone
- **Self-improving skills** — Hermes writes and saves its own skills automatically from experience;
  no marketplace to browse, no plugins to install
- **Provider-agnostic** — OpenAI, Anthropic, Google, DeepSeek, OpenRouter, and more
- **Orchestrates other agents** — can spawn Claude Code or Codex for heavy coding tasks and bring
  the results back into its own memory
- **Self-hosted** — your conversations, your memory, your hardware

**vs. the field** *(landscape is actively shifting — see [docs/why-hermes.md](docs/why-hermes.md) for the full breakdown)*:

| | OpenClaw | Claude Code | Codex CLI | OpenCode | Hermes |
|---|---|---|---|---|---|
| Persistent memory (auto) | Yes | Partial† | Partial | Partial | Yes |
| Scheduled jobs (self-hosted) | Yes | No‡ | No | No | Yes |
| Messaging app access | Yes (15+ platforms) | Partial (Telegram/Discord preview) | No | No | Yes (10+) |
| Web UI (self-hosted) | Dashboard only | No | No | Yes | Yes |
| Self-improving skills | Partial | No | No | No | Yes |
| Python / ML ecosystem | No (Node.js) | No | No | No | Yes |
| Provider-agnostic | Yes | No (Claude only) | Yes | Yes | Yes |
| Open source | Yes (MIT) | No | Yes | Yes | Yes |

† Claude Code has CLAUDE.md / MEMORY.md project context and rolling auto-memory, but not full automatic cross-session recall  
‡ Claude Code has cloud-managed scheduling (Anthropic infrastructure) and session-scoped `/loop`; no self-hosted cron

**The closest competitor is OpenClaw** — both are always-on, self-hosted, open-source agents
with memory, cron, and messaging. The key differences: Hermes writes and saves its own skills
automatically as a core behavior (OpenClaw's skill system centers on a community marketplace);
Hermes is more stable across updates (OpenClaw has documented release regressions and ClawHub
has had security incidents involving malicious skills); and Hermes runs natively in the Python
ecosystem. See [docs/why-hermes.md](docs/why-hermes.md) for the full side-by-side.

---

## Quick start

Run the repo bootstrap:

```bash
git clone https://github.com/nesquena/hermes-webui.git hermes-webui
cd hermes-webui
python3 bootstrap.py
```

Or keep using the shell launcher:

```bash
./start.sh
```

For self-hosted VM or homelab installs, `ctl.sh` wraps the common daemon lifecycle commands without requiring `fuser` or `pkill`:

```bash
./ctl.sh start              # background daemon, PID at ~/.hermes/webui.pid
./ctl.sh status             # PID, uptime, bound host/port, log path, /health
./ctl.sh logs --lines 100   # tail ~/.hermes/webui.log
./ctl.sh restart
./ctl.sh stop
```

`ctl.sh start` runs the bootstrap in foreground/no-browser mode behind the daemon wrapper, writes logs to `~/.hermes/webui.log`, and respects `.env` plus inline overrides such as `HERMES_WEBUI_HOST=0.0.0.0 ./ctl.sh start`.

> **Stopping the server.** Each launch method has its own stop path because only `ctl.sh start` writes a PID file (`~/.hermes/webui.pid`):
>
> | Launch method | How to stop |
> |---|---|
> | `python3 bootstrap.py` | **Ctrl-C** in the terminal (runs in the foreground) |
> | `./ctl.sh start` | `./ctl.sh stop` (sends SIGTERM, waits, then SIGKILL) |
> | Detached `bootstrap.py` (no `--foreground`) or `./start.sh` | Find the PID via `lsof -i :8787` (or `ss -tlnp`) and `kill` it |
>
> `./ctl.sh stop` cannot stop a server launched by `bootstrap.py` or `start.sh` directly — it only manages processes it started itself.

> **How chat runs by default.** WebUI runs the Hermes agent in-process, reading
> your `HERMES_HOME` config directly. It does not connect to an external
> Hermes/agent OpenAI-compatible API server to run chat. `HERMES_API_URL` is only
> read by the Tasks/cron health probe and does not route chat.
>
> Two options if you run an external endpoint:
>
> 1. **Use its models as a chat provider** (supported today): add it in
>    **Settings → Providers** as a custom OpenAI-compatible provider with
>    `base_url = http://127.0.0.1:8642/v1` and your bearer token.
> 2. **Route chat through a Hermes Gateway API server** (supported today via
>    `HERMES_WEBUI_CHAT_BACKEND=gateway`): see [`docs/advanced-chat-setup.md`](docs/advanced-chat-setup.md).
>    Full agent-loop delegation is not yet shipped; tracked in [#1925](https://github.com/nesquena/hermes-webui/issues/1925).

### Advanced: dynamic recall prefill & Gateway-backed chat

Two optional, self-hosted-deployment features — attaching dynamic **session-recall prefill** to browser turns (Joplin/Obsidian/Notion/llm-wiki routers), and routing browser chat through a running **Hermes Gateway** — are documented in [`docs/advanced-chat-setup.md`](docs/advanced-chat-setup.md). Most users need neither.

The bootstrap will:

1. Detect Hermes Agent and, if missing, attempt the official installer (`curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash`).
2. Find or create a Python environment with the WebUI dependencies.
3. Start the web server and wait for `/health`.
4. Open the browser unless you pass `--no-browser`.
5. Drop you into a first-run onboarding wizard inside the WebUI.

> Native Windows is not supported for this bootstrap yet. Use Linux, macOS, or WSL2.
> For Windows / WSL auto-start at login, see [`docs/wsl-autostart.md`](docs/wsl-autostart.md).

A community-maintained native Windows setup is documented at [@markwang2658/hermes-windows-native-guide](https://github.com/markwang2658/hermes-windows-native-guide) (companion setup repo: [@markwang2658/hermes-windows-native](https://github.com/markwang2658/hermes-windows-native)). Notes from the community report in [#1952](https://github.com/nesquena/hermes-webui/issues/1952):

- **Memory:** community-measured ~330 MB native vs ~1080 MB with WSL2+Docker (varies by configuration).
- **What works:** chat, workspace browser, session management, all themes.
- **Known limitations:** some POSIX-style file paths surface in the workspace browser; bash-assuming agent tools may not work natively.
- **Native Windows setup:** install Python 3.11+, then from the hermes-agent root in PowerShell: `python -m venv venv` → `pip install -r requirements.txt` → `pwsh .\start.ps1` (it auto-discovers `venv\Scripts\python.exe`).
- **WSL2 relationship:** not a prerequisite — a WSL2-built venv (`venv/bin/python`, ELF) isn't invokable by native Windows Python, so use the native setup above. WSL2 stays useful as a parallel install if you want the full `bootstrap.py` + Linux runtime.

If provider setup is still incomplete after install, the onboarding wizard will point you to finish it with `hermes model` instead of trying to replicate the full CLI setup in-browser.
For a step-by-step walkthrough of the wizard, provider choices, local model server Base URLs, and safe re-runs, see [`docs/onboarding.md`](docs/onboarding.md).
If an AI assistant is helping with install, reinstall, bootstrap, provider setup, or first-run support, have it read [`docs/onboarding-agent-checklist.md`](docs/onboarding-agent-checklist.md) before running commands or inspecting logs.

---

## Features

### Chat and agent
- Streaming responses via SSE (tokens appear as they are generated)
- Multi-provider model support -- any Hermes API provider (OpenAI, Anthropic, Google, DeepSeek, Nous Portal, OpenRouter, MiniMax, Xiaomi MiMo, Z.AI); dynamic model dropdown populated from configured keys
- Send a message while one is processing -- it queues automatically
- Edit any past user message inline and regenerate from that point
- Retry the last assistant response with one click
- Cancel a running task directly from the composer footer (Stop button next to Send)
- Tool call cards inline -- each shows the tool name, args, and result snippet; expand/collapse all toggle for multi-tool turns
- Subagent delegation cards -- child agent activity shown with distinct icon and indented border
- Mermaid diagram rendering inline (flowcharts, sequence diagrams, gantt charts)
- Thinking/reasoning display -- collapsible gold-themed cards for Claude extended thinking and o3 reasoning blocks
- Approval card for dangerous shell commands (allow once / session / always / deny)
- SSE auto-reconnect on network blips (SSH tunnel resilience)
- File attachments persist across page reloads and are stored outside the active workspace by default (`~/.hermes/webui/attachments/<session_id>/`, or `HERMES_WEBUI_ATTACHMENT_DIR/<session_id>/` when configured)
- Message timestamps (HH:MM next to each message, full date on hover)
- Code block copy button with "Copied!" feedback
- Syntax highlighting via Prism.js (Python, JS, bash, JSON, SQL, and more)
- Safe HTML rendering in AI responses (bold, italic, code converted to markdown)
- rAF-throttled token streaming for smoother rendering during long responses
- Context usage indicator in composer footer -- token count, cost, and fill bar (model-aware)

### Sessions
- Create, rename, duplicate, delete, search by title and message content
- Session actions via `⋯` dropdown per session — pin, move to project, archive, duplicate, delete
- Pin/star sessions to the top of the sidebar (gold indicator)
- Archive sessions (hide without deleting, toggle to show)
- Session projects -- named groups with colors for organizing sessions
- Session tags -- add #tag to titles for colored chips and click-to-filter
- Grouped by Today / Yesterday / Earlier in the sidebar (collapsible date groups)
- Download as Markdown transcript, full JSON export, or import from JSON
- Create a public read-only share link for the active conversation from the Control Center; shared pages show a sanitized transcript snapshot without workspace, profile, or live controls
- Sessions persist across page reloads and SSH tunnel reconnects
- Browser tab title reflects the active session name
- CLI session bridge -- CLI sessions from hermes-agent's SQLite store appear in the sidebar with a gold "cli" badge; click to import with full history and reply normally
- Token/cost display -- input tokens, output tokens, estimated cost shown per conversation (toggle in Settings or `/usage` command)

### Workspace file browser
- Directory tree with expand/collapse (single-click toggles, double-click navigates)
- Breadcrumb navigation with clickable path segments
- Preview text, code, Markdown (rendered), and images inline
- Chat links using `workspace://path/to/file` open files in the right-side preview pane
- Edit, create, delete, and rename files; create folders
- Binary file download (auto-detected from server)
- File preview auto-closes on directory navigation (with unsaved-edit guard)
- Git detection -- branch name and dirty file count badge in workspace header
- Right panel is drag-resizable
- Syntax highlighted code preview (Prism.js)

### Voice input
- Microphone button in the composer (Web Speech API)
- Tap to record, tap again or send to stop
- Live interim transcription appears in the textarea
- Auto-stops after ~2s of silence
- Appends to existing textarea content (doesn't replace)
- Hidden when browser doesn't support Web Speech API (Chrome, Edge, Safari)

### Profiles
- Profile chip in the **composer footer** -- dropdown showing all profiles with gateway status and model info
- Gateway status dots (green = running), model info, skill count per profile
- Profiles management panel -- create, switch, and delete profiles from the sidebar
- Clone config from active profile on create
- Optional custom endpoint fields on create -- Base URL and API key written into the profile's `config.yaml` at creation time, so Ollama, LMStudio, and other local endpoints can be configured without editing files manually
- Seamless switching -- no server restart; reloads config, skills, memory, cron, models
- Per-session profile tracking (records which profile was active at creation)

### Authentication and security
- Optional password auth -- off by default, zero friction for localhost
- Enable via `HERMES_WEBUI_PASSWORD` env var or Settings panel
- Installed PWAs work best with WebUI's own password. Reverse proxies are supported, but proxy basic auth can block the service-worker update fetches an installed app needs and leave it on a blank screen after an update; see `docs/troubleshooting.md` for recovery steps.
- Optional passkeys/WebAuthn -- register from Settings -> System after signing in with a password; the login page only shows passkey sign-in after at least one passkey exists
- After registering at least one passkey, Settings -> System can remove the password and keep passkey-only sign-in enabled. Password auth remains the bootstrap/recovery path until you choose to go passwordless; passkeys are same-origin and stored locally in the WebUI state directory
- Optional native OIDC login for WebUI sessions -- configure `webui_oidc.issuer`, `client_id`, `allow_claim`, and `allow_values` in `config.yaml`, or set the matching `HERMES_WEBUI_OIDC_*` environment variables. OIDC stays disabled until all four are present, and startup prints a warning if the config is partial.
- Native OIDC stores the PKCE/state nonce flow in process memory. That works for the shipped single-process server, and it also works behind a load balancer when callbacks stay sticky to the same WebUI instance. Multi-instance deployments need session affinity, or the callback can land on a different process and fail state validation.
- Signed HMAC HTTP-only cookie with 24h TTL
- Minimal dark-themed login page at `/login`
- Security headers on all responses (X-Content-Type-Options, X-Frame-Options, Referrer-Policy)
- 20MB POST body size limit
- CDN resources pinned with SRI integrity hashes

### Themes
- Appearance is split into two axes: Theme (`system`, `dark`, `light`) and Skin
  (`default`, `ares`, `mono`, `slate`, `poseidon`, `sisyphus`, `charizard`,
  `sienna`, `catppuccin`, `nous`, `geist-contrast` / Geist Contrast)
- Switch via Settings -> Appearance (instant live preview) or `/theme <theme-or-skin>`
- Tint the browser and installed-app icon in Settings -> Appearance to distinguish multiple Hermes agents
- Persists across reloads (server-side in settings.json + localStorage for flicker-free loading)
- Skins use `data-skin` plus CSS variables; dark mode resolves through the
  `.dark` class, not a `data-theme` custom-theme axis — see [THEMES.md](THEMES.md)

### Settings and configuration
- **Hermes Control Center** (sidebar launcher button) -- Conversation tab (export/import/clear), Preferences tab (model, send key, theme, language, all toggles), System tab (version, password)
- Send key: Enter (default) or Ctrl/Cmd+Enter
- Show/hide CLI sessions toggle (enabled by default)
- Token usage display toggle (off by default, also via `/usage` command)
- Control Center always opens on the Conversation tab; resets on close
- Unsaved changes guard -- discard/save prompt when closing with unpersisted changes
- Cron completion alerts -- toast notifications and unread badges scoped to the active profile on the Tasks tab and session sidebar
- Background agent error alerts -- banner when a non-active session encounters an error

### Slash commands
- Type `/` in the composer for autocomplete dropdown
- Built-in: `/help`, `/clear`, `/compress [focus topic]`, `/compact` (alias), `/model <name>`, `/workspace <name>`, `/new`, `/usage`, `/theme`
- Arrow keys navigate, Tab/Enter select, Escape closes
- Unrecognized commands pass through to the agent

### Panels
- **Chat** -- session list, search, pin, archive, projects, new conversation
- **Tasks** -- view, create, edit, run, pause/resume, delete cron jobs; run history; completion alerts
- **Skills** -- list all skills by category, search, preview, create/edit/delete; linked files viewer
- **Memory** -- view and edit MEMORY.md and USER.md inline
- **Profiles** -- create, switch, delete agent profiles; clone config
- **Todos** -- live task list from the current session
- **Spaces** -- add, rename, remove workspaces; quick-switch from topbar

### Mobile responsive
- Hamburger sidebar -- slide-in overlay on mobile (<640px)
- Sidebar top tabs stay available on mobile; no fixed bottom nav stealing chat height
- Files slide-over panel from right edge
- Touch targets minimum 44px on all interactive elements
- Full-height chat/composer on phones without bottom-nav spacing
- Desktop layout completely unchanged

---

## Configuration & access

`start.sh` auto-detects almost everything; the subsections below cover the knobs for when it can't, and how to reach the UI remotely.

### What start.sh discovers automatically

| Thing | How it finds it |
|---|---|
| Hermes agent dir | `HERMES_WEBUI_AGENT_DIR`, then known checkout paths, the `hermes` launcher on `PATH`, and finally the installed `run_agent` module exposed by `HERMES_WEBUI_PYTHON` |
| Python executable | Agent venv first, then `.venv` in this repo, then system `python3` |
| State directory | `HERMES_WEBUI_STATE_DIR` env, then `$HERMES_HOME/webui` (Windows default `%LOCALAPPDATA%\hermes\webui`, POSIX default `~/.hermes/webui`) |
| Default workspace | `HERMES_WEBUI_DEFAULT_WORKSPACE` env, then `~/workspace`, then state dir |
| Port | `HERMES_WEBUI_PORT` env or first argument, default `8787` |

If discovery finds everything, nothing else is required.

---

### Overrides (only needed if auto-detection misses)

```bash
export HERMES_WEBUI_AGENT_DIR=/path/to/hermes-agent
export HERMES_WEBUI_PYTHON=/path/to/python
export HERMES_WEBUI_PORT=9000
export HERMES_WEBUI_AUTO_INSTALL=1  # enable auto-install of agent deps (disabled by default)
./start.sh
```

Or inline:

```bash
HERMES_WEBUI_AGENT_DIR=/custom/path ./start.sh 9000
```

Full list of environment variables:

| Variable | Default | Description |
|---|---|---|
| `HERMES_WEBUI_AGENT_DIR` | auto-discovered | Path to the Hermes Agent source or installed module root |
| `HERMES_WEBUI_PYTHON` | auto-discovered | Python executable |
| `HERMES_WEBUI_HOST` | `127.0.0.1` | Bind address (`0.0.0.0` for all IPv4, `::` for all IPv6, `::1` for IPv6 loopback) |
| `HERMES_WEBUI_PORT` | `8787` | Port |
| `HERMES_WEBUI_STATE_DIR` | `$HERMES_HOME/webui` (Windows default `%LOCALAPPDATA%\hermes\webui`, POSIX default `~/.hermes/webui`) | Where sessions and state are stored. **Note (upgrade):** the default now follows `HERMES_HOME` — if you previously relocated `HERMES_HOME` to a non-default base **without** setting `HERMES_WEBUI_STATE_DIR`, your WebUI state now resolves to `$HERMES_HOME/webui` instead of the old platform-default `~/.hermes/webui`. To keep using the old location, set `HERMES_WEBUI_STATE_DIR` to it (or move the directory). Installs with `HERMES_HOME` unset or at the default base are unaffected. |
| `HERMES_WEBUI_DEFAULT_WORKSPACE` | `~/workspace` | Default workspace |
| `HERMES_WEBUI_DEFAULT_MODEL` | *(provider default)* | Optional model override; leave unset to use the active Hermes provider default |
| `HERMES_WEBUI_PASSWORD` | *(unset)* | Set to enable password authentication |
| `HERMES_WEBUI_CSP_CONNECT_EXTRA` | *(unset)* | Optional space-separated `http(s)://` or `ws(s)://` origins to append to the enforced and report-only CSP `connect-src` directives for trusted reverse-proxy, tunnel, or extension sidecar deployments |
| `HERMES_WEBUI_SSE_CHUNKED` | *(unset)* | Set truthy (`1`/`true`/`yes`/`on`) to send SSE with `Transfer-Encoding: chunked`. Needed behind buffering reverse proxies (e.g. `jupyter-server-proxy`) that otherwise buffer the whole stream; harmless but unnecessary for directly-served deployments |
| `HERMES_WEBUI_EXTENSION_DIR` | *(unset)* | Optional local directory served at `/extensions/`; must point to an existing directory before extension injection is enabled |
| `HERMES_WEBUI_EXTENSION_MANIFEST` | *(unset)* | Optional relative JSON manifest inside `HERMES_WEBUI_EXTENSION_DIR` listing bundled scripts/styles to inject; see [WebUI Extensions](docs/EXTENSIONS.md) |
| `HERMES_WEBUI_EXTENSION_SCRIPT_URLS` | *(unset)* | Optional comma-separated same-origin script URLs to inject; appended after manifest scripts; see [WebUI Extensions](docs/EXTENSIONS.md) |
| `HERMES_WEBUI_EXTENSION_STYLESHEET_URLS` | *(unset)* | Optional comma-separated same-origin stylesheet URLs to inject; appended after manifest stylesheets; see [WebUI Extensions](docs/EXTENSIONS.md) |
| `HERMES_HOME` | Windows: `%LOCALAPPDATA%\hermes`; POSIX: `~/.hermes` | Base directory for Hermes state (affects all paths) |
| `HERMES_CONFIG_PATH` | `$HERMES_HOME/config.yaml` | Path to Hermes config file |
| `HERMES_WEBUI_SERVER_CWD` | *(unset)* | Working directory for the server process. Defaults to the agent dir; point it at a writable workspace when the agent dir is read-only so fallback relative writes land somewhere writable |
| `HERMES_WEBUI_AGENT_CACHE_MAX` | `25` | Max live agent instances kept warm in the in-memory LRU. Each pins a full conversation transcript, so this is the dominant lever on resident memory — lower it on installs with many long sessions to cap RAM (at the cost of more cold reloads) |
| `HERMES_WEBUI_SESSIONS_MAX` | `300` | Legacy operator override for the max compact `Session` objects held in the in-memory LRU. Prefer the `webui.sessions_cache_max` key in `config.yaml` (which takes precedence); this env var remains a fallback. Bounds resident memory so long-running installs cannot accumulate every session ever touched and eventually crash (#4765/#2233/#4633). Eviction only ever drops clean, persisted, non-active sessions — an evicted session lazily reloads from its JSON sidecar on next access |

Extension deployments can inspect sanitized, authenticated diagnostics at `GET /api/extensions/status`; see [WebUI Extensions](docs/EXTENSIONS.md#diagnostics).

---

### Nix flake and NixOS module

Hermes WebUI has a Nix flake package and a NixOS service module so you can run it declaratively.

Install the latest package with:

```bash
nix shell github:nesquena/hermes-webui#default
```

Use this flake input in your system configuration:

```nix
inputs.hermes-webui.url = "github:nesquena/hermes-webui";
```

Then add the module and configure it in `nixosModules`:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    hermes-agent.url = "github:NousResearch/hermes-agent";
    hermes-webui.url = "github:nesquena/hermes-webui";
  };

  outputs = { self, nixpkgs, hermes-agent, hermes-webui, ... }: {
    nixosConfigurations.<host> = nixpkgs.lib.nixosSystem {
      modules = [
        hermes-agent.nixosModules.default
        hermes-webui.nixosModules.default
        ({ pkgs, ... }: {
          services.hermes-agent.enable = true;
          services.hermes-webui = {
            enable = true;
            host = "127.0.0.1";
            port = 8787;
            stateDir = "/var/lib/hermes-webui";
            user = "hermes";
            group = "hermes";
            hermesHome = "/var/lib/hermes/.hermes";
            agent.package = hermes-agent.packages.${pkgs.stdenv.hostPlatform.system}.default;
            environmentFiles = [ "/run/secrets/hermes-webui.env" ];
          };
        })
      ];
    };
  };
}
```

The module defaults to `127.0.0.1`. Set `host = "0.0.0.0"` and `openFirewall = true` only when you want direct network access, and pair that with auth, for example `HERMES_WEBUI_PASSWORD` via `environmentFiles`.

The published Hermes Agent package exposes `passthru.hermesVenv`, so the module derives `HERMES_WEBUI_PYTHON` from its interpreter. Bootstrap then locates the installed `run_agent.py` through that interpreter without importing Agent code and exports its parent as `HERMES_WEBUI_AGENT_DIR`. Packages may expose `passthru.hermesAgentDir` as a direct path instead. Use `agent.dir` and `agent.python` as explicit overrides for custom package layouts.

When WebUI reads shared Hermes Agent state, run the service as a user that can already read that state. For a co-located Hermes Agent service, set `user` and `group` to the agent service account; the module only creates the default `hermes-webui` account and never changes ownership of an existing `hermesHome`.

The module maps directly onto existing WebUI environment variables, including:
`HERMES_WEBUI_HOST`, `HERMES_WEBUI_PORT`, `HERMES_WEBUI_STATE_DIR`, `HERMES_HOME`, `HERMES_WEBUI_AGENT_DIR`, and `HERMES_WEBUI_PYTHON`.

Set `environmentFiles` for secrets like API keys. Protected WebUI runtime keys from the module are rejected there, so keep host, port, state, and agent wiring in the module options. Keep reverse proxy and TLS configuration in your surrounding deployment module because those details are deployment-specific.

### Remote access (SSH tunnel, Tailscale, phone)

The server binds to `127.0.0.1` by default. To reach it from another machine use an SSH tunnel (`ssh -N -L 8787:127.0.0.1:8787 user@host`, which `start.sh` prints for you over SSH), or join your server and phone to a [Tailscale](https://tailscale.com) network and browse to `http://<server-tailscale-ip>:8787` with `HERMES_WEBUI_HOST=0.0.0.0` + `HERMES_WEBUI_PASSWORD` set. Full walkthrough (incl. a community ARM64-Android field report): [`docs/remote-access.md`](docs/remote-access.md).

### Manual launch (without start.sh)

If you prefer to launch the server directly:

```bash
cd /path/to/hermes-agent          # or wherever sys.path can find Hermes modules
HERMES_WEBUI_PORT=8787 venv/bin/python /path/to/hermes-webui/server.py
```

Note: use the agent venv Python (or any Python environment that has the Hermes agent dependencies installed). System Python will be missing `openai`, `httpx`, and other required packages.

Health check:

```bash
curl http://127.0.0.1:8787/health
```

---

## Docker

**Pre-built images** (amd64 + arm64) are published to GHCR on every release.

For a comprehensive setup guide covering all 3 compose files, common failure modes, and bind-mount migration, see [`docs/docker.md`](docs/docker.md). The README covers the 5-minute happy path.

### 5-minute quickstart (single container)

The simplest setup: one WebUI container that runs the agent in-process.

```bash
git clone https://github.com/nesquena/hermes-webui
cd hermes-webui
cp .env.docker.example .env
# Edit .env if your host UID isn't 1000 (e.g. macOS where UIDs start at 501)
docker compose up -d
# Open http://localhost:8787
```

Run Compose as the user who owns your Hermes home. `sudo docker compose up -d` can make `${HOME}` expand to the root user's home, so Docker mounts the wrong `.hermes` directory instead of your real `~/.hermes` and the WebUI starts with `config.yaml (not found, using defaults)`. Prefer adding your user to the Docker group and running `docker compose up -d`; if you must use sudo, set absolute paths first, for example `HERMES_HOME=/home/you/.hermes HERMES_WORKSPACE=/home/you/workspace sudo -E docker compose up -d`, then verify with `docker compose config`.

The container auto-detects your UID/GID from the mounted `~/.hermes` volume so files written by the agent stay readable by you on the host.

To enable password protection (required if you expose the port outside `127.0.0.1`):

```bash
echo "HERMES_WEBUI_PASSWORD=change-me-to-something-strong" >> .env
docker compose up -d --force-recreate
```

### Manual `docker run` (no compose)

```bash
docker pull ghcr.io/nesquena/hermes-webui:latest
docker run -d \
  -e WANTED_UID=$(id -u) -e WANTED_GID=$(id -g) \
  -v ~/.hermes:/home/hermeswebui/.hermes \
  -e HERMES_WEBUI_STATE_DIR=/home/hermeswebui/.hermes/webui \
  -v ~/workspace:/workspace \
  -p 127.0.0.1:8787:8787 \
  ghcr.io/nesquena/hermes-webui:latest
```

### Build locally

```bash
docker build -t hermes-webui .
docker run -d \
  -e WANTED_UID=$(id -u) -e WANTED_GID=$(id -g) \
  -v ~/.hermes:/home/hermeswebui/.hermes \
  -e HERMES_WEBUI_STATE_DIR=/home/hermeswebui/.hermes/webui \
  -v ~/workspace:/workspace \
  -p 127.0.0.1:8787:8787 \
  hermes-webui
```

### Multi-container setups

If you want the agent and WebUI in separate containers (for isolation, or because you're already running an agent gateway elsewhere):

```bash
# Agent + WebUI
docker compose -f docker-compose.two-container.yml up -d

# Agent + Dashboard + WebUI
docker compose -f docker-compose.three-container.yml up -d
```

Both compose files use **named Docker volumes** by default, which solves the UID/GID problem by construction. If you need bind mounts to share an existing host directory, see [`docs/docker.md`](docs/docker.md) for the full migration recipe.

> **Known limitation (#681)**: in the two-container setup, tools triggered from the WebUI run in the **WebUI container**, not the agent container. If you need git/node/etc. on the WebUI's filesystem, either use the single-container setup, extend the WebUI Dockerfile, or use the community [all-in-one image](https://github.com/sunnysktsang/hermes-suite).
>
> **Source boundary note (#2453)**: the multi-container setup mounts `hermes-agent-src` read-only into the WebUI by default. This prevents WebUI-side source rewrites but is still an implementation-coupling bridge, not a stable Agent API boundary. See [`docs/rfcs/agent-source-boundary.md`](docs/rfcs/agent-source-boundary.md) for the current source/API decoupling inventory.

### Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `PermissionError` at startup | UID mismatch on bind mount | Set `UID=$(id -u)` in `.env` |
| `.env: permission denied` (#1389) | `fix_credential_permissions()` enforced 0600 | Set `HERMES_SKIP_CHMOD=1` in `.env` |
| Workspace appears empty | UID mismatch on `/workspace` mount | Set `UID=$(id -u)` in `.env` |
| `git: command not found` in chat | Two-container architectural limit (#681) | Use single-container or extend Dockerfile |
| WebUI can't find agent source | `hermes-agent-src` volume misconfigured | Use the named volumes from compose files as-is |
| Podman shared `.hermes` fails | Podman 3.4 `keep-id` limitation | Use Podman 4+ or single-container |
| Host API at `localhost` fails from WebUI | Container `localhost` means the container, not your host (#3012) | Use `http://host.docker.internal:<port>` on Docker Desktop, or `http://host.containers.internal:<port>` on Podman |
| WebUI can't see `~/.hermes` after `sudo docker compose` | `${HOME}` expanded to the root user's home (#3006) | Run Compose as your user, or pass absolute `HERMES_HOME`/`HERMES_WORKSPACE` with `sudo -E` |

For the deep dive on each of these, see [`docs/docker.md`](docs/docker.md).

> **Note:** By default, Docker Compose binds to `127.0.0.1` (localhost only).
> To expose on a network, change the port to `"8787:8787"` in `docker-compose.yml`
> and set `HERMES_WEBUI_PASSWORD` to enable authentication.

---

## Running tests

Tests discover the repo and the Hermes agent dynamically -- no hardcoded paths.
Use the repo test runner so local runs do not accidentally use an unsupported
system Python. It creates/uses `.venv` with Python 3.11, 3.12, or 3.13 and
installs the dev test dependencies from `requirements-dev.txt` when missing.

```bash
cd hermes-webui
./scripts/test.sh
```

Pass normal pytest arguments after the script for focused runs:

```bash
./scripts/test.sh tests/test_regressions.py -v
```

Or seed the repo `.venv` from an explicit supported base interpreter:

```bash
HERMES_WEBUI_TEST_PYTHON=/path/to/python3.12 ./scripts/test.sh tests/ -v
```

The override selects the Python used to create or rebuild `.venv`; dependencies
are still installed into the repo-local virtual environment, not into the
system/Homebrew interpreter.

Tests run against an isolated server with a separate state directory.
Production data and real cron jobs are never touched. Current snapshot:
**~11,500 tests collected** across **~1,150 test files**, run in CI on Python 3.11,
3.12, and 3.13 (3 parallel shards each).

---

## Architecture

No build step, no framework, no bundler — a Python standard-library HTTP server
and vanilla JS. The backend lives in `api/`, the frontend in `static/`.

**Backend (`api/`)**

```
server.py         HTTP routing shell + auth middleware
api/
  auth.py         Optional password authentication, signed cookies, passkeys
  config.py       Discovery, globals, model detection, reloadable config
  helpers.py      HTTP helpers, security headers
  models.py       Session model + CRUD + CLI/state.db bridge
  onboarding.py   First-run onboarding wizard, OAuth provider support
  profiles.py     Profile state management, hermes_cli wrapper
  routes.py       All GET + POST route handlers (if/elif dispatch, no decorators)
  state_sync.py   /insights sync — message_count to state.db
  streaming.py    SSE engine, run_agent, cancellation, compression
  updates.py      Self-update check and release notes
  upload.py       Multipart parser, file upload handler
  workspace.py    File ops, workspace helpers, git detection
```

**Frontend (`static/`)**

```
index.html        HTML template
style.css         All CSS incl. mobile responsive, themes + skins
ui.js             DOM helpers, renderMd, tool cards, context indicator
workspace.js      File preview, file ops, git badge, central api() fetch wrapper
sessions.js       Session CRUD, collapsible groups, search, reload recovery
messages.js       send(), SSE handlers, live streaming, session recovery
panels.js         Cron, skills, memory, profiles, settings (Control Center)
commands.js       Slash command autocomplete
boot.js           Mobile nav, voice input, theme/skin boot, bfcache handler
```

**Tests + packaging**

```
tests/            Pytest suite (~11,500 tests; isolated server/state fixtures)
pyproject.toml    Tooling config (ruff lint gate) — not a packaged distribution
Dockerfile        python:3.12-slim container image
docker-compose.yml  Compose with named volume and optional auth
.github/workflows/  CI: ruff + sharded pytest, browser smoke, Docker smoke,
                    multi-arch Docker build + GitHub Release on tag
```

State lives outside the repo at `~/.hermes/webui/` by default
(sessions, workspaces, settings, projects, last_workspace). Override with `HERMES_WEBUI_STATE_DIR`.
Full design notes and the endpoint catalog are in [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Compatibility

The version shown in the WebUI runtime status is the **WebUI version only** (build/image/tag currently running). It is not a full compatibility map.

The WebUI is still coupled to Hermes Agent internals for runtime execution, provider/model access, and state/schema usage until the stable agent boundary work in [#1925](https://github.com/nesquena/hermes-webui/issues/1925) and [#2491](https://github.com/nesquena/hermes-webui/issues/2491) land. In practice, the WebUI imports Agent modules directly (`api/config.py`, `api/providers.py`, `api/streaming.py`) and reads Agent state layout directly, so version skew can cause import or behavior drift.

**Compatibility policy**
- WebUI release branches are tested against the matching Hermes Agent release available at that WebUI release time.
- **Upgrade both together**: upgrade or pin WebUI and hermes-agent together (same release train/version/date), especially before enabling production traffic.
- Running pinned older/newer combinations is **untested and unsupported** until the stable API boundary work in [#1925](https://github.com/nesquena/hermes-webui/issues/1925) / [#2491](https://github.com/nesquena/hermes-webui/issues/2491) is in place.
- Record the full `hermes-agent` + `hermes-webui` versions in issue reports when upgrade mismatches are suspected.

**Docker users**: pin both image tags (or corresponding pinned source revisions) rather than using `latest` on one side and a fixed tag on the other. When upgrading the multi-container setup, follow the agent-image upgrade procedure in [`docs/docker.md`](docs/docker.md) (which requires dropping the `hermes-agent-src` volume before recreating). The current source-boundary status is tracked in [`docs/rfcs/agent-source-boundary.md`](docs/rfcs/agent-source-boundary.md).

---

## Docs

**Start here**
- [`docs/why-hermes.md`](docs/why-hermes.md) — why Hermes, the mental model, and a detailed comparison to Claude Code / Codex / OpenCode / Cursor
- [`docs/onboarding.md`](docs/onboarding.md) — first-run wizard, provider setup, local model server Base URLs, and safe re-runs
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — diagnostic flows for common failures (e.g. "AIAgent not available")

**Using & customizing**
- [`THEMES.md`](THEMES.md) — theme + skin system, custom theme guide
- [`docs/workspace-git.md`](docs/workspace-git.md) — the workspace Git controls
- [`docs/EXTENSIONS.md`](docs/EXTENSIONS.md) — administrator-controlled WebUI extension injection

**Deploying & operating**
- [`docs/remote-access.md`](docs/remote-access.md) — SSH tunnel, Tailscale, and phone access (incl. a community ARM64-Android field report)
- [`docs/advanced-chat-setup.md`](docs/advanced-chat-setup.md) — optional dynamic recall-prefill and Gateway-backed browser chat for self-hosted deployments
- [`docs/docker.md`](docs/docker.md) — Docker compose setup, common failures, and bind-mount migration
- [`docs/supervisor.md`](docs/supervisor.md) — launchd, systemd, supervisord, runit, and s6 process-supervisor setup
- [`docs/wsl-autostart.md`](docs/wsl-autostart.md) — WSL2 auto-start at Windows login
- [`docs/onboarding-agent-checklist.md`](docs/onboarding-agent-checklist.md) — safety rules and pass/fail checks for assistant-led install/reinstall support

**Contributing & design**
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — contribution style, PR expectations, and local verification
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — system design, all API endpoints, implementation notes
- [`TESTING.md`](TESTING.md) — manual browser test plan and automated coverage reference
- [`DESIGN.md`](DESIGN.md) — design tokens and the calm-console direction
- [`docs/UIUX-GUIDE.md`](docs/UIUX-GUIDE.md) — UI/UX principles sourced from the design docs and visual inventories
- [`docs/CONTRACTS.md`](docs/CONTRACTS.md) — project contract/RFC/design index for contributors and agents
- [`docs/rfcs/README.md`](docs/rfcs/README.md) — RFC index for larger architecture and durability proposals

**Release history & plan**
- [`CHANGELOG.md`](CHANGELOG.md) — release notes per version
- [`ROADMAP.md`](ROADMAP.md) — feature roadmap and sprint history
- [`SPRINTS.md`](SPRINTS.md) — forward sprint plan with CLI + Claude parity targets
- [`CONTRIBUTORS.md`](CONTRIBUTORS.md) — the full community credit roll

---

## Contributors

Hermes WebUI is built with help from the open-source community. Every PR — whether merged directly, absorbed into a batch release, or salvaged from a larger proposal — shapes the project, and we're grateful to everyone who has taken the time to contribute.

<!-- BEGIN GENERATED CONTRIBUTORS -->
Over **326 contributors** have shipped code that landed in a release tag. The full, continuously-updated credit roll — including everyone with one or two PRs and the special-thanks roll for design and architectural work — lives in [`CONTRIBUTORS.md`](CONTRIBUTORS.md). A snapshot of the most prolific contributors:

### Top contributors (by PR count, including absorbed/batch-released work)

| # | Contributor | PRs | First → latest release |
|---|---|---:|---|
| 1 | [@rodboev](https://github.com/rodboev) | 336 | `v0.51.223` → `v0.51.893` |
| 2 | [@franksong2702](https://github.com/franksong2702) | 301 | `v0.49.3` → `v0.51.893` |
| 3 | [@Michaelyklam](https://github.com/Michaelyklam) | 157 | `v0.50.240` → `v0.51.198` |
| 4 | [@ai-ag2026](https://github.com/ai-ag2026) | 121 | `v0.50.279` → `v0.51.835` |
| 5 | [@bergeouss](https://github.com/bergeouss) | 80 | `v0.48.0` → `v0.51.703` |
| 6 | [@AJV20](https://github.com/AJV20) | 57 | `v0.51.93` → `v0.51.346` |
| 7 | [@dso2ng](https://github.com/dso2ng) | 43 | `v0.50.227` → `v0.51.578` |
| 8 | [@starship-s](https://github.com/starship-s) | 28 | `v0.50.123` → `v0.51.763` |
| 9 | [@Sanjays2402](https://github.com/Sanjays2402) | 27 | `v0.50.292` → `v0.51.484` |
| 10 | [@allenliang2022](https://github.com/allenliang2022) | 24 | `v0.51.185` → `v0.51.869` |

See [`CONTRIBUTORS.md`](CONTRIBUTORS.md) for the full ranked list of all 326 contributors — the 3+ PR tables, the 1–2 PR roll, and the special-thanks notes for design and architectural contributions.
<!-- END GENERATED CONTRIBUTORS -->

### Notable contributions

**[@franksong2702](https://github.com/franksong2702)** — Most prolific external contributor (180 PRs, `v0.49.3` → `v0.51.384`)
Across the longest tenure of any external contributor: the session title guard (#301), breadcrumb workspace navigation (#302), embedded workspace terminal (#1099), worktree-backed session creation (#2053), onboarding documentation (#2052), composer footer container queries, streaming-session sidebar exemption (#1327), session sidecar repair, cron output preservation (#1295), profile default workspace persistence, manual `/compress` async start/status endpoints (#2128), worktree status surface (#2109) + guarded remove (#2156) for the lifecycle umbrella #2057, session post-render dedup (#2166), native-WebUI fast path (#2170), tail-window response trim (#2171), stale-stream guard extension (#2158), CSP report collector (#2160), and a long tail of polish across mobile/responsive, the session sidebar, and the workspace state machine.

**[@Michaelyklam](https://github.com/Michaelyklam)** — Most prolific contributor of recent releases (118 PRs, `v0.50.240` → `v0.51.198`)
Production Docker hardening (#1921, drops sudo-capable staging user), profile-scoped skills endpoints (#1903), gateway PID resolution under profile-scoped HERMES_HOME (#1901), profile-aware AIAgent cache (#1898/#1904), backslash LaTeX delimiters (#1848), Codex quota error surfacing (#1770), shell-route HTML 503 (#1836), stale Kanban client recovery (#1828), context auto-compression toast lifetime (#1988), `/goal` command (#1866), Kanban detail-view scrolling (#1916), CLI session tool metadata preservation (#1778), Traditional Chinese kanban locale backfill (#1979), v0.51.51 mobile Insights bucketing/layout (#2120/#2121), Hermes run adapter RFC (#2105 for #1925), fork-from-here absolute index (#2198 for #2184), opencode-go custom-provider overlap routing (#2204 for #1894).

**[@rodboev](https://github.com/rodboev)** — Windows / cross-platform correctness + test reliability (83 PRs, `v0.51.223` → `v0.51.384`)
A broad, sustained sweep with a strong focus on making the project work outside Linux: the `ctl.sh` Windows process-tree-kill fix (#3670), the local Windows full-suite signal/orphan-process work, slash-command autocomplete polish, and a long tail of frontend and infrastructure fixes shipped across dozens of release batches.

**[@bergeouss](https://github.com/bergeouss)** — Provider management UI + Docker hardening (70 PRs, `v0.48.0` → `v0.51.385`)
Provider management UI for adding/editing custom providers from Settings, OAuth provider status detection (#1552), two-container Docker setup, profile isolation hardening (per-profile `.env` secrets), the bulk of what users see when they touch Settings → Providers, Reveal-in-Finder context menu (#1551), gateway status card (#1552), auto-assign session to active project filter (#1550), "What's new?" link in update banner (#1549), OpenRouter free-tier live fetch (#1548), credential pool 401 self-heal (#1553), inline provider chip + group model count in model picker (#1644).

**[@ai-ag2026](https://github.com/ai-ag2026)** — Session recovery + audit infrastructure (75 PRs, `v0.50.279` → `v0.51.367`)
Autonomous-AI contributor (Hermes Agent-driven) focused on durability: `state.db`-backed sidecar reconciliation (#2041), orphan `.json.bak` recovery on startup (#2035), read-only session recovery audit endpoints (#2036, #2040), active run lifecycle in `/health` (#2039), crash-safe turn-journal RFC at `docs/rfcs/turn-journal.md` (#2042), append-only turn-journal helper (#2059), lifecycle events layer (#2062), `Content-Security-Policy-Report-Only` header (#2084), per-cron toast toggle (#2100), fork-session compression lineage isolation (#2014).

**[@dso2ng](https://github.com/dso2ng)** — Session lineage + diagnostics (30 PRs, `v0.50.227` → `v0.51.327`)
`/api/session/lineage-report/<sid>` endpoint for bounded session graph diagnostics (#2012), stale Mermaid render error cleanup (#1337), `session_source="fork"` continuation-chain isolation (#2063), lazy lineage-report fetch on sidebar badge expand (#2130), and a long tail of frontend reliability fixes around session loading.

**[@jasonjcwu](https://github.com/jasonjcwu)** — Composer + transcript polish (16 PRs, `v0.50.227` → `v0.51.132`)
Sidebar collapse via active-rail click (#2054, fuses #1884 + #1924), composer chip lightbox (#1758), title fixes for tool-heavy first turns, silent compress-status during session switch (#2185), concurrent-send loss fix (#2186), in-transcript steer message badges (#2187), and a string of frontend polish fixes.

**[@Jordan-SkyLF](https://github.com/Jordan-SkyLF)** — Live streaming + UX polish (12 PRs, `v0.50.18` → `v0.51.66`)
Original sprint of workspace fallback resolution, live reasoning cards (#366, #367, #394–#397), then a recent burst: manual "Refresh usage" button on the Provider quota card (#2150), cancelled-turn status classification (#2151), Firefox sidebar scroll stabilization (#2200), early provisional session titles (#2202), target-aware "What's new?" update-banner links (#2207), and MCP tools overflow fix in Settings (#2210).

**[@aronprins](https://github.com/aronprins)** — `v0.50.0` UI overhaul (PR #242, plus 9 follow-ups)
The biggest single contribution to the project: a complete UI redesign that moved model/profile/workspace controls into the composer footer, replaced the gear-icon settings panel with the Hermes Control Center (tabbed modal), removed the activity bar in favor of inline composer status, redesigned the session list with a `⋯` action dropdown, and added the workspace panel state machine. Plus chat transcript redesign (#587), sidebar declutter (#584), three-column layout refactor (#899), light/dark theme + accent skins (#627), and shared `confirm()`/`prompt()` dialog replacement (PR #251 extracted from #242).

**[@iRonin](https://github.com/iRonin)** — Security hardening sprint (PRs #196–#204)
Six consecutive, focused security PRs: session memory leak fix (expired token pruning), CSP + Permissions-Policy headers, 30-second slow-client connection timeout, optional HTTPS/TLS support via environment variables, upstream branch tracking fix for self-update, and CLI session support in the file-browser API. The kind of focused, high-quality security work that makes a self-hosted tool trustworthy.

**[@lucasrc](https://github.com/lucasrc)** — Auth-hardening trilogy (PRs #2191, #2192, #2193)
Three coordinated security PRs that all landed in v0.51.57: thread-safe login rate limiter with PBKDF2 key separation, password-hash cache invalidation on Settings save, and the full 64-char HMAC-SHA256 session signature with a backwards-compatible migration bridge. The kind of cleanly-decomposed security work that's reviewable as three independent pieces.

**[@LumenYoung](https://github.com/LumenYoung)** — Streaming hot-path correctness (8 PRs, `v0.51.47` → `v0.51.99`)
The original stale-stream writeback guard (#2136 — the bug class the next two releases extended), gateway-state alive-null classification (#2075), compression-banner anchor alignment (#2182), and context-progress ring auto-refresh on compression complete (#2188). Each PR opened a small surgical fix in one of the most fragile subsystems in the codebase.

**[@dobby-d-elf](https://github.com/dobby-d-elf)** — Frontend reliability + motion polish (15 PRs, `v0.51.38` → `v0.51.161`)
Workspace fallback on deleted directories (#2138), iPhone PWA bottom-scroll fix (#2143), the new "Activity: X tools" composer footer shimmer animation (#2203), and follow-up animation tuning (#2212).

**[@JKJameson](https://github.com/JKJameson)** — Composer + session polish (10 PRs)
Persistent composer draft per session (#1956), and a long tail of polish across the composer and session sidebar.

**[@gabogabucho](https://github.com/gabogabucho)** — Spanish locale + onboarding wizard
Full Spanish (`es`) locale covering all UI strings, plus the one-shot bootstrap onboarding wizard that guides new users through provider setup on first launch.

**[@deboste](https://github.com/deboste)** — Reverse-proxy auth + mobile responsive layout (PRs #3, #4, #5)
Three of the very first community PRs: fixed EventSource/fetch to use URL origin for reverse-proxy setups, corrected model provider routing from config, and added mobile responsive layout with dvh viewport fix. Early foundation work.

**[@indigokarasu](https://github.com/indigokarasu)** — Visual redesign proposal (PR #213)
A CSS-only redesign of the full UI — proper design tokens, an icon rail sidebar replacing the emoji tab strip, consistent form cards, breadcrumb nav, and 7 built-in themes as custom properties. The PR didn't merge as-is but shaped the design language and theme architecture that shipped in v0.50.0.

**[@zenc-cp](https://github.com/zenc-cp)** — Anti-hallucination guard for the ReAct loop (PR #133)
A three-layer approach (ephemeral anti-hallucination prompt, live token filtering, session-history cleanup) that the streaming pipeline still uses.

**[@Hinotoi-agent](https://github.com/Hinotoi-agent)** — Profile + session security (PRs #351, #2048)
Profile `.env` secret isolation fix (PR #351) preventing API key leakage between profiles, and session-import workspace validation (PR #2048) blocking a crafted-JSON file-read against `/`.

**[@Sanjays2402](https://github.com/Sanjays2402)** — Endless-scroll + Start-jump race fix (PR #1949)
A generation-token + mutex pair fixing the v0.51.30 race between endless-scroll prefetch and Start-jump's `_ensureAllMessagesLoaded`. The naive same-flag-check approach (proposed in #1942 and #1962) was a no-op for the post-await race — Sanjays2402's fix was the correct shape.

**[@fxd-jason](https://github.com/fxd-jason)** — Real-time approval + clarify via SSE (PRs #1350, #1355)
Replaced 1.5s HTTP polling with SSE long-connections for both approval and clarify, cutting latency from up to 1.5s to near-instant. Got all the correctness details right (atomic subscribe + snapshot, notify-inside-lock, head-of-queue payload, trailing event re-emission).

**[@happy5318](https://github.com/happy5318)** — Custom provider model dedup (PR #1947)
Fixed the same model from different named custom providers being silently deduplicated in the picker, with Opus catching a race in the original tests that needed augmentation.

**[@NocGeek](https://github.com/NocGeek)** — Streaming scroll + manual cron output persistence (7 PRs)
Streaming scroll viewport stability when tool/queue cards insert (#1360), manual cron-run output and metadata persistence (#1372, split from held #1352).

**[@DavidSchuchert](https://github.com/DavidSchuchert)** — German translation (PR #190)
Complete German locale (`de`) covering all UI strings, settings labels, commands, and system messages — and stress-tested the i18n system, exposing several elements that weren't yet translatable and getting them fixed as part of the same PR.

**[@Bobby9228](https://github.com/Bobby9228)** — Mobile Profiles button (PR #265)
Added the Profiles entry to the mobile navigation flow, making profile switching reachable on phones.

**[@kevin-ho](https://github.com/kevin-ho)** — OLED theme (PR #168)
The 7th built-in theme: pure black backgrounds with warm accents tuned to reduce burn-in risk.

**[@andrewy-wizard](https://github.com/andrewy-wizard)** — Chinese localization (PR #177)
Initial Simplified Chinese (`zh`) locale. One of the first non-English locales.

**[@DelightRun](https://github.com/DelightRun)** — `session_search` fix for WebUI sessions (PR #356)
Tracked down the missing `SessionDB` injection in the streaming path that was silently breaking the tool for every WebUI session.

**[@lawrencel1ng](https://github.com/lawrencel1ng)** — Bandit security fixes (PR #354)
Systematic bandit-scan fixes: URL scheme validation before `urlopen`, MD5 `usedforsecurity=False`, and 40+ bare `except: pass` blocks replaced with proper logging.

**[@shaoxianbilly](https://github.com/shaoxianbilly)** — Unicode filename downloads (PR #378)
Proper `Content-Disposition` with RFC 5987 `filename*=UTF-8''...` encoding so non-ASCII filenames download without crashing.

**[@lx3133584](https://github.com/lx3133584)** — CSRF fix for reverse proxy (PR #360)
A real-world blocker for anyone hosting behind Nginx Proxy Manager or similar on a port other than 80/443.

**[@betamod](https://github.com/betamod)** — Security audit (PR #171)
A comprehensive CSRF / SSRF / XSS / env-race-condition audit that shipped in v0.39.0.

**[@TaraTheStar](https://github.com/TaraTheStar)** — Bot name + thinking blocks + login refactor (PRs #132, #176, #181)
Configurable assistant display name, thinking/reasoning block display, and a login page refactor.

---

## Repo

```
git@github.com:nesquena/hermes-webui.git
```
