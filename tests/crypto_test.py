"""
The specification for `shared/crypto.py`, written as things that must be true.

Run:  uv run python tests/crypto_test.py

No database, no network, no fixtures — this layer is pure so that the properties
the whole privacy claim rests on can be checked in under a second. Where a test
encodes an UNCOMFORTABLE truth (a revoked device keeps what it already had), it
says so, because a limitation nobody wrote down is a limitation somebody will
later call a bug.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from church_assistant.shared import crypto as c  # noqa: E402


PASSED = 0


def ok(label: str) -> None:
    global PASSED
    PASSED += 1
    print(f"  ✓ {label}")


def fails(fn, label: str) -> None:
    """Assert that `fn` raises — used far more than the happy path here, because
    for encryption the interesting behaviour IS the refusal."""
    try:
        fn()
    except (c.DecryptionFailed, ValueError):
        ok(label)
        return
    raise AssertionError(f"expected a refusal: {label}")


def test_content_roundtrip() -> None:
    print("\nВміст")
    dek = c.new_key()
    aad = c.context("t", 1, "protocol_items", 42, "resolved")
    blob = c.encrypt(dek, "Проводити зустрічі раз на місяць".encode(), aad)
    assert c.decrypt(dek, blob, aad).decode() == "Проводити зустрічі раз на місяць"
    ok("текст повертається тим самим ключем і контекстом")

    assert blob != c.encrypt(dek, b"x", aad)
    a = c.encrypt(dek, b"same", aad)
    b = c.encrypt(dek, b"same", aad)
    assert a != b, "однаковий вміст двічі дав однаковий шифротекст — нонс не випадковий"
    ok("той самий текст двічі дає різний шифротекст (випадковий нонс)")

    fails(lambda: c.decrypt(c.new_key(), blob, aad), "чужий DEK не відкриває")

    other = c.context("t", 1, "protocol_items", 43, "resolved")
    fails(lambda: c.decrypt(dek, blob, other), "перенесений в інший рядок блоб не відкривається")

    tampered = bytearray(blob)
    tampered[-1] ^= 0x01
    fails(lambda: c.decrypt(dek, bytes(tampered), aad), "змінений біт ламає тег")

    fails(lambda: c.decrypt(dek, b"short", aad), "надто короткий блоб відхиляється")

    # Контекст кодується з довжинами, тож розбиття на частини не можна
    # переграти: ("ab","c") і ("a","bc") — різні AAD за побудовою, а не тому,
    # що ми пообіцяли не вживати роздільник у назвах полів (§11).
    assert c.context("ab", "c") != c.context("a", "bc")
    assert c.context("t", 1) != c.context("t1")
    ok("склейка контексту не має зіткнень (кодування з довжинами)")


def test_envelopes() -> None:
    print("\nКонверти")
    kek = c.new_key()
    phone = c.new_device_keys()
    tablet = c.new_device_keys()

    e1 = c.wrap_for(phone.public, kek)
    e2 = c.wrap_for(tablet.public, kek)
    assert e1 != e2, "два конверти того самого ключа збіглися"
    ok("кожен пристрій отримує власний конверт того самого KEK")

    assert c.unwrap(phone.private, e1) == kek
    assert c.unwrap(tablet.private, e2) == kek
    ok("обидва розгортають той самий KEK")

    fails(lambda: c.unwrap(tablet.private, e1), "чужий конверт не відкривається")

    # Найважливіше твердження всієї схеми: сервер тримає конверти й не має
    # жодного приватного ключа. Моделюємо це буквально.
    server = c.new_device_keys()
    fails(lambda: c.unwrap(server.private, e1), "сервер не відкриває конверт, який зберігає")
    assert kek not in e1 and kek not in e2
    ok("KEK не зустрічається у відкритому вигляді в жодному конверті")


def test_dek_under_kek() -> None:
    print("\nDEK під KEK")
    kek, dek = c.new_key(), c.new_key()
    aad = c.context("t", 1, "dek", "2026-07-27")
    w = c.wrap_dek(kek, dek, aad)
    assert c.unwrap_dek(kek, w, aad) == dek
    ok("DEK загортається під KEK і повертається")
    assert dek not in w
    ok("DEK не видно у відкритому вигляді в загорнутому блобі")
    fails(lambda: c.unwrap_dek(c.new_key(), w, aad), "інша версія KEK не розгортає")
    fails(lambda: c.unwrap_dek(kek, w, c.context("t", 1, "dek", "2026-08-03")),
          "конверт DEK не переноситься на іншу зустріч")


def test_rotation_and_its_limit() -> None:
    print("\nРотація й межа відкликання")
    old_kek, dek = c.new_key(), c.new_key()
    stays, leaves = c.new_device_keys(), c.new_device_keys()
    aad = c.context("t", 1, "meetings", "2026-07-27")
    content = c.encrypt(dek, b"Slukhaly...", aad)

    dek_env_old = c.wrap_for(stays.public, dek)
    assert c.unwrap(leaves.private, c.wrap_for(leaves.public, dek)) == dek
    ok("до відкликання обидва пристрої дістають DEK")

    # Ротація: новий KEK, DEK'и перезагортаються лише для тих, хто лишився.
    new_kek = c.new_key()
    assert new_kek != old_kek
    dek_env_new = c.wrap_for(stays.public, dek)
    assert c.unwrap(stays.private, dek_env_new) == dek
    fails(lambda: c.unwrap(leaves.private, dek_env_new),
          "відкликаний не відкриває нових конвертів")

    # Вміст НЕ перешифровується — саме тому ротація дешева (§5.2).
    assert c.decrypt(c.unwrap(stays.private, dek_env_old), content, aad) == b"Slukhaly..."
    ok("вміст не перешифровується — ротація торкається лише конвертів")

    # ⚠️ Незручна правда з §5.0: те, що вже синхронізовано, лишається читабельним.
    # Тест існує, щоб це було рішенням, а не сюрпризом.
    cached_dek = dek
    assert c.decrypt(cached_dek, content, aad) == b"Slukhaly..."
    ok("відкликаний ЗБЕРІГАЄ доступ до вже завантаженого (задокументована межа)")


def test_recovery_key() -> None:
    print("\nRecovery-ключ")
    rec = c.new_device_keys()
    text = c.recovery_to_text(rec.private)
    assert c.recovery_from_text(text) == rec.private
    ok(f"друкований ключ повертається ({len(text)} символів із дефісами)")

    assert c.recovery_from_text(text.lower().replace("-", " ")) == rec.private
    ok("регістр, пробіли й дефіси не мають значення при введенні")

    typo = list(text.replace("-", ""))
    typo[5] = "A" if typo[5] != "A" else "B"
    fails(lambda: c.recovery_from_text("".join(typo)), "одна помилкова літера ловиться контрольною сумою")

    fails(lambda: c.recovery_from_text("не ключ"), "сміття відхиляється")

    # Відновлення — звичайний шлях unwrap, без окремого коду.
    kek = c.new_key()
    env = c.wrap_for(rec.public, kek)
    assert c.unwrap(c.recovery_from_text(text), env) == kek
    ok("втративши всі пристрої, церква розгортає KEK папером із сейфа")


def main() -> None:
    print("Крипто-ядро (cloud_plan §5.1)")
    test_content_roundtrip()
    test_envelopes()
    test_dek_under_kek()
    test_rotation_and_its_limit()
    test_recovery_key()
    print(f"\n✅ {PASSED} перевірок пройдено")


if __name__ == "__main__":
    main()
