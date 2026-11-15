#!/usr/bin/env bash
# Measure a recording made with the phone: what the screen counted vs what the
# server joined, and what the seams between the ten-minute pieces look like.
#
#   bash deploy/check_recording.sh                 # recordings of the last 7 days
#   bash deploy/check_recording.sh 2026-11-14      # one date
#   CMA_CHECK_DAYS=30 bash deploy/check_recording.sh
#
# Runs from the M1 over the tunnel (same access as deploy/backup_from_vps.sh):
#   1. VPS, as cma: ffprobe of <meetings>/<date>/audio.m4a, leftover parts/.
#   2. DB (through the local cma-dev-postgres container, like the backup):
#      audit_log «recording.finished» — the seconds the screen counted and the
#      number of parts — plus the job's status.
#   3. Copies audio.m4a here and reads the AAC packet timestamps: every place
#      where the step between packets is not one frame is a seam left by
#      ffmpeg concat. Cuts a 6-second WAV around each so it can be listened to.
#
# The verdict is: lost = screen seconds − file duration; per seam = lost/(parts−1).
# More than ~1 s per seam → the recorder goes PCM/WAV (docs/session_2026-11-11.md).
#
# Reads the DB password from the VPS .env into an env var; never prints it.

set -euo pipefail

ONLY_DATE="${1:-}"
DAYS="${CMA_CHECK_DAYS:-7}"
VPS="${CMA_VPS_HOST:-cma@10.10.0.1}"
DB_HOST="${CMA_VPS_DB:-10.10.0.1}"
DEST="${CMA_CHECK_DEST:-$HOME/cma-recording-check}"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15"

DOCKER="$(command -v docker 2>/dev/null || true)"
[[ -x "$DOCKER" ]] || DOCKER=/Applications/Docker.app/Contents/Resources/bin/docker
PG_CONTAINER="${CMA_PG_CONTAINER:-cma-dev-postgres}"

say() { echo; echo "── $*"; }

if [[ -n "$ONLY_DATE" && ! "$ONLY_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    echo "✗ дата має вигляд РРРР-ММ-ДД" >&2; exit 1
fi

# ─── 1. On the VPS ───────────────────────────────────────────
say "VPS: записи з audio.m4a за останні $DAYS днів${ONLY_DATE:+ (лише $ONLY_DATE)}"
$SSH "$VPS" "ONLY_DATE='$ONLY_DATE' DAYS='$DAYS' bash -s" <<'REMOTE'
set -u
ROOT="$(grep '^DATA_ROOT=' /srv/cma/.env | cut -d= -f2-)"; ROOT="${ROOT:-/srv/cma/data}"
echo "  DATA_ROOT=$ROOT"
PROBE="$(command -v ffprobe || true)"
[[ -n "$PROBE" ]] || echo "  ! на VPS немає ffprobe (пакет ffmpeg) — тривалість не виміряю"
dur() { [[ -n "$PROBE" ]] && "$PROBE" -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null || echo "?"; }
for T in "$ROOT"/tenants/*/; do
    tenant="$(basename "$T")"
    M="$T/meetings"; [[ -d "$M" ]] || continue
    if [[ -n "$ONLY_DATE" ]]; then
        dirs="$M/$ONLY_DATE"
    else
        dirs="$(find "$M" -mindepth 2 -maxdepth 2 -name audio.m4a -mtime -"$DAYS" -printf '%h\n' | sort)"
        parts="$(find "$M" -mindepth 2 -maxdepth 2 -name parts -type d -printf '%h\n' | sort)"
        dirs="$(printf '%s\n%s\n' "$dirs" "$parts" | sort -u | sed '/^$/d')"
    fi
    for d in $dirs; do
        [[ -d "$d" ]] || { echo "  $tenant/$(basename "$d"): теки нема"; continue; }
        date="$(basename "$d")"
        if [[ -f "$d/audio.m4a" ]]; then
            printf '  %s/%s  audio.m4a  %s байт  mtime %s  тривалість %s с\n' \
                "$tenant" "$date" "$(stat -c %s "$d/audio.m4a")" "$(stat -c '%y' "$d/audio.m4a" | cut -d. -f1)" "$(dur "$d/audio.m4a")"
        fi
        if [[ -d "$d/parts" ]]; then
            echo "  $tenant/$date  НЕЗАВЕРШЕНИЙ: лишилися шматки в parts/ (finish не пройшов):"
            for p in "$d"/parts/*; do
                printf '      %s  %s байт  %s с\n' "$(basename "$p")" "$(stat -c %s "$p")" "$(dur "$p")"
            done
        fi
        [[ -f "$d/recording.json" ]] && echo "      recording.json: $(cat "$d/recording.json")"
    done
done
REMOTE

# ─── 2. In the database ──────────────────────────────────────
say "База: що порахував телефон (audit_log recording.finished) і стан job"
if [[ -x "$DOCKER" ]] && "$DOCKER" inspect "$PG_CONTAINER" >/dev/null 2>&1; then
    PGPW="$($SSH "$VPS" 'grep "^POSTGRES_SUPERUSER_PASSWORD=" /srv/cma/.env | cut -d= -f2-')"
    [[ -n "$PGPW" ]] || { echo "✗ пароль не прочитався" >&2; exit 1; }
    WHERE_A="a.timestamp > now() - interval '$DAYS days'"
    WHERE_J="j.created_at > now() - interval '$DAYS days'"
    if [[ -n "$ONLY_DATE" ]]; then
        WHERE_A="a.resource = 'meeting/$ONLY_DATE'"
        WHERE_J="j.meeting_date = '$ONLY_DATE'"
    fi
    "$DOCKER" exec -i -e PGPASSWORD="$PGPW" "$PG_CONTAINER" psql -h "$DB_HOST" -U cma -d cma -X -q <<SQL
\pset border 1
SELECT a.timestamp::timestamp(0) AS "коли", t.slug AS "церква", a.resource AS "зустріч",
       (a.detail->>'seconds')::int AS "екран, с",
       (a.detail->>'parts')::int AS "шматків",
       a.detail->>'size_mb' AS "МБ"
FROM audit_log a LEFT JOIN tenants t ON t.id = a.tenant_id
WHERE a.action = 'recording.finished' AND $WHERE_A
ORDER BY a.timestamp;

SELECT l.timestamp::timestamp(0) AS "коли", l.level, l.event, left(l.message, 110) AS "повідомлення"
FROM logs l
WHERE l.event IN ('recording.assemble_failed', 'ingestion.uploaded') AND l.timestamp > now() - interval '$DAYS days'
ORDER BY l.timestamp;

SELECT j.id, j.meeting_date AS "дата", j.status, j.original_filename AS "файл",
       j.created_at::timestamp(0) AS "створено", j.started_at::timestamp(0) AS "почато",
       left(coalesce(j.progress_note, ''), 60) AS "нотатка"
FROM ingestion_jobs j
WHERE $WHERE_J
ORDER BY j.created_at;
SQL
    unset PGPW
else
    echo "  ! контейнер $PG_CONTAINER не запущено (docker start $PG_CONTAINER) — пропускаю базу"
fi

# ─── 3. Here: the seams ──────────────────────────────────────
say "Шви: копіюю audio.m4a на M1 і читаю мітки часу пакетів"
command -v ffprobe >/dev/null || { echo "  ! локально немає ffprobe (brew install ffmpeg)"; exit 0; }
mkdir -p "$DEST"
ROOT="$($SSH "$VPS" "grep '^DATA_ROOT=' /srv/cma/.env | cut -d= -f2-")"; ROOT="${ROOT:-/srv/cma/data}"
if [[ -n "$ONLY_DATE" ]]; then
    LIST="$($SSH "$VPS" "ls -d $ROOT/tenants/*/meetings/$ONLY_DATE/audio.m4a 2>/dev/null" || true)"
else
    LIST="$($SSH "$VPS" "find $ROOT/tenants/*/meetings -mindepth 2 -maxdepth 2 -name audio.m4a -mtime -$DAYS 2>/dev/null | sort" || true)"
fi
[[ -n "$LIST" ]] || { echo "  нічого копіювати"; exit 0; }
for remote in $LIST; do
    date="$(basename "$(dirname "$remote")")"
    tenant="$(echo "$remote" | sed -E 's#.*/tenants/([^/]+)/.*#\1#')"
    local_dir="$DEST/$tenant-$date"; mkdir -p "$local_dir"
    local_file="$local_dir/audio.m4a"
    rsync -a --partial -e "$SSH" "$VPS:$remote" "$local_file"
    echo "  $tenant/$date → $local_file"
    python3 -I - "$local_file" "$local_dir" <<'PY'
import csv, subprocess, sys
f, out = sys.argv[1], sys.argv[2]
sr = int(subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate", "-of", "csv=p=0", f]).split()[0])
dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", f]))
rows = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "packet=pts_time", "-of", "csv=p=0", f]).decode().split()
pts = [float(r.split(",")[0]) for r in rows if r]
frame = 1024 / sr
seams = []                              # [time, net gap]
for a, b in zip(pts, pts[1:]):
    d = b - a
    if abs(d - frame) > 0.002:          # not one AAC frame → the join
        if seams and a - seams[-1][0] < 0.5:   # concat leaves two odd steps per join
            seams[-1][1] += d - frame
        else:
            seams.append([a, d - frame])
print(f"      {sr} Гц, {len(pts)} пакетів, тривалість {dur:.2f} с ({int(dur)//60} хв {dur%60:.0f} с)")
if not seams:
    print("      розривів у мітках часу нема — або один шматок, або concat склеїв впритул")
for i, (t, gap) in enumerate(seams, 1):
    kind = "дірка" if gap > 0 else "накладка"
    print(f"      шов {i}: на {t:9.2f} с ({int(t)//60}:{int(t)%60:02d})  {kind} у мітках {abs(gap)*1000:.0f} мс")
    clip = f"{out}/seam-{i:02d}-{int(t)//60:03d}m{int(t)%60:02d}s.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(t-3,0):.3f}", "-t", "6", "-i", f, clip], check=False)
if seams:
    print(f"      кліпи по 6 с довкола кожного шва: {out}/seam-*.wav (послухати на M1)")
print("      Втрачене = «екран, с» з таблиці вище − тривалість; на один шов = втрачене / (шматків − 1).")
PY
done
