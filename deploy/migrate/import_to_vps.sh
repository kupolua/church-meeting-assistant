#!/usr/bin/env bash
#
# Restore the M1's export into the VPS's fresh containers. Run ON THE VPS,
# after `docker compose -f deploy/docker-compose.vps.yml up -d`.
#
#   ./deploy/migrate/import_to_vps.sh /srv/cma/migrate-in
#
# Refuses to run against a database that already holds meetings — restoring
# twice would be the fast way to lose the first restore's edits.

set -euo pipefail

IN="${1:-/srv/cma/migrate-in}"
PG_CONTAINER="${CMA_PG_CONTAINER:-cma-postgres}"
QDRANT_URL="${CMA_QDRANT_URL:-http://10.10.0.1:6333}"
COLLECTIONS=(cma_protocols cma_analyses cma_turns cma_protocol_full)

# Must match the M1's superuser: the dump names `cma` as owner of every table
# and in every grant. Restoring as a differently-named superuser leaves objects
# owned by a role the policies do not mention.
PG_SUPER="${CMA_PG_SUPERUSER:-cma}"

say() { echo "  · $*"; }
ok()  { echo "  ✓ $*"; }
die() { echo "  ✗ $*" >&2; exit 1; }

[ -f "$IN/cma.dump" ]  || die "немає $IN/cma.dump"
[ -f "$IN/roles.sql" ] || die "немає $IN/roles.sql"

echo "Імпорт на VPS ← $IN"

# ─── 0. Refuse to overwrite a populated database ─────────────
existing="$(docker exec "$PG_CONTAINER" psql -U "$PG_SUPER" -d cma -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_name = 'ingestion_jobs'" 2>/dev/null || echo 0)"
if [ "$existing" != "0" ]; then
    rows="$(docker exec "$PG_CONTAINER" psql -U "$PG_SUPER" -d cma -tAc \
        "SELECT count(*) FROM ingestion_jobs" 2>/dev/null || echo 0)"
    [ "$rows" = "0" ] || die "у базі вже $rows джобів — імпорт зупинено, щоб не затерти їх"
fi

# ─── 1. Roles, then the database ─────────────────────────────
# Roles first: the dump's policies and grants name cma_app, and restoring
# before it exists turns into a wall of ignorable-looking errors that hide the
# one that matters.
say "ролі…"
docker exec -i "$PG_CONTAINER" psql -U "$PG_SUPER" -v ON_ERROR_STOP=0 < "$IN/roles.sql" >/dev/null
ok "ролі відновлено (cma_app має існувати)"

say "база…"
docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_SUPER" -d cma --clean --if-exists --no-owner \
    < "$IN/cma.dump" >/dev/null
ok "база відновлена"

say "перевірка RLS…"
docker exec "$PG_CONTAINER" psql -U "$PG_SUPER" -d cma -tAc \
    "SELECT count(*) FROM pg_policies WHERE schemaname='public'" | sed 's/^/  · політик: /'

# ─── 2. Qdrant ───────────────────────────────────────────────
for c in "${COLLECTIONS[@]}"; do
    f="$IN/qdrant/$c.snapshot"
    [ -f "$f" ] || die "немає знімка $f"
    say "відновлення ${c}…"
    curl -sf -X POST "$QDRANT_URL/collections/$c/snapshots/upload?priority=snapshot" \
         -H 'Content-Type:multipart/form-data' -F "snapshot=@$f" >/dev/null \
        || die "не вдалося відновити $c"
    n="$(curl -sf "$QDRANT_URL/collections/$c" \
         | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["points_count"])')"
    ok "$c → $n точок"
done

echo
echo "Звірте лічильники з M1 (крок 6 ранбука):"
echo "  cma_protocols 597 · cma_analyses 960 · cma_turns 9280 · cma_protocol_full 20"
echo "Розбіжність = не переносьте трафік, розбирайтесь."
