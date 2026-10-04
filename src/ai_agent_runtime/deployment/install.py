"""Side-by-side Runtime release installation without service activation."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from ai_agent_runtime.errors import RuntimeFailureError
from ai_agent_runtime.migration.inventory import InventoryError, _digest
from ai_agent_runtime.process_safety import file_sha256, validate_owned_executable

INSTALL_REPORT_SCHEMA: Final = "ai-agent-runtime.install-report.v1"
INSTALL_VERIFICATION_SCHEMA: Final = "ai-agent-runtime.install-verification.v1"
_MAX_REPORT_BYTES: Final = 1024 * 1024
_RELEASE_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
CommandRunner = Callable[[Sequence[str]], None]


def _run(command: Sequence[str]) -> None:
    subprocess.run(
        tuple(command),
        check=True,
        stdin=subprocess.DEVNULL,
        timeout=300,
    )


def _safe_releases_root(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or ".." in expanded.parts or expanded == Path("/"):
        raise InventoryError("releases root must be absolute, non-root, and traversal-free")
    if expanded.is_symlink():
        raise InventoryError("releases root must not be a symlink")
    expanded.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = expanded.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise InventoryError("releases root ownership/type/mode is unsafe")
    os.chmod(expanded, 0o700)
    return expanded.resolve(strict=True)


def _safe_wheel(path: Path, expected_sha256: str) -> tuple[Path, str]:
    expanded = path.expanduser()
    if expanded.is_symlink() or not expanded.is_file():
        raise InventoryError("Runtime wheel must be a regular non-symlink file")
    resolved = expanded.resolve(strict=True)
    actual = file_sha256(resolved)
    if actual != expected_sha256:
        raise InventoryError("Runtime wheel digest mismatch")
    return resolved, actual


def install_runtime_release(
    *,
    wheel: Path,
    wheel_sha256: str,
    releases_root: Path,
    release_id: str,
    python: Path,
    runner: CommandRunner = _run,
) -> dict[str, object]:
    """Install one exact wheel beside existing releases and never activate it."""

    if not _RELEASE_ID_RE.fullmatch(release_id):
        raise InventoryError("release id is invalid")
    wheel_path, actual_wheel_digest = _safe_wheel(wheel, wheel_sha256)
    root = _safe_releases_root(releases_root)
    python_path = validate_owned_executable(python.expanduser())
    destination = root / release_id
    if destination.exists() or destination.is_symlink():
        raise InventoryError("refusing to overwrite an existing Runtime release")
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise InventoryError("refusing to overwrite an existing Runtime release") from exc
    installing_marker = destination / ".installing"
    marker_fd = os.open(installing_marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(marker_fd)
    try:
        # A Python venv is not relocatable: generated entrypoints contain the
        # absolute interpreter path. Build it at its final, newly-created path
        # and use the marker to distinguish an incomplete release. Nothing is
        # activated merely by appearing under the side-by-side releases root.
        runner((str(python_path), "-m", "venv", str(destination)))
        # venv deliberately creates ``bin/python`` as a symlink to the base
        # interpreter. Validate the resolved target, but execute through the
        # venv path so Python preserves sys.prefix and installs into this
        # release instead of the PEP-668-protected system environment.
        environment_python = destination / "bin" / "python"
        validate_owned_executable(environment_python)
        runner(
            (
                str(environment_python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-index",
                str(wheel_path),
            )
        )
        runtime_executable = validate_owned_executable(destination / "bin" / "ai-agent-runtime")
        wrapper_executable = validate_owned_executable(destination / "bin" / "codex-wrapper")
        installing_marker.unlink()
        release_fd = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(release_fd)
        finally:
            os.close(release_fd)
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if destination.exists() and destination.parent == root and not destination.is_symlink():
            shutil.rmtree(destination)
        raise
    report: dict[str, object] = {
        "schema": INSTALL_REPORT_SCHEMA,
        "status": "INSTALLED_NOT_ACTIVATED",
        "release_id": release_id,
        "release_path": str(destination),
        "wheel_path": str(wheel_path),
        "wheel_sha256": actual_wheel_digest,
        "python_path": str(python_path),
        "runtime_executable": str(destination / "bin" / runtime_executable.name),
        "wrapper_executable": str(destination / "bin" / wrapper_executable.name),
        "existing_releases_preserved": True,
        "activation_performed": False,
        "services_restarted": False,
        "production_cutover_authorized": False,
    }
    report["report_digest"] = _digest(report)
    return report


def verify_runtime_install_report(path: Path) -> dict[str, object]:
    """Re-hash a completed side-by-side install without changing it."""

    source = path.expanduser()
    if source.is_symlink() or not source.is_file():
        raise InventoryError("Runtime install report must be a regular non-symlink file")
    if not 0 < source.stat().st_size <= _MAX_REPORT_BYTES:
        raise InventoryError("Runtime install report has unsafe size")
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InventoryError("Runtime install report is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise InventoryError("Runtime install report must be an object")
    expected_fields = {
        "schema",
        "status",
        "release_id",
        "release_path",
        "wheel_path",
        "wheel_sha256",
        "python_path",
        "runtime_executable",
        "wrapper_executable",
        "existing_releases_preserved",
        "activation_performed",
        "services_restarted",
        "production_cutover_authorized",
        "report_digest",
    }
    errors: list[str] = []
    if set(raw) != expected_fields:
        errors.append("install_report_fields_mismatch")
    if raw.get("schema") != INSTALL_REPORT_SCHEMA:
        errors.append("install_report_schema_mismatch")
    if raw.get("report_digest") != _digest(
        {key: value for key, value in raw.items() if key != "report_digest"}
    ):
        errors.append("install_report_digest_mismatch")
    if raw.get("status") != "INSTALLED_NOT_ACTIVATED":
        errors.append("install_report_status_invalid")
    for field, expected in {
        "existing_releases_preserved": True,
        "activation_performed": False,
        "services_restarted": False,
        "production_cutover_authorized": False,
    }.items():
        if raw.get(field) is not expected:
            errors.append(f"install_report_{field}_invalid")
    try:
        wheel = Path(str(raw["wheel_path"]))
        wheel_digest = str(raw["wheel_sha256"])
        if wheel.is_symlink() or not wheel.is_file() or file_sha256(wheel) != wheel_digest:
            errors.append("install_report_wheel_mismatch")
    except (KeyError, OSError, RuntimeFailureError):
        errors.append("install_report_wheel_invalid")
    try:
        release = Path(str(raw["release_path"]))
        release_id = str(raw["release_id"])
        if (
            release.is_symlink()
            or not release.is_dir()
            or release.name != release_id
            or (release / ".installing").exists()
        ):
            errors.append("install_report_release_invalid")
        runtime = validate_owned_executable(Path(str(raw["runtime_executable"])))
        wrapper = validate_owned_executable(Path(str(raw["wrapper_executable"])))
        if runtime.parent.parent != release or wrapper.parent.parent != release:
            errors.append("install_report_executable_path_mismatch")
    except (KeyError, OSError, RuntimeFailureError):
        errors.append("install_report_executable_invalid")
    verification: dict[str, object] = {
        "schema": INSTALL_VERIFICATION_SCHEMA,
        "accepted": not errors,
        "status": "VERIFIED_NOT_ACTIVATED" if not errors else "BLOCKED",
        "install_report": str(source.resolve(strict=True)),
        "release_id": raw.get("release_id"),
        "release_path": raw.get("release_path"),
        "runtime_executable": raw.get("runtime_executable"),
        "wrapper_executable": raw.get("wrapper_executable"),
        "wheel_sha256": raw.get("wheel_sha256"),
        "errors": errors,
        "mutations_performed": False,
    }
    verification["verification_digest"] = _digest(verification)
    return verification
