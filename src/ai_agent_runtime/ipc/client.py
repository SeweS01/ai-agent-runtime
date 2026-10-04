"""Small request/response client for the local Runtime socket."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .protocol import read_frame, validate_version, write_frame


async def request(
    socket_path: Path,
    payload: dict[str, Any],
    *,
    response_timeout: float = 30.0,
    write_timeout: float = 10.0,
) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        await write_frame(writer, payload, timeout=write_timeout)
        response = await read_frame(reader, timeout=response_timeout)
        validate_version(response)
        return response
    finally:
        writer.close()
        await writer.wait_closed()
