#!/usr/bin/env bash
# Watchdog: check hermes-webui for available updates via upstream git.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${HERMES_WEBUI_REPO:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
CHANGELOG="${REPO}/CHANGELOG.md"

cd "$REPO"

# Get current state
LOCAL_COMMIT=$(git rev-parse HEAD)
LOCAL_DATE=$(git log -1 --format=%ci HEAD)
LOCAL_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "no-tag")
LOCAL_COUNT=$(git rev-list --count HEAD)

# Fetch remote
git fetch origin master 2>&1

REMOTE_COMMIT=$(git rev-parse origin/master)
REMOTE_COUNT=$(git rev-list --count origin/master)

# Compare
BEHIND=$((REMOTE_COUNT - LOCAL_COUNT))

if [ "$BEHIND" -le 0 ]; then
    echo "✓ hermes-webui est à jour. ($LOCAL_TAG / $LOCAL_DATE)"
    exit 0
fi

# There are updates — extract changelog entries since local HEAD
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📦 hermes-webui — mise à jour disponible !"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Version locale :  ${LOCAL_TAG} (r${LOCAL_COUNT})"
echo "  Date locale :     ${LOCAL_DATE}"
echo "  Commits derrière : ${BEHIND}"
echo ""

# Show what's new: extract the relevant section from CHANGELOG
# The CHANGELOG has ## [Unreleased] at the top, then version entries like ## [v0.52.157]
# We need to find the section between the latest remote version and our current version.
# Simple approach: print CHANGELOG entries since our last known version.

echo "━━━ Nouveautés depuis votre dernière mise à jour ━━━"
echo ""

git log --max-count=40 HEAD..origin/master --oneline --no-decorate

echo ""
echo "━━━ Nouveautés du CHANGELOG ━━━"
echo ""

# Extract changelog entries between the versions we don't have yet
# Strategy: find our current commit's position in the changelog via release tags
# and show everything above it.

# Look at all version headers in CHANGELOG since our local commit
LOCAL_SHORT=$(git rev-parse --short HEAD)
git log --format="%H %s" HEAD..origin/master | while read -r commit msg; do
    echo "  • ${msg}"
done

echo ""
echo "━━━ extrait du CHANGELOG ━━━"
echo ""

# Extract CHANGELOG entries for the new versions
# Get the first version header in CHANGELOG that matches a new tag
# Simple: just show the [Unreleased] section + any version sections that reference new tags
while read -r ref; do
    tag=$(echo "$ref" | grep -oP 'tag: \K[^,)]+' || true)
    [ -z "$tag" ] && continue
    if grep -q "^## \[${tag}\]" "$CHANGELOG" 2>/dev/null; then
        echo "  → $tag"
    fi
done < <(git log --max-count=10 --format="%D" --tags --simplify-by-decoration 2>/dev/null)

echo ""
echo "💡 Pour mettre à jour : dis-moi \"hermes-webui update\""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
