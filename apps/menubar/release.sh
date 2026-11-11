#!/bin/bash
# Builds Recall for release: universal, signed with a Developer ID, notarized and stapled, as a
# .dmg, plus the signature Sparkle needs. It publishes nothing: it ends by printing what's left to do.
#
#   ./release.sh check     what this Mac still needs
#   ./release.sh keys      make Sparkle's update key (once, ever) and print how to back it up
#   ./release.sh           the release build, in build/out/
#   ./release.sh test      signed and notarized from this checkout as it is, named -test, for trying it out before a merge
#   ./release.sh dry       the same with an ad hoc signature and no notarizing, to test the packaging
#
# The signing certificate is found in this Mac's keychain and never written down here. Override with
# RECALL_SIGN_ID, and RECALL_NOTARY_PROFILE for the notarytool profile (default recall-desktop).
set -euo pipefail
cd "$(dirname "$0")"

mode="${1:-release}"
profile="${RECALL_NOTARY_PROFILE:-recall-desktop}"
repo=alirng/recall
app="build/Recall.app"
out=build/out
version="$(sed -n 's/^version="\(.*\)"$/\1/p' build.sh | head -1)"
build_number="$(sed -n 's/^build_number="\(.*\)"$/\1/p' build.sh | head -1)"
[ -n "$version" ] && [ -n "$build_number" ] || { echo "build.sh must set version= and build_number="; exit 1; }
tag="desktop-v$version"
suffix=""
[ "$mode" != test ] || suffix="-test"
dmg="$out/Recall-$version$suffix.dmg"
sparkle_bin() { echo "build/vendor/Sparkle/bin/$1"; }

identity() {
  if [ -n "${RECALL_SIGN_ID:-}" ]; then echo "$RECALL_SIGN_ID"; return; fi
  local found
  found="$(security find-identity -v -p codesigning | sed -n 's/.*"\(Developer ID Application: .*\)"$/\1/p')"
  [ "$(printf '%s\n' "$found" | grep -c .)" -le 1 ] || { echo "several Developer ID certificates: set RECALL_SIGN_ID" >&2; exit 1; }
  printf '%s' "$found"
}

# Its own Keychain entry, so another project's Sparkle key on this Mac can never be picked up by mistake.
key_account=recall-desktop
public_key() { [ -x "$(sparkle_bin generate_keys)" ] && "$(sparkle_bin generate_keys)" --account "$key_account" -p 2>/dev/null || true; }

need_sparkle() {
  [ -x "$(sparkle_bin generate_keys)" ] || ./build.sh >/dev/null
  [ -x "$(sparkle_bin generate_keys)" ] || { echo "Sparkle's tools aren't in build/vendor/Sparkle/bin"; exit 1; }
}

case "$mode" in
check)
  bad=0
  if [ -n "$(identity)" ]; then echo "ok   signing certificate is installed"; else echo "need the Developer ID Application certificate"; bad=1; fi
  if xcrun notarytool history --keychain-profile "$profile" >/dev/null 2>&1; then echo "ok   notary profile $profile works"; else echo "need the notary profile: xcrun notarytool store-credentials $profile"; bad=1; fi
  need_sparkle
  if [ -n "$(public_key)" ]; then echo "ok   update key is in the keychain"; else echo "need the update key: ./release.sh keys"; bad=1; fi
  echo "version $version (build $build_number), tag $tag"
  exit $bad ;;
keys)
  need_sparkle
  "$(sparkle_bin generate_keys)" --account "$key_account"
  echo
  echo "Back the private key up now. It is the only way to ship updates to people who already have the app:"
  echo "  $(pwd)/$(sparkle_bin generate_keys) --account $key_account -x ~/recall-update-key.txt"
  echo "Put that file in your password manager, then delete it from your home folder."
  exit 0 ;;
release | dry | test) ;;
*) sed -n 2,9p "$0"; exit 1 ;;
esac

# --- build -------------------------------------------------------------------------------------
if [ "$mode" = release ]; then
  # A release must be exactly a commit on main, and the tag must land on that commit: other chats merge to main meanwhile.
  [ -z "$(git status --porcelain)" ] || { echo "This checkout has uncommitted changes. Build a release from a clean checkout of main."; exit 1; }
  git fetch -q origin main
  git merge-base --is-ancestor HEAD origin/main || { echo "HEAD isn't on origin/main. Merge first, then build from that commit."; exit 1; }
fi
if [ "$mode" != dry ]; then
  id="$(identity)"
  [ -n "$id" ] || { echo "No Developer ID Application certificate on this Mac: ./release.sh check"; exit 1; }
  need_sparkle
  key="$(public_key)"
  [ -n "$key" ] || { echo "No update key, and a first public build without one can never be updated: ./release.sh keys"; exit 1; }
else
  id="-"
  key="${RECALL_SPARKLE_PUBLIC_KEY:-}"
fi
RECALL_UNIVERSAL=1 RECALL_SPARKLE_PUBLIC_KEY="$key" RECALL_DEV_PROJECT=none ./build.sh
if /usr/libexec/PlistBuddy -c 'Print :RecallCommand' "$app/Contents/Info.plist" >/dev/null 2>&1; then
  echo "This build points at a checkout. Refusing to package it."; exit 1
fi
archs="$(lipo -archs "$app/Contents/MacOS/Recall")"
case "$archs" in *arm64*x86_64* | *x86_64*arm64*) ;; *) echo "Not universal: $archs"; exit 1 ;; esac

# --- sign, inside out, never --deep (Sparkle's own instructions for builds outside Xcode) ----------
sign() {
  if [ "$id" = "-" ]; then codesign --force --sign - "$@"
  else codesign --force --sign "$id" --options runtime --timestamp "$@"; fi
}
sparkle="$app/Contents/Frameworks/Sparkle.framework"
if [ -d "$sparkle" ]; then
  v="$sparkle/Versions/B"
  [ -d "$v/XPCServices/Installer.xpc" ] && sign "$v/XPCServices/Installer.xpc"
  [ -d "$v/XPCServices/Downloader.xpc" ] && sign --preserve-metadata=entitlements "$v/XPCServices/Downloader.xpc"
  sign "$v/Autoupdate"
  sign "$v/Updater.app"
  sign "$sparkle"
fi
sign "$app"
codesign --verify --strict --verbose=1 "$app"

rm -rf "$out"; mkdir -p "$out"
notarize() {
  xcrun notarytool submit "$1" --keychain-profile "$profile" --wait --timeout 30m
}
if [ "$mode" != dry ]; then
  ditto -c -k --keepParent "$app" "$out/app.zip"
  notarize "$out/app.zip"
  xcrun stapler staple "$app"
  rm "$out/app.zip"
fi

# --- .dmg ----------------------------------------------------------------------------------------
# dmgbuild writes the window's layout itself, so nothing has to script Finder.
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
"$app/Contents/MacOS/Recall" --render-dmg "$stage/background.png" 1
"$app/Contents/MacOS/Recall" --render-dmg "$stage/background@2x.png" 2
tiffutil -cathidpicheck "$stage/background.png" "$stage/background@2x.png" -out "$stage/background.tiff" 2>/dev/null
uvx --quiet --from dmgbuild==1.6.7 dmgbuild -s dmg-settings.py -D app="$app" -D background="$stage/background.tiff" "Recall" "$dmg"
if [ "$mode" != dry ]; then
  sign "$dmg"
  notarize "$dmg"
  xcrun stapler staple "$dmg"
  spctl --assess --type open --context context:primary-signature --verbose=2 "$dmg"
  spctl --assess --type execute --verbose=2 "$app"
fi

# --- the update feed's entry -----------------------------------------------------------------------
url="https://github.com/$repo/releases/download/$tag/Recall-$version.dmg"
if [ "$mode" != dry ]; then
  edsig="$("$(sparkle_bin sign_update)" --account "$key_account" "$dmg")"
else
  edsig="sparkle:edSignature=\"dry-run\" length=\"$(stat -f%z "$dmg")\""
fi
now="$(LC_ALL=C date -u '+%a, %d %b %Y %H:%M:%S +0000')"
item="    <item>
      <title>$version</title>
      <pubDate>$now</pubDate>
      <link>https://rererecall.com/changelog/desktop/$version/</link>
      <sparkle:version>$build_number</sparkle:version>
      <sparkle:shortVersionString>$version</sparkle:shortVersionString>
      <sparkle:minimumSystemVersion>14.0</sparkle:minimumSystemVersion>
      <enclosure url=\"$url\" $edsig type=\"application/octet-stream\"/>
    </item>"
ITEM="$item" python3 -I - "$out/appcast.xml" <<'PY'
import os, sys, urllib.request
head = '<?xml version="1.0" encoding="utf-8"?>\n<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">\n  <channel>\n    <title>Recall</title>\n'
tail = "  </channel>\n</rss>\n"
older = ""
try:  # the feed keeps every version, newest first
    live = urllib.request.urlopen("https://rererecall.com/desktop/appcast.xml", timeout=15).read().decode()
    if "<item>" in live:
        older = "    <item>" + live.split("<item>", 1)[1].rsplit("</channel>", 1)[0].rstrip() + "\n"
except Exception:
    pass
open(sys.argv[1], "w", encoding="utf-8").write(head + os.environ["ITEM"] + "\n" + older + tail)
PY

bytes="$(stat -f%z "$dmg")"
echo
echo "Built $(pwd)/$dmg ($((bytes / 1024 / 1024)) MB)"
echo "Update feed: $(pwd)/$out/appcast.xml"
if [ "$mode" = dry ]; then echo "Dry run: ad hoc signature, not notarized, not for anyone else."; exit 0; fi
if [ "$mode" = test ]; then echo "Test build: signed and notarized from this checkout, for trying out. Nothing is published."; exit 0; fi
echo
echo "Nothing has been published. To release, in this order:"
echo "  gh release create $tag \"$(pwd)/$dmg\" --repo $repo --target $(git rev-parse HEAD) --title \"Recall $version\" --latest=false --notes-file <notes>"
echo "  copy appcast.xml to the site's /desktop/appcast.xml and push the site"
echo "(--latest=false matters: a desktop release must not become the repo's Latest.)"
