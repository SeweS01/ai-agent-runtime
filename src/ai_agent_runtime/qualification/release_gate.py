"""Fail-closed aggregation of staging qualification evidence.

This module deliberately does not execute or infer real Telegram/Codex tests. It
only validates immutable evidence emitted by those harnesses for one exact release
tuple. Unit-test success can therefore never be promoted to production qualification.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Final, cast

from ai_agent_runtime.migration.inventory import InventoryError, _digest

QUALIFICATION_MANIFEST_SCHEMA: Final = "ai-agent-runtime.qualification-manifest.v1"
QUALIFICATION_EVIDENCE_SCHEMA: Final = "ai-agent-runtime.qualification-evidence.v1"
QUALIFICATION_REPORT_SCHEMA: Final = "ai-agent-runtime.qualification-report.v1"
_MAX_EVIDENCE_BYTES: Final = 16 * 1024 * 1024
_SHA256_RE: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_SHA_RE: Final = re.compile(r"[0-9a-f]{40}\Z")

# Each assertion is a fact that must have been exercised by the staging harness.
# Additional assertions are permitted, but cannot replace these required facts.
QUALIFICATION_GATES: Final[dict[str, tuple[str, ...]]] = {
    "upstream_source_contract": (
        "exact_commit",
        "tmux_naming",
        "resume_switch_tmux_naming",
        "spawn_before_state_save",
        "safe_binary_launch",
    ),
    "phase_correlation": (
        "phase_a_before_state",
        "phase_b_state_confirmation",
        "phase_b_mismatch_terminates_child",
        "fresh_topic_config_both_phases",
    ),
    "real_codex_launch": (
        "selected_codex_home",
        "selected_model",
        "selected_reasoning",
        "rollout_identity",
    ),
    "telegram_e2e": (
        "prompt_correct_binding",
        "rollout_discovered",
        "final_answer_exactly_once",
    ),
    "upstream_commands": (
        "new",
        "resume_same_binding_same_account",
        "resume_foreign_binding_denied",
        "tui",
        "kill",
        "recycle",
    ),
    "observed_reconciliation": (
        "structured_provider_evidence",
        "online_revision_order_lww",
        "stale_observed_rejected",
        "no_feedback_loop",
    ),
    "agent_instance_isolation": (
        "one_forum_per_instance",
        "forum_assignment_unique",
        "bot_identity_unique",
        "ambiguous_onboarding_denied",
    ),
    "topic_config_reconciliation": (
        "never_create_thread_key",
        "lost_update_detected",
        "managed_drift_blocks_launch",
        "codex_subprocess_denied",
    ),
    "session_storage_lifecycle": (
        "sessions",
        "archived_sessions",
        "index_and_state",
        "concurrent_accounts",
        "legacy_session_provenance_import",
        "same_binding_resume",
        "foreign_binding_resume_denied",
        "rollback",
    ),
    "retirement_race": (
        "after_is_active_before_paste",
        "during_paste",
        "after_paste_before_enter",
        "after_pending_turn_registration",
        "turn_complete_before_runtime_idle",
        "two_simultaneous_messages",
    ),
    "offline_recovery": (
        "cus_offline_lkg_launch",
        "daemon_offline_new_launch_denied",
        "live_child_survives_daemon_restart",
        "wrapper_reconnects",
    ),
    "systemd_process_survival": (
        "watchdog",
        "telegram_agent_restart_preserves_tmux",
        "runtime_restart_preserves_child",
    ),
    "cus_bot_onboarding": (
        "exact_topic_notice",
        "exact_topic_miniapp",
        "backend_apply_before_success",
        "completion_notice",
        "expired_token_regenerated",
    ),
    "rollback_drill": (
        "legacy_artifact_verified",
        "session_topology_restored",
        "new_sessions_preserved",
        "old_path_e2e",
    ),
}


def _read_json(path: Path) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise InventoryError(f"qualification artifact is not a regular non-symlink file: {path}")
    size = path.stat().st_size
    if size <= 0 or size > _MAX_EVIDENCE_BYTES:
        raise InventoryError(f"qualification artifact has unsafe size: {path}")
    raw = path.read_bytes()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InventoryError(f"qualification artifact is not valid JSON: {path}") from exc
    if not isinstance(parsed, dict):
        raise InventoryError(f"qualification artifact must be a JSON object: {path}")
    return parsed, f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _without_digest(value: dict[str, Any], field: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != field}


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InventoryError(f"qualification {field} must be a non-empty string")
    return value


def validate_release_tuple(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InventoryError("release_tuple must be an object")
    required = {
        "telegram_agent_sha",
        "codex_version",
        "codex_binary_sha256",
        "runtime_release",
        "runtime_wheel_sha256",
        "protocol_major",
        "cus_connector_version",
    }
    if set(value) != required:
        missing = sorted(required - set(value))
        extra = sorted(set(value) - required)
        raise InventoryError(f"release_tuple fields mismatch: missing={missing}, extra={extra}")
    telegram_sha = _required_string(value["telegram_agent_sha"], "telegram_agent_sha")
    binary_sha = _required_string(value["codex_binary_sha256"], "codex_binary_sha256")
    runtime_wheel_sha = _required_string(value["runtime_wheel_sha256"], "runtime_wheel_sha256")
    protocol_major = value["protocol_major"]
    if not _GIT_SHA_RE.fullmatch(telegram_sha):
        raise InventoryError("telegram_agent_sha must be an exact lowercase 40-hex commit")
    if not _SHA256_RE.fullmatch(binary_sha):
        raise InventoryError("codex_binary_sha256 must use sha256:<64 lowercase hex>")
    if not _SHA256_RE.fullmatch(runtime_wheel_sha):
        raise InventoryError("runtime_wheel_sha256 must use sha256:<64 lowercase hex>")
    if (
        not isinstance(protocol_major, int)
        or isinstance(protocol_major, bool)
        or protocol_major < 1
    ):
        raise InventoryError("protocol_major must be a positive integer")
    return {
        "telegram_agent_sha": telegram_sha,
        "codex_version": _required_string(value["codex_version"], "codex_version"),
        "codex_binary_sha256": binary_sha,
        "runtime_release": _required_string(value["runtime_release"], "runtime_release"),
        "runtime_wheel_sha256": runtime_wheel_sha,
        "protocol_major": protocol_major,
        "cus_connector_version": _required_string(
            value["cus_connector_version"], "cus_connector_version"
        ),
    }


def release_tuple_digest(release_tuple: object) -> str:
    """Return the canonical digest for a validated exact release tuple."""

    return _digest(validate_release_tuple(release_tuple))


def _evidence_reference(path: Path) -> dict[str, str]:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise InventoryError(f"qualification evidence must not be a symlink: {expanded}")
    resolved = expanded.resolve(strict=True)
    evidence, sha256 = _read_json(resolved)
    gate_id = _required_string(evidence.get("gate_id"), "evidence gate_id")
    if gate_id not in QUALIFICATION_GATES:
        raise InventoryError(f"unknown qualification gate: {gate_id}")
    return {"gate_id": gate_id, "path": str(resolved), "sha256": sha256}


def build_qualification_manifest(
    *,
    report_id: str,
    release_tuple: object,
    evidence_paths: list[Path],
) -> dict[str, object]:
    """Pin a set of existing evidence files without claiming they passed."""

    normalized_report_id = _required_string(report_id, "report_id")
    normalized_tuple = validate_release_tuple(release_tuple)
    evidence = [_evidence_reference(path) for path in evidence_paths]
    gate_ids = [item["gate_id"] for item in evidence]
    if len(gate_ids) != len(set(gate_ids)):
        raise InventoryError("qualification manifest contains duplicate gate evidence")
    manifest: dict[str, object] = {
        "schema": QUALIFICATION_MANIFEST_SCHEMA,
        "report_id": normalized_report_id,
        "release_tuple": normalized_tuple,
        "release_tuple_digest": _digest(normalized_tuple),
        "required_gate_ids": sorted(QUALIFICATION_GATES),
        "evidence": sorted(evidence, key=lambda item: item["gate_id"]),
    }
    manifest["manifest_digest"] = _digest(manifest)
    return manifest


def _validate_retirement_assertions(assertions: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    allowed_outcomes = {"exactly_once", "explicit_not_delivered"}
    for assertion_id in QUALIFICATION_GATES["retirement_race"]:
        assertion = assertions.get(assertion_id)
        if assertion is None:
            continue
        if assertion.get("outcome") not in allowed_outcomes:
            errors.append(f"retirement_race:{assertion_id}:invalid_outcome")
        for forbidden in ("silent_loss", "duplicate_execution", "misroute"):
            if assertion.get(forbidden) is not False:
                errors.append(f"retirement_race:{assertion_id}:{forbidden}_not_false")
    return errors


def _validate_evidence(
    evidence: dict[str, Any],
    *,
    expected_gate_id: str,
    report_id: str,
    tuple_digest: str,
) -> list[str]:
    errors: list[str] = []
    if evidence.get("schema") != QUALIFICATION_EVIDENCE_SCHEMA:
        errors.append(f"{expected_gate_id}:schema_mismatch")
    if evidence.get("gate_id") != expected_gate_id:
        errors.append(f"{expected_gate_id}:gate_id_mismatch")
    if evidence.get("report_id") != report_id:
        errors.append(f"{expected_gate_id}:report_id_mismatch")
    if evidence.get("release_tuple_digest") != tuple_digest:
        errors.append(f"{expected_gate_id}:release_tuple_mismatch")
    if evidence.get("environment") != "staging":
        errors.append(f"{expected_gate_id}:not_staging_evidence")
    if evidence.get("status") != "PASS":
        errors.append(f"{expected_gate_id}:status_not_pass")
    if evidence.get("evidence_digest") != _digest(_without_digest(evidence, "evidence_digest")):
        errors.append(f"{expected_gate_id}:self_digest_mismatch")
    raw_assertions = evidence.get("assertions")
    if not isinstance(raw_assertions, list) or not raw_assertions:
        return [*errors, f"{expected_gate_id}:assertions_missing"]
    assertions: dict[str, dict[str, Any]] = {}
    for raw_assertion in raw_assertions:
        if not isinstance(raw_assertion, dict):
            errors.append(f"{expected_gate_id}:assertion_not_object")
            continue
        assertion_id = raw_assertion.get("id")
        if not isinstance(assertion_id, str) or not assertion_id:
            errors.append(f"{expected_gate_id}:assertion_id_invalid")
            continue
        if assertion_id in assertions:
            errors.append(f"{expected_gate_id}:{assertion_id}:duplicate")
            continue
        assertions[assertion_id] = raw_assertion
        if raw_assertion.get("passed") is not True:
            errors.append(f"{expected_gate_id}:{assertion_id}:not_passed")
    for assertion_id in QUALIFICATION_GATES[expected_gate_id]:
        if assertion_id not in assertions:
            errors.append(f"{expected_gate_id}:{assertion_id}:missing")
    if expected_gate_id == "retirement_race":
        errors.extend(_validate_retirement_assertions(assertions))
    return errors


def verify_qualification_manifest(path: Path) -> dict[str, object]:
    """Verify all pinned evidence and return a non-authoritative gate report."""

    manifest_path = path.expanduser().resolve(strict=True)
    manifest, manifest_file_sha256 = _read_json(manifest_path)
    errors: list[str] = []
    if manifest.get("schema") != QUALIFICATION_MANIFEST_SCHEMA:
        errors.append("manifest_schema_mismatch")
    report_id_value = manifest.get("report_id")
    report_id = report_id_value if isinstance(report_id_value, str) else ""
    if not report_id:
        errors.append("manifest_report_id_invalid")
    try:
        normalized_tuple = validate_release_tuple(manifest.get("release_tuple"))
        tuple_digest = _digest(normalized_tuple)
    except InventoryError as exc:
        normalized_tuple = {}
        tuple_digest = ""
        errors.append(f"release_tuple_invalid:{exc}")
    if manifest.get("release_tuple_digest") != tuple_digest:
        errors.append("release_tuple_digest_mismatch")
    if manifest.get("manifest_digest") != _digest(_without_digest(manifest, "manifest_digest")):
        errors.append("manifest_digest_mismatch")
    expected_gates = set(QUALIFICATION_GATES)
    raw_required = manifest.get("required_gate_ids")
    if (
        not isinstance(raw_required, list)
        or any(not isinstance(gate_id, str) for gate_id in raw_required)
        or set(raw_required) != expected_gates
    ):
        errors.append("required_gate_set_mismatch")
    raw_evidence = manifest.get("evidence")
    evidence_by_gate: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_evidence, list):
        errors.append("manifest_evidence_invalid")
        raw_evidence = []
    for raw_reference in raw_evidence:
        if not isinstance(raw_reference, dict):
            errors.append("evidence_reference_not_object")
            continue
        gate_id = raw_reference.get("gate_id")
        if not isinstance(gate_id, str) or gate_id not in expected_gates:
            errors.append("evidence_reference_gate_invalid")
            continue
        if gate_id in evidence_by_gate:
            errors.append(f"{gate_id}:duplicate_evidence_reference")
            continue
        evidence_by_gate[gate_id] = cast(dict[str, Any], raw_reference)
    missing_gates = sorted(expected_gates - set(evidence_by_gate))
    extra_gates = sorted(set(evidence_by_gate) - expected_gates)
    errors.extend(f"{gate_id}:evidence_missing" for gate_id in missing_gates)
    errors.extend(f"{gate_id}:evidence_unexpected" for gate_id in extra_gates)
    verified_gates: list[str] = []
    for gate_id in sorted(expected_gates & set(evidence_by_gate)):
        reference = evidence_by_gate[gate_id]
        raw_path = reference.get("path")
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            errors.append(f"{gate_id}:evidence_path_not_absolute")
            continue
        evidence_path = Path(raw_path)
        try:
            evidence, actual_sha256 = _read_json(evidence_path)
        except (InventoryError, OSError) as exc:
            errors.append(f"{gate_id}:evidence_unreadable:{exc}")
            continue
        if reference.get("sha256") != actual_sha256:
            errors.append(f"{gate_id}:artifact_digest_mismatch")
            continue
        gate_errors = _validate_evidence(
            evidence,
            expected_gate_id=gate_id,
            report_id=report_id,
            tuple_digest=tuple_digest,
        )
        if gate_errors:
            errors.extend(gate_errors)
        else:
            verified_gates.append(gate_id)
    accepted = not errors and set(verified_gates) == expected_gates
    report: dict[str, object] = {
        "schema": QUALIFICATION_REPORT_SCHEMA,
        "report_id": report_id,
        "status": "QUALIFIED" if accepted else "BLOCKED",
        "accepted": accepted,
        "release_tuple": normalized_tuple,
        "release_tuple_digest": tuple_digest,
        "manifest_path": str(manifest_path),
        "manifest_file_sha256": manifest_file_sha256,
        "verified_gate_ids": verified_gates,
        "missing_gate_ids": missing_gates,
        "errors": errors,
        "telegram_e2e_verified": accepted and "telegram_e2e" in verified_gates,
        "runtime_health_inferred": False,
    }
    report["report_digest"] = _digest(report)
    return report
