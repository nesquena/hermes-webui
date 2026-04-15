#!/bin/bash
set -e

APP_NAME="HermesAgent"
DISPLAY_NAME="Hermes Agent"
BUILD_DIR=".build/release"
APP_BUNDLE="$DISPLAY_NAME.app"

echo "→ Building..."
swift build -c release

echo "→ Bundling $APP_BUNDLE..."
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources"

cp "$BUILD_DIR/$APP_NAME" "$APP_BUNDLE/Contents/MacOS/"

echo "→ Converting icon..."
ICONSET="AppIcon.iconset"
mkdir -p "$ICONSET"
sips -z 16 16     "Hermes Icon.png" --out "$ICONSET/icon_16x16.png"
sips -z 32 32     "Hermes Icon.png" --out "$ICONSET/icon_16x16@2x.png"
sips -z 32 32     "Hermes Icon.png" --out "$ICONSET/icon_32x32.png"
sips -z 64 64     "Hermes Icon.png" --out "$ICONSET/icon_32x32@2x.png"
sips -z 128 128   "Hermes Icon.png" --out "$ICONSET/icon_128x128.png"
sips -z 256 256   "Hermes Icon.png" --out "$ICONSET/icon_128x128@2x.png"
sips -z 256 256   "Hermes Icon.png" --out "$ICONSET/icon_256x256.png"
sips -z 512 512   "Hermes Icon.png" --out "$ICONSET/icon_256x256@2x.png"
sips -z 512 512   "Hermes Icon.png" --out "$ICONSET/icon_512x512.png"
sips -z 1024 1024 "Hermes Icon.png" --out "$ICONSET/icon_512x512@2x.png"
iconutil -c icns "$ICONSET" -o "$APP_BUNDLE/Contents/Resources/AppIcon.icns"
rm -rf "$ICONSET"

cat > "$APP_BUNDLE/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>$DISPLAY_NAME</string>
    <key>CFBundleIdentifier</key>
    <string>com.local.$APP_NAME</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>$APP_NAME</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <false/>
</dict>
</plist>
PLIST

echo "→ Installing to Applications..."
rm -rf "/Applications/$APP_BUNDLE"
cp -r "$APP_BUNDLE" "/Applications/$APP_BUNDLE"

echo "→ Busting icon cache..."
touch "/Applications/$APP_BUNDLE"
sudo find /private/var/folders -name "com.apple.dock.iconcache" -exec rm {} \; 2>/dev/null || true
sudo rm -rf /Library/Caches/com.apple.iconservices.store 2>/dev/null || true
killall Dock
killall Finder

echo "✓ Done! Run with: open \"$APP_BUNDLE\""
