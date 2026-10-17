"""Byte-level image fixtures shared by the Python test modules."""

from __future__ import annotations

import struct
import zlib


def png_bytes(width: int, height: int, text: list[bytes] | None = None) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + b"\x00\x00\x00\x00" * width for _ in range(height))
    chunks = [chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))]
    chunks += [chunk(b"tEXt", item) for item in (text or [])]
    chunks += [chunk(b"IDAT", zlib.compress(raw)), chunk(b"IEND", b"")]
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def jpeg_header(width: int, height: int, sof: int = 0xC0, big_app: int = 0) -> bytes:
    app1_body = b"Exif\x00\x00" + b"\x00" * 20
    app1 = b"\xff\xe1" + struct.pack(">H", len(app1_body) + 2) + app1_body
    filler = b""
    while big_app > 0:
        size = min(big_app, 65533)
        filler += b"\xff\xed" + struct.pack(">H", size + 2) + b"\x00" * size
        big_app -= size
    sof_body = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    sof_segment = bytes([0xFF, sof]) + struct.pack(">H", len(sof_body) + 2) + sof_body
    return b"\xff\xd8" + app1 + filler + sof_segment + b"\xff\xd9"


def webp_header(kind: str, width: int, height: int) -> bytes:
    if kind == "VP8X":
        payload = (b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little")
                   + (height - 1).to_bytes(3, "little"))
    elif kind == "VP8L":
        bits = (width - 1) | ((height - 1) << 14)
        payload = b"\x2f" + bits.to_bytes(4, "little") + b"\x00" * 5
    else:
        payload = (b"\x00\x00\x00\x9d\x01\x2a" + width.to_bytes(2, "little")
                   + height.to_bytes(2, "little"))
    chunk = kind.ljust(4).encode("ascii") + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk
