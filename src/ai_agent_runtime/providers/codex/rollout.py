"""Metadata-only parser for version-qualified Codex rollout events."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ai_agent_runtime.domain import sha256_digest

QUALIFIED_CODEX_VERSIONS = frozenset({"codex-cli 0.156.1"})


class RolloutSignalKind(StrEnum):
    TURN_STARTED = "TURN_STARTED"
    TURN_CONTEXT = "TURN_CONTEXT"
    TURN_COMPLETED = "TURN_COMPLETED"


@dataclass(frozen=True, slots=True)
class RolloutSignal:
    kind: RolloutSignalKind
    end_offset: int
    turn_id: str | None
    model: str | None = None
    reasoning_effort: str | None = None
    evidence_digest: str | None = None


@dataclass(frozen=True, slots=True)
class RolloutScan:
    next_offset: int
    signals: tuple[RolloutSignal, ...]
    at_eof: bool


class RolloutContractError(ValueError):
    pass


def scan_rollout(
    path: Path,
    *,
    codex_version: str,
    start_offset: int,
    expected_device: int,
    expected_inode: int,
    max_bytes: int = 4 * 1024 * 1024,
    max_line_bytes: int = 2 * 1024 * 1024,
) -> RolloutScan:
    """Read only complete JSONL records and retain no prompt/transcript content."""

    if codex_version not in QUALIFIED_CODEX_VERSIONS:
        raise RolloutContractError(f"unqualified Codex rollout version: {codex_version}")
    if start_offset < 0 or max_bytes < 1 or max_line_bytes < 1:
        raise RolloutContractError("invalid rollout scan bounds")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RolloutContractError("rollout must be an owned regular file")
        if metadata.st_dev != expected_device or metadata.st_ino != expected_inode:
            raise RolloutContractError("rollout identity changed")
        if start_offset > metadata.st_size:
            raise RolloutContractError("rollout was truncated")
        os.lseek(fd, start_offset, os.SEEK_SET)
        chunk = os.read(fd, max_bytes)
        file_size = metadata.st_size
    finally:
        os.close(fd)
    complete_length = chunk.rfind(b"\n") + 1
    if complete_length == 0:
        if len(chunk) > max_line_bytes:
            raise RolloutContractError("rollout line exceeds maximum size")
        return RolloutScan(
            next_offset=start_offset,
            signals=(),
            at_eof=start_offset + len(chunk) >= file_size,
        )

    signals: list[RolloutSignal] = []
    consumed = 0
    for raw_line in chunk[:complete_length].splitlines(keepends=True):
        consumed += len(raw_line)
        if len(raw_line) > max_line_bytes:
            raise RolloutContractError("rollout line exceeds maximum size")
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise RolloutContractError("invalid complete rollout JSON record") from exc
        signal = _signal_from_record(record, end_offset=start_offset + consumed)
        if signal is not None:
            signals.append(signal)
    return RolloutScan(
        next_offset=start_offset + complete_length,
        signals=tuple(signals),
        at_eof=start_offset + complete_length >= file_size,
    )


def _signal_from_record(record: object, *, end_offset: int) -> RolloutSignal | None:
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if record.get("type") == "event_msg" and isinstance(payload, dict):
        event_type = payload.get("type")
        turn_id = _optional_string(payload.get("turn_id"))
        if event_type in {"task_started", "turn_started"}:
            return RolloutSignal(
                kind=RolloutSignalKind.TURN_STARTED,
                end_offset=end_offset,
                turn_id=turn_id,
            )
        if event_type in {"task_complete", "turn_complete"}:
            return RolloutSignal(
                kind=RolloutSignalKind.TURN_COMPLETED,
                end_offset=end_offset,
                turn_id=turn_id,
            )
        return None
    if record.get("type") != "turn_context" or not isinstance(payload, dict):
        return None
    model = _optional_string(payload.get("model"))
    effort = _optional_string(payload.get("effort"))
    turn_id = _optional_string(payload.get("turn_id"))
    if model is None or effort is None or turn_id is None:
        raise RolloutContractError("turn_context lacks qualified model/effort/turn identity")
    evidence = {
        "end_offset": end_offset,
        "event_type": "turn_context",
        "model": model,
        "reasoning_effort": effort,
        "turn_id": turn_id,
    }
    return RolloutSignal(
        kind=RolloutSignalKind.TURN_CONTEXT,
        end_offset=end_offset,
        turn_id=turn_id,
        model=model,
        reasoning_effort=effort,
        evidence_digest=sha256_digest(evidence),
    )


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
