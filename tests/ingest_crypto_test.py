"""
Encryption on the ingest path: what leaves the M1 is unreadable, what comes back
is byte-identical, and a church that never asked for any of this is untouched.

    uv run python tests/ingest_crypto_test.py

Builds its own `cma_ingest_test` database from schema.sql plus every migration,
pairs a processing node, and drives the real keyring — no stubs for anything
that could be wrong in production. The rsync itself is not exercised: it moves
bytes and has no opinion about them, so the two ends are what matter here.

⚠️ Drops the database it creates. The name is hard-coded for that reason.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402

from church_assistant.ingestion import artifact_crypto  # noqa: E402
from church_assistant.shared import crypto, keyring  # noqa: E402


DB = "cma_ingest_test"
CONTAINER = os.getenv("CMA_PG_CONTAINER", "cma-postgres")
DOCKER = next(
    (p for p in ("/opt/homebrew/bin/docker",
                 "/usr/local/bin/docker",
                 "/Applications/Docker.app/Contents/Resources/bin/docker")
     if Path(p).exists()),
    "docker",
)
ROOT = Path(__file__).resolve().parents[1]
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


def make_meeting(root: Path) -> Path:
    """A folder shaped like a real one: text artifacts plus audio."""
    d = root / "2026-07-27"
    (d / "chunks").mkdir(parents=True)
    (d / "transcript.txt").write_text("Роман: Дякую, брате…\n", encoding="utf-8")
    (d / "polished.md").write_text("# Систематичні зустрічі\n", encoding="utf-8")
    (d / "speakers.json").write_text('{"SPEAKER_00": "Роман Вечерківський"}', encoding="utf-8")
    (d / "chunks" / "000.txt").write_text("Потреба в регулярних зборах", encoding="utf-8")
    (d / "audio.m4a").write_bytes(os.urandom(2048))
    return d


async def main() -> None:
    load_dotenv(ROOT / ".env")
    pw = os.getenv("DB_PASSWORD")
    if not pw:
        raise SystemExit("немає DB_PASSWORD у .env")

    print("Шифрування на шляху інжесту")
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
        min_size=1, max_size=2, open=False)
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
        ok("обробний вузол — рядок kind='processor' зі своїм конвертом")

        # Церква без ключа: keyring мовчки каже «плейнтекст».
        assert await keyring.kek_for(pool, t_plain) is None
        assert await keyring.dek_for(pool, t_plain, "2026-07-27") is None
        ok("церква з kek_version = 0 повертає None, а не помилку")

        dek = await keyring.dek_for(pool, t_enc, "2026-07-27")
        assert dek is not None and len(dek) == 32
        ok("DEK зустрічі створився при першому зверненні")

        keyring.forget()
        again = await keyring.dek_for(pool, t_enc, "2026-07-27")
        assert again == dek
        ok("повторний виклик після скидання кешу дає ТОЙ САМИЙ DEK")
        assert sup("SELECT count(*) FROM dek_envelopes", True) == "1"
        ok("другого конверта для тієї самої зустрічі не створено")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = make_meeting(root / "m1")
            remote = root / "vps" / "2026-07-27"
            back = root / "back" / "2026-07-27"

            n = artifact_crypto.encrypt_tree(src, remote, dek, t_enc, "2026-07-27")
            assert n == 4, n
            ok(f"зашифровано {n} артефакти, аудіо лишено як є (§4)")

            names = sorted(p.name for p in remote.rglob("*") if p.is_file())
            assert names == ["000.txt.enc", "audio.m4a", "polished.md.enc",
                             "speakers.json.enc", "transcript.txt.enc"], names
            ok("на сервері лежать .enc, аудіо — під своїм ім'ям")

            blob = (remote / "transcript.txt.enc").read_bytes()
            assert "Роман".encode() not in blob and b"transcript" not in blob
            ok("у шифротексті немає ні тексту, ні імені спікера")

            assert (remote / "audio.m4a").read_bytes() == (src / "audio.m4a").read_bytes()
            ok("аудіо доходить незмінним — свідомий виняток, не помилка")

            m = artifact_crypto.decrypt_tree(remote, back, dek, t_enc, "2026-07-27")
            assert m == 4
            for p in sorted(src.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(src)
                    assert (back / rel).read_bytes() == p.read_bytes(), rel
            ok("зворотний шлях відновлює папку побайтово")

            # Чужий ключ і перенесений файл — обидва мають ламатися.
            for bad_dek, label in ((crypto.new_key(), "чужим DEK"),):
                try:
                    artifact_crypto.decrypt_tree(remote, root / "x", bad_dek, t_enc, "2026-07-27")
                    raise AssertionError(f"розшифрувалося {label}")
                except crypto.DecryptionFailed:
                    ok(f"папку не відкрити {label}")
            try:
                artifact_crypto.decrypt_tree(remote, root / "y", dek, t_enc, "2026-08-03")
                raise AssertionError("розшифрувалося під іншою зустріччю")
            except crypto.DecryptionFailed:
                ok("папку не видати за іншу зустріч (AAD тримає дату)")

            swapped = root / "swap" / "2026-07-27"
            swapped.mkdir(parents=True)
            (swapped / "polished.md.enc").write_bytes(
                (remote / "transcript.txt.enc").read_bytes())
            try:
                artifact_crypto.decrypt_tree(swapped, root / "z", dek, t_enc, "2026-07-27")
                raise AssertionError("підміна файлу пройшла")
            except crypto.DecryptionFailed:
                ok("перейменований блоб не відкривається під чужим ім'ям")
    finally:
        await pool.close()

    subprocess.run([DOCKER, "exec", CONTAINER, "psql", "-U", "cma", "-d", "postgres",
                    "-q", "-c", f"DROP DATABASE {DB}"], capture_output=True)
    print(f"\n✅ {PASSED} перевірок пройдено (базу {DB} прибрано)")


if __name__ == "__main__":
    asyncio.run(main())
