"""
Envelope encryption for church content — the scheme in `docs/cloud_plan.md` §5.1.

TWO LAYERS, AND THE POINT IS THE SECOND ONE.

    content  --[XChaCha20-Poly1305, random DEK]-->  ciphertext
    DEK      --[sealed box, per recipient        ]-->  one envelope each
    KEK      --[sealed box, per recipient        ]-->  one envelope each

The server keeps ciphertext and envelopes. An envelope is addressed to an X25519
public key whose private half never leaves the device that made it (Android
Keystore, hardware-backed). So the server can hold an envelope, hand it to the
right device, and be unable to open it — it stores *letters addressed to someone
else*, not a key it promises not to look at. That difference is the whole design:
§5 of the plan used to say the device re-fetches the key every 24 hours, which
quietly meant the server had it.

WHY SEALED BOX RATHER THAN A SHARED SECRET. `crypto_box_seal` needs only the
recipient's public key: the sender is anonymous and ephemeral. Adding a device
therefore needs the newcomer's public key and nothing of theirs in return, which
is exactly what a QR code can carry. The pairing must still be device-to-device
(§5.2) — a server that substitutes its own public key reads everything while
remaining, in its own logs, unable to unwrap anything.

WHY AAD IS NOT OPTIONAL HERE. Ciphertext alone is movable: a row's `resolved`
blob would decrypt just as happily in another row, another meeting, another
church. Every call binds the ciphertext to where it lives (tenant, meeting,
field), so a swapped blob fails to open rather than silently telling the wrong
story about the wrong meeting. This costs nothing and is the difference between
confidentiality and integrity.

WHAT THIS MODULE DELIBERATELY DOES NOT DO. No key storage, no rotation policy,
no device registry — those are the database's job (migration) and the app's.
This is the primitive layer, so it stays testable without a server, and the
tests in `tests/crypto_test.py` are the specification.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass

from nacl import bindings
from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox


# Both key kinds are 32 bytes: XChaCha20-Poly1305 takes a 256-bit key, and
# X25519 public/private halves are 32 bytes each. Naming them separately is for
# the reader — a KEK and a DEK are the same shape and very different things.
KEY_BYTES = 32
NONCE_BYTES = bindings.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES


class DecryptionFailed(Exception):
    """Wrong key, wrong context, or tampered bytes — deliberately one error.

    Distinguishing "wrong key" from "wrong AAD" from "modified ciphertext" would
    hand an attacker a probe. The caller cannot act differently on them anyway.
    """


# ─────────────────────────────────────────────────────────────
# Keys
# ─────────────────────────────────────────────────────────────

def new_key() -> bytes:
    """A fresh 256-bit key — used for both KEK (church) and DEK (per meeting)."""
    return os.urandom(KEY_BYTES)


@dataclass(frozen=True)
class DeviceKeys:
    """An X25519 pair. On Android the private half lives in Keystore and never
    appears here; this type exists for the processing node and for tests."""
    private: bytes
    public: bytes


def new_device_keys() -> DeviceKeys:
    sk = PrivateKey.generate()
    return DeviceKeys(private=bytes(sk), public=bytes(sk.public_key))


# ─────────────────────────────────────────────────────────────
# Envelopes — wrapping a key for a recipient
# ─────────────────────────────────────────────────────────────

def wrap_for(recipient_public: bytes, key: bytes) -> bytes:
    """Seal `key` to a public key. Anyone may create one; only the holder of the
    matching private key can open it. This is how a KEK reaches a new device and
    how a DEK reaches everyone at once."""
    if len(key) != KEY_BYTES:
        raise ValueError(f"key must be {KEY_BYTES} bytes, got {len(key)}")
    return SealedBox(PublicKey(recipient_public)).encrypt(key)


def unwrap(private_key: bytes, envelope: bytes) -> bytes:
    """Open an envelope addressed to us. Raises DecryptionFailed for an envelope
    meant for somebody else — which is the server's permanent situation."""
    try:
        return SealedBox(PrivateKey(private_key)).decrypt(envelope)
    except (CryptoError, ValueError) as e:
        raise DecryptionFailed("envelope is not for this key") from e


# ─────────────────────────────────────────────────────────────
# Content
# ─────────────────────────────────────────────────────────────

def context(*parts: object) -> bytes:
    """Build the AAD that pins a ciphertext to its place.

    Order matters and is part of the format: ("t", 1, "protocol_items", 42,
    "resolved"). Any change to how a caller spells its context makes existing
    ciphertext undecryptable, so this is a schema decision, not a convenience —
    write it once per call site and never "improve" it.
    """
    return "\x1f".join(str(p) for p in parts).encode("utf-8")


def encrypt(dek: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """XChaCha20-Poly1305. Returns nonce || ciphertext || tag as one blob.

    XChaCha rather than ChaCha for the 192-bit nonce: at that size a random
    nonce per message never repeats in practice, so callers need no counter and
    no state — which matters because writers here are a phone, a laptop worker
    and a web process that do not coordinate.
    """
    if len(dek) != KEY_BYTES:
        raise ValueError(f"DEK must be {KEY_BYTES} bytes, got {len(dek)}")
    nonce = os.urandom(NONCE_BYTES)
    ct = bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
        plaintext, aad, nonce, dek
    )
    return nonce + ct


def decrypt(dek: bytes, blob: bytes, aad: bytes) -> bytes:
    """Inverse of encrypt(). Fails on a wrong key, a wrong context, or a single
    flipped bit — the AEAD tag covers ciphertext and AAD together."""
    if len(blob) <= NONCE_BYTES:
        raise DecryptionFailed("blob too short to contain a nonce")
    nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        return bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
            ct, aad, nonce, dek
        )
    except CryptoError as e:
        raise DecryptionFailed("wrong key, wrong context, or tampered bytes") from e


# ─────────────────────────────────────────────────────────────
# Recovery key — the printed third recipient
# ─────────────────────────────────────────────────────────────
#
# The recovery key is not a special mechanism: it is another X25519 pair whose
# private half is printed and put in the parish safe. Recovery is then the
# ordinary unwrap path, which means it is exercised by the same code every
# device uses — a recovery route with its own code path is a recovery route
# nobody finds out is broken until the day it is needed.

_ALPHABET_RE = str.maketrans("", "", " -\n\t")


def recovery_to_text(private_key: bytes) -> str:
    """Render a private half as something a human can copy off paper.

    Base32 (no 0/1/8/O/I ambiguity once uppercased), grouped in fours, with two
    checksum bytes appended so a typo is caught on entry rather than becoming a
    silent "wrong key" on the worst day the church has had.
    """
    if len(private_key) != KEY_BYTES:
        raise ValueError("recovery key must be 32 bytes")
    check = hashlib.sha256(private_key).digest()[:2]
    raw = base64.b32encode(private_key + check).decode("ascii").rstrip("=")
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def recovery_from_text(text: str) -> bytes:
    """Parse what was typed back in. Raises ValueError on a damaged key."""
    raw = text.translate(_ALPHABET_RE).upper()
    pad = (-len(raw)) % 8
    try:
        blob = base64.b32decode(raw + "=" * pad)
    except Exception as e:
        raise ValueError("recovery key is not readable") from e
    if len(blob) != KEY_BYTES + 2:
        raise ValueError("recovery key has the wrong length")
    key, check = blob[:KEY_BYTES], blob[KEY_BYTES:]
    if hashlib.sha256(key).digest()[:2] != check:
        raise ValueError("recovery key checksum does not match — mistyped?")
    return key
