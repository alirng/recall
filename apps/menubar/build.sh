#!/bin/sh
# Builds build/Recall.app: the menu bar and the recall window. By default the app runs the installed
# `recall` (~/.local/bin, /opt/homebrew/bin or /usr/local/bin) for `recall status --json` and
# `recall api`, and nothing about this checkout is written into the app. To build an app that runs a
# checkout through uv instead (it then shows the code there rather than the PyPI release), opt in:
#   RECALL_DEV_PROJECT=/path/to/recall ./build.sh
# That bakes the checkout's path into Info.plist (RecallCommand, RecallAPICommand), so such a build
# is for this Mac only and not to be passed on.
set -e
cd "$(dirname "$0")"
uv="$(command -v uv)"
app=build/Recall.app
dev="${RECALL_DEV_PROJECT:-}"
[ "$dev" = none ] && dev=""
cmd=""
api=""
if [ -n "$dev" ]; then
  dev="$(cd "$dev" && pwd)"
  cmd="<key>RecallCommand</key>
  <array>
    <string>$uv</string><string>run</string><string>--frozen</string><string>--project</string><string>$dev</string>
    <string>recall</string><string>status</string><string>--json</string>
  </array>"
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
uvx --quiet --with brotli==1.2.1 --from fonttools==4.68.0 fonttools ttLib.woff2 decompress \
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
  $cmd
  $api
</dict>
</plist>
EOF
# The app icon, drawn by the app itself from the same mark as the menu bar, then packed by iconutil.
icons="$(mktemp -d)/AppIcon.iconset"
"$app/Contents/MacOS/Recall" --render-icon "$icons"
iconutil -c icns -o "$app/Contents/Resources/AppIcon.icns" "$icons"
rm -rf "$(dirname "$icons")"
codesign --force --sign - "$app" >/dev/null
echo "Built $(pwd)/$app"
