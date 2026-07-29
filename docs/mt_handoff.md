# Multi-tenancy (MT) — Handoff (branch `feat/multi-tenancy`)

**Мета:** один сервер (план — DGX Spark, централізовано) обслуговує багато церков, із
**наглядовою радою** як моделлю довіри (audit-журнал + non-superuser доступ). Приватність:
усе локально (Voyage вже прибрано, LLM/embeddings/rerank локальні на M1 → потім Spark).

**Стан гілки:** `feat/multi-tenancy`, запушена (6 комітів після `main`). **`main` недоторкана**,
живий `cma` досі однотенантний, 4 сервіси працюють. Уся MT-робота — лише на гілці.

---

## ✅ Зроблено й ДОВЕДЕНО (на throwaway sandbox-БД; живий `cma` не чіпали)

**Дата-шар:**
- `db/migrations/003_multitenancy.sql` — `tenants`-реєстр; `tenant_id` на users/queries/logs/
  errors/ingestion_jobs (backfill → tenant 1 = наявна церква); `audit_log` (append-only);
  **RLS ENABLE+FORCE + політики `tenant_isolation`** (USING+WITH CHECK на
  `current_setting('app.current_tenant')`); SECURITY DEFINER `resolve_tenant_for_telegram`.
- `004_app_role_and_claim.sql` — роль **`cma_app`** (NOSUPERUSER, NOBYPASSRLS) + гранти;
  SECURITY DEFINER `claim_next_query()` / `claim_next_ingestion_job(text[])` — щоб спільні
  воркери сканували чергу **крос-tenant** (RLS-таблиці інакше нічого не бачать).
- `005_mt_fixups.sql` — `ingestion_jobs` унікальність по **(tenant_id, meeting_date)** (дві
  церкви можуть мати зустріч в один день); cross-tenant alert-хелпери
  `list_unalerted_errors_all` / `mark_error_alerted_any`.
- `db/tenant_context.py` — `set_tenant`, `tenant_cursor(pool, tenant_id)`, `resolve_tenant_for_telegram`.
- `db/tenants_repo.py` (реєстр), `db/audit_repo.py` (append-only, backbone ради).
- **4 репозиторії tenant-aware:** queries / ingestion_jobs / users / logs. `health_checks` — **глобальна**
  (без RLS). Черги — через claim-функції; verbose/bootstrap — через resolver.

**Викликачі (усі провалено, RLS активна через увесь застосунок):**
- `shared/logger.py` — Logger несе tenant (instance-default + per-call; системні → `SYSTEM_TENANT_ID`).
- `web/tenant.py` — `current_tenant(request)` → **DEFAULT_TENANT_ID (1)** доки немає web-auth.
- web-роути (query/dashboard/ingest/meetings/history), worker+ingestion processor'и (tenant з
  claimed-рядка), bot (whitelist резолвить tenant першим → `TENANT_KEY`; handlers+delivery),
  `scripts/add_user` (`--tenant`, дефолт 1).

**Провалідовано:** RLS-ізоляція (church A невидима church B), крос-tenant claim черги, audit
append-only, resolver, global-unique users, per-tenant same-date jobs, + інтеграційний smoke
(web-insert + bot-insert + worker claim/complete + logger + ізоляція) — усе PASS.

---

## 🔑 Інваріанти / підводні камені (ОБОВʼЯЗКОВО памʼятати)

1. **RLS fail-closed** + ігнорується суперюзером. Застосунок МУСИТЬ конектитись як **`cma_app`**
   (не `cma` — це суперюзер контейнера, обходить RLS). Без tenant-контексту сесія бачить 0 рядків.
2. Кожна tenant-операція → `tenant_cursor(pool, tenant_id)`. Спільні воркери → SECURITY DEFINER
   `claim_next_*`. Bootstrap tenant бота → `resolve_tenant_for_telegram` (SECURITY DEFINER).
3. `users.telegram_user_id` — **глобально унікальний** (одна людина → одна церква).
   `ingestion_jobs` — унікальність по (tenant_id, meeting_date).
4. Системні логи → `SYSTEM_TENANT_ID` (=1 тимчасово; TODO виділений `_system` tenant).

---

## ⏭️ Що лишилось до «повного MT»

1. **Web-auth** (логін → сесія → tenant). Зараз web дефолтить на tenant 1 — змінити лише в
   `web/tenant.py`. Найбільша net-new робота.
2. **FS per-tenant**: `data/tenants/<slug>/meetings/…` + `…/voice_profiles/…` (зараз спільні).
   Торкнеться `ingestion/paths.py`, ingest-upload, speaker_review, meetings_index.
3. **Qdrant колекції-на-тенанта** (`t_<slug>_protocols` …) або нативна MT Qdrant — зараз колекції
   спільні (rag.py / index_meeting.py / query.py).
4. Виділений `_system` tenant для системних логів.
5. **Live cutover (координовано, РУЙНІВНО):** створити роль `cma_app` + пароль → застосувати
   `003`+`004`+`005` до `cma` → `DB_USER=cma_app`/`DB_PASSWORD` у `.env` → рестарт сервісів.
   RLS fail-closed: старий код без tenant-контексту побачить 0 рядків, тож деплой лише разом.

---

## 🧪 Як тестувати MT (sandbox-патерн — НЕ чіпати живий `cma`)

```bash
DOCKER=/Applications/Docker.app/Contents/Resources/bin/docker
# окрема БД у тому ж контейнері
$DOCKER exec cma-postgres psql -U cma -d postgres -c "DROP DATABASE IF EXISTS cma_mt_test;" \
  -c "DROP ROLE IF EXISTS cma_app;" -c "CREATE DATABASE cma_mt_test;"
# heredoc/stdin ПОТРЕБУЄ  docker exec -i  (без -i stdin не доходить!)
for f in schema.sql migrations/003_multitenancy.sql migrations/004_app_role_and_claim.sql \
         migrations/005_mt_fixups.sql; do
  $DOCKER exec -i cma-postgres psql -U cma -d cma_mt_test -v ON_ERROR_STOP=1 < src/church_assistant/db/$f
done
$DOCKER exec cma-postgres psql -U cma -d cma_mt_test -c "ALTER ROLE cma_app PASSWORD 'testpass';"
# Python-тест: конект як cma_app. 127.0.0.1:5433 через Docker-forward доходить у контейнер як
# bridge-gateway → спрацьовує scram-правило → ПОТРІБЕН пароль (не trust):
#   DB_NAME=cma_mt_test DB_USER=cma_app DB_PASSWORD=testpass uv run python ...
# у сесії: SELECT set_config('app.current_tenant','<id>',false);  (або tenant_cursor)
```

---

## Стартовий крок у новому вікні

> Продовжуємо мультитенантність Church Meeting Assistant на гілці `feat/multi-tenancy`.
> Прочитай `docs/mt_handoff.md`. Робимо [Web-auth / FS per-tenant / Qdrant per-tenant / cutover].

**Джерело істини — гілка на GitHub.** Комітити далі в `feat/multi-tenancy`; `main` не чіпати до cutover.
