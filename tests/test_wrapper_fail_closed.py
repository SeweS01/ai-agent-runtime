import hashlib
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
    with pytest.raises(SystemExit) as failure:
        run([])
    assert failure.value.code == 70
    assert not marker.exists()


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
