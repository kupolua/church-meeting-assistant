#!/usr/bin/env bash
# Build the release APK and drop it where the phone is loaded from, named by
# the version in pubspec.yaml (bump it there — the name follows):
#
#   ./build_apk.sh                       → ~/Documents/ReChurch/CMA_app/cma-app-0.1.0+1.apk
#   ./build_apk.sh http://10.0.2.2:8010  → …-dev.apk, pointed at a local server
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$PATH:/opt/homebrew/bin"
API="${1:-}"
VERSION="$(sed -n 's/^version: *//p' pubspec.yaml | tr -d '[:space:]')"
DEST="${CMA_APK_DIR:-$HOME/Documents/ReChurch/CMA_app}"
mkdir -p "$DEST"
if [[ -n "$API" ]]; then
    flutter build apk --release --dart-define=CMA_API="$API"
    OUT="$DEST/cma-app-$VERSION-dev.apk"
else
    flutter build apk --release
    OUT="$DEST/cma-app-$VERSION.apk"
fi
cp build/app/outputs/flutter-apk/app-release.apk "$OUT"
echo "→ $OUT ($(du -h "$OUT" | cut -f1))"
