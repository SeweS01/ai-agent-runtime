"""Codex CLI classification and managed argument rewriting."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class InvocationKind(StrEnum):
    START = "start"
    RESUME = "resume"
    SAFE_LOCAL = "safe_local"
    DENIED_ADMIN = "denied_admin"


_SAFE_FLAGS = {"--help", "-h", "--version", "-V"}
_DENIED_COMMANDS = {"login", "logout", "update"}
_REASONING_KEYS = {"model_reasoning_effort", "model_reasoning_summary"}
_CODEX_SESSION_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


@dataclass(frozen=True, slots=True)
class ParsedInvocation:
    kind: InvocationKind
    argv: tuple[str, ...]
    resume_session_id: str | None = None


def parse_invocation(argv: list[str]) -> ParsedInvocation:
    if argv and argv[0] in _SAFE_FLAGS:
        return ParsedInvocation(InvocationKind.SAFE_LOCAL, tuple(argv))
    if argv and argv[0] in _DENIED_COMMANDS:
        return ParsedInvocation(InvocationKind.DENIED_ADMIN, tuple(argv))
    if argv and argv[0] == "resume":
        # The pinned upstream inserts MCP `-c key=value` arguments *before*
        # the positional session ID.  A first-non-option scan would treat an
        # MCP value as the session and falsely reject an owned transcript.
        # Upstream only accepts explicit Codex UUIDv7 session IDs here.
        candidates = [arg for arg in argv[1:] if _CODEX_SESSION_ID.fullmatch(arg)]
        session_id = candidates[0] if len(candidates) == 1 else None
        return ParsedInvocation(InvocationKind.RESUME, tuple(argv), session_id)
    return ParsedInvocation(InvocationKind.START, tuple(argv))


def rewrite_managed_args(argv: list[str], *, model: str, reasoning_effort: str) -> list[str]:
    """Replace only Runtime-owned model/reasoning arguments.

    Unknown options and unrelated `-c key=value` values retain their exact order.
    """

    rewritten: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--model":
            if index + 1 >= len(argv):
                raise ValueError("--model requires a value")
            index += 2
            continue
        if token.startswith("--model="):
            index += 1
            continue
        if token == "-c":
            if index + 1 >= len(argv):
                raise ValueError("-c requires key=value")
            value = argv[index + 1]
            key = value.partition("=")[0]
            if key not in _REASONING_KEYS:
                rewritten.extend((token, value))
            index += 2
            continue
        rewritten.append(token)
        index += 1
    rewritten.extend(("--model", model, "-c", f"model_reasoning_effort={reasoning_effort}"))
    return rewritten
