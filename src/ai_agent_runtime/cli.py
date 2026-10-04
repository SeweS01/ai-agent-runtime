"""Operator CLI for Runtime lifecycle and diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.daemon.server import RuntimeServer
from ai_agent_runtime.deployment.audit import audit_deployment_manifest
from ai_agent_runtime.health.doctor import collect_doctor_report
from ai_agent_runtime.ipc.client import request
from ai_agent_runtime.ipc.protocol import versioned
from ai_agent_runtime.migration.binding_plan import build_binding_migration_plan
from ai_agent_runtime.migration.inventory import (
    InventoryError,
    build_inventory,
    compare_inventories,
    read_inventory,
    write_inventory,
    write_json_artifact,
)
from ai_agent_runtime.migration.session_storage import (
    build_session_storage_manifest,
    stage_session_storage,
)
from ai_agent_runtime.qualification.release_gate import (
    build_qualification_manifest,
    verify_qualification_manifest,
)
from ai_agent_runtime.state.store import RuntimeStore


def _load_config(path: str) -> RuntimeConfig:
    config = RuntimeConfig.load(Path(path).expanduser())
    config.validate()
    return config


async def _status(config: RuntimeConfig) -> int:
    response = await request(
        config.socket_path,
        versioned("status", str(uuid.uuid4())),
    )
    print(json.dumps(response, indent=2, sort_keys=True))
    return 0 if response.get("ok") else 1


async def _doctor(config: RuntimeConfig) -> int:
    report = await collect_doctor_report(config)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["healthy"] is True else 1


def _local_summary(config: RuntimeConfig) -> int:
    store = RuntimeStore(config.database_path)
    try:
        print(json.dumps(store.summary(), indent=2, sort_keys=True))
    finally:
        store.close()
    return 0


def _inventory_export(
    config: RuntimeConfig,
    *,
    output: str,
    cus_control_store: str | None,
    skip_live_tmux: bool,
) -> int:
    live_tmux = (
        {"available": False, "reason": "operator_skipped", "panes": []} if skip_live_tmux else None
    )
    inventory = build_inventory(
        config,
        cus_control_store=Path(cus_control_store).expanduser() if cus_control_store else None,
        live_tmux=live_tmux,
    )
    target = Path(output).expanduser()
    write_inventory(target, inventory)
    print(json.dumps({"output": str(target), "inventory_digest": inventory["inventory_digest"]}))
    return 0


def _inventory_compare(*, baseline: str, candidate: str, output: str | None) -> int:
    report = compare_inventories(
        read_inventory(Path(baseline).expanduser()),
        read_inventory(Path(candidate).expanduser()),
    )
    if output:
        write_json_artifact(Path(output).expanduser(), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["accepted"] else 1


def _inventory_plan_bindings(*, inventory: str, generation_id: str, output: str) -> int:
    plan = build_binding_migration_plan(
        read_inventory(Path(inventory).expanduser()),
        generation_id=generation_id,
    )
    target = Path(output).expanduser()
    write_json_artifact(target, plan)
    print(
        json.dumps(
            {
                "output": str(target),
                "plan_digest": plan["plan_digest"],
                "accepted": plan["accepted"],
                "counts": plan["counts"],
            },
            sort_keys=True,
        )
    )
    return 0 if plan["accepted"] else 1


def _parse_profiles(values: list[str]) -> dict[str, Path]:
    profiles: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or not name or not raw_path or name in profiles:
            raise InventoryError("--profile must be a unique NAME=/absolute/path pair")
        profiles[name] = Path(raw_path).expanduser()
    return profiles


def _storage_manifest(
    *,
    profiles: list[str],
    shared_root: str,
    includes: list[str],
    output: str,
) -> int:
    manifest = build_session_storage_manifest(
        profiles=_parse_profiles(profiles),
        shared_root=Path(shared_root).expanduser(),
        includes=tuple(includes) if includes else ("sessions", "archived_sessions"),
    )
    target = Path(output).expanduser()
    write_json_artifact(target, manifest)
    print(
        json.dumps(
            {
                "output": str(target),
                "manifest_digest": manifest["manifest_digest"],
                "status": manifest["status"],
                "qualified": False,
            },
            sort_keys=True,
        )
    )
    return 0 if not manifest["collisions"] else 1


def _storage_stage(*, manifest: str, output: str) -> int:
    target = Path(output).expanduser()
    if target.exists() or target.is_symlink():
        raise InventoryError(f"refusing to overwrite stage report: {target}")
    report = stage_session_storage(Path(manifest).expanduser())
    write_json_artifact(target, report)
    print(
        json.dumps(
            {
                "output": str(target),
                "status": report["status"],
                "stage_digest": report["stage_digest"],
                "activation_performed": False,
            },
            sort_keys=True,
        )
    )
    return 0


def _read_release_tuple(path: str) -> dict[str, object]:
    source = Path(path).expanduser()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InventoryError(f"release tuple is not valid JSON: {source}") from exc
    if not isinstance(value, dict):
        raise InventoryError("release tuple must be a JSON object")
    return value


def _qualification_manifest(
    *, report_id: str, release_tuple: str, evidence: list[str], output: str
) -> int:
    manifest = build_qualification_manifest(
        report_id=report_id,
        release_tuple=_read_release_tuple(release_tuple),
        evidence_paths=[Path(path) for path in evidence],
    )
    target = Path(output).expanduser()
    write_json_artifact(target, manifest)
    print(
        json.dumps(
            {
                "output": str(target),
                "manifest_digest": manifest["manifest_digest"],
                "qualified": False,
            },
            sort_keys=True,
        )
    )
    return 0


def _qualification_verify(*, manifest: str, output: str | None) -> int:
    report = verify_qualification_manifest(Path(manifest))
    if output:
        write_json_artifact(Path(output).expanduser(), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["accepted"] else 1


def _deployment_audit(*, manifest: str, output: str | None) -> int:
    report = audit_deployment_manifest(Path(manifest))
    if output:
        write_json_artifact(Path(output).expanduser(), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["accepted"] else 1


def main() -> None:
    default_config = os.environ.get(
        "AI_AGENT_RUNTIME_CONFIG",
        str(Path.home() / ".config" / "ai-agent-runtime" / "config.toml"),
    )
    parser = argparse.ArgumentParser(prog="ai-agent-runtime")
    parser.add_argument("--config", default=default_config)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    commands.add_parser("status")
    commands.add_parser("local-summary")
    commands.add_parser("doctor")
    inventory = commands.add_parser("inventory")
    inventory_commands = inventory.add_subparsers(dest="inventory_command", required=True)
    inventory_export = inventory_commands.add_parser("export")
    inventory_export.add_argument("--output", required=True)
    inventory_export.add_argument("--cus-control-store")
    inventory_export.add_argument("--skip-live-tmux", action="store_true")
    inventory_compare = inventory_commands.add_parser("compare")
    inventory_compare.add_argument("--baseline", required=True)
    inventory_compare.add_argument("--candidate", required=True)
    inventory_compare.add_argument("--output")
    inventory_plan = inventory_commands.add_parser("plan-bindings")
    inventory_plan.add_argument("--inventory", required=True)
    inventory_plan.add_argument("--generation-id", required=True)
    inventory_plan.add_argument("--output", required=True)
    storage = commands.add_parser("storage")
    storage_commands = storage.add_subparsers(dest="storage_command", required=True)
    storage_manifest = storage_commands.add_parser("manifest")
    storage_manifest.add_argument("--profile", action="append", default=[], required=True)
    storage_manifest.add_argument("--shared-root", required=True)
    storage_manifest.add_argument("--include-relative", action="append", default=[])
    storage_manifest.add_argument("--output", required=True)
    storage_stage = storage_commands.add_parser("stage")
    storage_stage.add_argument("--manifest", required=True)
    storage_stage.add_argument("--output", required=True)
    qualification = commands.add_parser("qualification")
    qualification_commands = qualification.add_subparsers(
        dest="qualification_command", required=True
    )
    qualification_manifest = qualification_commands.add_parser("manifest")
    qualification_manifest.add_argument("--report-id", required=True)
    qualification_manifest.add_argument("--release-tuple", required=True)
    qualification_manifest.add_argument("--evidence", action="append", default=[], required=True)
    qualification_manifest.add_argument("--output", required=True)
    qualification_verify = qualification_commands.add_parser("verify")
    qualification_verify.add_argument("--manifest", required=True)
    qualification_verify.add_argument("--output")
    deployment = commands.add_parser("deployment")
    deployment_commands = deployment.add_subparsers(dest="deployment_command", required=True)
    deployment_audit = deployment_commands.add_parser("audit")
    deployment_audit.add_argument("--manifest", required=True)
    deployment_audit.add_argument("--output")
    namespace = parser.parse_args()
    try:
        # Artifact-only commands intentionally work on an isolated staging host
        # without a live Runtime configuration or daemon.
        if namespace.command == "qualification":
            if namespace.qualification_command == "manifest":
                raise SystemExit(
                    _qualification_manifest(
                        report_id=namespace.report_id,
                        release_tuple=namespace.release_tuple,
                        evidence=namespace.evidence,
                        output=namespace.output,
                    )
                )
            if namespace.qualification_command == "verify":
                raise SystemExit(
                    _qualification_verify(
                        manifest=namespace.manifest,
                        output=namespace.output,
                    )
                )
        if namespace.command == "storage" and namespace.storage_command == "manifest":
            raise SystemExit(
                _storage_manifest(
                    profiles=namespace.profile,
                    shared_root=namespace.shared_root,
                    includes=namespace.include_relative,
                    output=namespace.output,
                )
            )
        if namespace.command == "storage" and namespace.storage_command == "stage":
            raise SystemExit(
                _storage_stage(
                    manifest=namespace.manifest,
                    output=namespace.output,
                )
            )
        if namespace.command == "deployment" and namespace.deployment_command == "audit":
            raise SystemExit(
                _deployment_audit(
                    manifest=namespace.manifest,
                    output=namespace.output,
                )
            )
        if namespace.command == "inventory":
            if namespace.inventory_command == "compare":
                raise SystemExit(
                    _inventory_compare(
                        baseline=namespace.baseline,
                        candidate=namespace.candidate,
                        output=namespace.output,
                    )
                )
            if namespace.inventory_command == "plan-bindings":
                raise SystemExit(
                    _inventory_plan_bindings(
                        inventory=namespace.inventory,
                        generation_id=namespace.generation_id,
                        output=namespace.output,
                    )
                )
        config = _load_config(namespace.config)
        if namespace.command == "serve":
            asyncio.run(RuntimeServer(config).serve())
            return
        if namespace.command == "status":
            raise SystemExit(asyncio.run(_status(config)))
        if namespace.command == "local-summary":
            raise SystemExit(_local_summary(config))
        if namespace.command == "doctor":
            raise SystemExit(asyncio.run(_doctor(config)))
        if namespace.command == "inventory" and namespace.inventory_command == "export":
            raise SystemExit(
                _inventory_export(
                    config,
                    output=namespace.output,
                    cus_control_store=namespace.cus_control_store,
                    skip_live_tmux=namespace.skip_live_tmux,
                )
            )
    except (InventoryError, OSError, ValueError) as exc:
        print(f"ai-agent-runtime: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
