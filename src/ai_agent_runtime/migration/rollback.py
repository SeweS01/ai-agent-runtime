"""Fail-closed preservation manifest for the legacy rollback path.

The manifest deliberately performs no restore, service operation, archive deletion,
or production mutation.  It proves that the exact legacy checkout and operator-
selected, non-secret rollback files still exist unchanged while the control period
is active.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from ai_agent_runtime.migration.inventory import InventoryError, _digest

ROLLBACK_MANIFEST_SCHEMA: Final = "ai-agent-runtime.rollback-manifest.v1"
ROLLBACK_VERIFICATION_SCHEMA: Final = "ai-agent-runtime.rollback-verification.v1"
_MAX_MANIFEST_BYTES: Final = 16 * 1024 * 1024
# Qualified provider binaries can exceed 256 MiB. Hashing is streaming and the
# one-GiB ceiling remains a fail-closed bound rather than an in-memory read limit.
_MAX_ARTIFACT_BYTES: Final = 1024 * 1024 * 1024
_GIT_SHA_RE: Final = re.compile(r"[0-9a-f]{40}\Z")
_SAFE_NAME_RE: Final = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
_FORBIDDEN_PATH_NAMES: Final = {
    ".env",
    "auth.json",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
    "known_hosts",
}
_FORBIDDEN_SUFFIXES: Final = {".key", ".pem", ".p12", ".pfx"}


def _run_git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InventoryError(f"cannot inspect legacy Git checkout: {type(exc).__name__}") from exc
    if result.returncode != 0:
        raise InventoryError(f"legacy Git checkout rejected by {' '.join(args)}")
    return result.stdout.strip()


def _validate_repo(path: Path) -> tuple[Path, dict[str, object]]:
    expanded = path.expanduser()
    if not expanded.is_absolute() or expanded == Path("/") or expanded.is_symlink():
        raise InventoryError("legacy repository must be an absolute non-root, non-symlink path")
    if not expanded.is_dir():
        raise InventoryError(f"legacy repository is unavailable: {expanded}")
    resolved = expanded.resolve(strict=True)
    top_level = Path(_run_git(resolved, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if top_level != resolved:
        raise InventoryError("legacy repository path must be the exact Git worktree root")
    head = _run_git(resolved, "rev-parse", "HEAD")
    if not _GIT_SHA_RE.fullmatch(head):
        raise InventoryError("legacy repository HEAD is not an exact commit")
    dirty = _run_git(resolved, "status", "--porcelain=v1", "--untracked-files=no")
    if dirty:
        raise InventoryError("legacy repository has tracked worktree changes")
    tree = _run_git(resolved, "rev-parse", "HEAD^{tree}")
    origin = _run_git(resolved, "remote", "get-url", "origin")
    if not origin:
        raise InventoryError("legacy repository has no origin remote")
    return resolved, {
        "path": str(resolved),
        "head": head,
        "tree": tree,
        # A remote may embed credentials.  Store only a one-way identity proof.
        "origin_fingerprint": f"sha256:{hashlib.sha256(origin.encode()).hexdigest()}",
        "tracked_worktree_clean": True,
    }


def _safe_artifact_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or expanded == Path("/") or expanded.is_symlink():
        raise InventoryError("rollback artifacts must be absolute regular non-symlink files")
    lowered = {part.lower() for part in expanded.parts}
    if lowered & _FORBIDDEN_PATH_NAMES or expanded.suffix.lower() in _FORBIDDEN_SUFFIXES:
        raise InventoryError(f"rollback artifact path may contain secret material: {expanded}")
    if not expanded.is_file():
        raise InventoryError(f"rollback artifact is unavailable: {expanded}")
    metadata = expanded.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_ARTIFACT_BYTES:
        raise InventoryError(f"rollback artifact is not a bounded regular file: {expanded}")
    return expanded.resolve(strict=True)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_ARTIFACT_BYTES:
            raise InventoryError(f"rollback artifact changed type or size: {path}")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return f"sha256:{digest.hexdigest()}"


def _symlink_artifact_record(name: str, path: Path) -> dict[str, object]:
    link_before = path.lstat()
    if not stat.S_ISLNK(link_before.st_mode):
        raise InventoryError("rollback launcher path is not a symlink")
    link_target = os.readlink(path)
    resolved = path.resolve(strict=True)
    target = _safe_artifact_path(resolved)
    target_before = target.stat()
    digest = _file_digest(target)
    target_after = target.stat()
    link_after = path.lstat()
    if (
        os.readlink(path) != link_target
        or (link_before.st_dev, link_before.st_ino, link_before.st_mtime_ns)
        != (link_after.st_dev, link_after.st_ino, link_after.st_mtime_ns)
        or (
            target_before.st_dev,
            target_before.st_ino,
            target_before.st_size,
            target_before.st_mtime_ns,
        )
        != (
            target_after.st_dev,
            target_after.st_ino,
            target_after.st_size,
            target_after.st_mtime_ns,
        )
    ):
        raise InventoryError("rollback launcher symlink changed while hashing")
    return {
        "name": name,
        "path": str(path),
        "type": "symlink",
        "link_target": link_target,
        "link_mode": stat.S_IMODE(link_after.st_mode),
        "link_uid": link_after.st_uid,
        "link_gid": link_after.st_gid,
        "link_device": link_after.st_dev,
        "link_inode": link_after.st_ino,
        "resolved_path": str(target),
        "sha256": digest,
        "size": target_after.st_size,
        "mode": stat.S_IMODE(target_after.st_mode),
        "uid": target_after.st_uid,
        "gid": target_after.st_gid,
        "device": target_after.st_dev,
        "inode": target_after.st_ino,
    }


def _artifact_record(
    name: str,
    path: Path,
    *,
    allow_symlink: bool = False,
) -> dict[str, object]:
    if not _SAFE_NAME_RE.fullmatch(name):
        raise InventoryError("rollback artifact names must be simple and unique")
    expanded = path.expanduser()
    if (
        not expanded.is_absolute()
        or expanded == Path("/")
        or {part.lower() for part in expanded.parts} & _FORBIDDEN_PATH_NAMES
        or expanded.suffix.lower() in _FORBIDDEN_SUFFIXES
    ):
        raise InventoryError(f"rollback artifact path may contain secret material: {expanded}")
    if expanded.is_symlink():
        if not allow_symlink:
            raise InventoryError("rollback artifacts must be regular non-symlink files")
        return _symlink_artifact_record(name, expanded)
    source = _safe_artifact_path(path)
    before = source.stat()
    digest = _file_digest(source)
    after = source.stat()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise InventoryError(f"rollback artifact changed while hashing: {source}")
    return {
        "name": name,
        "path": str(source),
        "type": "regular",
        "sha256": digest,
        "size": after.st_size,
        "mode": stat.S_IMODE(after.st_mode),
        "uid": after.st_uid,
        "gid": after.st_gid,
        "device": after.st_dev,
        "inode": after.st_ino,
    }


def build_rollback_manifest(
    *,
    legacy_repo: Path,
    legacy_service: str,
    legacy_codex_bin: Path,
    artifacts: dict[str, Path],
    control_period_end: str,
) -> dict[str, object]:
    """Pin a rollback path without copying secrets or changing live state."""

    if not legacy_service.strip() or any(character.isspace() for character in legacy_service):
        raise InventoryError("legacy service must be one non-empty systemd unit name")
    codex_bin = legacy_codex_bin.expanduser()
    if not codex_bin.is_absolute() or codex_bin == Path("/"):
        raise InventoryError("legacy TELEGRAM_CODEX_BIN must be an absolute non-root path")
    try:
        period_end = datetime.fromisoformat(control_period_end.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InventoryError("control period end must be an ISO-8601 timestamp") from exc
    if period_end.tzinfo is None:
        raise InventoryError("control period end must include a timezone")
    if period_end.astimezone(UTC) <= datetime.now(UTC):
        raise InventoryError("control period end must be in the future")
    normalized_end = period_end.astimezone(UTC).isoformat().replace("+00:00", "Z")
    repo, repo_record = _validate_repo(legacy_repo)
    if "legacy_codex_bin" in artifacts:
        raise InventoryError("legacy_codex_bin is a reserved rollback artifact name")
    if not artifacts:
        raise InventoryError("at least one additional non-secret rollback artifact is required")
    if len(set(artifacts)) != len(artifacts):
        raise InventoryError("rollback artifact names must be unique")
    records = [_artifact_record("legacy_codex_bin", codex_bin, allow_symlink=True)]
    records.extend(_artifact_record(name, path) for name, path in sorted(artifacts.items()))
    manifest: dict[str, object] = {
        "schema": ROLLBACK_MANIFEST_SCHEMA,
        "status": "PINNED_NOT_DRILLED",
        "created_at": datetime.now(UTC).isoformat(),
        "control_period_end": normalized_end,
        "legacy": {
            "repository": repo_record,
            "systemd_service": legacy_service,
            "telegram_codex_bin": str(codex_bin),
        },
        "artifacts": records,
        "policy": {
            "repository_delete_allowed": False,
            "artifact_delete_allowed": False,
            "production_restore_authorized": False,
            "requires_staging_rollback_drill": True,
            "requires_control_period_completion": True,
            "requires_owner_cleanup_decision": True,
        },
        "safety": {
            "source_repo_mutated": False,
            "services_mutated": False,
            "files_restored": False,
            "files_deleted": False,
            "secret_contents_recorded": False,
        },
    }
    # Retain the resolved path only as evidence; no archive/package is silently created.
    assert str(repo) == str(repo_record["path"])
    manifest["manifest_digest"] = _digest(manifest)
    return manifest


def _read_manifest(path: Path) -> dict[str, Any]:
    source = path.expanduser()
    if source.is_symlink() or not source.is_file():
        raise InventoryError("rollback manifest must be a regular non-symlink file")
    size = source.stat().st_size
    if size <= 0 or size > _MAX_MANIFEST_BYTES:
        raise InventoryError("rollback manifest has unsafe size")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InventoryError("rollback manifest is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != ROLLBACK_MANIFEST_SCHEMA:
        raise InventoryError("unsupported rollback manifest schema")
    supplied = value.get("manifest_digest")
    unsigned = {key: item for key, item in value.items() if key != "manifest_digest"}
    if supplied != _digest(unsigned):
        raise InventoryError("rollback manifest digest verification failed")
    return value


def verify_rollback_manifest(path: Path) -> dict[str, object]:
    """Revalidate pinned sources; never restore or mutate them."""

    manifest = _read_manifest(path)
    failures: list[str] = []
    raw_period_end = manifest.get("control_period_end")
    try:
        period_end = datetime.fromisoformat(str(raw_period_end).replace("Z", "+00:00"))
    except ValueError:
        period_end = None
        failures.append("rollback control period timestamp is invalid")
    if period_end is not None and (
        period_end.tzinfo is None or period_end.astimezone(UTC) <= datetime.now(UTC)
    ):
        failures.append("rollback control period is not active")
    legacy = manifest.get("legacy")
    repository = legacy.get("repository") if isinstance(legacy, dict) else None
    if not isinstance(repository, dict):
        raise InventoryError("rollback manifest legacy repository is invalid")
    try:
        _resolved, observed_repo = _validate_repo(Path(str(repository.get("path", ""))))
    except InventoryError as exc:
        failures.append(str(exc))
    else:
        for field in ("path", "head", "tree", "origin_fingerprint", "tracked_worktree_clean"):
            if observed_repo.get(field) != repository.get(field):
                failures.append(f"legacy repository {field} changed")

    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise InventoryError("rollback manifest artifacts are invalid")
    checked_artifacts: list[dict[str, object]] = []
    for raw in raw_artifacts:
        if not isinstance(raw, dict):
            raise InventoryError("rollback manifest artifact entry is invalid")
        name = raw.get("name")
        if not isinstance(name, str):
            raise InventoryError("rollback manifest artifact name is invalid")
        try:
            observed = _artifact_record(
                name,
                Path(str(raw.get("path", ""))),
                allow_symlink=name == "legacy_codex_bin" and raw.get("type") == "symlink",
            )
        except InventoryError as exc:
            failures.append(str(exc))
            continue
        for field in set(raw) - {"name"}:
            if observed.get(field) != raw.get(field):
                failures.append(f"rollback artifact {name} {field} changed")
        checked_artifacts.append({"name": name, "sha256": observed["sha256"]})

    report: dict[str, object] = {
        "schema": ROLLBACK_VERIFICATION_SCHEMA,
        "status": "VERIFIED_NOT_DRILLED" if not failures else "ROLLBACK_PATH_INVALID",
        "accepted": not failures,
        "manifest_path": str(path.expanduser().resolve(strict=True)),
        "manifest_digest": manifest["manifest_digest"],
        "control_period_end": manifest.get("control_period_end"),
        "control_period_active": not any("control period" in item for item in failures),
        "checked_artifacts": checked_artifacts,
        "failures": failures,
        "production_restore_authorized": False,
        "cleanup_authorized": False,
        "mutations_performed": False,
    }
    report["verification_digest"] = _digest(report)
    return report
