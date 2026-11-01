# Running Hermes Web UI under a process supervisor

Use a process supervisor (launchd, systemd, supervisord, runit, s6) when you
want the Web UI to start at boot, restart on crash, or be managed alongside
other services.

## TL;DR

Pass ``--foreground`` to ``bootstrap.py`` (or ``bash start.sh``):

```bash
bash start.sh --foreground
```

Or set ``HERMES_WEBUI_FOREGROUND=1`` in the environment. The Web UI will
auto-detect launchd / systemd / supervisord even without the flag, but being
explicit is safer.

**Important (launchd on macOS):** if the ``com.parantoux.hermes-webui`` LaunchAgent is enabled, treat launchd as the single source of truth for WebUI lifecycle. Do **not** also run ``./ctl.sh start``, ``bash start.sh``, ``python bootstrap.py``, or ``python server.py`` against the same state dir/port, or you can create a second WebUI instance and trigger port-8787 restart churn.

## Why ``--foreground`` matters

Without it, ``bootstrap.py`` does this:

1. Spawn ``server.py`` as a detached subprocess (``start_new_session=True``)
2. Probe ``/health`` until the server is up
3. Exit 0

That works for an interactive shell run (``./start.sh`` returns to your
prompt with the server alive in the background). It is **broken** under any
process supervisor: the supervisor sees its tracked PID exit, marks the job
as completed, and respawns ``bootstrap.py``. The respawn fails to bind port
8787 (the orphaned server still has it), exits non-zero, supervisor
respawns again — loop.

In foreground mode, ``bootstrap.py`` does its setup work and then calls
``os.execv`` to replace its own process with ``server.py``. The supervisor
sees the long-lived server as the original child. ``KeepAlive=true`` /
``Restart=always`` work correctly.

## Stopping: the bounded run drain

A stop signal does not cut agent runs off mid-turn. The first ``SIGTERM`` (or
``SIGINT``) the server receives starts **one bounded drain**:

1. Run admission closes. A new chat turn gets HTTP ``503`` with
   ``"code": "server_draining"`` and ``"retryable": true``, and the message
   tells the user to send it again once the server is back. Runs that are
   already going are not touched.
2. Active WebUI agent runs may finish, for up to
   ``HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS`` (default **90** seconds).
3. Any run still active at the deadline is settled: it is stopped and its
   session is saved with a durable "interrupted" marker (plus any partial
   output), so the conversation shows what happened instead of a turn that
   silently vanished. Settling has its own short bound (a few seconds).
4. The HTTP server stops and the process exits.

If nothing is running, the server exits right away. A **second** stop signal
during the drain skips the rest of it: active runs are settled as interrupted
immediately and the server stops. A repeated signal never starts a second
drain. A Gateway run (a chat that executes inside the Hermes Gateway through
its Runs API) is not waited on or interrupted once its run identity is saved:
it keeps running in the Gateway and is picked up again after the restart. A
Gateway chat on the older ``/v1/chat/completions`` relay cannot be picked up
again, so it counts as an active WebUI run, like a Gateway run that is still
starting.

``HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS`` takes plain decimal seconds (``90``,
``7.5``). ``0`` disables the wait (active runs are settled immediately),
negative values count as ``0``, values above ``3600`` are capped at ``3600``,
and anything else (``1e2``, ``inf``, text) is ignored with a warning in favour
of the default. The server prints the effective value at startup
(``Stop drain budget: 90s``). ``./ctl.sh stop`` waits that budget plus 5 seconds
before it falls back to ``SIGKILL``.

Updates started from the WebUI's update banner follow the same rule from the
other side: while WebUI agent runs are active the update is refused with the
run count, and the banner offers an explicit **Update anyway (interrupts N
runs)**. That update settles those runs as interrupted before the server
re-executes itself.

### The supervisor's stop timeout must outlast the drain

Every supervisor sends ``SIGKILL`` when the process has not exited within its
stop timeout. ``SIGKILL`` ends the drain with nothing settled, which is exactly
the lost-turn failure the drain exists to prevent. Set the stop timeout to
**at least the drain budget + 30 seconds**: **120 seconds** for the default
90-second budget.

| Supervisor | Setting | Default | For the default 90 s budget |
|---|---|---|---|
| systemd | ``TimeoutStopSec=`` | 90 s (``DefaultTimeoutStopSec``) | ``TimeoutStopSec=120`` |
| launchd | ``ExitTimeOut`` | system-defined (see ``launchd.plist(5)``) | ``<integer>120</integer>`` |
| supervisord | ``stopwaitsecs`` | 10 s | ``stopwaitsecs=120`` |
| Docker / Compose | ``docker stop -t`` / ``stop_grace_period`` | 10 s | see [Docker](#docker-the-drain-does-not-run-yet) below |

Note that systemd's stock 90-second timeout equals the default budget, so
without the override systemd kills the server just as the drain reaches its
deadline, before the remaining runs are settled. If you change
``HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS``, change the stop timeout with it.

### Docker: the drain does not run yet

The bundled image does **not** get the bounded drain on ``docker stop`` /
``docker compose down`` today, whatever ``stop_grace_period`` you set. The
stop signal never reaches the WebUI server process:

- The image's ``CMD`` is ``/hermeswebui_init.bash`` and there is no init
  process (no ``tini`` / ``--init``), so that script is PID 1. When the
  container starts as root (the default) the script drops privileges with
  ``exec su ... hermeswebui``, which makes ``su`` PID 1, and the re-run script
  starts the server as a child with ``python server.py`` (no ``exec``).
- ``docker stop`` signals PID 1 only. util-linux ``su`` answers ``SIGTERM`` by
  sending ``SIGTERM`` to its child, which is the init script's shell, not the
  server. The shell exits without passing it on, ``su`` waits two seconds and
  exits, and the kernel then ``SIGKILL``s every process left in the container,
  the server included. Running with ``--init`` / Compose ``init: true`` does
  not help: ``tini`` forwards the signal only to its direct child, the same
  ``su`` (or init script), with the same result.
- When the container is started as a non-root user (``--user``), the script
  itself stays PID 1. It installs no ``SIGTERM`` handler, so the kernel drops
  the signal, and ``docker stop`` ends in ``SIGKILL`` after its timeout.

Either way the server is ``SIGKILL``ed: active runs are neither drained nor
settled (they end the way any unexpected kill ends them, and the existing
startup recovery handles them), and the in-process memory-commit drain on
shutdown does not run either. Making the init script hand the stop signal to
the server (``exec`` the server, or forward ``SIGTERM`` to it) is a planned
follow-up to #7293; until it lands, raising ``stop_grace_period`` does not
help. The run admission fence, the refuse-or-force update check and
the settle before a WebUI-initiated re-exec all run inside the server process,
so they do not depend on the stop signal.

**systemd ``KillMode``.** With the default ``KillMode=control-group``, systemd
sends ``SIGTERM`` to every process in the unit at once, so the tool processes an
agent run started (shell commands, builds, test runs) are terminated at the
start of the drain and the run cannot finish them. Use ``KillMode=mixed``: the
stop ``SIGTERM`` goes only to the WebUI server, which drains. Child tool
processes keep running during the drain and are ``SIGKILL``ed by systemd once
the server exits or ``TimeoutStopSec`` expires, whichever comes first.

## launchd (macOS)

``~/Library/LaunchAgents/com.example.hermes-webui.plist``:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.hermes-webui</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/yourname/hermes-webui/start.sh</string>
        <string>--foreground</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/yourname/hermes-webui</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <!-- Stop timeout: drain budget (default 90s) + 30s. See "Stopping". -->
    <key>ExitTimeOut</key>
    <integer>120</integer>

    <key>StandardOutPath</key>
    <string>/Users/yourname/.hermes/webui/launchd-stdout.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/yourname/.hermes/webui/launchd-stderr.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>/Users/yourname</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
```

Load:

```bash
launchctl load ~/Library/LaunchAgents/com.example.hermes-webui.plist
launchctl print gui/$(id -u)/com.example.hermes-webui   # check state
```

Reload after editing the plist:

```bash
launchctl unload ~/Library/LaunchAgents/com.example.hermes-webui.plist
launchctl load   ~/Library/LaunchAgents/com.example.hermes-webui.plist
```

launchd sets ``XPC_SERVICE_NAME`` automatically, so even without the
``--foreground`` argument the Web UI will auto-promote to foreground mode.
The flag is still recommended as documentation of intent.

## systemd (Linux)

``~/.config/systemd/user/hermes-webui.service``:

```ini
[Unit]
Description=Hermes Web UI
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/hermes-webui
ExecStart=/bin/bash %h/hermes-webui/start.sh --foreground
Restart=on-failure
RestartSec=5
# Let active agent runs drain on stop: SIGTERM only the server (child tool
# processes are SIGKILLed when it exits or the timeout expires), and wait the
# drain budget (HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS, default 90) + 30s.
KillMode=mixed
TimeoutStopSec=120

# Optional: route stdout/stderr to journald instead of files
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

Enable + start:

```bash
systemctl --user daemon-reload
systemctl --user enable --now hermes-webui.service
journalctl --user -u hermes-webui.service -f
```

systemd sets ``INVOCATION_ID`` and ``JOURNAL_STREAM`` (when stdio is wired to
the journal), both of which auto-promote to foreground mode.

## supervisord (cross-platform)

``/etc/supervisor/conf.d/hermes-webui.conf``:

```ini
[program:hermes-webui]
command=/bin/bash /home/youruser/hermes-webui/start.sh --foreground
directory=/home/youruser/hermes-webui
user=youruser
autostart=true
autorestart=true
stopsignal=TERM
; drain budget (HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS, default 90) + 30s
stopwaitsecs=120
stdout_logfile=/var/log/hermes-webui.out.log
stderr_logfile=/var/log/hermes-webui.err.log
environment=HOME="/home/youruser",PATH="/usr/local/bin:/usr/bin:/bin"
```

Reload + start:

```bash
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl status hermes-webui
```

supervisord sets ``SUPERVISOR_ENABLED``, which auto-promotes to foreground
mode.

## Auto-detected env vars (full list)

These trigger ``--foreground`` behavior even when the flag is not passed:

| Env var | Set by | Notes |
|---|---|---|
| ``INVOCATION_ID`` | systemd | Set on every service activation |
| ``JOURNAL_STREAM`` | systemd | Set when stdio is wired to journald |
| ``NOTIFY_SOCKET`` | systemd ``Type=notify`` / s6 | sd_notify-style notification socket |
| ``XPC_SERVICE_NAME`` | launchd | Set to the plist Label — narrowed to ``com.<rdns>.<svc>`` form (see below) |
| ``SUPERVISOR_ENABLED`` | supervisord | Always set under supervisord |
| ``HERMES_WEBUI_FOREGROUND`` | you | Explicit opt-in; accepts ``1`` / ``true`` / ``yes`` / ``on`` |

### XPC_SERVICE_NAME noise filter

macOS launchd sets ``XPC_SERVICE_NAME`` in **every Terminal-spawned shell**,
not just real services. Typical noise values:

- ``0`` — set on launchd descendants generally
- ``application.com.apple.Terminal.<UUID>`` — Terminal.app shells
- ``application.com.googlecode.iterm2`` — iTerm2
- ``application.com.microsoft.VSCode`` — VSCode integrated terminal

A bare existence check on this var would auto-promote interactive
``./start.sh`` runs to foreground mode on every Mac dev machine, breaking
the most common installation path. We narrow detection to launchd
**Label-style** names (typically reverse-DNS like ``com.example.foo``).
Real launchd plists always use this form. If you ever see
``XPC_SERVICE_NAME=0`` in your service environment, the auto-detect will
ignore it — set ``HERMES_WEBUI_FOREGROUND=1`` or pass ``--foreground``
explicitly to be safe.

### Supervisors that are NOT auto-detected

The following set no env var that we can reliably detect. Pass
``--foreground`` (or ``HERMES_WEBUI_FOREGROUND=1``) explicitly:

- **runit** (without sd_notify) — pure runit chains
- **daemontools** / ``svc``
- **PM2** (Node.js process manager occasionally repurposed for Python)
- **Foreman** / **Honcho** (Procfile-style)
- **Docker** with a custom CMD entrypoint that doesn't already use ``exec``
- **Custom shell-script supervisors** that fork-and-wait

If your supervisor isn't in the auto-detect list and you see the orphan-PID
respawn loop, set ``HERMES_WEBUI_FOREGROUND=1`` in the service environment.

## Diagnostic recipe

If the Web UI keeps getting respawned and you suspect the double-fork loop:

```bash
# Check the running PID for the server
lsof -iTCP:8787 -sTCP:LISTEN

# Get its parent — should be the supervisor itself, NOT init (PID 1)
PID=$(lsof -tiTCP:8787 -sTCP:LISTEN)
ps -p "$PID" -o pid,ppid,cmd
ps -p "$(ps -o ppid= -p "$PID" | tr -d ' ')" -o pid,cmd
```

A healthy foreground-mode setup looks like:

```
PID    PPID  CMD
12345  6789  /path/to/python /path/to/server.py
6789   1     /sbin/launchd        # or /usr/lib/systemd/systemd, etc.
```

If PPID is ``1`` (init) when it should be the supervisor, the orphan-server
loop is happening — re-check that ``--foreground`` (or one of the env vars)
is reaching the process.

## HTTP watchdog / deep health

``KeepAlive`` / ``Restart=always`` only recover a process that exits. If the
process is still listening on the port but request handling is wedged, pair your
supervisor with an HTTP probe and force a restart when the probe fails.

Hermes Web UI exposes two health levels:

- ``/health`` — cheap liveness probe with ``active_streams``, uptime, and an
  ``accept_loop`` heartbeat counter.
- ``/health?deep=1`` — readiness probe that briefly acquires the stream lock,
  reads the sidebar/session path, reads projects state, and touches Hermes
  ``state.db`` if it exists. Use this for watchdogs.

At startup the server also tries to raise its file-descriptor soft limit to
4096 on platforms that support ``RLIMIT_NOFILE``. That is defense in depth for
persistent hosts: leaks should still be fixed, but a higher soft limit gives
you more diagnostic headroom before request handling falls over.

Minimal macOS launchd watchdog script:

```bash
#!/usr/bin/env bash
set -euo pipefail
LABEL="com.example.hermes-webui"
BASE="http://127.0.0.1:8787"

if ! curl -fsS --max-time 10 "$BASE/health?deep=1" >/dev/null; then
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
fi
```

Run it every few minutes from a separate ``StartInterval`` LaunchAgent. For
systemd, prefer a timer/service pair that runs the same curl probe and
``systemctl --user restart hermes-webui.service`` on failure.

The ``accept_loop.requests_total`` value should increase when probes arrive. If
it stays flat while the process is still alive, the server accept loop is not
making progress; capture logs/thread samples before restarting if you are
collecting diagnostics for a bug report.
