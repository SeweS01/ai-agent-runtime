"""Stage a separate upstream topic configuration without touching the legacy bot."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import cast

from ai_agent_runtime.migration.binding_plan import build_binding_migration_plan
from ai_agent_runtime.migration.inventory import (
    INVENTORY_SCHEMA,
    InventoryError,
    _digest,
    _source_evidence,
    write_inventory,
)
from ai_agent_runtime.telegram_upstream.topic_config import read_topic_config

TOPIC_CONFIG_STAGE_SCHEMA = "ai-agent-runtime.topic-config-stage.v1"


def stage_topic_config(
    *,
    baseline: dict[str, object],
    binding_plan: dict[str, object],
    decisions: dict[str, object] | None,
    target: Path,
) -> dict[str, object]:
    """Create a 0600 target config from one exact, approved inventory snapshot."""

    if baseline.get("schema") != INVENTORY_SCHEMA:
        raise InventoryError("topic-config stage requires a migration inventory")
    generation_id = binding_plan.get("generation_id")
    if not isinstance(generation_id, str):
        raise InventoryError("binding plan generation is missing")
    replayed = build_binding_migration_plan(
        baseline, generation_id=generation_id, decisions=decisions
    )
    if replayed.get("accepted") is not True or replayed.get("plan_digest") != binding_plan.get(
        "plan_digest"
    ):
        raise InventoryError("binding plan is blocked or cannot be reproduced from approved inputs")
    entries = cast(list[dict[str, object]], replayed["entries"])
    raw_topics = baseline.get("topics")
    if not isinstance(raw_topics, list) or baseline.get("invalid_topic_keys"):
        raise InventoryError("baseline topic inventory is malformed or contains invalid keys")
    baseline_sources = baseline.get("sources")
    topic_source = (
        baseline_sources.get("topic_config") if isinstance(baseline_sources, dict) else None
    )
    raw_source_path = topic_source.get("path") if isinstance(topic_source, dict) else None
    source_digest = topic_source.get("sha256") if isinstance(topic_source, dict) else None
    if not isinstance(raw_source_path, str) or not isinstance(source_digest, str):
        raise InventoryError("baseline topic-config source evidence is missing")
    source = Path(raw_source_path)
    if not source.is_absolute() or source.is_symlink():
        raise InventoryError("legacy topic config path is unsafe")
    evidence_before = _source_evidence(source)
    if evidence_before.get("sha256") != source_digest:
        raise InventoryError("legacy topic config changed since the approved inventory")
    source_snapshot = read_topic_config(source)
    evidence_after = _source_evidence(source)
    if evidence_after != evidence_before or source_snapshot.digest != source_digest:
        raise InventoryError("legacy topic config changed while staging")
    topics = source_snapshot.raw.get("topics")
    assert isinstance(topics, dict)

    by_source_ref: dict[str, dict[str, object]] = {}
    for raw_topic in raw_topics:
        if not isinstance(raw_topic, dict) or not isinstance(raw_topic.get("source_ref"), str):
            raise InventoryError("baseline topic is malformed")
        source_ref = raw_topic["source_ref"]
        if source_ref in by_source_ref:
            raise InventoryError("baseline contains duplicate topic source refs")
        by_source_ref[source_ref] = raw_topic
    if {entry.get("source_ref") for entry in entries} != set(by_source_ref):
        raise InventoryError("binding plan does not cover baseline topics exactly")

    excluded_threads: list[int] = []
    for entry in entries:
        source_ref = entry.get("source_ref")
        topic = by_source_ref[cast(str, source_ref)]
        thread_id = topic.get("thread_id")
        identity = entry.get("identity")
        if (
            type(thread_id) is not int
            or not isinstance(identity, dict)
            or identity.get("thread_id") != thread_id
            or identity.get("topic_name") != topic.get("name")
            or identity.get("canonical_cwd") != topic.get("canonical_cwd")
            or str(thread_id) not in topics
        ):
            raise InventoryError("binding plan topic identity does not match source config")
        if entry.get("status") == "EXCLUDED":
            excluded_threads.append(thread_id)
        elif entry.get("status") not in {"READY", "UNMANAGED"}:
            raise InventoryError("binding plan contains an unsupported topic status")

    if not target.is_absolute() or target == Path("/") or ".." in target.parts:
        raise InventoryError("target topic config path is unsafe")
    if target.resolve(strict=False) == source.resolve(strict=False):
        raise InventoryError("target topic config must not be the legacy source")
    parent = target.parent
    if not parent.is_dir() or parent.is_symlink():
        raise InventoryError("target topic config parent must already be a private directory")
    parent_metadata = parent.stat()
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.getuid()
        or parent_metadata.st_mode & 0o077
    ):
        raise InventoryError("target topic config parent must be owned and mode 0700")
    if target.exists() or target.is_symlink():
        raise InventoryError("refusing to overwrite target topic config")

    staged = dict(source_snapshot.raw)
    staged_topics = dict(topics)
    for thread_id in excluded_threads:
        staged_topics.pop(str(thread_id))
    staged["topics"] = staged_topics
    write_inventory(target, staged)
    target_evidence = _source_evidence(target)
    if read_topic_config(target).raw != staged:
        raise InventoryError("staged topic config failed post-write verification")
    if _source_evidence(source) != evidence_before:
        raise InventoryError(
            "legacy topic config changed after staging; target must not be activated"
        )

    report: dict[str, object] = {
        "schema": TOPIC_CONFIG_STAGE_SCHEMA,
        "baseline_inventory_digest": baseline.get("inventory_digest"),
        "binding_plan_digest": binding_plan.get("plan_digest"),
        "source_topic_config_sha256": source_digest,
        "target_topic_config": target_evidence,
        "retained_topic_count": len(entries) - len(excluded_threads),
        "excluded_thread_ids": sorted(excluded_threads),
        "status": "STAGED_NOT_ACTIVATED",
        "legacy_source_modified": False,
        "services_restarted": False,
        "production_cutover_authorized": False,
    }
    report["report_digest"] = _digest(report)
    return report
