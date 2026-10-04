"""Transparent Codex wrapper used as TELEGRAM_CODEX_BIN."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import functools
import os
import re
import signal
import subprocess
import sys
import threading
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
_HEARTBEAT_INTERVAL_SECONDS = 2.0
_HEARTBEAT_REQUEST_TIMEOUT_SECONDS = 2.0
_PHASE_B_RESPONSE_TIMEOUT_SECONDS = 60.0
_INSTANCE_EXECUTABLE = re.compile(r"^codex-wrapper@([A-Za-z0-9][A-Za-z0-9_.-]{0,63})$")
_EXIT_BY_CODE = {
    ErrorCode.UNSUPPORTED_CODEX_SUBPROCESS.value: 70,
    ErrorCode.RESUME_BINDING_MISMATCH.value: 71,
    ErrorCode.UNKNOWN_BINDING.value: 72,
    ErrorCode.AMBIGUOUS.value: 73,
    ErrorCode.CONFIG_DRIFT.value: 74,
    ErrorCode.CORRELATION_MISMATCH.value: 75,
    ErrorCode.RUNTIME_UNAVAILABLE.value: 76,
}


def _config_path(executable: str | None = None) -> Path:
    configured = os.environ.get("AI_AGENT_RUNTIME_CONFIG")
    if configured:
        return Path(configured).expanduser()
    executable_name = Path(executable or sys.argv[0]).name
    matched = _INSTANCE_EXECUTABLE.fullmatch(executable_name)
    if matched:
        return (
            Path.home() / ".config" / "ai-agent-runtime" / "instances" / f"{matched.group(1)}.toml"
        )
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


def _tmux_identity_from_controlling_tty() -> tuple[str, str]:
    """Recover the exact tmux pane when a sanitized launcher removed TMUX_PANE.

    Pinned Telegram Agent starts the configured Codex binary through ``env -i``.
    tmux sets ``TMUX_PANE`` for its child, but that intermediate command removes
    the variable before execing the wrapper.  The controlling PTY survives the
    sanitization and tmux exposes the same PTY for every pane, so require one
    exact match and independently verify its session before accepting it.
    """

    controlling_tty = os.path.realpath(os.ttyname(0))
    completed = subprocess.run(
        ("tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{pane_tty}\t#{session_name}"),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    matches: list[tuple[str, str]] = []
    for raw_line in completed.stdout.splitlines():
        fields = raw_line.split("\t")
        if len(fields) != 3:
            continue
        pane, pane_tty, session = fields
        if os.path.realpath(pane_tty) == controlling_tty:
            matches.append((pane, session))
    if len(matches) != 1:
        raise RuntimeError(
            f"controlling TTY maps to {len(matches)} tmux panes; expected exactly one"
        )
    pane, session = matches[0]
    if not pane or not session or _tmux_identity(pane) != session:
        raise RuntimeError("tmux pane/session verification failed")
    return pane, session


def _child_setup(expected_parent_pid: int) -> None:
    # A new process group permits bounded group termination while retaining the
    # pane's controlling terminal, which Codex TUI requires.
    os.setpgid(0, 0)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    # PR_SET_PDEATHSIG is not retroactive. If the wrapper died after fork but
    # before prctl, the kernel would otherwise leave the child alive. Compare
    # against the PID captured by the parent before Popen and fail immediately
    # when reparenting already happened.
    if os.getppid() != expected_parent_pid:
        os.kill(os.getpid(), signal.SIGKILL)


def _call_runtime(config: RuntimeConfig, payload: dict[str, Any]) -> dict[str, Any]:
    response_timeout = (
        _PHASE_B_RESPONSE_TIMEOUT_SECONDS if payload.get("action") == "verify_started" else 10.0
    )
    try:
        response = asyncio.run(
            request(
                config.socket_path,
                payload,
                response_timeout=response_timeout,
            )
        )
    except (TimeoutError, OSError, EOFError, ValueError) as exc:
        _fail(ErrorCode.RUNTIME_UNAVAILABLE.value, str(exc))
    if not response.get("ok"):
        error = response.get("error")
        if isinstance(error, dict):
            _fail(str(error.get("code", "RUNTIME_ERROR")), str(error.get("detail", "")))
        _fail("RUNTIME_ERROR", "Runtime rejected launch")
    return response


def _notify_runtime(config: RuntimeConfig, payload: dict[str, Any]) -> None:
    """Best-effort terminal notification; child exit status remains authoritative."""

    async def notify() -> None:
        await asyncio.wait_for(request(config.socket_path, payload), timeout=2.0)

    with contextlib.suppress(TimeoutError, OSError, EOFError, ValueError):
        asyncio.run(notify())


def _try_runtime_request(
    config: RuntimeConfig,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a best-effort Runtime response without affecting a live child."""

    async def notify() -> dict[str, Any]:
        return await asyncio.wait_for(
            request(config.socket_path, payload),
            timeout=_HEARTBEAT_REQUEST_TIMEOUT_SECONDS,
        )

    try:
        response = asyncio.run(notify())
    except (TimeoutError, OSError, EOFError, ValueError):
        return None
    return response if response.get("ok") is True else None


def _heartbeat_worker(
    stop: threading.Event,
    *,
    config: RuntimeConfig,
    registration: dict[str, Any],
    child_pid: int,
    child_starttime: int,
    initial_daemon_boot_id: str | None,
    interval: float = _HEARTBEAT_INTERVAL_SECONDS,
) -> None:
    """Reconnect and resync exact live identity after daemon outages/restarts."""

    previous_boot_id = initial_daemon_boot_id
    connection_generation = 1
    while not stop.wait(interval):
        response = _try_runtime_request(
            config,
            versioned(
                "heartbeat",
                str(uuid.uuid4()),
                agent_instance_id=config.agent_instance_id,
                instance_id=registration["instance_id"],
                binding_id=registration["binding_id"],
                child_pid=child_pid,
                child_starttime=child_starttime,
                connection_generation=connection_generation,
                previous_daemon_boot_id=previous_boot_id,
                **_identity_payload(
                    pane=str(registration["tmux_pane"]),
                    session=str(registration["tmux_session"]),
                ),
            ),
        )
        if response is None:
            continue
        observed_boot_id = response.get("daemon_boot_id")
        if isinstance(observed_boot_id, str) and observed_boot_id:
            if previous_boot_id is not None and observed_boot_id != previous_boot_id:
                connection_generation += 1
            previous_boot_id = observed_boot_id


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
    wrapper_pid = os.getpid()
    child = subprocess.Popen(
        [binary, *argv],
        env=environment,
        stdin=None,
        stdout=None,
        stderr=None,
        preexec_fn=functools.partial(_child_setup, wrapper_pid),
    )
    child_starttime = proc_starttime(child.pid)
    terminate_requested = False
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None

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
            child_starttime=child_starttime,
            **_identity_payload(
                pane=str(registration["tmux_pane"]),
                session=str(registration["tmux_session"]),
            ),
        )
        try:
            verification_response = _call_runtime(config, verification)
        except SystemExit:
            _terminate_group(child)
            raise
        raw_boot_id = (
            verification_response.get("daemon_boot_id")
            if isinstance(verification_response, dict)
            else None
        )
        initial_boot_id = raw_boot_id if isinstance(raw_boot_id, str) else None
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=_heartbeat_worker,
            kwargs={
                "stop": heartbeat_stop,
                "config": config,
                "registration": registration,
                "child_pid": child.pid,
                "child_starttime": child_starttime,
                "initial_daemon_boot_id": initial_boot_id,
            },
            name=f"codex-wrapper-heartbeat-{child.pid}",
            daemon=True,
        )
        heartbeat_thread.start()
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
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=_HEARTBEAT_REQUEST_TIMEOUT_SECONDS + 0.5)
        if child.poll() is not None:
            _notify_runtime(
                config,
                versioned(
                    "child_exited",
                    str(uuid.uuid4()),
                    agent_instance_id=config.agent_instance_id,
                    instance_id=registration["instance_id"],
                    binding_id=registration["binding_id"],
                    child_pid=child.pid,
                    child_starttime=child_starttime,
                    exit_code=int(child.returncode or 0),
                    **_identity_payload(
                        pane=str(registration["tmux_pane"]),
                        session=str(registration["tmux_session"]),
                    ),
                ),
            )
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def run(argv: list[str]) -> int:
    invocation = parse_invocation(argv)
    if invocation.kind is InvocationKind.DENIED_ADMIN:
        _fail("DENIED_ADMIN", "credential and update commands are not exposed by wrapper")
    if invocation.kind is InvocationKind.SAFE_LOCAL:
        if argv[0] in {"--version", "-V"}:
            print(f"codex-wrapper {__version__}")
        else:
            print("codex-wrapper: managed Codex launcher; agent sessions require tmux")
        return 0
    config = RuntimeConfig.load(_config_path())
    config.validate()

    pane = os.environ.get("TMUX_PANE")
    if pane:
        try:
            session = _tmux_identity(pane)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            _fail(ErrorCode.UNKNOWN_BINDING.value, f"cannot prove tmux identity: {exc}")
    else:
        try:
            pane, session = _tmux_identity_from_controlling_tty()
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            _fail(
                ErrorCode.UNSUPPORTED_CODEX_SUBPROCESS.value,
                f"managed Codex requires a proven Telegram Agent tmux pane: {exc}",
            )
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
    # The wrapper must preserve the real Codex argv exactly. argparse would
    # consume/reject flags such as ``--version`` before the formal Codex parser
    # sees them, violating TELEGRAM_CODEX_BIN compatibility.
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
