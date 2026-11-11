#!/bin/sh
# Builds build/Recall.app: the menu bar and the recall window. By default the app runs the installed
# `recall` (~/.local/bin, /opt/homebrew/bin or /usr/local/bin) for `recall status --json` and
# `recall api`, and nothing about this checkout is written into the app. To build an app that runs a
# checkout through uv instead (it then shows the code there rather than the PyPI release), opt in:
#   RECALL_DEV_PROJECT=/path/to/recall ./build.sh
# That bakes the checkout's path into Info.plist (RecallCommand, RecallAPICommand), so such a build
# is for this Mac only and not to be passed on.
#
# App updates use Sparkle, which checks https://rererecall.com/desktop/appcast.xml. A build with
# RECALL_SPARKLE_PUBLIC_KEY=<base64 EdDSA public key> in the environment can verify updates; a build
# without it still runs, with app updates off. The private key never comes near this script.

# Bump these two for a release (release.sh reads them). Sparkle compares build_number, an integer; people read version.
version="0.1.0"
build_number="1"

# Sparkle, pinned: the newest stable release that was at least two weeks old when it was chosen
# (2.10.1, 25 Oct 2026), downloaded into build/vendor (git-ignored) and checked against this hash.
SPARKLE_VERSION=2.10.1
SPARKLE_SHA256=1a856d2a9edb8e628de88121d7be04cf0e2167f8641650b78fca00b14244cf27
set -e
cd "$(dirname "$0")"
uv="$(command -v uv)"
app="build/Recall.app"
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

# Sparkle: fetched once into build/vendor, checked against the pinned hash, unpacked fresh each build.
vendor=build/vendor
mkdir -p "$vendor"
archive="$vendor/Sparkle-$SPARKLE_VERSION.tar.xz"
if [ ! -f "$archive" ]; then
  curl -fsSL -o "$archive" "https://github.com/sparkle-project/Sparkle/releases/download/$SPARKLE_VERSION/Sparkle-$SPARKLE_VERSION.tar.xz"
fi
if ! echo "$SPARKLE_SHA256  $archive" | shasum -a 256 -c - >/dev/null; then
  rm -f "$archive"
  echo "Sparkle download does not match the pinned sha256" >&2
  exit 1
fi
# The whole archive: the framework to link and bin/ (sign_update, generate_appcast) for release.sh.
rm -rf "$vendor/Sparkle"
mkdir -p "$vendor/Sparkle"
tar -xf "$archive" -C "$vendor/Sparkle"

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources" "$app/Contents/Frameworks"
# The framework goes in first: the binary links it by @rpath and build.sh runs it below for the icon.
# A non-sandboxed app doesn't need Sparkle's XPC services.
ditto "$vendor/Sparkle/Sparkle.framework" "$app/Contents/Frameworks/Sparkle.framework"
rm -rf "$app/Contents/Frameworks/Sparkle.framework/Versions/B/XPCServices" "$app/Contents/Frameworks/Sparkle.framework/XPCServices"
# RECALL_UNIVERSAL=1 builds arm64 and x86_64 and joins them with lipo.
compile() {
  swiftc -O -target "$1-apple-macos14.0" -o "$2" Sources/*.swift -framework AppKit -framework SwiftUI \
    -F "$vendor/Sparkle" -framework Sparkle -Xlinker -rpath -Xlinker @executable_path/../Frameworks
}
if [ "${RECALL_UNIVERSAL:-}" = 1 ]; then
  compile arm64 "$vendor/Recall-arm64"
  compile x86_64 "$vendor/Recall-x86_64"
  lipo -create -output "$app/Contents/MacOS/Recall" "$vendor/Recall-arm64" "$vendor/Recall-x86_64"
else
  compile "$(uname -m)" "$app/Contents/MacOS/Recall"
fi
# Paper Mono, the site's typeface (SIL OFL): the woff2 the site serves, as a TTF the app loads.
uvx --quiet --with brotli==1.2.1 --from fonttools==4.68.0 fonttools ttLib.woff2 decompress \
  -o "$app/Contents/Resources/PaperMono.ttf" Resources/PaperMono-wght.woff2
cp Resources/OFL.txt "$app/Contents/Resources/PaperMono-OFL.txt"

key=""
[ -n "${RECALL_SPARKLE_PUBLIC_KEY:-}" ] && key="<key>SUPublicEDKey</key><string>$RECALL_SPARKLE_PUBLIC_KEY</string>"

cat > "$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.rererecall.app</string>
  <key>CFBundleName</key><string>Recall</string>
  <key>CFBundleDisplayName</key><string>Recall</string>
  <key>CFBundleExecutable</key><string>Recall</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$version</string>
  <key>CFBundleVersion</key><string>$build_number</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>SUFeedURL</key><string>https://rererecall.com/desktop/appcast.xml</string>
  <key>SUEnableAutomaticChecks</key><true/>
  <key>SUAutomaticallyUpdate</key><false/>
  <key>SUEnableSystemProfiling</key><false/>
  $key
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
# Signed from the inside out, never with --deep. A dev build is signed ad hoc; a release signs with a
# Developer ID the same way, adding --options runtime --timestamp to each line.
fw="$app/Contents/Frameworks/Sparkle.framework"
codesign --force --sign - "$fw/Versions/B/Autoupdate" >/dev/null
codesign --force --sign - "$fw/Versions/B/Updater.app" >/dev/null
codesign --force --sign - "$fw" >/dev/null
codesign --force --sign - "$app" >/dev/null
echo "Built $(pwd)/$app"
