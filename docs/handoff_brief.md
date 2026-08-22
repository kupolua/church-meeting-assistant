# Church Meeting Assistant — Handoff Brief

**Дата:** 18 серпня 2026 *(попередня редакція — 29 липня; розділи нижче датовані)*
**Стан:** MVP-A (foundation + web + bot + worker), MVP-B (dashboard), MVP-C (ingestion), meeting-detail (стенограма + аудіоплеєр), UI-навігація, редагування спікерів, **повний self-host (bge-m3 + bge-reranker, Voyage прибрано)**, **мультитенантність у продакшні з 29.07** (див. `docs/mt_handoff.md`) і **PDF-експорт** (протоколи + власні Markdown-документи). Усе закомічено в `main` і запушено на GitHub.
**Ключове рішення (приватність):** усе, що торкається даних зустрічей, працює **локально**. Хмарний LLM (SiliconFlow тощо) і хмарний VPS відхилені — транскрипти не мають виходити з машини. Voyage (останню сторонню AI-залежність) прибрано.
**Система в реальній експлуатації:** корпус виріс із 17 до **20 зустрічей**; свіжі (`2026-06-27`, `2026-07-30`, `2026-08-03`, `2026-08-17`) пройшли повний ingestion через `/ingest` уже після мультитенантного cutover'а.
**Мета наступної сесії:** обрати пункт з backlog (див. «Що робимо далі») — блокерів немає.
**Відкритий напрям:** перенесення обробки в хмару зі збереженням конфіденційності — `docs/cloud_plan.md` (чернетка, рішення не ухвалене).

---

## Хто я

Pavlo Kulakovskyi. JS/DevOps 20+ років. Працюю в mentor+pair-coding mode з Claude:
"Claude пише код, я тестую та комітую."

Використовую **українську** для процесу і **англійську** для коду/architecture.

---

## Що вже готово (do NOT rewrite)

### Phase 2A/2B (completed, June 2026)

- Тоді — **17 meetings indexed**; станом на 18.08.2026 — **20** (`cma_protocols` 597, `cma_analyses` 960, `cma_turns` 9279, `cma_protocol_full` 20).
  ⚠️ Назви колекцій лишились `cma_*`, а не `t_default_*` — це навмисний legacy-виняток мультитенантності (`LEGACY_TENANT_SLUG`), див. `docs/mt_handoff.md`.
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

**✅ Провалідовано живим аудіо** (див. «MVP-C у бою» нижче) — це TODO знято.

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

*(Незакомічене в робочій копії: `--stop` гасить ще й langfuse-контейнери — рядок доданий у `stop_containers()` хардкодом на абсолютний шлях до `docker`, без `if`-обгортки, як у решти функції.)*

### ★ Мультитенантність — у продакшні (29 липня) ✅ 11 комітів, змержено в `main`

Один сервер обслуговує багато церков; модель довіри — наглядова рада (audit-журнал + non-superuser доступ). **Cutover виконано 29.07.2026:** живий `cma` мультитенантний, міграції 003–009 застосовані, застосунок конектиться як `cma_app` (тобто **RLS справді діє**), веб-логін із серверними сесіями, per-tenant FS (`data/tenants/<slug>/…`) і per-tenant Qdrant-колекції.

**Повний опис — `docs/mt_handoff.md`.** Що звідти треба знати, навіть якщо MT не чіпаєш:

- **Застосунок МУСИТЬ ходити як `cma_app`**, не `cma` (суперюзер обходить RLS).
- Кожна tenant-операція — через `tenant_cursor(pool, tenant_id)`; спільні воркери — через SECURITY DEFINER `claim_next_*`.
- **`LEGACY_TENANT_SLUG=default` прибирати НЕ можна** — вона мапить наявний корпус на колекції `cma_*`. Очистити = зламати RAG.
- `rag.retrieve/answer` вимагають `tenant_slug` **без дефолту** — Qdrant не має RLS, ізоляція там лише через окремі назви колекцій.
- Веб тепер за логіном: `/login`, `/admin/users` (адмінам), `/account/sessions` (усім). Живий адмін-акаунт — `pavlo`.

### Строга CSP + вендоринг htmx (29 липня) і дві регресії після неї (31 липня)

`5587b75` прибрав останню CDN-залежність (htmx → `static/htmx.min.js` 1.9.10 + `VENDOR.md` із sha256) і ввів CSP без `unsafe-inline`/`unsafe-eval`. Ціна виявилась через два дні:

- **`43df9d7`** — сканер шаблонів ловив inline-обробники та inline-`<style>`, але **не** inline-`<script>`. Три блоки тихо перестали виконуватись: клікабельні таймкоди (стенограма + теми) і діалог зміни спікера на `/meetings/<date>`, seek-хендлер на редакторі спікерів. Переїхали у `static/audio-seek.js` + `static/meeting-detail.js`; у `base.html` зʼявився блок `scripts` — **сиблінг `main`, не вкладений** (вкладений рендериться двічі й дублює обробники). Сканер тепер перевіряє й `<script` без `src=`, і `javascript:`-URL.
- **`4b949d0`** — «🔁 Запустити аналіз» постив на `/meetings//run-analysis` (404). Партіал будує URL із `date`, а `meeting_detail.html` передавав те саме значення як `current_date`; Jinja рендерить невизначену змінну порожнім рядком. Виглядало як «іноді працює», бо HTMX-шлях перерендерював панель правильно, а перезавантаження сторінки — ні. Прибито через `{% with date = current_date %}` на місці include.

**Урок на майбутнє:** ці дві поломки видно лише в браузері — сервер віддає 200, тест проходить. Після будь-якої зміни CSP/шаблонів клікати вживу.

### ★ MVP-C у бою — реальні зустрічі (кінець липня — серпень) ✅

Пункт «прогнати ingestion на реальному аудіо» закритий **експлуатацією, а не тестом**. Через живий `/ingest` пройшли `2026-07-02`, `2026-07-27`, `2026-07-30`, `2026-08-03`, `2026-08-17` — усі `completed` + `indexed`; серед них є й прогони з `force_reprocess=true`, тобто **force-переобробка теж перевірена живою**. Останній прогін (`2026-08-17`, 68 МБ m4a): повний цикл transcribe → speakers → analyze → polish → index за ~3 години, 358 turn-чанків у Qdrant.

Один job (`2026-06-27`) лишився `cancelled` — нормальний робочий стан, не поломка.

### ★ PDF-експорт (4–5 серпня) ✅ `1464390`, `4f40240`, `45f6e07`

**Навіщо:** протокол потрібен церкві **на папері** — роздати, підписати, підшити.

- **`shared/pdf_export.py`** (reportlab — pure Python поверх наявного PIL, без нативних бібліотек, це важливо для майбутнього Spark).
  - `build_topics_pdf(...)` — секція «Теми» як документ: заголовок «Пасторська зустріч 30.07.2026», під ним блок **«Присутні (N): …»**, далі нумеровані теми з текстом. Будується з **тих самих** розпарсених тем, що рендерить сторінка, тож розійтись вони не можуть.
  - `build_document_pdf(title, sections, header_note)` — виділена верстка (45f6e07), meeting-версія стала тонким викликачем.
  - **Шрифт шукається, не припускається** (`_resolve_font`): вбудовані шрифти PDF — Latin-1, український протокол ними — сторінка чорних квадратів **мовчки**. Пошук: macOS Arial + типові Linux-пакети (DejaVu/Liberation/Noto), override `PDF_FONT_PATH`, інакше виняток → роут віддає 503 із назвою пакета.
  - ⚠️ `bulletFontName` за замовчуванням **Times-Roman, а не шрифт абзацу** — булети виглядали правильно, але екстрактились як `(cid:127)`. Знайдено вилученням тексту назад із PDF, а не оглядом сторінки.
  - Таймкоди вирізаються **тим самим правилом**, що лінкує їх у `meeting-detail.js`: дужки, які складаються **виключно** з таймкодів. Саме тому «Псалом 84:6» виживає.
  - `**жирний**` від Gemma конвертується у справжній bold; екранування йде **перед** конвертацією, тож текст протоколу не може інжектнути reportlab-розмітку.
- **Роут** `GET /meetings/{date}/topics.pdf` — за auth-гейтом; чужа церква отримує 404, а не чужий протокол. Кирилична назва файлу — RFC 5987 `filename*` + ASCII-фолбек. Кнопка **⤓ PDF** біля заголовка «Теми».
- **`scripts/markdown_to_pdf.py`** — рендер довільного рукописного Markdown тією ж версткою (перший кейс — вимоги до бухгалтера, `data/tenants/default/documents/`). Джерело лишається Markdown навмисно: такі документи правляться й діфаються, а текст, замкнений у PDF, — ні. Парсер знає рівно чотири конструкції (`#`, `>`, `##`, булети) — усе несподіване рендериться як звичайний текст замість тихої переінтерпретації.

```bash
uv run python -m church_assistant.scripts.markdown_to_pdf INPUT.md [-o OUT.pdf]
```

### ★ Ручне додавання спікера, якого не почула діаризація (18 серпня) ✅

**Проблема:** учасник сказав дві фрази — pyannote не виділив його в окремий кластер. Перейменовувати
нічого: голосу просто немає ні в `speakers.json`, ні в «Присутніх». Але людина знає **час**.

- **`ingestion/manual_speakers.py`** (новий модуль) — типізований час + імʼя перетворюються на те,
  з чим уже вміє працювати весь пайплайн: **сегмент діаризації**.
  - `_meta.manual_speakers` у `speakers.json` — джерело істини (label, name, спец-рядок, вікна),
    плюс звичайний запис `SPEAKER_07: "Імʼя"` у мапінгу.
  - **`diarization.rttm` перезбирається**: pyannote-сегменти МІНУС ручні вікна ПЛЮС ручні рядки.
    Оригінал зберігається один раз як **`diarization.pyannote.rttm`** і кожна правка будується
    з нього — тож правки не нашаровуються, а зняття ручного спікера повертає файл байт-у-байт.
  - ⚠️ **Віднімання — не косметика.** `merge_transcript` обирає **домінантного** мовця за перекриттям:
    ручний сегмент, просто вставлений поряд із довшим, програв би голосування і правка «зберіглася б»,
    не змінивши нічого.
  - Вікна **прилипають до транскрипту**: атрибуція йде по whisper-сегментах, половину репліки
    переприсвоїти не можна. Гола мітка забирає репліку, сказану в ту мить; діапазон — репліки,
    які він покриває більш ніж наполовину; час у тиші отримує 5-секундне вікно (щоб людина
    все одно потрапила в «Присутні»).
  - Формати: `1:23:45`, `12:30, 47:05`, `12:30-12:50`. Помилка в часі **скасовує весь сабміт** —
    краще редірект із повідомленням, ніж напівзбережений файл і черга на кілька годин переобробки.
- **`polish_protocol.detect_attendees`** — ручні мітки **обходять поріг 30 с**. Хто сказав пару фраз,
  ніколи його не перетне, а сенс ручного додавання саме в тому, що людина стверджує: він був.
- **UI** — у таблиці «Редагування голосів» (і в ревʼю під час інжесту, шаблон спільний) зʼявився
  останній рядок: `SPEAKER_XX` (наступна вільна мітка, рахується на сервері) · **поле часу** ·
  «заповниться після збереження» · **поле імені**. Збережений ручний рядок повертається з бейджем
  ✋, редагованим часом і чекбоксом «прибрати».
- Голосового профілю такий спікер не має (немає кластера → немає ембедингу); `save_voice_profile_from_cluster`
  на цьому повертає зрозуміле повідомлення, а не падає.

**Провалідовано:** module smoke (`uv run python -m church_assistant.ingestion.manual_speakers`, 8 блоків)
+ 9 нових перевірок у `tests/mt_phase3_smoke.py` (**89 разом**): парсинг часу, прилипання до репліки,
віднімання вікна в RTTM, «Присутні» попри 30 с, редагованість збереженого рядка, крос-tenant 404,
відкат до оригіналу.

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

**Після мультитенантності:** веб більше не анонімний — усе, крім `/login` і `/static`, вимагає сесії
(логін `pavlo`). У `.env` обовʼязкові `WEB_SECRET_KEY` (без нього web не стартує) і `DB_USER=cma_app`
(під `cma` RLS вимикається і ізоляція проходить вхолосту). Артефакти зустрічей тепер лежать у
`data/tenants/default/meetings/<date>/`, голосові профілі — у `data/tenants/default/voice_profiles/`.

---

## Що робимо далі — план (backlog, prioritized)

*(Оновлено 18.08.2026. Блокерів немає — попереднє «★ НАСТУПНЕ» (валідація MVP-C) закрите живою експлуатацією.)*

**1. Калібрування rerank-порогів + якість retrieval на bge** — S ← **найстаріший невиконаний борг**
   Після Voyage→bge звірити релевантність на реальних запитах; підкрутити `RERANK_SCORE_GOOD/OK`
   (зараз 0.65/0.35 — first-pass). Впливає лише на кольори в UI, не на якість відповіді.
   Корпус із 17 зустрічей виріс до 20 — матеріалу для звірки більше.

**2. Прибрати сміття після cutover'а** — S
   - Веб-акаунт `test` (id=2, member, **досі активний**) — `/admin/users`.
   - Мертва `VOYAGE_API_KEY` у `.env`.
   - Незакомічена правка `restart_dev.sh` (langfuse-контейнери) — довести до ладу або відкотити.

**3. Analytics US-3/US-4** — M
   Recurring topics, stale issues. Dashboard-інфра вже є (views + repo); питання з `phase2b_plan.md`
   («які питання обговорювались 3+ разів і не попали в протокол?») досі без відповіді.

**4. Multi-query expansion + Hybrid BM25 (Phase 2B.2+)** — L
   bge-m3 підтримує dense+sparse — природний кандидат на hybrid.

**5. Cache embeddings перед Qdrant upsert** — S
   Уникнути повторного embed при re-index (embed локальний, але час усе одно).

**6. Переіндексація Qdrant у `t_default_*`** — M, **необовʼязково**
   Єдиний хвіст legacy-винятку MT. Дорого (години локального bge-m3) і без функційного виграшу,
   поки церква одна. Робити лише заради однорідності назв.

**7. Хмарна обробка для кількох церков** — L, **чернетка**
   `docs/cloud_plan.md`: GEX44 як ефемерний обробний вузол, артефакти на сервері **шифротекстом**,
   ключ у церкви, пошук на пристроях служителів. Причина: «купіть M1 32 ГБ» не масштабується
   на зацікавлені церкви. Перед кодом — етап 0: згода церков + бенчмарк GEX44.

**Не перевірене вживу після cutover'а:** Telegram-бот (потрібне реальне повідомлення від
whitelisted-користувача; процес запущений і підключений).

— Ops: single-command run — ✅ **ЗРОБЛЕНО** (`restart_dev.sh`).
— Speakers editor UI — ✅ **ЗРОБЛЕНО** (MVP-C + `/meetings/<date>/speakers` + стенограма).
— Manual guest entry — ✅ **ЗРОБЛЕНО** (веб-редактори дозволяють вводити нових учасників).
— MVP-C validation на реальному аудіо — ✅ **ЗРОБЛЕНО** (5 живих зустрічей, у т.ч. force-reprocess).
— PDF протоколів — ✅ **ЗРОБЛЕНО** (серпень).

Розмір: S=пів дня, M=1–2 сесії, L=кілька сесій.

---

## Development mode reminder

- Claude **редагує файли прямо в repo** (Edit/Write) і сам ганяє не-Telegram флоу: unit/smoke-тести, web-роути через curl, UI через браузер (claude-in-chrome). Комітить і пушить **коли Pavlo просить**.
- Claude НЕ тестує Telegram локально (немає доступу до TG API); тестує **Pavlo вручну**
- **Малі incremental commits** — по фічах; коміти прямо в `main` (solo-repo)
- `docs/` **відстежується git'ом** і комітиться окремими `docs:`-комітами, поза feature-комітами
  *(рядок «docs/ untracked» був застарілий — файли в репо з 29.07);* `.specstory/` — untracked
- Sanity після змін коду: перезапустити відповідний процес (uvicorn без `--reload` тримає старий код у памʼяті — часта причина «не працює»)
- ⚠️ **Живі сервіси крутяться з робочої копії** — `git checkout`/`stash` у цьому дереві б'є по продакшну церкви. Мержити переміщенням ref'а, а не переключенням дерева під запущеними процесами.
- ⚠️ **Тести MT — лише на sandbox-БД** (`cma_mt3`), і **ніколи** не дропати й не переставляти пароль ролі `cma_app`: під нею логіняться 4 живі сервіси. Рецепт — у докстрінгу `tests/mt_phase3_smoke.py`.
- Регресійний набір: `uv run python tests/mt_phase3_smoke.py` — **89 перевірок** (MT-ізоляція, auth, сесії, admin-UI, PDF-експорт, ручні спікери, сканер шаблонів на CDN/inline).

---

## Стартовий крок у новому chat

> Продовжуємо Church Meeting Assistant. Читай `docs/handoff_brief.md`
> (і `docs/mt_handoff.md`, якщо чіпаємо БД, auth або Qdrant). Робимо [пункт N з плану].

---

**End of brief.**
