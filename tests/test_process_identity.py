import os
from pathlib import Path

import pytest

from ai_agent_runtime import process_safety
from ai_agent_runtime.domain import WrapperIdentity
from ai_agent_runtime.errors import ErrorCode, RuntimeFailureError
from ai_agent_runtime.process_safety import TmuxPaneProcess, verify_wrapper_identity


def _identity() -> WrapperIdentity:
    return WrapperIdentity(
        pid=os.getpid(),
        starttime=123,
        uid=os.getuid(),
        tmux_session="cc-n1001-42",
        tmux_pane="%7",
        cwd=str(Path.cwd()),
    )


def test_wrapper_identity_is_independently_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_safety, "proc_uid", lambda _pid: os.getuid())
    monkeypatch.setattr(process_safety, "proc_starttime", lambda _pid: 123)
    monkeypatch.setattr(
        process_safety,
        "proc_environment",
        lambda _pid: {"TMUX_PANE": "%7", "TMUX": "/safe/tmux.sock,1,0"},
    )
    monkeypatch.setattr(
        process_safety,
        "_tmux_socket_from_environment",
        lambda _value: Path("/safe/tmux.sock"),
    )
    monkeypatch.setattr(
        process_safety,
        "inspect_tmux_pane",
        lambda _socket, _pane: TmuxPaneProcess("%7", "cc-n1001-42", os.getpid()),
    )
    verify_wrapper_identity(_identity())


def test_wrapper_identity_rejects_tmux_process_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_safety, "proc_uid", lambda _pid: os.getuid())
    monkeypatch.setattr(process_safety, "proc_starttime", lambda _pid: 123)
    monkeypatch.setattr(
        process_safety,
        "proc_environment",
        lambda _pid: {"TMUX_PANE": "%7", "TMUX": "/safe/tmux.sock,1,0"},
    )
    monkeypatch.setattr(
        process_safety,
        "_tmux_socket_from_environment",
        lambda _value: Path("/safe/tmux.sock"),
    )
    monkeypatch.setattr(
        process_safety,
        "inspect_tmux_pane",
        lambda _socket, _pane: TmuxPaneProcess("%7", "wrong", os.getpid()),
    )
    with pytest.raises(RuntimeFailureError) as failure:
        verify_wrapper_identity(_identity())
    assert failure.value.code is ErrorCode.AMBIGUOUS
