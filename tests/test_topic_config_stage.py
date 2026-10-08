from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from ai_agent_runtime.cli import _inventory_stage_topic_config
from ai_agent_runtime.migration.binding_plan import build_binding_migration_plan
from ai_agent_runtime.migration.inventory import INVENTORY_SCHEMA, InventoryError, _digest
from ai_agent_runtime.migration.topic_config_stage import stage_topic_config


def _inputs(
    tmp_path: Path,
) -> tuple[dict[str, object], dict[str, object], dict[str, object], Path, Path]:
    source = tmp_path / "legacy" / "topic_config.json"
    source.parent.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()
    source.write_text(
        json.dumps(
            {
                "unknown_top_level": {"keep": True},
                "topics": {
                    "42": {
                        "name": "Keep",
                        "cwd": str(cwd),
                        "engine": "codex",
                        "exec_mode": "tmux",
                        "unknown": "preserved",
                    },
                    "43": {
                        "name": "Deleted",
                        "cwd": str(cwd),
                        "engine": "codex",
                        "exec_mode": "tmux",
                    },
                },
            }
        )
    )
    topics: list[dict[str, object]] = []
    for thread_id, name in ((42, "Keep"), (43, "Deleted")):
        topics.append(
            {
                "source_ref": f"agent-1/-1001/{thread_id}",
                "agent_instance_id": "agent-1",
                "chat_id": -1001,
                "thread_id": thread_id,
                "name": name,
                "canonical_cwd": str(cwd),
                "engine": "codex",
                "exec_mode": "tmux",
                "issues": [],
                "cus_bindings": [
                    {
                        "binding_id": f"binding-{thread_id}",
                        "project_id": "project-1",
                        "canonical_cwd": str(cwd),
                        "lifecycle_status": "CONFIGURED",
                        "desired": {
                            "provider": "codex",
                            "account_profile": f"account-{thread_id}",
                            "model": "gpt-test",
                            "reasoning_effort": "high",
                        },
                    }
                ],
            }
        )
    baseline: dict[str, object] = {
        "schema": INVENTORY_SCHEMA,
        "topics": topics,
        "invalid_topic_keys": [],
        "sources": {
            "topic_config": {
                "path": str(source),
                "sha256": "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        },
    }
    baseline["inventory_digest"] = _digest(baseline)
    base_plan = build_binding_migration_plan(baseline, generation_id="migration-test")
    assert base_plan["accepted"] is True
    decisions: dict[str, object] = {
        "schema": "ai-agent-runtime.binding-migration-decisions.v1",
        "source_inventory_digest": baseline["inventory_digest"],
        "source_binding_plan_digest": base_plan["plan_digest"],
        "approved_by_role": "owner",
        "approved_at": "2026-10-08T12:00:00+03:00",
        "decisions": [
            {
                "source_ref": "agent-1/-1001/43",
                "binding_id": "binding-43",
                "thread_id": 43,
                "topic_name": "Deleted",
                "action": "EXCLUDE_DELETED_TOPIC",
                "reason": "Already deleted from the forum",
            }
        ],
    }
    decisions["decisions_digest"] = _digest(decisions)
    plan = build_binding_migration_plan(
        baseline, generation_id="migration-test", decisions=decisions
    )
    target_parent = tmp_path / "target"
    target_parent.mkdir(mode=0o700)
    target = target_parent / "topic_config.json"
    return baseline, plan, decisions, source, target


def test_stage_config_preserves_retained_topic_and_unknown_fields(tmp_path: Path) -> None:
    baseline, plan, decisions, source, target = _inputs(tmp_path)
    original = source.read_bytes()
    report = stage_topic_config(
        baseline=baseline, binding_plan=plan, decisions=decisions, target=target
    )
    staged = json.loads(target.read_text())
    assert staged["unknown_top_level"] == {"keep": True}
    assert staged["topics"] == {"42": json.loads(original)["topics"]["42"]}
    assert target.stat().st_mode & 0o777 == 0o600
    assert source.read_bytes() == original
    assert report["retained_topic_count"] == 1
    assert report["excluded_thread_ids"] == [43]
    assert report["status"] == "STAGED_NOT_ACTIVATED"
    assert report["production_cutover_authorized"] is False


def test_stage_config_rejects_changed_source_without_writing(tmp_path: Path) -> None:
    baseline, plan, decisions, source, target = _inputs(tmp_path)
    source.write_text(source.read_text() + " ")
    with pytest.raises(InventoryError, match="changed since the approved inventory"):
        stage_topic_config(baseline=baseline, binding_plan=plan, decisions=decisions, target=target)
    assert not target.exists()


def test_stage_config_requires_owner_decisions_for_exclusions(tmp_path: Path) -> None:
    baseline, plan, _decisions, _source, target = _inputs(tmp_path)
    with pytest.raises(InventoryError, match="cannot be reproduced"):
        stage_topic_config(baseline=baseline, binding_plan=plan, decisions=None, target=target)
    assert not target.exists()


def test_stage_config_refuses_existing_target_or_insecure_parent(tmp_path: Path) -> None:
    baseline, plan, decisions, _source, target = _inputs(tmp_path)
    target.write_text("existing")
    with pytest.raises(InventoryError, match="refusing to overwrite"):
        stage_topic_config(baseline=baseline, binding_plan=plan, decisions=decisions, target=target)
    assert target.read_text() == "existing"
    target.unlink()
    os.chmod(target.parent, 0o755)
    with pytest.raises(InventoryError, match="mode 0700"):
        stage_topic_config(baseline=baseline, binding_plan=plan, decisions=decisions, target=target)
    assert not target.exists()


def test_stage_command_rejects_report_target_collision_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "topic_config.json"
    with pytest.raises(InventoryError, match="report and target must be different"):
        _inventory_stage_topic_config(
            baseline="missing-baseline.json",
            binding_plan="missing-plan.json",
            decisions=None,
            target=str(target),
            output=str(target),
        )
    assert not target.exists()


def test_stage_command_rejects_insecure_report_parent_before_writing(tmp_path: Path) -> None:
    report_parent = tmp_path / "insecure"
    report_parent.mkdir(mode=0o755)
    with pytest.raises(InventoryError, match="report parent must be owned and mode 0700"):
        _inventory_stage_topic_config(
            baseline="missing-baseline.json",
            binding_plan="missing-plan.json",
            decisions=None,
            target=str(tmp_path / "topic_config.json"),
            output=str(report_parent / "stage.json"),
        )
    assert not (report_parent / "stage.json").exists()
