#!/usr/bin/env bash
# Publish an APK so the phones offer it as an update (routes/api_app.py):
#
#   ./deploy/publish_apk.sh                       → the newest CMA_app/cma-app-*.apk
#   ./deploy/publish_apk.sh CMA_app/cma-app-0.7.0+18.apk
#
# Copies the file to <DATA_ROOT>/app/ on the VPS, keeps the two newest builds
# there, and prints what /api/v1/app/latest now says. The server code needs no
# restart — it lists the directory on every request.
set -euo pipefail
cd "$(dirname "$0")/.."
HOST="${CMA_VPS:-cma@10.10.0.1}"
DIR="${CMA_APK_REMOTE_DIR:-/srv/cma-data/artifacts/app}"
URL="${CMA_URL:-https://cma.rechurch.org.ua}"

APK="${1:-$(ls CMA_app/cma-app-*+*.apk | grep -v -- '-dev.apk' | sort -t+ -k2 -n | tail -1)}"
[[ "$(basename "$APK")" =~ ^cma-app-[0-9]+\.[0-9]+\.[0-9]+\+[0-9]+\.apk$ ]] || { echo "not a release name: $APK" >&2; exit 1; }

ssh "$HOST" "mkdir -p '$DIR'"
scp -q "$APK" "$HOST:$DIR/"
ssh "$HOST" "cd '$DIR' && ls cma-app-*.apk | sort -t+ -k2 -n | head -n -2 | xargs -r rm -v"
echo "→ $URL/api/v1/app/latest"
curl -s "$URL/api/v1/app/latest"; echo
