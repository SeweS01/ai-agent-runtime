from __future__ import annotations

from copy import deepcopy

import pytest

from ai_agent_runtime.domain import sha256_digest
from ai_agent_runtime.migration.binding_plan import build_binding_migration_plan
from ai_agent_runtime.migration.inventory import INVENTORY_SCHEMA, InventoryError


def _topic(thread_id: int, *, binding_id: str, project_id: str) -> dict[str, object]:
    cwd = "/srv/repos/shared"
    return {
        "source_ref": f"agent-1/-1001/{thread_id}",
        "agent_instance_id": "agent-1",
        "chat_id": -1001,
        "thread_id": thread_id,
        "name": f"Topic {thread_id}",
        "canonical_cwd": cwd,
        "engine": "codex",
        "exec_mode": "tmux",
        "issues": [],
        "cus_bindings": [
            {
                "binding_id": binding_id,
                "project_id": project_id,
                "canonical_cwd": cwd,
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


def _inventory() -> dict[str, object]:
    return {
        "schema": INVENTORY_SCHEMA,
        "inventory_digest": "sha256:" + "1" * 64,
        "topics": [
            _topic(42, binding_id="binding-42", project_id="project-shared"),
            _topic(43, binding_id="binding-43", project_id="project-shared"),
        ],
    }


def test_plan_keeps_same_cwd_topics_as_distinct_ready_bindings() -> None:
    plan = build_binding_migration_plan(_inventory(), generation_id="migration-2026-10-03")
    assert plan["accepted"] is True
    assert plan["counts"] == {"READY": 2, "UNMANAGED": 0, "BLOCKED": 0}
    entries = plan["entries"]
    assert isinstance(entries, list)
    assert [entry["binding_id"] for entry in entries] == ["binding-42", "binding-43"]
    assert {entry["project_id"] for entry in entries} == {"project-shared"}
    assert {entry["desired"]["account_profile"] for entry in entries} == {
        "account-42",
        "account-43",
    }
    assert all(entry["desired"]["generation_id"] == "migration-2026-10-03" for entry in entries)
    assert all(entry["desired"]["revision"] == 1 for entry in entries)


def test_plan_never_defaults_missing_desired_or_codex_subprocess() -> None:
    inventory = _inventory()
    topics = inventory["topics"]
    assert isinstance(topics, list)
    topics[0]["cus_bindings"] = []
    topics[1]["exec_mode"] = "subprocess"
    plan = build_binding_migration_plan(inventory, generation_id="migration-1")
    assert plan["accepted"] is False
    entries = plan["entries"]
    assert isinstance(entries, list)
    assert all(entry["status"] == "BLOCKED" for entry in entries)
    assert all(entry["desired"] is None for entry in entries)
    assert "MISSING_OR_AMBIGUOUS_CUS_BINDING" in entries[0]["blockers"]
    assert "UNSUPPORTED_CODEX_SUBPROCESS" in entries[1]["blockers"]


def test_plan_blocks_duplicate_binding_and_project_identity_conflicts() -> None:
    inventory = deepcopy(_inventory())
    topics = inventory["topics"]
    assert isinstance(topics, list)
    topics[1]["cus_bindings"][0]["binding_id"] = "binding-42"
    topics[1]["cus_bindings"][0]["project_id"] = "other-project"
    plan = build_binding_migration_plan(inventory, generation_id="migration-1")
    assert plan["accepted"] is False
    entries = plan["entries"]
    assert isinstance(entries, list)
    for entry in entries:
        assert entry["status"] == "BLOCKED"
        assert entry["desired"] is None
        assert "DUPLICATE_BINDING_ID" in entry["blockers"]
        assert "PROJECT_ID_CONFLICT_FOR_CANONICAL_CWD" in entry["blockers"]


def test_plan_preserves_non_codex_topic_as_unmanaged_binding_with_profile() -> None:
    inventory = _inventory()
    topics = inventory["topics"]
    assert isinstance(topics, list)
    topics[0]["engine"] = "claude"
    topics[0]["exec_mode"] = "subprocess"
    plan = build_binding_migration_plan(inventory, generation_id="migration-1")
    entries = plan["entries"]
    assert isinstance(entries, list)
    first = entries[0]
    assert first["status"] == "UNMANAGED"
    assert first["binding_id"] == "binding-42"
    assert first["desired"]["account_profile"] == "account-42"
    assert first["identity"]["topic_name"] == "Topic 42"


def test_owner_decision_can_explicitly_exclude_deleted_topic() -> None:
    inventory = _inventory()
    topics = inventory["topics"]
    assert isinstance(topics, list)
    topics[0]["canonical_cwd"] = None
    topics[0]["cus_bindings"] = []
    base = build_binding_migration_plan(inventory, generation_id="migration-1")
    assert base["accepted"] is False
    decisions: dict[str, object] = {
        "schema": "ai-agent-runtime.binding-migration-decisions.v1",
        "source_inventory_digest": inventory["inventory_digest"],
        "source_binding_plan_digest": base["plan_digest"],
        "approved_by_role": "owner",
        "approved_at": "2026-10-04T12:00:00+03:00",
        "decisions": [
            {
                "source_ref": "agent-1/-1001/42",
                "binding_id": base["entries"][0]["binding_id"],
                "thread_id": 42,
                "topic_name": "Topic 42",
                "action": "EXCLUDE_DELETED_TOPIC",
                "reason": "Topic no longer exists in the Telegram forum",
            }
        ],
    }
    decisions["decisions_digest"] = sha256_digest(decisions)

    resolved = build_binding_migration_plan(
        inventory,
        generation_id="migration-1",
        decisions=decisions,
    )

    assert resolved["accepted"] is True
    assert resolved["counts"] == {
        "READY": 1,
        "UNMANAGED": 0,
        "EXCLUDED": 1,
        "BLOCKED": 0,
    }
    excluded = resolved["entries"][0]
    assert excluded["status"] == "EXCLUDED"
    assert excluded["desired"] is None
    assert excluded["blockers"] == []
    assert excluded["exclusion"]["decisions_digest"] == decisions["decisions_digest"]


def test_exclusion_decision_is_bound_to_exact_inventory_plan_and_identity() -> None:
    inventory = _inventory()
    base = build_binding_migration_plan(inventory, generation_id="migration-1")
    decisions: dict[str, object] = {
        "schema": "ai-agent-runtime.binding-migration-decisions.v1",
        "source_inventory_digest": inventory["inventory_digest"],
        "source_binding_plan_digest": base["plan_digest"],
        "approved_by_role": "owner",
        "approved_at": "2026-10-04T12:00:00+03:00",
        "decisions": [
            {
                "source_ref": "agent-1/-1001/42",
                "binding_id": "binding-42",
                "thread_id": 999,
                "topic_name": "Topic 42",
                "action": "EXCLUDE_DELETED_TOPIC",
                "reason": "Deleted",
            }
        ],
    }
    decisions["decisions_digest"] = sha256_digest(decisions)

    with pytest.raises(InventoryError, match="thread identity mismatch"):
        build_binding_migration_plan(
            inventory,
            generation_id="migration-1",
            decisions=decisions,
        )
