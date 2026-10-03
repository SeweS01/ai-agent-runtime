import os
from pathlib import Path

import pytest

from ai_agent_runtime import process_safety
from ai_agent_runtime.domain import WrapperIdentity
from ai_agent_runtime.errors import ErrorCode, RuntimeFailureError
from ai_agent_runtime.process_safety import (
    TmuxPaneProcess,
    verify_codex_child,
    verify_wrapper_identity,
)


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


def test_codex_child_evidence_checks_parent_environment_and_managed_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "codex-real"
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o700)
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(process_safety, "proc_uid", lambda _pid: os.getuid())
    monkeypatch.setattr(process_safety, "proc_starttime", lambda _pid: 99)
    monkeypatch.setattr(process_safety, "proc_parent_pid", lambda _pid: 10)
    monkeypatch.setattr(
        process_safety,
        "proc_environment",
        lambda _pid: {"CODEX_HOME": str(profile)},
    )
    monkeypatch.setattr(
        process_safety,
        "proc_cmdline",
        lambda _pid: (
            str(binary),
            "--model",
            "gpt-test",
            "-c",
            "model_reasoning_effort=high",
        ),
    )
    evidence = verify_codex_child(
        child_pid=20,
        child_starttime=99,
        wrapper_pid=10,
        codex_home=profile,
        model="gpt-test",
        reasoning_effort="high",
        real_codex=binary,
    )
    assert evidence["actual_model"] == "gpt-test"
    assert evidence["real_codex_inode"] == binary.stat().st_ino

    with pytest.raises(RuntimeFailureError) as mismatch:
        verify_codex_child(
            child_pid=20,
            child_starttime=99,
            wrapper_pid=11,
            codex_home=profile,
            model="gpt-test",
            reasoning_effort="high",
            real_codex=binary,
        )
    assert mismatch.value.code is ErrorCode.CORRELATION_MISMATCH
