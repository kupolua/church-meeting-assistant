"""
Minutes stored as ciphertext: what `protocols_repo` writes for an encrypted
church, what it gives back, and what it refuses to do without a key.

    uv run python tests/protocol_crypto_test.py

Builds its own `cma_protocol_test` database from schema.sql plus every
migration and drives the real repo, the real keyring and the real Postgres —
the point of this file is the round trip through a column, and a stub anywhere
in it would be checking a mock instead of the thing that ships.

EVERY ASSERTION ABOUT CIPHERTEXT READS THE DATABASE DIRECTLY, with psql and not
through the repo. Asking the repo whether it encrypted is asking it to grade
its own homework: it would decrypt on the way out and report success either
way. What matters is what a `pg_dump` of the VPS would contain.

⚠️ Drops the database it creates. The name is hard-coded for that reason.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, "src")

from dotenv import load_dotenv  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402

from church_assistant.db import protocols_repo as repo  # noqa: E402
from church_assistant.shared import crypto, field_crypto, keyring  # noqa: E402


DB = "cma_protocol_test"
CONTAINER = os.getenv("CMA_PG_CONTAINER", "cma-postgres")
DOCKER = next(
    (p for p in ("/opt/homebrew/bin/docker",
                 "/usr/local/bin/docker",
                 "/Applications/Docker.app/Contents/Resources/bin/docker")
     if Path(p).exists()),
    "docker",
)
ROOT = Path(__file__).resolve().parents[1]
DATE_ENC = date(2026, 9, 7)
DATE_PLAIN = date(2026, 9, 7)
PASSED = 0


def ok(label: str) -> None:
    global PASSED
    PASSED += 1
    print(f"  ✓ {label}")


def sup(sql: str, read: bool = False) -> str:
    flags = ["-tAc"] if read else ["-q", "-c"]
    r = subprocess.run(
        [DOCKER, "exec", CONTAINER, "psql", "-U", "cma", "-d", DB, *flags, sql],
        capture_output=True, text=True,
    )
    return r.stdout.strip()


def build() -> None:
    subprocess.run(
        [DOCKER, "exec", CONTAINER, "psql", "-U", "cma", "-d", "postgres", "-q",
         "-c", f"DROP DATABASE IF EXISTS {DB}", "-c", f"CREATE DATABASE {DB}"],
        capture_output=True, text=True, check=True)
    files = [ROOT / "src/church_assistant/db/schema.sql"]
    files += sorted((ROOT / "src/church_assistant/db/migrations").glob("*.sql"))
    for f in files:
        r = subprocess.run(
            [DOCKER, "exec", "-i", CONTAINER, "psql", "-U", "cma", "-d", DB,
             "-q", "-v", "ON_ERROR_STOP=1"],
            stdin=f.open("rb"), capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"✗ {f.name}\n{r.stderr[:400]}")


async def main() -> None:
    load_dotenv(ROOT / ".env")
    pw = os.getenv("DB_PASSWORD")
    if not pw:
        raise SystemExit("немає DB_PASSWORD у .env")

    print("Протоколи як шифротекст")
    build()
    sup("INSERT INTO tenants (slug, name) VALUES ('enc','Encrypted'),('plain','Plain')")
    ids = dict(l.split("|") for l in
               sup("SELECT slug, id FROM tenants WHERE slug IN ('enc','plain')", True).splitlines())
    t_enc, t_plain = int(ids["enc"]), int(ids["plain"])

    node = crypto.new_device_keys()
    os.environ[keyring.ENV_PROCESSOR_KEY] = crypto.recovery_to_text(node.private)
    kek = crypto.new_key()

    pool = AsyncConnectionPool(
        f"host=127.0.0.1 port=5433 dbname={DB} user=cma_app password={pw}",
        min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT set_config('app.current_tenant', %s, false)", (str(t_enc),))
            await cur.execute(
                "INSERT INTO church_devices (tenant_id, public_key, kind, label) "
                "VALUES (%s,%s,'processor','M1') RETURNING id", (t_enc, node.public))
            dev = (await cur.fetchone())[0]
            await cur.execute(
                "INSERT INTO key_envelopes (tenant_id, device_id, kek_version, wrapped_kek) "
                "VALUES (%s,%s,1,%s)", (t_enc, dev, crypto.wrap_for(node.public, kek)))
            await cur.execute("UPDATE tenants SET kek_version = 1 WHERE id = %s", (t_enc,))
        keyring.forget()
        ok("церква 'enc' має ключ; 'plain' лишається на kek_version = 0")

        # ── Церква без ключа: жодної зміни поведінки ────────────────────
        pid_p = await repo.create(pool, t_plain, meeting_date=DATE_PLAIN,
                                  created_by="web:pavlo", secretary="Ірина Гнатюк")
        item_p = await repo.add_item(pool, t_plain, pid_p, "Ремонт даху")
        await repo.add_ruling(pool, t_plain, item_p, text="Зібрати кошторис",
                              responsible="Роман", due="до 01.10")
        raw = sup(f"SELECT secretary FROM meeting_protocols WHERE id = {pid_p}", True)
        assert raw == "Ірина Гнатюк", raw
        raw = sup(f"SELECT question FROM protocol_items WHERE id = {item_p}", True)
        assert raw == "Ремонт даху", raw
        ok("церква без ключа пише плейнтекст — код шифрування її не чіпає")

        got = await repo.get_by_date(pool, t_plain, DATE_PLAIN)
        assert got["secretary"] == "Ірина Гнатюк"
        ok("і читає його назад незміненим")

        # ── Церква з ключем: те саме API, шифротекст у базі ─────────────
        pid = await repo.create(pool, t_enc, meeting_date=DATE_ENC,
                                created_by="web:pavlo", secretary="Ірина Гнатюк")
        row = sup("SELECT secretary || '|' || created_by || '|' || seq || '|' || status "
                  f"  FROM meeting_protocols WHERE id = {pid}", True)
        secretary, created_by, seq, status = row.split("|")
        assert secretary.startswith("enc.v1."), secretary
        assert created_by.startswith("enc.v1."), created_by
        ok("create() запечатує secretary і created_by — двофазний запис спрацював")

        assert seq == "1" and status == "draft"
        raw_date = sup(f"SELECT meeting_date FROM meeting_protocols WHERE id = {pid}", True)
        assert raw_date == "2026-09-07", raw_date
        ok("seq, status і дата лишились відкритими — §3.1, на них тримаються індекси")

        got = await repo.get_by_date(pool, t_enc, DATE_ENC)
        assert got["secretary"] == "Ірина Гнатюк", got["secretary"]
        assert got["created_by"] == "web:pavlo", got["created_by"]
        ok("get_by_date() повертає плейнтекст — маршрут ключа не бачить")

        # ── Шапка: attendees як JSONB ──────────────────────────────────
        people = ["Роман Вечерківський", "Ірина Гнатюк", "Павло Кулаковський"]
        await repo.update_header(pool, t_enc, pid, attendees=people,
                                 quorum="11 з 14 — кворум є", secretary="Ірина Г.")
        raw = sup(f"SELECT attendees::text FROM meeting_protocols WHERE id = {pid}", True)
        assert raw.startswith('"enc.v1.'), raw[:40]
        assert "Роман" not in raw and "Гнатюк" not in raw
        ok("attendees запечатано одним блобом — кількість присутніх теж не видно")

        raw = sup(f"SELECT quorum FROM meeting_protocols WHERE id = {pid}", True)
        assert raw.startswith("enc.v1.")
        got = await repo.get_by_date(pool, t_enc, DATE_ENC)
        assert got["attendees"] == people, got["attendees"]
        assert got["quorum"] == "11 з 14 — кворум є"
        assert got["secretary"] == "Ірина Г."
        ok("шапка повертається списком і рядками, а не JSON-рядком")

        # ── Порядок денний ─────────────────────────────────────────────
        item = await repo.add_item(pool, t_enc, pid, "Ремонт даху молитовного дому")
        await repo.update_item(pool, t_enc, item,
                               heard="Роман доповів про стан покрівлі",
                               resolved="Замовити кошторис до кінця вересня")
        await repo.set_votes(pool, t_enc, item, votes_for=9, votes_against=1,
                             votes_abstain=1)
        cols = sup("SELECT question || '~' || heard || '~' || resolved || '~' || "
                   "       position || '~' || COALESCE(votes_for::text,'-') || '~' || status "
                   f"  FROM protocol_items WHERE id = {item}", True)
        q, heard, resolved, pos, votes, st = cols.split("~")
        assert all(v.startswith("enc.v1.") for v in (q, heard, resolved))
        ok("question, heard і resolved — шифротекст у базі")
        assert (pos, votes, st) == ("1", "9", "considered")
        ok("position, голоси і status лишились числами — §3.1 свідомо")

        ruling = await repo.add_ruling(pool, t_enc, item, text="Замовити кошторис",
                                       responsible="Роман В.", due="01.10.2026")
        cols = sup("SELECT text || '~' || responsible || '~' || due || '~' || position "
                   f"  FROM protocol_rulings WHERE id = {ruling}", True)
        text, resp, due, rpos = cols.split("~")
        assert all(v.startswith("enc.v1.") for v in (text, resp, due)), cols
        assert rpos == "1"
        ok("постанова, виконавець і термін — шифротекст; порядок відкритий")

        items = await repo.list_items(pool, t_enc, pid)
        assert items[0]["question"] == "Ремонт даху молитовного дому"
        assert items[0]["resolved"] == "Замовити кошторис до кінця вересня"
        assert items[0]["rulings"][0]["responsible"] == "Роман В."
        ok("list_items() розшифровує і питання, і постанови — один DEK на зустріч")

        one = await repo.get_item(pool, t_enc, item)
        assert one["heard"] == "Роман доповів про стан покрівлі"
        assert (await repo.get_ruling(pool, t_enc, ruling))["due"] == "01.10.2026"
        ok("get_item() і get_ruling() відкривають свої рядки")

        # Порожній рядок лишається порожнім, а не стає блобом: інакше кожен
        # незаповнений «виконавець» був би шифротекстом, і сторінка показувала б
        # «зашифровано» там, де людина просто нічого не вписала.
        empty = await repo.add_ruling(pool, t_enc, item, text="Без виконавця")
        raw = sup(f"SELECT COALESCE(responsible,'') FROM protocol_rulings WHERE id = {empty}", True)
        assert raw == "", repr(raw)
        ok("порожнє поле лишається порожнім — не запечатується")

        # ── DEK один на зустріч, спільний з артефактами ─────────────────
        scope = sup("SELECT scope FROM dek_envelopes WHERE tenant_id = "
                    f"{t_enc} ORDER BY id", True).splitlines()
        assert scope == ["2026-09-07"], scope
        ok("на всю зустріч створився рівно один DEK, scope = тека зустрічі")

        # ── AAD: блоб не переносний ────────────────────────────────────
        other = await repo.add_item(pool, t_enc, pid, "Інше питання")
        sup(f"UPDATE protocol_items SET question = (SELECT question FROM protocol_items "
            f"WHERE id = {item}) WHERE id = {other}")
        try:
            await repo.get_item(pool, t_enc, other)
            raise AssertionError("перенесений шифротекст відкрився — AAD не тримає")
        except field_crypto.NotDecryptable:
            pass
        ok("шифротекст, перенесений в інший рядок, не відкривається (AAD прибитий до id)")

        sup(f"UPDATE protocol_items SET question = 'Інше питання' WHERE id = {other}")
        # Плейнтекст, дописаний в зашифровану церкву повз репозиторій, читається
        # як є: маркер відрізняє форми, тож змішана колонка — робочий стан, а не
        # пошкодження. Саме це робить вмикання шифрування рішенням, а не міграцією.
        assert (await repo.get_item(pool, t_enc, other))["question"] == "Інше питання"
        ok("плейнтекст у зашифрованій церкві проходить наскрізь — колонка тримає обидві форми")

        # Друга зустріч — на ній перевіряємо запис без ключа нижче. Створити
        # її треба ЗАРАЗ, бо перший протокол за мить буде затверджено, а
        # затвердження одностороннє (018): вернути його в draft нема як.
        pid2 = await repo.create(pool, t_enc, meeting_date=date(2026, 9, 14),
                                 created_by="web:pavlo", secretary="Ірина Г.")

        # ── Заморозка все ще заморожує ─────────────────────────────────
        await repo.set_status(pool, t_enc, pid, "approved", approved_by=None)
        try:
            await repo.add_item(pool, t_enc, pid, "Пізно")
            raise AssertionError("затверджений протокол прийняв нове питання")
        except repo.ProtocolFrozen:
            pass
        ok("тригер заморозки 018 діє й на двофазному записі — INSERT не пройшов")

        assert sup("SELECT COUNT(*) FROM protocol_items WHERE protocol_id = "
                   f"{pid}", True) == "2"
        ok("і нічого не лишилось після відкоченої транзакції")

        # ── Вузол без ключа: читання деградує, запис відмовляє ─────────
        keyring.forget()
        del os.environ[keyring.ENV_PROCESSOR_KEY]

        try:
            await repo.get_by_date(pool, t_enc, DATE_ENC)
            raise AssertionError("прочиталось без ключа")
        except field_crypto.NotDecryptable:
            pass
        ok("вузол без ключа: читання дає NotDecryptable, а не байти base64 у шаблоні")

        try:
            await repo.list_items(pool, t_enc, pid)
            raise AssertionError("порядок денний прочитався без ключа")
        except field_crypto.NotDecryptable:
            pass
        ok("те саме для порядку денного")

        # НАЙВАЖЛИВІШЕ ТУТ. Якби запис деградував так само, як читання, seal()
        # отримав би dek=None, пропустив текст наскрізь — і церква, яка просила
        # шифрування, отримала б плейнтекст у базі без жодної помилки.
        try:
            await repo.add_item(pool, t_enc, pid2, "Записано без ключа")
            raise AssertionError("запис без ключа пройшов — це був би тихий плейнтекст")
        except keyring.NoProcessorKey:
            pass
        assert sup("SELECT COUNT(*) FROM protocol_items WHERE question = "
                   "'Записано без ключа'", True) == "0"
        ok("вузол без ключа: запис відмовляє, а не пише плейнтекст мовчки")

        try:
            await repo.update_item(pool, t_enc, item, resolved="теж ні")
            raise AssertionError("update_item без ключа пройшов")
        except keyring.NoProcessorKey:
            pass
        ok("update_item без ключа так само відмовляє")

        # Список зустрічей мусить жити без ключа: у ньому лише метадані.
        listed = await repo.list_all(pool, t_enc)
        assert len(listed) == 2 and {r["seq"] for r in listed} == {1, 2}
        assert "secretary" not in listed[0] and "attendees" not in listed[0]
        ok("list_all() працює без ключа — і чутливих колонок у ньому просто немає")

        # Церква без шифрування нічого з цього не помітила.
        assert (await repo.get_by_date(pool, t_plain, DATE_PLAIN))["secretary"] == "Ірина Гнатюк"
        assert (await repo.list_items(pool, t_plain, pid_p))[0]["question"] == "Ремонт даху"
        ok("церква без ключа читається й тоді, коли ключа немає ні в кого")

        # ── Деградація вебу: те, що показують замість вмісту ───────────
        #
        # Живу сторінку перевірено прогоном проти sandbox-бази (GET 200 з
        # блоком «Зашифровано», POST 409, список зустрічей 200, жодного 500).
        # Тут лишається те, що ламається тихо: ознака «тека — шифротекст» і
        # самі обробники, зняття яких не впало б у жодному іншому тесті.
        from church_assistant.web import encrypted

        sealed_dir = Path("/tmp/cma_sealed_probe")
        shutil.rmtree(sealed_dir, ignore_errors=True)
        (sealed_dir / "enc").mkdir(parents=True)
        (sealed_dir / "plain").mkdir(parents=True)
        (sealed_dir / "empty").mkdir(parents=True)
        (sealed_dir / "enc" / "polished.md.enc").write_bytes(b"x")
        (sealed_dir / "plain" / "polished.md").write_text("# є", encoding="utf-8")
        assert encrypted.folder_is_sealed(sealed_dir / "enc")
        assert not encrypted.folder_is_sealed(sealed_dir / "plain")
        ok("тека з .enc розпізнається як зашифрована, звичайна — ні")

        # ПОРОЖНЯ ТЕКА — НЕ ЗАШИФРОВАНА. Аудіо завантажили, конвеєр ще не
        # відпрацював: якби ознакою була відсутність polished.md, ця нормальна
        # проміжна мить показувала б «відкрийте в застосунку» назавжди.
        assert not encrypted.folder_is_sealed(sealed_dir / "empty")
        assert not encrypted.folder_is_sealed(sealed_dir / "немає такої")
        ok("порожня тека не видається за зашифровану — це стан конвеєра, не крипти")
        shutil.rmtree(sealed_dir, ignore_errors=True)

        from church_assistant.web.main import app
        handlers = app.exception_handlers
        assert field_crypto.NotDecryptable in handlers, "читання деградує в 500"
        assert keyring.NoProcessorKey in handlers, "запис деградує в 500"
        ok("обробники зареєстровані — жоден маршрут не покаже пастору traceback")

    finally:
        await pool.close()
        subprocess.run(
            [DOCKER, "exec", CONTAINER, "psql", "-U", "cma", "-d", "postgres",
             "-q", "-c", f"DROP DATABASE IF EXISTS {DB}"],
            capture_output=True, text=True)

    print(f"\n✅ {PASSED} перевірок пройдено")


if __name__ == "__main__":
    asyncio.run(main())
