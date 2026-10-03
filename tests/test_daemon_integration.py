import asyncio
import contextlib
import json
import os
import uuid
from pathlib import Path

import pytest

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.daemon.server import RuntimeServer
from ai_agent_runtime.domain import DesiredSpec, RuntimeBinding
from ai_agent_runtime.ipc.client import request
from ai_agent_runtime.ipc.protocol import versioned
from ai_agent_runtime.process_safety import proc_starttime


@pytest.mark.asyncio
async def test_real_unix_socket_phase_a_then_phase_b(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    profiles = tmp_path / "profiles"
    profile = profiles / "work"
    profile.mkdir(parents=True, mode=0o700)
    real_codex = tmp_path / "codex-real"
    real_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real_codex.chmod(0o700)
    topic_config = tmp_path / "topic_config.json"
    topic_config.write_text(
        json.dumps(
            {
                "topics": {
                    "42": {
                        "cwd": str(project),
                        "engine": "codex",
                        "exec_mode": "tmux",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    tmux_state = tmp_path / "tmux_state.json"
    config = RuntimeConfig(
        agent_instance_id="agent-1",
        forum_chat_id=-1001,
        state_dir=tmp_path / "runtime-state",
        socket_path=tmp_path / "run" / "runtime.sock",
        database_path=tmp_path / "runtime-state" / "runtime.sqlite",
        real_codex=real_codex,
        profiles_root=profiles,
        topic_config_path=topic_config,
        tmux_state_path=tmux_state,
        upstream_sha="0" * 40,
    )
    daemon = RuntimeServer(config, phase_b_timeout=1.0)
    binding = RuntimeBinding(
        binding_id="binding-1",
        agent_instance_id="agent-1",
        chat_id=-1001,
        thread_id=42,
        project_id="project-1",
        canonical_cwd=str(project),
        expected_tmux_session="cc-n1001-42",
    )
    daemon.store.upsert_binding(binding)
    spec = DesiredSpec.build(
        binding_id="binding-1",
        agent_instance_id="agent-1",
        project_id="project-1",
        canonical_cwd=str(project),
        provider="codex",
        account_profile="work",
        model="gpt-test",
        reasoning_effort="high",
        generation_id="gen-1",
        revision=1,
    )
    daemon.store.put_effective_spec(spec, codex_home=profile, profile_fingerprint="fp")

    server_task = asyncio.create_task(daemon.serve())
    for _ in range(100):
        if config.socket_path.exists():
            break
        await asyncio.sleep(0.01)
    assert config.socket_path.exists()
    pid = os.getpid()
    identity = {
        "wrapper": {"pid": pid, "starttime": proc_starttime(pid), "uid": os.getuid()},
        "cwd": str(project),
        "tmux_session": "cc-n1001-42",
        "tmux_pane": "%1",
    }
    registration = await request(
        config.socket_path,
        versioned(
            "register",
            str(uuid.uuid4()),
            agent_instance_id="agent-1",
            resume_session_id=None,
            **identity,
        ),
    )
    assert registration["ok"] is True
    assert registration["binding_id"] == "binding-1"
    assert registration["spec"]["spec_digest"] == spec.spec_digest

    tmux_state.write_text(
        json.dumps(
            {
                "-1001:42": {
                    "session_name": "cc-n1001-42",
                    "cwd": str(project),
                    "provider": "codex",
                    "session_id": "session-1",
                }
            }
        ),
        encoding="utf-8",
    )
    verified = await request(
        config.socket_path,
        versioned(
            "verify_started",
            str(uuid.uuid4()),
            agent_instance_id="agent-1",
            instance_id=registration["instance_id"],
            binding_id="binding-1",
            child_pid=pid,
            child_starttime=proc_starttime(pid),
            **identity,
        ),
    )
    assert verified["ok"] is True
    assert verified["session_id"] == "session-1"
    assert daemon.store.session_owner("session-1") == ("binding-1", "work")

    server_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await server_task
