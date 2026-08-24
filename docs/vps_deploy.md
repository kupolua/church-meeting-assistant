# Розʼїзд площин: VPS (control plane) + M1 (обробка)

**Створено:** 2026-08-24 · **Стан:** ранбук, ще не виконаний
**Передумови:** `docs/cloud_plan.md` §13, `docs/handoff_brief.md` backlog п.8

Виконувати **по кроках**, звіряючи результат після кожного. Кроки 1–6 нічого не
ламають: жива система на M1 працює далі, поки не зроблено крок 9. До нього все
оборотне видаленням контейнерів на VPS.

---

## 0. Що виходить

| VPS (4× Zen 1, 8 ГБ, 68 ГіБ) | M1 |
|---|---|
| web, telegram-bot | Ollama (gemma4:26b + bge-m3) |
| Postgres, Qdrant | query-worker, ingestion-worker |
| артефакти (аудіо, стенограми, протоколи) | тимчасові файли обробки |

**Жодної моделі на VPS.** Після `fc781a9` веб не виконує RAG — питання лягає в
чергу `queries`, відповідає воркер на M1. Тому там не потрібні ні Ollama, ні
torch, ні реранкер (3.5 ГБ, які в 8 ГБ не влазять разом із рештою).

**Що церква отримує 24/7:** протоколи, стенограми, PDF, **аудіоплеєр**, пошук
по темах, завантаження нового запису, приймання питання в Telegram.
**Що чекає на M1:** семантична відповідь і обробка запису.

**Аудіо зберігається** — рішення 24.08.2026: служителям потрібен доступ до
запису після того, як протокол готовий. Наслідки — крок 10.

### Що має лишитись правдою

- `DB_USER=cma_app`, **ніколи** `cma`. Суперюзер обходить RLS, і тоді ізоляція
  церков проходить вхолосту.
- `LEGACY_TENANT_SLUG=default` не чіпати — він мапить наявний корпус на
  колекції `cma_*`.
- Пароль ролі `cma_app` **не переставляти**. Він переїде хешем у `roles.sql`.

---

## 1. WireGuard

Тунель — єдиний шлях, яким M1 дістає Postgres і Qdrant. Публічно вони не
слухають ніде.

**На VPS:**
```bash
apt update && apt install -y wireguard
umask 077
wg genkey | tee /etc/wireguard/vps.key | wg pubkey > /etc/wireguard/vps.pub
cat /etc/wireguard/vps.pub          # ← знадобиться на M1
```

**На M1:**
```bash
brew install wireguard-tools
umask 077
wg genkey | tee ~/wg-m1.key | wg pubkey > ~/wg-m1.pub
cat ~/wg-m1.pub                     # ← знадобиться на VPS
```

Заповнити обидва конфіги з `deploy/wireguard/` і підняти:

```bash
# VPS
cp deploy/wireguard/wg0.vps.conf.example /etc/wireguard/wg0.conf
chmod 600 /etc/wireguard/wg0.conf   # вписати VPS_PRIVATE_KEY + M1_PUBLIC_KEY
systemctl enable --now wg-quick@wg0
ufw allow 51820/udp                 # єдиний порт, що дивиться назовні

# M1
cp deploy/wireguard/wg0.m1.conf.example /opt/homebrew/etc/wireguard/wg0.conf
chmod 600 /opt/homebrew/etc/wireguard/wg0.conf
sudo wg-quick up wg0
```

**Перевірка (з M1):**
```bash
sudo wg show          # має бути свіжий "latest handshake"
ping -c 3 10.10.0.1
```

Виміряно 24.08.2026 після підняття: **RTT ~21 мс**. Цю затримку платять **лише
воркери на M1**, і вони пакетні (полінг раз на 10–15 с). Веб на VPS ходить до
Postgres по локальній петлі, тож на відчуття церкви вона не впливає.

### Якщо handshake немає

Симптом на M1: рядка `latest handshake` немає взагалі, `transfer: 0 B received`,
а `sent` росте. Пінг до `10.10.0.1` при цьому теж мовчить — це наслідок, а не
окрема поломка, за ним гнатися не треба.

Розділити причини одним заходом. **На VPS:**
```bash
sudo tcpdump -ni any udp port 51820
```
**Паралельно на M1:** `ping 10.10.0.1` (щоб гарантовано генерувати трафік).

Пакети по 148 байт кожні ~5 с — це handshake-ініціації, тобто M1 справний.

⚠️ **tcpdump знімає трафік ДО netfilter.** Тому «пакети видно» ще не означає, що
їх отримав WireGuard: фаєрвол, який їх дропає, виглядає точно так само. Далі —
`sudo wg show` на VPS, вона розрізняє все:

| Що показує `wg show` на VPS | Діагноз |
|---|---|
| порожньо / «Unable to access interface» | wg0 не піднятий: `systemctl status wg-quick@wg0` |
| є інтерфейс, але `peer:` ≠ публічний ключ M1 | **невідповідність ключів** |
| інтерфейс і ключ збігаються, але `0 B received` | фаєрвол: `iptables -nvL INPUT`, шукати `DROP` зі зростаючим лічильником |

**Невідповідність ключів мовчить за задумом.** На handshake від невідомого ключа
WireGuard не відповідає нічим — щоб не підтверджувати сам факт існування
інтерфейсу. Через це вона й виглядає як заблокований порт.

Звірка — навхрест, по одному `wg show` з кожного боку:

| Має збігатися | З чим |
|---|---|
| `[Peer] PublicKey` на **VPS** | власний `public key` з `wg show` на **M1** |
| `[Peer] PublicKey` на **M1** | власний `public key` з `wg show` на **VPS** |

У конфігу peer'а завжди ключ **іншої** сторони. Генерувати наново нічого не
треба — `wg show` з обох боків уже показує справжні значення.

*(Саме це й сталося 24.08.2026: не збігався жоден із чотирьох ключів. Ключі
перегенерували після того, як конфіги вже заповнили — повторний
`wg genkey | tee …` перезаписує файл і міняє ідентичність.)*

---

## 2. База на VPS

```bash
adduser --system --group --home /srv/cma cma
apt install -y docker.io docker-compose-plugin git
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

git clone git@github.com:kupolua/church-meeting-assistant.git /srv/cma
chown -R cma:cma /srv/cma
mkdir -p /srv/cma/data /srv/cma/logs && chown cma:cma /srv/cma/data /srv/cma/logs
```

**Swap** — 8 ГБ без запасу; пік у Postgres не має вбивати веб:
```bash
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

---

## 3. Postgres + Qdrant на VPS

**Спершу — порядок завантаження.** Контейнери привʼязані до `10.10.0.1`, якої
без тунелю не існує. Docker, стартуючи раніше за `wg-quick`, отримає
`cannot assign requested address` — і після кожного ребуту церква лежатиме,
поки хтось не зайде руками.

```bash
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/wg.conf <<'EOF'
[Unit]
After=wg-quick@wg0.service
Requires=wg-quick@wg0.service
EOF
systemctl daemon-reload
```

```bash
cp deploy/env/vps.env.example /srv/cma/.env    # заповнити всі <ПЛЕЙСХОЛДЕРИ>
chmod 600 /srv/cma/.env && chown cma:cma /srv/cma/.env

cd /srv/cma
set -a && . ./.env && set +a
docker compose -f deploy/docker-compose.vps.yml up -d
```

**Перевірка:**
```bash
docker compose -f deploy/docker-compose.vps.yml ps     # обидва healthy
ss -tlnp | grep -E '5432|6333'                         # ТІЛЬКИ 10.10.0.1
```

Якщо в останньому рядку видно `0.0.0.0` — зупинити негайно: Qdrant без
автентифікації, і його доступність **і є** контроль доступу.

---

## 4. Експорт із M1

Скрипт **нічого не змінює** на M1 — лише dump, знімки й читання. Прогнаний
наживо 24.08.2026: після нього в Qdrant не лишилось жодного знімка, лічильники
й `ingestion_jobs` ті самі.

```bash
cd ~/sandbox/church-meeting-assistant
./deploy/migrate/export_from_m1.sh
```

Виміряний результат — не оцінка:

| Файл | Розмір |
|---|---|
| `roles.sql` | 926 Б |
| `cma.dump` | 216 КБ *(база 11 МБ, custom-формат стиснутий)* |
| `qdrant/cma_turns.snapshot` | 98 МБ |
| `qdrant/cma_analyses.snapshot` | 21 МБ |
| `qdrant/cma_protocols.snapshot` | 14 МБ |
| `qdrant/cma_protocol_full.snapshot` | 596 КБ |
| **разом** | **~134 МБ** |

⚠️ **Суперюзер тут — `cma`, не `postgres`.** Контейнер створювався з
`POSTGRES_USER=cma`, ролі `postgres` не існує взагалі, і дамп називає `cma`
власником кожної таблиці. Тому те саме імʼя стоїть у `docker-compose.vps.yml`.

---

## 5. Перенесення

```bash
# метадані та вектори (~134 МБ)
rsync -avz --progress migrate-out/ root@10.10.0.1:/srv/cma/migrate-in/

# артефакти (1.9 ГБ, з них 94% — аудіо; rsync резюмується)
rsync -avz --progress --partial \
      data/tenants/default/ root@10.10.0.1:/srv/cma/data/tenants/default/
```

Через тунель повільно — можна тимчасово по публічному SSH, дані однаково
шифруються транспортом.

**Тільки `data/tenants/default/`** (documents + meetings + voice_profiles).
Решта `data/` на M1 — це 154 МБ тестового аудіо, tmux-логи й старі бекапи
профілів; на сервері церкви їм робити нічого.

---

## 6. Імпорт і звірка

```bash
# на VPS
cd /srv/cma && ./deploy/migrate/import_to_vps.sh /srv/cma/migrate-in
```

Скрипт відмовиться працювати, якщо в базі вже є джоби — щоб повторний запуск
не затер перший.

**Звірити з M1 — числа мають збігтися точно:**

| Що | Значення на 24.08.2026 |
|---|---|
| `cma_protocols` | 597 |
| `cma_analyses` | 960 |
| `cma_turns` | 9280 |
| `cma_protocol_full` | 20 |
| `ingestion_jobs` | 6 |
| `queries` | 20 |
| тек зустрічей | 20 |

```bash
# на VPS
docker exec -e PGPASSWORD=… cma-postgres psql -U cma_app -d cma -c \
  "SET app.current_tenant=1; SELECT count(*) FROM ingestion_jobs;"
ls /srv/cma/data/tenants/default/meetings | wc -l
```

Розбіжність — **не переносити трафік**, розбиратись.

---

## 7. Артефакти: єдина річ, що лишається невирішеною

Веб на VPS читає теки зустрічей із локального диска — це працює одразу.
**Воркер на M1 має дістати ті самі теки**, і ось тут вибір, який треба зробити
свідомо.

Хто чим володіє, за станом джоба (стан-машина вже це серіалізує):

| Статус | Власник | Пише |
|---|---|---|
| `pending`/`transcribing` | M1 | transcript, rttm, embeddings, speakers.json |
| `awaiting_review` | VPS (веб) | speakers.json, voice_profiles |
| `analyzing`/`indexing` | M1 | annotated, chunks, polished |
| `completed` | VPS (веб) | speakers.json при переправці |

**Варіант A — мережевий маунт (нуль коду).** На M1 змонтувати
`/srv/cma/data` через NFS і виставити `DATA_ROOT` на точку монтування. Працює
сьогодні, бо `1165ad0` зробив шлях похідним від (тенант, дата) — абсолютні
шляхи більше не мусять збігатися.
⚠️ **Ризик, який треба назвати:** транскрипція триває **2–3 години**, і весь цей
час процес тримає відкритим файл на 68 МБ через тунель. Обрив звʼязку — і
робота падає посеред найдорожчої фази.

**Варіант B — rsync на переходах стану (правка 3, ~60 рядків).** Воркер тягне
теку до себе, коли забирає джоб, і віддає 660 КБ, коли завершив фазу. Обрив
звʼязку псує лише синхронізацію, а її ретрай уже є в `processor.py`.

**Рекомендація:** для першого запуску — A, щоб перевірити всю решту.
Але **не покладатись** на нього постійно: B зробити до того, як через систему
піде перша чужа церква.

---

## 8. systemd + TLS

```bash
cp deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cma-web cma-telegram-bot
systemctl status cma-web --no-pager
```

Веб слухає `127.0.0.1:8000`. Назовні — термінатор TLS:

```bash
apt install -y caddy
cat > /etc/caddy/Caddyfile <<'EOF'
cma.example.org {
    reverse_proxy 127.0.0.1:8000
    request_body { max_size 500MB }   # аудіо зустрічі — 65–106 МБ
}
EOF
systemctl reload caddy
```

`max_size` не забути: дефолт відріже завантаження запису, і форма впаде на
файлі, який церква щойно 20 хвилин вантажила.

**Перевірка:** відкрити `https://cma.example.org/login`, зайти як `pavlo`,
побачити 20 зустрічей і **програти аудіо** на сторінці зустрічі.

---

## 9. Cutover

Аж тепер M1 перестає бути сервером.

```bash
# на M1: дописати в .env рядки з deploy/env/m1.env.example
./restart_dev.sh --stop          # гасить сервіси і локальні контейнери
# виставити CMA_ROLE=worker, DB_HOST=10.10.0.1, QDRANT_URL, DATA_ROOT
./restart_dev.sh --check         # має показати «через тунель»
./restart_dev.sh                 # підніме лише два воркери
```

**Наскрізна перевірка:** поставити питання у вебі → воно має пройти
`pending` → `processing` → відповідь. Це доводить, що воркер на M1 бачить
чергу на VPS і Qdrant через тунель.

### Відкат

Контейнер `cma-postgres` на M1 **не видаляти** щонайменше тиждень. Відкат:
прибрати `CMA_ROLE`, повернути `DB_HOST=127.0.0.1`, `DB_PORT=5433`,
`QDRANT_URL=http://localhost:6333`, `DATA_ROOT` на локальну теку — і
`./restart_dev.sh`. Дані на M1 лишились тими самими, бо крок 4 нічого не
змінював. Втратиш лише те, що встигли зробити на VPS.

---

## 10. За чим стежити

**Диск.** Аудіо зберігається, отже ~68 МБ на зустріч. При 55 ГБ вільних:
одна церква — роки, три — близько семи років, десять — приблизно два.
Моніторити, а не згадати, коли впреться:
```bash
df -h /srv | tail -1
du -sh /srv/cma/data/tenants/*
```

**Бекап — новий обовʼязок.** До переїзду копія-джерело була на ноутбуку. Тепер
вона на VPS, і без бекапу ти проміняв «ноутбук помер» на «VPS помер». Мінімум:
щоденний `pg_dump` + щотижневий rsync артефактів на інший майданчик.

**Тунель.** `sudo wg show` на M1. Немає handshake — воркери не бачать черги, і
завантажене церквою аудіо просто чекає.

**Що дашборд поки не показує:** чи M1 узагалі на звʼязку. `health_checks`
пише воркер, тож коли M1 спить, панель показує **останній** знімок, а не
«обробник відсутній». Кандидат на наступну правку.
