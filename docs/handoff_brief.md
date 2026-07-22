# Church Meeting Assistant — Handoff Brief

**Дата:** 22 липня 2026
**Стан:** MVP-A (foundation + web + bot + worker), MVP-B (dashboard), **MVP-C (ingestion: upload audio → protocol)**, **meeting-detail (стенограма + аудіоплеєр із клікабельними таймкодами)** і **перебудова UI-навігації (топ-меню, дашборд-landing, сайдбар лише в «Зустрічі»)** — код готовий, **закомічено й запушено на GitHub** (`main`). MVP-A/B провалідовані end-to-end; MVP-C провалідований покроково (unit + web-роути через curl + браузер), але **ще не прогнаний на реальному аудіо** (повний ~2-год pipeline через worker).
**Мета наступної сесії:** прогнати MVP-C на реальному записі + обрати наступний пункт backlog (single-command run стає важливішим — тепер 4 процеси).

---

## Хто я

Pavlo Kulakovskyi. JS/DevOps 20+ років. Працюю в mentor+pair-coding mode з Claude:
"Claude пише код, я тестую та комітую."

Використовую **українську** для процесу і **англійську** для коду/architecture.

---

## Що вже готово (do NOT rewrite)

### Phase 2A/2B (completed, June 2026)

- **14 meetings indexed** у Qdrant (`cma_protocols`, `cma_analyses`, `cma_turns`, `cma_protocol_full`)
- ~7,886 points total
- Full audio → polished.md → RAG pipeline working
- CLI query.py з Voyage rerank-2 (+49% avg precision boost)

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

**DB (schema v2 — застосовано через `init_db.py`):**
- `ingestion_jobs` — черга+історія (unique per meeting_date, `stage`/`progress_note`, timings, `speaker_count`, `indexed`, retry)
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

---

## Run-модель (важливо — 4 процеси одночасно)

```
web / Telegram-бот  →  INSERT pending (queries)  →  worker → RAG → completed → delivery
web /ingest         →  INSERT pending (ingestion_jobs) → ingestion-worker →
                         transcribe → [PAUSE: web-редактор speakers] → analyze → index
                                                    ↕
                                  dashboard: live-моніторинг + дії
```

Для повного циклу підняти **чотири термінали**:
```bash
uv run uvicorn church_assistant.web.main:app --host 127.0.0.1 --port 8000   # web + dashboard + /ingest
uv run python -m church_assistant.bot.main                                   # Telegram bot
uv run python -m church_assistant.worker.main                                # query worker
uv run python -m church_assistant.ingestion.main                             # ingestion worker (NEW)
```
Без query-worker'а запити висять у `pending`; без ingestion-worker'а завантажене аудіо висить у `pending`. Ollama `gemma4:26b`, Qdrant, Postgres мають бути up.

**Відомий tech-debt:** запуск **чотирьох** процесів вручну — single-command run (Makefile/honcho) тепер ще цінніший (план, п.0).

---

## Що робимо далі — план (backlog, prioritized)

**★ MVP-C validation — прогнати на реальному аудіо** ← **НАСТУПНЕ**
   Код готовий (Phases 1–5). Завантажити реальний запис через `/ingest`, запустити
   `ingestion.main`, пройти повний цикл: transcribe → web-редактор speakers → analyze
   → auto-index. Перевірити, що `polished.md` зʼявляється в `/meetings/<date>` і шукається
   через RAG. Ймовірні дрібні фікси в командах stages після живого прогону.

**0. Ops: single-command run (Makefile / honcho/foreman)** — S ← тепер цінніше (4 процеси!)
   Один `make dev` піднімає web+bot+query-worker+ingestion-worker. Прибирає footgun «забув worker».

**1. Cache embeddings перед Qdrant upsert** — S (~30 хв)
   З попередніх ітерацій. Уникнути повторного embed при re-index.

**2. Analytics US-3/US-4** — M
   Recurring topics, stale issues. Dashboard-інфра вже є (views + repo).

**3. Multi-query expansion + Hybrid BM25 (Phase 2B.2+)** — L
   Покращення retrieval-якості. Найбільший вплив на якість, найбільший обсяг.

**4. Speakers editor UI** — ✅ **ЗРОБЛЕНО** (злито в MVP-C: `/ingest/{id}/speakers`).

**5. Manual guest entry в wrapper CLI** — S
   Ручне додавання гостей у `new_meeting` pipeline (web-редактор уже дозволяє вводити гостей вручну).

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
