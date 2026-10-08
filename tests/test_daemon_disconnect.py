"""A disconnected IPC peer must not flood the daemon log or block its loop."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.daemon import server as server_module
from ai_agent_runtime.daemon.server import RuntimeServer
from ai_agent_runtime.ipc.protocol import versioned


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["status", "invalid_action", None])
async def test_disconnected_peer_during_response_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str | None
) -> None:
    config = RuntimeConfig(
        agent_instance_id="test-agent",
        forum_chat_id=-1001,
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "runtime.sock",
        database_path=tmp_path / "state" / "runtime.sqlite",
        real_codex=tmp_path / "codex-real",
        real_codex_version="test",
        real_codex_sha256="sha256:" + "0" * 64,
        profiles_root=tmp_path / "profiles",
        topic_config_path=tmp_path / "topics.json",
        tmux_state_path=tmp_path / "tmux-state.json",
        upstream_sha="0" * 40,
    )
    daemon = RuntimeServer(config)

    async def fake_read_frame(_reader: object) -> dict[str, object]:
        if action is None:
            raise asyncio.IncompleteReadError(partial=b"", expected=4)
        return versioned(action, "request-disconnect")

    monkeypatch.setattr(server_module, "read_frame", fake_read_frame)
    monkeypatch.setattr(daemon, "_validate_peer", lambda _writer: (os.getpid(), os.getuid()))
    writer = Mock()
    writer.drain = AsyncMock(side_effect=ConnectionResetError("peer closed"))
    writer.wait_closed = AsyncMock(side_effect=BrokenPipeError("peer closed"))

    try:
        await daemon._handle(Mock(), writer)
    finally:
        daemon.store.close()

    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()
    if action is None:
        writer.drain.assert_not_awaited()
    else:
        writer.drain.assert_awaited_once()
