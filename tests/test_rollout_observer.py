import hashlib
import json
from pathlib import Path

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.daemon.server import RuntimeServer
from ai_agent_runtime.domain import DesiredSpec, RuntimeBinding


def _append(path: Path, record: object) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")


def _daemon_with_applied_rollout(
    tmp_path: Path,
    *,
    rollout: Path,
) -> RuntimeServer:
    project = tmp_path / "project"
    project.mkdir()
    profile = tmp_path / "profiles" / "work"
    profile.mkdir(parents=True)
    real_codex = tmp_path / "codex-real"
    real_codex.write_text("#!/bin/sh\n", encoding="utf-8")
    real_codex.chmod(0o700)
    topic_config = tmp_path / "topic.json"
    topic_config.write_text('{"topics": {}}', encoding="utf-8")
    tmux_state = tmp_path / "state.json"
    tmux_state.write_text("{}", encoding="utf-8")
    config = RuntimeConfig(
        agent_instance_id="agent-1",
        forum_chat_id=-1001,
        state_dir=tmp_path / "runtime",
        socket_path=tmp_path / "run" / "runtime.sock",
        database_path=tmp_path / "runtime" / "runtime.sqlite",
        real_codex=real_codex,
        real_codex_version="codex-cli 0.156.1",
        real_codex_sha256=f"sha256:{hashlib.sha256(real_codex.read_bytes()).hexdigest()}",
        profiles_root=profile.parent,
        topic_config_path=topic_config,
        tmux_state_path=tmux_state,
        upstream_sha="0" * 40,
        allowed_project_roots=(tmp_path,),
    )
    daemon = RuntimeServer(config)
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
        model="gpt-old",
        reasoning_effort="medium",
        generation_id="gen-1",
        revision=1,
    )
    daemon.store.upsert_binding(binding)
    daemon.store.put_effective_spec(spec, codex_home=profile, profile_fingerprint="fp")
    daemon.store.record_instance(
        instance_id="instance-1",
        binding_id="binding-1",
        wrapper_pid=100,
        wrapper_starttime=10,
        child_pid=101,
        child_starttime=11,
        tmux_session="cc-n1001-42",
        tmux_pane="%1",
        lifecycle="IDLE",
        applied_digest=None,
    )
    rollout_stat = rollout.stat()
    daemon.store.record_applied_spec(
        instance_id="instance-1",
        binding_id="binding-1",
        spec=spec,
        evidence={
            "rollout_path": str(rollout),
            "rollout_device": rollout_stat.st_dev,
            "rollout_inode": rollout_stat.st_ino,
            "session_id": "session-1",
            "real_codex_version": "codex-cli 0.156.1",
        },
        session_id="session-1",
        account_profile="work",
        rollout_path=str(rollout),
        rollout_device=rollout_stat.st_dev,
        rollout_inode=rollout_stat.st_ino,
    )
    return daemon


def test_structured_turn_context_creates_one_durable_observed_proposal(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.touch(mode=0o600)
    daemon = _daemon_with_applied_rollout(tmp_path, rollout=rollout)
    _append(
        rollout,
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
    )
    _append(
        rollout,
        {
            "type": "turn_context",
            "payload": {"turn_id": "t1", "model": "gpt-new", "effort": "high"},
        },
    )
    _append(
        rollout,
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1"}},
    )
    target = daemon.store.rollout_observation_targets()[0]
    daemon._observe_codex_rollout(target)
    events = daemon.store.claim_outbox(now=1.0)
    assert len(events) == 1
    assert events[0]["kind"] == "OBSERVED_SPEC_PROPOSAL"
    assert events[0]["payload"]["model"] == "gpt-new"  # type: ignore[index]
    assert events[0]["payload"]["reasoning_effort"] == "high"  # type: ignore[index]
    assert events[0]["payload"]["base_revision"] == 1  # type: ignore[index]
    assert daemon.store.rollout_observation_targets()[0].lifecycle == "IDLE"

    daemon._observe_codex_rollout(daemon.store.rollout_observation_targets()[0])
    assert daemon.store.summary()["outbox_pending"] == 1


def test_historical_change_reverted_before_catchup_does_not_emit(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.touch(mode=0o600)
    daemon = _daemon_with_applied_rollout(tmp_path, rollout=rollout)
    for turn_id, model, effort in (
        ("t1", "gpt-new", "high"),
        ("t2", "gpt-old", "medium"),
    ):
        _append(
            rollout,
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": turn_id},
            },
        )
        _append(
            rollout,
            {
                "type": "turn_context",
                "payload": {"turn_id": turn_id, "model": model, "effort": effort},
            },
        )
        _append(
            rollout,
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": turn_id},
            },
        )
    daemon._observe_codex_rollout(daemon.store.rollout_observation_targets()[0])
    assert daemon.store.summary()["outbox_pending"] == 0


def test_incomplete_turn_is_busy_and_unstructured_text_is_not_evidence(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.touch(mode=0o600)
    daemon = _daemon_with_applied_rollout(tmp_path, rollout=rollout)
    _append(
        rollout,
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
    )
    _append(
        rollout,
        {
            "type": "response_item",
            "payload": {"type": "message", "content": "/model gpt-new"},
        },
    )
    daemon._observe_codex_rollout(daemon.store.rollout_observation_targets()[0])
    assert daemon.store.rollout_observation_targets()[0].lifecycle == "BUSY"
    assert daemon.store.summary()["outbox_pending"] == 0
