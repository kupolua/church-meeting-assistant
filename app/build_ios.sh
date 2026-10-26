#!/usr/bin/env bash
# Build the iOS app as an .ipa for TestFlight, named by the version in
# pubspec.yaml, next to the APKs:
#
#   ./build_ios.sh                       → <repo>/CMA_app/cma-app-<version>.ipa
#   ./build_ios.sh http://localhost:8010 → …-dev.ipa, pointed at a local server
#
# Needs, once, on this Mac (see README «iOS»): full Xcode (not just the
# Command Line Tools), CocoaPods, and an Apple Developer account added in
# Xcode → Settings → Accounts, with its Team ID in ios/ExportOptions.plist.
# iPhones get the build through TestFlight (the in-app update strip is
# Android-only: there is no APK on iOS).
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$PATH:/opt/homebrew/bin"
API="${1:-}"
VERSION="$(sed -n 's/^version: *//p' pubspec.yaml | tr -d '[:space:]')"
DEST="${CMA_APK_DIR:-$(cd .. && pwd)/CMA_app}"
mkdir -p "$DEST"
[[ -d /Applications/Xcode.app ]] || { echo "Xcode is not installed — App Store → Xcode, then: sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer" >&2; exit 1; }
command -v pod >/dev/null || { echo "CocoaPods missing — brew install cocoapods" >&2; exit 1; }
[[ -f ios/ExportOptions.plist ]] || { echo "ios/ExportOptions.plist missing — copy ios/ExportOptions.example.plist and put the Team ID in" >&2; exit 1; }
if [[ -n "$API" ]]; then
    flutter build ipa --export-options-plist=ios/ExportOptions.plist --dart-define=CMA_API="$API"
    OUT="$DEST/cma-app-$VERSION-dev.ipa"
else
    flutter build ipa --export-options-plist=ios/ExportOptions.plist
    OUT="$DEST/cma-app-$VERSION.ipa"
fi
cp build/ios/ipa/*.ipa "$OUT"
echo "→ $OUT ($(du -h "$OUT" | cut -f1))"
echo "upload: xcrun altool --upload-app -f '$OUT' -t ios --apiKey <KEY_ID> --apiIssuer <ISSUER_ID>   (or drag into Transporter.app)"
