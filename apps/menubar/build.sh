#!/bin/sh
# Builds build/Recall.app: the menu bar and the recall window. The menu bar runs
# `recall status --json` from this checkout through uv, so it shows the code here rather than the
# PyPI release. The window talks to `recall api`; it starts one from the installed recall, or from
# a checkout: this one by default, another with RECALL_DEV_PROJECT=/path/to/recall ./build.sh, or
# only the installed recall with RECALL_DEV_PROJECT=none.
set -e
cd "$(dirname "$0")"
repo="$(cd ../.. && pwd)"
uv="$(command -v uv)"
app=build/Recall.app
api=""
dev="${RECALL_DEV_PROJECT:-$repo}"
if [ "$dev" != none ]; then
  api="<key>RecallAPICommand</key>
  <array>
    <string>$uv</string><string>run</string><string>--frozen</string><string>--project</string><string>$dev</string>
    <string>recall</string><string>api</string>
  </array>"
fi

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
swiftc -O -o "$app/Contents/MacOS/Recall" Sources/*.swift -framework AppKit -framework SwiftUI
# Paper Mono, the site's typeface (SIL OFL): the woff2 the site serves, as a TTF the app loads.
uvx --quiet --with brotli --from fonttools fonttools ttLib.woff2 decompress \
  -o "$app/Contents/Resources/PaperMono.ttf" Resources/PaperMono-wght.woff2
cp Resources/OFL.txt "$app/Contents/Resources/PaperMono-OFL.txt"

cat > "$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.rererecall.app</string>
  <key>CFBundleName</key><string>Recall</string>
  <key>CFBundleExecutable</key><string>Recall</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.0.1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>RecallCommand</key>
  <array>
    <string>$uv</string><string>run</string><string>--frozen</string><string>--project</string><string>$repo</string>
    <string>recall</string><string>status</string><string>--json</string>
  </array>
  $api
</dict>
</plist>
EOF
# The app icon, drawn by the app itself from the same mark as the menu bar, then packed by iconutil.
icons="$(mktemp -d)/AppIcon.iconset"
"$app/Contents/MacOS/Recall" --render-icon "$icons"
iconutil -c icns -o "$app/Contents/Resources/AppIcon.icns" "$icons"
rm -rf "$(dirname "$icons")"
codesign --force --sign - "$app" >/dev/null 2>&1 || true
echo "Built $(pwd)/$app"
