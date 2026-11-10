#!/bin/sh
# Refuse to start when .env contradicts the backend address this unit needs.
#
# Why this exists: start.sh sources .env AFTER inheriting this unit's
# Environment= values (`set -a; source .env`), and bootstrap.py loads .env
# again on import (`_load_repo_dotenv`). Both set values unconditionally, so
# a value in .env ALWAYS wins over the unit. A stale HERMES_WEBUI_PORT there
# starts the backend on the wrong port while nginx still proxies to
# 127.0.0.1:8788 — the only symptom is a silent 502, with the unit looking
# correct. Fail loudly instead.
#
# Installed as part of the checkout at /opt/hermes-webui/deploy/check-env.sh
# and invoked by the unit's ExecStartPre. Exits 0 when .env is absent or
# agrees; exits 1 with a message when it conflicts.
set -u

ENV_FILE="${HERMES_WEBUI_ENV_FILE:-/opt/hermes-webui/.env}"
[ -f "$ENV_FILE" ] || exit 0

# Last assignment wins in a sourced .env, so read the last matching line and
# strip an optional `export`, surrounding quotes, and a trailing comment.
value_of() {
  grep -E "^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=" "$ENV_FILE" 2>/dev/null \
    | tail -n 1 \
    | cut -d= -f2- \
    | sed -e 's/[[:space:]]*#.*$//' \
          -e 's/^[[:space:]]*//' \
          -e 's/[[:space:]]*$//' \
          -e 's/^"//' -e 's/"$//' \
          -e "s/^'//" -e "s/'\$//"
}

for pair in "HERMES_WEBUI_PORT:8788" "HERMES_WEBUI_HOST:127.0.0.1"; do
  key="${pair%%:*}"
  want="${pair#*:}"
  got="$(value_of "$key")"
  if [ -n "$got" ] && [ "$got" != "$want" ]; then
    echo "[hermes-webui] ${key}=${got} in ${ENV_FILE} overrides this unit's required ${want}." >&2
    echo "[hermes-webui] nginx proxies to 127.0.0.1:8788, so the backend would not be reachable." >&2
    echo "[hermes-webui] Remove or correct that line in ${ENV_FILE}, then restart the unit." >&2
    exit 1
  fi
done

exit 0
