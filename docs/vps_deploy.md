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

⚠️ **Кожен наступний `git pull` — під `cma`, не під root:**
```bash
sudo -u cma git -C /srv/cma pull
```
Root'ів umask кладе файли так, що сервісний користувач їх не читає, і `cma-web`
падає з `PermissionError` на першому ж імпорті: Caddy віддає 502, а в журналі
застосунку — стек імпорту без жодної підказки, що річ у правах. Так сталося
25.08. Лікували `chown -R cma:cma /srv/cma` — і **саме воно спричинило другу,
гіршу аварію**: під `/srv/cma/deploy/data` тоді лежав том Postgres, який після
цього перестав читатися. Тому томи винесені (крок 3), а масові зміни прав
обмежуються вихідниками:

```bash
chown -R cma:cma /srv/cma/src /srv/cma/docs /srv/cma/tests /srv/cma/deploy
chmod 700 /srv/cma/.ssh && chmod 600 /srv/cma/.ssh/authorized_keys /srv/cma/.env
```

`.ssh` під `chmod -R go=rX` стає читабельним для інших, і sshd відмовляє ключу —
тобто разом із вебом ляже й синхронізація артефактів із M1.

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

**Три плейсхолдери — і лише один із них вільний.**

| Змінна | Звідки |
|---|---|
| `DB_PASSWORD` | пароль `cma_app` **із M1** (`grep '^DB_PASSWORD=' .env`) |
| `POSTGRES_SUPERUSER_PASSWORD` | пароль суперюзера **із M1** (`docker inspect cma-postgres … \| grep POSTGRES_PASSWORD`) |
| `WEB_SECRET_KEY` | **новий**, згенерувати тут: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |

Обидва паролі БД **не вигадуються**: `roles.sql` містить `ALTER ROLE … PASSWORD '<хеш>'`
і для `cma_app`, і для `cma`. Для `cma` контейнер уже створив роль, тож `CREATE ROLE`
впаде нешкідливо — а `ALTER ROLE` **пройде** і перезапише пароль суперюзера тим, що з M1.
Постав одразу ті самі значення, інакше compose стверджуватиме одне, а база матиме інше.

`WEB_SECRET_KEY` навпаки — тільки новий: ним підписані всі сесії на ноутбуку, і спільний
ключ на двох машинах перетворює один витік на два проникнення.

*(Ротацію обох паролів на свіжі роби **після** cutover'а, окремим заходом: воркери на M1
логіняться як `cma_app` саме в цю базу, тож міняти треба з двох боків синхронно.)*

⚠️ **Жодних коментарів після значення.** systemd (`EnvironmentFile=` у юнітах) вважає
`#` коментарем **лише** на початку рядка. `DB_PORT=5432  # примітка` виставить порт у
весь цей рядок разом із приміткою, і web упаде з
`missing "=" after "#" in connection info string`. Капкан у тому, що на M1 такий самий
рядок працює: `python-dotenv` інлайнові коментарі вирізає, але `load_dotenv()` **не
перезаписує** оточення, яке systemd уже зіпсував. Значення з `#` усередині (наприклад
пароль) брати в лапки: `DB_PASSWORD='pa#ssword'`.

Перевірити після заповнення:
```bash
grep -nE '^[A-Z_]+=.*#' /srv/cma/.env    # має бути порожньо
```

```bash
cp deploy/env/vps.env.example /srv/cma/.env    # заповнити три плейсхолдери
chmod 600 /srv/cma/.env && chown cma:cma /srv/cma/.env

mkdir -p /srv/cma-data
cd /srv/cma
set -a && . ./.env && set +a
docker compose -f deploy/docker-compose.vps.yml up -d
```

**Дані контейнерів лежать у `/srv/cma-data`, поза репозиторієм** — і це не
косметика. Раніше вони були під `/srv/cma/deploy/data`, і `chown -R cma:cma /srv/cma`
(яким лагодили права після root'ового `git pull`) забрав із собою базу: Postgres
перестав читати власні файли, продакшн ліг. Дані застосунку не мають бути
досяжні операцією над кодом.

**Якщо переносиш уже наявну інсталяцію** — порядок критичний:
```bash
cd /srv/cma
docker compose -f deploy/docker-compose.vps.yml down     # СПЕРШУ зупинити
mkdir -p /srv/cma-data
mv deploy/data/postgres deploy/data/qdrant /srv/cma-data/
mv deploy/backup deploy/snapshots /srv/cma-data/ 2>/dev/null || true
docker compose -f deploy/docker-compose.vps.yml up -d
```
⚠️ `up -d` на нових шляхах **до** переміщення створить порожні томи й
проініціалізує **чисту** базу. Старі дані нікуди не подінуться, але система
дивитиметься не на них — а виглядатиме це як «усе зникло».

**Права на томи належать контейнерам, не `cma`:**
```bash
docker exec cma-postgres id postgres        # postgres:16-alpine → 70:70
chown -R 70:70 /srv/cma-data/postgres && chmod 700 /srv/cma-data/postgres
chown -R root:root /srv/cma-data/qdrant /srv/cma-data/snapshots
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

## 7. Артефакти: воркер копіює теку, а не монтує її

Веб на VPS читає теки зустрічей із локального диска — це працює одразу.
**Воркер на M1 має дістати ті самі теки**, і робить це копіюванням на межах фаз
(`ingestion/artifact_sync.py`), бо стан-машина вже серіалізує, хто володіє текою:

| Перехід | Хто пише далі | Що робить синхронізація |
|---|---|---|
| `pending` → `transcribing` | M1 | **pull** теки + голосових профілів (тут приїжджає аудіо) |
| → `awaiting_review` | VPS (веб) | **push** транскрипту, щоб ревʼюер побачив `speakers.json` |
| `queued_analysis` → `analyzing` | M1 | **pull** (правки спікерів зроблено на вебі) |
| → `completed` | VPS (веб) | **push** протоколу |

Трафік несиметричний, і в цьому суть: pull тягне аудіо (68 МБ, читається раз на
початку транскрипції й далі декодується в память), push віддає ~660 КБ.

Обидва виклики **всередині `try`**, тож обрив тунелю валить джоб у той самий
ретрай, що й невдала стадія, а не лишає напівзаписану теку.

### Налаштування

**На VPS** — пустити ключ M1 до користувача `cma` (не до root):
```bash
mkdir -p /srv/cma/.ssh && chmod 700 /srv/cma/.ssh
cat >> /srv/cma/.ssh/authorized_keys        # вставити публічний ключ M1
chown -R cma:cma /srv/cma/.ssh && chmod 600 /srv/cma/.ssh/authorized_keys
```

**На M1** — у `.env`:
```
ARTIFACT_SYNC_REMOTE=cma@10.10.0.1:/srv/cma/data
```

Перевірити, що ключ ходить без пароля:
```bash
ssh -o BatchMode=yes cma@10.10.0.1 'ls /srv/cma/data/tenants'
```
`BatchMode=yes` стоїть навмисно: воркер, який отримає запит пароля, завис би на
невидимому питанні, а зависла транскрипція виглядає точно як повільна. Хай краще
джоб одразу впаде в ретрай.

Порожній `ARTIFACT_SYNC_REMOTE` = все на одній машині, кожен виклик — no-op.

⚠️ **macOS постачає `openrsync` (протокол 29), не GNU rsync.** Через це недоступний
`--mkpath`, а rsync сам батьківських тек не створює й рапортує це як голе
`error in file IO` (код 11). Push обходить це через `--rsync-path="mkdir -p … && rsync"` —
працює з обома реалізаціями й без зайвого заходу по SSH. Ставити GNU rsync через
brew не потрібно.

Перевірено проти живого VPS 24.08.2026: pull забрав 14 голосових профілів, push
створив віддалену теку з власником `cma:cma` (тобто веб її читає), а pull
неіснуючої теки дав `SyncError`, як і має.

### Чому не мережевий маунт

Спробували першим: NFS-експорт `/srv/cma/data` і `DATA_ROOT` на точку монтування.
Коду не треба взагалі — тим і спокусливо. Що вийшло 24.08.2026: маунт став
(`nfsstat -m` показав рівно замовлене — v4.0, tcp, 2049, `sec=sys`, `resvport`),
експорт коректний (`fsid=0`, `anonuid`/`anongid` зі справжніх `id cma`), порт
2049 відкритий — а `ls` віддавав `Permission denied` на самому корені. Чотири
кола діагностики, кожне з правдоподібною і хибною гіпотезою.

І це було лише читання. Попереду лишалися NFSv4 idmapping (мапить користувачів
**рядками**, і при розбіжності доменів усе стає `nobody` попри `all_squash`),
права на файли, які воркер **пише**, і вибір між `soft` (обрив = напівзаписаний
артефакт) та `hard` (обрив = процес, який не вбʼється). Три години транскрипції —
довгий час тримати таку конструкцію.

Копіювання робить відмову нудною: rsync повертає код, `SyncError` летить у
наявний ретрай, жодних uid-мапінгів.

---

## 8. systemd + TLS

```bash
cp deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cma-web cma-telegram-bot
systemctl status cma-web --no-pager
```

Веб слухає `127.0.0.1:8000`. Назовні — термінатор TLS:

Спершу переконатись, що 80/443 вільні — Caddy, піднятий колись руками повз
systemd, тримає порти й не дає юніту стартувати, а `systemctl reload` при цьому
каже «not active» і збиває з пантелику:

```bash
ss -tlnp | grep -E ':80|:443'
caddy stop 2>/dev/null || pkill -x caddy
```

```bash
apt install -y caddy
cat > /etc/caddy/Caddyfile <<'EOF'
cma.rechurch.org.ua {
    reverse_proxy 127.0.0.1:8000
    request_body {
        max_size 500MB
    }
}
EOF
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable --now caddy
```

**Домен — справжній, не плейсхолдер.** Caddy бере сертифікат через ACME, тож
`A`-запис має вказувати на VPS, а 80 і 443 — бути відкритими ззовні. Інакше на
443 отримаєш `tlsv1 alert internal error`, а на 80 — редірект на голу IP.

`enable --now`, а не `reload`: юніт ще жодного разу не стартував.

`max_size` не забути: дефолт відріже завантаження запису, і форма впаде на
файлі, який церква щойно 20 хвилин вантажила.

**Перевірка:** відкрити `https://cma.example.org/login`, зайти як `pavlo`,
побачити 20 зустрічей і **програти аудіо** на сторінці зустрічі.

---

## 9. Cutover

Аж тепер M1 перестає бути сервером.

⚠️ **Дописуй у `.env`, а не правь наявні рядки — і памʼятай, що виходять дублікати.**
`python-dotenv` бере **останнє** визначення ключа, тож дописані внизу `DB_HOST`/
`QDRANT_URL` переважать початкові. `restart_dev.sh` читає так само (`tail -1`).
Якщо дублікати заважають — звести вручну, але тоді за один раз, а не наполовину.

```bash
# на M1: дописати в .env рядки з deploy/env/m1.env.example
./restart_dev.sh --stop          # гасить УСІ сервіси (в т.ч. web і бота від
                                 # попереднього запуску) і локальні контейнери
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

⚠️ **Артефакти зустрічей поки лежать у `/srv/cma/data`** — тобто всередині
репозиторію, з тією самою вадою, через яку переїхали томи контейнерів. Це 1.9 ГБ
і найцінніше, що є: записи й протоколи. Переносити треба узгоджено з M1, бо шлях
зашитий у двох `.env`:

```bash
# на VPS
systemctl stop cma-web cma-telegram-bot
mv /srv/cma/data /srv/cma-data/artifacts
sed -i 's|^DATA_ROOT=.*|DATA_ROOT=/srv/cma-data/artifacts|' /srv/cma/.env
systemctl start cma-web cma-telegram-bot

# на M1, у .env
ARTIFACT_SYNC_REMOTE=cma@10.10.0.1:/srv/cma-data/artifacts
```

⚠️ **Юніт оголошує цей шлях у `ReadWritePaths`** — після переносу його треба
оновити й зробити `daemon-reload`, інакше `cma-web` упаде з `226/NAMESPACE` ще
до запуску Python, і повідомлення вказуватиме на namespace, а не на теку, що
переїхала:
```bash
sed -i 's|^ReadWritePaths=.*|ReadWritePaths=-/srv/cma-data/artifacts -/srv/cma/logs|' \
    /etc/systemd/system/cma-web.service
systemctl daemon-reload
```
Дефіс перед шляхом означає «дати права, якщо тека є; не падати, якщо немає» —
саме це перетворює перенесення каталогу з аварії на дрібницю.

⚠️ Переноситься **вся тека `data`**, а не `data/tenants`. `paths_for()` шукає
`<DATA_ROOT>/tenants/<slug>`, тож рівень `tenants` має лишитися всередині —
перенесеш лише його, і застосунок не знайде жодної зустрічі, хоча файли на
місці.

Поки не перенесено — `chown -R` по `/srv/cma` дістане й записи церкви.

**Тунель.** `sudo wg show` на M1. Немає handshake — воркери не бачать черги, і
завантажене церквою аудіо просто чекає.

**Що дашборд поки не показує:** чи M1 узагалі на звʼязку. `health_checks`
пише воркер, тож коли M1 спить, панель показує **останній** знімок, а не
«обробник відсутній». Кандидат на наступну правку.

## 11. Викочування зміни, що торкається схеми

Порядок нижче — не церемонія: кожен крок тут існує тому, що якийсь із них колись
пропустили. Робиться з M1, через тунель; на VPS від root — лише те, що інакше не
працює.

```bash
# 1. Бекап ДО, не після. Міграції, що переписують resolve_web_session,
#    вимикають вхід усім одразу, якщо в них помилка.
bash deploy/backup_from_vps.sh

# 2. Код
git push origin main
ssh cma@10.10.0.1 'cd /srv/cma && git pull --ff-only && git log --oneline -1'

# 3. Схема — ТІЛЬКИ від root, у власній сесії. Див. попередження нижче.
#    Суперюзер бази — cma, НЕ postgres: контейнер створено з POSTGRES_USER=cma.
PW=$(grep "^POSTGRES_SUPERUSER_PASSWORD=" /srv/cma/.env | cut -d= -f2-)
docker exec -i -e PGPASSWORD="$PW" cma-postgres \
    psql -U cma -d cma -q -v ON_ERROR_STOP=1 \
    < /srv/cma/src/church_assistant/db/migrations/013_tenant_archive.sql
docker exec -e PGPASSWORD="$PW" cma-postgres \
    psql -U cma -d cma -tAc "SELECT max(version) FROM schema_version;"

# 4. Перезапуск — лише після того, як схема стала новою. Навпаки не можна:
#    код, що чекає deleted_at, на старій схемі падає на кожному запиті.
#    Це `cma` вміє сам: два юніти прописані в /etc/sudoers.d/cma-deploy.
ssh cma@10.10.0.1 'sudo systemctl restart cma-web cma-telegram-bot'

# 5. Звірка
curl -s -o /dev/null -w '%{http_code}\n' https://cma.rechurch.org.ua/login
```

⚠️ **Міграції `cma` застосувати не може, і не має вміти.** Docker-сокет
належить root, а членство в групі `docker` рівносильне root — з неї монтується
`/` у контейнер. Обміняти цю межу на зручність одного `docker exec` — значить
віддати повний root назавжди заради команди, яку виконують раз на місяць. Права
`cma` навмисно закінчуються на `systemctl restart` двох юнітів
(`sudo -n -l` покаже точний список).

⚠️ **`git pull` на VPS робиться від `cma`, ніколи від root.** Один pull від root
переписав `auth.py` як root-owned, і `cma-web` після цього падав із
`PermissionError` — 502 на живому сайті, а причина видно лише в journalctl.

⚠️ **Ніколи `chown -R cma:cma /srv/cma`.** Поки томи Postgres лежали під
`deploy/data`, ця команда підмела й їх, і база не піднялась. Томи відтоді в
`/srv/cma-data`, але звичка небезпечна саме тим, що спрацьовує 99 разів.

⚠️ **Ніколи `git clean` у `/srv/cma` — жодною формою.** Домашня тека `cma` **і є**
каталог репозиторію (`getent passwd cma` → `/srv/cma`), тож усе, чим користувач
живе, лежить для git'а як сміття в робочій копії. Перевірено `git clean -nd`
25.08 на живій машині:

| що зникає | форма команди | наслідок |
|---|---|---|
| `.ssh/` (`authorized_keys`, deploy-ключ `github`) | **уже `-fd`** | ні SSH-входу, ні `git pull` |
| `.env` | `-fdx` | web не стартує — немає `WEB_SECRET_KEY` |
| `.venv/`, `.local/` | `-fdx` | нічим запускати |
| `logs/` | `-fdx` | історія, якої більше ніде немає |

Найгостріше тут не `-x`, а те, що **`.ssh/` виносить звичайний `git clean -fd`** —
форма, яку набирають, щоб «прибрати сміття перед pull'ом», не думаючи, що робиться
щось небезпечне. Відновлювати доступ доведеться з консолі провайдера.

Прибирати робочу копію на VPS не треба взагалі: `git pull --ff-only` не потребує
чистого дерева, а єдина «брудна» річ там — видалений `data/tenants/.../README.md`
з часів переносу даних. Якщо колись справді знадобиться — `git clean` з `-n`,
прочитати список очима, і тільки потім з явними шляхами.

⚠️ **Не перезапускати `cma-web` «щоб перевірити»** — кожен рестарт рве всі живі
сесії. Права перевіряються `sudo -n -u cma …`, а не рестартом.

## 12. Диск: що росте саме собою

24 серпня диск був на **99%**. Жодна зустріч уже не завантажилась би. Розбір
показав, що майже нічого з цього не було виною CMA — але наслідки були б наші.

**Скільки насправді займає CMA:** два образи (`postgres:16-alpine` 420 МБ,
`qdrant/qdrant:v1.18.1` 274 МБ) і 1.9 ГБ артефактів. Решта 30 ГБ — усе інше на
машині.

### Три джерела, що ростуть без стелі

**1. Docker build cache.** 74 записи, 0 активних, 2.4 ГБ. Найімовірніший винуватець
першого заповнення. Чистити безпечно, найгірше — довша наступна збірка:
```bash
docker builder prune -f
```

**2. journald без обмеження.** `SystemMaxUse` не було задано взагалі, журнал доріс
до 2.2 ГБ і йшов до стелі в 4 ГБ (10% диска). Одноразове прибирання проблему не
вирішує — потрібен саме ліміт:
```bash
journalctl --vacuum-size=200M
sed -i 's/^#\?SystemMaxUse=.*/SystemMaxUse=200M/' /etc/systemd/journald.conf
systemctl restart systemd-journald
```

**3. Зупинені контейнери утримують свої образи.** Це найменш очевидне. `docker stop`
місця не звільняє: образ лишається прив'язаним, поки існує контейнер. `open-webui`
стояв зупиненим і тримав **6.7 ГБ**. Спершу `rm`, і лише потім `rmi`.

### ⚠️ Чого не запускати

`docker system prune -a --volumes` і `docker volume prune` — на цій машині живуть
чужі проєкти. CMA їх переживе (Postgres і Qdrant монтуються **bind-маунтами** на
`/srv/cma-data`, а не іменованими томами), але сусіди — ні.

`docker image prune -a` теж не сліпма: воно прибирає образи, не задіяні **жодним**
контейнером, тож зупинений, але потрібний проєкт втратить свій образ.

### 4.5 ГБ CUDA у venv — вирішено групою `worker`

`/srv/cma/.venv` містив `nvidia` (2.7 ГБ), `torch` (1.1 ГБ) і `triton` (698 МБ) на
машині **без відеокарти**. Ні веб, ні бот їх не імпортують: у `local_rerank.py`
`torch` вантажиться ліниво, всередині функції, а `rag.answer()` контрольна площина
не викликає взагалі — бере звідти лише форматування (`format_hit_short`,
`score_color_hint`, `Hit.from_dict`).

Важке винесено в групу `worker` (`faster-whisper`, `pyannote-audio`,
`sentence-transformers`, `omegaconf`), а обидва юніти несуть
`Environment=UV_NO_GROUP=worker`.

⚠️ **Прапорець саме в юнітах, а не в `pyproject`.** `uv run` пересинхронізовує
оточення при **кожному** старті, тож без нього наступний рестарт повернув би всі
4.5 ГБ. Замовчування в `pyproject` навмисно нахилене в інший бік: M1 запускає
десятки `uv run`-команд зі скриптів і тестів, і один забутий прапорець там тихо
перезібрав би venv без torch і зламав обробку. VPS запускає рівно два юніти.

⚠️ **Ручні `uv run` на VPS теж мають нести прапорець** — інакше одна разова команда
поверне 4.5 ГБ. Тому в `~cma/.bashrc` стоїть `export UV_NO_GROUP=worker`.

⚠️ **Після синхронізації кеш треба чистити окремо.** Він жорстко злінкований у
venv (див. нижче): `uv sync` прибирає файли з venv, але в кеші вони лишаються, і
диск не зрушить. Причому `uv cache prune` тут майже не допомагає — він знімає лише
«нікому не потрібне», а колеса torch/nvidia лишає, бо на них посилається `uv.lock`,
хоча на цій машині вони не встановляться вже ніколи. Потрібен `uv cache clean`.

⚠️ **`uv run` тримає лок на кеші весь час життя сервісу**, тож ані `prune`, ані
`clean` не спрацюють, доки юніти запущені — команда мовчки чекає 300 с і падає по
таймауту. Чистити разом із зупинкою, одним заходом, а не після старту.

**Чистити кеш безпечно й майже без простою.** venv уже відповідає `uv.lock`, тож
синхронізація на старті проходить вхолосту й у мережу не йде: заміряний простій —
**5 секунд**. Кеш наповниться знову лише коли зміняться залежності.

На macOS цієї жирноти не було видно взагалі — `nvidia`-пакети існують лише під
Linux x86, тому `.venv` на M1 займає 1.2 ГБ проти 5.4 ГБ на VPS.

### Цифри du брешуть на цій машині

`du` показує кеш uv як 7.2 ГБ, а `.venv` як 5.4 ГБ — але разом це **7.3 ГБ**, не
12.6. Кеш **жорстко злінкований** у venv (`find -printf %n` показує по 2 посилання
на файл), і `du` за один прохід рахує такий файл один раз, а за два — двічі. Тому
`uv cache prune` звільнить лише те, чого немає у venv, а видалення venv — майже
нічого. Перевіряти двома проходами:
```bash
du -shcx /srv/cma/.venv /srv/cma/.cache    # разом — правда
du -shx /srv/cma/.venv; du -shx /srv/cma/.cache   # окремо — сума збігатись не буде
```

### Підсумок 25 серпня

99% → **22%** (14 ГБ зайнято, 51 ГБ вільно). Звільнено ~17 ГБ:

| | |
|---|---|
| CUDA з venv + кеш (група `worker`) | 9.7 ГБ |
| open-webui (контейнер + образ) | 6.7 ГБ |
| docker build cache | 2.4 ГБ |
| journald + ліміт 200 МБ | 2.0 ГБ |
| ollama-gateway, redis | 0.15 ГБ |

`/srv/cma/.venv` — **200 МБ** замість 5.4 ГБ.

### Моніторинг
```bash
df -h / | tail -1
docker system df
journalctl --disk-usage
```
