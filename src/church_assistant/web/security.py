"""
Web auth primitives — password hashing and signed session cookies (MT Phase 3).

Deliberately dependency-free (stdlib only): the whole point of this deployment is
that everything runs locally on the church's own server, so we don't pull in
passlib/bcrypt/itsdangerous for two well-understood primitives.

    Passwords — hashlib.scrypt (memory-hard, in the stdlib since 3.6). Stored as
        scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>
    so the cost parameters travel with the hash and can be raised later without
    invalidating existing accounts.

    Sessions — a signed (NOT encrypted) cookie carrying one opaque token:
        <payload_b64>.<hmac_sha256_b64>   where payload = {"sid": "<token>"}
    The identity itself lives in the web_sessions table (migration 008); the
    cookie only points at it. That is what makes a session revocable — see
    db/web_sessions_repo.py.

    Why still sign it, when the DB is the authority? Because the signature is a
    free pre-filter: a junk or tampered cookie is rejected in-process, so random
    traffic never reaches the session lookup. Rotating WEB_SECRET_KEY therefore
    still signs everyone out, and remains the blunt emergency lever next to the
    per-session revocation the table now provides.

    Tokens — 256 bits of CSPRNG output, stored only as a SHA-256 digest. Plain
    SHA-256 rather than scrypt is correct here precisely because the input is
    already high-entropy: there is no guessable password to slow an attacker
    down over, only a value they cannot enumerate.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Optional

from dotenv import load_dotenv


# ─────────────────────────────────────────────────────────────
# Parameters
# ─────────────────────────────────────────────────────────────

# scrypt cost: 16 MiB (128 * n * r), ~50-100 ms on an M1. Raising N later is safe
# — old hashes still verify with the parameters recorded inside them.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16
# scrypt needs ~128*n*r bytes; give OpenSSL explicit headroom (its default cap is
# lower than what N=16384 needs on some builds).
SCRYPT_MAXMEM = 128 * SCRYPT_N * SCRYPT_R * 2

SESSION_COOKIE = "cma_session"
# 12 h — long enough for a working day, short enough that a forgotten open
# browser doesn't stay authorized indefinitely.
SESSION_TTL_SECONDS = int(os.getenv("WEB_SESSION_TTL", str(12 * 3600)))


class SecretKeyMissing(RuntimeError):
    """WEB_SECRET_KEY is not configured — refuse to run with a guessable key."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# ─────────────────────────────────────────────────────────────
# Secret key
# ─────────────────────────────────────────────────────────────

def get_secret_key() -> bytes:
    """
    The HMAC key for session cookies (WEB_SECRET_KEY in .env).

    Raises rather than falling back to a default: a predictable key would let
    anyone forge a session for any tenant, which is exactly the isolation RLS is
    there to guarantee. Generate one with:
        python -c "import secrets; print(secrets.token_urlsafe(48))"
    """
    load_dotenv()
    key = os.getenv("WEB_SECRET_KEY", "").strip()
    if not key:
        raise SecretKeyMissing(
            "WEB_SECRET_KEY is not set. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(48))"\n'
            "and add it to .env (see .env.example)."
        )
    return key.encode("utf-8")


# ─────────────────────────────────────────────────────────────
# Passwords
# ─────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Hash a plaintext password → 'scrypt$n$r$p$salt$hash' (safe to store)."""
    if not password:
        raise ValueError("password cannot be empty")
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """
    Check a plaintext password against a stored hash. False on any malformed
    input — never raises, so a corrupt row can't 500 the login route.
    """
    try:
        scheme, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
    except (ValueError, AttributeError, TypeError):
        return False

    try:
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n, r=r, p=p,
            dklen=len(expected),
            maxmem=128 * n * r * 2,
        )
    except ValueError:
        return False
    return hmac.compare_digest(dk, expected)


# A hash of a random throwaway password, computed once per process. Verifying
# against it makes the "unknown username" path cost the same as a real check, so
# response timing doesn't reveal which accounts exist.
_DUMMY_HASH: Optional[str] = None


def waste_time_like_a_real_check() -> None:
    """Spend one scrypt round on nothing (timing-equalizer for unknown users)."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    verify_password("wrong password, deliberately", _DUMMY_HASH)


# ─────────────────────────────────────────────────────────────
# Session tokens
# ─────────────────────────────────────────────────────────────

SESSION_TOKEN_BYTES = 32          # 256 bits — not enumerable


def new_session_token() -> str:
    """A fresh opaque session token. Handed to the browser, never stored."""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_token(token: str) -> str:
    """
    SHA-256 hex of a session token — this is what goes in the database.

    Unsalted and fast on purpose: the token is already 256 random bits, so the
    slow, salted hashing that protects human-chosen passwords buys nothing here,
    while the lookup happens on every single request. What it does buy is that a
    leaked dump of web_sessions contains nothing a browser could present.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────
# Signed session cookie
# ─────────────────────────────────────────────────────────────

def sign_session(data: dict[str, Any], *, ttl: int = SESSION_TTL_SECONDS) -> str:
    """Serialize + sign a session payload. Adds 'exp' (unix seconds)."""
    payload = dict(data)
    payload["exp"] = int(time.time()) + ttl
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = _b64e(raw)
    sig = hmac.new(get_secret_key(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def load_session(token: Optional[str]) -> Optional[dict[str, Any]]:
    """
    Verify + decode a session cookie. None if absent, tampered, or expired.

    Signature is checked BEFORE the payload is parsed, so nothing an attacker
    controls reaches json.loads with any authority.
    """
    if not token or "." not in token:
        return None
    body, _, sig_b64 = token.partition(".")
    try:
        expected = hmac.new(
            get_secret_key(), body.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64d(sig_b64), expected):
            return None
        payload = json.loads(_b64d(body))
    except (ValueError, TypeError, SecretKeyMissing):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        return None
    return payload


# ─────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────

def _smoke_test() -> None:
    os.environ.setdefault("WEB_SECRET_KEY", secrets.token_urlsafe(48))

    print("=" * 60)
    print("  web/security — smoke test")
    print("=" * 60)

    h = hash_password("правильний пароль")
    assert h.startswith("scrypt$")
    assert verify_password("правильний пароль", h)
    assert not verify_password("неправильний", h)
    assert not verify_password("правильний пароль", "garbage")
    assert not verify_password("правильний пароль", "scrypt$x$y$z$q$w")
    print("1. scrypt hash/verify ✓ (wrong password + malformed hash rejected)")

    # Two hashes of the same password differ (random salt).
    assert hash_password("same") != hash_password("same")
    print("2. per-password salt ✓")

    t1, t2 = new_session_token(), new_session_token()
    assert t1 != t2 and len(t1) >= 40
    assert hash_token(t1) == hash_token(t1) != hash_token(t2)
    assert t1 not in hash_token(t1)      # the stored form does not contain it
    print("3. session tokens unique + hashed one-way ✓")

    tok = sign_session({"sid": t1})
    s = load_session(tok)
    assert s is not None and s["sid"] == t1
    print("4. session cookie sign/load ✓")

    # Tamper with the payload → signature fails, so a swapped-in session id
    # never reaches the database lookup.
    body, _, sig = tok.partition(".")
    forged_payload = _b64e(
        json.dumps({"sid": t2, "exp": int(time.time()) + 60},
                   separators=(",", ":"), sort_keys=True).encode()
    )
    assert load_session(f"{forged_payload}.{sig}") is None
    print("5. forged session id rejected before any DB hit ✓")

    assert load_session(sign_session({"sid": t1}, ttl=-1)) is None
    assert load_session(None) is None and load_session("nonsense") is None
    print("6. expiry + garbage rejected ✓")

    print("=" * 60)
    print("  ✓ ALL SECURITY SMOKE TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    _smoke_test()
