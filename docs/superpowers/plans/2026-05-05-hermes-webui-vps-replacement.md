# Hermes WebUI VPS Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the upstream `hermes-webui` currently running at `/opt/hermes-webui` on `srvjosemaria` with the customized `melojrx/neo-webui` fork, keeping the existing Hermes runtime, nginx, auth, state, sessions, and rollback path intact.

**Architecture:** Keep the production topology unchanged: nginx terminates HTTPS for `neo.investiorion.com` and proxies to `hermes-webui.service` on `127.0.0.1:8787`. The only application change is the Git working tree under `/opt/hermes-webui`, plus a small env alignment in `/etc/hermes-webui/env`.

**Tech Stack:** Ubuntu 24.04, systemd, nginx, Python 3.12 via `/opt/hermes/.venv/bin/python`, vanilla JS static frontend, Hermes state in `/home/hermes-admin/.hermes`.

---

## Current Verified Baseline

- Local repo: `/home/jrmelo/Projetos/neo-webui`, branch `main`, clean, commit `2f0587e merge: promote develop to production main`.
- Local verification completed on 2026-05-05:
  - `.venv/bin/pytest -q`: `3699 passed, 56 skipped, 3 xpassed, 1 warning, 8 subtests passed`.
  - `git diff --check`: pass.
  - `node --check static/sw.js static/dashboard.js static/kanban.js static/boot.js static/ui.js`: pass.
  - Temporary isolated launcher on `127.0.0.1:8799` returned `/health` status `ok`.
- VPS service: `hermes-webui.service` active, running as `hermes-admin`, `WorkingDirectory=/opt/hermes-webui`, `ExecStart=/opt/hermes/.venv/bin/python /opt/hermes-webui/server.py`.
- VPS current repo: `/opt/hermes-webui` is still upstream `https://github.com/nesquena/hermes-webui.git`, branch `master`, commit `9e31a2ac65c3fa7c26a733e213a308aa4a04f992`.
- VPS has one local production patch not yet in the fork: `api/gateway_watcher.py` changes `POLL_INTERVAL = 5` to `POLL_INTERVAL = 30`.
- VPS env currently contains:
  - `HERMES_WEBUI_DEFAULT_MODEL=glm-5.1`
  - `HERMES_WEBUI_BOT_NAME=Neo`
  - `HERMES_WEBUI_PASSWORD=<set>`
  - `HERMES_WEBUI_ALLOWED_ORIGINS=https://neo.investiorion.com`
- VPS nginx already proxies `neo.investiorion.com` to `127.0.0.1:8787` and serves `/static/` directly from `/opt/hermes-webui/static/`.
- Automated timers are active: `hermes-backup.timer` daily at 03:30 and `hermes-auto-update.timer` weekly Sunday at 04:15.

## Release Gate

Do not start the replacement until all release-gate items are true.

- [ ] **Step 1: Decide the gateway watcher polling interval**

Current production uses `30s`; the local Neo fork still uses `5s`.

Recommended action before deployment:

```bash
sed -n '80,100p' api/gateway_watcher.py
```

Expected local line after the decision:

```python
    POLL_INTERVAL = 30  # seconds between polls
```

If keeping `5s`, record the decision in `docs/neo/TASKS.md` or the Obsidian operational doc because the deploy will intentionally remove the current VPS tuning.

- [ ] **Step 2: Re-run the local release checks**

```bash
git status --short --branch
.venv/bin/pytest -q
git diff --check
node --check static/sw.js
node --check static/dashboard.js
node --check static/kanban.js
node --check static/boot.js
node --check static/ui.js
```

Expected:

```text
## main...origin/main
3699 passed, 56 skipped, 3 xpassed
```

The exact pytest duration can vary. Any failure blocks deploy.

- [ ] **Step 3: Push the exact production commit**

```bash
git push origin main
git rev-parse HEAD
git status --short --branch
```

Expected:

```text
## main...origin/main
```

Record the commit hash in the deployment note before touching the VPS.

## VPS Cutover

- [ ] **Step 4: Capture the live baseline**

Run from the local machine:

```bash
ssh -o BatchMode=yes root@38.52.128.62 systemctl show hermes-webui -p FragmentPath -p User -p Group -p WorkingDirectory -p ExecStart -p EnvironmentFiles -p MainPID -p ActiveState -p SubState
ssh -o BatchMode=yes root@38.52.128.62 sudo -u hermes-admin -H git -C /opt/hermes-webui status --short --branch
ssh -o BatchMode=yes root@38.52.128.62 sudo -u hermes-admin -H git -C /opt/hermes-webui rev-parse HEAD
ssh -o BatchMode=yes root@38.52.128.62 curl -fsS http://127.0.0.1:8787/health
```

Expected:

```text
ActiveState=active
SubState=running
{"status":"ok", ...}
```

- [ ] **Step 5: Run an explicit backup before the cutover**

```bash
ssh -o BatchMode=yes root@38.52.128.62 systemctl start hermes-backup.service
ssh -o BatchMode=yes root@38.52.128.62 systemctl status hermes-backup.service --no-pager
```

Expected: `hermes-backup.service` exits successfully. If it fails, stop and inspect `/var/log/neo-ops/` before deploying.

- [ ] **Step 6: Save a direct WebUI rollback snapshot**

```bash
ssh -o BatchMode=yes root@38.52.128.62 mkdir -p /srv/backups/webui-cutover-2026-05-05
ssh -o BatchMode=yes root@38.52.128.62 tar -C /opt -czf /srv/backups/webui-cutover-2026-05-05/hermes-webui-pre-neo.tar.gz hermes-webui
ssh -o BatchMode=yes root@38.52.128.62 cp /etc/hermes-webui/env /srv/backups/webui-cutover-2026-05-05/env.pre-neo
ssh -o BatchMode=yes root@38.52.128.62 cp /etc/systemd/system/hermes-webui.service /srv/backups/webui-cutover-2026-05-05/hermes-webui.service.pre-neo
```

Expected: the tarball and two config copies exist in `/srv/backups/webui-cutover-2026-05-05/`.

- [ ] **Step 7: Preserve the current upstream commit and local patch**

```bash
ssh -o BatchMode=yes root@38.52.128.62 sudo -u hermes-admin -H git -C /opt/hermes-webui rev-parse HEAD
ssh -o BatchMode=yes root@38.52.128.62 sudo -u hermes-admin -H git -C /opt/hermes-webui diff -- api/gateway_watcher.py
ssh -o BatchMode=yes root@38.52.128.62 sudo -u hermes-admin -H git -C /opt/hermes-webui diff -- api/gateway_watcher.py > /tmp/hermes-webui-gateway-watcher-pre-neo.patch
```

Expected patch content includes:

```diff
-    POLL_INTERVAL = 5  # seconds between polls
+    POLL_INTERVAL = 30  # seconds between polls
```

- [ ] **Step 8: Repoint `/opt/hermes-webui` to the Neo fork**

```bash
ssh -o BatchMode=yes root@38.52.128.62 sudo -u hermes-admin -H git -C /opt/hermes-webui remote set-url origin git@github.com:melojrx/neo-webui.git
ssh -o BatchMode=yes root@38.52.128.62 sudo -u hermes-admin -H git -C /opt/hermes-webui fetch origin main
ssh -o BatchMode=yes root@38.52.128.62 sudo -u hermes-admin -H git -C /opt/hermes-webui switch -C main --track origin/main
ssh -o BatchMode=yes root@38.52.128.62 sudo -u hermes-admin -H git -C /opt/hermes-webui reset --hard origin/main
```

Expected:

```text
branch 'main' set up to track 'origin/main'
HEAD is now at <neo-production-commit>
```

This intentionally removes the uncommitted upstream `gateway_watcher.py` change. Only run this after Step 1 has been resolved in the fork or explicitly waived.

- [ ] **Step 9: Align the WebUI env for Neo production**

Edit `/etc/hermes-webui/env` so it contains these non-secret values:

```bash
HERMES_WEBUI_BOT_NAME=Neo
HERMES_WEBUI_DEFAULT_SKIN=neo
HERMES_WEBUI_LOCALE=pt-BR
HERMES_WEBUI_DEFAULT_PANEL=dashboard
HERMES_WEBUI_ALLOWED_ORIGINS=https://neo.investiorion.com
```

Keep the existing `HERMES_WEBUI_PASSWORD` value unchanged.

Remove or comment out:

```bash
HERMES_WEBUI_DEFAULT_MODEL=glm-5.1
```

Reason: GLM/Z.AI is documented as inactive since 2026-05-04, and the current fork no longer wants to persist a stale WebUI default model over the Hermes runtime provider default.

- [ ] **Step 10: Restart and verify the service**

```bash
ssh -o BatchMode=yes root@38.52.128.62 systemctl daemon-reload
ssh -o BatchMode=yes root@38.52.128.62 systemctl restart hermes-webui
ssh -o BatchMode=yes root@38.52.128.62 systemctl status hermes-webui --no-pager
ssh -o BatchMode=yes root@38.52.128.62 curl -fsS http://127.0.0.1:8787/health
```

Expected:

```text
Active: active (running)
{"status":"ok", ...}
```

- [ ] **Step 11: Verify static file serving through nginx**

```bash
ssh -o BatchMode=yes root@38.52.128.62 curl -fsSI https://neo.investiorion.com/
ssh -o BatchMode=yes root@38.52.128.62 curl -fsSI https://neo.investiorion.com/static/dashboard.js
ssh -o BatchMode=yes root@38.52.128.62 curl -fsSI https://neo.investiorion.com/static/kanban.js
ssh -o BatchMode=yes root@38.52.128.62 curl -fsSI https://neo.investiorion.com/static/brand/neo-avatar.svg
```

Expected:

```text
HTTP/2 200
```

For `/`, `HTTP/2 302` to `/login` is acceptable if auth is active and no cookie is sent. Static assets must be `200`.

- [ ] **Step 12: Tail logs during first browser login**

```bash
ssh -o BatchMode=yes root@38.52.128.62 journalctl -u hermes-webui -n 160 --no-pager
```

Expected: no Python traceback and no repeated 500s.

Manual browser checks:

- Login opens `https://neo.investiorion.com`.
- Dashboard is the first screen.
- Branding, favicon, skin, and pt-BR text show Neo defaults.
- Chat composer is visible and sends a short message.
- Projects opens Kanban and List views.
- Skills and Configurações open inside the Neo shell.
- Browser console has no critical JavaScript errors.

## Rollback

Use rollback if `/health` fails, login is broken, static assets fail, chat cannot start, or service logs show repeated tracebacks after the restart.

- [ ] **Step 13: Restore the pre-cutover tree from tarball**

```bash
ssh -o BatchMode=yes root@38.52.128.62 systemctl stop hermes-webui
ssh -o BatchMode=yes root@38.52.128.62 mv /opt/hermes-webui /opt/hermes-webui.failed-neo-2026-05-05
ssh -o BatchMode=yes root@38.52.128.62 tar -C /opt -xzf /srv/backups/webui-cutover-2026-05-05/hermes-webui-pre-neo.tar.gz
ssh -o BatchMode=yes root@38.52.128.62 cp /srv/backups/webui-cutover-2026-05-05/env.pre-neo /etc/hermes-webui/env
ssh -o BatchMode=yes root@38.52.128.62 systemctl start hermes-webui
ssh -o BatchMode=yes root@38.52.128.62 curl -fsS http://127.0.0.1:8787/health
```

Expected:

```text
{"status":"ok", ...}
```

- [ ] **Step 14: Record the rollback**

Add an entry to the Obsidian operational document:

```text
2026-05-05 Neo WebUI cutover rollback:
- attempted commit: <neo commit>
- failure symptom: <exact symptom>
- restored from: /srv/backups/webui-cutover-2026-05-05/hermes-webui-pre-neo.tar.gz
- next action: <fix before retry>
```

## Post-Deploy Hardening

- [ ] **Step 15: Update the Obsidian VPS documentation**

Change the WebUI subsection so it no longer says:

```text
Repo: nesquena/hermes-webui
HERMES_WEBUI_DEFAULT_MODEL=glm-5.1
```

Expected replacement:

```text
Repo: melojrx/neo-webui
Branch: main
HERMES_WEBUI_BOT_NAME=Neo
HERMES_WEBUI_DEFAULT_SKIN=neo
HERMES_WEBUI_LOCALE=pt-BR
HERMES_WEBUI_DEFAULT_PANEL=dashboard
Default model: resolved by Hermes config.yaml/provider default; no WebUI override
```

- [ ] **Step 16: Decide how auto-update should treat the WebUI fork**

Inspect `/usr/local/sbin/hermes-auto-update.sh` before the next Sunday timer:

```bash
ssh -o BatchMode=yes root@38.52.128.62 sed -n '1,260p' /usr/local/sbin/hermes-auto-update.sh
```

Expected policy: the runtime Hermes auto-update can keep tracking `NousResearch/hermes-agent`, but `/opt/hermes-webui` must track `melojrx/neo-webui main`, not `nesquena/hermes-webui master`.

If the script hardcodes upstream WebUI behavior, update it before `Sun 2026-05-10 04:15:00 -03`.

