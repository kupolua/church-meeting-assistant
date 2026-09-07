"""
Turn encryption on for one church — the first act of cloud_plan §5.

    uv run python -m church_assistant.scripts.enable_encryption --tenant <slug>
    uv run python -m church_assistant.scripts.enable_encryption --tenant <slug> --rotate

WHAT IT DOES, and why in this order:

    1. generate the church KEK                     (never written anywhere raw)
    2. seal it to the processing node              (so the pipeline can work)
    3. seal it to a fresh recovery key             (so the church can survive)
    4. PRINT the recovery key                      (the only time it is readable)
    5. set tenants.kek_version                     (the switch itself, last)

The switch is last on purpose. Everything before it is additive and harmless —
rows nobody reads. The moment kek_version stops being 0, the next artifact this
church produces is ciphertext, and a half-finished setup would mean a church
whose data is being encrypted with a key nobody has printed.

⚠️ THE RECOVERY KEY IS SHOWN ONCE AND NOWHERE ELSE. It is not stored, not
logged, not recoverable. Printing it into a terminal means it is in scrollback,
so print it on paper, put it in the safe, and clear the screen. A church that
loses every device and has no paper has lost its archive permanently — that
property is what is being bought here, not a flaw to work around.

⚠️ AND IT IS ALSO THE COMPROMISE POINT. Whoever holds that paper can read
everything, forever, including after every device is revoked. cloud_plan §5.3.

THE PROCESSING NODE MUST ALREADY HAVE A KEY. CMA_PROCESSOR_KEY holds the M1's
private half; this script needs the matching public half to address an envelope
to it. If it is unset the script generates a pair, prints it, and stops without
touching anything — because a KEK sealed to a key nobody saved is a church that
cannot be processed and cannot be read.

ROTATION (--rotate) writes a new KEK generation: new envelopes for the devices
that remain, new wrapped DEKs, and nothing at all for anyone revoked. Content is
never re-encrypted (§5.2), so it costs the same whether the church has twenty
meetings or two thousand.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from psycopg.rows import dict_row

from church_assistant.db.connection import close_pool, get_pool
from church_assistant.shared import crypto, keyring


def _print_recovery(slug: str, text: str) -> None:
    line = "═" * 68
    print(f"\n{line}")
    print(f"  RECOVERY-КЛЮЧ ЦЕРКВИ «{slug}» — показується ОДИН РАЗ")
    print(line)
    # Ламаємо по межах груп, а не посеред них: рядок, що починається з дефіса,
    # людина передрукує з помилкою.
    groups = text.split("-")
    line_parts: list[str] = []
    for g in groups:
        line_parts.append(g)
        if len(line_parts) == 12:
            print("    " + "-".join(line_parts)); line_parts = []
    if line_parts:
        print("    " + "-".join(line_parts))
    print(line)
    print("  Роздрукуйте й покладіть у сейф. Ніде більше він не зберігається.")
    print("  Без нього втрата всіх пристроїв = архів мертвий назавжди.")
    print("  Хто має цей аркуш — читає все й після відкликання пристроїв.")
    print(f"{line}\n")


async def run(slug: str, rotate: bool, label: str) -> int:
    try:
        node_private = keyring.processor_private_key()
    except keyring.NoProcessorKey:
        pair = crypto.new_device_keys()
        print("✗ CMA_PROCESSOR_KEY не заданий — обробний вузол не має ключа.\n")
        print("  Згенеровано пару. Покладіть приватну половину в .env на M1")
        print("  і запустіть знову. Нічого в базі не змінено.\n")
        print(f"  CMA_PROCESSOR_KEY={crypto.recovery_to_text(pair.private)}\n")
        return 2
    # The public half is derived, never configured separately: two spellings of
    # one key is how they drift apart, and the drift is silent until a church
    # cannot be processed.
    node_public = crypto.public_of(node_private)

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT id, name, kek_version FROM tenants WHERE slug = %s", (slug,))
        tenant = await cur.fetchone()
        if tenant is None:
            print(f"✗ церкви '{slug}' немає")
            return 1
        tid, version = tenant["id"], int(tenant["kek_version"])

        if version and not rotate:
            print(f"✗ «{tenant['name']}» уже зашифрована (KEK v{version}).")
            print("  Для нової генерації ключа: --rotate")
            return 1
        if not version and rotate:
            print(f"✗ «{tenant['name']}» ще не зашифрована — ротувати нічого.")
            return 1

        new_version = version + 1
        action = "РОТАЦІЯ" if rotate else "УВІМКНЕННЯ"
        print(f"\n{action} шифрування: «{tenant['name']}» (slug={slug}, id={tid})")
        print(f"  поточна версія KEK: {version} → {new_version}")

        await cur.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tid),))

        kek = crypto.new_key()
        recovery = crypto.new_device_keys()

        # Recipients: the node, the paper, and — on a rotation — every device
        # that has not been revoked. A revoked device simply gets no row, which
        # is what revocation is, mechanically.
        await cur.execute(
            "INSERT INTO church_devices (tenant_id, public_key, kind, label) "
            "VALUES (%s, %s, 'processor', %s) "
            "ON CONFLICT (tenant_id, public_key) DO UPDATE SET revoked_at = NULL "
            "RETURNING id",
            (tid, node_public, label))
        node_id = (await cur.fetchone())["id"]

        await cur.execute(
            "INSERT INTO church_devices (tenant_id, public_key, kind, label) "
            "VALUES (%s, %s, 'recovery', %s) RETURNING id",
            (tid, recovery.public, f"Сейф парафії (v{new_version})"))
        rec_id = (await cur.fetchone())["id"]

        await cur.execute(
            "SELECT id, public_key, kind, label FROM church_devices "
            "WHERE tenant_id = %s AND revoked_at IS NULL AND id <> %s",
            (tid, rec_id))
        recipients = [(r["id"], bytes(r["public_key"]), r["kind"], r["label"])
                      for r in await cur.fetchall()]
        recipients.append((rec_id, recovery.public, "recovery", "Сейф парафії"))

        for did, pub, kind, lbl in recipients:
            await cur.execute(
                "INSERT INTO key_envelopes (tenant_id, device_id, kek_version, wrapped_kek) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (device_id, kek_version) DO NOTHING",
                (tid, did, new_version, crypto.wrap_for(pub, kek)))
            print(f"  ✓ конверт для {kind:<9} {lbl}")

        # Rotation re-wraps the data keys, never the content (§5.2).
        rewrapped = 0
        if rotate:
            old_kek = await keyring.kek_for(pool, tid)
            await cur.execute(
                "SELECT scope, wrapped_dek FROM dek_envelopes "
                "WHERE tenant_id = %s AND kek_version = %s", (tid, version))
            for row in await cur.fetchall():
                scope = row["scope"]
                aad = crypto.context(tid, "dek", scope)
                dek = crypto.unwrap_dek(old_kek, bytes(row["wrapped_dek"]), aad)
                await cur.execute(
                    "INSERT INTO dek_envelopes (tenant_id, scope, kek_version, wrapped_dek) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (tid, scope, new_version, crypto.wrap_dek(kek, dek, aad)))
                rewrapped += 1
            print(f"  ✓ перезагорнуто DEK'ів: {rewrapped} (вміст не чіпали)")

        await cur.execute("UPDATE tenants SET kek_version = %s WHERE id = %s",
                          (new_version, tid))
        print(f"  ✓ kek_version = {new_version}")

    keyring.forget(tid)
    _print_recovery(slug, crypto.recovery_to_text(recovery.private))
    if not rotate:
        print("  Далі: наступна оброблена зустріч поїде на сервер шифротекстом.")
        print("  ⚠️  Веб більше не покаже стенограму й протокол цієї церкви —")
        print("      він читає ті самі файли з диска VPS. Це очікувано.\n")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Увімкнути шифрування для церкви")
    ap.add_argument("--tenant", required=True, help="slug церкви")
    ap.add_argument("--rotate", action="store_true",
                    help="нова генерація KEK (відкликання доступу)")
    ap.add_argument("--label", default="Обробний вузол (M1)",
                    help="як назвати обробний вузол у списку пристроїв")
    args = ap.parse_args()
    try:
        code = asyncio.run(run(args.tenant, args.rotate, args.label))
    finally:
        try:
            asyncio.run(close_pool())
        except Exception:
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
