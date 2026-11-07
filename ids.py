"""ULID-like identifiers: 26 Crockford base32 characters, 48-bit millisecond time then 80 random bits."""

from __future__ import annotations

import os
import re
import time

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(now_ms: int | None = None, randomness: bytes | None = None) -> str:
    """Return a new time-sortable id. Both inputs are injectable for tests."""
    ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if not 0 <= ms < 2**48:
        raise ValueError("timestamp out of range")
    rand = os.urandom(10) if randomness is None else randomness
    if len(rand) != 10:
        raise ValueError("randomness must be 10 bytes")
    return _encode(ms, 10) + _encode(int.from_bytes(rand, "big"), 16)


def is_valid(value: object) -> bool:
    return isinstance(value, str) and bool(ID_RE.match(value))


def require_valid(value: object) -> str:
    """Return the id or raise ValueError; ids end up in ref names and paths, so be strict."""
    if not is_valid(value):
        raise ValueError(f"invalid baton id: {value!r}")
    return value  # type: ignore[return-value]


def timestamp_ms(value: str) -> int:
    require_valid(value)
    out = 0
    for ch in value[:10]:
        out = out * 32 + ALPHABET.index(ch)
    return out
