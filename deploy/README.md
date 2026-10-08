# deploy/

Файли для розʼїзду площин: **VPS — control plane, сервер «ai» (DL165 G7 + RX 580) — обробка**.
До 08.10.2026 обробкою був M1; його файли нижче лишаються для відкату.
Покроковий порядок дій — `docs/vps_deploy.md`. Тут лише те, що там згадується.

| Що | Куди |
|---|---|
| `wireguard/wg0.vps.conf.example` | `/etc/wireguard/wg0.conf` на VPS |
| `wireguard/wg0.m1.conf.example` | `/opt/homebrew/etc/wireguard/wg0.conf` на M1 |
| `docker-compose.vps.yml` | Postgres + Qdrant на VPS, привʼязані до `10.10.0.1` |
| `systemd/cma-web.service` | web на VPS |
| `systemd/cma-telegram-bot.service` | бот на VPS (приймає питання цілодобово) |
| `env/vps.env.example` | `/srv/cma/.env` |
| `env/m1.env.example` | **діф** до наявного `.env` на M1, не заміна (відкат) |
| `wireguard/wg0.ai.conf.example` | `/etc/wireguard/wg0.conf` на сервері «ai» (10.10.0.3) |
| `env/ai.env.example` | `/srv/cma/.env` на сервері «ai»: діф до `.env` з M1 |
| `systemd/cma-ingestion-worker.service` | обробка записів на сервері «ai» (обидві фази) |
| `systemd/cma-query-worker.service` | відповіді на запитання (RAG / Gemma) на сервері «ai» |
| `provision-ai-server.sh` | сервер «ai» після чистої Ubuntu: Vulkan, whisper.cpp, модель, pyannote, udev для GPU |
| `migrate/export_from_m1.sh` | запускати на M1 — read-only |
| `migrate/import_to_vps.sh` | запускати на VPS після `compose up` |

**Жодних справжніх ключів у цих файлах.** Усе секретне — плейсхолдери
`<У_КУТОВИХ_ДУЖКАХ>`; заповнюються на місці, у файли з `chmod 600`.

Нічого з цього не запускається на M1 у звичайному режимі: `restart_dev.sh`
лишається як був, поки в `.env` не зʼявиться `CMA_ROLE=worker`. Від 08.10.2026
M1 у конвеєрі не потрібен узагалі — обидва воркери живуть на сервері «ai» як
systemd-юніти і піднімаються самі після відключення світла. Якщо ноутбук
відкритий, на ньому можна додатково запустити query-воркер: обидва беруть з
однієї черги, запитання дістанеться швидшому.
