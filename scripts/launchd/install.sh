#!/usr/bin/env bash
set -euo pipefail

# install.sh — Install the hermes-webui launchd plist into ~/Library/LaunchAgents/
#
# What it does:
#   1. Backs up any existing identically-named plist to ~/.hermes/backups/launchd-YYYYMMDD-HHMMSS/
#   2. Copies the plist to ~/Library/LaunchAgents/com.parantoux.hermes-webui.plist
#   3. Prints the manual command to load the job — does NOT execute launchctl bootstrap/load
#
# Usage: ./scripts/launchd/install.sh

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLIST_SRC="${REPO_ROOT}/scripts/launchd/com.parantoux.hermes-webui.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/com.parantoux.hermes-webui.plist"
BACKUP_BASE="${HOME}/.hermes/backups"
TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
BACKUP_DIR="${BACKUP_BASE}/launchd-${TIMESTAMP}"

echo "=== hermes-webui launchd installer ==="
echo ""

# --- Validate source plist exists ---
if [[ ! -f "${PLIST_SRC}" ]]; then
    echo "[install] ERROR: plist not found at ${PLIST_SRC}" >&2
    exit 1
fi

# --- Backup existing plist if present ---
if [[ -f "${PLIST_DST}" ]]; then
    echo "[install] Existing plist found at ${PLIST_DST}"
    mkdir -p "${BACKUP_DIR}"
    cp "${PLIST_DST}" "${BACKUP_DIR}/"
    echo "[install]   → backed up to ${BACKUP_DIR}/"
else
    echo "[install] No existing plist to back up."
fi

# --- Copy plist to LaunchAgents ---
mkdir -p "$(dirname "${PLIST_DST}")"
cp "${PLIST_SRC}" "${PLIST_DST}"
echo "[install] Plist installed to ${PLIST_DST}"

echo ""

# --- Print manual activation instructions ---
echo "=== Manual activation (run these yourself when ready) ==="
echo ""
echo "  # Load and start the launchd job:"
echo "  launchctl bootstrap gui/\$(id -u) ${PLIST_DST}"
echo ""
echo "  # Or, on older macOS (pre-Ventura):"
echo "  launchctl load ${PLIST_DST}"
echo ""
echo "  # Verify it's running:"
echo "  launchctl print gui/\$(id -u)/com.parantoux.hermes-webui"
echo "  curl http://127.0.0.1:8788/health"
echo ""
echo "  # To uninstall later, run:"
echo "  ${REPO_ROOT}/scripts/launchd/uninstall.sh"
