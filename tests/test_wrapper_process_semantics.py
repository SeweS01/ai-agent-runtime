from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ai_agent_runtime.wrapper import main as wrapper_main


def _process_running(pid: int) -> bool:
    status = Path(f"/proc/{pid}/status")
    try:
        for line in status.read_text(encoding="utf-8").splitlines():
            if line.startswith("State:"):
                return "Z" not in line.split()
    except FileNotFoundError:
        return False
    return True


def test_child_setup_closes_prctl_parent_death_race(monkeypatch) -> None:
    killed: list[tuple[int, signal.Signals]] = []

    class FakeLibC:
        @staticmethod
        def prctl(*_args: object) -> int:
            return 0

    monkeypatch.setattr(wrapper_main.os, "setpgid", lambda *_args: None)
    monkeypatch.setattr(wrapper_main.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibC())
    monkeypatch.setattr(wrapper_main.os, "getppid", lambda: 999)
    monkeypatch.setattr(wrapper_main.os, "getpid", lambda: 1000)
    monkeypatch.setattr(wrapper_main.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    wrapper_main._child_setup(998)

    assert killed == [(1000, signal.SIGKILL)]


def test_console_entrypoint_version_is_local_and_does_not_parse_or_load_config(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(sys, "argv", ["codex-wrapper", "--version"])
    monkeypatch.setattr(
        wrapper_main.RuntimeConfig,
        "load",
        lambda *_args, **_kwargs: pytest.fail("--version must not load Runtime config"),
    )

    with pytest.raises(SystemExit) as stopped:
        wrapper_main.main()

    assert stopped.value.code == 0
    assert capsys.readouterr().out.startswith("codex-wrapper ")


def test_sigkill_wrapper_does_not_leave_running_child() -> None:
    script = """
import functools
import os
import subprocess
import sys
import time

from ai_agent_runtime.wrapper.main import _child_setup

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    preexec_fn=functools.partial(_child_setup, os.getpid()),
)
print(child.pid, flush=True)
time.sleep(60)
"""
    wrapper = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert wrapper.stdout is not None
    child_pid = int(wrapper.stdout.readline().strip())
    assert _process_running(child_pid)

    os.kill(wrapper.pid, signal.SIGKILL)
    wrapper.wait(timeout=5)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _process_running(child_pid):
        time.sleep(0.05)
    if _process_running(child_pid):
        os.kill(child_pid, signal.SIGKILL)
    assert not _process_running(child_pid)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_supervised_child_receives_tmux_keyboard_input(tmp_path: Path) -> None:
    socket_path = tmp_path / "tmux.sock"
    script = """
import functools
import os
import signal
import subprocess
import sys

from ai_agent_runtime.wrapper.main import _child_setup, _set_tty_foreground_group

child_code = "print('INPUT_READY', flush=True); print('RECEIVED:' + input(), flush=True)"
child = subprocess.Popen(
    [sys.executable, "-c", child_code],
    preexec_fn=functools.partial(_child_setup, os.getpid()),
)
_set_tty_foreground_group(child.pid)
os.killpg(child.pid, signal.SIGCONT)
child.wait(timeout=10)
_set_tty_foreground_group(os.getpgrp())
"""
    command = ["tmux", "-S", str(socket_path)]
    subprocess.run(
        [*command, "new-session", "-d", "-s", "wrapper-tty", "sleep 30"],
        check=True,
        capture_output=True,
    )
    try:
        subprocess.run(
            [*command, "set-window-option", "-t", "=wrapper-tty:0", "remain-on-exit", "on"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                *command,
                "respawn-pane",
                "-k",
                "-t",
                "=wrapper-tty:0",
                shlex.join([sys.executable, "-c", script]),
            ],
            check=True,
            capture_output=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pane = subprocess.run(
                [*command, "capture-pane", "-pt", "=wrapper-tty:"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            if "INPUT_READY" in pane:
                break
            time.sleep(0.05)
        else:
            pytest.fail("child never reached its input prompt")
        subprocess.run(
            [*command, "send-keys", "-t", "=wrapper-tty:", "hello", "Enter"],
            check=True,
            capture_output=True,
        )
        while time.monotonic() < deadline:
            pane = subprocess.run(
                [*command, "capture-pane", "-pt", "=wrapper-tty:"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            if "RECEIVED:hello" in pane:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"child did not receive tmux input: {pane!r}")
    finally:
        subprocess.run([*command, "kill-server"], check=False, capture_output=True)
