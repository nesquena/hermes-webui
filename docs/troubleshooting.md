# Troubleshooting

Concrete diagnostic flows for the most common failure modes when running Hermes WebUI. Each entry has the symptom, the diagnostic commands you should run *before* opening an issue, and the fix that has worked for past reporters.

If your symptom isn't listed and the diagnostics don't narrow it down, file a bug at https://github.com/nesquena/hermes-webui/issues — include the relevant command output after redacting secrets, private paths, full `.env` files, full `auth.json` files, cookies, tokens, and password hashes.

---

## "AIAgent not available -- check that hermes-agent is on sys.path"

**Symptom.** WebUI starts, shows the chat interface, but every chat request fails immediately with this error in the response or the server log. As of v0.51.6 the error includes a diagnostic block with the running Python interpreter, the relevant `sys.path` entries, and the most-common fix; on older versions the message is bare.

**Why it happens.** The WebUI imports the agent class at chat time via `from run_agent import AIAgent`. That import only succeeds if the running Python's `sys.path` contains either the hermes-agent checkout or a pip-installed copy of the agent. Three common failure modes:

1. **Agent installed but not on `sys.path`.** Most common. The agent is checked out somewhere (e.g. `~/Programmes/hermes-agent`), the WebUI was launched with a Python that doesn't know about it, and there's no `pip install -e .` linking the two.
2. **Symlink with a typo or wrong target.** A symlink to the agent looks correct on `ls`, but `readlink` resolves to a path that doesn't exist or doesn't contain `agent/__init__.py`.
3. **`HERMES_WEBUI_AGENT_DIR` set to the wrong directory.** Override env var beats auto-discovery and points at a directory that has no agent code.

### Step 1 — confirm the agent location

```bash
# If you have ~/hermes-agent (the default location):
ls -la ~/hermes-agent
readlink ~/hermes-agent          # if it's a symlink, where does it resolve?
ls ~/hermes-agent/agent/__init__.py 2>&1
```

The third command must succeed (the file must exist). If it fails, your symlink is broken or pointing at a directory that's missing the agent module — fix that first.

### Step 2 — confirm the WebUI is using the right Python

```bash
cd ~/hermes-webui && ./start.sh 2>&1 | grep -iE 'agent|python|hermes_webui_python' | head -20
```

The startup banner prints which Python and agent dir it resolved. If the agent dir is empty or the Python is the wrong one, set the override:

```bash
export HERMES_WEBUI_AGENT_DIR=/absolute/path/to/hermes-agent
export HERMES_WEBUI_PYTHON=/absolute/path/to/agent/venv/bin/python
./start.sh
```

### Step 3 — install the agent in editable mode

This is the most common fix and resolves the original issue #1695:

```bash
cd /path/to/hermes-agent          # the directory holding pyproject.toml + the agent/ module
pip install -e .                  # use the same python that runs the WebUI
```

Then restart the WebUI:

```bash
cd ~/hermes-webui
./start.sh
```

### Step 4 — verify by importing manually

If steps 1-3 still don't work, check whether the WebUI's Python can import the agent at all:

```bash
$HERMES_WEBUI_PYTHON -c "from run_agent import AIAgent; print('ok')" 2>&1
```

(Replace `$HERMES_WEBUI_PYTHON` with the actual Python path from step 2 if the env var isn't set.) If this prints `ok`, the agent IS on `sys.path` for that Python — and the WebUI should work.

If this fails, `import run_agent` itself is broken — check that the agent's pyproject.toml lists `run_agent` as a top-level module or that the agent dir is on PYTHONPATH:

```bash
PYTHONPATH=/path/to/hermes-agent $HERMES_WEBUI_PYTHON -c "from run_agent import AIAgent; print('ok')"
```

If adding PYTHONPATH fixes it, persist the path either via `pip install -e .` (preferred) or by setting `HERMES_WEBUI_AGENT_DIR` to that directory.

### When to file a bug

If after running steps 1-4 the import still fails *and* `pip install -e .` succeeded *and* `PYTHONPATH=... python -c "from run_agent import AIAgent"` succeeds — that's a real WebUI bug. File at https://github.com/nesquena/hermes-webui/issues with:

- The output of every command in steps 1-4
- The full diagnostic block printed by the WebUI's `ImportError` (v0.51.6+)
- Your OS, Python version, and how the agent was installed

---

## Existing Hermes setup opens onboarding or reports `needs_provider`

**Symptom.** You already use Hermes from the CLI or another running profile,
but WebUI opens the first-run wizard. `/api/onboarding/status` may report
`completed=false`, `system.setup_state=needs_provider`,
`system.provider_configured=false`, or `system.config_exists=false`.

**Why.** WebUI reads Hermes config and credentials from the `HERMES_HOME` of
the running WebUI process. If WebUI was launched for an isolated support trial
such as `HERMES_HOME=~/hermes-onboarding-test/.hermes`, or a repo `.env` points
`HERMES_HOME` / `HERMES_WEBUI_STATE_DIR` somewhere unexpected, the existing
Hermes setup is invisible. In that case the wizard is accurately reporting the
empty trial home; it does not mean the real Hermes install lost its provider
setup.

**Diagnostic.**

1. Check only the non-secret onboarding fields:
   ```bash
   python3 - <<'PY'
   import json, urllib.request

   status = json.load(urllib.request.urlopen(
       "http://127.0.0.1:8789/api/onboarding/status",
       timeout=10,
   ))
   system = status.get("system", {})
   print("completed=", status.get("completed"))
   for key in (
       "config_path",
       "config_exists",
       "setup_state",
       "provider_configured",
       "provider_ready",
       "chat_ready",
       "current_provider",
       "current_model",
       "env_path",
   ):
       print(f"{key}={system.get(key)}")
   PY
   ```
2. Check whether repo-local `.env` overrides are changing the active state. Do
   not print the whole file:
   ```bash
   test -f .env && grep -nE '^(HERMES_HOME|HERMES_WEBUI_STATE_DIR|HERMES_WEBUI_PORT|HERMES_WEBUI_HOST)=' .env || true
   ```
3. Compare those paths with the Hermes home or profile you meant to run. For a
   named profile, the home usually looks like `~/.hermes/profiles/<profile>` and
   WebUI state usually belongs under that profile, for example
   `~/.hermes/profiles/<profile>/webui`.

**Fix.** Stop the WebUI process that is using the wrong state, then restart it
with the intended Hermes home and WebUI state directory:

```bash
HERMES_HOME="$HOME/.hermes" \
HERMES_WEBUI_STATE_DIR="$HOME/.hermes/webui" \
HERMES_WEBUI_PORT=8789 \
python3 bootstrap.py --no-browser
```

For a named Hermes profile, point both variables at that profile instead:

```bash
HERMES_HOME="$HOME/.hermes/profiles/<profile>" \
HERMES_WEBUI_STATE_DIR="$HOME/.hermes/profiles/<profile>/webui" \
HERMES_WEBUI_PORT=8789 \
python3 bootstrap.py --no-browser
```

If you use `ctl.sh`, put the intended values in the repo `.env` or pass them
inline to `./ctl.sh restart` so future daemon restarts keep the same state.

**When to file a bug.** If `system.config_path` points at the intended Hermes
home, `system.config_exists=true`, and WebUI still reports `needs_provider` for
a provider that works from the same environment in `hermes`, collect the
diagnostic output above plus the provider name/model with secrets redacted and
file an issue.

---

## "Response interrupted." marker keeps saying "no agent output was recovered"

**Symptom.** After the WebUI process restarts mid-turn (manual restart, OOM, crash, …), the affected chat shows an `**Response interrupted.**` marker with the wording *"The user message above was preserved, but no agent output was recovered."*, even though the run-journal for that turn is present on disk and contains the partial tokens the agent had already streamed.

**Why.** Sidecar repair re-checks the run-journal at restart and uses the result as a one-shot signal. On WSL2 (9p / DrvFs) and on some network-backed setups, the run-journal `.jsonl` is written by the dead worker but the WebUI process reads it through a page-cache state that has not yet seen those writes — recovery returns "empty" and the marker is baked permanently. The fix introduces a *lazy* retry path: when sidecar repair cannot read visible output but knows the stream id, it stores a `_pending_journal_recovery` flag on the marker and re-attempts recovery from `get_session()` until the journal becomes readable (or the retry budget is exhausted).

**Diagnostic.**

The on-disk locations below assume the default `~/.hermes/webui` state directory. If you override it via `HERMES_WEBUI_STATE_DIR`, substitute that path for `~/.hermes/webui` in every step.

1. Identify the affected session id and stream id from the marker. The marker JSON lives at `~/.hermes/webui/sessions/<sid>.json`; after the fix it shows them on the `_journal_retry_stream_id` key. Pre-fix sessions only carry the legacy wording, with no retry meta.
2. Check whether the run-journal contains real events:
   ```bash
   ls -la ~/.hermes/webui/sessions/_run_journal/<sid>/<stream_id>.jsonl
   head -2 ~/.hermes/webui/sessions/_run_journal/<sid>/<stream_id>.jsonl
   ```
   If the file exists and contains `token` / `tool` events, the lazy-retry path will pick them up the next time the session is opened.

**Fix.** Reload the session in the browser. On the next `get_session()` call the marker is re-evaluated; if the journaled events are visible on disk the marker promotes to *"The partial output above was recovered from the run journal …"* wording and the journaled assistant text + tool cards land above the marker in chronological order. No manual sidecar editing is required.

**Trigger.** Sidebar metadata polling is intentionally not enough to run this self-heal. Requests such as `/api/session?messages=0&resolve_model=0` load the session with `metadata_only=True`, skip the full messages array, and therefore skip the lazy journal retry helper. Click/open the affected conversation so the message panel performs a full `messages=1` load; that full render is what re-checks the journal and can promote the marker.

**Caps.** The lazy retry path gives up after 12 failed attempts or 24h of wall-clock age, at which point the marker is demoted to a neutral *"Partial output may have been lost."* wording so the "reload to retry" prompt doesn't linger forever for genuinely lost journals.

**When to file a bug.** If, after the fix, you see the lazy-retry wording (*"Recovering the partial output from the run journal — reload this session to retry."*) but reloading the session never promotes it to the recovered wording even though the `.jsonl` clearly contains `token` events, capture the marker JSON and the run-journal file and file a bug.

---

## Other troubleshooting

This document grows over time. If a recurring failure mode isn't covered here yet, add it via PR. The format for each entry: **Symptom → Why → Diagnostic commands → Fix → When to file a bug**.

Related references:

- [`docs/supervisor.md`](supervisor.md) — process-supervisor setup (launchd, systemd, supervisord, runit/s6) including the bootstrap supervisor-foreground flag.
- [`docs/docker.md`](docker.md) — Docker compose setup, common failure modes, bind-mount migration.
- [`docs/wsl-autostart.md`](wsl-autostart.md) — WSL2 auto-start at login on Windows.
- [`docs/EXTENSIONS.md`](EXTENSIONS.md) — WebUI extension injection, security model, examples.
