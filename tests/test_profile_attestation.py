from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.daemon.server import RuntimeServer
from ai_agent_runtime.domain import DesiredSpec, LifecycleState, RuntimeBinding
from ai_agent_runtime.errors import ErrorCode, RuntimeFailureError
from ai_agent_runtime.process_safety import profile_fingerprint


def _profile(tmp_path: Path) -> Path:
    profile = tmp_path / "profiles" / "work"
    profile.mkdir(parents=True, mode=0o700)
    auth = profile / "auth.json"
    auth.write_text('{"token":"first"}\n', encoding="utf-8")
    auth.chmod(0o600)
    return profile


def test_profile_fingerprint_survives_safe_auth_rotation(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    before = profile_fingerprint(profile)

    replacement = profile / "auth.next"
    replacement.write_text('{"token":"rotated-and-longer"}\n', encoding="utf-8")
    replacement.chmod(0o600)
    replacement.replace(profile / "auth.json")

    assert before.startswith("profile-v2:sha256:")
    assert profile_fingerprint(profile) == before


def test_profile_fingerprint_rejects_exposed_auth_file(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    (profile / "auth.json").chmod(0o640)

    with pytest.raises(RuntimeFailureError) as rejected:
        profile_fingerprint(profile)

    assert rejected.value.code is ErrorCode.UNSAFE_PROFILE


def test_profile_fingerprint_rejects_dangling_auth_symlink(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    auth = profile / "auth.json"
    auth.unlink()
    auth.symlink_to("missing-auth.json")

    with pytest.raises(RuntimeFailureError) as rejected:
        profile_fingerprint(profile)

    assert rejected.value.code is ErrorCode.UNSAFE_PROFILE


def test_live_legacy_instance_is_reattested_without_replacement(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    profile = _profile(tmp_path)
    real_codex = tmp_path / "codex-real"
    real_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real_codex.chmod(0o700)
    rollout = profile / "sessions" / "rollout.jsonl"
    rollout.parent.mkdir(mode=0o700)
    rollout.write_text("{}\n", encoding="utf-8")
    rollout.chmod(0o600)
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
    tmux_state = tmp_path / "tmux-state.json"
    tmux_state.write_text(
        json.dumps(
            {
                "-1001:42": {
                    "session_name": "cc-n1001-42",
                    "cwd": str(project),
                    "provider": "codex",
                    "session_id": "session-1",
                    "transcript_path": str(rollout),
                }
            }
        ),
        encoding="utf-8",
    )
    config = RuntimeConfig(
        agent_instance_id="agent-1",
        forum_chat_id=-1001,
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "run" / "runtime.sock",
        database_path=tmp_path / "state" / "runtime.sqlite",
        real_codex=real_codex,
        real_codex_version="test-1",
        real_codex_sha256=f"sha256:{hashlib.sha256(real_codex.read_bytes()).hexdigest()}",
        profiles_root=tmp_path / "profiles",
        topic_config_path=topic_config,
        tmux_state_path=tmux_state,
        upstream_sha="0" * 40,
        allowed_project_roots=(tmp_path,),
    )
    daemon = RuntimeServer(
        config,
        child_verifier=lambda **kwargs: {
            "actual_codex_home": str(kwargs["codex_home"]),
            "actual_model": kwargs["model"],
            "actual_reasoning_effort": kwargs["reasoning_effort"],
        },
    )
    binding = RuntimeBinding(
        binding_id="binding-1",
        agent_instance_id="agent-1",
        chat_id=-1001,
        thread_id=42,
        project_id="project-1",
        canonical_cwd=str(project),
        expected_tmux_session="cc-n1001-42",
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
        generation_id="generation-1",
        revision=1,
    )
    legacy_fingerprint = "sha256:" + "1" * 64
    daemon.store.upsert_binding(binding)
    daemon.store.put_effective_spec(
        spec,
        codex_home=profile,
        profile_fingerprint=legacy_fingerprint,
    )
    daemon.store.record_instance(
        instance_id="instance-1",
        binding_id=binding.binding_id,
        wrapper_pid=100,
        wrapper_starttime=200,
        child_pid=101,
        child_starttime=201,
        tmux_session=binding.expected_tmux_session,
        tmux_pane="%1",
        lifecycle=LifecycleState.IDLE,
        applied_digest=None,
        launch_spec={
            "desired": asdict(spec),
            "codex_home": str(profile),
            "profile_fingerprint": legacy_fingerprint,
            "real_codex": str(real_codex),
            "real_codex_version": config.real_codex_version,
            "real_codex_sha256": config.real_codex_sha256,
        },
        daemon_boot_id="boot-1",
        connection_generation=2,
        heartbeat_at=time.time(),
    )

    daemon._refresh_effective_profile_attestations_once()
    daemon._collect_applied_evidence_once()

    current_fingerprint = profile_fingerprint(profile)
    effective = daemon.store.get_effective_spec(binding.binding_id)
    assert effective is not None
    assert effective[2] == current_fingerprint
    registered = daemon.store.get_registered_instance("instance-1")
    assert registered is not None
    assert registered.launch_spec["profile_fingerprint"] == current_fingerprint
    evidence = daemon.store.get_applied_evidence("instance-1")
    assert evidence is not None
    assert evidence["profile_fingerprint"] == current_fingerprint
    assert evidence["session_id"] == "session-1"
    status = daemon.store.status_report("agent-1")
    bindings = cast(list[dict[str, Any]], status["bindings"])
    assert bindings[0]["health"] == "RUNTIME_HEALTHY"
    daemon.store.close()
