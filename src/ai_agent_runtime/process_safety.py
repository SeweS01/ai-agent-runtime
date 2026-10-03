"""Linux process and filesystem checks used at the execution boundary."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from ai_agent_runtime.errors import ErrorCode, RuntimeFailureError


def proc_starttime(pid: int) -> int:
    """Return Linux /proc starttime without misparsing spaces in comm."""

    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    closing = raw.rfind(")")
    if closing < 0:
        raise ValueError("malformed /proc stat")
    fields_after_comm = raw[closing + 2 :].split()
    return int(fields_after_comm[19])


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def validate_owned_executable(path: Path, *, uid: int | None = None) -> Path:
    expected_uid = os.getuid() if uid is None else uid
    if not path.is_absolute():
        raise RuntimeFailureError(ErrorCode.UNSAFE_BINARY, "real Codex path must be absolute")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeFailureError(ErrorCode.UNSAFE_BINARY, "real Codex is not a regular file")
    if metadata.st_uid not in {0, expected_uid}:
        raise RuntimeFailureError(ErrorCode.UNSAFE_BINARY, "real Codex has an unexpected owner")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeFailureError(ErrorCode.UNSAFE_BINARY, "real Codex is group/world writable")
    if not metadata.st_mode & stat.S_IXUSR:
        raise RuntimeFailureError(ErrorCode.UNSAFE_BINARY, "real Codex is not owner-executable")
    return resolved


def validate_profile_path(path: Path, *, root: Path, uid: int | None = None) -> Path:
    expected_uid = os.getuid() if uid is None else uid
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise RuntimeFailureError(ErrorCode.UNSAFE_PROFILE, "CODEX_HOME escapes profiles root")
    metadata = resolved.stat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeFailureError(ErrorCode.UNSAFE_PROFILE, "CODEX_HOME is not a directory")
    if metadata.st_uid != expected_uid:
        raise RuntimeFailureError(ErrorCode.UNSAFE_PROFILE, "CODEX_HOME has an unexpected owner")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeFailureError(ErrorCode.UNSAFE_PROFILE, "CODEX_HOME is group/world writable")
    return resolved


def validate_project_path(path: Path, *, allowed_roots: tuple[Path, ...]) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("project cwd is not a directory")
    roots = tuple(root.resolve(strict=True) for root in allowed_roots)
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise ValueError("project cwd is outside allowed project roots")
    return resolved


def profile_fingerprint(path: Path) -> str:
    """Fingerprint profile identity/metadata without reading credential content."""

    resolved = path.resolve(strict=True)
    profile_stat = resolved.stat()
    auth_path = resolved / "auth.json"
    auth_metadata: tuple[int, int, int, int] | None = None
    if auth_path.exists():
        auth_stat = auth_path.stat(follow_symlinks=False)
        if stat.S_ISLNK(auth_stat.st_mode):
            raise RuntimeFailureError(ErrorCode.UNSAFE_PROFILE, "auth.json must not be a symlink")
        auth_metadata = (
            auth_stat.st_uid,
            stat.S_IMODE(auth_stat.st_mode),
            auth_stat.st_size,
            auth_stat.st_mtime_ns,
        )
    material = repr(
        (
            str(resolved),
            profile_stat.st_dev,
            profile_stat.st_ino,
            profile_stat.st_uid,
            stat.S_IMODE(profile_stat.st_mode),
            auth_metadata,
        )
    ).encode()
    return f"sha256:{hashlib.sha256(material).hexdigest()}"
