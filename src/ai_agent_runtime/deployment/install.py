"""Side-by-side Runtime release installation without service activation."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from ai_agent_runtime.migration.inventory import InventoryError, _digest
from ai_agent_runtime.process_safety import file_sha256, validate_owned_executable

INSTALL_REPORT_SCHEMA: Final = "ai-agent-runtime.install-report.v1"
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
    temporary = Path(tempfile.mkdtemp(prefix=f".{release_id}.", dir=root))
    try:
        runner((str(python_path), "-m", "venv", str(temporary)))
        environment_python = validate_owned_executable(temporary / "bin" / "python")
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
        runtime_executable = validate_owned_executable(temporary / "bin" / "ai-agent-runtime")
        wrapper_executable = validate_owned_executable(temporary / "bin" / "codex-wrapper")
        os.rename(temporary, destination)
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if (
            temporary.exists()
            and temporary.parent == root
            and temporary.name.startswith(f".{release_id}.")
        ):
            shutil.rmtree(temporary)
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
