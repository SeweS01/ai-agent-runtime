import asyncio
import struct

import pytest

from ai_agent_runtime.ipc.protocol import MAX_FRAME_SIZE, read_frame


@pytest.mark.asyncio
async def test_read_frame_rejects_oversized_frame() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", MAX_FRAME_SIZE + 1))
    with pytest.raises(ValueError, match="invalid frame size"):
        await read_frame(reader)


@pytest.mark.asyncio
async def test_read_frame_rejects_partial_payload() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", 10) + b"{}")
    reader.feed_eof()
    with pytest.raises(asyncio.IncompleteReadError):
        await read_frame(reader)
