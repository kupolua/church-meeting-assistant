# Multi-tenancy (MT) — Handoff (branch `feat/multi-tenancy`)

**Мета:** один сервер (план — DGX Spark, централізовано) обслуговує багато церков, із
**наглядовою радою** як моделлю довіри (audit-журнал + non-superuser доступ). Приватність:
усе локально (Voyage вже прибрано, LLM/embeddings/rerank локальні на M1 → потім Spark).

**Стан:** ✅ **CUTOVER ЗРОБЛЕНО** (2026-07-29). Живий `cma` мультитенантний: міграції 003–009
застосовані, застосунок конектиться як `cma_app` (non-superuser → RLS діє), 4 сервіси працюють.
Бекап перед cutover: `backups/cma_before_mt_20260729-163749.dump` + `.env` поруч (у `.gitignore`).
`main` досі не змержена — злити після періоду спостереження.

---

## ✅ Phase 1 — дата-шар (зроблено й доведено раніше)

- `003_multitenancy.sql` — `tenants`-реєстр; `tenant_id` на users/queries/logs/errors/
  ingestion_jobs (backfill → tenant 1); `audit_log` (append-only); **RLS ENABLE+FORCE +
  політики `tenant_isolation`**; SECURITY DEFINER `resolve_tenant_for_telegram`.
- `004_app_role_and_claim.sql` — роль **`cma_app`** (NOSUPERUSER, NOBYPASSRLS) + гранти;
  SECURITY DEFINER `claim_next_query()` / `claim_next_ingestion_job(text[])` — спільні
  воркери сканують чергу **крос-tenant**.
- `005_mt_fixups.sql` — `ingestion_jobs` унікальність по **(tenant_id, meeting_date)**;
  cross-tenant alert-хелпери.
- `db/tenant_context.py`, `db/tenants_repo.py`, `db/audit_repo.py`; 4 репозиторії
  tenant-aware (queries / ingestion_jobs / users / logs). `health_checks` — **глобальна**.
- Викликачі: `shared/logger.py`, web-роути, worker+ingestion processor'и, bot,
  `scripts/add_user`.

---

## ✅ Phase 3 — web-auth + FS + Qdrant (ця сесія)

### Web-auth (логін → сесія → tenant)
- **`006_web_auth.sql`** — таблиця `web_users` (tenant_id; `username` **глобально
  унікальний**; scrypt-хеш; role `member`/`admin`), RLS ENABLE+FORCE + `tenant_isolation`,
  SECURITY DEFINER **`resolve_tenant_for_web_user(text)`** (той самий bootstrap-трюк, що й
  для Telegram), гранти `cma_app`. `schema_version` = 7.
- **`db/web_users_repo.py`** — tenant-aware через `tenant_cursor`.
- **`web/security.py`** — **без нових залежностей** (stdlib): `hashlib.scrypt` для паролів
  (`scrypt$n$r$p$salt$hash` — параметри всередині хеша, тож вартість можна піднімати без
  інвалідації акаунтів) + HMAC-підписана cookie-сесія `<payload>.<sig>`.
  *(Спершу payload ніс саму особу — tenant/роль. Після 008 у ньому лише вказівник на рядок
  `web_sessions`; див. розділ «Серверні сесії».)*
- **`web/auth.py`** — `SessionUser`, `AuthMiddleware` (deny-by-default: усе, крім `/login` і
  `/static`, вимагає сесії; HTMX отримує 401 + `HX-Redirect`, бо htmx не вміє корисно
  слідувати за 303), cookie-хелпери.
- **`web/routes/auth.py`** — `GET/POST /login`, `POST /logout`. Порядок кроків: resolver →
  усе далі в RLS-контексті цього tenant'а → перевірка, що церква `is_active`. Невдачі
  однакові на вигляд і за часом (dummy-scrypt на невідомий логін) — жодного enumeration.
  Брутфорс-гальмо в пам'яті (8 спроб / 15 хв). `?next` тільки same-site.
- **`web/tenant.py`** — `current_user` / `current_tenant` / `current_tenant_slug` /
  `require_admin` із сесії. **Немає фолбеку на tenant 1** — його відсутність була б витоком.
- **`scripts/add_web_user.py`** — створення/список/деактивація/зміна пароля (`--tenant`
  приймає id або slug).
- `login.html`, у `base.html` — плашка «⛪ slug + ім'я + Вийти». Логін/вихід → `audit_log`.

### FS per-tenant
- **`shared/tenant_paths.py`** — `data/tenants/<slug>/{meetings,voice_profiles}`;
  `DATA_ROOT` з env; **валідація slug** (він приходить із cookie й із БД, а `..` вивів би за
  корінь — у `tenants.slug` такого обмеження немає, тож це точка контролю).
  **Legacy-фолбек:** slug `default` далі бачить `data/meetings` / `data/voice_profiles`,
  доки не створено новий підкаталог — тобто до й після міграції все працює.
- `shared/meetings_index.py` — усі входи приймають `meetings_dir` явно (константи більше
  немає). `ingestion/speaker_review.py` — `profiles_dir` параметром.
- web-роути (home/search/history/dashboard/ingest/meetings) резолвлять шляхи з сесії.
- `ingestion/stages.py` — `--profiles-dir` у `match_speakers`, `--tenant-slug` у
  `index_meeting`; `ingestion/processor.py` бере slug із claimed-рядка.
- **`scripts/migrate_tenant_fs.py`** — dry-run за замовчуванням; переносить legacy-теки
  **і переписує `ingestion_jobs.meeting_dir`** (там абсолютні шляхи — без цього
  переставлений job упав би через кілька годин у середині пайплайну).

### Qdrant per-tenant
- **`shared/collections.py`** — `kind` (protocols/analyses/turns/protocol_full) → фізична
  назва `t_<slug>_<kind>`; для legacy-slug лишаються `cma_*` (**існуючий корпус не треба
  переіндексовувати** — це години локального bge-m3). `kind_of()` розбирає обидві схеми.
- `rag.retrieve/answer` — **обов'язковий** `tenant_slug` (без дефолту: забудькуватий
  викликач має впасти з TypeError, а не прочитати чужий індекс). Порівняння переведені з
  назв на `Hit.kind`.
- `index_meeting.py --tenant-slug`, `query.py --tenant-slug` (обидва дефолтять у legacy).
- Викликачі: web query-роут (slug із сесії), `worker/processor.py` (slug із claimed-рядка
  через `tenants_repo.get_slug`, кешовано на час процесу).

### `_system` tenant + UI веб-акаунтів
- **`007_system_tenant.sql`** — рядок tenants із **детермінованим id = 0**, slug `_system`,
  `is_active = FALSE`. Чому 0, а не BIGSERIAL: `shared/logger.py` мусить обрати тенант ще до
  будь-якого tenant-контексту й «не має права падати» — async-лукап у реєстрі додав би режим
  відмови саме тому, що має працювати, коли решта ламається. `is_active = FALSE` тут несуче:
  веб-логін уже відмовляє неактивним тенантам, тож «увійти як платформа» неможливо задарма.
  Старі системні логи під tenant 1 **не переносяться** (нічим надійно не відрізнити).
- `SYSTEM_TENANT_ID` = 0 у `tenants_repo`; `tenant_paths`/`collections` відмовляють `_system`
  окремим винятком `SystemTenantHasNoArtifacts` — у платформи немає ні тек, ні векторів.
- `Logger` більше не ковтає помилки **мовчки**: перший провал запису йде в stderr один раз
  (`_warn_once`). Без цього невиконана міграція 007 = усі системні події зникають без сліду.
- Bot-хендлери (`admin`/`help`/`verbose`) тепер передають `tenant_id` — інакше активність
  члена церкви після зміни дефолту потрапляла б у `_system`. Ті, що його НЕ передають
  (`bot.unauthorized`, `web.login_failed` для невідомого логіну, `*.started`) — навмисно:
  там церква справді невідома.
- **`web/routes/admin.py`** + `admin_users.html` — `/admin/users`: список, створення,
  вимкнення/увімкнення, зміна ролі, скидання пароля. Два захисні правила, обидва про «не
  замкнути себе»: не можна змінити **власний** доступ і не можна прибрати **останнього
  активного адміна**. Колізія логіну з іншою церквою відхиляється **без розкриття** її
  існування. Кожна зміна → `audit_log`.
- Пункт «👥 Користувачі» у навігації видно лише адмінам; member на `/admin/users` отримує
  редірект на дашборд, а не голий 403.
- **Побічно знайдено й виправлено:** базовий `.dash-btn` не задавав `color` і успадковував
  білий від Pico — біле на білому. Досі не було видно, бо кожна наявна кнопка мала модифікатор
  із власним кольором.

### Серверні сесії (відкликані)
- **`008_web_sessions.sql`** — `web_sessions` під RLS. Cookie став **вказівником**: у ньому
  лише непрозорий токен, а вся особа резолвиться з БД на кожен запит. У таблиці зберігається
  **тільки sha256** токена (плюс UA/IP — щоб адмін розумів, чи «3 сесії» очікувані). Простий
  sha256, а не scrypt, саме тому, що вхід — 256 біт CSPRNG: сповільнювати нема чого перебирати.
- **`resolve_web_session(token_hash)`** — ЄДИНЕ визначення «сесія дійсна зараз»: не відкликана,
  не протухла, акаунт активний, церква активна. Через це деактивація діє **з наступного
  запиту** без коду, що бігає по сесіях, а роль/імʼя читаються живими — пониження прав або
  перейменування діє одразу, а не з наступного логіну.
- `web_sessions_repo` — create/resolve/touch(тротлінг 60с)/revoke/revoke_all/purge_expired.
  Вихід **відкликає рядок**, а не лише чистить cookie: cookie, перехоплений до виходу, мертвий.
  `revoke_all_for_user(except_session_id=…)` — щоб адмін, змінюючи СОБІ пароль, не вилітав зі
  сторінки, на якій це робить, а решта пристроїв вилітала.
- Підпис cookie лишили: це безкоштовний префільтр — сміттєвий cookie відкидається в процесі й
  не доходить до БД. Ротація `WEB_SECRET_KEY` і далі розлогінює всіх (грубий важіль поруч із
  точковим).
- Admin UI: колонка «сесії» (🟢N) + «вийти скрізь». Деактивація й скидання пароля тепер самі
  закривають сесії.
- **Ціна:** один індексований лукап на кожен захищений запит (публічні шляхи — без запиту).
  Це і є плата за можливість забрати доступ.

### Загартовування + самообслуговування
- **`009_session_idle_timeout.sql`** — idle-таймаут у `resolve_web_session` (нова версія з
  `p_idle_seconds`; односигнатурну з 008 **дропнуто**, інакше перевантаження могло б резолвити
  сесії без перевірки). Тепер там ДВІ незалежні межі: `WEB_SESSION_TTL` — абсолютна від входу
  (була завжди), `WEB_SESSION_IDLE_TIMEOUT` — від останнього використання. Обидві в тій самій
  функції, бо саме для цього вона й існує.
  ⚠️ `last_seen_at` оновлюється не частіше ніж раз на 60с — idle-вікно близьке до цього
  вибивало б людей посеред роботи. Старт друкує попередження, якщо вікно замале або
  безглузде (≥ TTL).
- **`WEB_COOKIE_SECURE=auto|true|false`** — `Secure` вирішується **на кожен запит** за схемою,
  бо той самий білд крутиться і по HTTP у церковній мережі, і за TLS на спільному сервері.
  `auto` каже правду за проксі лише з `--proxy-headers`; для такого випадку є `true`.
- **`web/headers.py`** — `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`
  завжди; HSTS лише коли запит реально https І деплой це підтвердив (інакше HTTP-коробка
  запинила б браузери до схеми, якої не вміє). Middleware додано **останнім** → працює
  найзовнішнім, тож заголовки є і на редіректах/401 від AuthMiddleware.
  CSP свідомо немає: htmx тягнеться з CDN, тож будь-яка чесна політика або дозволяла б
  довільний зовнішній скрипт, або ламала б UI — робити разом із вендорингом htmx.
- **`/account/sessions`** — «мої сесії» для **всіх ролей**: свої пристрої, поточний позначено,
  закрити одну або «вийти на всіх інших». Перевірка власності явна, а не через RLS: RLS
  обмежує церквою, чого замало — це дозволяло б закрити сесію колеги.
- **`scripts/cutover_preflight.py`** — READ-ONLY перевірка живої БД + рунбук із бекапом і
  відкатом. Cutover навмисно не автоматизований.

**Провалідовано:** `tests/mt_phase3_smoke.py` — **69 перевірок, усі PASS** на sandbox-БД
`cma_mt3` під роллю `cma_app`: RLS для `web_users`, глобальна унікальність логінів, реальний
HTTP-потік логіну (deny-by-default, HTMX 401, невірний пароль, невідомий логін, призупинена
церква, підроблений cookie, open-redirect), ізоляція FS (та сама дата у двох церквах, пошук,
голосові профілі, traversal), ізоляція колекцій, append-only audit, маршрутизація системних
подій у tenant 0, весь admin-UI (member не має доступу, захисні правила, крос-церковний id →
«не знайдено»), idle-таймаут окремо від абсолютної стелі, Secure-cookie й заголовки,
«мої сесії» (member має доступ, чужу сесію закрити не може) і сесії (відкликання діє одразу, деактивація вибиває сесію, replay cookie після
виходу мертвий, «вийти скрізь» у двох браузерах). Тест **ідемпотентний** — сам чистить
`web_users` на старті. Плюс module-level
smoke-тести: `web.security`, `shared.tenant_paths`, `shared.collections`,
`shared.meetings_index`, `ingestion.speaker_review`.

UI також перевірено в браузері вживу (створення акаунта, HTMX-свап панелі, повідомлення про
колізію логіну) — на sandbox-БД, окремим портом, живі сервіси не чіпалися.

---

## 🔑 Інваріанти / підводні камені (ОБОВʼЯЗКОВО памʼятати)

1. **RLS fail-closed** + ігнорується суперюзером. Застосунок МУСИТЬ конектитись як **`cma_app`**
   (не `cma` — це суперюзер контейнера, обходить RLS). Без tenant-контексту сесія бачить 0 рядків.
2. Кожна tenant-операція → `tenant_cursor(pool, tenant_id)`. Спільні воркери → SECURITY DEFINER
   `claim_next_*`. Bootstrap tenant бота → `resolve_tenant_for_telegram`; веб-логіну →
   `resolve_tenant_for_web_user`.
3. `users.telegram_user_id` і `web_users.username` — **глобально унікальні** (одна людина →
   одна церква). `ingestion_jobs` — унікальність по (tenant_id, meeting_date).
4. Системні логи → `SYSTEM_TENANT_ID` = **0** (`_system`, міграція 007). Розгортання без 007
   МУСИТЬ виставити `SYSTEM_TENANT_ID=1`, інакше FK-порушення й усі системні логи зникають
   (Logger попередить у stderr один раз, але не впаде).
5. **`WEB_SECRET_KEY` обов'язковий** — web не стартує без нього (`lifespan` падає одразу).
   Після 008 підпис уже не носить `tenant_id` (він у БД), але лишається префільтром від
   сміттєвих cookie; ротація ключа й далі розлогінює всіх (аварійний важіль).
6. **Усе про «сесія ще дійсна?» — лише в `resolve_web_session`.** Додаєш умову (напр. стелю
   життя) — там, і ніде більше; інакше зʼявиться другий екземпляр правил.
6. **Qdrant не має RLS.** Ізоляція там — виключно окремі колекції. Тому `tenant_slug` у
   `rag.*` без дефолту, а `resolve_kind()` навмисно НЕ приймає фізичну назву колекції.
7. Slug потрапляє і в шлях, і в назву колекції → `validate_slug()` перед обома.

---

## ✅ Cutover (2026-07-29)

Зроблено за рунбуком `scripts/cutover_preflight.py`. Порядок: зупинка 4 сервісів → бекап
(`pg_dump -Fc` + копія `.env`) → `DROP ROLE cma_app` (стара, з sandbox-паролем; довелося спершу
дропнути `cma_mt3`, який її тримав) → міграції 003–009 → новий 40-символьний пароль ролі →
`.env`: `DB_USER=cma_app` + новий `DB_PASSWORD` → preflight (усе ✓) → перший веб-акаунт
`pavlo`/admin → старт сервісів.

**Провалідовано на живій системі:** дані цілі (users=1, queries=11, logs, errors=36, jobs=3;
усі рядки на tenant 1); анонім → 303 на `/login`; невірний пароль → 401; вхід → дашборд 200
під RLS; security-заголовки; 17 зустрічей у сайдбарі (**legacy-фолбек FS працює** — `data/meetings`
не переносили); RAG-пошук по `cma_protocols` (**переіндексації не було**); воркер підхопив запит
через крос-tenant `claim_next_query()` і довів його до `completed` (Gemma, 111с); системні логи
йдуть у tenant 0, церковні — в tenant 1; audit_log пише входи.

**Не перевірено вживу:** Telegram-бот (потрібне реальне повідомлення від whitelisted-користувача).
Процес запущений і підключений (`@ReChurchAssistant_bot`).

---

## ⏭️ Що лишилось

1. **Змінити пароль першого акаунта** `pavlo` — він видавався одноразово при створенні.
2. Помержити `main` після періоду спостереження (зараз усе на `feat/multi-tenancy`).
3. Вендорити htmx локально → тоді можна ввімкнути CSP (зараз CDN у `base.html`).
4. `migrate_tenant_fs.py` — коли захочеться однорідної розкладки (не обовʼязково: legacy-фолбек
   лишає tenant 1 там, де він є).

---

## 🧪 Як тестувати MT (sandbox-патерн — НЕ чіпати живий `cma`)

Повний рецепт (створення БД, міграції, сідинг двох церков) — у докстрінгу
`tests/mt_phase3_smoke.py`. Коротко:

⚠️ **НІКОЛИ не дропати й не переставляти пароль ролі `cma_app` у тестовому рецепті.** Ролі
кластерні, гранти — на базу. Після cutover саме під цією роллю логіняться 4 живі сервіси, тож
`DROP ROLE` / `ALTER ROLE … PASSWORD` поклали б продакшн. Sandbox переюзує наявну роль і її
справжній пароль із `.env`; ізолює саме **імʼя бази** (`cma_mt3`), а тест на старті відмовляється
працювати, якщо `DB_NAME` не той.

```bash
DOCKER=/Applications/Docker.app/Contents/Resources/bin/docker
$DOCKER exec cma-postgres psql -U cma -d postgres -c "DROP DATABASE IF EXISTS cma_mt3;" \
  -c "CREATE DATABASE cma_mt3;"
# heredoc/stdin ПОТРЕБУЄ  docker exec -i  (без -i stdin не доходить!)
for f in schema.sql migrations/003_multitenancy.sql migrations/004_app_role_and_claim.sql \
         migrations/005_mt_fixups.sql migrations/006_web_auth.sql; do
  $DOCKER exec -i cma-postgres psql -U cma -d cma_mt3 -v ON_ERROR_STOP=1 < src/church_assistant/db/$f
done
# + сідинг церков (див. докстрінг), далі:
uv run python tests/mt_phase3_smoke.py
```

Чому саме так: `127.0.0.1:5433` через Docker-forward доходить у контейнер як bridge-gateway
→ спрацьовує scram-правило → **потрібен пароль** (не trust). І конектитись треба як
`cma_app`, інакше суперюзер обходить RLS і всі перевірки ізоляції пройдуть вхолосту.

---

## Стартовий крок у новому вікні

> Продовжуємо мультитенантність Church Meeting Assistant на гілці `feat/multi-tenancy`.
> Прочитай `docs/mt_handoff.md`. Робимо [cutover / CSP+вендоринг htmx].

**Перед cutover:** `uv run python -m church_assistant.scripts.cutover_preflight` — він нічого
не змінює, друкує стан живої БД і покроковий рунбук із бекапом та відкатом.

**Джерело істини — гілка на GitHub.** Комітити далі в `feat/multi-tenancy`; `main` не чіпати до cutover.
