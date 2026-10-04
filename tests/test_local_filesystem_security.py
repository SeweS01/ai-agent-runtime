from __future__ import annotations

import socket
from dataclasses import replace
from pathlib import Path

import pytest

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.daemon.server import (
    _prepare_runtime_socket,
    _socket_identity,
    _unlink_runtime_socket,
)
from ai_agent_runtime.state.store import RuntimeStore


def _valid_config(tmp_path: Path) -> RuntimeConfig:
    state = tmp_path / "state"
    return replace(
        RuntimeConfig.defaults(),
        agent_instance_id="agent-1",
        forum_chat_id=-1001,
        state_dir=state,
        socket_path=tmp_path / "run" / "runtime.sock",
        database_path=state / "runtime.sqlite",
        real_codex_version="test",
        real_codex_sha256=f"sha256:{'0' * 64}",
        upstream_sha="0" * 40,
        allowed_project_roots=(tmp_path,),
    )


def test_config_contains_database_and_uses_private_socket_directory(tmp_path: Path) -> None:
    config = _valid_config(tmp_path)
    config.validate()
    with pytest.raises(ValueError, match="contained by state_dir"):
        replace(config, database_path=tmp_path / "other" / "runtime.sqlite").validate()
    with pytest.raises(ValueError, match="private non-root directory"):
        replace(config, socket_path=Path("/runtime.sock")).validate()


def test_runtime_store_refuses_symlink_or_unsafe_existing_database(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    target = tmp_path / "outside.sqlite"
    target.write_bytes(b"outside")
    database = state / "runtime.sqlite"
    database.symlink_to(target)
    with pytest.raises(ValueError, match="must not be a symlink"):
        RuntimeStore(database)
    database.unlink()
    database.write_bytes(b"not sqlite")
    database.chmod(0o666)
    with pytest.raises(ValueError, match="mode is unsafe"):
        RuntimeStore(database)


def test_runtime_socket_replaces_only_owned_stale_socket(tmp_path: Path) -> None:
    path = tmp_path / "run" / "runtime.sock"
    path.parent.mkdir(mode=0o700)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    _prepare_runtime_socket(path)
    assert not path.exists()

    path.write_text("do not unlink", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to replace"):
        _prepare_runtime_socket(path)
    assert path.read_text(encoding="utf-8") == "do not unlink"


def test_socket_cleanup_does_not_unlink_replacement_path(tmp_path: Path) -> None:
    path = tmp_path / "run" / "runtime.sock"
    path.parent.mkdir(mode=0o700)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    identity = _socket_identity(path)
    listener.close()
    path.unlink()
    path.write_text("replacement", encoding="utf-8")
    _unlink_runtime_socket(path, identity)
    assert path.read_text(encoding="utf-8") == "replacement"


def test_runtime_socket_rejects_unsafe_parent_mode(tmp_path: Path) -> None:
    parent = tmp_path / "run"
    parent.mkdir(mode=0o700)
    parent.chmod(0o777)
    with pytest.raises(ValueError, match="group/world writable"):
        _prepare_runtime_socket(parent / "runtime.sock")
