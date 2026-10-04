"""Fail-closed audit of a captured multi-AgentInstance deployment manifest."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from ai_agent_runtime import __version__
from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.errors import RuntimeFailureError
from ai_agent_runtime.ipc.protocol import PROTOCOL_MAJOR
from ai_agent_runtime.migration.inventory import InventoryError, _digest
from ai_agent_runtime.process_safety import file_sha256, validate_owned_executable
from ai_agent_runtime.qualification.release_gate import verify_qualification_manifest

DEPLOYMENT_MANIFEST_SCHEMA: Final = "ai-agent-runtime.deployment-manifest.v1"
DEPLOYMENT_AUDIT_SCHEMA: Final = "ai-agent-runtime.deployment-audit.v1"
_MAX_MANIFEST_BYTES: Final = 4 * 1024 * 1024

QualificationVerifier = Callable[[Path], dict[str, object]]
GitHeadReader = Callable[[Path], str]


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise InventoryError(f"deployment manifest is not a regular non-symlink file: {path}")
    if path.stat().st_size <= 0 or path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise InventoryError("deployment manifest has unsafe size")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InventoryError("deployment manifest is not valid JSON") from exc
    if not isinstance(value, dict):
        raise InventoryError("deployment manifest must be a JSON object")
    return value


def _git_head(path: Path) -> str:
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise InventoryError(f"cannot read pinned Telegram checkout: {path}")
    return completed.stdout.strip()


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise InventoryError(f"deployment {field} must be a non-empty string")
    return value


def _absolute_file(value: object, field: str) -> Path:
    path = Path(_required_string(value, field)).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise InventoryError(f"deployment {field} must be an absolute traversal-free path")
    return path


def _check_service_properties(
    *,
    instance_id: str,
    runtime_service: object,
    telegram_service: object,
    config_path: Path,
    wrapper_path: Path,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(runtime_service, dict):
        return [f"{instance_id}:runtime_service_invalid"]
    if not isinstance(telegram_service, dict):
        return [f"{instance_id}:telegram_service_invalid"]
    required_runtime: dict[str, object] = {
        "type": "notify",
        "restart": "always",
        "restart_sec_seconds": 2,
        "watchdog_sec_seconds": 30,
        "runtime_directory_mode": "0700",
        "umask": "0077",
        "socket_mode": "0600",
        "socket_accept": False,
        "config_path": str(config_path),
    }
    required_telegram: dict[str, object] = {
        "kill_mode": "process",
        "telegram_codex_bin": str(wrapper_path),
        "codex_auto_update_enabled": False,
    }
    for key, expected in required_runtime.items():
        if runtime_service.get(key) != expected:
            errors.append(f"{instance_id}:runtime_service:{key}_mismatch")
    for key, expected in required_telegram.items():
        if telegram_service.get(key) != expected:
            errors.append(f"{instance_id}:telegram_service:{key}_mismatch")
    for service_name, service in (
        ("runtime_service", runtime_service),
        ("telegram_service", telegram_service),
    ):
        unit_name = service.get("unit_name")
        if not isinstance(unit_name, str) or not unit_name.endswith(".service"):
            errors.append(f"{instance_id}:{service_name}:unit_name_invalid")
    return errors


def _audit_instance(
    raw: object,
    *,
    tuple_data: dict[str, object],
    git_head_reader: GitHeadReader,
) -> tuple[dict[str, object], list[str]]:
    if not isinstance(raw, dict):
        raise InventoryError("deployment AgentInstance entry must be an object")
    instance_id = _required_string(raw.get("agent_instance_id"), "agent_instance_id")
    errors: list[str] = []
    forum_chat_id = raw.get("forum_chat_id")
    if not isinstance(forum_chat_id, int) or isinstance(forum_chat_id, bool) or forum_chat_id >= 0:
        errors.append(f"{instance_id}:forum_chat_id_invalid")
    fingerprint = raw.get("telegram_bot_token_fingerprint")
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 71
        or not fingerprint.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in fingerprint[7:])
    ):
        errors.append(f"{instance_id}:bot_fingerprint_invalid")
    config_path = _absolute_file(raw.get("runtime_config"), "runtime_config")
    wrapper_path = _absolute_file(raw.get("wrapper_path"), "wrapper_path")
    checkout = _absolute_file(raw.get("telegram_checkout"), "telegram_checkout")
    try:
        config = RuntimeConfig.load(config_path)
        config.validate()
    except (OSError, ValueError, RuntimeFailureError) as exc:
        raise InventoryError(f"{instance_id}: runtime config invalid: {exc}") from exc
    if config.agent_instance_id != instance_id:
        errors.append(f"{instance_id}:runtime_config_namespace_mismatch")
    if config.forum_chat_id != forum_chat_id:
        errors.append(f"{instance_id}:runtime_config_forum_mismatch")
    if config.upstream_sha != tuple_data.get("telegram_agent_sha"):
        errors.append(f"{instance_id}:upstream_tuple_mismatch")
    if config.real_codex_version != tuple_data.get("codex_version"):
        errors.append(f"{instance_id}:codex_version_tuple_mismatch")
    if config.real_codex_sha256 != tuple_data.get("codex_binary_sha256"):
        errors.append(f"{instance_id}:codex_digest_tuple_mismatch")
    try:
        real_codex = validate_owned_executable(config.real_codex)
        if file_sha256(real_codex) != config.real_codex_sha256:
            errors.append(f"{instance_id}:real_codex_file_digest_mismatch")
    except (OSError, ValueError, RuntimeFailureError) as exc:
        errors.append(f"{instance_id}:real_codex_unsafe:{type(exc).__name__}")
    try:
        validate_owned_executable(wrapper_path)
    except (OSError, ValueError) as exc:
        errors.append(f"{instance_id}:wrapper_unsafe:{type(exc).__name__}")
    if wrapper_path.name != f"codex-wrapper@{instance_id}":
        errors.append(f"{instance_id}:wrapper_name_mismatch")
    if not checkout.is_dir() or checkout.is_symlink():
        errors.append(f"{instance_id}:telegram_checkout_unsafe")
    else:
        try:
            if git_head_reader(checkout) != tuple_data.get("telegram_agent_sha"):
                errors.append(f"{instance_id}:telegram_checkout_sha_mismatch")
        except (InventoryError, OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{instance_id}:telegram_checkout_unreadable:{type(exc).__name__}")
    errors.extend(
        _check_service_properties(
            instance_id=instance_id,
            runtime_service=raw.get("runtime_service"),
            telegram_service=raw.get("telegram_service"),
            config_path=config_path,
            wrapper_path=wrapper_path,
        )
    )
    return (
        {
            "agent_instance_id": instance_id,
            "forum_chat_id": forum_chat_id,
            "telegram_bot_token_fingerprint": fingerprint,
            "runtime_config": str(config_path),
            "wrapper_path": str(wrapper_path),
            "telegram_checkout": str(checkout),
            "state_dir": str(config.state_dir),
            "socket_path": str(config.socket_path),
            "database_path": str(config.database_path),
            "topic_config_path": str(config.topic_config_path),
            "tmux_state_path": str(config.tmux_state_path),
            "errors": errors,
        },
        errors,
    )


def audit_deployment_manifest(
    path: Path,
    *,
    qualification_verifier: QualificationVerifier = verify_qualification_manifest,
    git_head_reader: GitHeadReader = _git_head,
) -> dict[str, object]:
    """Validate captured deployment inputs without mutating services or files."""

    manifest_path = path.expanduser().resolve(strict=True)
    manifest = _read_json(manifest_path)
    errors: list[str] = []
    if manifest.get("schema") != DEPLOYMENT_MANIFEST_SCHEMA:
        errors.append("deployment_manifest_schema_mismatch")
    expected_manifest_fields = {
        "schema",
        "qualification_manifest",
        "agent_instances",
        "manifest_digest",
    }
    if set(manifest) != expected_manifest_fields:
        errors.append("deployment_manifest_fields_mismatch")
    if manifest.get("manifest_digest") != _digest(
        {key: value for key, value in manifest.items() if key != "manifest_digest"}
    ):
        errors.append("deployment_manifest_digest_mismatch")
    qualification_path = _absolute_file(
        manifest.get("qualification_manifest"), "qualification_manifest"
    )
    try:
        qualification = qualification_verifier(qualification_path)
    except (InventoryError, OSError, ValueError) as exc:
        qualification = {"accepted": False, "release_tuple": {}}
        errors.append(f"qualification_unreadable:{type(exc).__name__}")
    if qualification.get("accepted") is not True:
        errors.append("release_not_qualified")
    tuple_value = qualification.get("release_tuple")
    tuple_data = tuple_value if isinstance(tuple_value, dict) else {}
    if tuple_data.get("runtime_release") != __version__:
        errors.append("runtime_release_tuple_mismatch")
    if tuple_data.get("protocol_major") != PROTOCOL_MAJOR:
        errors.append("protocol_major_tuple_mismatch")
    raw_instances = manifest.get("agent_instances")
    if not isinstance(raw_instances, list) or not raw_instances:
        raise InventoryError("deployment manifest requires AgentInstance entries")
    instances: list[dict[str, object]] = []
    for raw in raw_instances:
        instance, instance_errors = _audit_instance(
            raw,
            tuple_data=tuple_data,
            git_head_reader=git_head_reader,
        )
        instances.append(instance)
        errors.extend(instance_errors)
    identity_fields = {
        "agent_instance_id": [item["agent_instance_id"] for item in instances],
        "forum_chat_id": [item["forum_chat_id"] for item in instances],
        "telegram_bot_token_fingerprint": [
            item["telegram_bot_token_fingerprint"] for item in instances
        ],
        "state_dir": [item["state_dir"] for item in instances],
        "socket_path": [item["socket_path"] for item in instances],
        "database_path": [item["database_path"] for item in instances],
        "topic_config_path": [item["topic_config_path"] for item in instances],
        "tmux_state_path": [item["tmux_state_path"] for item in instances],
        "wrapper_path": [item["wrapper_path"] for item in instances],
    }
    for field, values in identity_fields.items():
        if len(values) != len(set(str(value) for value in values)):
            errors.append(f"deployment_{field}_not_unique")
    state_dirs = [Path(str(value)) for value in identity_fields["state_dir"]]
    for index, first in enumerate(state_dirs):
        for second in state_dirs[index + 1 :]:
            if first in second.parents or second in first.parents:
                errors.append("deployment_state_dir_overlaps")
    report: dict[str, object] = {
        "schema": DEPLOYMENT_AUDIT_SCHEMA,
        "manifest_path": str(manifest_path),
        "qualification_report_id": qualification.get("report_id"),
        "release_tuple": tuple_data,
        "accepted": not errors,
        "status": "READY_FOR_STAGING" if not errors else "BLOCKED",
        "instances": instances,
        "errors": errors,
        "mutations_performed": False,
        "production_cutover_authorized": False,
    }
    report["report_digest"] = _digest(report)
    return report
