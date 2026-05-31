#!/usr/bin/env bash
#
# Build Hermes.app and (optionally) a signed, notarized DMG for distribution.
#
# Stages:
#   1. Fetch a relocatable CPython (python-build-standalone) for the host arch.
#   2. Stage the .app bundle: Swift shell binary + bundled Python + WebUI source.
#   3. Install the WebUI's light deps into the bundled Python.
#   4. Compile main.swift.
#   5. (--sign)     codesign nested code then the app, hardened runtime.
#   6. (--notarize) submit to Apple notary service, staple the ticket.
#   7. Build a DMG with create-dmg.
#
# Usage:
#   ./build.sh                      # unsigned local build (for testing on THIS Mac)
#   ./build.sh --sign               # sign with Developer ID (set SIGN_IDENTITY)
#   ./build.sh --sign --notarize    # sign + notarize + staple + DMG (for others)
#
# Required for --sign / --notarize:
#   SIGN_IDENTITY  e.g. "Developer ID Application: bo wang (772APYS786)"
#   NOTARY_PROFILE keychain profile created once via:
#       xcrun notarytool store-credentials hermes-notary \
#         --apple-id you@example.com --team-id 772APYS786 --password <app-specific-pw>
#
set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
BUILD_DIR="${HERE}/build"
APP="${BUILD_DIR}/Hermes.app"
PY_MINOR="${HERMES_PY_MINOR:-3.12}"            # CPython series to bundle
ARCH="$(uname -m)"                             # arm64 | x86_64
SIGN_IDENTITY="${SIGN_IDENTITY:-}"
NOTARY_PROFILE="${NOTARY_PROFILE:-hermes-notary}"
BUNDLE_ID="com.nousresearch.hermes.webui"
VERSION="$(cd "${REPO_ROOT}" && git describe --tags --always 2>/dev/null || echo 0.0.0)"

DO_SIGN=0; DO_NOTARIZE=0
for a in "$@"; do
  case "$a" in
    --sign) DO_SIGN=1 ;;
    --notarize) DO_SIGN=1; DO_NOTARIZE=1 ;;
    *) echo "unknown arg: $a" >&2; exit 1 ;;
  esac
done

say() { printf '\033[1;36m▶ %s\033[0m\n' "$*" >&2; }

# ── 1. Fetch relocatable CPython ─────────────────────────────────────────────
fetch_python() {
  local cache="${HERE}/.python-cache"; mkdir -p "${cache}"
  case "${ARCH}" in
    arm64)  local pbs_arch="aarch64-apple-darwin" ;;
    x86_64) local pbs_arch="x86_64-apple-darwin" ;;
    *) echo "unsupported arch ${ARCH}" >&2; exit 1 ;;
  esac
  # Resolve the newest python-build-standalone asset for this series + arch.
  say "Resolving python-build-standalone (${PY_MINOR}, ${pbs_arch})…"
  local api="https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
  local asset_url
  # NB: GitHub URL-encodes the '+' in the version as %2B, so match either form.
  asset_url="$(curl -fsSL "${api}" \
    | grep -oE "https://[^\"]*cpython-${PY_MINOR}\.[0-9]+(%2B|\+)[0-9]+-${pbs_arch}-install_only\.tar\.gz" \
    | head -1)"
  [ -n "${asset_url}" ] || { echo "could not find a CPython ${PY_MINOR} ${pbs_arch} asset" >&2; exit 1; }
  local tarball="${cache}/$(basename "${asset_url}")"
  [ -f "${tarball}" ] || { say "Downloading ${asset_url##*/}"; curl -fsSL "${asset_url}" -o "${tarball}"; }
  rm -rf "${cache}/python"
  tar -xzf "${tarball}" -C "${cache}"   # extracts to ${cache}/python
  echo "${cache}/python"
}

# ── 2-4. Stage bundle + compile ──────────────────────────────────────────────
say "Cleaning ${BUILD_DIR}"
rm -rf "${BUILD_DIR}"; mkdir -p "${APP}/Contents/MacOS" "${APP}/Contents/Resources"

PY_SRC="$(fetch_python)"
say "Bundling Python from ${PY_SRC}"
cp -R "${PY_SRC}" "${APP}/Contents/Resources/python"
PY_BIN="${APP}/Contents/Resources/python/bin/python3"

say "Installing WebUI deps into bundled Python"
"${PY_BIN}" -m pip install --upgrade pip >/dev/null
"${PY_BIN}" -m pip install -r "${REPO_ROOT}/requirements.txt" >/dev/null

say "Copying WebUI source into bundle"
rsync -a --delete \
  --exclude '.git' --exclude 'node_modules' --exclude '*.venv' --exclude 'venv' \
  --exclude '__pycache__' --exclude 'packaging/macos/build' \
  --exclude 'packaging/macos/.python-cache' \
  "${REPO_ROOT}/" "${APP}/Contents/Resources/webui/"

say "Compiling Swift shell"
swiftc -O -o "${APP}/Contents/MacOS/Hermes" \
  -framework AppKit -framework WebKit \
  "${HERE}/HermesApp/main.swift"

say "Writing Info.plist (version ${VERSION})"
sed "s/__BUILD_VERSION__/${VERSION#v}/g" "${HERE}/Info.plist" > "${APP}/Contents/Info.plist"

# Optional icon
[ -f "${HERE}/AppIcon.icns" ] && cp "${HERE}/AppIcon.icns" "${APP}/Contents/Resources/AppIcon.icns"

# ── 5. Sign ──────────────────────────────────────────────────────────────────
if [ "${DO_SIGN}" = 1 ]; then
  [ -n "${SIGN_IDENTITY}" ] || { echo "SIGN_IDENTITY is required for --sign" >&2; exit 1; }
  local_ent="${HERE}/entitlements.plist"
  say "Signing nested code (Python + dylibs)…"
  # Sign every Mach-O inside the bundled Python first (deepest first), then the app.
  find "${APP}/Contents/Resources/python" \( -name '*.dylib' -o -name '*.so' -o -perm -u+x -type f \) -print0 \
    | while IFS= read -r -d '' f; do
        if file "$f" | grep -q 'Mach-O'; then
          codesign --force --timestamp --options runtime \
            --entitlements "${local_ent}" -s "${SIGN_IDENTITY}" "$f" 2>/dev/null || true
        fi
      done
  say "Signing app bundle…"
  codesign --force --deep --timestamp --options runtime \
    --entitlements "${local_ent}" -s "${SIGN_IDENTITY}" "${APP}"
  codesign --verify --deep --strict --verbose=2 "${APP}"
fi

# ── 6. Notarize + staple ─────────────────────────────────────────────────────
DMG="${BUILD_DIR}/Hermes-${VERSION#v}.dmg"
make_dmg() {
  say "Building DMG"
  rm -f "${DMG}"
  create-dmg \
    --volname "Hermes" \
    --window-size 540 380 \
    --icon-size 110 \
    --icon "Hermes.app" 150 180 \
    --app-drop-link 390 180 \
    --hide-extension "Hermes.app" \
    "${DMG}" "${APP}" || hdiutil create -volname Hermes -srcfolder "${APP}" -ov -format UDZO "${DMG}"
}

if [ "${DO_NOTARIZE}" = 1 ]; then
  make_dmg
  say "Submitting DMG to Apple notary service (profile: ${NOTARY_PROFILE})…"
  xcrun notarytool submit "${DMG}" --keychain-profile "${NOTARY_PROFILE}" --wait
  say "Stapling tickets"
  xcrun stapler staple "${APP}"
  xcrun stapler staple "${DMG}"
  say "Re-building DMG with stapled app"
  make_dmg
  xcrun stapler staple "${DMG}"
elif [ "${DO_SIGN}" = 1 ]; then
  make_dmg
else
  make_dmg
fi

say "Done → ${DMG}"
say "App  → ${APP}"
