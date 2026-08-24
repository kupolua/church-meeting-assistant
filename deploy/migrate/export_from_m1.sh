#!/usr/bin/env bash
#
# Export everything the VPS needs, from the M1. READ-ONLY on this machine:
# pg_dump, Qdrant snapshots and a file copy. Nothing here drops, truncates or
# moves anything — if the migration is abandoned halfway, the laptop is exactly
# as it was.
#
#   ./deploy/migrate/export_from_m1.sh [OUTDIR]
#
# Default OUTDIR is ./migrate-out, which is gitignored. Expect ~2 GB: the audio
# is 94% of it.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR"

OUT="${1:-$PROJECT_DIR/migrate-out}"
PG_CONTAINER="${CMA_PG_CONTAINER:-cma-postgres}"

# Same trick as restart_dev.sh: with Docker Desktop, `docker` is a shell alias
# that a script does not inherit, so resolve the real binary or the first
# pg_dumpall below dies with "command not found".
DOCKER="$(command -v docker 2>/dev/null || true)"
if [[ -z "$DOCKER" ]]; then
    for cand in \
        /Applications/Docker.app/Contents/Resources/bin/docker \
        /usr/local/bin/docker /opt/homebrew/bin/docker; do
        [[ -x "$cand" ]] && { DOCKER="$cand"; break; }
    done
fi
[[ -n "$DOCKER" ]] || { echo "  ✗ не знайдено бінарник docker" >&2; exit 1; }
QDRANT_URL="${CMA_QDRANT_URL:-http://localhost:6333}"
COLLECTIONS=(cma_protocols cma_analyses cma_turns cma_protocol_full)

# The container was created with POSTGRES_USER=cma, so there is no `postgres`
# role at all — the usual default would fail on the very first command.
PG_SUPER="${CMA_PG_SUPERUSER:-cma}"

# `|| true` matters: with `set -euo pipefail`, a key that is simply absent from
# .env makes grep exit 1, which kills the whole script at the assignment — no
# message, no partial output, just a silent stop. DATA_ROOT is exactly such a
# key on the M1, so this is the normal case, not the edge one.
env_val() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | sed 's/#.*//' | tr -d '"' | xargs || true; }
DB_NAME="$(env_val DB_NAME)";  DB_NAME="${DB_NAME:-cma}"
# Unset on the M1 today — tenant_paths.data_root() then defaults to <repo>/data.
DATA_ROOT="$(env_val DATA_ROOT)"; DATA_ROOT="${DATA_ROOT:-$PROJECT_DIR/data}"

say() { echo "  · $*"; }
ok()  { echo "  ✓ $*"; }

echo "Експорт із M1 → $OUT"
mkdir -p "$OUT/qdrant"

# ─── 1. Postgres ─────────────────────────────────────────────
# Roles are dumped separately: pg_dumpall -r carries cma_app, and without it the
# restored database has policies referring to a role that does not exist, which
# surfaces much later as "permission denied" rather than as a failed restore.
say "pg_dumpall -r (ролі)…"
"$DOCKER" exec "$PG_CONTAINER" pg_dumpall -U "$PG_SUPER" --roles-only > "$OUT/roles.sql"
ok "ролі → roles.sql ($(wc -c < "$OUT/roles.sql" | tr -d ' ') байт)"

say "pg_dump ${DB_NAME}…"
"$DOCKER" exec "$PG_CONTAINER" pg_dump -U "$PG_SUPER" --format=custom "$DB_NAME" > "$OUT/cma.dump"
ok "база → cma.dump ($(du -h "$OUT/cma.dump" | cut -f1))"

# ─── 2. Qdrant ───────────────────────────────────────────────
# One snapshot per collection rather than a whole-node snapshot: this instance
# also holds construction_*, cookbook and test_collection, which belong to
# unrelated projects and have no business on a church's server.
for c in "${COLLECTIONS[@]}"; do
    say "знімок ${c}…"
    name="$(curl -sf -X POST "$QDRANT_URL/collections/$c/snapshots" \
            | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["name"])')"
    curl -sf "$QDRANT_URL/collections/$c/snapshots/$name" -o "$OUT/qdrant/$c.snapshot"
    ok "$c → qdrant/$c.snapshot ($(du -h "$OUT/qdrant/$c.snapshot" | cut -f1))"
    # Delete only the snapshot we just made on the source — not the collection.
    curl -sf -X DELETE "$QDRANT_URL/collections/$c/snapshots/$name" >/dev/null
done

# ─── 3. Artifacts ────────────────────────────────────────────
# Left as a directory, not a tarball: rsync can resume 2 GB over a tunnel, a
# half-transferred tarball is worth nothing.
say "артефакти з ${DATA_ROOT}…"
[ -d "$DATA_ROOT" ] || { echo "  ✗ DATA_ROOT не існує: $DATA_ROOT" >&2; exit 1; }
du -sh "$DATA_ROOT" | sed 's/^/  · розмір: /'
ok "готово до rsync (крок 5 ранбука) — файли не копіювалися сюди"

echo
echo "Далі — docs/vps_deploy.md, крок 5."
echo "Джерело недоторкане: тут лише dump, знімки й читання."
