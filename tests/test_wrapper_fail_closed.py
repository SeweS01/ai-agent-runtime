import hashlib
import threading
from pathlib import Path

import pytest

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.wrapper import main as wrapper_main
from ai_agent_runtime.wrapper.main import _config_path, run


def test_instance_specific_executable_selects_namespaced_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AI_AGENT_RUNTIME_CONFIG", raising=False)
    assert _config_path("/safe/bin/codex-wrapper@ta_prod_02") == (
        Path.home() / ".config" / "ai-agent-runtime" / "instances" / "ta_prod_02.toml"
    )


def test_phase_b_response_deadline_exceeds_daemon_verification_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, float]] = []

    async def fake_request(
        _path: Path,
        payload: dict[str, object],
        *,
        response_timeout: float,
    ) -> dict[str, object]:
        observed.append((str(payload["action"]), response_timeout))
        return {"ok": True}

    monkeypatch.setattr(wrapper_main, "request", fake_request)
    wrapper_main._call_runtime(RuntimeConfig.defaults(), {"action": "register"})
    wrapper_main._call_runtime(RuntimeConfig.defaults(), {"action": "verify_started"})
    assert observed == [("register", 10.0), ("verify_started", 60.0)]


def test_agent_invocation_without_tmux_never_runs_real_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "codex-real"
    marker = tmp_path / "ran"
    real.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    real.chmod(0o700)
    real_digest = hashlib.sha256(real.read_bytes()).hexdigest()
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[runtime]
agent_instance_id = "agent-1"
forum_chat_id = -1001
state_dir = "{tmp_path / "state"}"
socket = "{tmp_path / "runtime.sock"}"

[telegram_agent]
topic_config = "{tmp_path / "topic.json"}"
tmux_state = "{tmp_path / "state.json"}"
upstream_sha = "0000000000000000000000000000000000000000"

[codex]
real_binary = "{real}"
version = "test-1"
sha256 = "sha256:{real_digest}"
profiles_root = "{tmp_path / "profiles"}"

[policy]
allowed_project_roots = ["{tmp_path}"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_AGENT_RUNTIME_CONFIG", str(config))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr(
        wrapper_main.os,
        "ttyname",
        lambda _fd: (_ for _ in ()).throw(OSError("not a tty")),
    )
    with pytest.raises(SystemExit) as failure:
        run([])
    assert failure.value.code == 70
    assert not marker.exists()


def test_tmux_identity_recovers_from_controlling_tty_after_env_sanitization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Completed:
        stdout = "%4\t/dev/pts/4\tother-session\n%12\t/dev/pts/12\tcc-n1001-42\n"

    calls: list[tuple[str, ...]] = []

    def fake_run(argv: tuple[str, ...], **_kwargs: object) -> Completed:
        calls.append(argv)
        return Completed()

    monkeypatch.setattr(wrapper_main.os, "ttyname", lambda _fd: "/dev/pts/12")
    monkeypatch.setattr(wrapper_main.subprocess, "run", fake_run)
    monkeypatch.setattr(
        wrapper_main,
        "_tmux_identity",
        lambda pane: "cc-n1001-42" if pane == "%12" else "unexpected",
    )

    assert wrapper_main._tmux_identity_from_controlling_tty() == (
        "%12",
        "cc-n1001-42",
    )
    assert calls == [
        (
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}\t#{pane_tty}\t#{session_name}",
        )
    ]


def test_tmux_identity_recovery_rejects_ambiguous_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Completed:
        stdout = "%1\t/dev/pts/9\tone\n%2\t/dev/pts/9\ttwo\n"

    monkeypatch.setattr(wrapper_main.os, "ttyname", lambda _fd: "/dev/pts/9")
    monkeypatch.setattr(wrapper_main.subprocess, "run", lambda *_args, **_kwargs: Completed())

    with pytest.raises(RuntimeError, match="maps to 2 tmux panes"):
        wrapper_main._tmux_identity_from_controlling_tty()


def test_version_is_answered_locally_without_running_real_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = tmp_path / "codex-real"
    marker = tmp_path / "ran"
    real.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    real.chmod(0o700)
    real_digest = hashlib.sha256(real.read_bytes()).hexdigest()
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[runtime]
agent_instance_id = "agent-1"
forum_chat_id = -1001

[telegram_agent]
topic_config = "{tmp_path / "topic.json"}"
tmux_state = "{tmp_path / "state.json"}"
upstream_sha = "0000000000000000000000000000000000000000"

[codex]
real_binary = "{real}"
version = "test-1"
sha256 = "sha256:{real_digest}"
profiles_root = "{tmp_path / "profiles"}"

[policy]
allowed_project_roots = ["{tmp_path}"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_AGENT_RUNTIME_CONFIG", str(config))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert run(["--version"]) == 0
    assert "codex-wrapper" in capsys.readouterr().out
    assert not marker.exists()


def test_supervisor_reports_child_exit_without_changing_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeChild:
        pid = 12345
        returncode = 7

        def poll(self) -> int:
            return 7

    monkeypatch.setattr(wrapper_main.subprocess, "Popen", lambda *_args, **_kwargs: FakeChild())
    monkeypatch.setattr(wrapper_main, "proc_starttime", lambda _pid: 10)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        wrapper_main, "_call_runtime", lambda _config, payload: calls.append(payload)
    )
    monkeypatch.setattr(
        wrapper_main, "_notify_runtime", lambda _config, payload: calls.append(payload)
    )
    config = RuntimeConfig.defaults()
    registration = {
        "instance_id": "instance-1",
        "binding_id": "binding-1",
        "tmux_pane": "%1",
        "tmux_session": "cc-n1001-42",
    }
    monkeypatch.chdir(tmp_path)
    assert (
        wrapper_main._run_supervised(
            "/bin/false",
            [],
            environment={},
            registration=registration,
            config=config,
        )
        == 7
    )
    assert [call["action"] for call in calls] == ["verify_started", "child_exited"]
    assert calls[1]["exit_code"] == 7


def test_heartbeat_reconnect_increments_generation_after_daemon_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()
    calls: list[dict[str, object]] = []

    def reconnect(_config: RuntimeConfig, payload: dict[str, object]) -> dict[str, object]:
        calls.append(payload)
        if len(calls) == 2:
            stop.set()
        return {"ok": True, "daemon_boot_id": "boot-2"}

    monkeypatch.setattr(wrapper_main, "_try_runtime_request", reconnect)
    monkeypatch.setattr(wrapper_main, "proc_starttime", lambda _pid: 10)
    monkeypatch.chdir(tmp_path)
    wrapper_main._heartbeat_worker(
        stop,
        config=RuntimeConfig.defaults(),
        registration={
            "instance_id": "instance-1",
            "binding_id": "binding-1",
            "tmux_pane": "%1",
            "tmux_session": "cc-n1001-42",
        },
        child_pid=12345,
        child_starttime=11,
        initial_daemon_boot_id="boot-1",
        interval=0.0,
    )
    assert len(calls) == 2
    assert calls[0]["connection_generation"] == 1
    assert calls[0]["previous_daemon_boot_id"] == "boot-1"
    assert calls[1]["connection_generation"] == 2
    assert calls[1]["previous_daemon_boot_id"] == "boot-2"
