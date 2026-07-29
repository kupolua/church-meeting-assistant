# Church Meeting Assistant — Handoff Brief

**Дата:** 29 липня 2026
**Стан:** MVP-A (foundation + web + bot + worker), MVP-B (dashboard), MVP-C (ingestion), meeting-detail (стенограма + аудіоплеєр), UI-навігація, **редагування спікерів зі стенограми + фінгерпринт нових учасників**, **`restart_dev.sh`** і — головне — **повний self-host: Voyage прибрано, embeddings/rerank тепер локальні (bge-m3 + bge-reranker), корпус переіндексовано** — усе закомічено й **запушено на GitHub** (`main`). RAG провалідований end-to-end на локальному стеку.
**Ключове рішення сесії (приватність):** усе, що торкається даних зустрічей, працює **локально**. Хмарний LLM (SiliconFlow тощо) і хмарний VPS відхилені — транскрипти не мають виходити з машини. Voyage (останню сторонню AI-залежність) прибрано.
**Мета наступної сесії:** прогнати **MVP-C ingestion на реальному аудіо** (повний ~2-год pipeline + reprocess ще не ганялись живими) + обрати пункт backlog.

---

## Хто я

Pavlo Kulakovskyi. JS/DevOps 20+ років. Працюю в mentor+pair-coding mode з Claude:
"Claude пише код, я тестую та комітую."

Використовую **українську** для процесу і **англійську** для коду/architecture.

---

## Що вже готово (do NOT rewrite)

### Phase 2A/2B (completed, June 2026)

- **17 meetings indexed** у Qdrant (`cma_protocols` 512, `cma_analyses` 830, `cma_turns` 8105, `cma_protocol_full` 17)
- Full audio → polished.md → RAG pipeline working
- Embeddings/rerank **тепер локальні** (bge-m3 + bge-reranker-v2-m3) — див. «Self-host: Voyage → local» нижче. *(Voyage rerank-2 давав +49% precision історично; локальний варіант треба калібрувати.)*

### Phase 3 / MVP-A.1 Foundation (13 липня, 4 commits)

**Repo:** github.com/kupolua/church-meeting-assistant, branch `main`

**PostgreSQL:** container `cma-postgres`, port **5433** (5432 зайнятий langfuse-postgres)

**Схема (`db/schema.sql`):**
- `users` — whitelist (Telegram IDs, role='pastor'|'admin')
- `queries` — queue + history (status: pending/processing/completed/failed/cancelled)
- `logs` — T1+T2+T3+T4 events
- `errors` — окремо для alerts
- `health_checks` — 60s snapshots
- Views: `v_queue_depth`, `v_stats_today`, `v_latest_health`

**Ключове про queries table:**
- `verbose_mode` (не `verbose` — reserved word в PostgreSQL)
- `source` = 'web' | 'telegram'
- `telegram_chat_id` + `telegram_message_id` для delivery
- `hits` — JSONB (Hit.to_dict() serialization); `sources` — TEXT[]
- Всі timings (embed_ms, qdrant_ms, rerank_ms, gemma_ms, total_ms)

**RAG service API (`shared/rag.py`):**
```python
result = await rag.answer(question, collection="protocols", limit=5, rerank=True)
# → AnswerResult with .hits, .synthesis, .sources, .timings
# .hits_as_json() для JSONB storage
ret = await rag.retrieve(question, ...)  # без Gemma → RetrievalResult
```

**Logger API (`shared/logger.py`):**
```python
log = Logger(process="bot")
await log.info("event.name", message="...", query_id=..., user_id=...)
await log.record_error(error_type="...", error_message="...", traceback=..., ...)
```

### MVP-A.2 Web UI (13 липня, 2 commits)

Sidebar (meetings) + query form (sync Gemma) + meeting detail + HTMX search + history.
*(Маршрути/навігацію перебудовано 22 липня — див. «Web UI навігація + layout»: `/` тепер дашборд, головна-запит → `/meetings`.)*

### MVP-A.3 Telegram bot (15 липня, 3 commits) ✅

`src/church_assistant/bot/` — `python-telegram-bot` v21, polling.
- `config.py` — TELEGRAM_BOT_TOKEN loader
- `formatting.py` — Telegram-markdown escape helpers
- `middleware/whitelist.py` — auth-гейт перед кожним handler (`is_authorized`)
- `handlers/` — `query` (INSERT pending + immediate ack), `help` (/start,/help), `verbose` (/verbose → hits останнього completed), `admin` (/stats, admin-only)
- `delivery.py` — `send_answer` / `send_failure`, викликається worker'ом
- Pavlo в DB як admin (id=5, telegram_user_id=356584956, @kupolua)

### MVP-A.4 Worker (15 липня, 2 commits) ✅

`src/church_assistant/worker/` — background consumer.
- `config.py` — WORKER_* env (poll=10s, health=60s, retry_sleep=60s, max_retries=3)
- `processor.py` — `process_query`: RAG → `mark_completed` → `delivery`; при помилці retry (`requeue_for_retry`) до max_retries, потім permanent fail + notify
- `main.py` — consumer-loop: health-gating (пауза якщо Ollama/Qdrant down) + `fetch_next_pending` (FOR UPDATE SKIP LOCKED) + graceful shutdown (SIGINT/SIGTERM)

### MVP-B Dashboard (15 липня, 1 commit) ✅

`web/routes/dashboard.py` + `templates/dashboard.html` + `partials/dashboard_panel.html`.
- `GET /dashboard` — self-polling панель (HTMX `every 5s`)
- Секції: health-pills, queue depth + today stats (tiles), активні запити, відкриті помилки, whitelist
- Actions (кожна повертає свіжу панель): `cancel`/`requeue` queries, `resolve` errors, `deactivate` users
- **Admin захищений від deactivate** — і в template, і в route (defense in depth)

### MVP-C Ingestion: upload audio → protocol (15–22 липня) ✅ закомічено (`2f7596e`)

Повний цикл: web-форма завантаження аудіо → діаризація+транскрипція → **web-редактор speakers.json** → аналіз (Gemma) → polish → **авто-index у Qdrant**. Асинхронна job-модель (окремий worker), стан-машина в `ingestion_jobs`.

**Стан-машина:** `pending → transcribing → awaiting_review → queued_analysis → analyzing → indexing → completed | failed | cancelled`

**DB (schema **v3** — застосовано через `init_db.py`, idempotent):**
- `ingestion_jobs` — черга+історія (unique per meeting_date, `stage`/`progress_note`, timings, `speaker_count`, `indexed`, retry, **`force_reprocess`** — v3)
- view `v_ingestion_depth`
- `db/ingestion_jobs_repo.py` — repo у стилі `queries_repo` (+ smoke test); `fetch_next_runnable(allowed_statuses=…)` — щоб транскрипція йшла навіть коли Ollama/Qdrant down

**Ingestion worker (`src/church_assistant/ingestion/`):**
- `config.py` — `INGESTION_*` env (poll=15s, max_retries=2, sequential, auto_index)
- `paths.py` — резолвінг артефактів папки (сумісно з `new_meeting.py`)
- `stages.py` — async subprocess-обгортки навколо `match_speakers`/`transcribe`/`merge_transcript`/`chunked_analyze`/`polish_protocol`/`index_meeting` (**verbatim-команди** з `new_meeting.py`), resumable
- `speakers.py` — load/save speakers.json (**зберігає `_meta`**) + RTTM talk-time hints + review-rows (+ smoke test)
- `processor.py` — диспетч за статусом, never-raise, retry+requeue до правильної фази
- `main.py` — consumer-loop, health-gating (транскрипція завжди; аналіз/індекс лише коли deps up), graceful shutdown

**Web (`web/routes/ingest.py` + templates):**
- `GET /ingest` — форма upload + self-polling панель (tiles + активні/завершені)
- `POST /ingest` — multipart → створює `data/meetings/<date>/audio.<ext>` + insert pending (duplicate/format guard)
- `GET/POST /ingest/{id}/speakers` — редактор speakers.json (бейджі review/no_match/invalid, час мовлення) → `queued_analysis`
- `GET /ingest/{id}` — job-detail (timeline, прогрес, traceback, протокол-CTA)
- `POST /ingest/{id}/cancel|requeue` — HTMX (панель) або plain-form (redirect на detail)
- Лінк «🎙️ Нова зустріч» у сайдбарі

**⚠ TODO наступної сесії:** прогнати ingestion worker на **реальному** аудіо (unit + web-роути перевірені через curl, але повний ~2-год pipeline ще ні).

### Meeting-detail: стенограма + аудіоплеєр із таймкодами (22 липня) ✅ закомічено (`927f56a`)

Покращення сторінки зустрічі `GET /meetings/<date>` (`web/routes/meetings.py` + `meeting_detail.html` + `shared/meetings_index.py`):
- **Стенограма** — `annotated.md` парситься в репліки зі спікерами (`_parse_transcript_from_annotated` → `TranscriptTurn`), рендериться **після** переліку тем, згорнута в `<details>`. Лінива група спікера в regex обробляє й плейсхолдери `[немає мовця]` / `[нерозбірливо]`.
- **Аудіоплеєр** — sticky `<audio>` (якщо є `audio.*`), джерело `GET /meetings/<date>/audio` через Starlette `FileResponse` з **нативним HTTP Range** (перемотка без саморобного стрімера).
- **Клікабельні таймкоди** — і в стенограмі (кожен `.turn-ts`), і в темах (дужкові списки, роздільники **кома або крапка з комою**: `(00:21)`, `(24:11, 28:16)`, `(31:30; 33:52; 34:42)`). Клік → seek + play. Хибних збігів (біблійні посилання «Псалом 84:6») уникнуто — лінкуються лише дужкові timestamp-списки.

**⚠ Відома засторога:** у автоматизованому тесті великі m4a (74–103 МБ) інколи повільно вантажили метадані в плеєр (пул зʼєднань браузера, не сервер — curl віддає Range миттєво). У звичайному свіжому браузері працює; якщо на великих файлах буде повільний старт — кандидат на легкий metadata-endpoint / оптимізацію m4a під стрімінг.

### Web UI навігація + layout (22 липня) ✅

`base.html` + `web/routes/home.py` + `app.css`:
- **Landing:** `GET /` → 307 redirect на `/dashboard` (дашборд = стартова сторінка).
- **Топ-меню** (завжди видиме, у `base.html`, обгорнуте в колонковий `.app-shell`): 📊 Моніторинг (`/dashboard`) · 📅 Зустрічі (`/meetings`) · 🎙️ Нова зустріч (`/ingest`) · 📜 Історія запитів (`/history`). Активний пункт за `request.url.path`.
- **«Зустрічі»** = стара головна (форма запитів + огляд корпусу), переїхала з `/` на `GET /meetings` (`home.py`).
- **Сайдбар** (пошук + список зустрічей) — **лише** в розділі «Зустрічі» (`/meetings` + `/meetings/<date>`); на решті сторінок його немає (клас `.no-sidebar` → контент центрується max-width 1100px). Bottom-nav із сайдбара прибрано (тепер у топ-меню).

### ★ Self-host: Voyage → локальні bge-m3 + bge-reranker (29 липня) ✅ `fed06b3`, `132bdf1`

**Остання стороння AI-залежність прибрана — RAG повністю локальний, дані не виходять з машини.**
- `shared/local_embed.py` — **bge-m3 через Ollama** `/api/embed` (1024-dim = схема Qdrant; симетрична, без query/document split). `ollama pull bge-m3` обовʼязково.
- `shared/local_rerank.py` — **bge-reranker-v2-m3** через sentence-transformers CrossEncoder (lazy singleton, sigmoid 0-1). `RERANK_DEVICE=auto` → **mps** на M1 (~2с) / cuda / cpu, з graceful fallback.
- Свопнуто в `rag.py` / `index_meeting.py` / `query.py` (публічні API незмінні). `pyproject`: −voyageai, +sentence-transformers. `.env.example`: `EMBED_MODEL`/`RERANK_MODEL`/`RERANK_DEVICE`, без `VOYAGE_API_KEY`.
- **Переіндексовано всі 17 зустрічей** (`index_meeting --force`) — Voyage-вектори замінено на bge-m3 (лічильники ті самі). Валідація: full RAG дає точні цитовані відповіді.
- **⚠ Пороги кольорів rerank** (`RERANK_SCORE_GOOD=0.65 / OK=0.35`) — first-pass калібрація під bge-reranker, дотюнити на реальних запитах. Впливає лише на UI (green/yellow/dim), не на якість.
- **⚠ У `.env`**: прибрано застаріле `RERANK_MODEL="rerank-2"` (ламало reranker); лишилась мертва `VOYAGE_API_KEY` (не використовується, можна видалити).

### Редагування спікерів зі стенограми + фінгерпринт нових учасників (29 липня) ✅ `b597c36`

Per-cluster (голосовий кластер SPEAKER_XX), у розділі «Стенограма» на `/meetings/<date>`:
- Кожна репліка показує спікера як **посилання на зміну** (діалог із datalist відомих імен + вільне поле для нового учасника). `meetings_index` виводить `speaker_label` кожної репліки з RTTM (узгоджено з іменем через speakers.json).
- `ingestion/speaker_review.py` — чернетка змін (`speaker_review.json`) + `save_voice_profile_from_cluster` (.npy з ембедингу кластера, як `add_voice_profile.py`). Голосові профілі: `data/voice_profiles/<Ім'я>.npy`.
- Окрема кнопка **«🔁 Запустити аналіз»**: для нових учасників зберігає профіль → пише speakers.json → ставить зустріч у **повну переобробку (force)**.

### Редагування голосів обробленої зустрічі → повний re-run (`5344ed0`) + аудіо в редакторі спікерів (`82146ee`)

- «Учасники» на `/meetings/<date>` → кнопка **«🎙️ Редагувати голоси»** → редактор speakers.json; збереження = **force-переобробка** (`ingestion_jobs.force_reprocess`, schema **v3**). Worker регенерує annotated/analysis/protocol **на місці** (bypass skip + `--no-cache` + index `--force`) — стара версія вціліє при збої; сторінка не 404-иться.
- Бейдж «🔄 Перезбирається» на detail поки активна переобробка.
- У редакторі спікерів — аудіоплеєр + клікабельні таймкоди-приклади (послухати голос перед підписом).

### `restart_dev.sh` (dev-helper) `cb68e70`

Перевіряє контейнери (Postgres `cma-postgres`, Qdrant `lf-client-qdrant-1`) + Ollama/модель, тоді (пере)запускає 4 сервіси у фоні з `logs/`. Резолвить `docker` (shell-alias Docker Desktop) у реальний бінарник. Режими: `--check`, `--stop` (гасить і сервіси, **і контейнери**), `--help`.

---

## Run-модель (важливо — 4 процеси одночасно)

```
web / Telegram-бот  →  INSERT pending (queries)  →  worker → RAG → completed → delivery
web /ingest         →  INSERT pending (ingestion_jobs) → ingestion-worker →
                         transcribe → [PAUSE: web-редактор speakers] → analyze → index
                                                    ↕
                                  dashboard: live-моніторинг + дії
```

**Одна команда** піднімає все (перевіряє контейнери+Ollama, рестартує 4 сервіси у фоні):
```bash
./restart_dev.sh          # web + bot + query-worker + ingestion-worker; логи в logs/
./restart_dev.sh --stop   # зупинити сервіси + контейнери
```
Без query-worker'а запити висять у `pending`; без ingestion-worker'а завантажене аудіо висить у `pending`. Мають бути up: **Ollama з `gemma4:26b` ТА `bge-m3`** (`ollama pull bge-m3`), Qdrant, Postgres. `VOYAGE_API_KEY` більше **не потрібен**.

*(Терміналами вручну — ті самі 4 команди: `uvicorn …web.main:app`, `-m …bot.main`, `-m …worker.main`, `-m …ingestion.main`.)*

**Примітка:** контейнери на M1 інколи «відпадають» після сну машини — `restart_dev.sh` їх піднімає.

---

## Що робимо далі — план (backlog, prioritized)

**★ MVP-C validation — прогнати на реальному аудіо** ← **НАСТУПНЕ**
   Увесь код готовий, але **живими на реальному аудіо ще НЕ ганялись**: (а) повний свіжий
   ingestion (`/ingest` → transcribe → speakers → analyze → auto-index), (б) force-переобробка
   («Редагувати голоси»/«Запустити аналіз» → merge→analyze→polish→index). У тестах джоби
   ставились у чергу, але видалялись до обробки (щоб не тригерити 2-год Gemma). Треба живий прогін
   + перевірка, що `polished.md`/стенограма оновлюються і шукаються через RAG. Ймовірні дрібні фікси в stages.

**1. Калібрування rerank-порогів + якість retrieval на bge** — S
   Після Voyage→bge звірити релевантність на реальних запитах; підкрутити `RERANK_SCORE_GOOD/OK`.
   (Voyage давав +49% — переконатись, що локальний не просів.)

**2. Analytics US-3/US-4** — M
   Recurring topics, stale issues. Dashboard-інфра вже є (views + repo).

**3. Multi-query expansion + Hybrid BM25 (Phase 2B.2+)** — L
   Покращення retrieval-якості. bge-m3 підтримує dense+sparse — природний кандидат на hybrid.

**4. Cache embeddings перед Qdrant upsert** — S
   Уникнути повторного embed при re-index (тепер embed локальний, але все одно час).

— Ops: single-command run — ✅ **ЗРОБЛЕНО** (`restart_dev.sh`).
— Speakers editor UI — ✅ **ЗРОБЛЕНО** (MVP-C + `/meetings/<date>/speakers` + стенограма).
— Manual guest entry — ✅ по суті **ЗРОБЛЕНО** (web-редактори дозволяють вводити нових учасників).

Розмір: S=пів дня, M=1–2 сесії, L=кілька сесій.

---

## Development mode reminder

- Claude **редагує файли прямо в repo** (Edit/Write) і сам ганяє не-Telegram флоу: unit/smoke-тести, web-роути через curl, UI через браузер (claude-in-chrome). Комітить і пушить **коли Pavlo просить**.
- Claude НЕ тестує Telegram локально (немає доступу до TG API); тестує **Pavlo вручну**
- **Малі incremental commits** — по фічах; коміти прямо в `main` (solo-repo)
- `docs/` і `.specstory/` — untracked, поза feature-комітами
- Sanity після змін коду: перезапустити відповідний процес (uvicorn без `--reload` тримає старий код у памʼяті — часта причина «не працює»)

---

## Стартовий крок у новому chat

> Продовжуємо Church Meeting Assistant. Читай `docs/handoff_brief.md`.
> Робимо [пункт N з плану].

---

**End of brief.**
