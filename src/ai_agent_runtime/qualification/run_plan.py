"""Fail-closed, release-bound staging qualification run plan."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from ai_agent_runtime.deployment.install import verify_runtime_install_report
from ai_agent_runtime.migration.inventory import InventoryError, _digest
from ai_agent_runtime.qualification.release_gate import (
    QUALIFICATION_GATES,
    release_tuple_digest,
    validate_release_tuple,
)

QUALIFICATION_RUN_PLAN_SCHEMA: Final = "ai-agent-runtime.qualification-run-plan.v1"
_REPORT_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
InstallVerifier = Callable[[Path], dict[str, object]]
GitRunner = Callable[[Path, Sequence[str]], str]


def _git(checkout: Path, arguments: Sequence[str]) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _safe_directory(path: Path, label: str, *, private: bool = False) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or ".." in expanded.parts or expanded == Path("/"):
        raise InventoryError(f"{label} must be absolute, non-root, and traversal-free")
    if expanded.is_symlink() or not expanded.is_dir():
        raise InventoryError(f"{label} must be an existing non-symlink directory")
    metadata = expanded.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise InventoryError(f"{label} ownership/type/write mode is unsafe")
    if private and metadata.st_mode & 0o077:
        raise InventoryError(f"{label} must have mode 0700")
    return expanded.resolve(strict=True)


def _safe_artifact_path(path: Path, label: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or ".." in expanded.parts or expanded == Path("/"):
        raise InventoryError(f"{label} must be absolute, non-root, and traversal-free")
    if expanded.exists() or expanded.is_symlink():
        raise InventoryError(f"{label} already exists")
    parent = _safe_directory(expanded.parent, f"{label} parent", private=True)
    return parent / expanded.name


def build_staging_run_plan(
    *,
    report_id: str,
    release_tuple: object,
    release_tuple_path: Path,
    runtime_install_report: Path,
    upstream_checkout: Path,
    evidence_directory: Path,
    qualification_manifest_output: Path,
    qualification_report_output: Path,
    install_verifier: InstallVerifier = verify_runtime_install_report,
    git_runner: GitRunner = _git,
) -> dict[str, object]:
    """Verify immutable inputs and enumerate every staging gate without running it."""

    if not _REPORT_ID_RE.fullmatch(report_id):
        raise InventoryError("qualification report id is invalid")
    normalized_tuple = validate_release_tuple(release_tuple)
    tuple_source = release_tuple_path.expanduser()
    if tuple_source.is_symlink() or not tuple_source.is_file():
        raise InventoryError("release tuple must be a regular non-symlink file")
    tuple_source = tuple_source.resolve(strict=True)
    install_source = runtime_install_report.expanduser()
    verification = install_verifier(install_source)
    if verification.get("accepted") is not True:
        raise InventoryError("Runtime install report is not verified")
    if verification.get("release_id") != normalized_tuple["runtime_release"]:
        raise InventoryError("Runtime install release does not match release tuple")
    if verification.get("wheel_sha256") != normalized_tuple["runtime_wheel_sha256"]:
        raise InventoryError("Runtime install wheel does not match release tuple")

    runtime_executable = verification.get("runtime_executable")
    raw_harnesses = verification.get("qualification_executables")
    if not isinstance(runtime_executable, str) or not runtime_executable:
        raise InventoryError("verified Runtime executable is missing")
    if not isinstance(raw_harnesses, dict):
        raise InventoryError("verified qualification executables are missing")
    source_harness = raw_harnesses.get("ai-agent-runtime-qualify-upstream")
    retirement_harness = raw_harnesses.get("ai-agent-runtime-qualify-retirement")
    if not isinstance(source_harness, str) or not isinstance(retirement_harness, str):
        raise InventoryError("verified qualification executable map is incomplete")

    upstream = _safe_directory(upstream_checkout, "upstream checkout")
    expected_upstream_sha = str(normalized_tuple["telegram_agent_sha"])
    if git_runner(upstream, ("rev-parse", "HEAD")) != expected_upstream_sha:
        raise InventoryError("upstream checkout does not match release tuple")
    if git_runner(upstream, ("status", "--porcelain")):
        raise InventoryError("upstream checkout is dirty")

    evidence_root = _safe_directory(evidence_directory, "evidence directory", private=True)
    manifest_output = _safe_artifact_path(
        qualification_manifest_output, "qualification manifest output"
    )
    report_output = _safe_artifact_path(
        qualification_report_output, "qualification report output"
    )
    if manifest_output == report_output:
        raise InventoryError("qualification manifest and report outputs must differ")

    gates: list[dict[str, object]] = []
    evidence_paths: list[Path] = []
    for gate_id, assertions in sorted(QUALIFICATION_GATES.items()):
        evidence_path = evidence_root / f"{gate_id}.json"
        evidence_paths.append(evidence_path)
        harness_kind = (
            "runtime_installed"
            if gate_id in {"upstream_source_contract", "retirement_race"}
            else "external_staging_required"
        )
        gates.append(
            {
                "gate_id": gate_id,
                "required_assertion_ids": list(assertions),
                "evidence_path": str(evidence_path),
                "harness_kind": harness_kind,
                "state": "PENDING",
            }
        )

    source_evidence = evidence_root / "upstream_source_contract.json"
    retirement_evidence = evidence_root / "retirement_race.json"
    commands: dict[str, object] = {
        "upstream_source_contract": [
            source_harness,
            str(upstream),
            "--release-tuple",
            str(tuple_source),
            "--report-id",
            report_id,
            "--environment",
            "staging",
            "--output",
            str(source_evidence),
        ],
        "retirement_race": [
            retirement_harness,
            "--upstream",
            str(upstream),
            "--release-tuple",
            str(tuple_source),
            "--report-id",
            report_id,
            "--environment",
            "staging",
            "--output",
            str(retirement_evidence),
        ],
        "build_manifest_after_all_gates": [
            runtime_executable,
            "qualification",
            "manifest",
            "--report-id",
            report_id,
            "--release-tuple",
            str(tuple_source),
            *[argument for path in evidence_paths for argument in ("--evidence", str(path))],
            "--output",
            str(manifest_output),
        ],
        "verify_manifest": [
            runtime_executable,
            "qualification",
            "verify",
            "--manifest",
            str(manifest_output),
            "--output",
            str(report_output),
        ],
    }
    plan: dict[str, object] = {
        "schema": QUALIFICATION_RUN_PLAN_SCHEMA,
        "report_id": report_id,
        "environment": "staging",
        "release_tuple": normalized_tuple,
        "release_tuple_digest": release_tuple_digest(normalized_tuple),
        "release_tuple_path": str(tuple_source),
        "runtime_install_report": str(install_source.resolve(strict=True)),
        "runtime_install_verification_digest": verification.get("verification_digest"),
        "upstream_checkout": str(upstream),
        "evidence_directory": str(evidence_root),
        "gates": gates,
        "commands": commands,
        "qualification_complete": False,
        "production_cutover_authorized": False,
    }
    plan["plan_digest"] = _digest(plan)
    return plan
