"""Stable runtime error codes shared by CLI, IPC, and operators."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    UNKNOWN_BINDING = "UNKNOWN_BINDING"
    AMBIGUOUS = "AMBIGUOUS"
    CONFIG_DRIFT = "CONFIG_DRIFT"
    CORRELATION_MISMATCH = "CORRELATION_MISMATCH"
    RESUME_BINDING_MISMATCH = "RESUME_BINDING_MISMATCH"
    UNSUPPORTED_CODEX_SUBPROCESS = "UNSUPPORTED_CODEX_SUBPROCESS"
    RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
    PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"
    INVALID_REQUEST = "INVALID_REQUEST"
    UNSAFE_PROFILE = "UNSAFE_PROFILE"
    UNSAFE_BINARY = "UNSAFE_BINARY"
    SESSION_VIEW_UNAVAILABLE = "SESSION_VIEW_UNAVAILABLE"
    GENERATION_RECONCILE_REQUIRED = "GENERATION_RECONCILE_REQUIRED"
    PROJECT_RELOCATION_REQUIRED = "PROJECT_RELOCATION_REQUIRED"


@dataclass(slots=True)
class RuntimeFailureError(Exception):
    code: ErrorCode
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"
