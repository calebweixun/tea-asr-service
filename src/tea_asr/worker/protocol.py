from __future__ import annotations

import asyncio
import json
import struct
from typing import Any, BinaryIO

MAX_HEADER_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_PCM_BYTES = 960_000


def encode_message(payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ValueError("IPC message is too large")
    return struct.pack(">I", len(encoded)) + encoded


def read_exact(stream: BinaryIO, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = stream.read(length - len(chunks))
        if not chunk:
            raise EOFError("Unexpected EOF")
        chunks.extend(chunk)
    return bytes(chunks)


def read_request(stream: BinaryIO) -> tuple[dict[str, Any], bytes]:
    size = struct.unpack(">I", read_exact(stream, 4))[0]
    if size > MAX_HEADER_BYTES:
        raise ValueError("IPC header is too large")
    header = json.loads(read_exact(stream, size))
    pcm_bytes = int(header.get("pcm_bytes", 0))
    if pcm_bytes < 0 or pcm_bytes > MAX_PCM_BYTES or pcm_bytes % 2:
        raise ValueError("Invalid IPC PCM length")
    return header, read_exact(stream, pcm_bytes)


async def read_response(stream: asyncio.StreamReader) -> dict[str, Any]:
    size = struct.unpack(">I", await stream.readexactly(4))[0]
    if size > MAX_RESPONSE_BYTES:
        raise ValueError("IPC response is too large")
    return json.loads(await stream.readexactly(size))

