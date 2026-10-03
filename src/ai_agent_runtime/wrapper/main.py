"""Transparent Codex wrapper used as TELEGRAM_CODEX_BIN."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, NoReturn

from ai_agent_runtime import __version__
from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.errors import ErrorCode
from ai_agent_runtime.ipc.client import request
from ai_agent_runtime.ipc.protocol import versioned
from ai_agent_runtime.process_safety import proc_starttime
from ai_agent_runtime.providers.codex.argv import (
    InvocationKind,
    parse_invocation,
    rewrite_managed_args,
)

_PR_SET_PDEATHSIG = 1
_EXIT_BY_CODE = {
    ErrorCode.UNSUPPORTED_CODEX_SUBPROCESS.value: 70,
    ErrorCode.RESUME_BINDING_MISMATCH.value: 71,
    ErrorCode.UNKNOWN_BINDING.value: 72,
    ErrorCode.AMBIGUOUS.value: 73,
    ErrorCode.CONFIG_DRIFT.value: 74,
    ErrorCode.CORRELATION_MISMATCH.value: 75,
    ErrorCode.RUNTIME_UNAVAILABLE.value: 76,
}


def _config_path() -> Path:
    configured = os.environ.get("AI_AGENT_RUNTIME_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "ai-agent-runtime" / "config.toml"


def _tmux_identity(pane: str) -> str:
    completed = subprocess.run(
        ("tmux", "display-message", "-p", "-t", pane, "#{session_name}"),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    session = completed.stdout.strip()
    if not session:
        raise RuntimeError("tmux returned an empty session name")
    return session


def _child_setup() -> None:
    # A new process group permits bounded group termination while retaining the
    # pane's controlling terminal, which Codex TUI requires.
    os.setpgid(0, 0)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _call_runtime(config: RuntimeConfig, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = asyncio.run(request(config.socket_path, payload))
    except (TimeoutError, OSError, EOFError, ValueError) as exc:
        _fail(ErrorCode.RUNTIME_UNAVAILABLE.value, str(exc))
    if not response.get("ok"):
        error = response.get("error")
        if isinstance(error, dict):
            _fail(str(error.get("code", "RUNTIME_ERROR")), str(error.get("detail", "")))
        _fail("RUNTIME_ERROR", "Runtime rejected launch")
    return response


def _fail(code: str, detail: str) -> NoReturn:
    print(f"codex-wrapper: {code}: {detail}", file=sys.stderr)
    raise SystemExit(_EXIT_BY_CODE.get(code, 78))


def _identity_payload(*, pane: str, session: str) -> dict[str, Any]:
    pid = os.getpid()
    return {
        "wrapper": {"pid": pid, "starttime": proc_starttime(pid), "uid": os.getuid()},
        "cwd": str(Path.cwd().resolve(strict=True)),
        "tmux_session": session,
        "tmux_pane": pane,
    }


def _terminate_group(child: subprocess.Popen[bytes], *, timeout: float = 10.0) -> None:
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def _run_supervised(
    binary: str,
    argv: list[str],
    *,
    environment: dict[str, str],
    registration: dict[str, Any],
    config: RuntimeConfig,
) -> int:
    child = subprocess.Popen(
        [binary, *argv],
        env=environment,
        stdin=None,
        stdout=None,
        stderr=None,
        preexec_fn=_child_setup,
    )
    terminate_requested = False

    def handle_termination(_signum: int, _frame: object) -> None:
        nonlocal terminate_requested
        terminate_requested = True
        if child.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)

    old_term = signal.signal(signal.SIGTERM, handle_termination)
    old_int = signal.signal(signal.SIGINT, handle_termination)
    try:
        verification = versioned(
            "verify_started",
            str(uuid.uuid4()),
            agent_instance_id=config.agent_instance_id,
            instance_id=registration["instance_id"],
            binding_id=registration["binding_id"],
            child_pid=child.pid,
            child_starttime=proc_starttime(child.pid),
            **_identity_payload(
                pane=str(registration["tmux_pane"]),
                session=str(registration["tmux_session"]),
            ),
        )
        try:
            _call_runtime(config, verification)
        except SystemExit:
            _terminate_group(child)
            raise
        if terminate_requested:
            _terminate_group(child)
            return 128 + signal.SIGTERM
        while child.poll() is None:
            if terminate_requested:
                _terminate_group(child)
                break
            time.sleep(0.1)
        return int(child.returncode or 0)
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def run(argv: list[str]) -> int:
    invocation = parse_invocation(argv)
    config = RuntimeConfig.load(_config_path())
    config.validate()
    if invocation.kind is InvocationKind.DENIED_ADMIN:
        _fail("DENIED_ADMIN", "credential and update commands are not exposed by wrapper")
    if invocation.kind is InvocationKind.SAFE_LOCAL:
        if argv[0] in {"--version", "-V"}:
            print(f"codex-wrapper {__version__}")
        else:
            print("codex-wrapper: managed Codex launcher; agent sessions require tmux")
        return 0

    pane = os.environ.get("TMUX_PANE")
    if not pane:
        _fail(
            ErrorCode.UNSUPPORTED_CODEX_SUBPROCESS.value,
            "managed Codex requires a Telegram Agent tmux pane in Runtime V1",
        )
    try:
        session = _tmux_identity(pane)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        _fail(ErrorCode.UNKNOWN_BINDING.value, f"cannot prove tmux identity: {exc}")
    identity = _identity_payload(pane=pane, session=session)
    register = versioned(
        "register",
        str(uuid.uuid4()),
        agent_instance_id=config.agent_instance_id,
        resume_session_id=invocation.resume_session_id,
        **identity,
    )
    response = _call_runtime(config, register)
    spec = response.get("spec")
    if not isinstance(spec, dict):
        _fail("INVALID_RESPONSE", "Runtime response has no launch spec")
    managed_argv = rewrite_managed_args(
        argv,
        model=str(spec["model"]),
        reasoning_effort=str(spec["reasoning_effort"]),
    )
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(spec["codex_home"])
    response["tmux_pane"] = pane
    response["tmux_session"] = session
    return _run_supervised(
        str(spec["real_codex"]),
        managed_argv,
        environment=environment,
        registration=response,
        config=config,
    )


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    namespace = parser.parse_args()
    raise SystemExit(run(namespace.args))


if __name__ == "__main__":
    main()
