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

DO_SIGN=0; DO_NOTARIZE=0; UNIVERSAL=0
for a in "$@"; do
  case "$a" in
    --sign) DO_SIGN=1 ;;
    --notarize) DO_SIGN=1; DO_NOTARIZE=1 ;;
    --universal) UNIVERSAL=1 ;;
    *) echo "unknown arg: $a" >&2; exit 1 ;;
  esac
done

# Arches to bundle. A universal build ships both Pythons + a fat Swift binary so
# one DMG runs natively on Apple Silicon and Intel; otherwise just the host arch.
if [ "${UNIVERSAL}" = 1 ]; then ARCHES="arm64 x86_64"; else ARCHES="${ARCH}"; fi
pbs_arch_of() { case "$1" in arm64) echo aarch64-apple-darwin ;; x86_64) echo x86_64-apple-darwin ;; esac; }
swift_target_of() { case "$1" in arm64) echo arm64-apple-macosx12.0 ;; x86_64) echo x86_64-apple-macosx12.0 ;; esac; }

say() { printf '\033[1;36m▶ %s\033[0m\n' "$*" >&2; }

# ── 1. Fetch relocatable CPython for a given arch ────────────────────────────
fetch_python() {  # $1 = arch (arm64|x86_64) → prints extracted python dir
  local arch="$1"; local pbs_arch; pbs_arch="$(pbs_arch_of "${arch}")"
  local cache="${HERE}/.python-cache"; mkdir -p "${cache}"
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
  local out="${cache}/python-${arch}"
  rm -rf "${out}"; mkdir -p "${out}"
  tar -xzf "${tarball}" -C "${out}" --strip-components=1   # python/* → ${out}/*
  echo "${out}"
}

# Install the WebUI's light deps into a bundled Python tree (host-arch runs it
# directly; a foreign arch is installed via Rosetta, else cross-resolved wheels).
install_deps() {  # $1 = python dir, $2 = arch
  local pydir="$1"; local arch="$2"; local pybin="${pydir}/bin/python3"
  if [ "${arch}" = "${ARCH}" ]; then
    "${pybin}" -m pip install --upgrade pip >/dev/null
    "${pybin}" -m pip install -r "${REPO_ROOT}/requirements.txt" >/dev/null
  elif arch -"${arch}" /usr/bin/true 2>/dev/null; then
    say "Installing deps for ${arch} via Rosetta"
    arch -"${arch}" "${pybin}" -m pip install --upgrade pip >/dev/null
    arch -"${arch}" "${pybin}" -m pip install -r "${REPO_ROOT}/requirements.txt" >/dev/null
  else
    say "Rosetta unavailable — cross-installing ${arch} wheels"
    local sp="${pydir}/lib/python${PY_MINOR}/site-packages"
    python3 -m pip install --only-binary=:all: --platform "macosx_11_0_${arch}" \
      --implementation cp --python-version "${PY_MINOR}" --target "${sp}" \
      -r "${REPO_ROOT}/requirements.txt" >/dev/null
  fi
}

# ── 2-4. Stage bundle + compile ──────────────────────────────────────────────
say "Cleaning ${BUILD_DIR}"
rm -rf "${BUILD_DIR}"; mkdir -p "${APP}/Contents/MacOS" "${APP}/Contents/Resources"

for a in ${ARCHES}; do
  PY_SRC="$(fetch_python "${a}")"
  if [ "${UNIVERSAL}" = 1 ]; then dest="${APP}/Contents/Resources/python-${a}"; else dest="${APP}/Contents/Resources/python"; fi
  say "Bundling Python (${a}) → $(basename "${dest}")"
  cp -R "${PY_SRC}" "${dest}"
  say "Installing WebUI deps into bundled Python (${a})"
  install_deps "${dest}" "${a}"
done

say "Copying WebUI source into bundle"
rsync -a --delete \
  --exclude '.git' --exclude 'node_modules' --exclude '*.venv' --exclude 'venv' \
  --exclude '__pycache__' --exclude 'packaging/macos/build' \
  --exclude 'packaging/macos/.python-cache' \
  "${REPO_ROOT}/" "${APP}/Contents/Resources/webui/"

say "Compiling Swift shell (${ARCHES})"
SWIFT_OUT="${APP}/Contents/MacOS/Hermes"
if [ "${UNIVERSAL}" = 1 ]; then
  slices=""
  for a in ${ARCHES}; do
    swiftc -O -target "$(swift_target_of "${a}")" -framework AppKit -framework WebKit \
      -o "${BUILD_DIR}/Hermes.${a}" "${HERE}/HermesApp/main.swift"
    slices="${slices} ${BUILD_DIR}/Hermes.${a}"
  done
  lipo -create -output "${SWIFT_OUT}" ${slices}
  rm -f ${slices}
else
  swiftc -O -framework AppKit -framework WebKit -o "${SWIFT_OUT}" "${HERE}/HermesApp/main.swift"
fi

say "Writing Info.plist (version ${VERSION})"
sed "s/__BUILD_VERSION__/${VERSION#v}/g" "${HERE}/Info.plist" > "${APP}/Contents/Info.plist"

# App icon — generated from the Hermes brand mark (static/favicon-512.svg).
build_icon() {
  # Prefer the dedicated app-icon source (official Hermes/Nous mark); fall back
  # to the WebUI brand favicon.
  local svg="${HERE}/appicon.svg"
  [ -f "${svg}" ] || svg="${REPO_ROOT}/static/favicon-512.svg"
  local png="${REPO_ROOT}/static/favicon-512.png"
  local iconset="${BUILD_DIR}/Hermes.iconset"
  rm -rf "${iconset}"; mkdir -p "${iconset}"
  # size:filename pairs the iconset format requires.
  local specs="16:icon_16x16 32:icon_16x16@2x 32:icon_32x32 64:icon_32x32@2x \
128:icon_128x128 256:icon_128x128@2x 256:icon_256x256 512:icon_256x256@2x \
512:icon_512x512 1024:icon_512x512@2x"
  for spec in ${specs}; do
    local px="${spec%%:*}"; local name="${spec##*:}"
    if command -v rsvg-convert >/dev/null 2>&1 && [ -f "${svg}" ]; then
      rsvg-convert -w "${px}" -h "${px}" "${svg}" -o "${iconset}/${name}.png"
    else  # fallback: rescale the 512 PNG (slight quality loss above 512)
      sips -z "${px}" "${px}" "${png}" --out "${iconset}/${name}.png" >/dev/null 2>&1
    fi
  done
  iconutil -c icns "${iconset}" -o "${APP}/Contents/Resources/AppIcon.icns"
  rm -rf "${iconset}"
}
if [ -f "${REPO_ROOT}/static/favicon-512.svg" ] || [ -f "${REPO_ROOT}/static/favicon-512.png" ]; then
  say "Generating AppIcon.icns from Hermes brand mark"
  build_icon
elif [ -f "${HERE}/AppIcon.icns" ]; then
  cp "${HERE}/AppIcon.icns" "${APP}/Contents/Resources/AppIcon.icns"
fi

# ── 5. Sign ──────────────────────────────────────────────────────────────────
if [ "${DO_SIGN}" = 1 ]; then
  [ -n "${SIGN_IDENTITY}" ] || { echo "SIGN_IDENTITY is required for --sign" >&2; exit 1; }
  local_ent="${HERE}/entitlements.plist"
  say "Signing nested code (Python + dylibs)…"
  # Sign every Mach-O inside the bundled Python(s) first, then the app. The glob
  # covers both single-arch (python) and universal (python-arm64/python-x86_64).
  find "${APP}/Contents/Resources/"python* \( -name '*.dylib' -o -name '*.so' -o -perm -u+x -type f \) -print0 \
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

# Submit to the notary service with retries — the multipart upload to Apple's
# S3 bucket occasionally dies with a connectTimeout/abortedUpload mid-flight, and
# that is worth retrying (a genuine rejection is rare once signing is correct and
# will simply exhaust the retries). Each retry re-uploads from scratch.
notarize() {  # $1 = path to submit (.zip or .dmg)
  local target="$1" attempt
  for attempt in 1 2 3; do
    if xcrun notarytool submit "${target}" --keychain-profile "${NOTARY_PROFILE}" --wait; then
      return 0
    fi
    say "Notary upload failed (attempt ${attempt}/3) — retrying in 20s…"
    sleep 20
  done
  echo "Notarization failed after 3 attempts for $(basename "${target}")" >&2
  return 1
}

if [ "${DO_NOTARIZE}" = 1 ]; then
  # 1) Notarize the .app itself, then staple it — so the app inside the shipped
  #    DMG carries its own ticket and validates offline.
  APPZIP="${BUILD_DIR}/Hermes-notarize.zip"
  ditto -c -k --keepParent "${APP}" "${APPZIP}"
  say "Submitting app to Apple notary service (profile: ${NOTARY_PROFILE})…"
  notarize "${APPZIP}"
  xcrun stapler staple "${APP}"
  rm -f "${APPZIP}"
  # 2) Build the DMG from the now-stapled app, then notarize + staple the DMG.
  make_dmg
  say "Submitting DMG to Apple notary service…"
  notarize "${DMG}"
  xcrun stapler staple "${DMG}"
elif [ "${DO_SIGN}" = 1 ]; then
  make_dmg
else
  make_dmg
fi

say "Done → ${DMG}"
say "App  → ${APP}"
