"""Read-only guard against accidental legacy-session reuse at cutover."""

from __future__ import annotations

from pathlib import Path

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.migration.inventory import (
    INVENTORY_SCHEMA,
    InventoryError,
    _digest,
    _read_json_object,
    _source_evidence,
    collect_live_tmux,
)
from ai_agent_runtime.telegram_upstream.topic_config import read_topic_config

FRESH_CONTEXT_PREFLIGHT_SCHEMA = "ai-agent-runtime.fresh-context-preflight.v1"
_BINDING_PLAN_SCHEMA = "ai-agent-runtime.binding-migration-plan.v1"


def _legacy_source_path(baseline: dict[str, object], name: str) -> Path:
    sources = baseline.get("sources")
    source = sources.get(name) if isinstance(sources, dict) else None
    raw_path = source.get("path") if isinstance(source, dict) else None
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise InventoryError(f"baseline {name} path is missing or unsafe")
    return Path(raw_path)


def build_fresh_context_preflight(
    config: RuntimeConfig,
    *,
    baseline: dict[str, object],
    binding_plan: dict[str, object],
    live_tmux: dict[str, object] | None = None,
) -> dict[str, object]:
    """Check isolation and empty new session state without changing either service."""

    if baseline.get("schema") != INVENTORY_SCHEMA:
        raise InventoryError("unsupported baseline inventory schema")
    if (
        binding_plan.get("schema") != _BINDING_PLAN_SCHEMA
        or binding_plan.get("accepted") is not True
        or binding_plan.get("source_inventory_digest") != baseline.get("inventory_digest")
        or binding_plan.get("plan_digest")
        != _digest({key: value for key, value in binding_plan.items() if key != "plan_digest"})
    ):
        raise InventoryError("fresh-context preflight requires an accepted baseline-bound plan")
    entries = binding_plan.get("entries")
    if not isinstance(entries, list):
        raise InventoryError("binding plan entries are malformed")
    baseline_topics = baseline.get("topics")
    if not isinstance(baseline_topics, list):
        raise InventoryError("baseline topics are malformed")
    baseline_by_thread = {
        topic.get("thread_id"): topic
        for topic in baseline_topics
        if isinstance(topic, dict) and type(topic.get("thread_id")) is int
    }

    issues: list[str] = []
    if config.tmux_tmpdir is None:
        issues.append("ISOLATED_TMUX_SELECTOR_NOT_EXPLICIT")
    if config.tmux_state_path.resolve(strict=False) == _legacy_source_path(
        baseline, "tmux_state"
    ).resolve(strict=False):
        issues.append("LEGACY_TMUX_STATE_PATH_REUSED")
    if config.topic_config_path.resolve(strict=False) == _legacy_source_path(
        baseline, "topic_config"
    ).resolve(strict=False):
        issues.append("LEGACY_TOPIC_CONFIG_PATH_REUSED")
    retained_threads: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise InventoryError("binding plan entry is malformed")
        if entry.get("status") == "EXCLUDED":
            continue
        identity = entry.get("identity")
        if (
            not isinstance(identity, dict)
            or identity.get("agent_instance_id") != config.agent_instance_id
            or identity.get("chat_id") != config.forum_chat_id
            or type(identity.get("thread_id")) is not int
        ):
            issues.append("RETAINED_BINDING_TARGET_INSTANCE_MISMATCH")
            continue
        retained_threads.add(identity["thread_id"])

    topic_path = config.topic_config_path
    topic_evidence: dict[str, object]
    if topic_path.is_symlink():
        issues.append("TARGET_TOPIC_CONFIG_IS_SYMLINK")
        topic_evidence = {"present": True, "sha256": None}
    else:
        topic_evidence = _source_evidence(topic_path, optional=True)
        if topic_evidence.get("present") is not True:
            issues.append("TARGET_TOPIC_CONFIG_MISSING")
        else:
            snapshot = read_topic_config(topic_path)
            target_topics = snapshot.raw["topics"]
            assert isinstance(target_topics, dict)
            if set(target_topics) != {str(thread_id) for thread_id in retained_threads}:
                issues.append("TARGET_TOPIC_SET_MISMATCH")
            for thread_id in retained_threads:
                expected_topic = baseline_by_thread.get(thread_id)
                observed = snapshot.topic(thread_id)
                if expected_topic is None or observed is None:
                    issues.append("RETAINED_TOPIC_MISSING")
                    continue
                observed_cwd = (
                    str(Path(observed.cwd).resolve(strict=False)) if observed.cwd else None
                )
                if (
                    observed.raw.get("name") != expected_topic.get("name")
                    or observed_cwd != expected_topic.get("canonical_cwd")
                    or observed.engine != expected_topic.get("engine")
                    or observed.exec_mode != expected_topic.get("exec_mode")
                ):
                    issues.append("RETAINED_TOPIC_CONFIG_DRIFT")
            if _source_evidence(topic_path) != topic_evidence:
                issues.append("TARGET_TOPIC_CONFIG_CHANGED_DURING_PREFLIGHT")

    state_path = config.tmux_state_path
    if state_path.is_symlink():
        issues.append("TARGET_TMUX_STATE_IS_SYMLINK")
        state_evidence: dict[str, object] = {"present": True, "sha256": None}
    else:
        state_evidence = _source_evidence(state_path, optional=True)
        if state_evidence.get("present") is True:
            state, _raw_digest = _read_json_object(state_path)
            if state:
                issues.append("TARGET_TMUX_STATE_NOT_EMPTY")
            if _source_evidence(state_path) != state_evidence:
                issues.append("TARGET_TMUX_STATE_CHANGED_DURING_PREFLIGHT")

    observed_tmux = live_tmux if live_tmux is not None else collect_live_tmux(config.tmux_tmpdir)
    raw_panes = observed_tmux.get("panes")
    if observed_tmux.get("available") is True:
        if not isinstance(raw_panes, list):
            issues.append("TARGET_TMUX_OBSERVATION_MALFORMED")
        elif raw_panes:
            issues.append("TARGET_TMUX_SERVER_NOT_EMPTY")
    elif observed_tmux.get("reason") != "isolated_tmux_socket_absent" or raw_panes != []:
        issues.append("TARGET_TMUX_SERVER_UNVERIFIED")

    report: dict[str, object] = {
        "schema": FRESH_CONTEXT_PREFLIGHT_SCHEMA,
        "baseline_inventory_digest": baseline.get("inventory_digest"),
        "binding_plan_digest": binding_plan.get("plan_digest"),
        "agent_instance_id": config.agent_instance_id,
        "forum_chat_id": config.forum_chat_id,
        "target_topic_config": topic_evidence,
        "target_tmux_state": state_evidence,
        "target_tmux_observation": {
            "available": observed_tmux.get("available"),
            "reason": observed_tmux.get("reason"),
            "pane_count": len(raw_panes) if isinstance(raw_panes, list) else None,
        },
        "issues": sorted(set(issues)),
        "accepted": not issues,
        "mutations_performed": False,
        "production_cutover_authorized": False,
    }
    report["report_digest"] = _digest(report)
    return report
