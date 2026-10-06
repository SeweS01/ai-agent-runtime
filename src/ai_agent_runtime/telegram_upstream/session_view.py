"""Read-only transcript projection for an isolated upstream Telegram Agent HOME.

Codex keeps credentials and its own index under each account's CODEX_HOME,
while pinned upstream discovers TUI rollout files under HOME/.codex/sessions.
This adapter projects only rollout JSONL files as symlinks.  It never copies or
merges auth, config, SQLite, or index files, and never changes source storage.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

_DATE_PART = re.compile(r"^[0-9]{2,4}$")
_ROLLOUT_NAME = re.compile(r"^rollout-[A-Za-z0-9T:._-]+\.jsonl$")
_MAX_ROLLOUTS = 250_000


class SessionViewError(ValueError):
    """A transcript view cannot be built without risking wrong-session routing."""


@dataclass(frozen=True, slots=True)
class SessionViewResult:
    sources: int
    added: int
    existing: int


def _owned_directory(path: Path, *, label: str, private: bool = False) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SessionViewError(f"{label} is unavailable: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or (private and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
    ):
        raise SessionViewError(f"{label} has unsafe type, owner, or permissions: {path}")


def _rollout_sources(sessions: Path) -> list[tuple[Path, Path]]:
    # A newly activated account has no sessions directory until its first Codex
    # launch. Treat only that absent directory as an empty source; an existing
    # symlink or an unsafe node must still fail closed.
    if not sessions.exists() and not sessions.is_symlink():
        return []
    _owned_directory(sessions, label="profile sessions directory")
    result: list[tuple[Path, Path]] = []
    for path in sessions.glob("*/*/*/rollout-*.jsonl"):
        relative = path.relative_to(sessions)
        if (
            len(relative.parts) != 4
            or not all(_DATE_PART.fullmatch(part) for part in relative.parts[:3])
            or not _ROLLOUT_NAME.fullmatch(relative.name)
        ):
            continue
        current = sessions
        for part in relative.parts[:3]:
            current = current / part
            _owned_directory(current, label="profile sessions date directory")
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise SessionViewError(f"rollout vanished during projection: {path}") from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise SessionViewError(f"rollout has unsafe type or owner: {path}")
        result.append((relative, path))
        if len(result) > _MAX_ROLLOUTS:
            raise SessionViewError("rollout view exceeds file limit")
    return result


def _view_parent(root: Path, relative: Path) -> Path:
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if not current.exists() and not current.is_symlink():
            current.mkdir(mode=0o700)
        _owned_directory(current, label="transcript view directory", private=True)
    return current


def _preflight_view_parent(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if not current.exists() and not current.is_symlink():
            return
        _owned_directory(current, label="transcript view directory", private=True)


def project_session_transcripts(*, profiles: dict[str, Path], view_root: Path) -> SessionViewResult:
    """Expose rollout files to one upstream instance without merging account homes.

    Existing paths are never replaced. Filename collisions between profiles
    fail before any link is created. A later call adds newly created rollouts.
    """

    if not profiles:
        raise SessionViewError("at least one account profile is required")
    if not view_root.is_absolute() or view_root == Path("/") or ".." in view_root.parts:
        raise SessionViewError("transcript view root must be an absolute non-root path")
    _owned_directory(view_root, label="transcript view root", private=True)
    planned: dict[Path, Path] = {}
    for name, profile in sorted(profiles.items()):
        if not name or name in {".", ".."} or "/" in name:
            raise SessionViewError("account profile name is unsafe")
        _owned_directory(profile, label="account profile", private=True)
        for relative, source in _rollout_sources(profile / "sessions"):
            previous = planned.setdefault(relative, source)
            if previous != source:
                raise SessionViewError(f"rollout filename collision: {relative}")
            if len(planned) > _MAX_ROLLOUTS:
                raise SessionViewError("rollout view exceeds file limit")

    # Preflight all existing destinations before writing any link.
    existing = 0
    for relative, source in planned.items():
        _preflight_view_parent(view_root, relative)
        target = view_root / relative
        if not target.exists() and not target.is_symlink():
            continue
        try:
            matches_source = target.is_symlink() and target.resolve(strict=True) == source.resolve(
                strict=True
            )
        except OSError:
            matches_source = False
        if not matches_source:
            raise SessionViewError(f"transcript view target collision: {target}")
        existing += 1

    added = 0
    for relative, source in planned.items():
        parent = _view_parent(view_root, relative)
        target = parent / relative.name
        if target.is_symlink():
            continue
        try:
            target.symlink_to(source)
        except FileExistsError as exc:
            raise SessionViewError(f"transcript view target changed: {target}") from exc
        added += 1
    return SessionViewResult(sources=len(planned), added=added, existing=existing)
