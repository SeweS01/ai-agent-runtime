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
from ai_agent_runtime.telegram_upstream.topic_config import read_topic_config


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
        allowed_project_roots=(tmp_path,),
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
        config_digest=read_topic_config(topic_config).digest,
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


@pytest.mark.asyncio
async def test_apply_binding_is_durable_idempotent_and_never_uses_default_codex(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    profiles = tmp_path / "profiles"
    profile = profiles / "work"
    profile.mkdir(parents=True, mode=0o700)
    (profile / "auth.json").write_text("{}", encoding="utf-8")
    (profile / "auth.json").chmod(0o600)
    real_codex = tmp_path / "codex-real"
    real_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real_codex.chmod(0o700)
    topic_config = tmp_path / "topic_config.json"
    topic_config.write_text(
        json.dumps(
            {
                "topics": {
                    "42": {
                        "cwd": str(tmp_path),
                        "engine": "claude",
                        "exec_mode": "subprocess",
                        "unknown": "preserve-me",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = RuntimeConfig(
        agent_instance_id="agent-1",
        forum_chat_id=-1001,
        state_dir=tmp_path / "runtime-state",
        socket_path=tmp_path / "run" / "runtime.sock",
        database_path=tmp_path / "runtime-state" / "runtime.sqlite",
        real_codex=real_codex,
        profiles_root=profiles,
        topic_config_path=topic_config,
        tmux_state_path=tmp_path / "tmux_state.json",
        upstream_sha="0" * 40,
        allowed_project_roots=(tmp_path,),
    )
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
    desired_payload = {
        "binding_id": spec.binding_id,
        "agent_instance_id": spec.agent_instance_id,
        "project_id": spec.project_id,
        "canonical_cwd": spec.canonical_cwd,
        "provider": spec.provider,
        "account_profile": spec.account_profile,
        "model": spec.model,
        "reasoning_effort": spec.reasoning_effort,
        "generation_id": spec.generation_id,
        "revision": spec.revision,
        "spec_digest": spec.spec_digest,
    }
    request_id = str(uuid.uuid4())
    payload = versioned(
        "apply_binding",
        request_id,
        binding={
            "binding_id": "binding-1",
            "agent_instance_id": "agent-1",
            "chat_id": -1001,
            "thread_id": 42,
            "project_id": "project-1",
            "canonical_cwd": str(project),
        },
        desired=desired_payload,
    )

    daemon = RuntimeServer(config)
    server_task = asyncio.create_task(daemon.serve())
    for _ in range(100):
        if config.socket_path.exists():
            break
        await asyncio.sleep(0.01)
    applied = await request(config.socket_path, payload)
    assert applied["ok"] is True
    topic = read_topic_config(topic_config).topic(42)
    assert topic is not None
    assert topic.cwd == str(project)
    assert topic.is_managed_candidate
    assert topic.raw["unknown"] == "preserve-me"
    backups = list((config.state_dir / "topic-config-backups").iterdir())
    assert len(backups) == 1

    before_stale = read_topic_config(topic_config).digest
    stale_spec = DesiredSpec.build(
        binding_id="binding-1",
        agent_instance_id="agent-1",
        project_id="project-1",
        canonical_cwd=str(project),
        provider="codex",
        account_profile="work",
        model="stale-model",
        reasoning_effort="low",
        generation_id="gen-1",
        revision=0,
    )
    stale_payload = versioned(
        "apply_binding",
        str(uuid.uuid4()),
        binding=payload["binding"],
        desired={
            "binding_id": stale_spec.binding_id,
            "agent_instance_id": stale_spec.agent_instance_id,
            "project_id": stale_spec.project_id,
            "canonical_cwd": stale_spec.canonical_cwd,
            "provider": stale_spec.provider,
            "account_profile": stale_spec.account_profile,
            "model": stale_spec.model,
            "reasoning_effort": stale_spec.reasoning_effort,
            "generation_id": stale_spec.generation_id,
            "revision": stale_spec.revision,
            "spec_digest": stale_spec.spec_digest,
        },
    )
    stale = await request(config.socket_path, stale_payload)
    assert stale["ok"] is False
    assert read_topic_config(topic_config).digest == before_stale
    assert len(list((config.state_dir / "topic-config-backups").iterdir())) == 1

    server_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await server_task

    restarted = RuntimeServer(config)
    restarted_task = asyncio.create_task(restarted.serve())
    for _ in range(100):
        if config.socket_path.exists():
            break
        await asyncio.sleep(0.01)
    replayed = await request(config.socket_path, payload)
    assert replayed["ok"] is True
    assert replayed["local_effective"] == applied["local_effective"]
    assert len(list((config.state_dir / "topic-config-backups").iterdir())) == 1

    conflicting = dict(payload)
    conflicting["desired"] = {**desired_payload, "model": "different"}
    rejected = await request(config.socket_path, conflicting)
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "INVALID_REQUEST"
    restarted_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await restarted_task
