#!/usr/bin/env bash
#
# Pull a backup of the VPS onto this machine.
#
#   ./deploy/backup_from_vps.sh [DEST]      (default: ~/cma-backups)
#
# RUN IT FROM THE M1, not on the server. A copy that lives beside the original
# survives a deleted file and nothing else — not a dead disk, not a lost VPS,
# not a mistaken `rm -rf`. The laptop is a different machine in a different
# building, which is the only property that makes this a backup.
#
# Nothing here needs root on either side. It reads the database over the
# WireGuard tunnel and the artifacts over SSH as `cma`, and the superuser
# password comes from the VPS's own .env rather than being stored here — this
# script is committed, and a password in a repository is not a password.
#
# WHY pg_dump RUNS IN A CONTAINER: macOS has no postgresql-client, and the VPS
# host has none either (Postgres lives in a container there). The M1's own
# cma-postgres container has the right client version and can reach 10.10.0.1
# through the tunnel, so it is borrowed as a tool. Version matters: dumping a
# server with an older client silently omits newer features.

set -euo pipefail

DEST="${1:-$HOME/cma-backups}"
VPS="${CMA_VPS_HOST:-cma@10.10.0.1}"
DB_HOST="${CMA_VPS_DB:-10.10.0.1}"
KEEP="${CMA_BACKUP_KEEP:-14}"          # how many dated dumps to retain

# Docker Desktop presents `docker` as a shell alias that scripts do not inherit.
DOCKER="$(command -v docker 2>/dev/null || true)"
[[ -x "$DOCKER" ]] || DOCKER=/Applications/Docker.app/Contents/Resources/bin/docker
[[ -x "$DOCKER" ]] || { echo "✗ не знайдено docker" >&2; exit 1; }
PG_CONTAINER="${CMA_PG_CONTAINER:-cma-postgres}"

STAMP="$(date +%F-%H%M)"
mkdir -p "$DEST/artifacts"

say() { echo "  · $*"; }
ok()  { echo "  ✓ $*"; }

# Read, never echo, never store. The VPS .env is chmod 600 and owned by cma;
# reaching it requires the same SSH key this script already needs.
say "беру пароль суперюзера з VPS…"
PGPW="$(ssh -o BatchMode=yes "$VPS" 'grep "^POSTGRES_SUPERUSER_PASSWORD=" /srv/cma/.env | cut -d= -f2-')"
[[ -n "$PGPW" ]] || { echo "✗ пароль не прочитався" >&2; exit 1; }

# ─── Roles, then the database ────────────────────────────────
# Roles separately: the dump's policies and grants name cma_app, and restoring
# into a cluster without it produces errors that look ignorable and are not.
say "pg_dumpall --roles-only…"
"$DOCKER" exec -e PGPASSWORD="$PGPW" "$PG_CONTAINER" \
    pg_dumpall -h "$DB_HOST" -U cma --roles-only > "$DEST/roles-$STAMP.sql"
ok "roles-$STAMP.sql ($(wc -c < "$DEST/roles-$STAMP.sql" | tr -d ' ') байт)"

say "pg_dump cma…"
"$DOCKER" exec -e PGPASSWORD="$PGPW" "$PG_CONTAINER" \
    pg_dump -h "$DB_HOST" -U cma --format=custom cma > "$DEST/cma-$STAMP.dump"
ok "cma-$STAMP.dump ($(du -h "$DEST/cma-$STAMP.dump" | cut -f1))"

# A dump that cannot be listed cannot be restored. Cheap to check now, and the
# alternative is finding out on the day it is needed.
TABLES="$("$DOCKER" exec -i "$PG_CONTAINER" pg_restore -l < "$DEST/cma-$STAMP.dump" \
          2>/dev/null | grep -c 'TABLE DATA' || true)"
[[ "$TABLES" -ge 8 ]] || { echo "✗ у дампі лише $TABLES таблиць — не довіряю" >&2; exit 1; }
ok "дамп читається: $TABLES таблиць із даними"

# ─── Artifacts ───────────────────────────────────────────────
# --delete so the mirror matches the source: without it a meeting deleted on the
# server lives on here forever, and the copy slowly stops being a copy.
say "rsync артефактів…"
rsync -az --partial --delete \
    -e "ssh -o BatchMode=yes -o ConnectTimeout=15" \
    "$VPS:/srv/cma-data/artifacts/" "$DEST/artifacts/"
MEETINGS="$(ls "$DEST/artifacts/tenants/default/meetings" 2>/dev/null | wc -l | tr -d ' ')"
ok "артефакти: $MEETINGS зустрічей, $(du -sh "$DEST/artifacts" | cut -f1)"

# ─── Rotation ────────────────────────────────────────────────
# Dated dumps only. The artifact mirror is one directory that is kept current;
# keeping dated copies of 1.9 GB would fill this disk instead of the server's.
# `ls` on a pattern that matches nothing exits non-zero, and with `set -o
# pipefail` that kills the whole script AFTER the backup already succeeded —
# a green backup reported as a failure. Each pattern is guarded, and each
# handled separately rather than with a brace expansion that produces one
# never-matching glob per prefix.
for pat in "cma-*.dump" "roles-*.sql"; do
    { ls -1t "$DEST"/$pat 2>/dev/null || true; } | tail -n +$((KEEP + 1)) | while read -r old; do
        [[ -n "$old" ]] && rm -f "$old" && say "прибрано старий $(basename "$old")"
    done
done

# A zero-byte dump is a failed attempt, not a backup, and leaving it in the
# rotation means it can become the newest thing here.
find "$DEST" -maxdepth 1 -type f -empty \( -name 'cma-*.dump' -o -name 'roles-*.sql' \) \
    -print -delete | while read -r f; do say "прибрано порожній $(basename "$f")"; done

echo
ok "бекап у $DEST ($(du -sh "$DEST" | cut -f1))"
echo "  відновлення: deploy/migrate/import_to_vps.sh або pg_restore -d cma <dump>"
