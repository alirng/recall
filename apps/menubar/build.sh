#!/bin/sh
# Builds build/Recall.app: the menu bar status prototype. It runs `recall status --json`
# from this checkout through uv, so it shows the code here rather than the PyPI release.
set -e
cd "$(dirname "$0")"
repo="$(cd ../.. && pwd)"
uv="$(command -v uv)"
app=build/Recall.app

rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
swiftc -O -o "$app/Contents/MacOS/Recall" Sources/main.swift -framework AppKit

cat > "$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.rererecall.menubar</string>
  <key>CFBundleName</key><string>Recall</string>
  <key>CFBundleExecutable</key><string>Recall</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.0.1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>RecallCommand</key>
  <array>
    <string>$uv</string><string>run</string><string>--frozen</string><string>--project</string><string>$repo</string>
    <string>recall</string><string>status</string><string>--json</string>
  </array>
</dict>
</plist>
EOF
codesign --force --sign - "$app" >/dev/null 2>&1 || true
echo "Built $(pwd)/$app"
