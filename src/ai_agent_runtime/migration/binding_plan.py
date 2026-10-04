"""Fail-closed Runtime Binding migration planning from an M0 inventory."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path
from typing import cast

from ai_agent_runtime.domain import sha256_digest
from ai_agent_runtime.migration.inventory import (
    INVENTORY_SCHEMA,
    InventoryError,
    _digest,
    _read_json_object,
)

BINDING_PLAN_SCHEMA = "ai-agent-runtime.binding-migration-plan.v1"


def read_binding_migration_plan(path: Path) -> dict[str, object]:
    payload, _sha256 = _read_json_object(path)
    if payload.get("schema") != BINDING_PLAN_SCHEMA:
        raise InventoryError("unsupported binding migration plan schema")
    supplied = payload.get("plan_digest")
    if not isinstance(supplied, str):
        raise InventoryError("binding migration plan digest is missing")
    unsigned = dict(payload)
    unsigned.pop("plan_digest", None)
    if supplied != _digest(unsigned):
        raise InventoryError("binding migration plan digest verification failed")
    return payload


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _desired_from_cus(
    *,
    topic: dict[str, object],
    generation_id: str,
    blockers: list[str],
) -> tuple[str, str, dict[str, object] | None]:
    source_ref = cast(str, topic["source_ref"])
    canonical_cwd = cast(str, topic["canonical_cwd"])
    raw_bindings = topic.get("cus_bindings")
    bindings = raw_bindings if isinstance(raw_bindings, list) else []
    binding_id = _stable_id("rb", source_ref)
    project_id = _stable_id("prj", canonical_cwd)
    if len(bindings) != 1 or not isinstance(bindings[0], dict):
        blockers.append("MISSING_OR_AMBIGUOUS_CUS_BINDING")
        return binding_id, project_id, None
    source = bindings[0]
    binding_id = _string(source.get("binding_id")) or binding_id
    project_id = _string(source.get("project_id")) or project_id
    if source.get("canonical_cwd") != canonical_cwd:
        blockers.append("CUS_BINDING_CWD_MISMATCH")
    if source.get("lifecycle_status") != "CONFIGURED":
        blockers.append("CUS_BINDING_NOT_CONFIGURED")
    desired = source.get("desired")
    if not isinstance(desired, dict):
        blockers.append("CUS_DESIRED_MISSING")
        return binding_id, project_id, None
    provider = _string(desired.get("provider"))
    account_profile = _string(desired.get("account_profile"))
    model = _string(desired.get("model"))
    reasoning_effort = _string(desired.get("reasoning_effort"))
    if provider != "codex":
        blockers.append("CUS_PROVIDER_NOT_CODEX")
    if account_profile is None:
        blockers.append("CUS_ACCOUNT_PROFILE_MISSING")
    if model is None:
        blockers.append("CUS_MODEL_MISSING")
    if reasoning_effort is None:
        blockers.append("CUS_REASONING_MISSING")
    if blockers:
        return binding_id, project_id, None
    desired_payload: dict[str, object] = {
        "binding_id": binding_id,
        "agent_instance_id": topic["agent_instance_id"],
        "generation_id": generation_id,
        "revision": 1,
        "project_id": project_id,
        "canonical_cwd": canonical_cwd,
        "provider": provider,
        "account_profile": account_profile,
        "model": model,
        "reasoning_effort": reasoning_effort,
    }
    desired_payload["spec_digest"] = sha256_digest(desired_payload)
    desired_payload["source_kind"] = "migration"
    desired_payload["source_ref"] = source_ref
    return binding_id, project_id, desired_payload


def build_binding_migration_plan(
    inventory: dict[str, object],
    *,
    generation_id: str,
) -> dict[str, object]:
    """Build a deterministic, non-mutating binding plan.

    Managed topics are READY only when the inventory proves one exact CUS
    binding and all required desired values. No defaults are invented.
    """

    if inventory.get("schema") != INVENTORY_SCHEMA:
        raise InventoryError("binding plan requires a migration inventory")
    if not generation_id or len(generation_id) > 128:
        raise InventoryError("generation_id must be non-empty and at most 128 characters")
    raw_topics = inventory.get("topics")
    if not isinstance(raw_topics, list):
        raise InventoryError("inventory topics must be a list")
    entries: list[dict[str, object]] = []
    for raw_topic in raw_topics:
        if not isinstance(raw_topic, dict):
            raise InventoryError("inventory contains a malformed topic")
        topic = cast(dict[str, object], raw_topic)
        source_ref = _string(topic.get("source_ref"))
        canonical_cwd = _string(topic.get("canonical_cwd"))
        if source_ref is None:
            raise InventoryError("inventory topic source_ref is missing")
        raw_issues = topic.get("issues", [])
        blockers = (
            [str(item) for item in raw_issues if isinstance(item, str)]
            if isinstance(raw_issues, list)
            else ["TOPIC_ISSUES_MALFORMED"]
        )
        if _string(topic.get("name")) is None:
            blockers.append("TOPIC_NAME_MISSING")
        engine = _string(topic.get("engine")) or "claude"
        exec_mode = _string(topic.get("exec_mode")) or "subprocess"
        if engine != "codex":
            if canonical_cwd is None:
                blockers.append("CANONICAL_CWD_MISSING")
                binding_id = _stable_id("rb", source_ref)
                project_id = None
                desired = None
            else:
                binding_id, project_id, desired = _desired_from_cus(
                    topic=topic,
                    generation_id=generation_id,
                    blockers=blockers,
                )
            entries.append(
                {
                    "source_ref": source_ref,
                    "status": "BLOCKED" if blockers else "UNMANAGED",
                    "binding_id": binding_id,
                    "project_id": project_id,
                    "identity": {
                        "agent_instance_id": topic.get("agent_instance_id"),
                        "chat_id": topic.get("chat_id"),
                        "thread_id": topic.get("thread_id"),
                        "topic_name": topic.get("name"),
                        "canonical_cwd": canonical_cwd,
                    },
                    "desired": desired if not blockers else None,
                    "blockers": sorted(set(blockers)),
                }
            )
            continue
        if exec_mode != "tmux":
            blockers.append("UNSUPPORTED_CODEX_SUBPROCESS")
        if canonical_cwd is None:
            blockers.append("CANONICAL_CWD_MISSING")
            binding_id = _stable_id("rb", source_ref)
            project_id = None
            desired = None
        else:
            binding_id, project_id, desired = _desired_from_cus(
                topic=topic,
                generation_id=generation_id,
                blockers=blockers,
            )
        entries.append(
            {
                "source_ref": source_ref,
                "status": "BLOCKED" if blockers else "READY",
                "binding_id": binding_id,
                "project_id": project_id,
                "identity": {
                    "agent_instance_id": topic.get("agent_instance_id"),
                    "chat_id": topic.get("chat_id"),
                    "thread_id": topic.get("thread_id"),
                    "topic_name": topic.get("name"),
                    "canonical_cwd": canonical_cwd,
                },
                "desired": desired if not blockers else None,
                "blockers": sorted(set(blockers)),
            }
        )

    binding_sources: dict[str, list[str]] = defaultdict(list)
    cwd_projects: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for entry in entries:
        entry_binding_id = entry.get("binding_id")
        entry_source_ref = cast(str, entry["source_ref"])
        if isinstance(entry_binding_id, str):
            binding_sources[entry_binding_id].append(entry_source_ref)
        identity = entry.get("identity")
        entry_project_id = entry.get("project_id")
        if isinstance(identity, dict) and isinstance(entry_project_id, str):
            cwd = identity.get("canonical_cwd")
            if isinstance(cwd, str):
                cwd_projects[cwd][entry_project_id].append(entry_source_ref)

    duplicate_binding_ids = {
        binding_id: sorted(refs) for binding_id, refs in binding_sources.items() if len(refs) > 1
    }
    conflicting_projects = {
        cwd: {project_id: sorted(refs) for project_id, refs in projects.items()}
        for cwd, projects in cwd_projects.items()
        if len(projects) > 1
    }
    for entry in entries:
        entry_source_ref = cast(str, entry["source_ref"])
        entry_blockers = cast(list[str], entry["blockers"])
        entry_binding_id = entry.get("binding_id")
        if isinstance(entry_binding_id, str) and entry_binding_id in duplicate_binding_ids:
            entry_blockers.append("DUPLICATE_BINDING_ID")
        identity = entry.get("identity")
        if isinstance(identity, dict) and identity.get("canonical_cwd") in conflicting_projects:
            entry_blockers.append("PROJECT_ID_CONFLICT_FOR_CANONICAL_CWD")
        if entry_blockers:
            entry["status"] = "BLOCKED"
            entry["desired"] = None
            entry["blockers"] = sorted(set(entry_blockers))
        assert entry_source_ref

    entries.sort(key=lambda entry: cast(str, entry["source_ref"]))
    counts = {
        status: sum(1 for entry in entries if entry["status"] == status)
        for status in ("READY", "UNMANAGED", "BLOCKED")
    }
    plan: dict[str, object] = {
        "schema": BINDING_PLAN_SCHEMA,
        "source_inventory_digest": inventory.get("inventory_digest"),
        "generation_id": generation_id,
        "accepted": counts["BLOCKED"] == 0,
        "counts": counts,
        "duplicate_binding_ids": duplicate_binding_ids,
        "conflicting_projects": conflicting_projects,
        "entries": entries,
    }
    plan["plan_digest"] = _digest(plan)
    return plan
