"""Small request/response client for the local Runtime socket."""

from __future__ import annotations

import asyncio
import socket
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .protocol import read_frame, validate_version, write_frame

_PEERCRED = struct.Struct("3i")
PeerAuthorizer = Callable[[int, int, int], None]


async def request(
    socket_path: Path,
    payload: dict[str, Any],
    *,
    response_timeout: float = 30.0,
    write_timeout: float = 10.0,
    peer_authorizer: PeerAuthorizer | None = None,
) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        if peer_authorizer is not None:
            transport_socket = writer.get_extra_info("socket")
            if transport_socket is None:
                raise OSError("Runtime connection has no peer socket")
            raw = transport_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size)
            peer_authorizer(*_PEERCRED.unpack(raw))
        await write_frame(writer, payload, timeout=write_timeout)
        response = await read_frame(reader, timeout=response_timeout)
        validate_version(response)
        return response
    finally:
        writer.close()
        await writer.wait_closed()
