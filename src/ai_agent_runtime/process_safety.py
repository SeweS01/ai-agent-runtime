"""Linux process and filesystem checks used at the execution boundary."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ai_agent_runtime.domain import WrapperIdentity
from ai_agent_runtime.errors import ErrorCode, RuntimeFailureError


def proc_starttime(pid: int) -> int:
    """Return Linux /proc starttime without misparsing spaces in comm."""

    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    closing = raw.rfind(")")
    if closing < 0:
        raise ValueError("malformed /proc stat")
    fields_after_comm = raw[closing + 2 :].split()
    return int(fields_after_comm[19])


def proc_environment(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    environment: dict[str, str] = {}
    for item in raw.split(b"\0"):
        key, separator, value = item.partition(b"=")
        if separator:
            environment[key.decode(errors="strict")] = value.decode(errors="strict")
    return environment


def proc_uid(pid: int) -> int:
    return Path(f"/proc/{pid}").stat().st_uid


@dataclass(frozen=True, slots=True)
class TmuxPaneProcess:
    pane_id: str
    session_name: str
    pane_pid: int


def _tmux_socket_from_environment(value: str) -> Path:
    socket_value, separator, _remainder = value.partition(",")
    if not separator or not socket_value:
        raise RuntimeFailureError(ErrorCode.AMBIGUOUS, "malformed TMUX environment")
    socket_path = Path(socket_value)
    if not socket_path.is_absolute() or ".." in socket_path.parts:
        raise RuntimeFailureError(ErrorCode.AMBIGUOUS, "unsafe tmux socket path")
    metadata = socket_path.stat()
    parent = socket_path.parent.stat()
    if not stat.S_ISSOCK(metadata.st_mode):
        raise RuntimeFailureError(ErrorCode.AMBIGUOUS, "TMUX path is not a socket")
    if metadata.st_uid != os.getuid() or parent.st_uid != os.getuid():
        raise RuntimeFailureError(ErrorCode.AMBIGUOUS, "tmux socket ownership mismatch")
    if parent.st_mode & stat.S_IWOTH:
        raise RuntimeFailureError(ErrorCode.AMBIGUOUS, "tmux socket directory is world writable")
    return socket_path


def inspect_tmux_pane(socket_path: Path, pane_id: str) -> TmuxPaneProcess:
    completed = subprocess.run(
        (
            "tmux",
            "-S",
            str(socket_path),
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}\t#{session_name}\t#{pane_pid}",
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if completed.returncode != 0:
        raise RuntimeFailureError(ErrorCode.AMBIGUOUS, "cannot inspect tmux panes")
    matches: list[TmuxPaneProcess] = []
    for line in completed.stdout.splitlines():
        raw_pane, separator, remainder = line.partition("\t")
        session_name, second_separator, raw_pid = remainder.partition("\t")
        if not separator or not second_separator or raw_pane != pane_id:
            continue
        try:
            pane_pid = int(raw_pid)
        except ValueError:
            continue
        matches.append(TmuxPaneProcess(raw_pane, session_name, pane_pid))
    if len(matches) != 1:
        raise RuntimeFailureError(
            ErrorCode.AMBIGUOUS,
            f"expected one matching tmux pane, found {len(matches)}",
        )
    return matches[0]


def verify_wrapper_identity(identity: WrapperIdentity) -> None:
    if proc_uid(identity.pid) != identity.uid or identity.uid != os.getuid():
        raise RuntimeFailureError(ErrorCode.AMBIGUOUS, "wrapper process UID mismatch")
    if proc_starttime(identity.pid) != identity.starttime:
        raise RuntimeFailureError(ErrorCode.AMBIGUOUS, "wrapper process starttime mismatch")
    actual_cwd = Path(f"/proc/{identity.pid}/cwd").resolve(strict=True)
    if actual_cwd != Path(identity.cwd).resolve(strict=True):
        raise RuntimeFailureError(ErrorCode.AMBIGUOUS, "wrapper process cwd mismatch")
    environment = proc_environment(identity.pid)
    if environment.get("TMUX_PANE") != identity.tmux_pane:
        raise RuntimeFailureError(ErrorCode.AMBIGUOUS, "wrapper TMUX_PANE mismatch")
    tmux_value = environment.get("TMUX")
    if not tmux_value:
        raise RuntimeFailureError(ErrorCode.AMBIGUOUS, "wrapper has no TMUX environment")
    pane = inspect_tmux_pane(_tmux_socket_from_environment(tmux_value), identity.tmux_pane)
    if pane.session_name != identity.tmux_session or pane.pane_pid != identity.pid:
        raise RuntimeFailureError(
            ErrorCode.AMBIGUOUS,
            "tmux pane/session/process identity mismatch",
        )


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
