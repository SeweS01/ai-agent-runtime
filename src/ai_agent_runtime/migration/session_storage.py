"""Non-destructive session-storage topology manifest and collision gate."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, cast

from ai_agent_runtime.migration.inventory import InventoryError, _digest

SESSION_STORAGE_SCHEMA = "ai-agent-runtime.session-storage-manifest.v1"
SESSION_STORAGE_STAGE_SCHEMA = "ai-agent-runtime.session-storage-stage.v1"
DEFAULT_INCLUDES = ("sessions", "archived_sessions")
_FORBIDDEN_NAMES = {
    ".env",
    "auth.json",
    "credentials",
    "credentials.json",
    "config.toml",
}
_MAX_NODES = 250_000
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024


def _relative_path(value: str) -> PurePosixPath:
    candidate = PurePosixPath(value)
    if not value or candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
        raise InventoryError(f"session storage include is unsafe: {value}")
    if any(part in _FORBIDDEN_NAMES for part in candidate.parts):
        raise InventoryError(f"session storage include targets credential material: {value}")
    return candidate


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise InventoryError(f"session storage file cannot be opened safely: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise InventoryError(f"session storage node stopped being a regular file: {path}")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise InventoryError(f"session storage file changed while hashing: {path}")
    return f"sha256:{digest.hexdigest()}"


def _node_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "unsupported"


def _scan_path(root: Path, relative: PurePosixPath) -> dict[str, object]:
    path = root.joinpath(*relative.parts)
    if not path.exists() and not path.is_symlink():
        return {
            "relative_path": str(relative),
            "source_type": "missing",
            "entries": [],
            "file_count": 0,
            "byte_count": 0,
            "tree_digest": _digest([]),
        }
    root_stat = path.lstat()
    source_type = _node_kind(root_stat.st_mode)
    if source_type == "unsupported":
        raise InventoryError(f"unsupported session storage node: {path}")
    entries: list[dict[str, object]] = []

    def visit(current: Path, rel: PurePosixPath) -> None:
        if len(entries) >= _MAX_NODES:
            raise InventoryError("session storage manifest exceeds node limit")
        metadata = current.lstat()
        kind = _node_kind(metadata.st_mode)
        if kind == "unsupported":
            raise InventoryError(f"unsupported session storage node: {current}")
        record: dict[str, object] = {
            "path": str(rel),
            "type": kind,
            "mode": stat.S_IMODE(metadata.st_mode),
            "size": metadata.st_size if kind == "file" else 0,
        }
        if kind == "file":
            record["sha256"] = _file_digest(current)
        elif kind == "symlink":
            record["target"] = os.readlink(current)
        entries.append(record)
        if kind != "directory":
            return
        with os.scandir(current) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
        for child in children:
            if child.name in _FORBIDDEN_NAMES:
                raise InventoryError(
                    f"credential material appears inside included storage: {child.path}"
                )
            visit(Path(child.path), rel / child.name)
        after = current.lstat()
        if (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mtime_ns,
        ):
            raise InventoryError(f"session storage directory changed while scanning: {current}")

    visit(path, relative)
    files = [entry for entry in entries if entry["type"] == "file"]
    return {
        "relative_path": str(relative),
        "source_type": source_type,
        "entries": entries,
        "file_count": len(files),
        "byte_count": sum(cast(int, entry["size"]) for entry in files),
        "tree_digest": _digest(entries),
    }


def _validate_root(path: Path, *, must_exist: bool) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or ".." in expanded.parts or expanded == Path("/"):
        raise InventoryError("session storage root must be absolute, non-root and safe")
    if expanded.is_symlink():
        raise InventoryError("session storage root must not be a symlink")
    if must_exist and not expanded.is_dir():
        raise InventoryError(f"session storage profile root is unavailable: {expanded}")
    if expanded.exists() and not expanded.is_dir():
        raise InventoryError(f"session storage root is not a directory: {expanded}")
    return expanded.resolve(strict=must_exist)


def build_session_storage_manifest(
    *,
    profiles: dict[str, Path],
    shared_root: Path,
    includes: tuple[str, ...] = DEFAULT_INCLUDES,
) -> dict[str, object]:
    """Describe a candidate shared topology without changing any filesystem path."""

    if not profiles:
        raise InventoryError("at least one account profile is required")
    if any(not name or "/" in name or name in {".", ".."} for name in profiles):
        raise InventoryError("account profile names must be simple non-empty names")
    normalized_includes = tuple(_relative_path(value) for value in includes)
    if not normalized_includes or len(set(normalized_includes)) != len(normalized_includes):
        raise InventoryError("session storage includes must be unique and non-empty")
    target_root = _validate_root(shared_root, must_exist=False)
    profile_scans: list[dict[str, object]] = []
    target_owners: dict[str, list[dict[str, object]]] = defaultdict(list)

    for profile_name, raw_root in sorted(profiles.items()):
        profile_root = _validate_root(raw_root, must_exist=True)
        scans = [_scan_path(profile_root, include) for include in normalized_includes]
        profile_scans.append(
            {
                "account_profile": profile_name,
                "profile_root": str(profile_root),
                "includes": scans,
            }
        )
        for scan in scans:
            entries = scan["entries"]
            assert isinstance(entries, list)
            for entry in entries:
                assert isinstance(entry, dict)
                if entry.get("type") != "file":
                    continue
                target_owners[str(entry["path"])].append(
                    {
                        "owner": profile_name,
                        "sha256": entry.get("sha256"),
                        "size": entry.get("size"),
                    }
                )

    target_scans: list[dict[str, object]] = []
    if target_root.exists():
        target_scans = [_scan_path(target_root, include) for include in normalized_includes]
        for scan in target_scans:
            entries = scan["entries"]
            assert isinstance(entries, list)
            for entry in entries:
                assert isinstance(entry, dict)
                if entry.get("type") != "file":
                    continue
                target_owners[str(entry["path"])].append(
                    {
                        "owner": "existing-target",
                        "sha256": entry.get("sha256"),
                        "size": entry.get("size"),
                    }
                )

    collisions = [
        {"target_relative_path": path, "sources": owners}
        for path, owners in sorted(target_owners.items())
        if len(owners) > 1
    ]
    operations: list[dict[str, object]] = []
    for profile in profile_scans:
        profile_includes = profile["includes"]
        assert isinstance(profile_includes, list)
        for scan in profile_includes:
            assert isinstance(scan, dict)
            source = Path(str(profile["profile_root"])) / str(scan["relative_path"])
            target = target_root / str(scan["relative_path"])
            operations.append(
                {
                    "account_profile": profile["account_profile"],
                    "source": str(source),
                    "source_type": scan["source_type"],
                    "target": str(target),
                    "file_count": scan["file_count"],
                    "byte_count": scan["byte_count"],
                    "tree_digest": scan["tree_digest"],
                    "collision_policy": "BLOCK",
                    "rollback": {
                        "restore_path": str(source),
                        "restore_type": scan["source_type"],
                        "source_tree_digest": scan["tree_digest"],
                        "never_delete_new_target_files": True,
                    },
                }
            )

    manifest: dict[str, object] = {
        "schema": SESSION_STORAGE_SCHEMA,
        "status": "BLOCKED_COLLISIONS" if collisions else "READY_FOR_QUALIFICATION",
        "qualified": False,
        "shared_root_candidate": str(target_root),
        "includes": [str(item) for item in normalized_includes],
        "profiles": profile_scans,
        "existing_target": target_scans,
        "operations": operations,
        "collisions": collisions,
        "safety": {
            "destructive_actions_performed": False,
            "replace_nonempty_directory_with_symlink": False,
            "requires_staged_copy_and_post_copy_verification": True,
            "requires_real_codex_lifecycle_qualification": True,
        },
    }
    manifest["manifest_digest"] = _digest(manifest)
    return manifest


def read_session_storage_manifest(path: Path) -> dict[str, Any]:
    """Read and authenticate a manifest before any staging mutation."""

    expanded = path.expanduser()
    if expanded.is_symlink() or not expanded.is_file():
        raise InventoryError("session storage manifest must be a regular non-symlink file")
    size = expanded.stat().st_size
    if size <= 0 or size > _MAX_MANIFEST_BYTES:
        raise InventoryError("session storage manifest has unsafe size")
    try:
        value = json.loads(expanded.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InventoryError("session storage manifest is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != SESSION_STORAGE_SCHEMA:
        raise InventoryError("unsupported session storage manifest schema")
    supplied = value.get("manifest_digest")
    unsigned = {key: item for key, item in value.items() if key != "manifest_digest"}
    if supplied != _digest(unsigned):
        raise InventoryError("session storage manifest digest verification failed")
    if value.get("status") != "READY_FOR_QUALIFICATION" or value.get("collisions") != []:
        raise InventoryError("session storage manifest is blocked by collisions")
    if value.get("qualified") is not False:
        raise InventoryError("staging expects a pre-qualification manifest")
    return value


def _manifest_sources(
    manifest: dict[str, Any],
) -> tuple[dict[str, tuple[Path, str]], set[str], list[dict[str, object]]]:
    raw_profiles = manifest.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise InventoryError("session storage manifest has no profiles")
    files: dict[str, tuple[Path, str]] = {}
    directories: set[str] = set()
    verified_sources: list[dict[str, object]] = []
    for raw_profile in raw_profiles:
        if not isinstance(raw_profile, dict):
            raise InventoryError("session storage profile entry is invalid")
        profile_root = _validate_root(
            Path(str(raw_profile.get("profile_root", ""))), must_exist=True
        )
        raw_includes = raw_profile.get("includes")
        if not isinstance(raw_includes, list):
            raise InventoryError("session storage profile includes are invalid")
        for raw_scan in raw_includes:
            if not isinstance(raw_scan, dict):
                raise InventoryError("session storage scan entry is invalid")
            relative = _relative_path(str(raw_scan.get("relative_path", "")))
            observed = _scan_path(profile_root, relative)
            for field in ("source_type", "file_count", "byte_count", "tree_digest"):
                if observed[field] != raw_scan.get(field):
                    raise InventoryError(
                        "session storage source changed after manifest: "
                        f"{profile_root / str(relative)}"
                    )
            entries = observed["entries"]
            assert isinstance(entries, list)
            for entry in entries:
                assert isinstance(entry, dict)
                raw_path = str(entry.get("path", ""))
                safe_path = _relative_path(raw_path)
                kind = entry.get("type")
                if kind == "symlink":
                    raise InventoryError(
                        f"staging refuses session-storage symlink: {profile_root / raw_path}"
                    )
                if kind == "directory":
                    directories.add(str(safe_path))
                elif kind == "file":
                    digest = entry.get("sha256")
                    if not isinstance(digest, str):
                        raise InventoryError("session storage file digest is missing")
                    key = str(safe_path)
                    if key in files:
                        raise InventoryError(f"session storage staging collision: {key}")
                    files[key] = (profile_root / key, digest)
            verified_sources.append(
                {
                    "account_profile": raw_profile.get("account_profile"),
                    "relative_path": str(relative),
                    "source_tree_digest": observed["tree_digest"],
                }
            )
    for file_relative in files:
        if file_relative in directories or any(
            str(parent) in files
            for parent in PurePosixPath(file_relative).parents
            if str(parent) != "."
        ):
            raise InventoryError(f"session storage file/directory collision: {file_relative}")
    return files, directories, verified_sources


def _copy_verified_file(source: Path, target: Path, expected_digest: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    target_fd: int | None = None
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(source_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise InventoryError(f"session storage source stopped being a file: {source}")
        target_fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        while chunk := os.read(source_fd, 1024 * 1024):
            digest.update(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(target_fd, remaining)
                if written <= 0:
                    raise OSError("short write while staging session storage")
                remaining = remaining[written:]
        os.fsync(target_fd)
        observed = f"sha256:{digest.hexdigest()}"
        if observed != expected_digest:
            raise InventoryError(f"session storage file changed while copying: {source}")
    finally:
        os.close(source_fd)
        if target_fd is not None:
            os.close(target_fd)


def stage_session_storage(manifest_path: Path) -> dict[str, object]:
    """Copy manifest-pinned session data to a fresh target without activation.

    Source profiles are never renamed, linked, or removed. The target is published
    by one atomic rename only after every copied file matches its manifest digest.
    """

    manifest = read_session_storage_manifest(manifest_path)
    raw_target = manifest.get("shared_root_candidate")
    if not isinstance(raw_target, str):
        raise InventoryError("session storage target is missing")
    target = _validate_root(Path(raw_target), must_exist=False)
    if target.exists() or target.is_symlink():
        raise InventoryError("session storage staging target must not already exist")
    parent = target.parent.resolve(strict=True)
    parent_metadata = parent.stat()
    if parent_metadata.st_uid != os.getuid() or parent_metadata.st_mode & stat.S_IWOTH:
        raise InventoryError("session storage target parent ownership/mode is unsafe")
    files, directories, verified_sources = _manifest_sources(manifest)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=parent))
    os.chmod(staging, 0o700)
    try:
        for relative in sorted(directories, key=lambda item: (item.count("/"), item)):
            directory = staging.joinpath(*PurePosixPath(relative).parts)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
        for relative, (source, digest) in sorted(files.items()):
            destination = staging.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(destination.parent, 0o700)
            _copy_verified_file(source, destination, digest)
        for relative, (_source, digest) in files.items():
            destination = staging.joinpath(*PurePosixPath(relative).parts)
            if _file_digest(destination) != digest:
                raise InventoryError(f"staged session storage verification failed: {relative}")
        staged_files = {
            str(path.relative_to(staging).as_posix())
            for path in staging.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        if staged_files != set(files):
            raise InventoryError("staged session storage file inventory differs from manifest")
        # Re-scan all sources after copying to detect concurrent mutation.
        _manifest_sources(manifest)
        os.replace(staging, target)
        parent_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    includes = manifest.get("includes")
    if not isinstance(includes, list):
        raise InventoryError("session storage manifest includes are invalid")
    target_scans = [_scan_path(target, _relative_path(str(value))) for value in includes]
    report: dict[str, object] = {
        "schema": SESSION_STORAGE_STAGE_SCHEMA,
        "status": "STAGED_NOT_ACTIVATED",
        "manifest_path": str(manifest_path.expanduser().resolve(strict=True)),
        "manifest_digest": manifest["manifest_digest"],
        "shared_root": str(target),
        "verified_sources": verified_sources,
        "target_scans": target_scans,
        "file_count": len(files),
        "source_paths_mutated": False,
        "activation_performed": False,
        "destructive_cleanup_performed": False,
        "rollback": {
            "source_profiles_remain_authoritative": True,
            "staged_target_must_not_be_deleted_if_it_later_contains_new_sessions": True,
        },
    }
    report["stage_digest"] = _digest(report)
    return report
