#!/usr/bin/env bash
#
# Hermes Web UI — one-line installer (community project)
# https://get-hermes.ai/install.sh
#
#   curl -fsSL https://get-hermes.ai/install.sh | bash
#
# What it does (and nothing more):
#   1. Checks you have git + python3 (3.11+).
#   2. Checks the Hermes Agent `hermes` CLI is installed. If not, it prints the
#      one-line agent installer and exits — it does NOT install the agent for you.
#   3. Clones github.com/nesquena/hermes-webui to ~/hermes-webui  (or `git pull`
#      if it's already there — safe to re-run).
#   4. Hands off to ./start.sh, which builds the venv and starts the server on
#      http://127.0.0.1:8787.
#
# Hermes Web UI is an independent community project (nesquena/hermes-webui), NOT
# affiliated with or endorsed by Nous Research. It runs on Hermes Agent
# (github.com/NousResearch/hermes-agent), the official project by Nous Research.
#
# Don't trust a script piped from the internet? Read it first:
#   curl -fsSL https://get-hermes.ai/install.sh -o install.sh
#   less install.sh && bash install.sh
#
set -euo pipefail

REPO_URL="https://github.com/nesquena/hermes-webui.git"
DEST="${HERMES_WEBUI_DIR:-$HOME/hermes-webui}"
AGENT_INSTALL="curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash"

# ---- pretty output (no color if not a tty) --------------------------------
if [ -t 1 ]; then
  B="$(printf '\033[1m')"; DIM="$(printf '\033[2m')"; R="$(printf '\033[0m')"
  GRN="$(printf '\033[32m')"; YLW="$(printf '\033[33m')"; RED="$(printf '\033[31m')"
else
  B=""; DIM=""; R=""; GRN=""; YLW=""; RED=""
fi
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GRN" "$R" "$*"; }
warn() { printf '%s!%s %s\n' "$YLW" "$R" "$*"; }
die()  { printf '%s✗ %s%s\n' "$RED" "$*" "$R" >&2; exit 1; }

say ""
say "${B}Hermes Web UI installer${R} ${DIM}(community project)${R}"
say ""

# ---- 1. preflight: git + python3 (3.11+) ----------------------------------
command -v git >/dev/null 2>&1 || die "git is required but not found. Install git, then re-run."
command -v python3 >/dev/null 2>&1 || die "python3 (3.11+) is required but not found. Install Python 3.11+, then re-run."

PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
PYMAJ="${PYV%%.*}"; PYMIN="${PYV##*.}"
if [ "$PYMAJ" -lt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 11 ]; }; then
  die "Python 3.11+ is required (found $PYV). Install a newer Python, then re-run."
fi
ok "git and Python $PYV found"

# ---- 2. require the Hermes Agent CLI (we do NOT install it for you) --------
if ! command -v hermes >/dev/null 2>&1; then
  say ""
  warn "Hermes Agent (the ${B}hermes${R} CLI) is not installed yet."
  say "  Hermes Web UI runs on top of Hermes Agent, so install that first:"
  say ""
  say "    ${B}${AGENT_INSTALL}${R}"
  say ""
  say "  Then re-open your shell (or ${DIM}source ~/.bashrc${R}) and run this installer again."
  say "  Agent docs: https://hermes-agent.nousresearch.com/docs/getting-started/installation"
  say ""
  exit 1
fi
ok "Hermes Agent CLI detected ($(command -v hermes))"

# ---- 3. clone or update ~/hermes-webui (idempotent) -----------------------
if [ -d "$DEST/.git" ]; then
  say ""
  say "Updating existing checkout at ${B}$DEST${R} ..."
  if git -C "$DEST" pull --ff-only; then
    ok "Repository up to date"
  else
    warn "Could not fast-forward $DEST. Starting the existing version; update was not applied."
  fi
elif [ -e "$DEST" ]; then
  die "$DEST already exists but is not a git checkout. Move it aside or set HERMES_WEBUI_DIR, then re-run."
else
  say ""
  say "Cloning ${B}nesquena/hermes-webui${R} to ${B}$DEST${R} ..."
  git clone --depth 1 "$REPO_URL" "$DEST"
  ok "Cloned"
fi

# ---- 4. hand off to start.sh ----------------------------------------------
[ -x "$DEST/start.sh" ] || die "start.sh not found in $DEST — the clone may be incomplete."
say ""
say "${B}Starting Hermes Web UI${R} ${DIM}(first run builds the Python environment — this can take a minute)${R}"
say "Once it's up, open ${B}http://127.0.0.1:8787${R} in your browser."
say "Re-run any time with: ${DIM}cd $DEST && ./start.sh${R}"
say ""
cd "$DEST"
exec ./start.sh
