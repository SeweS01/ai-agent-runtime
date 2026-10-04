from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.daemon.server import RuntimeServer
from ai_agent_runtime.domain import DesiredSpec, RuntimeBinding
from ai_agent_runtime.errors import ErrorCode, RuntimeFailureError
from ai_agent_runtime.ipc.client import request
from ai_agent_runtime.ipc.protocol import versioned
from ai_agent_runtime.migration.binding_plan import build_binding_migration_plan
from ai_agent_runtime.migration.inventory import INVENTORY_SCHEMA, InventoryError, _digest
from ai_agent_runtime.migration.session_provenance import (
    build_session_provenance_plan,
    inspect_rollout_session,
    validate_session_provenance_plan,
)
from ai_agent_runtime.state.store import RuntimeStore


def _sources(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], Path, Path]:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "session-42", "cwd": str(project)},
            }
        )
        + "\n"
        + json.dumps({"type": "response_item", "payload": {"redacted": True}})
        + "\n",
        encoding="utf-8",
    )
    rollout.chmod(0o600)
    topic = {
        "source_ref": "agent-1/-1001/42",
        "agent_instance_id": "agent-1",
        "chat_id": -1001,
        "thread_id": 42,
        "name": "Existing topic",
        "canonical_cwd": str(project),
        "engine": "codex",
        "exec_mode": "tmux",
        "issues": [],
        "live_tmux_panes": [{"pane_pid": 1234}],
        "live_codex_process": {
            "verified": True,
            "pid": 1234,
            "starttime": 5678,
            "uid": os.getuid(),
            "codex_home": str(tmp_path / "profiles" / "work"),
            "account_profile": "work",
            "binary_sha256": "sha256:" + "1" * 64,
            "executable": {
                "path": "/safe/codex-real",
                "device": 1,
                "inode": 2,
                "size": 3,
                "mtime_ns": 4,
                "sha256": "sha256:" + "1" * 64,
            },
        },
        "cus_bindings": [
            {
                "binding_id": "binding-42",
                "project_id": "project-1",
                "canonical_cwd": str(project),
                "lifecycle_status": "CONFIGURED",
                "desired": {
                    "provider": "codex",
                    "account_profile": "work",
                    "model": "gpt-test",
                    "reasoning_effort": "high",
                },
            }
        ],
        "tmux_state": [
            {
                "session_name": "cc-n1001-42",
                "canonical_cwd": str(project),
                "provider": "codex",
                "session_id": "session-42",
                "transcript_path": str(rollout),
            }
        ],
    }
    inventory: dict[str, object] = {
        "schema": INVENTORY_SCHEMA,
        "agent_instance": {"agent_instance_id": "agent-1"},
        "topics": [topic],
    }
    inventory["inventory_digest"] = _digest(inventory)
    binding_plan = build_binding_migration_plan(inventory, generation_id="migration-1")
    return inventory, binding_plan, project, rollout


def _spec(project: Path) -> DesiredSpec:
    return DesiredSpec.build(
        binding_id="binding-42",
        agent_instance_id="agent-1",
        project_id="project-1",
        canonical_cwd=str(project),
        provider="codex",
        account_profile="work",
        model="gpt-test",
        reasoning_effort="high",
        generation_id="migration-1",
        revision=1,
        source_kind="migration",
        source_ref="agent-1/-1001/42",
    )


def _seed_store(store: RuntimeStore, project: Path, profile: Path) -> None:
    store.upsert_binding(
        RuntimeBinding(
            binding_id="binding-42",
            agent_instance_id="agent-1",
            chat_id=-1001,
            thread_id=42,
            project_id="project-1",
            canonical_cwd=str(project),
            expected_tmux_session="cc-n1001-42",
        )
    )
    store.put_effective_spec(_spec(project), codex_home=profile, profile_fingerprint="fp")


def test_plan_proves_exact_legacy_session_without_reading_transcript_body(
    tmp_path: Path,
) -> None:
    inventory, binding_plan, project, rollout = _sources(tmp_path)

    plan = build_session_provenance_plan(inventory, binding_plan)

    assert plan["accepted"] is True
    assert plan["record_count"] == 1
    records = plan["records"]
    assert isinstance(records, list)
    assert records == [
        {
            "agent_instance_id": "agent-1",
            "source_ref": "agent-1/-1001/42",
            "binding_id": "binding-42",
            "account_profile": "work",
            "canonical_cwd": str(project),
            "session_id": "session-42",
            "live_process": {
                "pid": 1234,
                "starttime": 5678,
                "uid": os.getuid(),
                "codex_home": str(tmp_path / "profiles" / "work"),
                "account_profile": "work",
                "executable": {
                    "path": "/safe/codex-real",
                    "device": 1,
                    "inode": 2,
                    "size": 3,
                    "mtime_ns": 4,
                    "sha256": "sha256:" + "1" * 64,
                },
            },
            "rollout_path": str(rollout),
            "rollout_device": rollout.stat().st_dev,
            "rollout_inode": rollout.stat().st_ino,
            "rollout_size": rollout.stat().st_size,
        }
    ]
    assert validate_session_provenance_plan(plan) == plan


def test_rollout_inspection_handles_short_os_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inventory, _binding_plan, project, rollout = _sources(tmp_path)
    real_read = os.read

    def short_read(descriptor: int, size: int) -> bytes:
        return real_read(descriptor, min(size, 7))

    monkeypatch.setattr("ai_agent_runtime.migration.session_provenance.os.read", short_read)

    observed = inspect_rollout_session(
        rollout,
        expected_session_id="session-42",
        expected_cwd=str(project),
    )

    assert observed["rollout_inode"] == rollout.stat().st_ino


def test_plan_blocks_rollout_metadata_mismatch_and_digest_tampering(tmp_path: Path) -> None:
    inventory, binding_plan, _project, rollout = _sources(tmp_path)
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "other", "cwd": "/wrong"}}) + "\n",
        encoding="utf-8",
    )

    plan = build_session_provenance_plan(inventory, binding_plan)

    assert plan["accepted"] is False
    assert plan["record_count"] == 0
    assert plan["blockers"] == [
        {
            "source_ref": "agent-1/-1001/42",
            "session_id": "session-42",
            "code": "ROLLOUT_VERIFICATION_FAILED",
            "detail": "session rollout identity does not match upstream state",
        }
    ]
    with pytest.raises(InventoryError, match="blocked"):
        validate_session_provenance_plan(plan)

    accepted = build_session_provenance_plan(*_sources(tmp_path / "second")[:2])
    accepted["record_count"] = 2
    with pytest.raises(InventoryError, match="digest verification"):
        validate_session_provenance_plan(accepted)


def test_plan_blocks_unproved_or_mismatched_live_account(tmp_path: Path) -> None:
    inventory, binding_plan, _project, _rollout = _sources(tmp_path)
    topics = inventory["topics"]
    assert isinstance(topics, list) and isinstance(topics[0], dict)
    topics[0]["live_codex_process"] = {
        "verified": False,
        "code": "LIVE_CODEX_ACCOUNT_UNVERIFIED",
    }
    inventory["inventory_digest"] = _digest(
        {key: value for key, value in inventory.items() if key != "inventory_digest"}
    )
    binding_plan = build_binding_migration_plan(inventory, generation_id="migration-1")

    missing = build_session_provenance_plan(inventory, binding_plan)

    assert missing["accepted"] is False
    assert missing["blockers"] == [
        {
            "source_ref": "agent-1/-1001/42",
            "code": "LIVE_ACCOUNT_EVIDENCE_MISSING",
        }
    ]

    inventory, binding_plan, _project, _rollout = _sources(tmp_path / "mismatch")
    topics = inventory["topics"]
    assert isinstance(topics, list) and isinstance(topics[0], dict)
    process = topics[0]["live_codex_process"]
    assert isinstance(process, dict)
    process["account_profile"] = "personal"
    inventory["inventory_digest"] = _digest(
        {key: value for key, value in inventory.items() if key != "inventory_digest"}
    )
    binding_plan = build_binding_migration_plan(inventory, generation_id="migration-1")

    mismatch = build_session_provenance_plan(inventory, binding_plan)

    assert mismatch["accepted"] is False
    assert mismatch["blockers"] == [
        {
            "source_ref": "agent-1/-1001/42",
            "code": "LIVE_ACCOUNT_PROFILE_MISMATCH",
        }
    ]


def test_store_import_is_idempotent_immutable_and_atomic(tmp_path: Path) -> None:
    inventory, binding_plan, project, _rollout = _sources(tmp_path)
    plan = build_session_provenance_plan(inventory, binding_plan)
    records = plan["records"]
    assert isinstance(records, list) and isinstance(records[0], dict)
    profile = tmp_path / "profiles" / "work"
    profile.mkdir(parents=True)
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    _seed_store(store, project, profile)
    try:
        record = dict(records[0])
        assert store.import_session_provenance((record,)) == 1
        assert store.import_session_provenance((record,)) == 0
        assert store.session_owner("session-42") == ("binding-42", "work")

        second = {**record, "session_id": "session-43"}
        invalid = {**record, "session_id": "session-44", "account_profile": "other"}
        with pytest.raises(ValueError, match="account"):
            store.import_session_provenance((second, invalid))
        assert store.session_owner("session-43") is None
    finally:
        store.close()


def test_daemon_reverifies_process_and_rollout_identity_before_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory, binding_plan, project, rollout = _sources(tmp_path)
    plan = build_session_provenance_plan(inventory, binding_plan)
    profile = tmp_path / "profiles" / "work"
    profile.mkdir(parents=True)
    real_codex = tmp_path / "codex-real"
    real_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real_codex.chmod(0o700)
    config = RuntimeConfig(
        agent_instance_id="agent-1",
        forum_chat_id=-1001,
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "run" / "runtime.sock",
        database_path=tmp_path / "state" / "runtime.sqlite",
        real_codex=real_codex,
        real_codex_version="test",
        real_codex_sha256=f"sha256:{hashlib.sha256(real_codex.read_bytes()).hexdigest()}",
        profiles_root=tmp_path / "profiles",
        topic_config_path=tmp_path / "topic_config.json",
        tmux_state_path=tmp_path / "state.json",
        upstream_sha="0" * 40,
        allowed_project_roots=(tmp_path,),
    )
    monkeypatch.setattr(
        "ai_agent_runtime.daemon.server._verify_legacy_process_evidence",
        lambda *_args, **_kwargs: None,
    )
    daemon = RuntimeServer(config)
    _seed_store(daemon.store, project, profile)
    try:
        response = daemon._import_session_provenance("request-1", {"plan": plan})
        assert response["ok"] is True
        assert response["imported"] == 1
        assert daemon.store.session_owner("session-42") == ("binding-42", "work")

        def reject_process(*_args: object, **_kwargs: object) -> None:
            raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "process evidence changed")

        monkeypatch.setattr(
            "ai_agent_runtime.daemon.server._verify_legacy_process_evidence",
            reject_process,
        )
        with pytest.raises(RuntimeFailureError, match="process evidence changed"):
            daemon._import_session_provenance("request-process-change", {"plan": plan})
        monkeypatch.setattr(
            "ai_agent_runtime.daemon.server._verify_legacy_process_evidence",
            lambda *_args, **_kwargs: None,
        )

        changed = copy.deepcopy(plan)
        changed_records = changed["records"]
        assert isinstance(changed_records, list) and isinstance(changed_records[0], dict)
        changed_records[0]["rollout_size"] = rollout.stat().st_size + 1
        changed.pop("plan_digest")
        changed["plan_digest"] = _digest(changed)
        with pytest.raises(RuntimeFailureError, match="rollout evidence changed"):
            daemon._import_session_provenance("request-2", {"plan": changed})
    finally:
        daemon.store.close()


@pytest.mark.asyncio
async def test_provenance_import_ipc_is_durable_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory, binding_plan, project, _rollout = _sources(tmp_path)
    plan = build_session_provenance_plan(inventory, binding_plan)
    profile = tmp_path / "profiles" / "work"
    profile.mkdir(parents=True)
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
    tmux_state = tmp_path / "tmux-state.json"
    tmux_state.write_text("{}", encoding="utf-8")
    config = RuntimeConfig(
        agent_instance_id="agent-1",
        forum_chat_id=-1001,
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "run" / "runtime.sock",
        database_path=tmp_path / "state" / "runtime.sqlite",
        real_codex=real_codex,
        real_codex_version="test",
        real_codex_sha256=f"sha256:{hashlib.sha256(real_codex.read_bytes()).hexdigest()}",
        profiles_root=tmp_path / "profiles",
        topic_config_path=topic_config,
        tmux_state_path=tmux_state,
        upstream_sha="0" * 40,
        allowed_project_roots=(tmp_path,),
    )
    monkeypatch.setattr(
        "ai_agent_runtime.daemon.server._verify_legacy_process_evidence",
        lambda *_args, **_kwargs: None,
    )
    daemon = RuntimeServer(config)
    _seed_store(daemon.store, project, profile)
    server_task = asyncio.create_task(daemon.serve())
    try:
        for _ in range(100):
            if config.socket_path.exists():
                break
            await asyncio.sleep(0.01)
        assert config.socket_path.exists()
        payload = versioned("import_session_provenance", "import-1", plan=plan)
        first = await request(config.socket_path, payload)
        replay = await request(config.socket_path, payload)
        second_request = await request(
            config.socket_path,
            versioned("import_session_provenance", "import-2", plan=plan),
        )
        assert first["ok"] is True and first["imported"] == 1
        assert replay["ok"] is True and replay["imported"] == 1
        assert second_request["ok"] is True and second_request["imported"] == 0
    finally:
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
