"""Compress-then-encrypt for snapshot uploads.

Each uploaded part (the manifest and every snapshot file) is sealed on its own:

    zstd (or zlib if zstandard is not installed)  ->  AES-256-GCM

Wire format of one sealed part:

    "RSW1" | codec (1 byte) | nonce (12 bytes) | ciphertext + 16-byte GCM tag

The header and the part's file name are authenticated (GCM additional data), so a part
cannot be passed off under another name, and a truncated or modified part fails to open.
The codec byte is self-describing, so the Jetson can fall back to zlib without breaking
the cloud side.

Compression happens BEFORE encryption: ciphertext looks random and cannot be compressed.
Each seal uses a fresh random 12-byte nonce, which is safe at this message volume.

The key is 32 random bytes, base64 encoded, shared by the Jetson and the cloud API
(env RESCUE_PAYLOAD_KEY). Generate one with:

    python -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"

KEEP THIS FILE IDENTICAL to server/rescue_api/wire.py (a test checks it); the two sides
deploy separately, so each carries its own copy.
"""
from __future__ import annotations

import base64
import binascii
import os
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:  # preferred codec; the pipeline still works without it
    import zstandard
except ImportError:  # pragma: no cover - depends on the environment
    zstandard = None

MAGIC = b"RSW1"
CODEC_ZSTD, CODEC_ZLIB = 1, 2
NONCE_LEN, TAG_LEN = 12, 16
PREFIX_LEN = len(MAGIC) + 1          # authenticated header: magic + codec
HEADER_LEN = PREFIX_LEN + NONCE_LEN
ZSTD_LEVEL = 3                       # fast on the Orin Nano; raise to 6-10 if bandwidth-bound
DEFAULT_MAX_OUT = 10 * 1024 * 1024   # decompressed-size cap, guards against zip bombs


class WireError(ValueError):
    """A sealed part could not be opened (bad key, tampered, truncated, or malformed)."""


def load_key(value: str | None) -> bytes | None:
    """Parse a base64 key from the environment. None/empty means 'encryption off'."""
    if not value or not value.strip():
        return None
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise WireError("payload key is not valid base64") from None
    if len(key) != 32:
        raise WireError(f"payload key must decode to 32 bytes, got {len(key)}")
    return key


def is_sealed(blob: bytes) -> bool:
    return blob.startswith(MAGIC)


def _aad(prefix: bytes, name: str) -> bytes:
    return prefix + name.encode()


def seal(data: bytes, key: bytes, name: str, level: int = ZSTD_LEVEL) -> bytes:
    if zstandard is not None:
        codec, body = CODEC_ZSTD, zstandard.ZstdCompressor(level=level).compress(data)
    else:
        codec, body = CODEC_ZLIB, zlib.compress(data, 6)
    prefix = MAGIC + bytes([codec])
    nonce = os.urandom(NONCE_LEN)
    return prefix + nonce + AESGCM(key).encrypt(nonce, body, _aad(prefix, name))


def open_sealed(blob: bytes, key: bytes, name: str, max_out: int = DEFAULT_MAX_OUT) -> bytes:
    if len(blob) < HEADER_LEN + TAG_LEN or not is_sealed(blob):
        raise WireError("not a sealed payload (missing or short header)")
    prefix, nonce, ct = blob[:PREFIX_LEN], blob[PREFIX_LEN:HEADER_LEN], blob[HEADER_LEN:]
    try:
        body = AESGCM(key).decrypt(nonce, ct, _aad(prefix, name))
    except InvalidTag:
        raise WireError("integrity check failed (wrong key, tampered, or truncated)") from None
    codec = blob[len(MAGIC)]
    if codec == CODEC_ZSTD:
        if zstandard is None:
            raise WireError("part is zstd-compressed but the 'zstandard' package is not installed")
        try:
            declared = zstandard.frame_content_size(body)
            if declared > max_out:
                raise WireError("decompressed payload exceeds the size limit")
            return zstandard.ZstdDecompressor().decompress(body, max_output_size=max_out)
        except zstandard.ZstdError:
            raise WireError("corrupt zstd stream") from None
    if codec == CODEC_ZLIB:
        d = zlib.decompressobj()
        try:
            out = d.decompress(body, max_out + 1)
        except zlib.error:
            raise WireError("corrupt zlib stream") from None
        if len(out) > max_out:
            raise WireError("decompressed payload exceeds the size limit")
        if not d.eof:
            raise WireError("truncated zlib stream")
        return out
    raise WireError(f"unknown codec {codec}")
