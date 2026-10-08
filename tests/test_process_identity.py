import os
from pathlib import Path

import pytest

from ai_agent_runtime import process_safety
from ai_agent_runtime.domain import WrapperIdentity
from ai_agent_runtime.errors import ErrorCode, RuntimeFailureError
from ai_agent_runtime.process_safety import (
    TmuxPaneProcess,
    process_executable_evidence,
    retire_tmux_session,
    verify_codex_child,
    verify_process_executable_evidence,
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


def test_wrapper_identity_accepts_sanitized_tmux_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_dir = tmp_path / f"tmux-{os.getuid()}"
    socket_dir.mkdir()
    socket_path = socket_dir / "default"
    socket_path.touch()
    tmp_path.chmod(0o700)
    socket_dir.chmod(0o700)
    monkeypatch.setattr(process_safety, "proc_uid", lambda _pid: os.getuid())
    monkeypatch.setattr(process_safety, "proc_starttime", lambda _pid: 123)
    monkeypatch.setattr(
        process_safety,
        "proc_environment",
        lambda _pid: {"TMUX_TMPDIR": str(tmp_path)},
    )
    monkeypatch.setattr(
        process_safety,
        "_validate_tmux_socket",
        lambda path: path,
    )
    observed: list[Path] = []

    def inspect(path: Path, _pane: str) -> TmuxPaneProcess:
        observed.append(path)
        return TmuxPaneProcess("%7", "cc-n1001-42", os.getpid())

    monkeypatch.setattr(process_safety, "inspect_tmux_pane", inspect)
    verify_wrapper_identity(_identity())
    assert observed == [socket_path]


@pytest.mark.usefixtures("allow_hosted_runner_python")
def test_process_executable_evidence_is_digest_bound() -> None:
    evidence = process_executable_evidence(os.getpid())
    assert evidence["inode"] > 0
    assert str(evidence["sha256"]).startswith("sha256:")
    verify_process_executable_evidence(os.getpid(), evidence, verify_digest=True)

    tampered = dict(evidence)
    tampered["inode"] = int(evidence["inode"]) + 1
    with pytest.raises(RuntimeFailureError) as mismatch:
        verify_process_executable_evidence(os.getpid(), tampered, verify_digest=False)
    assert mismatch.value.code is ErrorCode.CORRELATION_MISMATCH


def test_retirement_kills_exact_verified_tmux_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(process_safety, "verify_wrapper_identity", lambda _identity: None)
    monkeypatch.setattr(
        process_safety,
        "proc_environment",
        lambda _pid: {"TMUX": "/safe/tmux.sock,1,0"},
    )
    monkeypatch.setattr(
        process_safety,
        "_tmux_socket_from_environment",
        lambda _value: Path("/safe/tmux.sock"),
    )
    monkeypatch.setattr(
        process_safety,
        "inspect_tmux_pane",
        lambda _socket, _pane: TmuxPaneProcess("%7", "cc-n1001-42", os.getpid(), "$19"),
    )

    class Result:
        returncode = 0

    def fake_run(argv: tuple[str, ...], **_kwargs: object) -> Result:
        calls.append(argv)
        return Result()

    monkeypatch.setattr(process_safety.subprocess, "run", fake_run)
    retire_tmux_session(_identity())
    assert calls == [("tmux", "-S", "/safe/tmux.sock", "kill-session", "-t", "$19")]


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
