#!/usr/bin/env bash
set -euo pipefail

# uninstall.sh — Remove the hermes-webui launchd plist from ~/Library/LaunchAgents/
#
# What it does:
#   1. Prints the manual unload commands — does NOT execute launchctl bootout/unload
#   2. Removes the plist file from ~/Library/LaunchAgents/
#
# Usage: ./scripts/launchd/uninstall.sh

PLIST_DST="${HOME}/Library/LaunchAgents/com.parantoux.hermes-webui.plist"
LABEL="com.parantoux.hermes-webui"
UID_NOW="$(id -u)"

echo "=== hermes-webui launchd uninstaller ==="
echo ""

if [[ ! -f "${PLIST_DST}" ]]; then
    echo "[uninstall] No plist found at ${PLIST_DST} — nothing to remove."
    exit 0
fi

# --- Print manual unload instructions ---
echo "[uninstall] Before removing the file, stop the launchd job manually:"
echo ""
echo "  # Stop and unregister the job (macOS Ventura+):"
echo "  launchctl bootout gui/${UID_NOW}/${LABEL}"
echo ""
echo "  # Or, on older macOS (pre-Ventura):"
echo "  launchctl unload ${PLIST_DST}"
echo ""
echo "  # Verify it's gone:"
echo "  launchctl print gui/${UID_NOW}/${LABEL}"
echo ""

# --- Remove the plist file ---
rm -f "${PLIST_DST}"
echo "[uninstall] Removed ${PLIST_DST}"
echo "[uninstall] Done. The job has been unregistered from the filesystem."
echo "[uninstall] If the job was still loaded, run the manual unload command above."
