Honcho quickstart — make Honcho the active memory provider (Windows helper)

This repo includes a helper script to make it easy to configure Honcho as the
memory provider for Hermes WebUI on Windows environments.

Files added:
- configure_honcho.ps1  — PowerShell helper that copies docs/honcho-config-example.yaml
  into your HERMES_HOME (default: %USERPROFILE%\.hermes), backs up existing
  config.yaml, and optionally sets HONCHO_API_KEY as a persistent user env var.
- docs/honcho-config-example.yaml — example config (already present)

How to use:
1) Open PowerShell in the repo root (C:\Users\Jan\llm\hermes-webui)
2) Run one of the following commands:
   - Dry run (no API key):
       powershell -ExecutionPolicy Bypass -File .\configure_honcho.ps1

   - Provide API key and copy config:
       powershell -ExecutionPolicy Bypass -File .\configure_honcho.ps1 -HonchoApiKey "sk-..."

   - Overwrite existing config without backup (force):
       powershell -ExecutionPolicy Bypass -File .\configure_honcho.ps1 -HonchoApiKey "sk-..." -Force

3) Restart the Hermes WebUI server (bootstrap.py / ctl.sh / start.sh depending
   on your install).
4) Open the WebUI at http://127.0.0.1:8787 and verify in Settings → Plugins →
   Memory that Honcho is the active provider.

Notes and safety:
- The helper backs up any existing config.yaml to config.yaml.bak.<timestamp>
  unless you pass -Force.
- The script uses setx to persist HONCHO_API_KEY as a user environment variable
  (new shells will see it). Avoid committing real API keys into repository files.
- If you prefer a GUI route, open WebUI → Settings → Plugins → Memory providers
  and configure Honcho from there.

If you want, I can run the script now with a placeholder API key (I will not
commit any real secrets). Confirm if you'd like me to execute it and whether
it's OK to set a placeholder or real key.