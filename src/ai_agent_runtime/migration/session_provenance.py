"""Digest-bound migration of exact legacy Codex session ownership."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, cast

from ai_agent_runtime.migration.binding_plan import BINDING_PLAN_SCHEMA
from ai_agent_runtime.migration.inventory import (
    INVENTORY_SCHEMA,
    InventoryError,
    _digest,
    _read_json_object,
)

SESSION_PROVENANCE_PLAN_SCHEMA = "ai-agent-runtime.session-provenance-plan.v1"
_MAX_SESSION_META_BYTES = 1024 * 1024


def _nonempty(value: object, field: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
        or "\n" in value
        or "\r" in value
    ):
        raise InventoryError(f"session provenance {field} is invalid")
    return value


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise InventoryError(f"session provenance {field} is invalid")
    return value


def inspect_rollout_session(
    path: Path,
    *,
    expected_session_id: str,
    expected_cwd: str,
) -> dict[str, object]:
    """Verify an owned rollout's first metadata record without reading transcript text."""

    if not path.is_absolute() or ".." in path.parts or path == Path("/") or path.is_symlink():
        raise InventoryError("session rollout path is unsafe")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InventoryError("session rollout cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise InventoryError("session rollout must be an owned regular file")
        first_line = bytearray()
        terminated = False
        while len(first_line) <= _MAX_SESSION_META_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, _MAX_SESSION_META_BYTES + 1 - len(first_line)),
            )
            if not chunk:
                break
            newline = chunk.find(b"\n")
            if newline >= 0:
                first_line.extend(chunk[:newline])
                terminated = True
                break
            first_line.extend(chunk)
    finally:
        os.close(descriptor)
    if len(first_line) > _MAX_SESSION_META_BYTES or not terminated:
        raise InventoryError("session rollout metadata line is missing or oversized")
    try:
        record = json.loads(first_line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InventoryError("session rollout metadata is invalid") from exc
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        raise InventoryError("session rollout does not start with session_meta")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise InventoryError("session rollout session_meta payload is invalid")
    observed_session = payload.get("id") or payload.get("session_id")
    if observed_session != expected_session_id:
        raise InventoryError("session rollout identity does not match upstream state")
    raw_cwd = payload.get("cwd")
    if not isinstance(raw_cwd, str) or str(Path(raw_cwd).resolve(strict=False)) != expected_cwd:
        raise InventoryError("session rollout cwd does not match binding")
    resolved = path.resolve(strict=True)
    resolved_metadata = resolved.stat()
    if (
        resolved_metadata.st_dev != metadata.st_dev
        or resolved_metadata.st_ino != metadata.st_ino
    ):
        raise InventoryError("session rollout path identity changed during inspection")
    return {
        "rollout_path": str(resolved),
        "rollout_device": metadata.st_dev,
        "rollout_inode": metadata.st_ino,
        "rollout_size": metadata.st_size,
    }


def _validate_inputs(
    inventory: dict[str, object],
    binding_plan: dict[str, object],
) -> None:
    if inventory.get("schema") != INVENTORY_SCHEMA:
        raise InventoryError("session provenance requires a migration inventory")
    inventory_unsigned = dict(inventory)
    inventory_digest = inventory_unsigned.pop("inventory_digest", None)
    if not isinstance(inventory_digest, str) or inventory_digest != _digest(inventory_unsigned):
        raise InventoryError("session provenance inventory digest is invalid")
    if binding_plan.get("schema") != BINDING_PLAN_SCHEMA:
        raise InventoryError("session provenance requires a binding migration plan")
    plan_unsigned = dict(binding_plan)
    plan_digest = plan_unsigned.pop("plan_digest", None)
    if not isinstance(plan_digest, str) or plan_digest != _digest(plan_unsigned):
        raise InventoryError("session provenance binding plan digest is invalid")
    if binding_plan.get("source_inventory_digest") != inventory_digest:
        raise InventoryError("binding plan does not belong to the supplied inventory")


def build_session_provenance_plan(
    inventory: dict[str, object],
    binding_plan: dict[str, object],
) -> dict[str, object]:
    """Plan exact current-session ownership without mutating Runtime state."""

    _validate_inputs(inventory, binding_plan)
    raw_entries = binding_plan.get("entries")
    raw_topics = inventory.get("topics")
    if not isinstance(raw_entries, list) or not isinstance(raw_topics, list):
        raise InventoryError("session provenance sources are malformed")
    entries: dict[str, dict[str, object]] = {}
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise InventoryError("binding migration plan entry is malformed")
        source_ref = _nonempty(raw.get("source_ref"), "source_ref")
        if source_ref in entries:
            raise InventoryError("binding migration plan source_ref is duplicated")
        entries[source_ref] = cast(dict[str, object], raw)

    records: list[dict[str, object]] = []
    blockers: list[dict[str, object]] = []
    session_sources: dict[str, list[str]] = {}
    agent_instances: set[str] = {
        _nonempty(topic.get("agent_instance_id"), "agent_instance_id", maximum=64)
        for topic in raw_topics
        if isinstance(topic, dict)
    }
    if len(agent_instances) != 1:
        raise InventoryError("session provenance plan requires exactly one AgentInstance")
    for raw_topic in raw_topics:
        if not isinstance(raw_topic, dict):
            raise InventoryError("migration inventory topic is malformed")
        topic = cast(dict[str, object], raw_topic)
        source_ref = _nonempty(topic.get("source_ref"), "topic source_ref")
        state_rows = topic.get("tmux_state")
        if not isinstance(state_rows, list):
            raise InventoryError("migration inventory tmux_state is malformed")
        if not state_rows:
            continue
        entry = entries.get(source_ref)
        if entry is None or entry.get("status") != "READY":
            blockers.append(
                {"source_ref": source_ref, "code": "BINDING_PLAN_NOT_READY"}
            )
            continue
        desired = entry.get("desired")
        identity = entry.get("identity")
        if not isinstance(desired, dict) or not isinstance(identity, dict):
            raise InventoryError("ready binding migration entry is incomplete")
        binding_id = _nonempty(entry.get("binding_id"), "binding_id", maximum=128)
        agent_instance_id = _nonempty(
            identity.get("agent_instance_id"), "agent_instance_id", maximum=64
        )
        account_profile = _nonempty(
            desired.get("account_profile"), "account_profile", maximum=128
        )
        canonical_cwd = _nonempty(
            identity.get("canonical_cwd"), "canonical_cwd", maximum=4096
        )
        if agent_instance_id not in agent_instances:
            raise InventoryError("binding plan AgentInstance does not match inventory")
        for raw_state in state_rows:
            if not isinstance(raw_state, dict):
                blockers.append({"source_ref": source_ref, "code": "TMUX_STATE_MALFORMED"})
                continue
            state = cast(dict[str, Any], raw_state)
            raw_session_id = state.get("session_id")
            raw_rollout_path = state.get("transcript_path")
            if not isinstance(raw_session_id, str) or not raw_session_id:
                blockers.append({"source_ref": source_ref, "code": "SESSION_ID_MISSING"})
                continue
            session_id = _nonempty(raw_session_id, "session_id")
            session_sources.setdefault(session_id, []).append(source_ref)
            if state.get("provider") != "codex" or state.get("canonical_cwd") != canonical_cwd:
                blockers.append(
                    {
                        "source_ref": source_ref,
                        "session_id": session_id,
                        "code": "TMUX_STATE_BINDING_MISMATCH",
                    }
                )
                continue
            if not isinstance(raw_rollout_path, str) or not raw_rollout_path:
                blockers.append(
                    {"source_ref": source_ref, "session_id": session_id, "code": "ROLLOUT_MISSING"}
                )
                continue
            rollout_path = _nonempty(raw_rollout_path, "rollout_path", maximum=4096)
            try:
                rollout = inspect_rollout_session(
                    Path(rollout_path),
                    expected_session_id=session_id,
                    expected_cwd=canonical_cwd,
                )
            except InventoryError as exc:
                blockers.append(
                    {
                        "source_ref": source_ref,
                        "session_id": session_id,
                        "code": "ROLLOUT_VERIFICATION_FAILED",
                        "detail": str(exc),
                    }
                )
                continue
            records.append(
                {
                    "agent_instance_id": agent_instance_id,
                    "source_ref": source_ref,
                    "binding_id": binding_id,
                    "account_profile": account_profile,
                    "canonical_cwd": canonical_cwd,
                    "session_id": session_id,
                    **rollout,
                }
            )

    duplicate_sessions = {
        session_id: sorted(sources)
        for session_id, sources in session_sources.items()
        if len(sources) > 1
    }
    if duplicate_sessions:
        duplicate_ids = set(duplicate_sessions)
        records = [record for record in records if record["session_id"] not in duplicate_ids]
        blockers.extend(
            {
                "session_id": session_id,
                "source_refs": sources,
                "code": "SESSION_ID_AMBIGUOUS",
            }
            for session_id, sources in sorted(duplicate_sessions.items())
        )
    if len(agent_instances) > 1:
        raise InventoryError("session provenance plan spans multiple AgentInstances")
    records.sort(key=lambda record: str(record["session_id"]))
    blockers.sort(
        key=lambda item: (
            str(item.get("source_ref", "")),
            str(item.get("session_id", "")),
            str(item["code"]),
        )
    )
    plan: dict[str, object] = {
        "schema": SESSION_PROVENANCE_PLAN_SCHEMA,
        "agent_instance_id": next(iter(agent_instances)),
        "source_inventory_digest": inventory["inventory_digest"],
        "source_binding_plan_digest": binding_plan["plan_digest"],
        "accepted": not blockers,
        "records": records,
        "blockers": blockers,
        "record_count": len(records),
        "mutations_performed": False,
    }
    plan["plan_digest"] = _digest(plan)
    return plan


def validate_session_provenance_plan(plan: object) -> dict[str, object]:
    if not isinstance(plan, dict) or plan.get("schema") != SESSION_PROVENANCE_PLAN_SCHEMA:
        raise InventoryError("unsupported session provenance plan schema")
    if set(plan) != {
        "schema",
        "agent_instance_id",
        "source_inventory_digest",
        "source_binding_plan_digest",
        "accepted",
        "records",
        "blockers",
        "record_count",
        "mutations_performed",
        "plan_digest",
    }:
        raise InventoryError("session provenance plan fields mismatch")
    supplied = plan.get("plan_digest")
    unsigned = dict(plan)
    unsigned.pop("plan_digest", None)
    if _sha256(supplied, "plan_digest") != _digest(unsigned):
        raise InventoryError("session provenance plan digest verification failed")
    _nonempty(plan.get("agent_instance_id"), "agent_instance_id", maximum=64)
    _sha256(plan.get("source_inventory_digest"), "source_inventory_digest")
    _sha256(plan.get("source_binding_plan_digest"), "source_binding_plan_digest")
    if (
        plan.get("accepted") is not True
        or plan.get("blockers") != []
        or plan.get("mutations_performed") is not False
    ):
        raise InventoryError("blocked session provenance plan cannot be imported")
    records = plan.get("records")
    if not isinstance(records, list) or plan.get("record_count") != len(records):
        raise InventoryError("session provenance plan records are malformed")
    return cast(dict[str, object], plan)


def read_session_provenance_plan(path: Path) -> dict[str, object]:
    plan, _sha256 = _read_json_object(path)
    return validate_session_provenance_plan(plan)
