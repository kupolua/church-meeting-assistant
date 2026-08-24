#!/usr/bin/env bash
#
# restart_dev.sh — Church Meeting Assistant dev helper.
#
# 1. Перевіряє інфраструктуру:
#      - Docker-контейнери: Postgres (cma-postgres), Qdrant (lf-client-qdrant-1)
#        — запускає їх, якщо зупинені.
#      - Ollama (нативний сервіс на :11434) + наявність моделі — лише перевіряє.
# 2. Перезапускає 4 church_assistant-сервіси у фоні з логами:
#      web (uvicorn --reload) · query-worker · ingestion-worker · telegram-bot
#
# Використання:
#      ./restart_dev.sh              # повний цикл: перевірка + перезапуск
#      ./restart_dev.sh --check      # лише перевірка інфраструктури, без перезапуску
#      ./restart_dev.sh --stop       # зупинити сервіси + контейнери (Postgres, Qdrant)
#                                    # + сторонні контейнери, що ділять машину
#                                    #   (за замовчуванням langfuse — звільнити RAM)
#
# Перевизначення (env): CMA_PG_CONTAINER, CMA_QDRANT_CONTAINER, CMA_WEB_PORT,
#                       CMA_EXTRA_STOP (порожнє = не чіпати сторонні контейнери)
#
set -uo pipefail

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

PG_CONTAINER="${CMA_PG_CONTAINER:-cma-postgres}"
QDRANT_CONTAINER="${CMA_QDRANT_CONTAINER:-lf-client-qdrant-1}"
WEB_HOST="${CMA_WEB_HOST:-127.0.0.1}"
WEB_PORT="${CMA_WEB_PORT:-8000}"
LOG_DIR="$PROJECT_DIR/logs"

# `docker` is often a shell alias (Docker Desktop) not inherited by scripts —
# resolve the real binary (PATH first, then the Docker.app location).
DOCKER="$(command -v docker 2>/dev/null || true)"
if [[ -z "$DOCKER" ]]; then
    for cand in \
        /Applications/Docker.app/Contents/Resources/bin/docker \
        /usr/local/bin/docker /opt/homebrew/bin/docker; do
        [[ -x "$cand" ]] && { DOCKER="$cand"; break; }
    done
fi

# defaults; overridden from .env below if present
OLLAMA_URL="http://localhost:11434"
QDRANT_URL="http://localhost:6333"
OLLAMA_MODEL="gemma4:26b"

# Which half of the system this machine runs.
#   all    — one machine holds everything (the laptop, as it has always been)
#   worker — Postgres and Qdrant answer across the tunnel and the web lives on
#            the VPS; this box keeps Ollama and the two workers
# Set CMA_ROLE=worker in .env after the split (deploy/env/m1.env.example).
CMA_ROLE_DEFAULT="all"

# read a KEY=value from .env, stripping quotes / inline comments / whitespace
env_val() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | sed 's/#.*//' | tr -d '"' | xargs; }

# Exported variable first, .env second — the same precedence the application
# gets from load_dotenv(), which does not override an existing environment.
# Reading only .env here would let the script report health for an endpoint the
# services are not using.
cfg() { local v="${!1:-}"; [[ -n "$v" ]] && { echo "$v"; return; }; env_val "$1"; }

ROLE="$(cfg CMA_ROLE)"; ROLE="${ROLE:-$CMA_ROLE_DEFAULT}"
case "$ROLE" in all|worker) : ;; *) echo "невідома CMA_ROLE: $ROLE (all|worker)" >&2; exit 1 ;; esac

# Follow .env rather than assuming localhost: in the worker role these point
# across the tunnel, and checking the wrong endpoint would report health for a
# service nobody is using.
QDRANT_URL="$(cfg QDRANT_URL)"; QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
OLLAMA_URL="$(cfg OLLAMA_URL)"; OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
OLLAMA_MODEL="$(cfg OLLAMA_MODEL)"; OLLAMA_MODEL="${OLLAMA_MODEL:-gemma4:26b}"
if [[ -f .env ]]; then
    OLLAMA_URL="$(cfg OLLAMA_URL)";   OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
    QDRANT_URL="$(cfg QDRANT_URL)";   QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
    OLLAMA_MODEL="$(cfg OLLAMA_MODEL)"; OLLAMA_MODEL="${OLLAMA_MODEL:-gemma4:26b}"
fi

# ─────────────────────────────────────────────────────────────
# Pretty output
# ─────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    C_OK=$'\033[92m'; C_WARN=$'\033[93m'; C_ERR=$'\033[91m'; C_DIM=$'\033[90m'; C_B=$'\033[1m'; C_R=$'\033[0m'
else
    C_OK=; C_WARN=; C_ERR=; C_DIM=; C_B=; C_R=
fi
ok()   { echo "  ${C_OK}✓${C_R} $*"; }
warn() { echo "  ${C_WARN}⚠${C_R} $*"; }
err()  { echo "  ${C_ERR}✗${C_R} $*" >&2; }
info() { echo "  ${C_DIM}·${C_R} $*"; }
hdr()  { echo; echo "${C_B}$*${C_R}"; }

# ─────────────────────────────────────────────────────────────
# Infra checks
# ─────────────────────────────────────────────────────────────
INFRA_OK=1   # becomes 0 if a HARD dependency (Postgres) is unavailable

check_prereqs() {
    for bin in curl uv; do
        command -v "$bin" >/dev/null 2>&1 || { err "не знайдено '$bin' у PATH"; exit 1; }
    done
    # In the worker role Postgres and Qdrant live on the VPS; there is nothing
    # local for docker to manage, and demanding Docker Desktop here would block
    # transcription for no reason.
    [[ "$ROLE" == "worker" ]] && return 0
    [[ -n "$DOCKER" ]] || { err "не знайдено бінарник 'docker' (Docker Desktop встановлено?)"; exit 1; }
    if ! "$DOCKER" info >/dev/null 2>&1; then
        err "Docker daemon не запущений. Запусти Docker Desktop і повтори."
        exit 1
    fi
}

# ensure_container <name> <human-desc>  → starts it if stopped; hard-fails if missing
ensure_container() {
    local name="$1" desc="$2"
    if ! "$DOCKER" inspect "$name" >/dev/null 2>&1; then
        err "$desc: контейнер '$name' не існує — створи його вручну (docker run / compose)."
        return 1
    fi
    if [[ "$("$DOCKER" inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" == "true" ]]; then
        ok "$desc: '$name' працює"
        return 0
    fi
    warn "$desc: '$name' зупинений — запускаю…"
    if "$DOCKER" start "$name" >/dev/null 2>&1; then
        sleep 2; ok "$desc: '$name' запущено"
    else
        err "$desc: не вдалося запустити '$name'"
        return 1
    fi
}

check_postgres() {
    if [[ "$ROLE" == "worker" ]]; then
        local host port; host="$(cfg DB_HOST)"; port="$(cfg DB_PORT)"
        host="${host:-127.0.0.1}"; port="${port:-5432}"
        # No pg_isready without a local container, so ask the socket. A closed
        # port here almost always means wg0 is down, not that Postgres died.
        if (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; then
            ok "Postgres відповідає ($host:$port, через тунель)"
        else
            err "Postgres недоступний на $host:$port — підніми тунель: sudo wg-quick up wg0"
            INFRA_OK=0
        fi
        return
    fi
    ensure_container "$PG_CONTAINER" "Postgres" || { INFRA_OK=0; return; }
    if "$DOCKER" exec "$PG_CONTAINER" pg_isready -U "$(env_val DB_USER || echo cma)" >/dev/null 2>&1; then
        ok "Postgres приймає зʼєднання (:$(env_val DB_PORT || echo 5433))"
    else
        warn "Postgres контейнер працює, але pg_isready ще не готовий"
    fi
}

check_qdrant() {
    if [[ "$ROLE" == "worker" ]]; then
        if curl -sf -o /dev/null --max-time 4 "$QDRANT_URL/collections"; then
            ok "Qdrant відповідає ($QDRANT_URL, через тунель)"
        else
            warn "Qdrant не відповідає ($QDRANT_URL) — індексація чекатиме"
        fi
        return
    fi
    ensure_container "$QDRANT_CONTAINER" "Qdrant" || { warn "Qdrant недоступний — аналіз/індекс чекатимуть (worker health-гейтить)"; return; }
    if curl -sf -o /dev/null --max-time 4 "$QDRANT_URL/collections"; then
        ok "Qdrant відповідає ($QDRANT_URL)"
    else
        warn "Qdrant контейнер працює, але $QDRANT_URL/collections не відповідає"
    fi
}

check_ollama() {
    # Ollama — нативний сервіс (не контейнер): лише перевіряємо.
    local tags
    if ! tags="$(curl -sf --max-time 5 "$OLLAMA_URL/api/tags" 2>/dev/null)"; then
        warn "Ollama не відповідає ($OLLAMA_URL). Запусти застосунок Ollama або 'ollama serve'."
        warn "Сервіси стартують, але аналіз (Gemma) чекатиме, поки Ollama підніметься."
        return
    fi
    ok "Ollama відповідає ($OLLAMA_URL)"
    if grep -q "\"$OLLAMA_MODEL\"" <<<"$tags"; then
        ok "модель '$OLLAMA_MODEL' встановлена"
    else
        warn "модель '$OLLAMA_MODEL' не знайдена — постав її: ollama pull $OLLAMA_MODEL"
    fi
}

# ─────────────────────────────────────────────────────────────
# church_assistant services
# ─────────────────────────────────────────────────────────────
# label | pkill-pattern | command…
# The worker role runs only what needs the models. web and telegram-bot moved
# to the VPS so the church can upload and ask at any hour without this laptop.
if [[ "$ROLE" == "worker" ]]; then
    SVC_LABELS=(query-worker ingestion-worker)
else
    SVC_LABELS=(web query-worker ingestion-worker telegram-bot)
fi
svc_pattern() {
    case "$1" in
        web)              echo "uvicorn church_assistant.web.main:app" ;;
        query-worker)     echo "church_assistant.worker.main" ;;
        ingestion-worker) echo "church_assistant.ingestion.main" ;;
        telegram-bot)     echo "church_assistant.bot.main" ;;
    esac
}
svc_cmd() {
    case "$1" in
        web)              echo "uv run uvicorn church_assistant.web.main:app --host $WEB_HOST --port $WEB_PORT --reload" ;;
        query-worker)     echo "uv run python -m church_assistant.worker.main" ;;
        ingestion-worker) echo "uv run python -m church_assistant.ingestion.main" ;;
        telegram-bot)     echo "uv run python -m church_assistant.bot.main" ;;
    esac
}

stop_services() {
    for label in "${SVC_LABELS[@]}"; do
        local pat; pat="$(svc_pattern "$label")"
        if pgrep -f "$pat" >/dev/null 2>&1; then
            pkill -f "$pat" 2>/dev/null || true
            info "зупинено $label"
        fi
    done
    sleep 1
}

# Stop the infra containers this script manages (Postgres + Qdrant).
# Ollama is a native service, not a container — left untouched.
stop_containers() {
    if [[ -z "$DOCKER" ]] || ! "$DOCKER" info >/dev/null 2>&1; then
        warn "Docker недоступний — контейнери не чіпаю"
        return
    fi
    for spec in "$PG_CONTAINER:Postgres" "$QDRANT_CONTAINER:Qdrant"; do
        local name="${spec%%:*}" desc="${spec##*:}"
        if [[ "$("$DOCKER" inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" == "true" ]]; then
            if "$DOCKER" stop "$name" >/dev/null 2>&1; then
                ok "$desc: '$name' зупинено"
            else
                warn "$desc: не вдалося зупинити '$name'"
            fi
        else
            info "$desc: '$name' вже зупинений"
        fi
    done
    stop_extra_containers
}

# Stop unrelated containers that merely share this laptop (default: langfuse).
# They are not ours to manage, but they hold RAM that Gemma and whisper need on
# a 32 GB machine — and after --stop the point is to get the machine back.
# Matched by name substring, so this covers a whole compose stack at once.
# CMA_EXTRA_STOP="" leaves them alone.
stop_extra_containers() {
    local filter="${CMA_EXTRA_STOP-langfuse}"
    [[ -z "$filter" ]] && return

    local ids
    ids="$("$DOCKER" ps -q --filter "name=$filter" 2>/dev/null)"
    if [[ -z "$ids" ]]; then
        info "сторонні ($filter): запущених немає"
        return
    fi

    local n; n="$(printf '%s\n' "$ids" | wc -l | tr -d ' ')"
    # shellcheck disable=SC2086  # ids is a deliberate word-split list
    if "$DOCKER" stop $ids >/dev/null 2>&1; then
        ok "сторонні ($filter): зупинено $n"
    else
        warn "сторонні ($filter): не вдалося зупинити частину"
    fi
}

start_services() {
    mkdir -p "$LOG_DIR"
    for label in "${SVC_LABELS[@]}"; do
        local pat cmd log; pat="$(svc_pattern "$label")"; cmd="$(svc_cmd "$label")"; log="$LOG_DIR/$label.log"
        # shellcheck disable=SC2086
        nohup $cmd >"$log" 2>&1 &
        disown 2>/dev/null || true
        sleep 2
        if pgrep -f "$pat" >/dev/null 2>&1; then
            ok "$label запущено ${C_DIM}(лог: logs/$label.log)${C_R}"
        else
            err "$label впав одразу — див. logs/$label.log:"
            tail -n 6 "$log" 2>/dev/null | sed 's/^/      /'
        fi
    done
}

# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
MODE="${1:-restart}"

case "$MODE" in
    --stop)
        hdr "⏹  Зупинка church_assistant-сервісів"
        stop_services
        if [[ "$ROLE" == "worker" ]]; then
            info "контейнери не чіпав — Postgres і Qdrant на VPS"
        else
            hdr "⏹  Зупинка контейнерів"
            stop_containers
        fi
        ok "готово"
        exit 0
        ;;
    --check) : ;;                 # just run infra checks below, then exit
    --role)  echo "$ROLE"; exit 0 ;;
    --help|-h)
        # print the contiguous header comment block (lines after the shebang)
        awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
        exit 0
        ;;
    restart) : ;;
    *) err "невідомий аргумент: $MODE (див. --help)"; exit 1 ;;
esac

hdr "🔎 Перевірка інфраструктури"
check_prereqs
check_postgres
check_qdrant
check_ollama

if [[ "$INFRA_OK" -ne 1 ]]; then
    err "Postgres недоступний — це жорстка залежність. Виправ і повтори."
    exit 1
fi

if [[ "$MODE" == "--check" ]]; then
    hdr "✅ Перевірка завершена (сервіси не чіпав)."
    exit 0
fi

hdr "🔁 Перезапуск church_assistant-сервісів"
stop_services
start_services

hdr "📋 Підсумок"
if [[ "$ROLE" == "worker" ]]; then
    echo "  роль:      worker (web і бот — на VPS)"
    echo "  логи:      tail -f logs/{query-worker,ingestion-worker}.log"
else
    echo "  web:       http://$WEB_HOST:$WEB_PORT/   (дашборд)"
    echo "  логи:      tail -f logs/{web,query-worker,ingestion-worker,telegram-bot}.log"
fi
echo "  зупинити:  ./restart_dev.sh --stop"
echo
