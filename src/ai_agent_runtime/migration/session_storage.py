"""Non-destructive session-storage topology manifest and collision gate."""

from __future__ import annotations

import hashlib
import os
import stat
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import cast

from ai_agent_runtime.migration.inventory import InventoryError, _digest

SESSION_STORAGE_SCHEMA = "ai-agent-runtime.session-storage-manifest.v1"
DEFAULT_INCLUDES = ("sessions", "archived_sessions")
_FORBIDDEN_NAMES = {
    ".env",
    "auth.json",
    "credentials",
    "credentials.json",
    "config.toml",
}
_MAX_NODES = 250_000


def _relative_path(value: str) -> PurePosixPath:
    candidate = PurePosixPath(value)
    if (
        not value
        or candidate.is_absolute()
        or ".." in candidate.parts
        or "." in candidate.parts
    ):
        raise InventoryError(f"session storage include is unsafe: {value}")
    if any(part in _FORBIDDEN_NAMES for part in candidate.parts):
        raise InventoryError(f"session storage include targets credential material: {value}")
    return candidate


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
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
