"""
Turning a meeting folder into ciphertext on the way out, and back on the way in.

WHERE THIS SITS. The pipeline works on plaintext because it must (diarization,
Gemma). The VPS must hold ciphertext because that is the whole promise. So the
conversion belongs exactly at the sync boundary — `artifact_sync` pushes and
pulls, and this module is what those two calls run through when a church has a
key. A church at `kek_version = 0` never touches this code at all.

ONE FILE, ONE BLOB, ONE NAME + `.enc`. Not an archive: rsync's job is to move
only what changed, and a tar would make every push carry the whole folder. The
suffix is deliberate rather than cosmetic — a file that is ciphertext should not
be mistakable for one that is not, least of all by a future reader of the VPS
filesystem trying to work out what went wrong.

NOTHING IS LEFT IN THE CLEAR — INCLUDING AUDIO, since 07.09. It used to be the
exception, and the reason was the web player: serving a stream from ciphertext
means either the server holds the key during playback or the client downloads
95 MB before the first second. That reason died when the web stopped playing
audio at all (cloud_plan §2.1) — listening happens in the app, from the church's
own copy, and the server only ever holds the recording in transit. An exception
whose justification has gone is just a leak with a comment attached.

FILENAMES AND SIZES STAY VISIBLE. `speakers.json` is still called that, and its
length still shows. Hiding structure means padding and renaming, which buys
little against an adversary who already knows this is a meeting folder, and
costs the ability to sync incrementally. Named here so nobody later mistakes it
for something we failed to notice.
"""

from __future__ import annotations

from pathlib import Path

from church_assistant.shared import crypto


ENC_SUFFIX = ".enc"


def _aad(tenant_id: int, scope: str, rel: str) -> bytes:
    """Bind a blob to its church, its meeting and its place in the folder.

    Without the path, `polished.md` and `annotated.md` would be
    interchangeable — the same key opens both — and swapping them would produce
    a meeting whose summary belongs to a different part of itself.
    """
    return crypto.context(tenant_id, "artifact", scope, rel)


def _walk(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def encrypt_tree(src: Path, dst: Path, dek: bytes, tenant_id: int, scope: str) -> int:
    """Write an encrypted mirror of `src` into `dst`. Returns files encrypted.

    Every file, with no exception list — see the module docstring for why audio
    stopped being one.
    """
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for path in _walk(src):
        rel = str(path.relative_to(src))
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        blob = crypto.encrypt(dek, path.read_bytes(), _aad(tenant_id, scope, rel))
        out.with_name(out.name + ENC_SUFFIX).write_bytes(blob)
        n += 1
    return n


def decrypt_tree(src: Path, dst: Path, dek: bytes, tenant_id: int, scope: str) -> int:
    """Inverse of encrypt_tree(). Returns files decrypted.

    A blob that will not open raises rather than being skipped. A half-decrypted
    meeting folder is worse than a failed sync: the pipeline would run against
    whatever survived and write a protocol from half a transcript.
    """
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for path in _walk(src):
        rel = str(path.relative_to(src))
        if not rel.endswith(ENC_SUFFIX):
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(path.read_bytes())
            continue
        plain_rel = rel[: -len(ENC_SUFFIX)]
        out = dst / plain_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(
            crypto.decrypt(dek, path.read_bytes(), _aad(tenant_id, scope, plain_rel))
        )
        n += 1
    return n
