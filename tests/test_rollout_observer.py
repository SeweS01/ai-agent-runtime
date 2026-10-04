import hashlib
import json
import time
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
        heartbeat_at=time.time(),
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


def test_observed_change_is_local_lkg_until_cus_policy_decision(tmp_path: Path) -> None:
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

    daemon._observe_codex_rollout(daemon.store.rollout_observation_targets()[0])

    launch = daemon.store.get_launch_spec("binding-1")
    assert launch is not None
    overlay, _home, _fingerprint = launch
    assert (overlay.model, overlay.reasoning_effort, overlay.revision) == (
        "gpt-new",
        "high",
        2,
    )
    assert overlay.source_kind == "observed_runtime_pending"
    report = daemon.store.status_report("agent-1")["bindings"][0]  # type: ignore[index]
    assert report["health"] == "OBSERVED_PENDING_SYNC"  # type: ignore[index]
    assert report["instances"][0]["applied"]["spec_digest"] == overlay.spec_digest  # type: ignore[index]
    assert daemon.store.retirement_candidates() == ()

    event = daemon.store.claim_outbox(now=1.0)[0]
    daemon.store.acknowledge_outbox(str(event["event_id"]), "POLICY_REJECTED")

    restored_launch = daemon.store.get_launch_spec("binding-1")
    assert restored_launch is not None
    assert restored_launch[0].model == "gpt-old"
    rejected = daemon.store.status_report("agent-1")["bindings"][0]  # type: ignore[index]
    assert rejected["health"] == "OBSERVED_POLICY_REJECTED"  # type: ignore[index]
    candidates = daemon.store.retirement_candidates()
    assert len(candidates) == 1
    assert candidates[0].applied_spec_digest == overlay.spec_digest


def test_accepted_observed_revision_becomes_applied_without_retirement(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.touch(mode=0o600)
    daemon = _daemon_with_applied_rollout(tmp_path, rollout=rollout)
    for record in (
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
        {
            "type": "turn_context",
            "payload": {"turn_id": "t1", "model": "gpt-new", "effort": "high"},
        },
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1"}},
    ):
        _append(rollout, record)
    daemon._observe_codex_rollout(daemon.store.rollout_observation_targets()[0])
    event = daemon.store.claim_outbox(now=1.0)[0]
    launch = daemon.store.get_launch_spec("binding-1")
    assert launch is not None
    overlay, codex_home, profile_fingerprint = launch
    accepted = DesiredSpec.build(
        binding_id=overlay.binding_id,
        agent_instance_id=overlay.agent_instance_id,
        project_id=overlay.project_id,
        canonical_cwd=overlay.canonical_cwd,
        provider=overlay.provider,
        account_profile=overlay.account_profile,
        model=overlay.model,
        reasoning_effort=overlay.reasoning_effort,
        generation_id=overlay.generation_id,
        revision=overlay.revision,
        source_kind="observed_runtime",
        source_ref=str(event["event_id"]),
    )
    assert accepted.spec_digest == overlay.spec_digest

    daemon.store.put_effective_spec(
        accepted,
        codex_home=codex_home,
        profile_fingerprint=profile_fingerprint,
    )
    daemon.store.acknowledge_outbox(str(event["event_id"]), "ACKED")

    report = daemon.store.status_report("agent-1")["bindings"][0]  # type: ignore[index]
    assert report["health"] == "RUNTIME_HEALTHY"  # type: ignore[index]
    assert daemon.store.retirement_candidates() == ()
    effective = daemon.store.get_effective_spec("binding-1")
    assert effective is not None
    assert effective[0].source_kind == "observed_runtime"
    assert effective[0].source_ref == event["event_id"]


def test_stale_observed_ack_restores_newer_cus_desired_and_safe_retirement(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.touch(mode=0o600)
    daemon = _daemon_with_applied_rollout(tmp_path, rollout=rollout)
    for record in (
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
        {
            "type": "turn_context",
            "payload": {"turn_id": "t1", "model": "gpt-observed", "effort": "high"},
        },
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1"}},
    ):
        _append(rollout, record)
    daemon._observe_codex_rollout(daemon.store.rollout_observation_targets()[0])
    event = daemon.store.claim_outbox(now=1.0)[0]

    effective = daemon.store.get_effective_spec("binding-1")
    assert effective is not None
    base, codex_home, profile_fingerprint = effective
    newer_cus = DesiredSpec.build(
        binding_id=base.binding_id,
        agent_instance_id=base.agent_instance_id,
        project_id=base.project_id,
        canonical_cwd=base.canonical_cwd,
        provider=base.provider,
        account_profile=base.account_profile,
        model="gpt-cus-newer",
        reasoning_effort="medium",
        generation_id=base.generation_id,
        revision=base.revision + 1,
        source_kind="cus_ui",
        source_ref="change-2",
    )
    daemon.store.put_effective_spec(
        newer_cus,
        codex_home=codex_home,
        profile_fingerprint=profile_fingerprint,
    )

    launch = daemon.store.get_launch_spec("binding-1")
    assert launch is not None
    assert launch[0].spec_digest == newer_cus.spec_digest
    assert daemon.store.retirement_candidates() == ()

    daemon.store.acknowledge_outbox(str(event["event_id"]), "STALE_OBSERVED")

    report = daemon.store.status_report("agent-1")["bindings"][0]  # type: ignore[index]
    assert report["health"] == "PENDING_CHANGE"  # type: ignore[index]
    candidates = daemon.store.retirement_candidates()
    assert len(candidates) == 1
    assert candidates[0].desired_spec_digest == newer_cus.spec_digest
    assert candidates[0].applied_spec_digest != newer_cus.spec_digest


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


def test_delayed_applied_context_cannot_overwrite_newer_desired_revision(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.touch(mode=0o600)
    daemon = _daemon_with_applied_rollout(tmp_path, rollout=rollout)
    project = tmp_path / "project"
    newer = DesiredSpec.build(
        binding_id="binding-1",
        agent_instance_id="agent-1",
        project_id="project-1",
        canonical_cwd=str(project),
        provider="codex",
        account_profile="work",
        model="gpt-cus-new",
        reasoning_effort="high",
        generation_id="gen-1",
        revision=2,
    )
    daemon.store.put_effective_spec(
        newer,
        codex_home=tmp_path / "profiles" / "work",
        profile_fingerprint="fp",
    )
    _append(
        rollout,
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
    )
    _append(
        rollout,
        {
            "type": "turn_context",
            "payload": {"turn_id": "t1", "model": "gpt-old", "effort": "medium"},
        },
    )
    _append(
        rollout,
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1"}},
    )

    daemon._observe_codex_rollout(daemon.store.rollout_observation_targets()[0])

    assert daemon.store.summary()["outbox_pending"] == 0
